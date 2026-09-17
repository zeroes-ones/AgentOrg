#!/usr/bin/env python3
"""protocol.py — the NDJSON event and command contract between engine and app.

WHY THIS EXISTS
---------------
This module is the *only* thing that crosses the boundary between the Python engine
and the Swift application. Everything else could be rewritten on either side without
the other noticing, which is what lets the two halves be built and tested
independently. Making that contract explicit, versioned and validated in one place is
therefore worth more than any single feature.

DESIGN
------
- **One schema, two directions.** Events flow engine → app on stdout; commands flow
  app → engine on stdin. Both are newline-delimited JSON, so a partial read is
  recoverable and a single bad line cannot desynchronise the stream.
- **stdout is protocol only.** Diagnostics go to stderr. A stray `print()` on stdout
  would corrupt the stream, so the bus and CLI are the only writers.
- **Ordered and correlated.** Every event carries a monotonic `seq` per run and a
  stable `type`; every command carries a `cmd_id` that its `command.ack` echoes back.
  Without `cmd_id` the UI cannot distinguish its own reply from any other event.
- **Unknown types are tolerated, never fatal.** :func:`decode_event` accepts forward
  compatibility: an app built against v1 must not crash when the engine emits a v2
  event it has never seen. It reports the unknown type rather than raising.
- **Versioned.** `PROTOCOL_VERSION` is carried on every frame. A mismatch is a clear,
  early error instead of mysterious field-by-field confusion.

Usage:
    from engine.protocol import Event, EventType, encode, decode_event
    frame = encode(Event(seq=1, run_id="run_x", type=EventType.RUN_START, payload={}))
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable

__all__ = [
    "PROTOCOL_VERSION",
    "EventType",
    "CommandType",
    "Event",
    "Command",
    "Ack",
    "ProtocolError",
    "encode",
    "decode_event",
    "decode_command",
    "parse_stream",
    "new_cmd_id",
]

PROTOCOL_VERSION = 1
# Hard cap on a single frame. A runaway payload (a huge artifact body inlined into an
# event) must fail loudly at the boundary rather than exhausting memory in both halves.
MAX_FRAME_BYTES = 8 << 20


class ProtocolError(ValueError):
    """Raised when a frame is malformed or violates the protocol contract."""


class EventType(str, Enum):
    """Every event the engine can emit.

    Grouped by subject. The enum exists so the Swift side can mirror it exactly and
    the engine cannot emit a typo'd string at runtime.
    """

    # run lifecycle
    RUN_START = "run.start"
    RUN_QUEUED = "run.queued"
    RUN_ADMITTED = "run.admitted"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    RUN_ABORTED = "run.aborted"
    RUN_END = "run.end"

    # graph
    MANIFEST_PROPOSED = "manifest.proposed"
    MANIFEST_APPROVED = "manifest.approved"
    PHASE_ENTER = "phase.enter"
    PHASE_EXIT = "phase.exit"
    NODE_ENTER = "node.enter"
    NODE_EXIT = "node.exit"
    LOOP_PASS = "loop.pass"
    LOOP_STAGNATION = "loop.stagnation"

    # agents
    AGENT_SPAWN = "agent.spawn"
    AGENT_STATUS = "agent.status"
    AGENT_LOG = "agent.log"
    AGENT_SPAWN_REQUESTED = "agent.spawn.requested"
    AGENT_SPAWN_APPROVED = "agent.spawn.approved"
    AGENT_SPAWN_DENIED = "agent.spawn.denied"
    AGENT_SPAWNED = "agent.spawned"
    AGENT_DESTROYED = "agent.destroyed"
    AGENT_RETIREMENT_REVIEW = "agent.retirement_review"
    AGENT_ORG_CHANGED = "agent.org.changed"

    # model / llm
    LLM_REQUEST = "llm.request"
    LLM_RESPONSE = "llm.response"
    MODEL_CATALOG_REFRESHED = "model.catalog.refreshed"

    # work products
    ARTIFACT_WRITTEN = "artifact.written"
    CHECKLIST_RESULT = "checklist.result"
    REVIEW_REJECTED = "review.rejected"
    REVIEW_APPROVED = "review.approved"
    RUN_CRITERIA_SATISFIED = "run.criteria.satisfied"

    # routing & handoff
    ROUTE_PROPOSED = "route.proposed"
    ROUTE_DECIDED = "route.decided"
    ROUTE_OVERRIDDEN = "route.overridden"
    HANDOFF_PROPOSED = "handoff.proposed"
    HANDOFF_ACCEPTED = "handoff.accepted"
    HANDOFF_REJECTED = "handoff.rejected"
    HANDOFF_FULFILLED = "handoff.fulfilled"
    HANDOFF_BREACHED = "handoff.breached"
    HANDOFF_VERIFIED = "handoff.verified"
    DELEGATION_REJECTED = "delegation.rejected"

    # context / sessions
    SESSION_OPEN = "session.open"
    SESSION_SATURATION = "session.saturation"
    SESSION_COMPACT = "session.compact"
    SESSION_ROTATE_REQUESTED = "session.rotate.requested"
    SESSION_SEALED = "session.sealed"
    SESSION_HANDOFF_VERIFIED = "session.handoff.verified"
    SESSION_CLOSED = "session.closed"
    CONTEXT_IRREDUCIBLE_OVERFLOW = "context.irreducible_overflow"
    ATTENTION_DECAY = "attention.decay"

    # subagents — isolated child contexts with their own transcripts
    SUBAGENT_SPAWNED = "subagent.spawned"
    SUBAGENT_PROGRESS = "subagent.progress"
    SUBAGENT_DONE = "subagent.done"
    SUBAGENT_FAILED = "subagent.failed"
    SUBAGENT_READ = "subagent.read"

    # health & SLO
    AGENT_HEALTH_CHANGED = "agent.health.changed"
    AGENT_SLO_BREACH = "agent.slo.breach"
    AGENT_QUARANTINED = "agent.quarantined"
    AGENT_RECOVERED = "agent.recovered"
    AGENT_SPRAWL_SUSPECTED = "agent.sprawl.suspected"

    # human control
    HUMAN_GATE = "human.gate"
    HUMAN_DECISION = "human.decision"
    HUMAN_TAKEOVER = "human.takeover"
    HUMAN_RELEASED = "human.released"
    POLICY_CHANGED = "policy.changed"
    DECISION_RECORDED = "decision.recorded"

    # goal — the durable objective that keeps a run going
    GOAL_ARMED = "goal.armed"
    GOAL_RESUMED = "goal.resumed"
    GOAL_PROGRESS = "goal.progress"
    GOAL_PAUSED = "goal.paused"
    GOAL_COMPLETED = "goal.completed"
    GOAL_BLOCKED = "goal.blocked"
    GOAL_CLEARED = "goal.cleared"

    # ops
    BACKPRESSURE_ON = "backpressure.on"
    BACKPRESSURE_OFF = "backpressure.off"
    COST_CEILING = "cost.ceiling"
    COST_RECONCILED = "cost.reconciled"
    BUDGET_BURN = "budget.burn"
    WATCHDOG_RESTART = "watchdog.restart"
    SCHEMA_MIGRATED = "schema.migrated"
    SCHEMA_REFUSED = "schema.refused"
    EFFECT_APPLIED = "effect.applied"
    EFFECT_REPLAYED = "effect.replayed"
    LEAK_DETECTED = "leak.detected"
    SPAN_EXPORTED = "span.exported"
    DIAGNOSTICS_EXPORTED = "diagnostics.exported"
    ERROR = "error"
    COMMAND_ACK = "command.ack"


class CommandType(str, Enum):
    """Every command the app can send."""

    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    APPROVE = "approve"
    REJECT = "reject"
    INSTRUCT = "instruct"
    ASSIGN = "assign"
    SPAWN_AGENT = "spawn_agent"
    RENAME_AGENT = "rename_agent"
    REASSIGN = "reassign"
    TAKEOVER = "takeover"
    RELEASE = "release"
    INJECT = "inject"
    ROUTE = "route"
    SET_POLICY = "set_policy"
    RETIRE = "retire"
    QUARANTINE = "quarantine"
    RESTORE = "restore"
    SNAPSHOT = "snapshot"
    SHUTDOWN = "shutdown"
    # goal — the durable objective; `goal_*` because a bare `resume` already means "resume the run"
    GOAL_SET = "goal_set"
    GOAL_STATUS = "goal_status"
    GOAL_PAUSE = "goal_pause"
    GOAL_RESUME = "goal_resume"
    GOAL_CLEAR = "goal_clear"
    # subagents — the isolated children a run dispatched
    SUBAGENTS = "subagents"
    SUBAGENT_RESULT = "subagent_result"
    # providers — the endpoints and keys the console can configure
    PROVIDERS = "providers"
    PROVIDER_TEST = "provider_test"
    PROVIDER_ADD = "provider_add"
    PROVIDER_REMOVE = "provider_remove"
    # agents — the roster the console can grow and edit
    AGENTS = "agents"
    AGENT_UPDATE = "agent_update"
    AGENT_RETIRE = "agent_retire"
    SKILLS = "skills"
    # improver — the self-improvement loop: proposals only, never applied
    PROPOSALS = "proposals"
    IMPROVE = "improve"


@dataclass
class Event:
    """One engine → app frame.

    `type` is typed as `EventType | str` so a newer engine can emit an event this
    build does not know about; :meth:`is_known` reports which it is.
    """

    seq: int
    type: "EventType | str"
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    agent_id: str | None = None
    node_id: str | None = None
    session_id: str | None = None
    phase: str | None = None
    ts: str = ""
    v: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = _iso_now()

    @property
    def type_value(self) -> str:
        """The wire form of the type, whether it arrived as enum or string."""
        return self.type.value if isinstance(self.type, EventType) else str(self.type)

    def is_known(self) -> bool:
        """True when this event's type is one this build understands."""
        try:
            EventType(self.type_value)
            return True
        except ValueError:
            return False

    def to_dict(self, *, drop_empty: bool = True) -> dict[str, Any]:
        """Wire representation. Empty correlation fields are omitted to keep frames small."""
        data = asdict(self)
        data["type"] = self.type_value
        if drop_empty:
            for key in ("run_id", "agent_id", "node_id", "session_id", "phase"):
                if data.get(key) is None:
                    data.pop(key, None)
        return data


