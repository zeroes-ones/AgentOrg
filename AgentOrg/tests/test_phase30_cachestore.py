#!/usr/bin/env python3
"""Phase 30 tests — the durable cache store, and the oversize-result prune.

Prompt caching works and is measured, and both of those facts lived in memory: a restart lost the
history that explains a bill, and nothing on disk could answer the one question a restart raises —
*is this run still cache-warm?* The tests here guard the properties that make the durable store worth
having rather than merely present:

1. **It survives a restart.** The history is rebuilt by replay, so a resumed run can attribute a miss
   its predecessor saw.
2. **An unreported figure stays unreported.** A hit rate of "no idea" must not become a confident 0%
   by passing through a file. This is the honesty rule the whole cache layer keeps.
3. **It is bounded.** An unattended run appends a line per call; unbounded growth is a disk-filling
   bug, and the oldest-first eviction is what prevents it.
4. **A store failure never breaks a run.** A paid-for answer must not be discarded over bookkeeping.
5. **The prune does not weaken AR-04.** A pinned constraint is preserved verbatim whether the
   compaction succeeded or reverted.
6. **The two hash spaces are kept apart.** The gateway's `capture_shape` digest and the executor's
   `Prefix.prefix_hash` are different hashes over different bytes; a store that mixed them would
   confirm a prefix it never held.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine.cache import capture_shape, compare_shapes
from engine.cachestore import (
    MAX_PREFIX_FILES,
    MAX_SHAPE_LINES,
    CacheStore,
    CacheStoreError,
    PrefixRecord,
)
from engine.config import load
from engine.context import Band, CompactionAction, Session, Turn, compact
from engine.gateway import Gateway
from engine.prefix import Prefix
from engine.pinning import PrefixPins
from engine.providers.base import ChatRequest, Message, Role, Usage
from engine.providers.fake import FakeProvider
from engine.state import Workspace


@pytest.fixture(scope="module")
def config():
    return load()


def _store(tmp_path, **kwargs) -> CacheStore:
    return CacheStore(tmp_path / "cache", **kwargs)


# ── the store survives a restart ─────────────────────────────────────────────


def test_the_history_survives_a_simulated_restart(tmp_path):
    """Write, drop the object, reload: the whole history must be there.

    This is the property that did not exist. `ShapeTracker` keeps one shape for the life of one
    process, so after a restart the first call has no predecessor and a real miss is indistinguishable
    from a cold start.
    """
    store = _store(tmp_path)
    first = capture_shape(system="sys", schemas=[{"name": "read"}])
    second = capture_shape(system="sys", schemas=[{"name": "read"}, {"name": "write"}])
    store.record_shape(compare_shapes(None, first, usage=Usage(
        prompt_tokens=100, completion_tokens=1, cache_hit_tokens=0, cache_miss_tokens=100)))
    store.record_shape(compare_shapes(first, second, usage=Usage(
        prompt_tokens=120, completion_tokens=1, cache_hit_tokens=0, cache_miss_tokens=120)))
    store.remember_prefix(prefix_hash=first.prefix_hash, run_id="run_1")
    del store

    reloaded = _store(tmp_path)
    assert len(reloaded.shapes()) == 2
    # The *sequence* is the diagnosis, so it must come back in order rather than as a count.
    assert reloaded.shapes()[0]["prefix_changed"] is False
    assert reloaded.shapes()[1]["prefix_changed"] is True
    assert "tools" in reloaded.shapes()[1]["prefix_change_reasons"]
    assert reloaded.prefix(first.prefix_hash) is not None


def test_a_torn_final_line_is_skipped_rather_than_fatal(tmp_path):
    """A killed process leaves a partial line, which is the expected shape of a crash."""
    store = _store(tmp_path)
    store.record_shape(compare_shapes(None, capture_shape(system="s"), usage=Usage()))
    with open(store.shapes_path, "a", encoding="utf-8") as fh:
        fh.write('{"turn": 1, "prefix_ha')      # killed mid-write

    reloaded = _store(tmp_path)
    assert len(reloaded.shapes()) == 1, "the complete history must survive a torn tail"
    assert reloaded.load_error == ""


def _cost(saving: float | None, source: str = "estimated") -> Any:
    """The two attributes `record_usage` reads off a `Cost`, without constructing a real one."""
    class _Cost:
        cache_saving_usd = saving
    _Cost.source = source
    return _Cost


def test_the_saving_history_rebuilds_from_the_provider_counters(tmp_path):
    """The recorded saving is the provider's own arithmetic, replayed rather than recomputed."""
    store = _store(tmp_path)
    store.record_usage(Usage(prompt_tokens=1000, completion_tokens=10, cache_hit_tokens=800,
                             cache_miss_tokens=200), _cost(0.004),
                       provider_id="deepseek", model="deepseek-chat")
    reloaded = _store(tmp_path)
    records = reloaded.savings()
    assert records[0]["read_tokens"] == 800
    assert records[0]["miss_tokens"] == 200
    assert records[0]["saving_usd"] == pytest.approx(0.004)
    assert records[0]["cost_source"] == "estimated"


