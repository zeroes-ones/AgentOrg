#!/usr/bin/env python3
"""cachealign.py — spend the context budget in the order the provider can still read it.

WHY THIS EXISTS
---------------
Compaction chose what to forget by attention score alone. That is right about *meaning* and blind
about *bytes*: removing a turn from the middle of the log rewrites every turn after it, and a provider
reuses a request only up to its first changed byte — so that one removal misses from the middle to the
end, and the engine re-pays for bytes it has already sent. `CacheStore` recorded those misses. Nothing
consulted them when deciding what to forget, so the cache was measured and never used.

The provider's rule is simple and unforgiving, and it is what this module is built on:

    a request is reusable up to the first byte that changed.

So the quantity worth protecting is the **longest unbroken leading run of the log**, and the way to
protect it is to remove one *contiguous block* rather than scattered turns. A hole in the middle and a
hole at the front invalidate the same bytes — the whole remainder, in both cases — but a hole at the
front additionally throws away the tail, which is what the model is still working with.

DESIGN
------
- **One span, not a set.** The removal is a single contiguous run of evictable turns, so the
  invalidation boundary is one position instead of several. A scattered set pays for the whole
  remainder at its earliest removal anyway and buys nothing for it.
- **Attention still chooses.** The cost of a candidate span is the staleness score the caller supplies
  — compaction's own — so a cheap adjacent pair beats an expensive single turn and priority-based
  eviction is preserved rather than replaced. Protected turns (AR-04) are absent from the candidate
  list, so a span cannot straddle one.
- **The store's word decides the tie.** When the store says the prefix is warm, the plan prefers the
  span that leaves the longest intact head, because those are the bytes the provider can still reuse.
  With no warm signal there is nothing to protect, so attention leads and the *oldest* qualifying run
  breaks the tie. An absent hit rate is an absence of evidence, never a cold prefix — the honesty rule
  the whole cache layer keeps.
- **The measurement is the bytes, not a flag.** `common_prefix_chars` reports how much of the log is
  byte-identical to what was sent before. That is the figure a provider's cache can actually reuse,
  and it is what makes "this compaction kept the prefix" checkable rather than asserted.
- **Bounded by construction.** The span search is a two-pointer walk over the log — the end of the
  minimal qualifying span never moves backwards as the start advances — so it is linear per run of
  evictable turns rather than quadratic over a long log.

Usage:
    verdict = consult_store(store, prefix_hash=pin.prefix_hash, skill="code-reviewer")
    plan = plan_eviction(evictable, tokens, needed=4800, warm=verdict.warm)
    kept = common_prefix_chars(log_text(before), log_text(after))
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..prefix import hash_text

__all__ = [
    "CacheVerdict",
    "EvictionPlan",
    "common_prefix_chars",
    "consult_store",
    "log_text",
    "plan_eviction",
    "prefix_hash_of",
]

#: The separator the log is joined with. It matches the projection's own join, because the two are
#: measuring the same bytes: a different separator here would report a prefix as intact that the
#: provider sees as changed.
LOG_SEPARATOR = "\n"


def log_text(turns: Sequence[Any]) -> str:
    """The log exactly as it is measured: every turn's text, in order.

    Joined the same way `projection.py` joins it, so a hash or a prefix length taken here describes
    the bytes the saturation figure was computed over rather than a second, quietly different
    rendering of them.
    """
    return LOG_SEPARATOR.join(str(getattr(turn, "text", "") or "") for turn in turns)


def prefix_hash_of(text: str) -> str:
    """The digest naming a surviving log prefix.

    Reuses `engine.prefix.hash_text` rather than defining a second hash: one digest function over the
    cacheable bytes is what keeps a store record and a live prefix comparable.
    """
    return hash_text(text)


def common_prefix_chars(before: str, after: str) -> int:
    """How many leading characters the two strings share.

    The only honest measure of what a prefix cache can still reuse, and deliberately a character count
    rather than a boolean: "the prefix survived" is a claim, while "the first 3602 characters are
    byte-identical" is a figure someone can hold against a bill.
    """
    limit = min(len(before), len(after))
    index = 0
    while index < limit and before[index] == after[index]:
        index += 1
    return index


@dataclass(frozen=True)
class CacheVerdict:
    """What the durable store says about this skill's prefix, and whether it had anything to say.

    `warm` is three-valued on purpose, and the three values drive different behaviour:

    - `True` — the store holds this prefix and reports no evidence against it, so the long head is
      worth protecting.
    - `False` — the store knows this prefix and it is not the one recorded, or the provider's own
      counters say nothing is being reused. There is no warm head to protect.
    - `None` — nothing to say: no store, or a store that could not be read. Not the same as cold, and
      the caller must not treat it as one.
    """

    warm: bool | None = None
    prefix_hash: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"warm": self.warm, "prefix_hash": self.prefix_hash, "note": self.note}


@dataclass(frozen=True)
class EvictionPlan:
    """Which turns to remove, and what that costs the provider's cache."""

    #: Ascending log order, because a removal is only cache-aligned if it reads as one block in the
    #: order the bytes are sent.
    positions: tuple[int, ...] = ()
    #: True when the removal is one contiguous run. False means the log could not be freed by a single
    #: run of evictable turns, and the attention-ordered set was used instead.
    aligned: bool = True
    #: The half-open span `[start, end)` that was removed, when the plan is aligned.
    span: tuple[int, int] | None = None
    #: Tokens the plan frees.
    freed: int = 0
    #: Turns before `start`, which stay byte-identical — the part the provider can still reuse.
    prefix_turns_kept: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "positions": list(self.positions),
            "aligned": self.aligned,
            "span": list(self.span) if self.span else None,
            "freed": self.freed,
            "prefix_turns_kept": self.prefix_turns_kept,
            "note": self.note,
        }


