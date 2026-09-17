#!/usr/bin/env python3
"""session.py — one bounded context window, with a turn-boundary state machine.

WHY THIS EXISTS
---------------
A session is the unit of *attention*, not of work. A model's effective attention runs out well
before its window does — the library's research puts it at roughly 70% — so the interesting question
is not "does this fit" but "is this still being attended to".

This module owns the state that answers that: the saturation band, the turn count and its attention
decay, the pinned constraints that must survive compaction, and the lifecycle that decides when to
compact and when to rotate.

DESIGN
------
- **Transitions happen only at a turn boundary.** Compacting during generation would prune references
  the model is mid-way through using, which produces corrupted output rather than an error.
- **Pinned constraints are tracked as a count, not a hope.** Rule AR-04 is enforced by comparing the
  count before and after a compaction; a drop reverts it.
- **The band is derived, never set.** A caller asking "which band am I in?" cannot get an answer that
  disagrees with the saturation it just computed.
- **Rotation is a distinct primitive from a handoff.** A rotation keeps the same skill deliberately, so
  it is exempt from the self-handoff rule by construction rather than by special-casing.

Usage:
    session = Session(agent_id="ag_1", node_id="fixer", window=32768)
    session.append(Turn(role="user", text="..."))
    band = session.band             # HEALTHY | WARNING | CRITICAL | OVERFLOW
    if session.should_compact(context_config):
        result = compact(session, context_config)
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

__all__ = [
    "Band",
    "ContextBudgetError",
    "Session",
    "SessionError",
    "SessionState",
    "Turn",
]


class SessionError(RuntimeError):
    """Raised on an illegal session operation."""


class ContextBudgetError(SessionError):
    """Raised when a prompt cannot fit even after compaction.

    Distinct from a generic error because the correct response differs: this one means the
    *irreducible* content is too large, so rotating cannot help — the fix is a lower skill tier, a
    smaller recall block, or a bigger window.
    """

    def __init__(self, message: str, *, irreducible_tokens: int, window: int) -> None:
        self.irreducible_tokens = irreducible_tokens
        self.window = window
        super().__init__(message)


class Band(str, Enum):
    """The saturation band, with the action each implies.

    The thresholds are the library's: proactive compaction at 70% because attention degrades before
    capacity runs out, eviction at 85%, emergency at 95%.
    """

    HEALTHY = "healthy"      # < 70%
    WARNING = "warning"      # 70-84%
    CRITICAL = "critical"    # 85-94%
    OVERFLOW = "overflow"    # >= 95%

    @property
    def requires_action(self) -> bool:
        """Whether this band demands intervention before the next call."""
        return self is not Band.HEALTHY

    def as_dict(self) -> dict[str, Any]:
        return {"band": self.value, "requires_action": self.requires_action}


class SessionState(str, Enum):
    """Where a session is in its lifecycle."""

    ACTIVE = "active"          # normal operation
    SEALING = "sealing"        # being compacted and serialized for handoff
    HANDOFF = "handoff"        # the handoff payload is written, pending verification
    CLOSED = "closed"          # archived; its transcript is never re-sent


def new_session_id(index: int = 1) -> str:
    """A session id that sorts chronologically within an agent's history."""
    return f"ses_{index:03d}_{uuid.uuid4().hex[:4]}"


