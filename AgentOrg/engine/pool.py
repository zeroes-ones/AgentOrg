#!/usr/bin/env python3
"""pool.py — the task pool: work that agents pull, rather than work pushed at them.

WHY THIS EXISTS
---------------
The engine's model is push: the orchestrator binds a node to an agent and the runner executes it. That
is the right default — it makes a run reproducible and its bindings auditable — but it has two limits
the reference architecture (`agent-swarm`) answers differently and well:

- **A graph is planned in advance.** Push assumes the planner knew every task up front. Real work
  discovers tasks: a reviewer finds a missing migration, a developer needs a spike. There was nowhere
  to *put* such work except a hand-authored manifest.
- **An agent is chosen by a rule, not by capability and availability.** Push binds by score at plan
  time; it cannot let a pool fill while one agent is busy and another is idle.

This module is the pool that closes both gaps, and it is deliberately **additive**: a graph still runs
push-style, and a pool is what a node (or an agent, mid-run) can create work in. Nothing about the
push path changes, so every existing guarantee — reviewer independence, evidence-gated completion,
bounded loops — still holds for the work that flows through it.

DESIGN
------
- **Claim, don't assign.** A task sits in the pool unassigned; a worker claims it. That is what makes
  capability and availability decide rather than a static score, and it is the reference's core idea
  transplanted into an engine that already has a richer notion of *who* can do what.
- **Offers are first-class.** `offer` puts a task in front of one agent, who accepts or rejects. The
  difference matters: a claim is anonymous and fast, an offer carries accountability and a refusal
  reason — and a refusal is information the pool keeps.
- **A lease, not a lock.** A claim carries a deadline. A worker that dies holding a task does not
  strand it forever, which is the failure mode a plain "assigned" flag produces.
- **Capability-gated.** `required_skills` and `required_capabilities` are checked at claim time
  against the roster, so a task cannot be taken by an agent that cannot do it.
- **Schema-checked results.** A task may declare an `output_schema`; a completion whose output does
  not match is rejected, so a caller that needs structured JSON gets it or gets a refusal.
- **Durable and file-backed.** The pool survives a restart like every other run artifact. An
  in-memory pool would lose exactly the work that was hardest to describe.

Usage:
    pool = TaskPool(path=workspace.pool_path)
    task = pool.create("add a migration for the new column", required_skills=["database-designer"],
                       output_schema={"type": "object", "required": ["file"]})
    claimed = pool.claim(agent)                      # None when nothing is eligible
    pool.complete(claimed.id, agent, output='{"file": "migrations/001.sql"}')
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = ["TaskPool", "PoolTask", "PoolError", "TaskState", "validate_output"]


class PoolError(RuntimeError):
    """A pool operation that cannot be honoured, named so the caller can act on it."""


class TaskState:
    """The states a pooled task moves through. Plain strings, because they are persisted."""

    POOL = "pool"            # unassigned, claimable
    OFFERED = "offered"      # in front of one agent, awaiting accept/reject
    CLAIMED = "claimed"      # taken, with a lease
    DONE = "done"
    FAILED = "failed"
    BACKLOG = "backlog"      # deliberately parked, not claimable


#: How long a claim is valid without a progress heartbeat. Long enough for a slow local model, short
#: enough that a dead worker's task returns to the pool within one useful interval.
DEFAULT_LEASE_S = 900.0


@dataclass
class PoolTask:
    """One unit of claimable work."""

    id: str
    description: str
    state: str = TaskState.POOL
    priority: int = 50
    required_skills: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    #: JSON Schema the completion's output must satisfy. None means any text is acceptable.
    output_schema: dict[str, Any] | None = None
    #: Free-form tags, for filtering and for a human reading the pool.
    tags: list[str] = field(default_factory=list)
    #: The task this one came from, so a decomposed tree stays reconstructable.
    parent_id: str | None = None
    #: Tasks that must finish first. Checked at claim time.
    depends_on: list[str] = field(default_factory=list)
    claimed_by: str | None = None
    offered_to: str | None = None
    lease_until: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    output: str = ""
    failure_reason: str = ""
    #: Who refused it and why, kept because a refusal is information.
    refusals: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "description": self.description, "state": self.state,
            "priority": self.priority, "required_skills": list(self.required_skills),
            "required_capabilities": list(self.required_capabilities),
            "output_schema": self.output_schema, "tags": list(self.tags),
            "parent_id": self.parent_id, "depends_on": list(self.depends_on),
            "claimed_by": self.claimed_by, "offered_to": self.offered_to,
            "lease_until": self.lease_until, "created_at": self.created_at,
            "updated_at": self.updated_at, "output": self.output,
            "failure_reason": self.failure_reason, "refusals": list(self.refusals),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PoolTask":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def lease_expired(self, *, now: float | None = None) -> bool:
        return self.lease_until is not None and (now or time.time()) > self.lease_until


class TaskPool:
    """A durable, capability-routed pool of claimable work.

    Parameters
    ----------
    path:
        Where the pool is stored. One JSON document, rewritten atomically — a pool is small and the
        alternative (an append-only log) needs a compaction policy for no benefit at this scale.
    now:
        Injectable clock, so lease expiry is testable without sleeping.
    """

    def __init__(self, path: Path | str | None = None, *, now: Any = time.time) -> None:
        self.path = Path(path) if path is not None else None
        self._now = now
        self.tasks: dict[str, PoolTask] = {}
        if self.path is not None and self.path.is_file():
            self._load()

    # ── persistence ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PoolError(f"task pool at {self.path} is unreadable: {exc}") from exc
        self.tasks = {str(t["id"]): PoolTask.from_dict(t)
                      for t in document.get("tasks") or []}

    def save(self) -> None:
        """Write the pool atomically. A torn write would lose the hardest-to-describe work."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {"tasks": [t.as_dict() for t in sorted(self.tasks.values(),
                                                          key=lambda t: t.id)]}
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # ── creating work ───────────────────────────────────────────────────────

    def create(self, description: str, *, priority: int = 50,
               required_skills: Iterable[str] = (), required_capabilities: Iterable[str] = (),
               output_schema: dict[str, Any] | None = None, tags: Iterable[str] = (),
               parent_id: str | None = None, depends_on: Iterable[str] = (),
               task_id: str | None = None) -> PoolTask:
        """Add unassigned work to the pool.

        `required_skills` is what makes the pool *routable* rather than a queue: a task that names a
        skill can only be claimed by an agent that holds it, so capability decides who does the work.
        """
        if not str(description).strip():
            raise PoolError("a pooled task needs a description")
        for parent in [parent_id, *depends_on]:
            if parent and parent not in self.tasks:
                raise PoolError(f"pooled task references unknown task {parent!r}")
        task = PoolTask(
            id=task_id or f"task_{uuid.uuid4().hex[:10]}",
            description=str(description).strip(),
            priority=max(0, min(100, int(priority))),
            required_skills=[str(s) for s in required_skills],
            required_capabilities=[str(c) for c in required_capabilities],
            output_schema=output_schema,
            tags=[str(t) for t in tags],
            parent_id=parent_id,
            depends_on=[str(d) for d in depends_on],
        )
        self.tasks[task.id] = task
        self.save()
        return task

    # ── eligibility and claiming ────────────────────────────────────────────

    def _blocked_by_dependencies(self, task: PoolTask) -> list[str]:
        return [d for d in task.depends_on
                if d in self.tasks and self.tasks[d].state not in (TaskState.DONE,)]

    def eligible(self, agent: Any, *, now: float | None = None) -> list[PoolTask]:
        """Every task this agent may claim, best-first.

        The filter is the whole point of a pool: a task is offered to a *capable* agent, and an
        agent is never handed work it cannot do. Unfinished dependencies and an unexpired lease held
        by someone else both make a task ineligible, so two workers cannot do the same thing.

        Expired leases are reclaimed first, so a read never reports a task as held when it is in fact
        claimable — the kind of stale answer that makes a pool view untrustworthy.
        """
        self._expire_leases()
        moment = now or self._now()
        out: list[PoolTask] = []
        for task in self.tasks.values():
            if task.state != TaskState.POOL:
                continue
            if self._blocked_by_dependencies(task):
                continue
            if not self._agent_can(agent, task):
                continue
            out.append(task)
        # Highest priority first, then oldest, so a tie cannot starve an early task indefinitely.
        return sorted(out, key=lambda t: (-t.priority, t.created_at))

    def _agent_can(self, agent: Any, task: PoolTask) -> bool:
        """Whether the agent holds every skill and capability the task requires."""
        skills = set(getattr(agent, "skills", []) or [])
        if any(s not in skills for s in task.required_skills):
            return False
        have = set(getattr(agent, "capabilities", []) or [])
        for needed in task.required_capabilities:
            if needed in have:
                continue
            # `read:*` grants `read:src/**`; a wildcard scope covers its namespace.
            namespace = needed.split(":", 1)[0]
            if f"{namespace}:*" in have:
                continue
            return False
        return True

    def claim(self, agent: Any, *, task_id: str | None = None,
              lease_s: float = DEFAULT_LEASE_S) -> PoolTask | None:
        """Take the best eligible task, or a specific one. Returns None when nothing is claimable.

        None rather than raising, because "there is nothing for me right now" is the normal state of a
        pull-based worker — not an error. The caller distinguishes it from failure by the absence of
        an exception.
        """
        self._expire_leases()
        agent_id = getattr(agent, "id", str(agent))
        if task_id is not None:
            task = self.tasks.get(task_id)
            if task is None:
                raise PoolError(f"no pooled task {task_id!r}")
            if task.state != TaskState.POOL:
                raise PoolError(f"task {task_id!r} is {task.state}, not claimable")
            if self._blocked_by_dependencies(task):
                raise PoolError(f"task {task_id!r} is blocked by unfinished dependencies")
            if not self._agent_can(agent, task):
                raise PoolError(
                    f"agent {agent_id!r} does not satisfy task {task_id!r}: needs "
                    f"{task.required_skills or []} / {task.required_capabilities or []}"
                )
        else:
            candidates = self.eligible(agent)
            if not candidates:
                return None
            task = candidates[0]

        task.state = TaskState.CLAIMED
        task.claimed_by = agent_id
        task.offered_to = None
        task.lease_until = self._now() + float(lease_s)
        task.updated_at = self._now()
        self.save()
        return task

    def release(self, task_id: str, *, reason: str = "") -> PoolTask:
        """Return a claimed task to the pool, so a worker that cannot finish it is not a dead end."""
        task = self._require(task_id)
        if task.state not in (TaskState.CLAIMED, TaskState.OFFERED):
            raise PoolError(f"task {task_id!r} is {task.state}, not held")
        task.state = TaskState.POOL
        task.claimed_by = None
        task.offered_to = None
        task.lease_until = None
        task.updated_at = self._now()
        if reason:
            task.refusals.append({"agent": task.claimed_by, "reason": reason,
                                  "at": self._now()})
        self.save()
        return task

    def _expire_leases(self) -> list[str]:
        """Return every task whose lease lapsed to the pool.

        This is what stops a worker that died mid-task from stranding it forever — the failure mode a
        plain "assigned" flag produces.
        """
        expired: list[str] = []
        for task in self.tasks.values():
            if task.state == TaskState.CLAIMED and task.lease_expired(now=self._now()):
                task.state = TaskState.POOL
                task.claimed_by = None
                task.lease_until = None
                task.updated_at = self._now()
                expired.append(task.id)
        if expired:
            self.save()
        return expired

    # ── offers ──────────────────────────────────────────────────────────────

    def offer(self, task_id: str, agent_id: str) -> PoolTask:
        """Put a task in front of one agent, who must accept or refuse it.

        Distinct from a claim on purpose: a claim is anonymous and fast, an offer carries
        accountability and a refusal reason.
        """
        task = self._require(task_id)
        if task.state not in (TaskState.POOL, TaskState.BACKLOG):
            raise PoolError(f"task {task_id!r} is {task.state}, not offerable")
        task.state = TaskState.OFFERED
        task.offered_to = str(agent_id)
        task.updated_at = self._now()
        self.save()
        return task

    def accept(self, task_id: str, agent: Any, *, lease_s: float = DEFAULT_LEASE_S) -> PoolTask:
        task = self._require(task_id)
        agent_id = getattr(agent, "id", str(agent))
        if task.state != TaskState.OFFERED:
            raise PoolError(f"task {task_id!r} is {task.state}, not offered")
        if task.offered_to != agent_id:
            raise PoolError(f"task {task_id!r} was offered to {task.offered_to!r}, not {agent_id!r}")
        if not self._agent_can(agent, task):
            raise PoolError(f"agent {agent_id!r} does not satisfy task {task_id!r}")
        task.state = TaskState.CLAIMED
        task.claimed_by = agent_id
        task.lease_until = self._now() + float(lease_s)
        task.updated_at = self._now()
        self.save()
        return task

    def reject(self, task_id: str, agent: Any, *, reason: str) -> PoolTask:
        """Refuse an offer, recording why. The reason is the point — a silent refusal teaches nothing."""
        task = self._require(task_id)
        agent_id = getattr(agent, "id", str(agent))
        if task.state != TaskState.OFFERED or task.offered_to != agent_id:
            raise PoolError(f"task {task_id!r} is not offered to {agent_id!r}")
        task.refusals.append({"agent": agent_id, "reason": str(reason)[:400], "at": self._now()})
        task.state = TaskState.POOL
        task.offered_to = None
        task.updated_at = self._now()
        self.save()
        return task

    # ── completing ──────────────────────────────────────────────────────────

    def complete(self, task_id: str, agent: Any, *, output: str = "") -> PoolTask:
        """Finish a task, refusing a completion whose output does not match its schema.

        The schema check happens *before* the state change, so a rejected completion leaves the task
        claimed and the worker able to correct it — rather than marking it done with output a caller
        cannot parse.
        """
        task = self._require(task_id)
        agent_id = getattr(agent, "id", str(agent))
        if task.state != TaskState.CLAIMED or task.claimed_by != agent_id:
            raise PoolError(f"task {task_id!r} is not claimed by {agent_id!r}")
        if task.output_schema is not None:
            problem = validate_output(output, task.output_schema)
            if problem:
                raise PoolError(
                    f"completion of {task_id!r} does not match its output schema: {problem}"
                )
        task.state = TaskState.DONE
        task.output = output
        task.lease_until = None
        task.updated_at = self._now()
        self.save()
        return task

    def fail(self, task_id: str, agent: Any, *, reason: str) -> PoolTask:
        task = self._require(task_id)
        agent_id = getattr(agent, "id", str(agent))
        if task.state != TaskState.CLAIMED or task.claimed_by != agent_id:
            raise PoolError(f"task {task_id!r} is not claimed by {agent_id!r}")
        task.state = TaskState.FAILED
        task.failure_reason = str(reason)[:400]
        task.lease_until = None
        task.updated_at = self._now()
        self.save()
        return task

    def to_backlog(self, task_id: str) -> PoolTask:
        """Park a task deliberately, as opposed to it lapsing."""
        task = self._require(task_id)
        task.state = TaskState.BACKLOG
        task.claimed_by = None
        task.offered_to = None
        task.lease_until = None
        task.updated_at = self._now()
        self.save()
        return task

    def from_backlog(self, task_id: str) -> PoolTask:
        task = self._require(task_id)
        if task.state != TaskState.BACKLOG:
            raise PoolError(f"task {task_id!r} is {task.state}, not in the backlog")
        task.state = TaskState.POOL
        task.updated_at = self._now()
        self.save()
        return task

    # ── reporting ───────────────────────────────────────────────────────────

    def _require(self, task_id: str) -> PoolTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise PoolError(f"no pooled task {task_id!r}")
        return task

    def summary(self) -> dict[str, Any]:
        """Counts by state, for the pool view. Includes `claimable`, which is what a worker asks.

        Expired leases are reclaimed first for the same reason `eligible` does it: a count that says
        "1 claimed" when the holder is gone is a count nobody can act on.
        """
        self._expire_leases()
        counts: dict[str, int] = {}
        for task in self.tasks.values():
            counts[task.state] = counts.get(task.state, 0) + 1
        return {
            "total": len(self.tasks),
            "by_state": counts,
            "claimable": counts.get(TaskState.POOL, 0),
            "claimed": counts.get(TaskState.CLAIMED, 0),
            "offered": counts.get(TaskState.OFFERED, 0),
            "done": counts.get(TaskState.DONE, 0),
            "failed": counts.get(TaskState.FAILED, 0),
        }

    def children(self, task_id: str) -> list[PoolTask]:
        """Tasks created from another — how a decomposed tree is walked."""
        return sorted((t for t in self.tasks.values() if t.parent_id == task_id),
                      key=lambda t: t.created_at)