@dataclass
class Command:
    """One app → engine frame."""

    cmd_id: str
    type: "CommandType | str"
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = ""
    v: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = _iso_now()

    @property
    def type_value(self) -> str:
        return self.type.value if isinstance(self.type, CommandType) else str(self.type)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type_value
        return data


@dataclass
class Ack:
    """The engine's reply to a :class:`Command`, correlated by `cmd_id`."""

    cmd_id: str
    ok: bool
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_event(self, seq: int) -> Event:
        """Render the ack as an ordinary event so it shares one transport."""
        payload: dict[str, Any] = {"cmd_id": self.cmd_id, "ok": self.ok}
        if self.error:
            payload["error"] = self.error
        if self.detail:
            payload["detail"] = self.detail
        return Event(seq=seq, type=EventType.COMMAND_ACK, payload=payload)


def _iso_now() -> str:
    """UTC timestamp with millisecond precision, fixed-width for stable sorting."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def new_cmd_id() -> str:
    """Generate a unique command id.

    Time-prefixed so ids sort chronologically in a trace, and random-suffixed so two
    commands minted in the same millisecond cannot collide.
    """
    import os

    return f"cmd_{int(time.time() * 1000):013d}_{os.urandom(4).hex()}"


def encode(frame: Event | Command | dict[str, Any]) -> str:
    """Serialise a frame to a single NDJSON line (no trailing newline).

    Raises
    ------
    ProtocolError
        When the frame exceeds :data:`MAX_FRAME_BYTES`, which usually means an
        artifact body was inlined instead of referenced by path.
    """
    if isinstance(frame, Event):
        data = frame.to_dict()
    elif isinstance(frame, Command):
        data = frame.to_dict()
    elif isinstance(frame, dict):
        data = frame
    else:
        raise ProtocolError(f"cannot encode {type(frame).__name__}; expected Event, Command or dict")
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"frame exceeds {MAX_FRAME_BYTES} bytes; "
            "reference large artifacts by path instead of inlining them"
        )
    return text


def _check_version(data: dict[str, Any]) -> None:
    v = data.get("v", PROTOCOL_VERSION)
    if not isinstance(v, int):
        raise ProtocolError(f"frame 'v' must be an integer; got {v!r}")
    if v > PROTOCOL_VERSION:
        raise ProtocolError(
            f"frame declares protocol v{v} but this build speaks v{PROTOCOL_VERSION}. "
            "Upgrade the engine or the app so both halves agree."
        )


def decode_event(line: str) -> Event:
    """Parse one NDJSON line into an :class:`Event`.

    Accepts an unknown `type` (forward compatibility) but rejects a malformed frame,
    so a corrupt line is reported precisely rather than producing a half-built event.
    """
    data = _load_line(line, "event")
    _check_version(data)
    if "seq" not in data:
        raise ProtocolError("event frame is missing required field 'seq'")
    if not isinstance(data["seq"], int):
        raise ProtocolError(f"event 'seq' must be an integer; got {data['seq']!r}")
    if "type" not in data:
        raise ProtocolError("event frame is missing required field 'type'")
    # Explicit None check, not `or {}`: an empty list is falsy and would be silently
    # coerced into an empty dict, hiding a wrong-typed payload.
    payload = data.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ProtocolError(f"event 'payload' must be an object; got {type(payload).__name__}")
    return Event(
        seq=data["seq"],
        type=data["type"],
        payload=payload,
        run_id=data.get("run_id"),
        agent_id=data.get("agent_id"),
        node_id=data.get("node_id"),
        session_id=data.get("session_id"),
        phase=data.get("phase"),
        ts=data.get("ts") or _iso_now(),
        v=data.get("v", PROTOCOL_VERSION),
    )


def decode_command(line: str) -> Command:
    """Parse one NDJSON line into a :class:`Command`.

    A missing `cmd_id` is fatal: without it the ack cannot be correlated, so the UI
    would await a reply that never arrives.
    """
    data = _load_line(line, "command")
    _check_version(data)
    if not data.get("cmd_id"):
        raise ProtocolError("command frame is missing required field 'cmd_id'")
    if not data.get("type"):
        raise ProtocolError("command frame is missing required field 'type'")
    # Same reasoning as decode_event: distinguish a missing payload from a wrong-typed one.
    payload = data.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ProtocolError(f"command 'payload' must be an object; got {type(payload).__name__}")
    return Command(
        cmd_id=str(data["cmd_id"]),
        type=data["type"],
        payload=payload,
        ts=data.get("ts") or _iso_now(),
        v=data.get("v", PROTOCOL_VERSION),
    )


def _load_line(line: str, kind: str) -> dict[str, Any]:
    text = line.strip()
    if not text:
        raise ProtocolError(f"empty {kind} frame")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = text[:120] + ("…" if len(text) > 120 else "")
        raise ProtocolError(f"malformed {kind} JSON at column {exc.colno}: {exc.msg} | {preview}") from exc
    if not isinstance(data, dict):
        raise ProtocolError(f"{kind} frame must be a JSON object; got {type(data).__name__}")
    return data


def parse_stream(lines: Iterable[str], *, strict: bool = False) -> list[Event]:
    """Decode a sequence of NDJSON lines into events.

    With ``strict=False`` (the default) a malformed line is skipped rather than
    aborting: replaying a partially-written `trace.jsonl` after a crash should recover
    every complete record, not refuse the whole file because the last write was torn.
    """
    events: list[Event] = []
    for line in lines:
        try:
            events.append(decode_event(line))
        except ProtocolError:
            if strict:
                raise
            continue
    return events
