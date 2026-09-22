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

import contextlib
import functools
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:  # pragma: no cover - platform probe
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

__all__ = ["TaskPool", "PoolTask", "PoolError", "TaskState", "validate_output",
           "MIN_PRIORITY", "MAX_PRIORITY", "DEFAULT_PRIORITY"]


class PoolError(RuntimeError):
    """A pool operation that cannot be honoured, named so the caller can act on it."""


def _guarded(method: Any) -> Any:
    """Run a pool method inside `_entered()` — the locks plus a refresh from disk.

    Applied by decorator rather than by re-indenting every body, because the *point* is that no
    operation may escape the guard: a method someone adds later without it is exactly the race this
    closes, and a decorator makes the guard visible in one line at the definition site.
    """
    @functools.wraps(method)
    def wrapper(self: "TaskPool", *args: Any, **kwargs: Any) -> Any:
        with self._entered():
            return method(self, *args, **kwargs)
    return wrapper


class TaskState:
    """The states a pooled task moves through. Plain strings, because they are persisted."""

    POOL = "pool"            # unassigned, claimable
    OFFERED = "offered"      # in front of one agent, awaiting accept/reject
    CLAIMED = "claimed"      # taken, with a lease
    DONE = "done"
    FAILED = "failed"
    BACKLOG = "backlog"      # deliberately parked, not claimable

    @classmethod
    def all(cls) -> tuple[str, ...]:
        """Every state, in lifecycle order, read off this class rather than written out again.

        The vocabulary has a reader outside this module — the terminal's `pool list --state` filter,
        whose help named four of the six and whose flag accepted any typo as an empty list. A second
        copy of the list is what makes that possible; a caller that needs the states asks for them.
        """
        return tuple(value for name, value in vars(cls).items()
                     if not name.startswith("_") and isinstance(value, str))


#: The priority a task is created with, and the range it is clamped into. Named rather than written
#: into `__init__`'s signature and again into `create`'s clamp and again into the terminal's help: a
#: ceiling a surface restates is a ceiling that disagrees with the engine the first time it moves.
MIN_PRIORITY = 0
MAX_PRIORITY = 100
DEFAULT_PRIORITY = 50


#: How long a claim is valid without a progress heartbeat. Long enough for a slow local model, short
#: enough that a dead worker's task returns to the pool within one useful interval.
#:
#: "Heartbeat" means :meth:`TaskPool.renew`, and it is the *caller's* job to send it: before renewal
#: existed, this deadline was set once at claim and never extended, so a node that legitimately ran
#: longer than 900s had its task reclaimed and handed to a second worker while it was still working on
#: it — the same task executed twice, and the first worker's result refused as "not claimed by agent".
#: A worker doing long work renews; one that has died cannot.
DEFAULT_LEASE_S = 900.0