# ── an unreported figure is stored as absent, never as zero ──────────────────


def test_an_unreported_hit_rate_is_stored_as_absent(tmp_path):
    """`None` to the file, never 0 — the same rule the in-memory layer keeps."""
    store = _store(tmp_path)
    store.record_usage(Usage(prompt_tokens=1000, completion_tokens=10))
    record = json.loads(store.savings_path.read_text(encoding="utf-8").strip())
    assert record["read_tokens"] is None
    assert record["miss_tokens"] is None
    assert record["hit_rate"] is None
    assert record["cache_reported"] is False
    assert record["saving_usd"] is None


def test_an_unreported_rate_does_not_become_zero_after_a_reload(tmp_path):
    store = _store(tmp_path)
    store.record_usage(Usage(prompt_tokens=1000, completion_tokens=10))
    reloaded = _store(tmp_path)
    assert reloaded.savings()[0]["hit_rate"] is None
    summary = reloaded.summary()
    assert summary["cache_reported"] is False
    assert summary["cache_hit_tokens"] is None
    assert summary["cache_miss_tokens"] is None
    assert summary["cache_hit_rate"] is None
    assert summary["cache_saving_usd"] is None
    assert summary["unreported_records"] == 1


def test_an_explicitly_reported_zero_is_stored_as_zero(tmp_path):
    """Unlike an absent report, a reported total of zero is a fact and must survive as one."""
    store = _store(tmp_path)
    store.record_usage(Usage(prompt_tokens=1000, completion_tokens=1,
                             cache_hit_tokens=0, cache_miss_tokens=1000))
    record = store.savings()[0]
    assert record["read_tokens"] == 0
    assert record["hit_rate"] == 0.0
    assert record["cache_reported"] is True


def test_the_aggregate_rate_uses_only_records_that_reported_both_halves(tmp_path):
    """Summing a hit count against an unreported miss would produce a confident 100%."""
    store = _store(tmp_path)
    store.record_usage(Usage(prompt_tokens=1000, completion_tokens=1,
                             cache_hit_tokens=600, cache_miss_tokens=400))
    # A provider that reports a read with no miss: its tokens are counted, its *rate* is not claimed.
    store.record_usage(Usage(prompt_tokens=500, completion_tokens=1, cache_hit_tokens=500))
    summary = store.summary()
    assert summary["cache_reported"] is True
    assert summary["cache_hit_tokens"] == 1100
    assert summary["cache_hit_rate"] == pytest.approx(0.6)
    assert summary["rate_records"] == 1, "the rate is over the one record that gave a denominator"


# ── the store is bounded, and evicts oldest-first ────────────────────────────


def test_the_shape_stream_is_bounded_and_evicts_the_oldest_first(tmp_path):
    """An overnight run appends a line per call; growth without limit is a disk-filling bug."""
    store = _store(tmp_path, max_shape_lines=5)
    for turn in range(1, 13):
        store.record_shape(compare_shapes(None, capture_shape(system=f"s{turn}"), usage=Usage()),
                           turn=turn)
    assert len(store.shapes()) == 5
    assert [s["turn"] for s in store.shapes()] == [8, 9, 10, 11, 12], "the newest must survive"

    reloaded = _store(tmp_path, max_shape_lines=5)
    assert [s["turn"] for s in reloaded.shapes()] == [8, 9, 10, 11, 12], \
        "the bound must hold on disk, not only in memory"