def validate_output(output: str, schema: dict[str, Any]) -> str:
    """Check `output` against `schema`. Returns "" when valid, or the reason it is not.

    A deliberate subset — `type`, `required`, `properties`, `enum`, `const`, `items`, plus
    nested objects — because the supported keywords are the ones a task author actually reaches for,
    and an unsupported keyword is **reported** rather than silently ignored. A schema validator that
    quietly skips the check it did not understand is worse than none: it returns "valid" for output
    nobody verified.
    """
    text = str(output or "").strip()
    if not text:
        return "output is empty"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"output is not JSON ({exc.msg} at line {exc.lineno})"

    problem = _check(parsed, schema, path="$")
    return problem or ""


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
          "number": (int, float), "integer": int, "null": type(None)}


def _check(value: Any, schema: Any, *, path: str) -> str:
    """Recursive schema check. Returns "" when the value conforms."""
    if not isinstance(schema, dict):
        return ""
    for keyword in schema:
        if keyword not in ("type", "required", "properties", "enum", "const", "items",
                           "description", "title"):
            return f"{path}: unsupported schema keyword {keyword!r} — refusing rather than skipping it"

    if "const" in schema and value != schema["const"]:
        return f"{path}: expected the constant {schema['const']!r}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: {value!r} is not one of {schema['enum']}"
    if "type" in schema:
        expected = schema["type"]
        names = expected if isinstance(expected, list) else [expected]
        if not any(isinstance(value, _TYPES[n]) for n in names if n in _TYPES):
            return f"{path}: expected {expected}, got {type(value).__name__}"
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                return f"{path}: missing required property {key!r}"
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                problem = _check(value[key], sub, path=f"{path}.{key}")
                if problem:
                    return problem
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            problem = _check(item, schema["items"], path=f"{path}[{index}]")
            if problem:
                return problem
    return ""
