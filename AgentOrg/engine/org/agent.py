#!/usr/bin/env python3
"""agent.py — the agent spec: a named employee bound to skills and a model.

WHY THIS EXISTS
---------------
The whole design rests on one distinction: a skill is a capability, an agent is headcount.
`backend-developer` is a capability; "Alice on local Ollama, Bob on Anthropic and Chen on
OpenAI" are three employees who all hold it. Everything downstream — routing, health,
budget, session rotation, delegation lineage — is *per agent*, not per skill, so the agent
needs an identity richer than the skill name it carries.

DESIGN
------
- **A surrogate `id` plus a user-chosen `name`.** The id is stable across renames because
  traces, mailboxes and health records reference it; the name is what the Owner sees and may
  change. Using the name as the key would break every historical record on a rename.
- **The model binding is validated at construction.** A model with no known context window
  is refused here, because the session-rotation design depends on that number being real —
  catching it at binding is far cheaper than mid-run.
- **Budget is partitioned, not created.** An agent's budget is a share of the run's, so
  spawning agents cannot raise the total spend.
- **`kind` and `parent_id` carry delegation lineage** so a helper can be destroyed with its
  task and a specialist can be retired deliberately.
- **State is a first-class field** so the UI can show who is idle, working, blocked or
  terminated without inferring it from logs.

Usage:
    spec = AgentSpec(id="ag_7f3a", name="Alice", title="Backend Developer",
                     skills=["backend-developer"], provider="ollama",
                     model="qwen2.5-coder:7b", context_window=32768)
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "AgentError",
    "AgentKind",
    "AgentLevel",
    "AgentSpec",
    "AgentRuntime",
    "AgentState",
    "Budget",
    "Cost",
    "new_agent_id",
]


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"



class AgentError(RuntimeError):
    """Raised when an agent spec is invalid or an operation on it is not permitted."""


class AgentKind(str, Enum):
    """What sort of actor this is.

    A human is an agent because that is what makes a human handoff use the same machinery as
    an automated one — the same contract, ledger and audit trail.
    """

    AI = "ai"
    HUMAN = "human"
    ROUTER = "router"


class AgentLevel(int, Enum):
    """Capability level, mirroring the library's L1–L5 ladder.

    Routing scores on this, and a delegation tier can require a minimum level for a
    consequential task.
    """

    JUNIOR = 1
    PRACTITIONER = 2
    SENIOR = 3
    STAFF = 4
    PRINCIPAL = 5

    @property
    def label(self) -> str:
        return {
            1: "L1 Junior",
            2: "L2 Practitioner",
            3: "L3 Senior",
            4: "L4 Staff",
            5: "L5 Principal",
        }[self.value]


class AgentState(str, Enum):
    """Operational state, as the roster view shows it."""

    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"          # waiting on a gate or the Owner
    WAITING = "waiting"          # queued for a concurrency slot
    QUARANTINED = "quarantined"  # removed from the routing pool by the health engine
    TERMINATED = "terminated"    # destroyed (helper) or retired (specialist)


def new_agent_id() -> str:
    """Mint a stable agent id.

    Short and hex so it reads well in a trace, and prefixed so an id is recognisable as an
    agent reference in a log line that also contains node and run ids.
    """
    return f"ag_{uuid.uuid4().hex[:8]}"


@dataclass
class Budget:
    """Spend limits for one agent, carved from the run's budget.

    `allocated_usd` is what this agent may spend; `spent_usd` tracks usage. The run's ceiling
    is enforced by the gateway, so this is a second, tighter bound rather than the only one.
    """

    allocated_usd: float = 0.0
    allocated_tokens: int = 0
    spent_usd: float = 0.0
    spent_tokens: int = 0

    @property
    def remaining_usd(self) -> float:
        """Unspent USD, floored at zero so a negative never reads as credit."""
        return max(0.0, self.allocated_usd - self.spent_usd)

    @property
    def remaining_tokens(self) -> int:
        """Unspent tokens, floored at zero."""
        return max(0, self.allocated_tokens - self.spent_tokens)

    @property
    def exhausted(self) -> bool:
        """True when either limit is reached."""
        if self.allocated_usd and self.spent_usd >= self.allocated_usd:
            return True
        if self.allocated_tokens and self.spent_tokens >= self.allocated_tokens:
            return True
        return False

    def charge(self, *, usd: float = 0.0, tokens: int = 0) -> None:
        """Record a spend against this agent."""
        self.spent_usd += max(0.0, usd)
        self.spent_tokens += max(0, tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allocated_usd": round(self.allocated_usd, 6),
            "spent_usd": round(self.spent_usd, 6),
            "remaining_usd": round(self.remaining_usd, 6),
            "allocated_tokens": self.allocated_tokens,
            "spent_tokens": self.spent_tokens,
            "remaining_tokens": self.remaining_tokens,
            "exhausted": self.exhausted,
        }


@dataclass
class Cost:
    """A single charged call, for the per-agent economics view."""

    usd: float = 0.0
    tokens: int = 0
    source: str = "unknown"   # measured | estimated | free | unknown
    model: str = ""
    at: str = ""

    def __post_init__(self) -> None:
        if not self.at:
            self.at = _iso_now()


@dataclass
class AgentSpec:
    """A named employee: identity, capability, model binding and limits.

    Parameters
    ----------
    context_window:
        Required for an AI agent. It is the single number the session projection cannot
        work without, so an agent bound without one is refused rather than allowed to
        overflow later.
    """

    id: str
    name: str
    title: str = ""
    skills: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    context_window: int | None = None
    max_output: int | None = None
    kind: AgentKind = AgentKind.AI
    role: str = "worker"           # "owner" | "reviewer" | "worker"
    level: AgentLevel = AgentLevel.PRACTITIONER
    team: str = ""
    reports_to: str | None = None
    capabilities: list[str] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    max_concurrency: int = 1
    workdir: str = ""
    # Delegation lineage.
    parent_id: str | None = None
    # Provenance: how this agent came to exist ("owner" | "template" | "requisition").
    origin: str = "owner"
    created: str = field(default_factory=_iso_now)
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id:
            raise AgentError("an agent requires an id")
        if not (self.name or "").strip():
            raise AgentError(f"agent {self.id} requires a name; an unnamed agent is unassignable")
        if not self.skills:
            raise AgentError(
                f"agent {self.name!r} has no skills. An agent with no capability cannot be "
                "routed work."
            )
        if self.kind is AgentKind.AI:
            if not self.provider or not self.model:
                raise AgentError(
                    f"agent {self.name!r} is an AI agent and needs both a provider and a "
                    "model; without them it cannot be called."
                )
            # The refusal that matters: a window of None would silently break the
            # pre-flight projection, so it is caught here rather than at overflow time.
            if not self.context_window:
                raise AgentError(
                    f"agent {self.name!r} is bound to model {self.model!r} with an unknown "
                    "context window. The session projection cannot size a prompt without it, "
                    "so this binding is refused. Probe the model (the catalog does this for "
                    "Ollama via /api/show) or declare context_window in the config."
                )
        if self.max_concurrency < 1:
            raise AgentError(f"agent {self.name!r} max_concurrency must be >= 1")
        if self.level not in tuple(AgentLevel):
            raise AgentError(f"agent {self.name!r} has an invalid level {self.level!r}")

    # ── capability ──────────────────────────────────────────────────────────

    def has_skill(self, skill: str) -> bool:
        """Whether this agent holds a skill."""
        return skill in self.skills

    def has_capability(self, capability: str) -> bool:
        """Whether this agent was granted an explicit capability (least-privilege check).

        Prefix matching is deliberate: a grant of `read:src/` covers `read:src/app.py`, which
        is how a delegation can hand a helper read access to one directory without listing
        every file.
        """
        for granted in self.capabilities:
            if capability == granted or capability.startswith(granted):
                return True
        return False

    @property
    def is_human(self) -> bool:
        return self.kind is AgentKind.HUMAN

    @property
    def is_ai(self) -> bool:
        return self.kind is AgentKind.AI

    def as_dict(self) -> dict[str, Any]:
        """Serialisation for the org file, events and the UI roster."""
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "skills": list(self.skills),
            "provider": self.provider,
            "model": self.model,
            "context_window": self.context_window,
            "max_output": self.max_output,
            "kind": self.kind.value,
            "role": self.role,
            "level": int(self.level),
            "level_label": self.level.label,
            "team": self.team,
            "reports_to": self.reports_to,
            "capabilities": list(self.capabilities),
            "budget": self.budget.as_dict(),
            "max_concurrency": self.max_concurrency,
            "workdir": self.workdir,
            "parent_id": self.parent_id,
            "origin": self.origin,
            "created": self.created,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSpec":
        """Rebuild from a persisted dict, tolerating older records.

        Unknown keys are ignored and missing optional ones defaulted, so an org file written
        by an earlier build still loads — a rename or an added field must not strand an
        existing roster.
        """
        budget_raw = data.get("budget") or {}
        budget = Budget(
            allocated_usd=float(budget_raw.get("allocated_usd", 0.0)),
            allocated_tokens=int(budget_raw.get("allocated_tokens", 0)),
            spent_usd=float(budget_raw.get("spent_usd", 0.0)),
            spent_tokens=int(budget_raw.get("spent_tokens", 0)),
        )
        try:
            kind = AgentKind(str(data.get("kind", "ai")))
        except ValueError:
            kind = AgentKind.AI
        try:
            level = AgentLevel(int(data.get("level", 2)))
        except (ValueError, TypeError):
            level = AgentLevel.PRACTITIONER
        return cls(
            id=str(data.get("id") or new_agent_id()),
            name=str(data.get("name") or "unnamed"),
            title=str(data.get("title") or ""),
            skills=[str(s) for s in (data.get("skills") or [])],
            provider=str(data.get("provider") or ""),
            model=str(data.get("model") or ""),
            context_window=data.get("context_window"),
            max_output=data.get("max_output"),
            kind=kind,
            role=str(data.get("role") or "worker"),
            level=level,
            team=str(data.get("team") or ""),
            reports_to=data.get("reports_to"),
            capabilities=[str(c) for c in (data.get("capabilities") or [])],
            budget=budget,
            max_concurrency=int(data.get("max_concurrency", 1)),
            workdir=str(data.get("workdir") or ""),
            parent_id=data.get("parent_id"),
            origin=str(data.get("origin") or "owner"),
            created=str(data.get("created") or _iso_now()),
            tags=[str(t) for t in (data.get("tags") or [])],
        )


@dataclass
class AgentRuntime:
    """The live state of an agent: what it is doing, and what it has cost.

    Kept separate from :class:`AgentSpec` so the *definition* can be persisted and diffed
    while the *state* changes constantly. Conflating them would mean writing the roster file
    on every heartbeat.

    **Occupancy is counted, not inferred from `state`.** `state` is single-valued, so it cannot
    express "two of this agent's three slots are held" — and it was *never set at all*, because
    nothing called `begin`, so `available()` answered "free" for an agent that was mid-node. The
    count below is the truth binding reads; `state` is what the roster view shows.
    """

    agent_id: str
    #: How many units of work this agent may hold at once, copied from `AgentSpec.max_concurrency`
    #: by :class:`~engine.org.roster.Org`. Single-flight means *no more than this*, not "exactly
    #: one": the default is 1, so an ordinary agent is one-node-at-a-time, while an agent hired as
    #: "may run three at once" is held to three.
    capacity: int = 1
    state: AgentState = AgentState.IDLE
    current_task: str | None = None
    current_node: str | None = None
    current_session: str | None = None
    #: Units of work currently held by a caller. Incremented once per `begin`/`try_begin` and
    #: released once per `finish`, so the pair must bracket the work exactly.
    inflight: int = 0
    #: Guards `state`, `current_*` and `inflight`. Per-runtime rather than per-org because the
    #: decision this protects — "is a slot free, and is it now mine?" — is about one agent, and a
    #: global lock would serialise every binding in the run for a property that never spans agents.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    # Delegation chain: the agents that led here, oldest first.
    chain: list[str] = field(default_factory=list)
    # Rolling health signals, filled by health.py.
    tasks_completed: int = 0
    tasks_failed: int = 0
    escalations: int = 0
    guardrail_blocks: int = 0
    contract_breaches: int = 0
    turns: int = 0
    active_reports: int = 0
    last_active: str = field(default_factory=_iso_now)
    last_error: str | None = None

    def busy(self) -> bool:
        """True when the agent holds work or is parked — the single-flight check uses this."""
        with self._lock:
            return self.inflight > 0 or self.state is AgentState.BLOCKED

    def available(self) -> bool:
        """True when the agent has a free slot and is not parked, gone or quarantined.

        Read by the binder's ordering, by routing and by delegation, which is why it answers with
        the *count*: an agent at its limit is not available however cheerful its `state` looks.
        """
        with self._lock:
            if self.state in (AgentState.BLOCKED, AgentState.QUARANTINED, AgentState.TERMINATED):
                return False
            return self.inflight < self.capacity

    def pressure(self) -> float:
        """Held slots as a fraction of the limit — the tie-break when every candidate is full.

        Capacity-normalised rather than raw, so a two-slot agent holding two slots loses to a
        four-slot agent holding two: the question is which one is closest to its own limit.
        """
        with self._lock:
            return self.inflight / self.capacity if self.capacity else 0.0

    def try_begin(self, *, task_id: str, node_id: str, session_id: str | None = None) -> bool:
        """Take a slot for this work, but only if one is free, and report whether it was taken.

        THE ONE ATOMIC STEP. The check and the claim are the same call under the same lock because
        a caller that asked `available()` and then called `begin` has a window in which a sibling —
        a parallel group's other member, or a second fan-out item — sees the same free slot and
        takes it too. That is precisely how two nodes were bound to one agent and then shared a
        session, so the window is closed here rather than trusted not to open.
        """
        with self._lock:
            if self.state in (AgentState.QUARANTINED, AgentState.TERMINATED):
                return False
            if self.inflight >= self.capacity:
                return False
            self.inflight += 1
            self._mark_working(task_id, node_id, session_id)
            return True

    def begin(self, *, task_id: str, node_id: str, session_id: str | None = None) -> None:
        """Take a slot and mark the agent working, whatever the count already says.

        The unconditional claim, for the one path that must not refuse: when every capable agent is
        at its limit, the executor runs the node anyway rather than stopping a run that works today.
        Refusing here would leave that work holding no slot at all, so the agent would look free
        while it worked — the defect this class exists to fix, wearing a different hat. The overload
        is therefore *recorded* (the count goes past the limit, and falls back as work finishes)
        rather than hidden.
        """
        with self._lock:
            self.inflight += 1
            self._mark_working(task_id, node_id, session_id)

    def _mark_working(self, task_id: str, node_id: str, session_id: str | None) -> None:
        """Set the descriptive half of a claim. Callers hold `_lock`."""
        self.state = AgentState.WORKING
        self.current_task = task_id
        self.current_node = node_id
        self.current_session = session_id
        self.last_active = _iso_now()

    def note_session(self, session_id: str) -> None:
        """Record which session the held work is using.

        Separate from `begin` because the session cannot exist before the claim it belongs to: the
        session is keyed on the agent that *won* the slot, and which agent that is is what the claim
        decides. A display field, not a claim — the roster view says which transcript an agent is
        working in, and nothing makes a decision from it.
        """
        with self._lock:
            self.current_session = session_id

    def set_capacity(self, capacity: int) -> None:
        """Re-read the limit from the spec that owns it.

        The agent's `max_concurrency` can be edited while the org is live (`people.update_agent`),
        and the spec — not this copy — is the source of truth. Lowering the limit under work that is
        already running is allowed and safe: the count simply stays above it until that work
        finishes, `available()` answers False meanwhile, and `finish` brings it back down.
        """
        with self._lock:
            self.capacity = max(1, int(capacity))

    def finish(self, *, failed: bool = False, error: str | None = None) -> None:
        """Release the slot this unit of work took, and record the outcome for the health signals.

        The agent becomes idle only when the *last* held unit finishes: with a limit of three, the
        first of three to complete must not report a free agent that is still working.
        """
        with self._lock:
            self.inflight = max(0, self.inflight - 1)
            if self.inflight == 0:
                # Not TERMINATED or QUARANTINED: a roster edited mid-run must not be undone by a
                # node that happens to finish afterwards, which would put a retired agent back in
                # the pool the health engine removed it from.
                if self.state not in (AgentState.TERMINATED, AgentState.QUARANTINED):
                    self.state = AgentState.IDLE
                self.current_task = None
                self.current_node = None
                self.current_session = None
            self.last_active = _iso_now()
            if failed:
                self.tasks_failed += 1
                self.last_error = error
            else:
                self.tasks_completed += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "state": self.state.value,
            "current_task": self.current_task,
            "current_node": self.current_node,
            "current_session": self.current_session,
            "capacity": self.capacity,
            "inflight": self.inflight,
            "chain": list(self.chain),
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "escalations": self.escalations,
            "guardrail_blocks": self.guardrail_blocks,
            "contract_breaches": self.contract_breaches,
            "turns": self.turns,
            "active_reports": self.active_reports,
            "last_active": self.last_active,
            "last_error": self.last_error,
        }