def test_the_savings_stream_is_bounded_too(tmp_path):
    store = _store(tmp_path, max_savings_lines=3)
    for index in range(1, 8):
        store.record_usage(Usage(prompt_tokens=index, completion_tokens=1,
                                 cache_hit_tokens=index, cache_miss_tokens=0))
    assert len(store.savings()) == 3
    lines = store.savings_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3, "the file itself must be trimmed, not only the in-memory view"


def test_the_prefix_set_is_bounded_by_count(tmp_path):
    """A genuinely unstable prefix writes a file per call while never repeating one."""
    store = _store(tmp_path, max_prefix_files=4)
    for index in range(1, 10):
        store.remember_prefix(prefix_hash=f"hash{index:02d}", skill="s", chars=10 * index)
    assert len(store.prefixes()) == 4
    assert len(list(store.prefix_dir.glob("*.json"))) == 4, "the evicted files must be gone"
    assert store.prefix("hash09") is not None, "the newest prefix must survive"


def test_the_prefix_eviction_drops_the_least_recently_seen(tmp_path):
    store = _store(tmp_path, max_prefix_files=2)
    store.remember_prefix(prefix_hash="aaa", skill="s")
    store.remember_prefix(prefix_hash="bbb", skill="s")
    # Re-seeing `aaa` is what makes `bbb` the least recently seen, and therefore the one to drop.
    store.remember_prefix(prefix_hash="aaa", skill="s")
    store.remember_prefix(prefix_hash="ccc", skill="s")
    assert store.prefix("aaa") is not None
    assert store.prefix("ccc") is not None
    assert store.prefix("bbb") is None


def test_the_bounds_are_named_constants(tmp_path):
    """The bound is the promise, so it is stated rather than buried in a signature."""
    from engine import cachestore

    assert cachestore.MAX_SHAPE_LINES > 0
    assert _store(tmp_path).summary()["bounds"]["max_shape_lines"] == MAX_SHAPE_LINES
    assert _store(tmp_path).summary()["bounds"]["max_prefix_files"] == MAX_PREFIX_FILES


# ── the question a restart raises: is this run still cache-warm? ─────────────


def test_a_re_pinned_prefix_hashes_the_same_across_a_reload(tmp_path):
    """The whole point: the bytes this run would send, against the bytes its predecessor sent."""
    pins = PrefixPins(run_id="run_1")
    prefix = pins.get_or_pin(skill="code-reviewer", system="sys", procedure="sop",
                             tools=[{"name": "read_file"}])

    store = _store(tmp_path)
    store.remember_prefix(prefix_hash=prefix.prefix_hash, skill="code-reviewer",
                          tool_names=["read_file"], chars=prefix.chars, run_id="run_1")
    del store, prefix

    # A fresh process, re-pinning from the same sources.
    reloaded = _store(tmp_path)
    again = PrefixPins(run_id="run_2").get_or_pin(
        skill="code-reviewer", system="sys", procedure="sop", tools=[{"name": "read_file"}])
    check = reloaded.verify_prefix(prefix_hash=again.prefix_hash, skill="code-reviewer",
                                   tool_names=["read_file"], chars=again.chars)
    assert check.known is True
    assert check.unchanged is True
    assert "same one recorded" in check.reason


def test_a_changed_procedure_is_reported_as_a_cold_cache_not_a_cold_start(tmp_path):
    """The distinction matters: 'never seen' has nothing to fix, 'seen then changed' does."""
    store = _store(tmp_path)
    before = Prefix.for_skill(skill="s", system="sys", procedure="v1", tools=[])
    store.remember_prefix(prefix_hash=before.prefix_hash, skill="s", chars=before.chars)

    after = Prefix.for_skill(skill="s", system="sys", procedure="v2", tools=[])
    check = store.verify_prefix(prefix_hash=after.prefix_hash, skill="s", chars=after.chars)
    assert check.known is False, "a different hash is a prefix that has never been sent"

    # And the same hash with different bytes under it is the case that *is* reported as a change.
    mismatched = store.verify_prefix(prefix_hash=before.prefix_hash, skill="s",
                                     chars=before.chars + 1)
    assert mismatched.known is True
    assert mismatched.unchanged is False
    assert mismatched.mismatches == ["chars"]
    assert "gone cold" in mismatched.reason


