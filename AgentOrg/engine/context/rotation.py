#!/usr/bin/env python3
"""rotation.py — when to start a fresh session, and the handoff that carries the state.

WHY THIS EXISTS
---------------
Compaction shrinks a session. Rotation replaces it. They are different tools for different problems,
and using the wrong one wastes a session or burns budget:

- **Capacity** rotation is for when compaction has run out of things to remove.
- **Attention-decay** rotation is for when the session is *small enough* but the model has stopped
  attending to it — a rule read at turn 1 is only ~60% as likely to be followed by turn 15.
- **Phase-change** rotation is for when the work has moved on and the carried research is now noise.

The subtle and important part is what a rotation must *not* do. If a fresh session would immediately
overflow, rotating cannot help — the irreducible content is too large — so the design refuses rather
than looping, which is the difference between a bounded system and one that rotates forever.

DESIGN
------
- **Three triggers, three guards.** The guards (no-progress, cap, checksum) are what make rotation
  safe; the triggers alone would let it spin.
- **The handoff is capped at 12,000 tokens** by rule R1, so a fresh session never starts bloated.
- **Rotation is exempt from the self-handoff rule** (R3) because it keeps the same skill by design —
  expressed as a distinct `kind` rather than by weakening the rule.
- **The payload is checksummed** (R4), so a corrupted rotation aborts instead of propagating bad state.

Usage:
    decision = decide_rotation(session, config, projection=projection)
    if decision.should_rotate:
        handoff = build_handoff(session, decision, ...)
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .session import Session, SessionState

__all__ = [
    "RotationDecision",
    "RotationGuardError",
    "RotationTrigger",
    "SessionHandoff",
    "build_handoff",
    "decide_rotation",
]

#: Rule R1's ceiling on a rotation payload. Bounded by construction, which is why a fresh session
#: cannot start already bloated.
MAX_HANDOFF_TOKENS = 12_000

#: Rule R6's ceiling on unresolved questions crossing a rotation.
MAX_OPEN_QUESTIONS = 3

#: The payload schema version, so a receiver can tell an old payload from a new one.
HANDOFF_VERSION = "1.0.0"


class RotationGuardError(RuntimeError):
    """Raised when a rotation would breach a guard.

    Carries the guard's name so a caller can report which one fired, and an operator can look it up.
    """

    def __init__(self, guard: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
        self.guard = guard
        self.detail = detail or {}
        super().__init__(f"[{guard}] {message}")


class RotationTrigger(str, Enum):
    """Why a session should rotate."""

    NONE = "none"
    CAPACITY = "capacity"                   # compaction is exhausted
    ATTENTION_DECAY = "attention_decay"     # the model has stopped attending
    PHASE_CHANGE = "phase_change"           # the work has moved on


@dataclass
class RotationDecision:
    """The verdict, with the reason and the guards that were checked."""

    should_rotate: bool
    trigger: RotationTrigger = RotationTrigger.NONE
    reason: str = ""
    saturation: float = 0.0
    attention_weight: float = 1.0
    # The guard that refused, when rotation was blocked.
    blocked_by: str = ""
    # True when rotation cannot help because the irreducible content is too large.
    impossible: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "should_rotate": self.should_rotate,
            "trigger": self.trigger.value,
            "reason": self.reason,
            "saturation": round(self.saturation, 4),
            "attention_weight": round(self.attention_weight, 4),
            "blocked_by": self.blocked_by,
            "impossible": self.impossible,
            "detail": self.detail,
        }


def decide_rotation(session: Session, *, rotation_count: int = 0, max_rotations: int = 4,
                    attention_floor: float = 0.30, rotate_on_phase_change: bool = True,
                    phase_changed: bool = False, projection: Any = None,
                    irreducible_saturation: float | None = None) -> RotationDecision:
    """Decide whether to rotate, and why.

    The order matters: an impossible rotation is detected first, because it must not be attempted at
    all. Then the cap, then the triggers.

    Parameters
    ----------
    session:
        The session to evaluate.
    rotation_count, max_rotations:
        The guard against a rotation storm. A run that rotates more than this is not making progress.
    attention_floor:
        The recency weight below which attention is considered lost. The library's figure is 0.30,
        reached at about turn 12.
    phase_changed:
        Whether the node advanced INTAKE→EXECUTE→…→DECIDE since the last turn. A natural checkpoint.
    projection:
        A :class:`~engine.context.projection.Projection`, used to detect the impossible case.
    irreducible_saturation:
        The floor, when a projection is not available.
    """
    saturation = session.saturation
    attention = session.attention_weight

    # The impossible case, checked first: rotating cannot reduce content compaction cannot remove, so
    # attempting it would produce a fresh session that overflows identically and a rotation loop.
    floor = irreducible_saturation
    if projection is not None:
        floor = getattr(projection, "irreducible_saturation", None)
    if floor is not None and floor >= 0.85:
        return RotationDecision(
            should_rotate=False,
            trigger=RotationTrigger.NONE,
            reason=(
                f"rotation cannot help: the irreducible content is {floor:.0%} of the usable window. "
                "A fresh session would overflow identically. Lower the skill tier, reduce the recall "
                "block, or raise context_window."
            ),
            saturation=saturation,
            attention_weight=attention,
            blocked_by="irreducible-overflow",
            impossible=True,
            detail={"irreducible_saturation": round(float(floor), 4)},
        )

    # The cap. A fourth rotation in one node means the work is not converging, which is a signal to
    # escalate rather than to rotate again.
    if rotation_count >= max_rotations:
        return RotationDecision(
            should_rotate=False,
            reason=(
                f"the rotation cap is reached ({rotation_count} of {max_rotations}). Rotating again "
                "would not be progress; escalate with what has been tried."
            ),
            saturation=saturation,
            attention_weight=attention,
            blocked_by="rotation-cap",
        )

    # Trigger 1: capacity — compaction has been applied and saturation is still critical.
    if saturation >= 0.85:
        return RotationDecision(
            should_rotate=True, trigger=RotationTrigger.CAPACITY,
            reason=(
                f"saturation is {saturation:.0%} after compaction, so in-session compaction is "
                "exhausted. A fresh session with a bounded handoff is the only way forward."
            ),
            saturation=saturation, attention_weight=attention,
            detail={"rotation_count": rotation_count, "max_rotations": max_rotations},
        )

    # Trigger 2: attention decay — the session fits, but the model has stopped attending to it.
    if attention < attention_floor:
        return RotationDecision(
            should_rotate=True, trigger=RotationTrigger.ATTENTION_DECAY,
            reason=(
                f"attention weight for the oldest turns has fallen to {attention:.2f}, below the "
                f"floor of {attention_floor:.2f} ({session.turns_count} turns). The session is not "
                "too large — it is no longer being attended to, and rotation re-pins the guardrails "
                "to the primacy zone."
            ),
            saturation=saturation, attention_weight=attention,
            detail={"turns": session.turns_count, "floor": attention_floor},
        )

    # Trigger 3: phase change — a natural checkpoint at which carried research becomes noise.
    if rotate_on_phase_change and phase_changed:
        return RotationDecision(
            should_rotate=True, trigger=RotationTrigger.PHASE_CHANGE,
            reason=(
                f"the node moved to phase {session.phase!r}. A phase change is a natural checkpoint, "
                "and research carried across it is noise for the next phase."
            ),
            saturation=saturation, attention_weight=attention,
            detail={"phase": session.phase},
        )

    return RotationDecision(
        should_rotate=False,
        reason=(
            f"saturation {saturation:.0%} and attention {attention:.2f} are both within bounds; "
            "no rotation is warranted"
        ),
        saturation=saturation, attention_weight=attention,
        detail={"turns": session.turns_count},
    )


@dataclass
class SessionHandoff:
    """The bounded payload that carries state from one session to the next.

    Deliberately not the transcript. A rotation exists to *reduce* what is carried, so the payload is
    the distilled state: the constraints that must survive, the decisions that were made, the
    artifacts in flight, and what is still open.
    """

    handoff_version: str = HANDOFF_VERSION
    # "session-rotation" so the receiver knows this is a rotation, which exempts it from rule R3.
    kind: str = "session-rotation"
    run_id: str = ""
    node_id: str = ""
    agent_id: str = ""
    agent_name: str = ""
    skill: str = ""
    model: str = ""
    from_session: str = ""
    to_session: str = ""
    session_index: int = 1
    node_phase: str = ""
    attempt: int = 1
    reason: str = ""
    saturation_at_rotation: float = 0.0
    constraints: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    context_pruned: dict[str, Any] = field(default_factory=dict)
    verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _iso_now()
        if not self.checksum:
            self.checksum = self.compute_checksum()

    def payload(self) -> dict[str, Any]:
        """The handoff body, excluding the checksum (which covers the body)."""
        return {
            "handoff_version": self.handoff_version,
            "kind": self.kind,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "skill": self.skill,
            "model": self.model,
            "from_session": self.from_session,
            "to_session": self.to_session,
            "session_index": self.session_index,
            "node_phase": self.node_phase,
            "attempt": self.attempt,
            "reason": self.reason,
            "saturation_at_rotation": round(self.saturation_at_rotation, 4),
            "constraints": self.constraints,
            "decisions": self.decisions,
            "artifacts": self.artifacts,
            "open_questions": self.open_questions,
            "context_pruned": self.context_pruned,
            "verification_evidence": self.verification_evidence,
            "created_at": self.created_at,
        }

    def compute_checksum(self) -> str:
        """The sha256 of the payload, for rule R4.

        Deterministic, so the same state hashes identically and a redelivery is recognisable.
        """
        material = json.dumps(self.payload(), sort_keys=True, default=str)
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def estimated_tokens(self) -> int:
        """Rough token cost of carrying this payload, for rule R1."""
        return max(1, len(json.dumps(self.payload(), default=str)) // 4)

    def verify(self) -> None:
        """Check the invariants a rotation must satisfy.

        Raises
        ------
        RotationGuardError
            Named by the guard that fired, so the failure is actionable.
        """
        # R1 — the payload must be bounded, or the fresh session starts bloated.
        tokens = self.estimated_tokens()
        if tokens > MAX_HANDOFF_TOKENS:
            raise RotationGuardError(
                "R1",
                f"the handoff payload is ~{tokens} tokens, over the {MAX_HANDOFF_TOKENS} ceiling. "
                "Prune the context: reference artifacts by path rather than inlining them.",
                detail={"estimated_tokens": tokens, "limit": MAX_HANDOFF_TOKENS},
            )
        # R2 — every non-negotiable constraint must survive verbatim.
        lost = [c for c in self.constraints if c.get("non_negotiable") and not c.get("value")]
        if lost:
            raise RotationGuardError(
                "R2",
                f"{len(lost)} non-negotiable constraint(s) have no value and would be lost. A "
                "rotation must preserve every NEVER/MUST NOT rule verbatim.",
                detail={"lost": lost},
            )
        # R6 — too much unresolved uncertainty must not compound across a rotation.
        if len(self.open_questions) > MAX_OPEN_QUESTIONS:
            raise RotationGuardError(
                "R6",
                f"{len(self.open_questions)} open questions exceed the {MAX_OPEN_QUESTIONS} "
                "ceiling. Resolve them, or escalate rather than compounding uncertainty.",
                detail={"count": len(self.open_questions), "limit": MAX_OPEN_QUESTIONS},
            )
        # R4 — the checksum must match the payload.
        expected = self.compute_checksum()
        if self.checksum != expected:
            raise RotationGuardError(
                "R4",
                "the payload changed after its checksum was recorded. Refusing to hand off state "
                "that may be corrupt.",
                detail={"recorded": self.checksum, "computed": expected},
            )

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "checksum": self.checksum}


def build_handoff(session: Session, decision: RotationDecision, *, run_id: str = "",
                  agent_name: str = "", skill: str = "", model: str = "",
                  constraints: Iterable[dict[str, Any]] = (), decisions: Iterable[dict[str, Any]] = (),
                  artifacts: Iterable[dict[str, Any]] = (),
                  open_questions: Iterable[dict[str, Any]] = (),
                  verification_evidence: Iterable[dict[str, Any]] = (),
                  to_session: str = "", pruned: dict[str, Any] | None = None) -> SessionHandoff:
    """Build and validate the handoff for a rotation.

    The session's own pinned constraints are included automatically, because they are the one thing a
    rotation must never drop — the whole point of the AR-04 guard is that a constraint does not
    disappear between two agents who each believed the other kept it.

    Raises
    ------
    RotationGuardError
        When the payload breaches R1, R2, R4 or R6. The caller should escalate rather than retry.
    """
    if not decision.should_rotate:
        raise RotationGuardError(
            "trigger", f"no rotation was decided ({decision.reason}), so there is nothing to hand off"
        )

    # Every pinned constraint, marked non-negotiable so the receiver treats it as such and the R2
    # check has something to verify.
    pinned = [{"type": "constraint", "value": text, "source": "session", "non_negotiable": True}
              for text in session.pinned]
    # A caller-supplied constraint that is not already pinned is added as negotiable, since it was
    # not explicitly pinned on this session.
    supplied = list(constraints)
    combined = pinned + [c for c in supplied if c.get("value") not in session.pinned]

    from .session import new_session_id

    handoff = SessionHandoff(
        run_id=run_id,
        node_id=session.node_id,
        agent_id=session.agent_id,
        agent_name=agent_name,
        skill=skill,
        model=model,
        from_session=session.session_id,
        to_session=to_session or new_session_id(session.index + 1),
        session_index=session.index + 1,
        node_phase=session.phase,
        attempt=session.attempt,
        reason=decision.reason,
        saturation_at_rotation=decision.saturation,
        constraints=combined,
        decisions=list(decisions),
        artifacts=list(artifacts),
        open_questions=list(open_questions),
        verification_evidence=list(verification_evidence),
        context_pruned=pruned or {
            "removed_turns": session.turns_count,
            "tokens_before": session.used_tokens,
            "preserved_verbatim_count": len(session.pinned),
            "rotation_trigger": decision.trigger.value,
        },
    )
    handoff.verify()
    return handoff


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