@dataclass
class Turn:
    """One exchange in a session.

    `tier` records the disclosure tier the text was loaded at, so compaction knows what it may drop:
    a tier-3 example is evictable, a tier-1 ground rule is not.
    """

    role: str
    text: str
    tokens: int = 0
    tier: int = 2
    at: float = field(default_factory=time.time)
    # True for text that must never be lossily compacted (a NEVER/MUST NOT rule, a non-negotiable).
    pinned: bool = False
    # The criterion or checklist id this turn served, when it was a work turn.
    serves: str = ""

    def __post_init__(self) -> None:
        if not self.tokens:
            # ~4 characters per token is the usual English approximation, and this figure is
            # corrected against real usage by the estimator once a call returns.
            self.tokens = max(1, len(self.text) // 4)

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text, "tokens": self.tokens,
                "tier": self.tier, "pinned": self.pinned, "serves": self.serves, "at": self.at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Turn":
        return cls(
            role=str(data.get("role") or "user"),
            text=str(data.get("text") or ""),
            tokens=int(data.get("tokens") or 0),
            tier=int(data.get("tier") or 2),
            at=float(data.get("at") or time.time()),
            pinned=bool(data.get("pinned", False)),
            serves=str(data.get("serves") or ""),
        )


@dataclass
class Session:
    """One bounded context window for one agent on one node.

    Parameters
    ----------
    window:
        The model's context window. Must be real — the whole projection depends on it, which is why
        an agent bound to a model with an unknown window is refused at binding time.
    pinned:
        Text that must survive compaction verbatim: `NEVER`/`MUST NOT` rules and every
        `non_negotiable` constraint. Tracked as a list so the count can be compared after a
        compaction, which is what makes AR-04 enforceable rather than advisory.
    """

    agent_id: str
    node_id: str = ""
    window: int = 0
    session_id: str = ""
    index: int = 1
    state: SessionState = SessionState.ACTIVE
    turns: list[Turn] = field(default_factory=list)
    pinned: list[str] = field(default_factory=list)
    # The node phase this session is working in, so the phase-change trigger can fire.
    phase: str = ""
    # The attempt number, for the review loop's rework passes.
    attempt: int = 1
    rotation_reason: str = ""
    rotation_count: int = 0
    created_at: float = field(default_factory=time.time)
    sealed_at: float | None = None
    # The window actually available for input: the window minus the reply reserve. Set by the caller
    # from `ContextConfig.output_reserve`, so the projection never fills the window completely.
    output_reserve: int = 0

    def __post_init__(self) -> None:
        if self.window <= 0:
            raise SessionError(
                f"session for {self.agent_id!r} needs a positive context window; got {self.window}. "
                "An agent bound to a model with an unknown window is refused for exactly this reason."
            )
        if not self.session_id:
            self.session_id = new_session_id(self.index)

    # ── capacity ────────────────────────────────────────────────────────────

    @property
    def usable_window(self) -> int:
        """The window minus the reply reserve.

        A prompt that fills the window leaves no room for the answer, so the reserve is subtracted
        before any saturation figure is computed.
        """
        return max(1, self.window - max(0, self.output_reserve))

    @property
    def history_tokens(self) -> int:
        """Tokens held by the conversation history."""
        return sum(turn.tokens for turn in self.turns)

    @property
    def pinned_tokens(self) -> int:
        """Tokens held by pinned constraints — the irreducible part of the history."""
        return sum(max(1, len(text) // 4) for text in self.pinned)

    @property
    def used_tokens(self) -> int:
        """Total tokens currently held, history plus pinned."""
        return self.history_tokens + self.pinned_tokens

    @property
    def saturation(self) -> float:
        """Fraction of the usable window in use, bounded to [0, 1]."""
        return min(1.0, self.used_tokens / self.usable_window)

    @property
    def turns_count(self) -> int:
        """How many turns this session has taken."""
        return len(self.turns)

    @property
    def attention_weight(self) -> float:
        """Recency-weighted attention for the *oldest* turn, per the library's λ=0.1 decay.

        At turn 1 this is 0.90, and by turn 12 it has fallen to about 0.30 — which is where the
        library says an agent starts ignoring instructions it was given early. The figure is the
        signal the attention-decay rotation trigger uses.
        """
        if not self.turns:
            return 1.0
        age = self.turns_count
        return math.exp(-0.1 * age)

    @property
    def band(self) -> Band:
        """The saturation band, derived so it cannot disagree with `saturation`."""
        value = self.saturation
        if value >= 0.95:
            return Band.OVERFLOW
        if value >= 0.85:
            return Band.CRITICAL
        if value >= 0.70:
            return Band.WARNING
        return Band.HEALTHY

    def headroom(self, *, reserve_tokens: int = 0) -> int:
        """Tokens still available, after an optional reservation for the next message."""
        return max(0, self.usable_window - self.used_tokens - max(0, reserve_tokens))

    def fits(self, tokens: int) -> bool:
        """Whether `tokens` more would fit without compacting."""
        return tokens <= self.headroom()

    # ── mutation (turn boundaries only) ─────────────────────────────────────

    def _assert_active(self) -> None:
        if self.state is not SessionState.ACTIVE:
            raise SessionError(
                f"session {self.session_id} is {self.state.value}; only an ACTIVE session takes turns"
            )

    def append(self, turn: Turn) -> None:
        """Add a turn.

        Raises
        ------
        SessionError
            When the session is no longer active. Appending to a sealing session would put text into
            a transcript that has already been serialized.
        """
        self._assert_active()
        self.turns.append(turn)

    def append_text(self, role: str, text: str, *, tier: int = 2, serves: str = "",
                    pinned: bool = False) -> Turn:
        """Convenience: build and append a turn.

        `pinned` marks the turn's *text* as protected from lossy compaction, which is distinct from
        `session.pin(text)` — pinning records the constraint itself, while this marks the turn that
        carries it so a compaction cannot evict it wholesale.
        """
        turn = Turn(role=role, text=text, tier=tier, serves=serves, pinned=pinned)
        self.append(turn)
        return turn

    def pin(self, text: str) -> None:
        """Record a constraint that must survive compaction verbatim.

        Idempotent: pinning the same rule twice would inflate the count and make the AR-04 check
        meaningless, so a duplicate is ignored.
        """
        cleaned = (text or "").strip()
        if cleaned and cleaned not in self.pinned:
            self.pinned.append(cleaned)

    def unpin(self, text: str) -> bool:
        """Remove a pin. Returns whether it was present.

        Removing a pin is a deliberate act — a constraint is normally unpinned only when the decision
        that produced it is superseded — so the caller is told if nothing changed.
        """
        cleaned = (text or "").strip()
        if cleaned in self.pinned:
            self.pinned.remove(cleaned)
            return True
        return False

    def begin_phase(self, phase: str) -> bool:
        """Move to a new node phase, reporting whether it actually changed.

        A phase change is one of the three rotation triggers, so the caller needs to know whether it
        happened rather than assuming.
        """
        if phase == self.phase:
            return False
        self.phase = phase
        return True

    # ── lifecycle ───────────────────────────────────────────────────────────

    def seal(self, *, reason: str) -> None:
        """Stop taking turns, recording why.

        Raises
        ------
        SessionError
            When the session is not ACTIVE. Sealing an already-sealed session would overwrite the
            reason on a transcript that is being serialized, which loses why the rotation happened.
        """
        if self.state is not SessionState.ACTIVE:
            raise SessionError(
                f"session {self.session_id} is {self.state.value} and cannot be sealed again; "
                f"the rotation reason {self.rotation_reason!r} would be overwritten"
            )
        self.state = SessionState.SEALING
        self.rotation_reason = reason
        self.sealed_at = time.time()

    def mark_handoff(self) -> None:
        """Record that the handoff payload has been written and is pending verification."""
        if self.state is not SessionState.SEALING:
            raise SessionError(
                f"session {self.session_id} is {self.state.value}; it must be SEALING before a "
                "handoff is written"
            )
        self.state = SessionState.HANDOFF

    def close(self) -> None:
        """Archive the session. Its transcript is never re-sent."""
        if self.state is SessionState.CLOSED:
            return
        self.state = SessionState.CLOSED

    @property
    def closed(self) -> bool:
        return self.state is SessionState.CLOSED

    # ── serialisation ───────────────────────────────────────────────────────

    def as_dict(self, *, include_turns: bool = True) -> dict[str, Any]:
        """Serialise the session.

        `include_turns=False` gives the summary used in telemetry and the UI, where the full
        transcript would be both large and sensitive.
        """
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "session_index": self.index,
            "agent_id": self.agent_id,
            "node_id": self.node_id,
            "phase": self.phase,
            "attempt": self.attempt,
            "state": self.state.value,
            "window": self.window,
            "usable_window": self.usable_window,
            "output_reserve": self.output_reserve,
            "sat_tokens": self.used_tokens,
            "saturation": round(self.saturation, 4),
            "band": self.band.value,
            "turns": self.turns_count,
            "attention_weight": round(self.attention_weight, 4),
            "pinned_count": len(self.pinned),
            "rotation_reason": self.rotation_reason,
            "rotation_count": self.rotation_count,
            "created_at": self.created_at,
            "sealed_at": self.sealed_at,
        }
        if include_turns:
            payload["turns"] = [turn.as_dict() for turn in self.turns]
            payload["pinned"] = list(self.pinned)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        """Rebuild a session from a persisted dict."""
        try:
            state = SessionState(str(data.get("state") or "active"))
        except ValueError:
            state = SessionState.ACTIVE
        session = cls(
            agent_id=str(data.get("agent_id") or ""),
            node_id=str(data.get("node_id") or ""),
            window=int(data.get("window") or 0),
            session_id=str(data.get("session_id") or ""),
            index=int(data.get("session_index") or 1),
            state=state,
            phase=str(data.get("phase") or ""),
            attempt=int(data.get("attempt") or 1),
            rotation_reason=str(data.get("rotation_reason") or ""),
            rotation_count=int(data.get("rotation_count") or 0),
            output_reserve=int(data.get("output_reserve") or 0),
        )
        session.turns = [Turn.from_dict(t) for t in (data.get("turns") or []) if isinstance(t, dict)]
        session.pinned = [str(p) for p in (data.get("pinned") or [])]
        return session

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"Session({self.session_id}, {self.band.value}, "
                f"{self.saturation:.0%}, turns={self.turns_count})")