def test_an_unknown_field_is_not_reported_as_a_mismatch(tmp_path):
    """The gateway records a prefix from a request and knows its tools but not its skill."""
    store = _store(tmp_path)
    prefix = Prefix.for_skill(skill="s", system="sys", procedure="p", tools=[{"name": "read_file"}])
    store.remember_prefix(prefix_hash=prefix.prefix_hash, tool_names=["read_file"],
                          chars=prefix.chars)
    check = store.verify_prefix(prefix_hash=prefix.prefix_hash, tool_names=["read_file"],
                                chars=prefix.chars)
    assert check.known is True
    assert check.unchanged is True, "an unrecorded skill must not be read as a change"


def test_re_seeing_a_prefix_records_the_observation_without_replacing_the_history(tmp_path):
    """`first_seen` is what separates a prefix introduced today from one stable all week."""
    store = _store(tmp_path)
    first = store.remember_prefix(prefix_hash="abc", skill="s", chars=10)
    second = store.remember_prefix(prefix_hash="abc", skill="s", chars=10)
    assert second.first_seen == first.first_seen
    assert second.observations == 2
    assert store.remember_prefix(prefix_hash="abc", run_id="r2").runs[:2] == ["r2", ""] or \
        store.prefix("abc").runs[0] == "r2"


def test_a_prefix_hash_that_cannot_name_a_file_is_refused(tmp_path):
    """The hash is a filename, so a traversal segment must not be writable through it."""
    store = _store(tmp_path)
    for bad in ("../escape", "a/b", "", ".hidden"):
        with pytest.raises(CacheStoreError):
            store.remember_prefix(prefix_hash=bad)


def test_the_two_hash_spaces_are_not_confused_for_each_other(tmp_path):
    """`capture_shape` and `Prefix` hash different bytes, so a record must say which it came from.

    A lookup that mixed them would report "never seen" for a prefix that *was* pinned — or, worse,
    confirm one it never held. Reporting nothing is the honest failure; a false confirmation is not.
    """
    from engine.cache import capture_shape

    store = _store(tmp_path)
    pin = Prefix.for_skill(skill="s", system="sys", procedure="p", tools=[])
    gateway_hash = capture_shape(system="sys", schemas=[]).prefix_hash
    assert pin.prefix_hash != gateway_hash, "the fixtures must actually be in different spaces"

    store.remember_prefix(prefix_hash=pin.prefix_hash, skill="s", source="pin")
    store.remember_prefix(prefix_hash=gateway_hash, source="gateway")
    assert store.pinned_prefix(pin.prefix_hash) is not None
    assert store.pinned_prefix(gateway_hash) is None, \
        "a request-shaped digest must not be served as a pin record"
    assert store.prefix(gateway_hash) is not None, "it is still recorded, just not as a pin"


def test_a_prefix_record_round_trips_through_its_dict():
    record = PrefixRecord(prefix_hash="abc", skill="s", tool_names=["read_file"], chars=12,
                          first_seen="t1", last_seen="t2", observations=3, runs=["r1"],
                          source="pin")
    assert PrefixRecord.from_dict(record.as_dict()).as_dict() == record.as_dict()


# ── a store failure does not break a run ─────────────────────────────────────


def test_a_store_that_cannot_write_does_not_stop_the_call(config, tmp_path):
    """A paid-for answer must not be discarded over bookkeeping."""
    class Exploding:
        def remember_prefix(self, **kwargs):
            raise CacheStoreError("the disk is full")

        def record_shape(self, *args, **kwargs):
            raise CacheStoreError("the disk is full")

        def record_usage(self, *args, **kwargs):
            raise CacheStoreError("the disk is full")

    fake = FakeProvider(script=[{"text": "done", "prompt_tokens": 10, "completion_tokens": 1}])
    gateway = Gateway(config, {"fake": fake}, cache_store=Exploding())
    response = gateway.complete(
        ChatRequest(model="fake-model", system="sys", messages=[Message.text_message(Role.USER, "go")]),
        provider_id="fake", agent_id="ag_1")
    assert response.text == "done"
    assert gateway.ledger.calls == 1
    assert gateway._cache_store_warning, "the failure must be reported, not swallowed silently"


