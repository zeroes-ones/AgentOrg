#!/usr/bin/env python3
"""compaction.py — the 70/85/95 ladder, with AR-04 enforced by a count.

WHY THIS EXISTS
---------------
Compaction is not summarisation. It is *attention curation*: deciding which tokens still earn their
place. The library is explicit that the window size is not the real limit — a model attends
effectively to roughly 70% of it — so compacting reactively at 95% means the agent already spent
fifteen turns with diluted attention, and the summariser runs on a nearly-full context where it
produces worse output.

Two rules make this safe rather than merely aggressive:

- **AR-04 — verbatim preservation.** Security and `NEVER`/`MUST NOT` content is never lossily
  compacted. The check is a *count*: if the number of pinned constraints drops, the compaction is
  reverted. This is what stops "NEVER store passwords in plaintext" becoming "use secure auth" two
  agents later, at which point the model picks MD5.
- **AR-05 — turn boundaries only.** Compaction during generation prunes references the model is
  mid-way through using, which produces corrupted output rather than an error.

DESIGN
------
- **The ladder is a table, not a heuristic.** Each band has a defined set of actions, so the behaviour
  at 86% is predictable rather than decided at the moment.
- **Eviction is priority-based, never uniform.** Pruning everything by the same percentage is how two
  critical ground rules get dropped while five verbose examples survive.
- **A compaction reports what it did**, so the eviction manifest can be logged and a revert is
  possible.

Usage:
    result = compact(session, config)
    if result.reverted:
        ...  # a pinned constraint would have been lost; the session is unchanged
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .session import Band, Session

__all__ = ["CompactionAction", "CompactionResult", "compact", "classify_band"]

#: Recency decay per turn, from the library's context-rotation research: a rule read at turn 1 is
#: about 60% as likely to be followed by turn 15. Used to score how stale a turn is.
_DECAY_LAMBDA = 0.1

#: A turn unreferenced for this many turns is a staleness candidate.
_STALE_AFTER_TURNS = 5

#: Similarity above which two turns are considered duplicates. The library's own redundancy threshold.
_REDUNDANCY_THRESHOLD = 0.92


class CompactionAction(str, Enum):
    """One step of the ladder."""

    NONE = "none"
    PREPARE = "prepare"          # WARNING: identify candidates, do not evict yet
    EVICT_TIER3 = "evict_tier3"  # CRITICAL: drop the lazily-loaded material
    COMPRESS_HISTORY = "compress_history"
    DOWNGRADE_SKILLS = "downgrade_skills"
    EMERGENCY = "emergency"      # OVERFLOW: tier 1 only


@dataclass
class CompactionResult:
    """What a compaction did, and whether it was allowed to stand.

    `reverted` is the AR-04 outcome: when a pinned constraint would have been lost, the whole
    compaction is undone and the session is left exactly as it was. A compaction that silently dropped
    a safety rule is worse than no compaction.
    """

    action: CompactionAction
    band_before: Band
    tokens_before: int
    tokens_after: int
    evicted_turns: int = 0
    evicted_tiers: tuple[int, ...] = ()
    pinned_before: int = 0
    pinned_after: int = 0
    reverted: bool = False
    revert_reason: str = ""
    candidates: tuple[str, ...] = ()
    note: str = ""

    @property
    def recovered(self) -> int:
        """Tokens freed, or zero when the compaction was reverted."""
        return 0 if self.reverted else max(0, self.tokens_before - self.tokens_after)

    @property
    def effective(self) -> bool:
        """Whether the compaction changed anything and stood."""
        return not self.reverted and self.recovered > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "band_before": self.band_before.value,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "recovered": self.recovered,
            "evicted_turns": self.evicted_turns,
            "evicted_tiers": list(self.evicted_tiers),
            "pinned_before": self.pinned_before,
            "pinned_after": self.pinned_after,
            "reverted": self.reverted,
            "revert_reason": self.revert_reason,
            "candidates": list(self.candidates),
            "effective": self.effective,
            "note": self.note,
        }


def classify_band(saturation: float) -> Band:
    """The band a saturation falls in, using the library's thresholds."""
    if saturation >= 0.95:
        return Band.OVERFLOW
    if saturation >= 0.85:
        return Band.CRITICAL
    if saturation >= 0.70:
        return Band.WARNING
    return Band.HEALTHY


