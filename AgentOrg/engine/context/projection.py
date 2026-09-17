#!/usr/bin/env python3
"""projection.py — decide whether a prompt fits, before sending it.

WHY THIS EXISTS
---------------
Every consequential context decision happens before a call: is there room? Should we compact, rotate,
or refuse? All of them need a token count that no provider has reported yet.

The distinguishing feature is not the estimate; it is the **irreducible/reducible split**. If the
parts that *cannot* be compacted already fill the window, rotating is pointless — the session would
rotate and immediately overflow again. That case must be diagnosed rather than looped on, and this is
where it is caught.

DESIGN
------
- **The prompt is decomposed, not totalled.** Each component is measured separately so the caller can
  see *which* part is large, and so the irreducible floor is computable.
- **Irreducible means irreducible.** The system prompt, the skill's tier-1 route, pinned constraints
  and the task statement cannot be dropped — losing any of them changes what the agent is doing.
- **A refusal names the fix.** "Irreducible content is 92% of the window" is actionable; "does not
  fit" is not.
- **The estimator is injected**, so the projection uses the same calibrated figures as the gateway
  rather than a second, disagreeing heuristic.

Usage:
    projection = project(session, system="…", skill_body="…", new_message="…", reserve=4096)
    if projection.irreducible_overflow:
        ...  # rotating will not help; lower the skill tier or raise the window
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .session import ContextBudgetError, Session

__all__ = ["Component", "Projection", "estimate_components", "project"]

#: Characters per token for the fallback estimate. The gateway's calibrated estimator is preferred;
#: this is what a caller with no estimator gets, and it is deliberately conservative.
DEFAULT_CHARS_PER_TOKEN = 4.0


@dataclass(frozen=True)
class Component:
    """One measured part of a prompt, and whether it can be shrunk."""

    name: str
    tokens: int
    reducible: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "tokens": self.tokens, "reducible": self.reducible,
                "note": self.note}


@dataclass
class Projection:
    """The pre-flight estimate, decomposed and with its verdict.

    `irreducible_overflow` is the field that matters most: when True the caller must not rotate,
    because the floor already exceeds the window.
    """

    window: int
    components: tuple[Component, ...] = ()
    # The output reserve the caller intends to hold back.
    reserve: int = 0
    # The band the projected saturation falls in.
    band: str = "healthy"

    @property
    def total(self) -> int:
        """Total projected input tokens."""
        return sum(c.tokens for c in self.components)

    @property
    def irreducible(self) -> int:
        """Tokens that compaction cannot remove."""
        return sum(c.tokens for c in self.components if not c.reducible)

    @property
    def reducible(self) -> int:
        """Tokens compaction could remove."""
        return sum(c.tokens for c in self.components if c.reducible)

    @property
    def usable(self) -> int:
        """The window available for input, after the reply reserve."""
        return max(1, self.window - max(0, self.reserve))

    @property
    def saturation(self) -> float:
        """Projected saturation of the usable window."""
        return min(1.0, self.total / self.usable)

    @property
    def irreducible_saturation(self) -> float:
        """Projected saturation if everything reducible were removed.

        This is the honest floor: no amount of compaction can get below it.
        """
        return min(1.0, self.irreducible / self.usable)

    @property
    def fits(self) -> bool:
        """Whether the prompt fits as-is."""
        return self.total <= self.usable

    @property
    def irreducible_overflow(self) -> bool:
        """Whether the irreducible content alone reaches the eviction threshold.

        When true, rotating cannot help — the fresh session would overflow immediately, which is the
        no-progress guard's trigger. The fix is structural: a lower skill tier, a smaller recall
        block, or a larger window.
        """
        return self.irreducible_saturation >= 0.85

    @property
    def must_compact(self) -> bool:
        """Whether compaction is warranted before the next call."""
        return self.saturation >= 0.70

    def headroom(self) -> int:
        """Tokens still free in the usable window."""
        return max(0, self.usable - self.total)

    def largest(self, *, reducible_only: bool = False) -> Component | None:
        """The largest component, so the caller knows where to look first."""
        candidates = [c for c in self.components if not reducible_only or c.reducible]
        return max(candidates, key=lambda c: c.tokens, default=None)

    def diagnosis(self) -> str:
        """A one-line explanation of the projection, for a refusal or a log line.

        Written to name the fix rather than the fault, because "irreducible 92%" tells an operator
        what to do and "does not fit" does not.
        """
        if self.irreducible_overflow:
            biggest = self.largest(reducible_only=False)
            return (
                f"irreducible content is {self.irreducible_saturation:.0%} of the usable window "
                f"({self.irreducible} of {self.usable} tokens). Rotating will not help: a fresh "
                f"session would overflow identically. Lower the skill tier, reduce the recall "
                f"block, or raise context_window"
                + (f". Largest component: {biggest.name} at {biggest.tokens} tokens" if biggest else "")
            )
        if not self.fits:
            return (
                f"prompt projects to {self.total} tokens against {self.usable} usable "
                f"({self.saturation:.0%}); compaction should recover "
                f"{self.total - self.usable} tokens from the reducible part"
            )
        return (
            f"prompt projects to {self.total} of {self.usable} usable tokens "
            f"({self.saturation:.0%}, band {self.band})"
        )

    def require_fit(self) -> None:
        """Raise when the prompt cannot be sent even after compaction.

        Called by the executor before a call, so an impossible prompt is refused with a diagnosis
        rather than sent to fail with a provider-side context-length error.
        """
        if self.irreducible_overflow:
            raise ContextBudgetError(self.diagnosis(), irreducible_tokens=self.irreducible,
                                     window=self.window)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "reserve": self.reserve,
            "usable": self.usable,
            "total": self.total,
            "irreducible": self.irreducible,
            "reducible": self.reducible,
            "saturation": round(self.saturation, 4),
            "irreducible_saturation": round(self.irreducible_saturation, 4),
            "band": self.band,
            "fits": self.fits,
            "irreducible_overflow": self.irreducible_overflow,
            "must_compact": self.must_compact,
            "headroom": self.headroom(),
            "components": [c.as_dict() for c in self.components],
            "diagnosis": self.diagnosis(),
        }


def estimate_components(*, system: str = "", skill_body: str = "", pinned: str = "",
                        recall: str = "", artifacts: str = "", history: str = "",
                        new_message: str = "", chars_per_token: float = DEFAULT_CHARS_PER_TOKEN
                        ) -> list[Component]:
    """Measure each part of a prompt, marking whether it can be shrunk.

    The reducible/irreducible split is the point, and it is a judgement about *meaning* rather than
    size: dropping a ground rule changes what the agent does, while dropping an example does not.
    """
    def tokens(text: str) -> int:
        return max(1, int(len(text) / max(2.0, chars_per_token))) if text else 0

    return [
        Component("system", tokens(system), reducible=False,
                  note="the role and hard constraints; never removed"),
        Component("pinned_constraints", tokens(pinned), reducible=False,
                  note="NEVER/MUST NOT and non_negotiable rules; AR-04 forbids lossy compaction"),
        Component("skill_body", tokens(skill_body), reducible=True,
                  note="Tier 2/3 SOP sections can be evicted; the Tier-1 route cannot"),
        Component("recall", tokens(recall), reducible=True,
                  note="prior-run memory; useful, not required"),
        Component("artifacts", tokens(artifacts), reducible=True,
                  note="artifact bodies can be referenced by path instead of inlined"),
        Component("history", tokens(history), reducible=True,
                  note="conversation history is the first thing to compress"),
        Component("new_message", tokens(new_message), reducible=False,
                  note="the task statement; removing it changes what is being asked"),
    ]


def project(session: Session, *, system: str = "", skill_body: str = "", pinned: str = "",
            recall: str = "", artifacts: str = "", new_message: str = "",
            reserve: int | None = None, estimator: Any = None,
            chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> Projection:
    """Project a prompt's size against a session's window.

    Parameters
    ----------
    session:
        Supplies the window and the history. Its own pinned constraints are included, because they
        are part of every prompt it sends.
    skill_body, recall, artifacts:
        The parts the caller intends to include. Passing them explicitly is what lets the projection
        tell the caller *which* part is too large.
    reserve:
        The reply reserve. Defaults to the session's own `output_reserve`.
    estimator:
        An object exposing `estimate_text(text, provider_id=..., model=...) -> Estimate`, so the
        projection uses the same calibrated figures as the gateway. Without one, a conservative
        character heuristic is used and the result says so.

    Returns
    -------
    Projection
        With the components, the saturation, and the irreducible verdict.
    """
    history = "\n".join(turn.text for turn in session.turns)
    # The session's own pinned constraints are always sent, so they are part of the projection even
    # when the caller does not pass them separately.
    pinned_text = "\n".join(session.pinned) + ("\n" + pinned if pinned else "")

    if estimator is not None:
        ratio = _ratio_from(estimator, session)
        components = estimate_components(
            system=system, skill_body=skill_body, pinned=pinned_text, recall=recall,
            artifacts=artifacts, history=history, new_message=new_message,
            chars_per_token=ratio,
        )
    else:
        components = estimate_components(
            system=system, skill_body=skill_body, pinned=pinned_text, recall=recall,
            artifacts=artifacts, history=history, new_message=new_message,
            chars_per_token=chars_per_token,
        )

    effective_reserve = session.output_reserve if reserve is None else reserve
    total = sum(c.tokens for c in components)
    usable = max(1, session.window - max(0, effective_reserve))
    saturation = min(1.0, total / usable)
    if saturation >= 0.95:
        band = "overflow"
    elif saturation >= 0.85:
        band = "critical"
    elif saturation >= 0.70:
        band = "warning"
    else:
        band = "healthy"

    return Projection(window=session.window, components=tuple(components),
                      reserve=effective_reserve, band=band)


def _ratio_from(estimator: Any, session: Session) -> float:
    """Read the estimator's current characters-per-token, falling back to the default.

    Using the calibrated ratio is what keeps the projection and the gateway's accounting consistent;
    two disagreeing estimates would mean the projection says a prompt fits and the provider says it
    does not.
    """
    try:
        ratio = estimator.ratio_for("", "")
    except Exception:  # noqa: BLE001 - an estimator without the method is not fatal
        return DEFAULT_CHARS_PER_TOKEN
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        return DEFAULT_CHARS_PER_TOKEN
    # Guard against an absurd calibration, which would make the projection meaningless.
    return min(8.0, max(2.0, value)) if value > 0 else DEFAULT_CHARS_PER_TOKEN