def test_a_store_failure_is_reported_once_rather_than_per_call(config):
    """A hundred-call run would otherwise bury the fact in a hundred copies of the same line.

    The counter is the whole guard, so it is asserted directly: the log line is emitted once per
    *process*, and a failure that repeats on every call must not turn into a wall of identical text.
    """
    class Exploding:
        def remember_prefix(self, **kwargs):
            raise CacheStoreError("nope")

        def record_shape(self, *args, **kwargs):
            raise CacheStoreError("nope")

        def record_usage(self, *args, **kwargs):
            raise CacheStoreError("nope")

    fake = FakeProvider(default_reply={"text": "ok"})
    gateway = Gateway(config, {"fake": fake}, cache_store=Exploding())
    for _ in range(4):
        gateway.complete(ChatRequest(model="m", messages=[]), provider_id="fake")
    assert gateway._cache_store_warning, "the failure must be recorded"
    # `_warn_cache_store` is the only writer of that field and it returns early once it is set, which
    # is what makes the emission a once-per-process fact rather than a once-per-call one.
    before = gateway._cache_store_warning
    gateway._warn_cache_store(CacheStoreError("again"))
    assert gateway._cache_store_warning == before


def test_a_gateway_with_no_store_records_nothing_and_still_calls(config):
    """The store is optional: a test or a probe builds a gateway without a workspace."""
    fake = FakeProvider(script=[{"text": "fine"}])
    gateway = Gateway(config, {"fake": fake})
    assert gateway.complete(ChatRequest(model="m", messages=[]), provider_id="fake").text == "fine"
    assert gateway.durable_cache_summary()["attached"] is False
    assert gateway.durable_cache_summary()["cache_hit_rate"] is None


def test_the_gateway_persists_the_shape_and_the_savings_as_it_observes_them(config, tmp_path):
    """Wired in, not merely available: the store must fill from real calls."""
    store = _store(tmp_path)
    fake = FakeProvider(script=[{"text": "a", "prompt_tokens": 100, "completion_tokens": 5},
                                {"text": "b", "prompt_tokens": 100, "completion_tokens": 5}])
    gateway = Gateway(config, {"fake": fake}, cache_store=store, run_id="run_1")
    request = ChatRequest(model="fake-model", system="sys",
                          messages=[Message.text_message(Role.USER, "go")])
    gateway.complete(request, provider_id="fake", agent_id="ag_1", node_id="fixer")
    gateway.complete(request, provider_id="fake", agent_id="ag_1", node_id="fixer")

    reloaded = _store(tmp_path)
    assert len(reloaded.shapes()) == 2
    assert len(reloaded.savings()) == 2
    assert reloaded.shapes()[1]["prefix_changed"] is False, "the same request is the same prefix"
    # The fake provider reports no cache at all, so the record must say so rather than say zero.
    assert reloaded.savings()[0]["cache_reported"] is False
    assert reloaded.summary()["cache_hit_rate"] is None
    assert gateway.durable_cache_summary()["shapes"] == 2


def test_the_streaming_path_records_the_same_facts(config, tmp_path):
    """A call is a call: which surface the caller used must not decide whether it is recorded."""
    store = _store(tmp_path)
    fake = FakeProvider(script=[{"text": "abc", "chunks": ["a", "b", "c"]}])
    gateway = Gateway(config, {"fake": fake}, cache_store=store, run_id="run_1")
    list(gateway.stream(ChatRequest(model="m", messages=[Message.text_message(Role.USER, "go")]),
                        provider_id="fake", agent_id="ag_1"))
    assert len(store.shapes()) == 1
    assert len(store.savings()) == 1


# ── the workspace owns the path ──────────────────────────────────────────────


