#!/usr/bin/env python3
"""Phase 36 tests — cache-first compaction: keep the bytes the provider can still read.

`CacheStore` measured prompt-cache hit and miss tokens, and `compaction.py` evicted by attention
score anywhere in the transcript. Those two facts never met: removing a *middle* turn rewrites every
turn after it, so the provider — which reuses a request only up to its first changed byte — misses
from the removal to the end, and the engine re-pays for bytes it has already sent. The store recorded
those misses and nothing consulted them.

The tests here guard the properties that make the cache *used* rather than merely measured, and they
follow the same discipline as the layers beneath them: a claim about the cache has to be a figure or a
hash, never a flag.

1. **The prefix survives where a scattered eviction would not.** Asserted by comparing the bytes that
   remain against the bytes that were sent, and by showing the two rules leave *different* hashes.
2. **AR-04 still holds.** A pinned constraint survives, and a compaction that would drop one still
   reverts with the log restored exactly — cache alignment narrows *which* turns are candidates and
   must not weaken the guard in either direction.
3. **The store's warm-prefix signal is consulted.** A warm prefix changes which run is removed, and a
   cold one does not — so the behaviour is driven by the store's word rather than by a constant.
4. **A broken warm prefix is recorded attributably.** The cost has a line naming the hash and the
   character at which the cut fell, rather than being inferable only by comparing bills.
5. **A resume re-pins to the same hash**, so a continuation does not re-derive bytes the provider
   already holds.
6. **The honesty rule holds end to end.** A store with no reported hit rate is not read as cold: the
   verdict stays `None`, and `None` is not `False`.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import engine.context.compaction as compaction_mod
from engine.cachestore import CacheStore, PrefixCheck, PrefixRecord
from engine.context import (
    Band,
    Session,
    common_prefix_chars,
    compact,
    consult_store,
    log_text,
    plan_eviction,
)
from engine.pinning import PrefixPins
from engine.prefix import Prefix


# ── fixtures ─────────────────────────────────────────────────────────────────


class RecordingStore:
    """A store that answers one fixed verdict, and counts how often it was asked.

    Used instead of a real `CacheStore` where the *question* is what is under test: a live store's
    answer depends on what has been recorded, which would make the assertion about the fixture rather
    than about the compaction.
    """

    def __init__(self, *, warm: bool = True, hit_rate: float | None = 0.9,
                 known: bool = True, unchanged: bool = True, prefix_hash: str = "warmprefix0001"):
        self.warm = warm
        self.hit_rate = hit_rate
        self.known = known
        self.unchanged = unchanged
        self.prefix_hash = prefix_hash
        self.verify_calls: list[dict[str, object]] = []
        self.summary_calls = 0

    def verify_prefix(self, *, prefix_hash=None, skill=None, tool_names=None, chars=None):
        self.verify_calls.append({"prefix_hash": prefix_hash, "skill": skill})
        return PrefixCheck(known=self.known, unchanged=self.unchanged,
                           prefix_hash=str(prefix_hash or self.prefix_hash))

    def latest_pinned(self, skill: str = ""):
        return PrefixRecord(prefix_hash=self.prefix_hash, skill=skill, source="pin")

    def summary(self):
        self.summary_calls += 1
        return {"cache_hit_rate": self.hit_rate, "cache_reported": self.hit_rate is not None}


class ExplodingStore:
    """A store whose every answer raises. A compaction must survive it, not fail over it."""

    def verify_prefix(self, **kwargs):
        raise CacheStoreError("the diagnostics directory is gone")

    def latest_pinned(self, skill: str = ""):
        raise CacheStoreError("the diagnostics directory is gone")

    def summary(self):
        raise CacheStoreError("the diagnostics directory is gone")


class CacheStoreError(RuntimeError):
    """A local stand-in, so this module does not import the private error only to raise it."""


def session_at(*, window_turns: int = 8, chars: int = 300, tier: int = 2,
               saturation: float = 0.90) -> Session:
    """A session of uniform turns, sized so the band lands where the test needs it."""
    session = Session(agent_id="a", window=1, output_reserve=0)
    for index in range(window_turns):
        session.append_text("assistant", f"turn{index} " + "z" * chars, tier=tier)
    session.window = int(session.used_tokens / saturation)
    return session


# ── 1. the prefix survives where a scattered eviction would not ──────────────


def test_one_contiguous_run_is_removed_when_a_single_run_is_enough():
    """The claim is about the shape of the removal, and a flag alone would not carry it.

    The removed positions are checked for adjacency *and* for being the ones the plan named, because
    "aligned" is a word while `(8, 9)` is a fact someone can verify against the transcript.
    """
    plan = plan_eviction([(1, 0.1), (2, 5.0), (3, 5.0), (8, 0.1)],
                         [0, 10, 10, 10, 10, 10, 10, 10, 10, 10], needed=20)
    assert plan.aligned is True
    removed = list(plan.positions)
    assert removed == sorted(removed), "a removal set is only one block if it reads in log order"
    assert removed == list(range(removed[0], removed[0] + len(removed))), \
        f"the removal must be contiguous, got {removed}"
    assert plan.freed >= 20


def test_the_aligned_rule_keeps_more_of_the_prefix_than_the_scattered_one():
    """Proved against the bytes, and against the rule this one replaces.

    The cheapest turns in this fixture are scattered — the cheap tier sits at the front and the middle
    while the expensive ones sit between them — so the attention-ordered rule cuts near the front and
    the aligned rule takes a run at the end. That difference *is* the saving, so it is asserted as a
    character count and as two different hashes rather than as a boolean.
    """
    def build() -> Session:
        session = session_at(window_turns=8, chars=300, tier=2)
        return session

    sent = log_text(build().turns)

    # The scattered rule, reached by the documented switch rather than by reimplementing it: a test
    # that reimplements the old behaviour measures its own copy of it.
    scattered = build()
    original = compaction_mod.plan_eviction
    compaction_mod.plan_eviction = (
        lambda scored, tokens, *, needed, warm=None, aligned=True:
        original(scored, tokens, needed=needed, warm=warm, aligned=False))
    try:
        old = compact(scattered, target=0.5)
    finally:
        compaction_mod.plan_eviction = original

    aligned = build()
    new = compact(aligned, target=0.5, cache_store=RecordingStore(), cache_skill="s")

    assert new.prefix_chars_kept > old.prefix_chars_kept, (
        f"the aligned rule must preserve strictly more of the sent bytes: "
        f"{new.prefix_chars_kept} vs {old.prefix_chars_kept}")
    # Both rules were shown the same log, so the digest of what was sent is the same and the digest of
    # what each *left* differs — the two outcomes are genuinely different, not one described twice.
    assert new.prefix_hash_before == old.prefix_hash_before, "both compacted the same transcript"
    assert new.prefix_hash_after != old.prefix_hash_after, \
        "the two rules must leave different bytes, or the comparison is vacuous"
    assert sent.startswith(log_text(aligned.turns)[:new.prefix_chars_kept])


def test_a_warm_prefix_is_what_makes_the_tail_the_choice():
    """The store's verdict changes the outcome, so the signal is consulted and not decorative.

    Equal scores throughout, so the tie-break is the whole decision. Without a warm signal the
    attention rule leads and the *oldest* qualifying run goes; with one, the bytes still reusable are
    what matters, and a cut later in the log leaves more of them intact.
    """
    cold = plan_eviction([(0, 1.0), (1, 1.0), (2, 1.0), (3, 1.0)],
                         [10, 10, 10, 10], needed=20, warm=False)
    unknown = plan_eviction([(0, 1.0), (1, 1.0), (2, 1.0), (3, 1.0)],
                            [10, 10, 10, 10], needed=20, warm=None)
    warm = plan_eviction([(0, 1.0), (1, 1.0), (2, 1.0), (3, 1.0)],
                         [10, 10, 10, 10], needed=20, warm=True)
    # Cold and unknown agree — an absent signal is not a cold one, and both leave attention in charge.
    assert cold.prefix_turns_kept == unknown.prefix_turns_kept
    assert warm.prefix_turns_kept > cold.prefix_turns_kept, (
        f"a warm prefix must buy a longer intact head: {warm.prefix_turns_kept} vs "
        f"{cold.prefix_turns_kept}")
    assert warm.prefix_turns_kept == 2


def test_an_aligned_compaction_reports_how_much_of_the_log_survived():
    session = session_at()
    sent = log_text(session.turns)
    result = compact(session, target=0.5)
    payload = result.as_dict()
    assert payload["aligned"] is True
    assert payload["log_chars_before"] == len(sent)
    assert 0 < payload["prefix_chars_kept"] <= len(sent)
    assert "one contiguous run removed" in payload["note"]


def test_common_prefix_chars_measures_the_bytes_not_the_intent():
    """The measurement the whole module rests on: a character count, computed against the real text."""
    assert common_prefix_chars("abcdef", "abcxyz") == 3
    assert common_prefix_chars("same", "same") == 4
    assert common_prefix_chars("", "abc") == 0
    assert common_prefix_chars("abc", "xyz") == 0


def test_a_spread_removal_that_frees_nothing_is_reported_as_not_aligned():
    """The honest failure: no single run reaches the target, so the scattered set is used and *said*.

    A protected turn in the middle splits the log into pieces too small to free the deficit on their
    own. Freeing less than the target is the worse outcome, so the fallback is used — but it must not
    be reported as aligned, because that is the case that costs a warm prefix.
    """
    plan = plan_eviction([(0, 1.0), (2, 1.0)], [10, 0, 10], needed=30)
    assert plan.aligned is False, "a target no single run can reach must not be claimed as aligned"
    assert plan.freed == 20
    assert "shorter than it could be" in plan.note or "attention-ordered" in plan.note


# ── 2. AR-04 still holds ─────────────────────────────────────────────────────


def test_a_pinned_constraint_survives_the_aligned_compaction():
    """The protection is load-bearing and alignment narrows the candidates, not the guard."""
    session = Session(agent_id="a", window=1, output_reserve=0)
    session.pin("NEVER store passwords in plaintext")
    session.append_text("assistant", "NEVER store passwords in plaintext. " * 30, tier=3, pinned=True)
    for index in range(6):
        session.append_text("assistant", f"ordinary {index} " + "x" * 300, tier=3)
    session.window = int(session.used_tokens / 0.90)
    assert session.band in (Band.CRITICAL, Band.OVERFLOW)

    result = compact(session, target=0.4, cache_store=RecordingStore(), cache_skill="s")
    assert result.pinned_after == result.pinned_before == 1
    assert any("NEVER store passwords" in turn.text for turn in session.turns), \
        "the pinned turn must still be in the transcript"
    assert "NEVER store passwords in plaintext" in session.pinned


def test_a_marker_bearing_turn_ends_a_run_rather_than_being_straddled():
    """AR-04's other half: a constraint that arrived through a handoff, not pinned here.

    The protected turn is in the middle and evictable turns sit on both sides of it, so a run that
    ignored the protection would swallow it. The protection is what ends the run.
    """
    session = Session(agent_id="a", window=1, output_reserve=0)
    for index in range(3):
        session.append_text("assistant", f"before{index} " + "x" * 300, tier=3)
    session.append_text("assistant", "Reminder: NEVER log the raw auth token. " * 20, tier=3)
    for index in range(4):
        session.append_text("assistant", f"after{index} " + "y" * 300, tier=3)
    session.window = int(session.used_tokens / 0.90)

    compact(session, target=0.4, cache_store=RecordingStore(), cache_skill="s")
    assert any("NEVER log the raw auth token" in turn.text for turn in session.turns), \
        "a span must not straddle a protected turn"


def test_a_lost_pin_still_reverts_and_restores_the_log_exactly():
    """The guard fires *after* the eviction, so the revert must undo the aligned removal too.

    Without this the guard could pass while leaving the transcript rewritten — a silent loss with no
    record of it, which is exactly what AR-04 exists to prevent.
    """
    session = session_at()
    session.pin("NEVER log tokens")
    original = [turn.text for turn in session.turns]

    real_plan = compaction_mod.plan_eviction

    def plan_and_strip(scored, tokens, **kwargs):
        plan = real_plan(scored, tokens, **kwargs)
        session.pinned = []          # simulate a compaction that lost the constraint
        return plan

    compaction_mod.plan_eviction = plan_and_strip
    try:
        result = compact(session, target=0.5)
    finally:
        compaction_mod.plan_eviction = real_plan

    assert result.reverted is True
    assert "AR-04" in result.revert_reason
    assert len(session.pinned) == 1, "the pin must be restored"
    assert [turn.text for turn in session.turns] == original, "turns must be restored exactly"
    assert result.prefix_chars_kept == result.log_chars_before, \
        "a reverted compaction leaves the whole log intact and must say so"
    assert result.effective is False


def test_a_reverted_compaction_does_not_record_an_invalidation():
    """A compaction that was undone broke nothing, so recording a broken prefix would be false."""
    session = session_at()
    session.pin("NEVER log tokens")

    real_plan = compaction_mod.plan_eviction

    def plan_and_strip(scored, tokens, **kwargs):
        plan = real_plan(scored, tokens, **kwargs)
        session.pinned = []
        return plan

    compaction_mod.plan_eviction = plan_and_strip
    try:
        result = compact(session, target=0.5, cache_store=RecordingStore(), cache_skill="s")
    finally:
        compaction_mod.plan_eviction = real_plan

    assert result.reverted is True
    assert result.invalidated_prefix is None


# ── 3. the store's warm-prefix signal is actually consulted ──────────────────


def test_compaction_asks_the_store_about_this_prefix():
    """Consulted at the seam, with the pin hash space named — a mixed lookup answers 'never seen'."""
    store = RecordingStore(prefix_hash="pinprefix000001")
    session = session_at()
    compact(session, target=0.5, cache_store=store, cache_prefix_hash="pinprefix000001",
            cache_skill="code-reviewer")
    assert store.verify_calls, "the compaction must ask the store which prefix it is about to break"
    assert store.verify_calls[0]["prefix_hash"] == "pinprefix000001"
    assert store.verify_calls[0]["skill"] == "code-reviewer"
    assert store.summary_calls >= 1, "the provider's own hit rate is part of the verdict"


def test_a_recorded_but_changed_prefix_is_cold_not_warm():
    """`known and unchanged` is the pair; either half failing means there is no warm head."""
    store = RecordingStore(known=True, unchanged=False)
    verdict = consult_store(store, prefix_hash="abc")
    assert verdict.warm is False
    assert "gone cold" in verdict.note


def test_an_unreported_hit_rate_is_no_opinion_rather_than_cold():
    """The honesty rule, at this seam: absence of evidence is `None`, and `None` is not `False`.

    A provider that reported nothing must not make the engine believe its prefix is cold — that would
    trade a real cache away on the strength of a missing field.
    """
    no_numbers = RecordingStore(hit_rate=None)
    assert consult_store(no_numbers, prefix_hash="abc").warm is True

    assert consult_store(None, prefix_hash="abc").warm is None
    assert consult_store(ExplodingStore(), prefix_hash="abc").warm is None


def test_a_reported_zero_hit_rate_is_read_as_cold():
    """The other side of the rule: a *reported* zero is evidence, and must be believed."""
    verdict = consult_store(RecordingStore(hit_rate=0.0), prefix_hash="abc")
    assert verdict.warm is False
    assert "0% hit rate" in verdict.note


def test_the_store_is_asked_through_a_real_cachestore_too(tmp_path):
    """The seam is exercised against the real object, not only against a stand-in.

    The stand-in proves the compaction's *use* of the verdict; this proves the verdict itself is
    producible from what `PrefixPins` really writes.
    """
    store = CacheStore(tmp_path / "cache")
    pins = PrefixPins(run_id="run_1", store=store)
    prefix = pins.get_or_pin(skill="code-reviewer", system="sys", procedure="sop",
                             tools=[{"name": "read_file"}])
    assert consult_store(store, prefix_hash=prefix.prefix_hash, skill="code-reviewer").warm is True

    # The gateway's digest lives in a different hash space, so it must not be served as this pin's.
    from engine.cache import capture_shape

    gateway_hash = capture_shape(system="sys", schemas=[]).prefix_hash
    store.remember_prefix(prefix_hash=gateway_hash, source="gateway")
    assert store.pinned_prefix(gateway_hash) is None


def test_a_store_with_no_record_is_reported_as_a_cold_start(tmp_path):
    """The distinction the store exists to make: nothing to fix versus a regression to chase."""
    store = CacheStore(tmp_path / "cache")
    verdict = consult_store(store, skill="never-used")
    assert verdict.warm is False
    assert "cold start" in verdict.note


def test_a_store_that_raises_does_not_break_the_compaction():
    """A cache diagnosis is bookkeeping, and a run must not die over it."""
    session = session_at()
    result = compact(session, target=0.5, cache_store=ExplodingStore(), cache_skill="s")
    assert result.reverted is False
    assert result.effective is True
    assert result.cache_warm is None, "an unreadable store has no opinion, not a cold prefix"


# ── 4. a broken warm prefix is recorded attributably ─────────────────────────


def test_a_compaction_that_breaks_a_warm_prefix_records_the_cost():
    """The gap this whole workstream closes: the miss had no line to attribute it to."""
    store = RecordingStore(prefix_hash="warmprefixabcd")
    session = session_at()
    sent = log_text(session.turns)
    result = compact(session, target=0.5, cache_store=store, cache_prefix_hash="warmprefixabcd",
                     cache_skill="code-reviewer")

    record = result.invalidated_prefix
    assert record is not None, "a warm prefix broken by a compaction must be recorded"
    assert record["prefix_hash"] == "warmprefixabcd"
    assert record["skill"] == "code-reviewer"
    assert record["prefix_chars_kept"] == result.prefix_chars_kept
    assert record["evicted_turns"] == result.evicted_turns
    assert str(result.prefix_chars_kept) in record["reason"], \
        "the reason names the character at which the cut fell, so it can be checked"
    assert sent[:result.prefix_chars_kept] == log_text(session.turns)[:result.prefix_chars_kept]
    assert "a warm prefix was invalidated" in result.note


def test_a_compaction_that_evicts_nothing_records_no_invalidation():
    """Nothing was broken, so a record would be a false alarm — the thing a log must not produce."""
    session = session_at(window_turns=6, chars=100, saturation=0.80)
    assert session.band is Band.WARNING, session.band
    result = compact(session, target=0.5, cache_store=RecordingStore(), cache_skill="s")
    assert result.evicted_turns == 0
    assert result.invalidated_prefix is None


def test_a_cold_prefix_is_not_recorded_as_invalidated():
    """There was no warm prefix to break, so the compaction is not the cause of a miss."""
    session = session_at()
    result = compact(session, target=0.5, cache_store=RecordingStore(known=False), cache_skill="s")
    assert result.cache_warm is False
    assert result.invalidated_prefix is None


def test_the_invalidation_travels_in_the_result_payload():
    """Held on the result, not merely logged, so a caller can put it on the stream it watches."""
    session = session_at()
    sent = log_text(session.turns)
    payload = compact(session, target=0.5, cache_store=RecordingStore(),
                      cache_skill="s").as_dict()
    assert payload["cache_warm"] is True
    assert payload["invalidated_prefix"] is not None
    assert payload["invalidated_prefix"]["aligned"] is True
    assert payload["prefix_hash_before"] != payload["prefix_hash_after"], \
        "the digest of the log as sent differs from the digest of what survived"
    assert payload["log_chars_before"] == len(sent)
    assert log_text(session.turns).startswith(sent[:payload["prefix_chars_kept"]])


# ── 5. a resume re-pins to the same hash ─────────────────────────────────────


def test_a_resume_re_pins_to_the_recorded_hash(tmp_path):
    """A continuation must send the bytes its predecessor sent, not bytes that look the same."""
    store = CacheStore(tmp_path / "cache")
    first = PrefixPins(run_id="run_1", store=store)
    prefix = first.get_or_pin(skill="code-reviewer", system="sys", procedure="sop",
                              tools=[{"name": "read_file"}])

    # A fresh process, resumed: a new pin store reading the same durable record.
    reloaded = CacheStore(tmp_path / "cache")
    resumed = PrefixPins(run_id="run_1", store=reloaded)
    check = resumed.resume_from_store(skill="code-reviewer", tools=[{"name": "read_file"}],
                                      system="sys", procedure="sop")
    assert check is not None
    assert check.known is True
    assert check.unchanged is True
    again = resumed.get_or_pin(skill="code-reviewer", system="sys", procedure="sop",
                               tools=[{"name": "read_file"}])
    assert again.prefix_hash == prefix.prefix_hash, "a resume must re-pin to the same hash"


def test_a_resume_reports_a_prefix_that_moved_rather_than_adopting_it(tmp_path):
    """An edited skill between runs is a real event, and the resumed run must be told."""
    store = CacheStore(tmp_path / "cache")
    PrefixPins(run_id="run_1", store=store).get_or_pin(
        skill="s", system="sys", procedure="v1", tools=[])
    recorded = store.latest_pinned(skill="s")
    assert recorded is not None

    reloaded = CacheStore(tmp_path / "cache")
    resumed = PrefixPins(run_id="run_2", store=reloaded)
    pinned = resumed.get_or_pin(skill="s", system="sys", procedure="v2", tools=[])
    # The bytes this run derived are genuinely different from the recorded ones, which is the case the
    # check exists to report rather than to paper over.
    assert pinned.prefix_hash != recorded.prefix_hash
    check = reloaded.verify_prefix(prefix_hash=recorded.prefix_hash, skill="s")
    assert check.known is True and check.unchanged is True


def test_a_resume_with_no_history_is_a_cold_start_and_never_raises(tmp_path):
    """A resume that cannot read its history is a degraded state, not a failed run."""
    resumed = PrefixPins(run_id="run_1", store=CacheStore(tmp_path / "cache"))
    assert resumed.resume_from_store(skill="s", tools=[], system="sys", procedure="p") is None
    assert PrefixPins(run_id="run_1").resume_from_store(
        skill="s", tools=[], system="sys", procedure="p") is None
    assert resumed.resume_from_store(skill="s", tools=[], store=ExplodingStore()) is None


def test_the_pins_can_be_read_without_being_mutated():
    """A reporting caller must not be handed the live mapping a running session sends from."""
    pins = PrefixPins(run_id="r")
    prefix = pins.get_or_pin(skill="s", system="sys", procedure="p", tools=[])
    snapshot = pins.pinned_all()
    snapshot.clear()
    assert pins.pinned(skill="s", tools=[]) is prefix


# ── 6. bounded and safe ──────────────────────────────────────────────────────


def test_the_run_search_is_linear_in_each_run_of_evictable_turns():
    """Bounded by construction: a long log must not turn a compaction into a quadratic walk.

    A thousand evictable turns, each tiny, with a target one turn's worth of tokens short. The
    two-pointer search must answer in one pass per run rather than a thousand nested ones.
    """
    count = 1_000
    evictable = [(index, 1.0) for index in range(count)]
    tokens = [1] * count
    plan = plan_eviction(evictable, tokens, needed=4, warm=True)
    assert plan.aligned is True
    assert plan.freed >= 4
    assert plan.prefix_turns_kept == count - len(plan.positions)


def test_a_plan_that_needs_nothing_removes_nothing():
    """The stopping condition is a deficit, so a session at its target is left alone."""
    plan = plan_eviction([(0, 1.0), (1, 1.0)], [10, 10], needed=0)
    assert plan.positions == ()
    assert plan.freed == 0
    plan = plan_eviction([], [], needed=100)
    assert plan.positions == ()


def test_an_empty_session_is_not_compacted():
    session = Session(agent_id="a", window=1000, output_reserve=0)
    result = compact(session, target=0.5, cache_store=RecordingStore(), cache_skill="s")
    assert result.action.value == "none"
    assert result.effective is False


def test_a_band_that_evicts_nothing_reports_the_log_whole():
    """Zero would read as "the prefix was destroyed" on a path that did not touch it.

    The two digests agreeing is what makes `prefix_chars_kept == log_chars_before` a statement about
    the bytes rather than an arithmetic coincidence.
    """
    for saturation, expected in ((0.50, Band.HEALTHY), (0.78, Band.WARNING)):
        session = Session(agent_id="a", window=1000, output_reserve=0)
        session.append_text("assistant", "x" * int(4000 * saturation))
        assert session.band is expected, session.band
        payload = compact(session, cache_store=RecordingStore(), cache_skill="s").as_dict()
        assert payload["evicted_turns"] == 0
        assert payload["prefix_chars_kept"] == payload["log_chars_before"] > 0
        assert payload["prefix_hash_before"] == payload["prefix_hash_after"], \
            "nothing changed, so the bytes the provider holds are the bytes it will see"
        assert payload["invalidated_prefix"] is None


def test_the_plan_carries_its_span_so_the_caller_can_check_it():
    """The whole half-open span is reported, which is what makes the removal auditable."""
    plan = plan_eviction([(2, 1.0), (3, 1.0), (4, 1.0)], [0, 0, 5, 5, 5], needed=10)
    assert plan.aligned is True
    assert plan.positions == (2, 3)
    assert plan.span == (2, 4), plan.span
    assert plan.as_dict()["span"] == [2, 4]
    assert plan.prefix_turns_kept == 2, "positions 0 and 1 are untouched, so the head is 2 turns"


def test_a_reverted_compaction_restores_turn_order_exactly():
    """AR-04's revert must restore the *sequence*, not merely the count.

    Found by running this file in a loop: it failed in 15 of 40 runs. `_evict` returned the removed
    turns, `_restore` put them back with `sorted(..., key=turn.at)`, and `Turn.at` collides when turns
    are appended in a tight loop — eight turns produced `…94642` twice. `sorted` is stable, but its
    input was `[*surviving, *removed]`, so tied turns kept *that* order instead of their original one
    and the log came back `turn0, turn1, turn2, turn4, turn3, …`.

    It matters because the guard verifies a count: the revert reported success while returning a
    reordered transcript, so a pinned constraint could change position with nothing recording it —
    the silent loss AR-04 exists to prevent. The restore now reinserts by the position each turn came
    from, which is exact.

    Asserted as full equality of the *text* in order, because a count alone passed on the broken code.
    """
    import engine.context.compaction as compaction_mod
    from engine.context.compaction import compact

    session = session_at()
    session.pin("NEVER log tokens")
    original = [turn.text for turn in session.turns]

    real_plan = compaction_mod.plan_eviction

    def plan_and_strip(scored, tokens, **kwargs):
        plan = real_plan(scored, tokens, **kwargs)
        session.pinned = []
        return plan

    compaction_mod.plan_eviction = plan_and_strip
    try:
        result = compact(session, target=0.5)
    finally:
        compaction_mod.plan_eviction = real_plan

    assert result.reverted is True
    assert [turn.text for turn in session.turns] == original, (
        "a reverted compaction must restore the turns in their original ORDER — equal counts with "
        "swapped positions is the bug this pins")
    # The same property stated positionally, so a future reordering cannot pass by coincidence.
    assert [turn.text[:6] for turn in session.turns] == [f"turn{i} " for i in range(8)]
