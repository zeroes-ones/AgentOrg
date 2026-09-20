#!/usr/bin/env python3
"""compaction.py — the 70/85/95 ladder, with AR-04 enforced by a count and the prefix kept cacheable.

WHY THIS EXISTS
---------------
Compaction is not summarisation. It is *attention curation*: deciding which tokens still earn their
place. The library is explicit that the window size is not the real limit — a model attends
effectively to roughly 70% of it — so compacting reactively at 95% means the agent already spent
fifteen turns with diluted attention, and the summariser runs on a nearly-full context where it
produces worse output.

Three rules make this safe rather than merely aggressive:

- **AR-04 — verbatim preservation.** Security and `NEVER`/`MUST NOT` content is never lossily
  compacted. The check is a *count*: if the number of pinned constraints drops, the compaction is
  reverted. This is what stops "NEVER store passwords in plaintext" becoming "use secure auth" two
  agents later, at which point the model picks MD5.
- **AR-05 — turn boundaries only.** Compaction during generation prunes references the model is
  mid-way through using, which produces corrupted output rather than an error.
- **The remaining log stays cacheable.** A provider reuses a request only up to its first changed
  byte, so the removal is one *contiguous run* of evictable turns — see `cachealign.py`. What is left
  then begins with the bytes that were already sent, and the session does not re-pay for them.

DESIGN
------
- **The ladder is a table, not a heuristic.** Each band has a defined set of actions, so the behaviour
  at 86% is predictable rather than decided at the moment.
- **Eviction is priority-based, never uniform.** Pruning everything by the same percentage is how two
  critical ground rules get dropped while five verbose examples survive. The staleness score still
  chooses *which* run goes; cache alignment constrains only the shape of the removal.
- **A prune comes before a summary, and replaces rather than removes.** An oversized single result is
  cut to a head, a marker and a tail before anything is evicted whole. Evicting it instead would lose
  it entirely — including the parts a model can still read — and would spend the session's eviction
  budget on the one turn that is cheapest to make small. The marker is written into the text, so the
  loss is *stated* to the model rather than hidden from it.
- **A compaction reports what it did**, so the eviction manifest can be logged and a revert is
  possible — including whether a revert undid a prune, because a revert that left a truncation behind
  would be a silent loss with no record of it. It reports the prefix it preserved and, when it broke
  one the store called warm, a record costing that rather than leaving it silent.

Usage:
    result = compact(session, config, cache_store=store, cache_prefix_hash=pin.prefix_hash)
    if result.reverted:
        ...  # a pinned constraint would have been lost; the session is unchanged
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

from .cachealign import (
    EvictionPlan,
    common_prefix_chars,
    consult_store,
    log_text,
    plan_eviction,
    prefix_hash_of,
)
from .session import Band, Session

__all__ = ["CompactionAction", "CompactionResult", "compact", "classify_band"]

#: Recency decay per turn, from the library's context-rotation research: a rule read at turn 1 is
#: about 60% as likely to be followed by turn 15. Used to score how stale a turn is.
_DECAY_LAMBDA = 0.1

#: A turn unreferenced for this many turns is a staleness candidate.
_STALE_AFTER_TURNS = 5

#: Similarity above which two turns are considered duplicates. The library's own redundancy threshold.
_REDUNDANCY_THRESHOLD = 0.92

#: A single turn larger than this is pruned to a head plus a marker plus a tail *before* anything is
#: evicted whole. The bound is Reasonix's ~8192 code points, and the reasoning behind it is the same
#: as for a truncated tool read: a 60KB file dump is one turn where the model needs its beginning and
#: its end, and the middle is where the tokens are.
_MAX_VERBATIM_TURN_CHARS = 8192

#: How many of the newest turns are never pruned. The newest turns are the live working set — the
#: model is mid-way through using them — and cutting one in half during generation is the same failure
#: AR-05 forbids for compaction.
_KEEP_NEWEST_VERBATIM = 3

#: How the pruned budget is spent: three fifths at the head, one quarter at the tail, the remainder
#: being the marker that says what happened. The head says what the result was about and the tail is
#: where a trailer, a summary line or the end of a listing lands — the two places a model actually
#: reads. The split is deliberately *under* the bound so the marker is paid for out of the budget
#: rather than added to it.
_PRUNE_HEAD_SHARE = 0.6
_PRUNE_TAIL_SHARE = 0.25


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
    #: Oversize turns reduced to a head, a marker and a tail before the eviction pass ran, and the
    #: tokens that recovered. Reported separately from `evicted_turns` because the two are different
    #: promises: an evicted turn is gone, a pruned one is still there and says what it lost.
    pruned_turns: int = 0
    pruned_tokens: int = 0
    pinned_before: int = 0
    pinned_after: int = 0
    reverted: bool = False
    revert_reason: str = ""
    candidates: tuple[str, ...] = ()
    #: Whether the eviction removed one contiguous run (cache-aligned) or scattered turns. False is
    #: not a failure — it is the case where no single span could free enough without crossing a
    #: protected turn — but it is recorded, because it is the case that costs a warm prefix.
    aligned: bool = True
    #: Turns left byte-identical at the head of the log — the part a provider's prefix cache can still
    #: reuse, as a turn count.
    prefix_turns_kept: int = 0
    #: Characters of the log left byte-identical, and the digests that make the figure checkable.
    #: `prefix_hash_before` is the log *as it was sent* — what the provider holds cached — and
    #: `prefix_hash_after` is the region this compaction leaves byte-identical. They are equal exactly
    #: when nothing was lost; hashing the surviving region on both sides would make that comparison
    #: vacuous by construction.
    prefix_chars_kept: int = 0
    log_chars_before: int = 0
    prefix_hash_before: str = ""
    prefix_hash_after: str = ""
    #: The store's verdict on whether this session's prefix was warm, and — when the eviction broke
    #: one — a sentence costing it. Three-valued: `None` means the store had nothing to say.
    cache_warm: bool | None = None
    cache_note: str = ""
    #: The attributable record of a compaction that invalidated a prefix the store said was warm.
    #: Held rather than merely logged so the caller can put it on the event stream it already watches.
    invalidated_prefix: dict[str, Any] | None = None
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
            "pruned_turns": self.pruned_turns,
            "pruned_tokens": self.pruned_tokens,
            "pinned_before": self.pinned_before,
            "pinned_after": self.pinned_after,
            "reverted": self.reverted,
            "revert_reason": self.revert_reason,
            "candidates": list(self.candidates),
            "effective": self.effective,
            "aligned": self.aligned,
            "prefix_turns_kept": self.prefix_turns_kept,
            "prefix_chars_kept": self.prefix_chars_kept,
            "log_chars_before": self.log_chars_before,
            "prefix_hash_before": self.prefix_hash_before,
            "prefix_hash_after": self.prefix_hash_after,
            "cache_warm": self.cache_warm,
            "cache_note": self.cache_note,
            "invalidated_prefix": self.invalidated_prefix,
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
            verbatim_markers: Iterable[str] = ("NEVER", "MUST NOT", "SECURITY", "AUTH", "COMPLIANCE"),
            cache_store: Any = None, cache_prefix_hash: str = "", cache_skill: str = ""
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
    cache_store, cache_prefix_hash, cache_skill:
        The durable cache record, and the prefix this session is sending. Supplying a store makes the
        eviction **cache-aligned**: the removal is one contiguous run, and when the store says the
        prefix is warm the run is chosen to leave the longest intact head. A compaction that breaks a
        warm prefix is then recorded attributably rather than silently. Optional, and a store that
        cannot be read is treated as having nothing to say.

    Returns
    -------
    CompactionResult
        Including `reverted=True` when a pinned constraint would have been lost.
    """
    band = classify_band(session.saturation)
    tokens_before = session.used_tokens
    pinned_before = len(session.pinned)
    # Snapshot the log's bytes before anything runs. Read here rather than after the band checks
    # because the digest and the surviving-region length are only meaningful against the log as it
    # was sent.
    log_before = log_text(session.turns)

    if band is Band.HEALTHY:
        return CompactionResult(
            action=CompactionAction.NONE, band_before=band, tokens_before=tokens_before,
            tokens_after=tokens_before, pinned_before=pinned_before, pinned_after=pinned_before,
            note=f"saturation {session.saturation:.0%} is healthy; nothing to do",
            candidates=tuple(_candidate_names(session, verbatim_markers)),
            # Nothing was removed, so the whole log is intact. Stated rather than left at zero, which
            # would read as "the prefix was destroyed" on a path that changed nothing.
            **_intact(log_before),
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
            **_intact(log_before),
        )

    # CRITICAL and OVERFLOW evict. The difference is how much.
    emergency = band is Band.OVERFLOW
    if target is None:
        # Aim below the warning threshold so the next few turns do not immediately re-trigger.
        target = max(0.30, compact_at - 0.10)

    # Ask the store before deciding anything. Its answer is three-valued, and all three matter: a warm
    # prefix is worth protecting, a cold one is not, and `None` means the store has no opinion — which
    # is not the same as cold, and must not be used as though it were.
    verdict = consult_store(cache_store, prefix_hash=cache_prefix_hash, skill=cache_skill)

    # ── prune before summarising ──
    # Order matters, and it is the opposite of the obvious one. Evicting whole turns first would throw
    # away a 60KB tool result *entirely* rather than keeping the head and the tail a model could still
    # read — and, worse, it would spend the session's eviction budget on the one turn that is easiest
    # to make small. Pruning the oversize turns first is what makes "nothing was lost" true: what a
    # turn loses is its middle, which is where the tokens are and the least of where the meaning is.
    #
    # The prune runs *before* the eviction and is therefore part of the same transaction: the pins are
    # snapshotted ahead of it, the AR-04 count is compared after it, and a prune that would touch a
    # protected turn does not touch it at all. So a prune can never be the thing that makes the
    # compaction revert, and AR-04 is exactly as strict as it was.
    pins_before = list(session.pinned)
    before_texts = _turn_signatures(session)
    pruned = _prune_oversize_turns(session, markers=tuple(verbatim_markers))
    # Measured here, while the pruned turn objects are still in the list the pruner walked. Doing it
    # after the eviction would silently drop the saving of any pruned turn the eviction then removed,
    # and the reported figure would understate what the prune recovered.
    pruned_tokens = sum(
        max(0, len(before_texts.get(id(turn), "")) // 4 - turn.tokens) for _, turn in pruned)

    plan = _plan(session, target=target, markers=tuple(verbatim_markers), warm=verdict.warm)
    removed = _evict(session, target=target, emergency=emergency,
                     markers=tuple(verbatim_markers), warm=verdict.warm, plan=plan)
    tokens_after = session.used_tokens
    pinned_after = len(session.pinned)

    # AR-04: a pinned constraint must never be lost. The guard is a count rather than a hope, because
    # a compaction that silently dropped a safety rule is worse than no compaction at all.
    #
    # Checked *after* the eviction and before anything is reported, so a revert restores the log
    # exactly — including the bytes the cache alignment above was reasoning about, which is why the
    # reported prefix figures below are taken from the restored log rather than the attempted one.
    if pinned_after < pinned_before:
        _restore(session, removed)
        _restore_pruned(session, before_texts)
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
            **_intact(log_before),
            cache_warm=verdict.warm, cache_note=verdict.note,
            note="reverted",
        )

    log_after = log_text(session.turns)
    chars_kept = common_prefix_chars(log_before, log_after)
    invalidated = _invalidation(verdict, plan=plan, removed=len(removed),
                                chars_kept=chars_kept, pruned=pruned, skill=cache_skill)

    note = (
        f"{'emergency: ' if emergency else ''}evicted {len(removed)} turn(s) to reach "
        f"{session.saturation:.0%} from {tokens_before / max(1, session.usable_window):.0%}; "
        f"{pinned_after} pinned constraint(s) preserved verbatim"
        + (f"; {len(pruned)} oversize turn(s) pruned head+tail first" if pruned else "")
    )
    if removed:
        # Stated in words because `aligned=False` is a flag, and the thing that costs money is the
        # sentence: the provider re-reads everything after the cut.
        note += ("; one contiguous run removed" if plan.aligned
                 else "; the removal was scattered, so the provider re-reads from the first cut")
    if invalidated:
        note += f"; a warm prefix was invalidated ({invalidated['reason']})"

    return CompactionResult(
        action=CompactionAction.EMERGENCY if emergency else CompactionAction.EVICT_TIER3,
        band_before=band, tokens_before=tokens_before, tokens_after=tokens_after,
        evicted_turns=len(removed),
        evicted_tiers=tuple(sorted({t.tier for _pos, t in removed})),
        pinned_before=pinned_before, pinned_after=pinned_after,
        pruned_turns=len(pruned), pruned_tokens=pruned_tokens,
        aligned=plan.aligned,
        prefix_turns_kept=plan.prefix_turns_kept,
        prefix_chars_kept=chars_kept,
        log_chars_before=len(log_before),
        # The digest of the whole log as sent, against the digest of what survived. Equal means the
        # provider's cached bytes are entirely intact; different means the cut is where the re-read
        # begins, and `prefix_chars_kept` says where that is.
        prefix_hash_before=prefix_hash_of(log_before),
        prefix_hash_after=prefix_hash_of(log_after[:chars_kept]),
        cache_warm=verdict.warm, cache_note=verdict.note,
        invalidated_prefix=invalidated,
        note=note,
    )


def _intact(log: str) -> dict[str, Any]:
    """The cache fields for a compaction that changed nothing.

    A band that does not evict leaves the whole log byte-identical, so the prefix is entirely intact
    and the two digests agree. Reported rather than defaulted, because the default of zero would read
    as "the prefix was destroyed" on a path that did not touch it.
    """
    return {
        "prefix_chars_kept": len(log),
        "log_chars_before": len(log),
        "prefix_hash_before": prefix_hash_of(log),
        "prefix_hash_after": prefix_hash_of(log),
    }


def _invalidation(verdict: Any, *, plan: Any, removed: int, chars_kept: int,
                  pruned: Sequence[Any], skill: str = "") -> dict[str, Any] | None:
    """The attributable record of a compaction that broke a warm prefix, or None.

    Recorded so the cost has a line rather than an inference: a bill that went up can be read against
    this, which is the whole difference between a cache the engine measures and a cache it uses. A
    compaction that evicted nothing is not an invalidation, and neither is one that leaves the whole
    log byte-identical — the record has to be about the provider's bytes, not about the intent.
    """
    if verdict.warm is not True:
        return None
    if not removed and not pruned:
        return None
    return {
        "skill": skill,
        "prefix_hash": verdict.prefix_hash,
        "reason": (
            f"the compaction cut the log at character {chars_kept} of a prefix the store reported "
            f"warm, so the provider re-reads everything after it"
            if removed
            else "the compaction rewrote an oversize turn in place, changing the bytes at that point"
        ),
        "prefix_chars_kept": chars_kept,
        "evicted_turns": removed,
        "aligned": plan.aligned,
        "pruned_turns": len(pruned),
    }


# ── pruning an oversize turn, before anything is summarised ──────────────────


def _turn_signatures(session: Session) -> dict[int, str]:
    """The text of every turn, keyed by object identity.

    Keyed by `id()` rather than by position because eviction rebuilds the turn list and a positional
    key would point at whatever slid into the gap. Identity survives the rebuild, which is what makes
    an exact restore possible.
    """
    return {id(turn): turn.text for turn in session.turns}


def _restore_pruned(session: Session, before_texts: dict[int, str]) -> None:
    """Put every pruned turn's original text back, for the AR-04 revert.

    The revert's contract is that a failed compaction leaves the session *exactly* as it was. A prune
    that survived a revert would leave a truncation behind with no record of it, which is precisely
    the silent loss the guard exists to prevent — so the prune is undone along with the eviction.
    """
    for turn in session.turns:
        original = before_texts.get(id(turn))
        if original is not None and original != turn.text:
            turn.text = original
            turn.tokens = max(1, len(original) // 4)


def _prunable(turn: Any, markers: tuple[str, ...]) -> bool:
    """Whether an oversize turn may be cut down to its head and tail.

    Refused, deliberately, for anything AR-04 protects:

    - a turn the marker scan calls security-critical (which includes an explicitly pinned one), and
    - a turn whose text carries a marker but was not detected as one — belt and braces, because the
      cost of being wrong here is a `NEVER` rule becoming "see the omitted middle".

    The rule is that protection beats size. A 40KB ground rule stays 40KB, and the eviction pass is
    where the session finds its headroom instead.
    """
    if _must_keep(turn, markers):
        return False
    upper = (turn.text or "").upper()
    if any(marker.upper() in upper for marker in markers):
        return False
    return len(turn.text or "") > _MAX_VERBATIM_TURN_CHARS


def _prune_oversize_turns(session: Session, *, markers: tuple[str, ...]) -> list[tuple[int, Any]]:
    """Cut every oversize turn down to a head, a marker and a tail. Returns what was pruned.

    A turn is *replaced*, never removed. What is cut is only ever a middle, and the marker says so in
    words the model can read, so it can ask for the rest rather than assume it saw everything. The
    turns not pruned are left exactly as they were.

    Honest about what it does *not* protect: this mutates turn text, so a session that has been
    compacted this way is no longer a byte-identical log of what was originally said. That is the
    point — the log was going to be rewritten anyway — but it is why the prune is confined to the
    CRITICAL and OVERFLOW bands and why a revert restores the text exactly (`_restore_pruned`).
    """
    pruned: list[tuple[int, Any]] = []
    total = len(session.turns)
    for position, turn in enumerate(session.turns):
        # The newest turns stay verbatim: they are what the model is working with right now.
        if total - position <= _KEEP_NEWEST_VERBATIM:
            continue
        if not _prunable(turn, markers):
            continue
        text = turn.text or ""
        head_chars = int(_MAX_VERBATIM_TURN_CHARS * _PRUNE_HEAD_SHARE)
        tail_chars = int(_MAX_VERBATIM_TURN_CHARS * _PRUNE_TAIL_SHARE)
        omitted = len(text) - head_chars - tail_chars
        marker = (f"\n\n… [{omitted} characters omitted from the middle of this result; "
                  "it was pruned to keep the session inside its window] …\n\n")
        turn.text = text[:head_chars] + marker + text[len(text) - tail_chars:]
        # Recomputed from the real text, so the session's saturation figure — and therefore the
        # eviction loop's stopping condition — is measured against what is actually held.
        turn.tokens = max(1, len(turn.text) // 4)
        pruned.append((position, turn))
    return pruned

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


def _plan(session: Session, *, target: float, markers: tuple[str, ...],
          warm: bool | None = None) -> EvictionPlan:
    """Decide which turns to remove, and in which shape.

    Split from `_evict` so the *decision* is inspectable without the mutation: the plan carries the
    span, the tokens freed and the surviving head, and a caller can be told what a compaction would
    cost before it does it. Scoring lives here because the run chosen is chosen by the same staleness
    score the previous rule sorted by — a cheap adjacent pair still beats an expensive single turn.
    """
    if not session.turns:
        return EvictionPlan(positions=(), freed=0, note="the session has no turns to evict")

    target_tokens = int(target * session.usable_window)
    total = len(session.turns)
    tokens = [turn.tokens for turn in session.turns]
    # Protected turns are never candidates, which is also what stops a contiguous run from straddling
    # one: the runs are built from adjacency in this list, so a protected turn ends a run there.
    scored: list[tuple[int, float]] = []
    for position, turn in enumerate(session.turns):
        if _must_keep(turn, markers):
            continue
        turns_since = total - position
        score = _staleness_score(turn, turns_since=turns_since, position=position, total=total)
        scored.append((position, score))

    # A *deficit*, not a loop re-checking the running total: the removals are applied only at the end,
    # so `session.used_tokens` would never decrease inside such a loop and it would evict everything
    # rather than stopping at the target.
    return plan_eviction(scored, tokens, needed=session.used_tokens - target_tokens, warm=warm)


def _evict(session: Session, *, target: float, emergency: bool, markers: tuple[str, ...],
           warm: bool | None = None, plan: EvictionPlan | None = None) -> list[Any]:
    """Apply an eviction plan, returning the removed turns so a revert can restore them exactly.

    `target`, `warm` and `markers` are accepted so this remains a complete description of the eviction
    on its own — the AR-04 revert path and its tests call it directly — but a caller that has already
    planned the removal passes `plan` so the decision is made once.

    `emergency` is accepted and unused: the band decides *how much* through the target, not how. Kept
    in the signature because every caller passes the band's facts together, and a helper that quietly
    dropped one would make a direct call describe a different function from the one that runs.
    """
    del emergency
    if not session.turns:
        return []

    resolved = plan if plan is not None else _plan(session, target=target, markers=markers, warm=warm)
    if not resolved.positions:
        return []

    removed_positions = set(resolved.positions)
    # Collected before the rebuild, because the rebuild is what makes them unfindable — and kept
    # paired with the position each came from, because that position is the only exact way back.
    # `Turn.at` cannot serve: turns appended in a tight loop collide on it (eight turns produced
    # `…94642` twice), so ordering by timestamp silently swapped adjacent turns on restore.
    removed = [(index, turn) for index, turn in enumerate(session.turns)
               if index in removed_positions]
    # Rebuild rather than mutate in place: it keeps the removal atomic from the caller's perspective.
    session.turns = [turn for index, turn in enumerate(session.turns)
                     if index not in removed_positions]
    return removed


def _restore(session: Session, removed: list[Any]) -> None:
    """Put removed turns back, at the positions they came from.

    Order matters: a restored history that is out of sequence would read as a different conversation.

    **Sorting by timestamp is not enough**, and that was a real bug. `Turn.at` has microsecond
    resolution and turns appended in a tight loop genuinely collide on it — eight turns written back
    to back produced `…94642` twice and `…946422` twice. `sorted` is stable, but its input here was
    `[*surviving, *removed]`, so two turns sharing a timestamp kept *that* order rather than their
    original one, and the log came back as `turn0, turn1, turn2, turn4, turn3, …`. It reproduced in
    15 of 40 runs.

    The consequence was not cosmetic: AR-04's guard verifies a *count*, so the revert reported success
    while returning a reordered transcript — a pinned constraint could change position with nothing
    recording it, which is exactly the silent loss AR-04 exists to prevent.

    `removed` is therefore a list of `(position, turn)` from `_evict`, reinserted in ascending order.
    Reinserting by position is exact; reinserting by a field that cannot tell two turns apart is not.
    A bare turn (no position) is appended, so a caller that has only the turn still restores it.
    """
    if not removed:
        return
    restored = list(session.turns)
    pairs = [entry for entry in removed if isinstance(entry, tuple) and len(entry) == 2]
    if len(pairs) != len(removed):
        # A caller that did not supply positions: keep the turns, in the order given, at the end.
        # Still no silent loss — the count is what AR-04 checks, and this path only adds turns back.
        restored.extend(entry for entry in removed if entry not in pairs)
        session.turns = restored
        return
    for position, turn in sorted(pairs, key=lambda pair: int(pair[0])):
        index = max(0, min(int(position), len(restored)))
        restored.insert(index, turn)
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