def test_the_workspace_owns_the_cache_directory(tmp_path):
    """Naming the layout once is what keeps the engine, the inspector and the bundle in step."""
    workspace = Workspace.for_project("demo", root=tmp_path / "projects")
    workspace.ensure()
    assert workspace.cache_dir == workspace.state_dir / "cache"
    assert CacheStore.for_workspace(workspace).directory == workspace.cache_dir


def test_the_store_is_created_on_first_write_not_on_open(tmp_path):
    """An inspector must not mutate a workspace merely by looking at it."""
    directory = tmp_path / "cache"
    CacheStore(directory).summary()
    assert not directory.exists()
    CacheStore(directory).remember_prefix(prefix_hash="abc")
    assert directory.is_dir()


def test_pinning_records_the_prefix_durably(tmp_path):
    """A pin that is only in memory cannot answer the question a restart raises."""
    store = _store(tmp_path)
    pins = PrefixPins(run_id="run_1", store=store)
    prefix = pins.get_or_pin(skill="s", system="sys", procedure="proc", tools=[{"name": "read_file"}])
    record = _store(tmp_path).prefix(prefix.prefix_hash)
    assert record is not None
    assert record.skill == "s"
    assert record.tool_names == ["read_file"]
    assert record.chars == prefix.chars
    assert record.runs == ["run_1"]


def test_a_pin_store_with_a_broken_store_still_pins(tmp_path):
    """A pin that cannot be *recorded* is still a pin."""
    class Exploding:
        def remember_prefix(self, **kwargs):
            raise CacheStoreError("nope")

    pins = PrefixPins(run_id="run_1", store=Exploding())
    prefix = pins.get_or_pin(skill="s", system="sys", procedure="proc", tools=[])
    assert pins.pinned(skill="s", tools=[]) is prefix


# ── the oversize-result prune, before summarisation ──────────────────────────


def _oversize_session(chars: int = 40_000) -> Session:
    """A critical session holding one enormous result and some ordinary history.

    The window is sized from the content so the saturation lands in the critical band: the point of
    these tests is the prune, and a fixture whose band depends on arithmetic happening to work out is
    a test that stops testing the prune the moment the token estimate changes. The background turns
    are there because the newest few turns are never pruned — a four-turn session is deliberately
    left alone, so a fixture that short would be asserting on the exception rather than the rule.
    """
    session = Session(agent_id="a", window=1, output_reserve=0)
    session.append_text("user", "summarise the listing", tier=1)
    session.append_text("tool", "HEAD" + "x" * chars + "TAIL", tier=3)
    for index in range(6):
        session.append_text("assistant", f"ordinary history {index}", tier=2)
    session.window = int(session.used_tokens / 0.90)
    assert session.band in (Band.CRITICAL, Band.OVERFLOW), session.saturation
    return session


def test_an_oversize_result_is_pruned_rather_than_evicted_whole():
    """The adopted behaviour: a head, a marker and a tail — not the loss of the whole turn."""
    session = _oversize_session()
    assert session.band in (Band.CRITICAL, Band.OVERFLOW)
    result = compact(session, target=0.4)
    assert result.pruned_turns >= 1
    assert result.pruned_tokens > 0
    pruned = [turn for turn in session.turns if "characters omitted from the middle" in turn.text]
    assert pruned, "the marker must be written into the text, not merely reported"
    text = pruned[0].text
    assert text.startswith("HEAD"), "the head is kept"
    assert text.endswith("TAIL"), "the tail is kept"
    assert len(text) < 40_000


def test_the_prune_keeps_the_newest_turns_verbatim():
    """The live working set is what the model is mid-way through using.

    A session of four turns is *never* pruned, which is asserted separately below: the guarantee is
    that the newest turns are left alone, and that has to hold for a session where older material
    exists to cut.
    """
    session = _oversize_session()
    result = compact(session, target=0.4)
    assert result.pruned_turns >= 1, "the fixture must have something prunable in it"
    for turn in session.turns[-3:]:
        assert "characters omitted" not in turn.text