def compact(session: Session, *, compact_at: float = 0.70, evict_at: float = 0.85,
            overflow_at: float = 0.95, target: float | None = None,
            verbatim_markers: Iterable[str] = ("NEVER", "MUST NOT", "SECURITY", "AUTH", "COMPLIANCE")
            ) -> CompactionResult:
    """Compact a session according to its band.

    Parameters
    ----------
    session:
        The session to compact. Its pinned constraints are the protected set.
    compact_at, evict_at, overflow_at:
        The thresholds, from config. Defaults are the library's 70/85/95.
    target:
        Compact down to this saturation rather than merely below a threshold. Defaults to
        `compact_at` minus a margin, so one compaction buys several turns of headroom rather than
        triggering again immediately.
    verbatim_markers:
        Substrings that mark text as security-critical even when it was not explicitly pinned.

    Returns
    -------
    CompactionResult
        Including `reverted=True` when a pinned constraint would have been lost.
    """
    band = classify_band(session.saturation)
    tokens_before = session.used_tokens
    pinned_before = len(session.pinned)

    if band is Band.HEALTHY:
        return CompactionResult(
            action=CompactionAction.NONE, band_before=band, tokens_before=tokens_before,
            tokens_after=tokens_before, pinned_before=pinned_before, pinned_after=pinned_before,
            note=f"saturation {session.saturation:.0%} is healthy; nothing to do",
            candidates=tuple(_candidate_names(session, verbatim_markers)),
        )

    if band is Band.WARNING:
        # WARNING prepares but does not evict: compacting this early would discard material the
        # session may still use, and the library's advice is to be ready rather than eager.
        candidates = _candidate_names(session, verbatim_markers)
        return CompactionResult(
            action=CompactionAction.PREPARE, band_before=band, tokens_before=tokens_before,
            tokens_after=tokens_before, pinned_before=pinned_before, pinned_after=pinned_before,
            candidates=tuple(candidates),
            note=(f"saturation {session.saturation:.0%} is in the warning band; "
                  f"{len(candidates)} eviction candidate(s) prepared, none evicted yet"),
        )

    # CRITICAL and OVERFLOW evict. The difference is how much.
    emergency = band is Band.OVERFLOW
    if target is None:
        # Aim below the warning threshold so the next few turns do not immediately re-trigger.
        target = max(0.30, compact_at - 0.10)

    # Snapshot the pins before eviction. A revert must restore *everything*, not only the turns — an
    # incomplete revert would leave the session holding a rule it no longer records, which is exactly
    # the state AR-04 exists to prevent.
    pins_before = list(session.pinned)
    removed = _evict(session, target=target, emergency=emergency,
                     markers=tuple(verbatim_markers))

    tokens_after = session.used_tokens
    pinned_after = len(session.pinned)

    # AR-04: a pinned constraint must never be lost. The guard is a count rather than a hope, because
    # a compaction that silently dropped a safety rule is worse than no compaction at all.
    if pinned_after < pinned_before:
        _restore(session, removed)
        session.pinned = pins_before
        return CompactionResult(
            action=CompactionAction.EMERGENCY if emergency else CompactionAction.EVICT_TIER3,
            band_before=band, tokens_before=tokens_before, tokens_after=tokens_before,
            pinned_before=pinned_before, pinned_after=pinned_before, reverted=True,
            revert_reason=(
                f"the compaction would have removed {pinned_before - pinned_after} pinned "
                f"constraint(s). AR-04 forbids lossy compaction of security and NEVER/MUST NOT "
                f"content, so the compaction was reverted."
            ),
            note="reverted",
        )

    return CompactionResult(
        action=CompactionAction.EMERGENCY if emergency else CompactionAction.EVICT_TIER3,
        band_before=band, tokens_before=tokens_before, tokens_after=tokens_after,
        evicted_turns=len(removed), evicted_tiers=tuple(sorted({t.tier for t in removed})),
        pinned_before=pinned_before, pinned_after=pinned_after,
        note=(
            f"{'emergency: ' if emergency else ''}evicted {len(removed)} turn(s) to reach "
            f"{session.saturation:.0%} from {tokens_before / max(1, session.usable_window):.0%}; "
            f"{pinned_after} pinned constraint(s) preserved verbatim"
        ),
    )