def plan_eviction(evictable: Sequence[tuple[int, float]], tokens: Sequence[int], *,
                  needed: int, warm: bool | None = None, aligned: bool = True) -> EvictionPlan:
    """Choose which turns to remove, preferring one contiguous run over scattered ones.

    Parameters
    ----------
    evictable:
        `(position, score)` for every turn that may be removed, in any order. A protected turn is
        absent from this list, and the runs are built from adjacency within it — which is what makes a
        span unable to straddle a protected turn by construction rather than by a check that could be
        forgotten. The AR-04 guard is untouched by any of this: it still reverts the whole compaction
        if the pin count drops.
    tokens:
        Tokens held by each turn, indexed by position.
    needed:
        Tokens to free. Zero or less removes nothing.
    warm:
        The store's verdict, when there is one. `True` prefers the span leaving the longest intact
        head — the bytes still reusable — while `False` and `None` leave attention in charge, since
        an absent verdict is not a cold prefix and neither one names anything worth protecting.
    aligned:
        False reproduces the rule this one replaces — the cheapest turns wherever they are. Kept
        because it is what the old behaviour was, and a comparison that has to reimplement the old
        rule to make its point is one that will drift from it.

    Returns
    -------
    EvictionPlan
        With `positions` empty when nothing needs to be freed.
    """
    if needed <= 0 or not evictable:
        return EvictionPlan(positions=(), freed=0,
                            note="nothing to free: the session is already at or below its target")

    ordered = sorted((int(position), float(score)) for position, score in evictable)

    if not aligned:
        return _scattered(ordered, tokens, needed=needed)

    best: tuple[tuple[float, int], list[tuple[int, float]], int, int] | None = None
    for run in _runs(ordered):
        # Two pointers: the minimal qualifying span for a later start cannot end earlier than the one
        # before it, because tokens are non-negative and the window only shrinks. That monotonicity is
        # what keeps this linear in the run's length.
        end = 0
        freed = 0
        cost = 0.0
        for start in range(len(run)):
            if end < start:
                end, freed, cost = start, 0, 0.0
            while end < len(run) and freed < needed:
                position, score = run[end]
                freed += max(0, int(tokens[position]))
                cost += score
                end += 1
            if freed >= needed:
                start_position = run[start][0]
                # Two objectives, and which one leads is the store's call.
                #
                # A provider caches a request up to its first changed byte, so the bytes still
                # reusable after a removal are those *before* the cut — which means removing the
                # latest qualifying run preserves the most. That is the cache-led choice, taken only
                # when the store actually says the prefix is warm: it costs attention, because the
                # latest turns are the ones the model is working with.
                #
                # With no warm signal there is nothing to protect, so the attention score leads and
                # the oldest qualifying run breaks the tie — the previous rule's priority-based
                # eviction, kept, with contiguousness as the only new constraint.
                key = (-start_position, cost) if warm else (cost, start_position)
                if best is None or key < best[0]:
                    best = (key, run, start, end)
            freed -= max(0, int(tokens[run[start][0]]))
            cost -= run[start][1]

    if best is None:
        # No single run of evictable turns reaches the target: a protected turn has split the log into
        # pieces too small. Attention-ordered eviction can still free it, and failing to reach the
        # target is the worse outcome — so the unaligned set is used, and reported as unaligned so the
        # shorter surviving prefix is on the record.
        fallback = _scattered(ordered, tokens, needed=needed)
        return EvictionPlan(
            positions=fallback.positions, aligned=False, span=None, freed=fallback.freed,
            prefix_turns_kept=fallback.prefix_turns_kept,
            note=("no single run of evictable turns reaches the target, so the attention-ordered "
                  "set was used and the surviving prefix is shorter than it could be"),
        )

    _, run, start, end = best
    positions = tuple(position for position, _ in run[start:end])
    freed = sum(max(0, int(tokens[position])) for position in positions)
    return EvictionPlan(
        positions=positions, aligned=True, span=(run[start][0], run[end - 1][0] + 1), freed=freed,
        prefix_turns_kept=run[start][0],
        note=(f"removed one contiguous run of {len(positions)} turn(s) from position "
              f"{run[start][0]}, leaving the first {run[start][0]} turn(s) byte-identical"),
    )