def test_a_pinned_oversize_turn_is_never_pruned():
    """AR-04 is a count, and this is the case where a prune could quietly break it."""
    session = Session(agent_id="a", window=1, output_reserve=0)
    ground_rule = "NEVER " + "x" * 30_000
    session.pin(ground_rule)
    session.append_text("user", ground_rule, tier=1, pinned=True)
    session.append_text("assistant", "ordinary history", tier=2)
    session.window = int(session.used_tokens / 0.90)
    assert session.band in (Band.CRITICAL, Band.OVERFLOW)
    compact(session, target=0.4)
    assert any(turn.text == ground_rule for turn in session.turns), \
        "the pinned text must survive verbatim, however large it is"
    assert ground_rule in session.pinned


def test_a_short_session_is_never_pruned_at_all():
    """Every turn is one of the newest few, so a short session is left entirely alone.

    The turn is still *evicted* if the session needs the headroom — that is the honest outcome and
    the eviction manifest says so. What is asserted here is that the pruner did not touch it: only the
    marker would tell the model its middle was cut, and a session too short to have history behind it
    is not where that should happen.
    """
    session = Session(agent_id="a", window=1, output_reserve=0)
    session.append_text("tool", "HEAD" + "x" * 40_000 + "TAIL", tier=3)
    session.append_text("assistant", "done", tier=2)
    session.window = int(session.used_tokens / 0.97)
    assert session.band is Band.OVERFLOW
    session.output_reserve = 0
    result = compact(session, target=0.3)
    assert result.pruned_turns == 0
    assert all("characters omitted" not in turn.text for turn in session.turns)


def test_a_marker_bearing_oversize_turn_is_not_pruned():
    """A constraint can arrive through a handoff without having been pinned on this session."""
    session = Session(agent_id="a", window=1, output_reserve=0)
    rule = "SECURITY: " + "x" * 30_000
    session.append_text("tool", rule, tier=3)
    # A separate, prunable result proves the compaction ran rather than returning early.
    session.append_text("tool", "HEAD" + "x" * 30_000 + "TAIL", tier=3)
    for index in range(6):
        session.append_text("assistant", f"ordinary {index}", tier=2)
    session.window = int(session.used_tokens / 0.90)
    result = compact(session, target=0.4)
    assert result.pruned_turns >= 1
    assert any(turn.text == rule for turn in session.turns)


def test_the_pinned_count_is_preserved_by_a_pruning_compaction():
    session = _oversize_session()
    session.pin("NEVER log tokens")
    session.pin("MUST NOT expose PHI")
    result = compact(session, target=0.4)
    assert result.pinned_after == result.pinned_before == 2
    assert len(session.pinned) == 2


def test_a_reverted_compaction_undoes_the_prune_as_well():
    """A revert that left a truncation behind would be a silent loss with no record of it."""
    session = _oversize_session()
    session.pin("NEVER log tokens")
    original = [turn.text for turn in session.turns]
    assert session.turns[1].text.startswith("HEAD")

    import engine.context.compaction as compaction_mod

    real_evict = compaction_mod._evict

    def evict_and_strip(sess, **kwargs):
        removed = real_evict(sess, **kwargs)
        sess.pinned = []          # simulate a compaction that lost a constraint
        return removed

    compaction_mod._evict = evict_and_strip
    try:
        result = compact(session)
    finally:
        compaction_mod._evict = real_evict

    assert result.reverted is True
    assert [turn.text for turn in session.turns] == original, \
        "a revert must restore the pruned text too, or the session is silently truncated"


def test_a_pruning_compaction_reports_the_prune_separately_from_the_eviction():
    session = _oversize_session()
    payload = compact(session, target=0.4).as_dict()
    assert payload["pruned_turns"] >= 1
    assert payload["pruned_tokens"] > 0
    assert payload["reverted"] is False
    assert "pruned head+tail" in payload["note"]


def test_a_healthy_or_warning_session_is_never_pruned():
    """Pruning early would discard material the session may still use."""
    session = Session(agent_id="a", window=1, output_reserve=0)
    session.append_text("tool", "x" * 40_000, tier=3)
    session.window = int(session.used_tokens / 0.50)
    assert session.band is Band.HEALTHY
    result = compact(session)
    assert result.pruned_turns == 0
    assert len(session.turns[0].text) == 40_000
    assert result.action is CompactionAction.NONE
