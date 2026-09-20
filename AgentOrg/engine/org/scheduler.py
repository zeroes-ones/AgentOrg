#!/usr/bin/env python3
"""scheduler.py — admission control, concurrency ceilings and liveness.

WHY THIS EXISTS
---------------
"Use system resources efficiently" and "do not hang the app" are the same problem seen from two
sides: how many things run at once, and what happens when one of them stops responding. This
module owns both.

The concurrency that matters is not CPU. Inference happens in Ollama, LM Studio or a cloud
provider — not in this engine — so the engine is I/O-bound and its real limits are:

- **local model memory**, because on Apple Silicon GPU and CPU share one pool and loading three
  models at once causes system-wide swap;
- **provider rate limits**, which must produce backpressure rather than a retry storm;
- **the budget ceiling**, because spending is the one thing that cannot be retried away.

DESIGN
------
- **Three tiers of concurrency**: a global ceiling from measured machine capacity, per-provider
  semaphores, and per-agent single-flight.
- **Bounded queues that shed.** When the queue is full the scheduler drops the lowest-priority
  work with an explicit event rather than growing memory.
- **Semaphores shrink adaptively on a 429** and recover slowly, so a rate-limited provider
  throttles the org instead of failing it.
- **Per-artifact locks** stop two agents writing the same file, which the design promises.
- **A heartbeat watchdog** distinguishes "slow" from "wedged", so a hang is detected in seconds
  and escalated SIGTERM → SIGKILL → resume rather than waiting forever.

Usage:
    sched = Scheduler(config=cfg, caps=detect())
    ticket = sched.admit(task_id="t1", agent_id="ag_1", provider="ollama", priority=5)
    ...execute...
    sched.release(ticket)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable

from ..config import Config
from ..resources import MachineCaps, derive_ceiling
from .agent import Budget

__all__ = [
    "Priority",
    "Scheduler",
    "SchedulerError",
    "Ticket",
    "TicketState",
    "Watchdog",
]


class SchedulerError(RuntimeError):
    """Raised when work cannot be admitted and cannot be queued."""


class Priority(int, Enum):
    """Queue priority. Higher runs first; the ordering is what shed picks against.

    The banding is deliberate: work that is *blocking a gate* is the most valuable to finish
    (an idle human is worse than an idle worker), a rework pass is next because it is already
    mid-flight, and new work is last because starting something new cannot unblock anything.
    """

    GATE_BLOCKED = 100
    REWORK = 75
    ACTIVE_RUN = 50
    NEW_WORK = 10


class TicketState(str, Enum):
    """Where a ticket is."""

    QUEUED = "queued"
    ADMITTED = "admitted"
    RUNNING = "running"
    DONE = "done"
    SHED = "shed"
    FAILED = "failed"


@dataclass
class Ticket:
    """One admitted unit of work."""

    id: str
    agent_id: str
    provider: str
    priority: Priority = Priority.NEW_WORK
    state: TicketState = TicketState.QUEUED
    node_id: str = ""
    task_id: str = ""
    created_at: float = field(default_factory=time.time)
    admitted_at: float | None = None
    finished_at: float | None = None
    heartbeat_at: float = field(default_factory=time.time)
    error: str | None = None

    @property
    def queued_s(self) -> float:
        """How long this ticket waited for a slot."""
        end = self.admitted_at or time.time()
        return max(0.0, end - self.created_at)

    @property
    def running_s(self) -> float:
        """How long it has been running."""
        if self.admitted_at is None:
            return 0.0
        end = self.finished_at or time.time()
        return max(0.0, end - self.admitted_at)

    def beat(self) -> None:
        """Record liveness. The watchdog measures the gap since the last beat."""
        self.heartbeat_at = time.time()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "provider": self.provider,
            "priority": self.priority.name,
            "state": self.state.value,
            "node_id": self.node_id,
            "task_id": self.task_id,
            "queued_s": round(self.queued_s, 3),
            "running_s": round(self.running_s, 3),
            "error": self.error,
        }


class Watchdog:
    """Detects a wedged worker from its heartbeat, and escalates in stages.

    A hung agent is worse than a failed one: a failure reports itself, while a hang consumes a
    concurrency slot forever and the run appears to be thinking. The watchdog turns silence into
    an escalation: a warning first, then SIGTERM, then SIGKILL, each with a grace period.
    """

    def __init__(self, *, heartbeat_s: float = 30.0, grace_s: float = 5.0) -> None:
        self.heartbeat_s = heartbeat_s
        self.grace_s = grace_s
        if heartbeat_s <= 0:
            raise SchedulerError("watchdog heartbeat_s must be > 0")

    def state(self, ticket: Ticket) -> str:
        """Classify a ticket as `alive`, `slow`, `warned` or `wedged`."""
        silence = time.time() - ticket.heartbeat_at
        if silence <= self.heartbeat_s:
            return "alive"
        if silence <= self.heartbeat_s * 2:
            return "slow"
        if silence <= self.heartbeat_s * 2 + self.grace_s:
            return "warned"
        return "wedged"

    def should_terminate(self, ticket: Ticket) -> bool:
        """True once the grace period after a warning has elapsed."""
        return self.state(ticket) == "wedged"

    def report(self, tickets: Iterable[Ticket]) -> list[dict[str, Any]]:
        """Every ticket that is not healthy, with its silence duration."""
        out: list[dict[str, Any]] = []
        for ticket in tickets:
            state = self.state(ticket)
            if state == "alive":
                continue
            out.append({
                "ticket": ticket.id, "agent_id": ticket.agent_id, "state": state,
                "silence_s": round(time.time() - ticket.heartbeat_at, 1),
                "action": ("SIGKILL then resume from checkpoint"
                           if state == "wedged" else
                           "SIGTERM after the grace period" if state == "warned" else
                           "watch for a stall"),
            })
        return out


@dataclass
class Scheduler:
    """Admission control and resource governance.

    Parameters
    ----------
    config:
        Supplies the concurrency block: queue depth, heartbeat, per-provider limits.
    caps:
        Measured machine capacity. When omitted the ceiling falls back to the configured value,
        which is honest about not having measured rather than pretending.
    """

    config: Config | None = None
    caps: MachineCaps | None = None
    local_models_in_use: int = 0
    local_model_ids: list[str] = field(default_factory=list)
    # A callback invoked when work is shed, so the caller can emit an event.
    on_shed: Callable[[Ticket, str], None] | None = None

    def __post_init__(self) -> None:
        concurrency = self.config.concurrency if self.config else None
        ceiling_cfg = getattr(concurrency, "global_ceiling", None)
        queue_max = getattr(concurrency, "queue_max_depth", 64)
        heartbeat = getattr(concurrency, "heartbeat_s", 30.0)
        grace = getattr(concurrency, "grace_s", 5.0)
        per_provider = dict(getattr(concurrency, "per_provider_limits", {}) or {})

        self._lock = threading.RLock()
        self._queue: list[Ticket] = []
        self._tickets: dict[str, Ticket] = {}
        self._running: dict[str, Ticket] = {}
        self._agent_inflight: dict[str, str] = {}
        self._artifact_locks: dict[str, threading.Lock] = {}
        self._shed: list[tuple[Ticket, str]] = []
        self.queue_max_depth = max(1, int(queue_max))
        self.watchdog = Watchdog(heartbeat_s=float(heartbeat), grace_s=float(grace))

        # The ceiling. Derived from measured capacity when available; a configured value only
        # ever lowers it, because a config cannot add memory to the machine.
        if self.caps is not None:
            derivation = derive_ceiling(
                self.caps,
                cpu_headroom=getattr(concurrency, "cpu_headroom", 1),
                configured_ceiling=ceiling_cfg,
                local_models_in_use=self.local_models_in_use,
                local_model_ids=self.local_model_ids,
            )
            self.ceiling = derivation["ceiling"]
            self.ceiling_reason = derivation["reason"]
        else:
            self.ceiling = int(ceiling_cfg or 4)
            self.ceiling_reason = "no machine caps supplied; using the configured or default value"

        # Per-provider semaphores. A local provider defaults to 1 because loading two models at
        # once on a unified-memory Mac causes system-wide swap, not merely slowness.
        self._provider_limits: dict[str, int] = {}
        self._provider_inflight: dict[str, int] = {}
        for provider_id, spec in ((self.config.providers.items() if self.config else [])):
            default_limit = 1 if self._is_local(spec.base_url) else spec.concurrency
            self._provider_limits[provider_id] = int(
                per_provider.get(provider_id, default_limit)
            )
            self._provider_inflight[provider_id] = 0
        for provider_id, limit in per_provider.items():
            self._provider_limits.setdefault(provider_id, int(limit))
            self._provider_inflight.setdefault(provider_id, 0)

    @staticmethod
    def _is_local(base_url: str) -> bool:
        """Whether a provider runs on this machine."""
        lowered = (base_url or "").lower()
        return any(m in lowered for m in ("localhost", "127.0.0.1", "::1", "0.0.0.0"))

    # ── admission ───────────────────────────────────────────────────────────

    def admit(self, *, ticket_id: str, agent_id: str, provider: str,
              priority: Priority = Priority.NEW_WORK, node_id: str = "",
              task_id: str = "", budget: Budget | None = None) -> Ticket:
        """Try to admit work immediately, or queue it.

        Raises
        ------
        SchedulerError
            When the queue is full and this ticket is the lowest priority — shedding the caller's
            own work is better than silently growing memory, and the caller is told.

        Returns a :class:`Ticket`. `ticket.state` says whether it was admitted or queued.
        """
        with self._lock:
            if ticket_id in self._tickets:
                return self._tickets[ticket_id]
            ticket = Ticket(id=ticket_id, agent_id=agent_id, provider=provider,
                            priority=priority, node_id=node_id, task_id=task_id)
            self._tickets[ticket_id] = ticket

            if budget is not None and budget.exhausted:
                ticket.state = TicketState.FAILED
                ticket.error = "the agent's allocated budget is exhausted"
                return ticket

            if not self._would_admit(ticket):
                if len(self._queue) >= self.queue_max_depth:
                    shed = self._shed_lowest(priority)
                    if shed is None:
                        # Everything queued outranks this, so this is the one to shed.
                        ticket.state = TicketState.SHED
                        ticket.error = (
                            f"queue is full ({self.queue_max_depth}) and this ticket has the "
                            f"lowest priority ({priority.name})"
                        )
                        self._shed.append((ticket, ticket.error))
                        if self.on_shed:
                            self.on_shed(ticket, ticket.error)
                        raise SchedulerError(
                            f"cannot admit {ticket_id!r}: {ticket.error}. Lower-priority work is "
                            "shed explicitly rather than queued without bound."
                        )
                self._queue.append(ticket)
                self._queue.sort(key=lambda t: (-int(t.priority), t.created_at))
                return ticket

            self._start(ticket)
            return ticket

    def _would_admit(self, ticket: Ticket) -> bool:
        """Every gate that must open for immediate admission."""
        if len(self._running) >= self.ceiling:
            return False
        if self._agent_inflight.get(ticket.agent_id):
            # One ticket per agent here, which is *stricter* than the engine now enforces: an agent's
            # real limit is `AgentSpec.max_concurrency`, claimed by `AgentRuntime.try_begin` around a
            # node's work, and a holder permitted three tasks genuinely runs three. This dict is
            # run-level admission, not binding, so it has no roster to read that limit from — wiring
            # this class in would throttle such an agent to one ticket. Nothing in the engine
            # constructs a `Scheduler` today; whoever wires one up must decide whether that
            # throttling is wanted.
            return False
        limit = self._provider_limits.get(ticket.provider)
        if limit is not None and self._provider_inflight.get(ticket.provider, 0) >= limit:
            return False
        return True

    def _start(self, ticket: Ticket) -> None:
        """Mark a ticket running and take its slots."""
        ticket.state = TicketState.RUNNING
        ticket.admitted_at = time.time()
        ticket.beat()
        self._running[ticket.id] = ticket
        self._agent_inflight[ticket.agent_id] = ticket.id
        if ticket.provider:
            self._provider_inflight[ticket.provider] = (
                self._provider_inflight.get(ticket.provider, 0) + 1
            )

    def _shed_lowest(self, incoming: Priority) -> Ticket | None:
        """Shed the lowest-priority queued ticket when it ranks below the incoming one.

        Returns the shed ticket, or None when nothing queued is lower priority — in which case
        the caller sheds its own work instead. Shedding explicitly is what keeps the queue
        bounded without silently dropping work.
        """
        if not self._queue:
            return None
        lowest = min(self._queue, key=lambda t: (int(t.priority), t.created_at))
        if int(lowest.priority) >= int(incoming):
            return None
        self._queue.remove(lowest)
        lowest.state = TicketState.SHED
        lowest.error = (
            f"preempted by higher-priority work ({incoming.name}); the queue is full at "
            f"{self.queue_max_depth}"
        )
        self._shed.append((lowest, lowest.error))
        if self.on_shed:
            self.on_shed(lowest, lowest.error)
        return lowest

    def release(self, ticket_id: str, *, error: str | None = None) -> Ticket | None:
        """Finish a ticket, free its slots, and admit the next queued work.

        Every released slot triggers one admission attempt, so a queue drains as work completes
        rather than only when a caller polls.
        """
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                return None
            if ticket.state is TicketState.RUNNING:
                ticket.state = TicketState.FAILED if error else TicketState.DONE
                ticket.error = error
                ticket.finished_at = time.time()
                self._running.pop(ticket_id, None)
                if self._agent_inflight.get(ticket.agent_id) == ticket_id:
                    self._agent_inflight.pop(ticket.agent_id, None)
                if ticket.provider:
                    self._provider_inflight[ticket.provider] = max(
                        0, self._provider_inflight.get(ticket.provider, 0) - 1
                    )
            self._drain()
            return ticket

    def _drain(self) -> list[Ticket]:
        """Admit queued work while slots remain. Returns what started."""
        started: list[Ticket] = []
        while self._queue:
            candidate = self._queue[0]
            if not self._would_admit(candidate):
                break
            self._queue.pop(0)
            self._start(candidate)
            started.append(candidate)
        return started

    def beat(self, ticket_id: str) -> None:
        """Record liveness for a running ticket."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is not None:
                ticket.beat()

    # ── backpressure ────────────────────────────────────────────────────────

    def on_rate_limited(self, provider: str, *, retry_after_s: float | None = None) -> dict[str, Any]:
        """Shrink a provider's limit after a 429, and report the new value.

        Shrinking rather than failing is what turns a rate limit into throttling. The limit never
        drops below 1 (the provider is still usable) and recovers on
        :meth:`on_provider_recovered`, slowly, so a brief limit does not leave the org throttled
        for the rest of the run.
        """
        with self._lock:
            current = self._provider_limits.get(provider, 1)
            new_limit = max(1, current - 1)
            self._provider_limits[provider] = new_limit
            return {
                "provider": provider,
                "previous_limit": current,
                "new_limit": new_limit,
                "retry_after_s": retry_after_s,
                "inflight": self._provider_inflight.get(provider, 0),
                "action": "backpressure on" if new_limit < current else "already at the floor of 1",
            }

    def on_provider_recovered(self, provider: str, *,
                              configured_limit: int | None = None) -> dict[str, Any]:
        """Recover a provider's limit toward its configured value, one step at a time.

        One step rather than a jump: a provider that has just stopped rate-limiting may still be
        close to its limit, and jumping straight back reproduces the 429.
        """
        with self._lock:
            current = self._provider_limits.get(provider, 1)
            target = configured_limit
            if target is None and self.config is not None:
                spec = self.config.providers.get(provider)
                if spec is not None:
                    target = _provider_spec_limit(spec.base_url, spec.concurrency)
            target = target or current
            new_limit = min(target, current + 1)
            self._provider_limits[provider] = new_limit
            return {"provider": provider, "previous_limit": current, "new_limit": new_limit,
                    "target": target, "action": "recovering" if new_limit < target else "recovered"}

    def provider_limit(self, provider: str) -> int:
        """The current in-flight limit for a provider."""
        with self._lock:
            return self._provider_limits.get(provider, 1)

    def backpressure_state(self) -> dict[str, Any]:
        """Per-provider limits and in-flight counts, for the resources view."""
        with self._lock:
            return {
                provider: {
                    "limit": limit,
                    "inflight": self._provider_inflight.get(provider, 0),
                    "throttled": limit < (self._configured_limit(provider) or limit),
                }
                for provider, limit in sorted(self._provider_limits.items())
            }

    def _configured_limit(self, provider: str) -> int | None:
        if self.config is None:
            return None
        spec = self.config.providers.get(provider)
        if spec is None:
            return None
        return _provider_spec_limit(spec.base_url, spec.concurrency)

    # ── artifact locks ──────────────────────────────────────────────────────

    def acquire_artifact(self, path: str, *, timeout_s: float = 60.0) -> bool:
        """Take the lock for an artifact path.

        Two agents must not write one file: the design promises a per-artifact lock, and without
        it two developers interleave their edits and the artifact is neither version.
        """
        with self._lock:
            lock = self._artifact_locks.get(path)
            if lock is None:
                lock = threading.Lock()
                self._artifact_locks[path] = lock
        return lock.acquire(timeout=timeout_s)

    def release_artifact(self, path: str) -> None:
        """Release an artifact lock, tolerating one that was never taken."""
        with self._lock:
            lock = self._artifact_locks.get(path)
        if lock is None:
            return
        try:
            lock.release()
        except RuntimeError:
            # Released by a non-owner or already released; harmless at this layer.
            pass

    def artifact_lock(self, path: str, *, timeout_s: float = 60.0) -> "_ArtifactLock":
        """Context manager form of the artifact lock."""
        return _ArtifactLock(self, path, timeout_s=timeout_s)

    # ── liveness ────────────────────────────────────────────────────────────

    def wedged(self) -> list[dict[str, Any]]:
        """Tickets whose watchdog state is not healthy, with the escalation for each."""
        with self._lock:
            return self.watchdog.report(list(self._running.values()))

    def reclaim(self, ticket_id: str, *, reason: str = "watchdog") -> Ticket | None:
        """Force-release a wedged ticket so its slot can be reused, and drain the queue.

        Called after the kill escalation: the slot must be reclaimed even though the worker never
        reported completion, or a single hang would permanently reduce the ceiling.
        """
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None or ticket.state is not TicketState.RUNNING:
                return None
            ticket.state = TicketState.FAILED
            ticket.error = f"reclaimed by the {reason}: the worker stopped responding"
            ticket.finished_at = time.time()
            self._running.pop(ticket_id, None)
            if self._agent_inflight.get(ticket.agent_id) == ticket_id:
                self._agent_inflight.pop(ticket.agent_id, None)
            if ticket.provider:
                self._provider_inflight[ticket.provider] = max(
                    0, self._provider_inflight.get(ticket.provider, 0) - 1
                )
            self._drain()
            return ticket

    # ── reporting ───────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Everything the resources view needs, in one call."""
        with self._lock:
            running = list(self._running.values())
            return {
                "ceiling": self.ceiling,
                "ceiling_reason": self.ceiling_reason,
                "running": len(running),
                "queued": len(self._queue),
                "queue_max_depth": self.queue_max_depth,
                "shed": len(self._shed),
                "providers": self.backpressure_state(),
                "agents_inflight": dict(self._agent_inflight),
                "utilization": round(len(running) / max(1, self.ceiling), 3),
                "queue_wait_p50_s": _percentile([t.queued_s for t in self._queue], 0.5),
                "watchdog": self.wedged(),
                "tickets": [t.as_dict() for t in self._tickets.values()],
            }

    def queue_view(self) -> list[dict[str, Any]]:
        """The queue in admission order, for the UI."""
        with self._lock:
            return [t.as_dict() for t in self._queue]

    def shed_log(self) -> list[dict[str, Any]]:
        """Work that was shed, with the reason — never silently dropped."""
        with self._lock:
            return [{"ticket": t.id, "priority": t.priority.name, "reason": reason}
                    for t, reason in self._shed]

    def tune_ceiling(self, *, queue_wait_s: float, compute_s: float) -> dict[str, Any]:
        """Suggest a ceiling adjustment from observed queue wait versus compute time.

        The governor self-tunes from its own telemetry: if work waits longer than it runs, the
        ceiling is too low; if nothing queues at all, capacity is idle. Offered as a suggestion
        with its reasoning rather than applied silently, because raising concurrency on a
        memory-bound machine makes things worse.
        """
        current = self.ceiling
        if compute_s <= 0:
            return {"ceiling": current, "suggestion": "hold",
                    "reason": "no compute time observed yet"}
        ratio = queue_wait_s / compute_s
        if ratio > 1.0 and current < (self.caps.cpu_count if self.caps else current):
            return {"ceiling": current, "suggestion": "raise",
                    "suggested_ceiling": current + 1,
                    "reason": f"queue wait ({queue_wait_s:.1f}s) exceeds compute ({compute_s:.1f}s); "
                              "capacity is the constraint"}
        if ratio < 0.1 and current > 1:
            return {"ceiling": current, "suggestion": "hold",
                    "reason": "queue wait is negligible; capacity is not the constraint"}
        return {"ceiling": current, "suggestion": "hold",
                "reason": f"queue wait to compute ratio {ratio:.2f} is balanced"}


class _ArtifactLock:
    """Context manager for :meth:`Scheduler.acquire_artifact`."""

    __slots__ = ("_scheduler", "_path", "_timeout", "_acquired")

    def __init__(self, scheduler: Scheduler, path: str, *, timeout_s: float) -> None:
        self._scheduler = scheduler
        self._path = path
        self._timeout = timeout_s
        self._acquired = False

    def __enter__(self) -> bool:
        self._acquired = self._scheduler.acquire_artifact(self._path, timeout_s=self._timeout)
        return self._acquired

    def __exit__(self, *exc: object) -> bool:
        if self._acquired:
            self._scheduler.release_artifact(self._path)
        return False


def _provider_spec_limit(base_url: str, concurrency: int) -> int:
    """A provider's configured limit, with local endpoints defaulting to 1."""
    if any(m in (base_url or "").lower() for m in ("localhost", "127.0.0.1", "::1")):
        return 1
    return max(1, int(concurrency))


def _percentile(values: Iterable[float], fraction: float) -> float:
    """A simple percentile, for the queue-wait metric."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return round(ordered[index], 3)
