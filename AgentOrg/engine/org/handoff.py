#!/usr/bin/env python3
"""handoff.py — the handoff contract and its mechanical validators (R1–R8).

WHY THIS EXISTS
---------------
A handoff is the only thing that crosses a node boundary, and it is where an agent system
fails quietly: state corruption across handoffs produces confidently wrong downstream work
rather than an error. The library specifies eight mechanical rules for this, and the point of
implementing them here is that they are *checked in code* rather than requested in a prompt.

A prompt that says "do not drop non-negotiable constraints" is advisory. A validator that
compares the constraint count and refuses the handoff is a mechanism.

DESIGN
------
- **The lifecycle is explicit**: PROPOSED → ACCEPTED/REJECTED → IN_PROGRESS → FULFILLED or
  BREACHED, with ESCALATED as the terminal escape. The design's diagram is the state machine.
- **Each rule has an id and its own check**, so a refusal names *which* rule failed — an
  operator can read "R2: constraint count dropped from 5 to 3" and act.
- **The rules are enforced at the transition they belong to**, not all at once: R4 (checksum)
  when receiving, R2 (constraints) when accepting, R6 (open questions) when proposing.
- **A refusal carries the evidence**, not just a verdict, because the upstream agent has to
  understand what to fix.

Usage:
    verdict = validate_handoff(handoff, stage="propose")
    if not verdict.ok:
        raise HandoffError(verdict.reason)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Iterable

__all__ = [
    "Handoff",
    "HandoffError",
    "HandoffState",
    "RuleViolation",
    "ValidationVerdict",
    "validate_handoff",
    "HANDOFF_VERSION",
]

#: The payload schema version. Carried so a receiver can tell an old payload from a new one.
HANDOFF_VERSION = "1.0.0"

#: The nine required payload fields, from the library's handoff payload registry.
REQUIRED_FIELDS: tuple[str, ...] = (
    "status",
    "summary",
    "artifacts",
    "decisions",
    "open_questions",
    "verification_evidence",
    "context",
    "budget",
    "next",
)

#: The five-element context pass-through a delegation must carry.
CONTEXT_ELEMENTS: tuple[str, ...] = (
    "problem",
    "tried",
    "logs",
    "paths",
    "hypothesis",
)

#: Rule R1's ceiling on a handoff payload, in estimated tokens. Above this the receiver starts
#: its turn already bloated, which is the state rotation exists to avoid.
MAX_HANDOFF_TOKENS = 12_000

#: Rule R6's ceiling on unresolved questions. Above three, uncertainty compounds faster than
#: it can be resolved, so the pipeline pauses instead.
MAX_OPEN_QUESTIONS = 3

_VALID_STATUS = ("done", "blocked", "needs_review", "skipped")


class HandoffError(RuntimeError):
    """Raised when a handoff is refused or an illegal transition is attempted."""


class HandoffState(str, Enum):
    """Where a handoff contract is in its lifecycle."""

    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    IN_PROGRESS = "IN_PROGRESS"
    FULFILLED = "FULFILLED"
    BREACHED = "BREACHED"
    ESCALATED = "ESCALATED"


@dataclass(frozen=True)
class RuleViolation:
    """One failed rule, named so it is actionable."""

    rule: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "message": self.message, "detail": self.detail}


@dataclass(frozen=True)
class ValidationVerdict:
    """The result of validating a handoff at one stage."""

    ok: bool
    stage: str
    violations: tuple[RuleViolation, ...] = ()

    @property
    def reason(self) -> str:
        """A single readable line naming every failed rule."""
        if self.ok:
            return "handoff accepted"
        return "; ".join(f"{v.rule}: {v.message}" for v in self.violations)

    @property
    def rules(self) -> tuple[str, ...]:
        """The failed rule ids."""
        return tuple(v.rule for v in self.violations)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "stage": self.stage,
                "violations": [v.as_dict() for v in self.violations]}


@dataclass
class Handoff:
    """A handoff payload plus its contract state.

    Parameters
    ----------
    payload:
        The wire payload. Must carry the nine registry fields.
    origin / target:
        The agents or skills at each end. R3 refuses a self-handoff — except for a session
        rotation, which is why `kind` exists.
    """

    payload: dict[str, Any]
    origin: str
    target: str
    handoff_id: str = ""
    state: HandoffState = HandoffState.PROPOSED
    # "handoff" | "session-rotation". A rotation keeps the same skill deliberately, so it is
    # exempt from R3 by construction rather than by special-casing the rule.
    kind: str = "handoff"
    attempt: int = 1
    # R8: a decision that overrides an earlier one must carry a SUPERSEDED marker.
    supersedes: dict[str, Any] | None = None
    created_at: str = ""
    # R4: the checksum of the serialised state, recorded by the sender and verified by the
    # receiver. A mismatch aborts rather than propagating bad state.
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _iso_now()
        if not self.handoff_id:
            self.handoff_id = self.compute_id()
        if not self.checksum:
            self.checksum = self.compute_checksum()

    # ── identity and integrity ──────────────────────────────────────────────

    def compute_id(self) -> str:
        """A stable id derived from the payload, so the same state has the same id.

        Deterministic rather than random: two attempts to send identical state are the same
        handoff, which is what makes redelivery idempotent.
        """
        material = json.dumps(self.payload, sort_keys=True, default=str)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def compute_checksum(self) -> str:
        """The sha256 of the payload, for rule R4."""
        material = json.dumps(self.payload, sort_keys=True, default=str)
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def non_negotiable(self) -> list[dict[str, Any]]:
        """Every constraint the payload marks non-negotiable.

        These are the constraints that must survive a handoff untouched; rule R2 exists because
        a silently dropped `NEVER store passwords in plaintext` is how a security property
        disappears between two agents who each believed the other kept it.
        """
        constraints = (self.payload.get("constraints") or [])
        if isinstance(constraints, dict):
            constraints = list(constraints.values())
        out: list[dict[str, Any]] = []
        for entry in constraints:
            if isinstance(entry, dict) and entry.get("non_negotiable"):
                out.append(entry)
        return out

    def estimated_tokens(self) -> int:
        """Rough token cost of carrying this payload, for rule R1."""
        material = json.dumps(self.payload, default=str)
        return max(1, len(material) // 4)

    # ── transitions ─────────────────────────────────────────────────────────

    def accept(self, *, stage_checks: bool = True) -> ValidationVerdict:
        """Move to ACCEPTED, validating the acceptance rules first.

        R7 (no delivery while merely proposed) is deliberately *not* checked here: acceptance
        is the transition that satisfies R7, so checking it before transitioning would refuse
        every legitimate acceptance. R7 is enforced on delivery and on fulfilment.

        Raises
        ------
        HandoffError
            When the transition is illegal or a rule fails. The state is left unchanged so a
            refused handoff does not appear accepted.
        """
        self._assert_transition(HandoffState.ACCEPTED)
        verdict = validate_handoff(self, stage="accept") if stage_checks else ValidationVerdict(True, "accept")
        if not verdict.ok:
            raise HandoffError(
                f"handoff {self.handoff_id} from {self.origin} to {self.target} was refused at "
                f"acceptance: {verdict.reason}"
            )
        self.state = HandoffState.ACCEPTED
        return verdict

    def reject(self, reason: str) -> None:
        """Move to REJECTED with a reason the origin can act on."""
        self._assert_transition(HandoffState.REJECTED)
        self.payload.setdefault("rejection", {})
        if isinstance(self.payload["rejection"], dict):
            self.payload["rejection"].update({"reason": reason, "at": _iso_now()})
        self.state = HandoffState.REJECTED

    def start(self) -> None:
        """Move to IN_PROGRESS once the receiver has begun work."""
        self._assert_transition(HandoffState.IN_PROGRESS)
        self.state = HandoffState.IN_PROGRESS

    def fulfil(self) -> ValidationVerdict:
        """Move to FULFILLED once delivery has been verified.

        R7 is checked before the transition, because fulfilling a handoff that was never
        accepted would record delivery of a contract the receiver never agreed to.
        """
        self._assert_transition(HandoffState.FULFILLED)
        verdict = validate_handoff(self, stage="fulfil")
        if not verdict.ok:
            raise HandoffError(
                f"handoff {self.handoff_id} cannot be fulfilled: {verdict.reason}"
            )
        self.state = HandoffState.FULFILLED
        return verdict

    def breach(self, reason: str) -> None:
        """Move to BREACHED when delivery fails acceptance."""
        self._assert_transition(HandoffState.BREACHED)
        self.payload.setdefault("breach", {})
        if isinstance(self.payload["breach"], dict):
            self.payload["breach"].update({"reason": reason, "at": _iso_now()})
        self.state = HandoffState.BREACHED

    def escalate(self, reason: str) -> None:
        """Move to ESCALATED — the terminal escape that reaches a human."""
        self.state = HandoffState.ESCALATED
        self.payload.setdefault("escalation", {})
        if isinstance(self.payload["escalation"], dict):
            self.payload["escalation"].update({"reason": reason, "at": _iso_now()})

    #: The legal transitions. Encoded as a table because an implicit state machine is where
    #: "how did it get to IN_PROGRESS without being accepted?" bugs live.
    #:
    #: Declared as a ClassVar so the dataclass machinery does not treat it as a field default.
    _TRANSITIONS: ClassVar[dict[HandoffState, tuple[HandoffState, ...]]] = {
        HandoffState.PROPOSED: (HandoffState.ACCEPTED, HandoffState.REJECTED, HandoffState.ESCALATED),
        HandoffState.ACCEPTED: (HandoffState.IN_PROGRESS, HandoffState.ESCALATED),
        HandoffState.REJECTED: (HandoffState.PROPOSED, HandoffState.ESCALATED),
        HandoffState.IN_PROGRESS: (HandoffState.FULFILLED, HandoffState.BREACHED, HandoffState.ESCALATED),
        HandoffState.FULFILLED: (),
        HandoffState.BREACHED: (HandoffState.ESCALATED,),
        HandoffState.ESCALATED: (HandoffState.PROPOSED,),
    }

    def _assert_transition(self, target: HandoffState) -> None:
        allowed = self._TRANSITIONS.get(self.state, ())
        if target not in allowed:
            raise HandoffError(
                f"illegal handoff transition {self.state.value} -> {target.value} for "
                f"{self.handoff_id}. Allowed from {self.state.value}: "
                + (", ".join(s.value for s in allowed) or "(terminal)")
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "handoff_version": HANDOFF_VERSION,
            "handoff_id": self.handoff_id,
            "kind": self.kind,
            "origin": self.origin,
            "target": self.target,
            "state": self.state.value,
            "attempt": self.attempt,
            "checksum": self.checksum,
            "created_at": self.created_at,
            "supersedes": self.supersedes,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Handoff":
        """Rebuild from a persisted dict, verifying the checksum (rule R4)."""
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        handoff = cls(
            payload=payload,
            origin=str(data.get("origin") or ""),
            target=str(data.get("target") or ""),
            handoff_id=str(data.get("handoff_id") or ""),
            kind=str(data.get("kind") or "handoff"),
            attempt=int(data.get("attempt", 1)),
            supersedes=data.get("supersedes") if isinstance(data.get("supersedes"), dict) else None,
            created_at=str(data.get("created_at") or ""),
            checksum=str(data.get("checksum") or ""),
        )
        try:
            handoff.state = HandoffState(str(data.get("state") or "PROPOSED"))
        except ValueError:
            handoff.state = HandoffState.PROPOSED
        recorded = str(data.get("checksum") or "")
        if recorded and recorded != handoff.compute_checksum():
            raise HandoffError(
                f"handoff {handoff.handoff_id} failed checksum verification (rule R4): "
                f"recorded {recorded[:24]}… but the payload hashes to "
                f"{handoff.compute_checksum()[:24]}…. Refusing to propagate state that may be "
                "corrupt — request retransmission from the origin."
            )
        return handoff


def validate_handoff(handoff: Handoff, *, stage: str,
                     previous: Handoff | None = None) -> ValidationVerdict:
    """Check the mechanical rules for one stage of a handoff.

    Stages and the rules they enforce:

    - `propose`  — R1 (size), R3 (no self-handoff), R5 (ledger entry for irreversible
      decisions), R6 (open-question ceiling), plus the required-field check.
    - `accept`   — R2 (non-negotiable constraints preserved), R4 (checksum), R8 (a superseding
      decision carries its marker). R7 is deliberately absent: acceptance is what satisfies it.
    - `deliver`  — R7 (no delivery while merely proposed).
    - `fulfil`   — R7 again, because a handoff must not be fulfilled before it was accepted.

    Parameters
    ----------
    previous:
        The upstream handoff, when one exists. R2 needs it: the constraint count can only be
        compared against what was there before.

    Returns
    -------
    ValidationVerdict
        `ok` plus every violation, each naming its rule.
    """
    violations: list[RuleViolation] = []

    if stage == "propose":
        violations.extend(_check_required_fields(handoff))
        violations.extend(_check_r1_size(handoff))
        violations.extend(_check_r3_self_handoff(handoff))
        violations.extend(_check_r5_irreversible(handoff))
        violations.extend(_check_r6_open_questions(handoff))
    elif stage == "accept":
        # R7 is not checked here: acceptance is precisely what satisfies it, so validating it
        # before the transition would refuse every legitimate acceptance. It is enforced on
        # delivery (see the `deliver`/`fulfil` stages below).
        violations.extend(_check_r4_checksum(handoff))
        violations.extend(_check_r2_constraints(handoff, previous))
        violations.extend(_check_r8_superseded(handoff))
    elif stage == "deliver":
        violations.extend(_check_r7_no_premature_delivery(handoff))
    elif stage == "fulfil":
        violations.extend(_check_r7_no_premature_delivery(handoff))
    else:
        raise HandoffError(
            f"unknown validation stage {stage!r}; use propose, accept, deliver or fulfil"
        )

    return ValidationVerdict(ok=not violations, stage=stage, violations=tuple(violations))


def _check_required_fields(handoff: Handoff) -> list[RuleViolation]:
    """The registry's required fields must be present.

    A missing field is not a formatting problem: a receiver that cannot see `open_questions`
    cannot know what upstream left unresolved, and will re-derive it incorrectly.
    """
    missing = [name for name in REQUIRED_FIELDS if name not in handoff.payload]
    if not missing:
        return []
    return [RuleViolation(
        rule="REGISTRY",
        message=f"payload is missing required field(s): {', '.join(missing)}",
        detail={"missing": missing, "required": list(REQUIRED_FIELDS)},
    )]


def _check_r1_size(handoff: Handoff) -> list[RuleViolation]:
    """R1: a handoff payload must not exceed the token ceiling.

    An oversized payload means the receiver starts its turn already bloated, which is the state
    the rotation design exists to avoid. The fix is to prune, not to send it anyway.
    """
    tokens = handoff.estimated_tokens()
    if tokens <= MAX_HANDOFF_TOKENS:
        return []
    return [RuleViolation(
        rule="R1",
        message=(
            f"handoff payload is ~{tokens} tokens, over the {MAX_HANDOFF_TOKENS} ceiling. "
            "Prune the context or reference artifacts by path instead of inlining them."
        ),
        detail={"estimated_tokens": tokens, "limit": MAX_HANDOFF_TOKENS},
    )]


def _check_r2_constraints(handoff: Handoff, previous: Handoff | None) -> list[RuleViolation]:
    """R2: non-negotiable constraints must survive a handoff.

    Compared against the previous payload's count, because the rule's whole purpose is to catch
    a constraint that was silently dropped en route. A drop is how "NEVER store passwords in
    plaintext" becomes "use secure auth" two agents later.
    """
    if previous is None:
        return []
    before = {_constraint_key(c) for c in previous.non_negotiable}
    after = {_constraint_key(c) for c in handoff.non_negotiable}
    lost = sorted(before - after)
    if not lost:
        return []
    return [RuleViolation(
        rule="R2",
        message=(
            f"non-negotiable constraints were dropped: {', '.join(lost[:3])}"
            + (" …" if len(lost) > 3 else "")
            + f" ({len(before)} before, {len(after)} after). Restore them before proceeding."
        ),
        detail={"lost": lost, "count_before": len(before), "count_after": len(after)},
    )]


def _check_r3_self_handoff(handoff: Handoff) -> list[RuleViolation]:
    """R3: an agent must not hand off to itself.

    The one deliberate exception is a session rotation, which keeps the same skill by design.
    That is expressed as a distinct `kind` rather than by weakening the rule, so the rule still
    catches an accidental skill self-loop.
    """
    if handoff.kind == "session-rotation":
        return []
    if handoff.origin and handoff.origin == handoff.target:
        return [RuleViolation(
            rule="R3",
            message=(
                f"handoff origin and target are both {handoff.origin!r}. A self-handoff is "
                "either an accidental loop or a session rotation — if it is the latter, mark "
                "the kind as 'session-rotation'."
            ),
            detail={"origin": handoff.origin, "target": handoff.target},
        )]
    return []


def _check_r4_checksum(handoff: Handoff) -> list[RuleViolation]:
    """R4: the received state must hash to what the sender recorded.

    A mismatch aborts rather than propagating: state that has been corrupted between two agents
    produces confidently wrong downstream work, which is worse than a failed handoff.
    """
    expected = handoff.compute_checksum()
    if not handoff.checksum or handoff.checksum == expected:
        return []
    return [RuleViolation(
        rule="R4",
        message=(
            "state checksum mismatch; the payload changed between sender and receiver. "
            "Refusing the handoff — request retransmission from the origin."
        ),
        detail={"recorded": handoff.checksum, "computed": expected},
    )]


def _check_r5_irreversible(handoff: Handoff) -> list[RuleViolation]:
    """R5: an irreversible decision must be recorded in the decision gate ledger.

    An unrecorded irreversible decision is invisible to every later agent, so a choice that
    cannot be undone becomes a constraint nobody knows about. The ledger entry is required in
    the payload itself so the check is local and mechanical.
    """
    decisions = handoff.payload.get("decisions") or []
    irreversible = [
        d for d in decisions
        if isinstance(d, dict) and d.get("reversible") is False
    ]
    if not irreversible:
        return []
    unrecorded = [
        d for d in irreversible
        if not (d.get("gate") and d.get("choice") and d.get("rationale"))
    ]
    if not unrecorded:
        return []
    return [RuleViolation(
        rule="R5",
        message=(
            f"{len(unrecorded)} irreversible decision(s) lack a gate, choice or rationale. "
            "An irreversible choice with no ledger entry is invisible to every later agent."
        ),
        detail={"unrecorded": [d.get("gate") or d.get("choice") or "?" for d in unrecorded]},
    )]


def _check_r6_open_questions(handoff: Handoff) -> list[RuleViolation]:
    """R6: no more than three unresolved questions may cross a boundary.

    Above three, uncertainty compounds faster than it can be resolved downstream, so the
    pipeline pauses and escalates instead of delegating a growing pile of ambiguity.
    """
    questions = handoff.payload.get("open_questions") or []
    if not isinstance(questions, list):
        return []
    if len(questions) <= MAX_OPEN_QUESTIONS:
        return []
    return [RuleViolation(
        rule="R6",
        message=(
            f"{len(questions)} open questions exceed the {MAX_OPEN_QUESTIONS} ceiling. "
            "Resolve them, or escalate to the Owner — do not hand off compounding uncertainty."
        ),
        detail={"count": len(questions), "limit": MAX_OPEN_QUESTIONS,
                "questions": [q.get("question") if isinstance(q, dict) else str(q)
                              for q in questions[:5]]},
    )]


def _check_r7_no_premature_delivery(handoff: Handoff) -> list[RuleViolation]:
    """R7: delivery must not occur while the contract is merely PROPOSED.

    The receiver has to accept before the sender delivers, or the sender is writing into a
    contract the other side never agreed to.
    """
    if handoff.state in (HandoffState.ACCEPTED, HandoffState.IN_PROGRESS, HandoffState.FULFILLED):
        return []
    return [RuleViolation(
        rule="R7",
        message=(
            f"delivery attempted while the handoff is {handoff.state.value}. The downstream "
            "agent must ACCEPT the contract before delivery."
        ),
        detail={"state": handoff.state.value},
    )]


def _check_r8_superseded(handoff: Handoff) -> list[RuleViolation]:
    """R8: overriding an earlier decision requires a SUPERSEDED marker.

    Without the marker a later agent sees two contradictory decisions and cannot tell which
    superseded which — so it may re-adopt the wrong one.
    """
    if not handoff.supersedes:
        return []
    marker = handoff.supersedes.get("marker", "")
    if str(marker).upper() == "SUPERSEDED" and handoff.supersedes.get("rationale"):
        return []
    return [RuleViolation(
        rule="R8",
        message=(
            "this handoff supersedes an earlier decision but carries no SUPERSEDED marker with "
            "a rationale. An unmarked override leaves two contradictory decisions visible."
        ),
        detail={"supersedes": handoff.supersedes},
    )]


def _constraint_key(constraint: dict[str, Any]) -> str:
    """A stable identity for a constraint, for R2's comparison."""
    value = str(constraint.get("value") or "").strip()
    return value or json.dumps(constraint, sort_keys=True, default=str)