def _runs(ordered: Sequence[tuple[int, float]]) -> list[list[tuple[int, float]]]:
    """Maximal runs of consecutive positions.

    Consecutive in *position*, because adjacency in the log is what makes a removal a single cut in
    the bytes. A protected turn is simply absent from `ordered`, so it ends the run containing it.
    """
    runs: list[list[tuple[int, float]]] = []
    for entry in sorted(ordered):
        if runs and entry[0] == runs[-1][-1][0] + 1:
            runs[-1].append(entry)
        else:
            runs.append([entry])
    return runs


def _scattered(ordered: Sequence[tuple[int, float]], tokens: Sequence[int], *,
               needed: int) -> EvictionPlan:
    """The rule this module replaces: the cheapest turns wherever they are, until the target is met.

    Kept as the fallback for a log a single run cannot free, and as the behaviour the aligned rule is
    measured against. It is honest about its cost: the first chosen turn rewrites the whole remainder,
    so every turn chosen after it is paid for by a miss that had already happened.
    """
    by_score = sorted(ordered, key=lambda entry: (entry[1], entry[0]))
    positions: list[int] = []
    freed = 0
    for position, _ in by_score:
        if freed >= needed:
            break
        positions.append(position)
        freed += max(0, int(tokens[position]))
    positions.sort()
    kept = positions[0] if positions else 0
    return EvictionPlan(
        positions=tuple(positions), aligned=False, span=None, freed=freed,
        prefix_turns_kept=kept,
        note=(f"attention-ordered removal of {len(positions)} turn(s); the provider's cached bytes "
              f"are cut at position {kept}, so everything after it is re-paid"),
    )


def consult_store(store: Any, *, prefix_hash: str = "", skill: str = "") -> CacheVerdict:
    """Ask the durable store whether this prefix is warm, and why it says so.

    Never raises. A store that cannot be read leaves the verdict `None` — not warm, not cold — because
    a bookkeeping failure is not evidence about the provider's cache, and a compaction must not fail
    over one.

    The question is asked in the *pin* hash space when a hash is supplied, because that is the space
    the executor's prefix lives in. Asking with a request-shaped digest would answer "never seen" for
    a prefix that was pinned, which is the one wrong answer this store exists to prevent.
    """
    if store is None:
        return CacheVerdict(warm=None, note="no cache store is attached to this session")

    try:
        resolved = str(prefix_hash or "")
        if not resolved:
            # No hash supplied — a compaction that knows its skill but not yet its digest. The store's
            # most recent *pinned* record is the one this session is continuing from.
            record = store.latest_pinned(skill=skill) if skill else store.latest_pinned()
            if record is None:
                return CacheVerdict(
                    warm=False,
                    note=(f"the store holds no pinned prefix for {skill or 'this session'}, so this "
                          "is a cold start rather than a regression"))
            resolved = record.prefix_hash

        # Verified whichever way the hash was found. Reporting a prefix warm on the strength of a
        # record's existence alone would confirm bytes the store never checked.
        check = store.verify_prefix(prefix_hash=resolved, skill=skill or None)
        if not check.known or not check.unchanged:
            return CacheVerdict(warm=False, prefix_hash=resolved, note=check.reason)

        rate = _reported_hit_rate(store)
        if rate is not None and rate <= 0:
            # The provider's own counters outrank our record of what we pinned: a prefix we believe we
            # sent, that the provider says it reused nothing from, has no warm head worth protecting.
            return CacheVerdict(
                warm=False, prefix_hash=resolved,
                note=("the store reports a 0% hit rate, so the pinned prefix is not being reused and "
                      "there is no warm head to protect"))
        return CacheVerdict(
            warm=True, prefix_hash=resolved,
            note=f"prefix {resolved} is recorded and matches: this session's head is warm")
    except Exception as exc:  # noqa: BLE001 - a cache diagnosis must never break a compaction
        return CacheVerdict(
            warm=None,
            note=f"the cache store could not be read ({type(exc).__name__}: {exc})",
        )


def _reported_hit_rate(store: Any) -> float | None:
    """The store's aggregate hit rate, or `None` when nothing reported one.

    Read through `summary()` rather than recomputed here, so the honest-absence rule lives in one
    place: a provider that said nothing yields `None`, and this caller must not turn that into a zero.
    """
    try:
        return store.summary().get("cache_hit_rate")
    except Exception:  # noqa: BLE001 - an unreadable aggregate is an absent figure, not a failure
        return None