# ── the eviction algorithm ───────────────────────────────────────────────────


def _must_keep(turn: Any, markers: tuple[str, ...]) -> bool:
    """Whether a turn is protected from lossy compaction (AR-04).

    A turn is protected when it was explicitly pinned, or when its text carries a marker the library
    treats as security-critical. The marker check exists because a constraint can reach the history
    through a handoff without having been pinned on this session explicitly.
    """
    if turn.pinned:
        return True
    upper = (turn.text or "").upper()
    return any(marker.upper() in upper for marker in markers)


def _staleness_score(turn: Any, *, turns_since: int, position: int, total: int) -> float:
    """A priority score: low means evict first.

    Combines three signals the library names: recency decay (λ=0.1 per turn), staleness (unreferenced
    for many turns), and disclosure tier (tier 3 is lazily loaded material, so it goes first).
    """
    decay = math.exp(-_DECAY_LAMBDA * max(0, turns_since))
    staleness = 0.5 if turns_since > _STALE_AFTER_TURNS else 1.0
    # Tier 3 is the cheapest to lose: it is examples and references. Tier 1 is the route and ground
    # rules, which is why it scores highest.
    tier_weight = {1: 3.0, 2: 1.5, 3: 0.6}.get(int(getattr(turn, "tier", 2)), 1.0)
    # A turn that served a specific criterion is worth more than a generic exchange.
    serves_bonus = 1.4 if getattr(turn, "serves", "") else 1.0
    return decay * staleness * tier_weight * serves_bonus


def _evict(session: Session, *, target: float, emergency: bool,
           markers: tuple[str, ...]) -> list[Any]:
    """Evict turns until the target saturation is reached, cheapest first.

    Returns the removed turns, so a revert can restore them exactly. Priority-based rather than
    uniform: pruning everything by the same percentage is how a critical ground rule is lost while a
    verbose example survives.
    """
    if not session.turns:
        return []

    target_tokens = int(target * session.usable_window)
    total = len(session.turns)
    # Score every evictable turn. Protected turns are never candidates.
    scored: list[tuple[float, int, Any]] = []
    for position, turn in enumerate(session.turns):
        if _must_keep(turn, markers):
            continue
        turns_since = total - position
        score = _staleness_score(turn, turns_since=turns_since, position=position, total=total)
        scored.append((score, position, turn))

    # Cheapest to lose first: lowest score.
    scored.sort(key=lambda entry: entry[0])

    # Track the running total *as we go*. Checking `session.used_tokens` inside the loop would never
    # decrease, because the removals are applied only after it — so the loop would evict everything
    # rather than stopping at the target.
    running_tokens = session.used_tokens
    removed: list[tuple[int, Any]] = []
    for score, position, turn in scored:
        if running_tokens <= target_tokens:
            break
        removed.append((position, turn))
        running_tokens -= turn.tokens

    if not removed:
        return []

    # Rebuild the turn list without the removed positions. Rebuilding rather than mutating in place
    # keeps the removal atomic from the caller's perspective.
    removed_positions = {position for position, _ in removed}
    session.turns = [turn for index, turn in enumerate(session.turns)
                     if index not in removed_positions]
    return [turn for _, turn in removed]


def _restore(session: Session, removed: list[Any]) -> None:
    """Put removed turns back, preserving their original order.

    Order matters: a restored history that is out of sequence would read as a different conversation.
    """
    if not removed:
        return
    restored = sorted([*session.turns, *removed], key=lambda turn: getattr(turn, "at", 0.0))
    session.turns = restored


def _candidate_names(session: Session, markers: tuple[str, ...]) -> list[str]:
    """Names of the turns that *would* be evicted — the warning-band preview.

    Reported so the caller can log or display what is at risk before anything is dropped.
    """
    total = len(session.turns)
    out: list[str] = []
    for position, turn in enumerate(session.turns):
        if _must_keep(turn, markers):
            continue
        out.append(f"{turn.role}[tier{turn.tier}]{'/' + turn.serves if turn.serves else ''}")
    return out[:20]