@dataclass
class PoolTask:
    """One unit of claimable work."""

    id: str
    description: str
    state: str = TaskState.POOL
    priority: int = DEFAULT_PRIORITY
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
    #: The last agent to hold this task, kept across a lapse. Without it a worker whose lease expired
    #: while it was still running could not prove it was the one running the task, so a late
    #: `renew` could either be refused (losing the claim) or accepted from anyone (a back door to a
    #: claim with no capability check).
    last_claimant: str = ""
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
            "lease_until": self.lease_until, "last_claimant": self.last_claimant,
            "created_at": self.created_at,
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

    Concurrency
    -----------
    Three writers can reach one pool, and all three used to be able to hand the same task to two
    workers:

    - **two threads on one instance** (two `parallel:` members both pulling from the pool) — a plain
      `eligible()` then mutate is a check-then-act race;
    - **two instances over one file** — the runner subprocess builds a pool over
      `.agent_state/pool.json` (`host.py:396`) while the orchestrator builds one over the same file
      (`serve.py:1159`), and each rewrote the whole document from its own copy, so one process's
      claim reverted the other's;
    - **the same worker twice** — a lease set once at claim with nothing to extend it, so a slow node
      outlived its own lease and was handed to a second worker mid-flight.

    So every operation runs inside `_entered()`: this process's `RLock`, then an `fcntl` lock on
    `<pool>.lock` for other processes, then a refresh from disk when another process has written
    since this instance last read or wrote. Renewal (`renew`) is the third half: the lease is a
    deadline the *holder* extends, which is what makes "the worker died" and "the worker is slow"
    different states instead of the same one.
    """

    def __init__(self, path: Path | str | None = None, *, now: Any = time.time) -> None:
        self.path = Path(path) if path is not None else None
        self._now = now
        self.tasks: dict[str, PoolTask] = {}
        #: Guards `self.tasks` for threads of this process. Reentrant because `claim` calls `eligible`
        #: and `_expire_leases`, and a helper that cannot be called from a guarded method forces one of
        #: the two to be wrong.
        self._lock = threading.RLock()
        #: `(mtime_ns, size)` of the document this instance last read or wrote. The whole document is
        #: rewritten on every save, so a write from another process silently reverts anything this
        #: instance is holding in memory unless it is noticed and re-read.
        self._stamp: tuple[int, int] | None = None
        #: Non-zero while an `flock` is held, which keeps a nested call from deadlocking against itself.
        self._file_depth = 0
        if self.path is not None and self.path.is_file():
            self._load()

    # ── concurrency ─────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _entered(self) -> Any:
        """Serialize an operation across threads and processes, and refresh a stale copy.

        Held for the *whole* operation rather than around the file write alone: the race is
        check-then-act (`eligible()` then mutate), and a lock that only covered the write would still
        let two workers both decide they may claim the same task.
        """
        with self._lock:
            with self._file_flock():
                self._sync()
                yield

    @contextlib.contextmanager
    def _file_flock(self) -> Any:
        """Hold an exclusive lock on `<pool>.lock` for the duration of the operation.

        `fcntl` is absent on Windows; there the in-process lock still applies and the cross-process
        case falls back to reload-before-mutate, which narrows the window without closing it. Saying
        so here rather than pretending is the point: a lock that silently degrades is worse than one
        whose limits are written down.
        """
        if self.path is None or fcntl is None:
            yield
            return
        if self._file_depth:
            # flock is per open file description, so taking it twice on two descriptions within one
            # process blocks against ourselves. Nested calls reuse the outer hold.
            self._file_depth += 1
            try:
                yield
            finally:
                self._file_depth -= 1
            return
        lock_path = self.path.with_name(self.path.name + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+")
        except OSError:
            # A lock file we cannot create is not a reason to refuse the work; the in-process lock and
            # the reload still apply.
            yield
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._file_depth = 1
            try:
                yield
            finally:
                self._file_depth = 0
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _sync(self) -> None:
        """Re-read the document when another process has written it since we last looked.

        Called under both locks, before every operation. Skipping this is how two processes each
        holding a copy of the whole document lose each other's work: the second to save overwrites the
        first's claim, offer or completion, and the first's `save()` had already reported success.
        """
        if self.path is None or not self.path.is_file():
            return
        try:
            stat = self.path.stat()
        except OSError:
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PoolError(f"task pool at {self.path} is unreadable: {exc}") from exc
        self.tasks = {str(t["id"]): PoolTask.from_dict(t)
                      for t in document.get("tasks") or []}
        self._stamp = self._file_stamp()

    def _file_stamp(self) -> tuple[int, int] | None:
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def save(self) -> None:
        """Write the pool atomically. A torn write would lose the hardest-to-describe work."""
        if self.path is None:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            document = {"tasks": [t.as_dict() for t in sorted(self.tasks.values(),
                                                              key=lambda t: t.id)]}
            # The temp name carries the pid (another process must not write over ours mid-rename) and a
            # short random suffix (two threads of one process must not either, which is what a bare pid
            # allowed).
            tmp = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}")
            tmp.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
            # Record what *we* wrote, so the next `_sync` does not mistake our own write for another
            # process's and re-read the file we just produced.
            self._stamp = self._file_stamp()

    # ── creating work ───────────────────────────────────────────────────────

    @_guarded
    def create(self, description: str, *, priority: int = DEFAULT_PRIORITY,
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
            priority=max(MIN_PRIORITY, min(MAX_PRIORITY, int(priority))),
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

    @_guarded
    def eligible(self, agent: Any, *, now: float | None = None) -> list[PoolTask]:
        """Every task this agent may claim, best-first.

        The filter is the whole point of a pool: a task is offered to a *capable* agent, and an
        agent is never handed work it cannot do. Unfinished dependencies and an unexpired lease held
        by someone else both make a task ineligible, so two workers cannot do the same thing.

        Expired leases are reclaimed first, so a read never reports a task as held when it is in fact
        claimable — the kind of stale answer that makes a pool view untrustworthy. `now` is the moment
        that judgement is made at, so a caller can ask about a hypothetical moment (a test, a plan)
        rather than only about this instant.
        """
        self._expire_leases(now=now)
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

    @_guarded
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
        task.last_claimant = agent_id
        task.offered_to = None
        task.lease_until = self._now() + float(lease_s)
        task.updated_at = self._now()
        self.save()
        return task

    @_guarded
    def renew(self, task_id: str, agent: Any, *, lease_s: float | None = None) -> PoolTask:
        """Extend the lease on a task this agent is still working on.

        This is the heartbeat `DEFAULT_LEASE_S` assumed and nothing provided. Without it a lease is a
        countdown from the claim that no live worker can reset, so a node that legitimately runs for
        longer than the lease has its task reclaimed by `_expire_leases`, handed to a second worker,
        and executed twice — while the first worker's `complete` then fails with "not claimed by
        agent" and its output is discarded as a warning.

        A renewal that arrives *after* the deadline but before anyone else has taken the task still
        restores the claim, because the alternative is refusing the one caller that can prove it was
        running the work (`last_claimant`). A renewal for a task another agent holds is refused: that
        task has genuinely been taken, and stealing it back is the duplicate this prevents.
        """
        task = self._require(task_id)
        agent_id = getattr(agent, "id", str(agent))
        holder = task.claimed_by or task.offered_to
        if task.state == TaskState.DONE or task.state == TaskState.FAILED:
            raise PoolError(f"task {task_id!r} is {task.state} and cannot be renewed")
        if task.state == TaskState.BACKLOG:
            raise PoolError(f"task {task_id!r} is parked in the backlog, not held by anyone")
        if holder is not None and holder != agent_id:
            raise PoolError(f"task {task_id!r} is held by {holder!r}, not {agent_id!r}")
        if holder is None and task.last_claimant != agent_id:
            # Nobody holds it and this agent never did: it lapsed and was re-claimed and released by
            # someone else, or the caller never had it. Renewing here would be an uncapability-checked
            # claim, so it is refused and the caller is told what to do instead.
            raise PoolError(
                f"task {task_id!r} returned to the pool and was not claimed by {agent_id!r}; "
                "claim it again rather than renewing it"
            )
        task.state = TaskState.CLAIMED
        task.claimed_by = agent_id
        task.offered_to = None
        task.last_claimant = agent_id
        task.lease_until = self._now() + float(lease_s if lease_s is not None else DEFAULT_LEASE_S)
        task.updated_at = self._now()
        self.save()
        return task

    @_guarded
    def release(self, task_id: str, *, reason: str = "") -> PoolTask:
        """Return a claimed task to the pool, so a worker that cannot finish it is not a dead end."""
        task = self._require(task_id)
        if task.state not in (TaskState.CLAIMED, TaskState.OFFERED):
            raise PoolError(f"task {task_id!r} is {task.state}, not held")
        # Read the holder before clearing it. The refusal used to be recorded with `claimed_by`
        # already set to None, so the one field that says *who* refused was always null.
        holder = task.claimed_by or task.offered_to or ""
        task.state = TaskState.POOL
        task.claimed_by = None
        task.last_claimant = holder
        task.offered_to = None
        task.lease_until = None
        task.updated_at = self._now()
        if reason:
            task.refusals.append({"agent": holder, "reason": reason, "at": self._now()})
        self.save()
        return task

    def _expire_leases(self, *, now: float | None = None) -> list[str]:
        """Return every task whose lease lapsed to the pool.

        This is what stops a worker that died mid-task from stranding it forever — the failure mode a
        plain "assigned" flag produces. The previous holder is remembered in `last_claimant` rather
        than forgotten, so a worker that was merely slow can still prove it was running the task and
        renew it (`renew`) instead of having to re-claim behind someone else's back.
        """
        moment = now or self._now()
        expired: list[str] = []
        for task in self.tasks.values():
            if task.state == TaskState.CLAIMED and task.lease_expired(now=moment):
                task.state = TaskState.POOL
                task.last_claimant = task.claimed_by or task.last_claimant
                task.claimed_by = None
                task.lease_until = None
                task.updated_at = self._now()
                expired.append(task.id)
        if expired:
            self.save()
        return expired

    # ── offers ──────────────────────────────────────────────────────────────

    @_guarded
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

    @_guarded
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
        task.last_claimant = agent_id
        task.lease_until = self._now() + float(lease_s)
        task.updated_at = self._now()
        self.save()
        return task

    @_guarded
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

    @_guarded
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

    @_guarded
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

    @_guarded
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

    @_guarded
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

    @_guarded
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

    @_guarded
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