def validate_delegation_context(context: dict[str, Any]) -> ValidationVerdict:
    """Check the five-element context pass-through a delegation must carry.

    Exposed separately from :func:`validate_handoff` because it applies to a *delegation*
    rather than a handoff, and the failure it prevents is specific: a delegate with fewer than
    all five elements re-discovers the problem from scratch and can arrive at a different fix
    that reintroduces the original regression.
    """
    missing = [element for element in CONTEXT_ELEMENTS if not context.get(element)]
    if not missing:
        return ValidationVerdict(ok=True, stage="delegation-context")
    return ValidationVerdict(
        ok=False,
        stage="delegation-context",
        violations=(RuleViolation(
            rule="S5",
            message=(
                f"delegation context is missing: {', '.join(missing)}. All five elements are "
                "required — a delegate without them re-discovers the problem from scratch."
            ),
            detail={"missing": missing, "required": list(CONTEXT_ELEMENTS)},
        ),),
    )


def build_context_pass_through(*, problem: str, tried: Iterable[str], logs: str,
                               paths: Iterable[str], hypothesis: str) -> dict[str, Any]:
    """Assemble the five mandatory delegation elements.

    A helper rather than a convention so the required shape is produced correctly at every call
    site — and so an empty element is visible here rather than at the validation that refuses it.
    """
    return {
        "problem": problem,
        "tried": list(tried),
        "logs": logs,
        "paths": list(paths),
        "hypothesis": hypothesis,
    }


def _iso_now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
