#!/usr/bin/env python3
"""Phase 12 tests — prefix caching.

Prompt caching is billed, invisible and easy to lose: the request still succeeds, the answer is still
right, and the only symptom is a larger bill. So the tests here guard three things that fail silently:

1. **The cache is read.** Every dialect that reports cache tokens is parsed, and a dialect that
   reports nothing stays `None` rather than becoming a confident 0% hit rate.
2. **The saving is real arithmetic.** Cached input is billed at the provider's cached rate, and the
   saving is computed from a counterfactual rather than asserted.
3. **The prefix is stable.** This is the one that matters most: a volatile byte in the middle of a
   28KB prompt destroys the cache for everything after it, and nothing about the output changes.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.cache import (
    ShapeTracker, capture_shape, compare_shapes, estimate_tokens, normalize_schemas, schema_costs,
)
from engine.config import load
from engine.gateway import Gateway
from engine.library import resolve
from engine.prompts import PromptBuilder, TaskContext
from engine.providers.anthropic import AnthropicProvider
from engine.providers.base import Usage, cache_tokens_from
from engine.providers.openai import OpenAICompatibleProvider
from engine.skills import FilesystemSkillSource


@pytest.fixture(scope="module")
def config():
    return load()


@pytest.fixture(scope="module")
def source():
    return FilesystemSkillSource(resolve())


# ── reading the cache from each dialect ──────────────────────────────────────


def test_deepseek_style_cache_counters_are_read():
    hit, miss = cache_tokens_from({"prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100})
    assert (hit, miss) == (900, 100)


def test_openai_nested_cached_tokens_are_read_and_the_miss_is_derived():
    """OpenAI reports only a hit count, so the miss comes from `prompt_tokens - cached`."""
    hit, miss = cache_tokens_from(
        {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 768}})
    assert hit == 768 and miss == 232


def test_an_unreported_cache_stays_none_rather_than_zero():
    """The whole honesty rule: silence must not render as a 0% hit rate."""
    hit, miss = cache_tokens_from({"prompt_tokens": 1000})
    assert hit is None and miss is None
    usage = Usage(prompt_tokens=1000, completion_tokens=10)
    assert usage.cache_reported is False
    assert usage.cache_hit_rate is None


def test_the_openai_adapter_parses_cache_end_to_end():
    usage = OpenAICompatibleProvider._parse_usage(
        {"prompt_tokens": 1000, "completion_tokens": 5,
         "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200})
    assert usage.cache_hit_tokens == 800
    assert usage.cache_hit_rate == pytest.approx(0.8)


def test_the_anthropic_adapter_reads_the_cache_read_and_write_separately():
    usage = AnthropicProvider._parse_usage(
        {"input_tokens": 1000, "output_tokens": 5,
         "cache_read_input_tokens": 640, "cache_creation_input_tokens": 360})
    assert usage.cache_hit_tokens == 640
    # A write is not a hit and is not a miss; it is billed separately and kept distinct.
    assert usage.cache_write_tokens == 360


def test_a_hit_rate_of_zero_is_reported_when_a_provider_says_so():
    """Explicitly reported totals of zero are a fact, unlike an absent report."""
    usage = Usage(prompt_tokens=1000, completion_tokens=1,
                  cache_hit_tokens=0, cache_miss_tokens=1000)
    assert usage.cache_reported is True
    assert usage.cache_hit_rate == 0.0


# ── the cost is cache-aware ──────────────────────────────────────────────────


def make_gateway(config):
    return Gateway(config, {}, None)


def test_a_cache_hit_costs_less_than_a_miss(config):
    gateway = make_gateway(config)
    cached = gateway.compute_cost("deepseek", "deepseek-chat", Usage(
        prompt_tokens=100_000, completion_tokens=1_000,
        cache_hit_tokens=90_000, cache_miss_tokens=10_000))
    uncached = gateway.compute_cost("deepseek", "deepseek-chat", Usage(
        prompt_tokens=100_000, completion_tokens=1_000))
    assert cached.usd < uncached.usd
    assert cached.usd == pytest.approx(uncached.usd * 0.2, rel=0.05), \
        "a 90% hit on a 10× cached discount should be roughly a fifth of the price"


def test_an_unreported_cache_is_not_penalised(config):
    """An engine that reports no cache must cost exactly what it did before caching existed."""
    gateway = make_gateway(config)
    cost = gateway.compute_cost("deepseek", "deepseek-chat", Usage(
        prompt_tokens=100_000, completion_tokens=1_000))
    assert cost.usd == cost.uncached_usd
    assert cost.cache_saving_usd == 0.0


def test_the_saving_is_computed_from_a_counterfactual(config):
    gateway = make_gateway(config)
    cost = gateway.compute_cost("deepseek", "deepseek-chat", Usage(
        prompt_tokens=100_000, completion_tokens=1_000,
        cache_hit_tokens=90_000, cache_miss_tokens=10_000))
    assert cost.uncached_usd is not None
    assert cost.cache_saving_usd == pytest.approx(cost.uncached_usd - cost.usd)


def test_an_unknown_cost_has_no_invented_saving(config):
    """A saving cannot be claimed for a call whose cost is not even known."""
    gateway = make_gateway(config)
    cost = gateway.compute_cost("deepseek", "deepseek-chat", Usage())
    assert cost.source == "unknown"
    assert cost.cache_saving_usd is None


def test_a_local_model_is_free_and_needs_no_cache(config):
    gateway = make_gateway(config)
    cost = gateway.compute_cost("ollama", "qwen2.5-coder:7b", Usage(
        prompt_tokens=1000, completion_tokens=10, reported_cost_usd=0.0))
    assert cost.source in ("measured", "free")
    assert cost.usd == 0.0


# ── the shape explains a miss ────────────────────────────────────────────────


def test_an_unchanged_prefix_reports_no_change():
    tracker = ShapeTracker()
    tracker.observe(system="s", schemas=[{"name": "read"}], usage=Usage())
    diag = tracker.observe(system="s", schemas=[{"name": "read"}], usage=Usage())
    assert diag.prefix_changed is False
    assert diag.prefix_change_reasons == []


def test_a_changed_system_prompt_is_named():
    tracker = ShapeTracker()
    tracker.observe(system="one", usage=Usage())
    diag = tracker.observe(system="two", usage=Usage())
    assert diag.prefix_changed is True
    assert "system" in diag.prefix_change_reasons


def test_changed_tools_are_named():
    tracker = ShapeTracker()
    tracker.observe(system="s", schemas=[{"name": "read"}], usage=Usage())
    diag = tracker.observe(system="s", schemas=[{"name": "read"}, {"name": "write"}], usage=Usage())
    assert "tools" in diag.prefix_change_reasons


def test_reordered_tools_do_not_look_like_a_change():
    """The subtle one: the same tools in a different order are different bytes to the provider."""
    a = capture_shape(schemas=[{"name": "read"}, {"name": "write"}])
    b = capture_shape(schemas=[{"name": "write"}, {"name": "read"}])
    assert a.tools_hash == b.tools_hash
    assert normalize_schemas([{"name": "b"}, {"name": "a"}])[0]["name"] == "a"


def test_a_local_only_edit_is_not_reported_as_a_cache_change():
    """A rewrite-version bump with no named reason never reached the provider."""
    tracker = ShapeTracker()
    tracker.observe(system="s", log_rewrite_version=0, usage=Usage())
    diag = tracker.observe(system="s", log_rewrite_version=1, usage=Usage())
    assert diag.prefix_changed is False


def test_a_named_content_rewrite_is_reported():
    """When a compaction *did* reach the provider, that is the reason for the miss."""
    tracker = ShapeTracker()
    tracker.observe(system="s", log_rewrite_version=0, usage=Usage())
    diag = tracker.observe(system="s", log_rewrite_version=1, usage=Usage(),
                           content_rewrite_reasons=["compact_auto"])
    assert "compact_auto" in diag.prefix_change_reasons


def test_the_hit_rate_comes_from_the_provider_not_from_our_own_comparison():
    previous = capture_shape(system="s")
    current = capture_shape(system="s")
    diag = compare_shapes(previous, current, usage=Usage(cache_hit_tokens=80, cache_miss_tokens=20))
    assert diag.cache_hit_rate == pytest.approx(0.8)
    assert diag.prefix_changed is False, "a hit does not mean nothing changed; both facts are kept"


def test_a_run_summary_is_none_when_no_provider_reported_a_cache():
    tracker = ShapeTracker()
    tracker.observe(system="s", usage=Usage(prompt_tokens=10, completion_tokens=1))
    summary = tracker.summary()
    assert summary["cache_reported"] is False
    assert summary["cache_hit_rate"] is None


def test_tool_schema_costs_attribute_the_prefix():
    costs = schema_costs([{"name": "read", "description": "x" * 400}])
    assert costs[0].name == "read"
    assert costs[0].tokens > 0
    assert estimate_tokens("") == 0


# ── the prefix is stable, and that is the point ──────────────────────────────


def _stable_prefix_ratio(source, skill: str) -> float:
    """How much of the prompt is byte-identical across two different tasks for one skill."""
    builder = PromptBuilder()
    bundle = source.load(skill)
    first = builder.node_prompt(bundle, TaskContext(node_id="a", instruction="Task one: create X"),
                                agent_name="A", agent_skill=skill)
    second = builder.node_prompt(bundle, TaskContext(node_id="b",
                                                     instruction="Task two: change Y entirely"),
                                 agent_name="A", agent_skill=skill)
    common = 0
    for left, right in zip(first.text, second.text):
        if left != right:
            break
        common += 1
    return common / max(1, len(first.text))


@pytest.mark.parametrize("skill", ["backend-developer", "system-architect", "code-reviewer"])
def test_the_prompt_prefix_is_stable_across_different_tasks(source, skill):
    """The regression that matters: a volatile byte near the top strands the whole SOP.

    Before the volatile intake block was moved behind the SOP, the stable prefix measured 6.2% —
    which is a guaranteed miss on every prompt, for every node, silently.
    """
    ratio = _stable_prefix_ratio(source, skill)
    assert ratio > 0.80, (
        f"{skill}: only {ratio:.1%} of the prompt is a stable prefix. A volatile block has been "
        "placed ahead of the SOP, so the cache cannot be reused across nodes."
    )


def test_the_intake_still_reaches_the_model(source):
    """Stability must not have been bought by dropping the task statement."""
    builder = PromptBuilder()
    bundle = source.load("backend-developer")
    prompt = builder.node_prompt(bundle, TaskContext(node_id="a", instruction="UNIQUE_TASK_MARKER"),
                                 agent_name="A", agent_skill="backend-developer")
    assert "UNIQUE_TASK_MARKER" in prompt.text
    assert "What did I receive?" in prompt.body


def test_the_attention_order_is_still_guardrails_then_body_then_contract(source):
    """The reorder must not have disturbed the zones the prompt design depends on."""
    builder = PromptBuilder()
    bundle = source.load("backend-developer")
    prompt = builder.node_prompt(bundle, TaskContext(node_id="a", instruction="x"),
                                 agent_name="A", agent_skill="backend-developer")
    text = prompt.text
    assert text.index(prompt.primacy) < text.index(prompt.body)
    assert text.index(prompt.body) < text.index(prompt.recency)


# ── the pinned prefix, and the invariant it enforces ─────────────────────────
#
# These guard the *design* property rather than one call site. The alignment lesson was learned once
# in the prompt body (6.2% → 90%) and then repeated one layer up, where the agent's own name sat at
# character 8 of the system prompt and cost a vote swarm 58% of its bill.


def test_the_system_prompt_is_identical_for_every_agent_working_a_skill(source):
    """The regression: the name opened the system prompt, so two voters diverged at character 8."""
    builder = PromptBuilder()
    bundle = source.load("code-reviewer")
    sana = builder.node_prompt(bundle, TaskContext(node_id="r", instruction="Review it"),
                               agent_name="Sana", agent_skill="code-reviewer")
    rita = builder.node_prompt(bundle, TaskContext(node_id="r", instruction="Review it"),
                               agent_name="Rita", agent_skill="code-reviewer")
    assert sana.system == rita.system
    assert "Sana" not in sana.system and "Rita" not in rita.system


def test_two_swarm_voters_share_almost_all_of_their_prompt(source):
    """The number that matters: a swarm should differ only in a short trailing identity."""
    builder = PromptBuilder()
    bundle = source.load("code-reviewer")
    sana = builder.node_prompt(bundle, TaskContext(node_id="r", instruction="Review the change"),
                               agent_name="Sana", agent_skill="code-reviewer")
    rita = builder.node_prompt(bundle, TaskContext(node_id="r", instruction="Review the change"),
                               agent_name="Rita", agent_skill="code-reviewer")
    common = 0
    for left, right in zip(sana.text, rita.text):
        if left != right:
            break
        common += 1
    ratio = common / len(sana.text)
    assert ratio > 0.95, (
        f"swarm voters share only {ratio:.1%} of their prompt. Something agent-specific has moved "
        "ahead of the shared procedure, so every voter re-pays for the whole SOP."
    )


def test_the_recency_zone_stays_small_so_it_cannot_dilute_the_prefix(source):
    """The contract must stay compact, because growing it dilutes the cacheable prefix.

    Measured, not stylistic. An attempt to fix a real trailer-completeness failure by pre-filling every
    criterion and checklist id into the schema grew this zone from ~2.8KB to ~5.6KB and dropped the
    stable-prefix ratio from 0.90/0.88/0.85 to 0.82/0.79/0.75 — below the floor asserted above, i.e. a
    cache regression bought with no proven benefit. The contract shapes the reply and must say what it
    needs; the *criteria themselves* belong in the completion block in the body, where every node reads
    them once. This pins the budget so the same mistake is a failing test next time rather than a
    silent cost increase.
    """
    builder = PromptBuilder()
    for skill in ("backend-developer", "system-architect", "code-reviewer"):
        bundle = source.load(skill)
        prompt = builder.node_prompt(bundle, TaskContext(node_id="a", instruction="x"),
                                     agent_name="A", agent_skill=skill)
        assert len(prompt.recency) < 3500, (
            f"{skill}: the recency zone is {len(prompt.recency)} chars. It is the last thing the model "
            "reads and it sits inside the cacheable prefix; keep it to the contract and its rules."
        )


def test_the_identity_is_still_stated(source):
    """Cache alignment must not have been bought by dropping who the agent is."""
    builder = PromptBuilder()
    bundle = source.load("code-reviewer")
    prompt = builder.node_prompt(bundle, TaskContext(node_id="r", instruction="x"),
                                 agent_name="Sana", agent_skill="code-reviewer")
    assert "Sana" in prompt.text
    assert prompt.text.index("Sana") > len(prompt.primacy), \
        "the name belongs after the shared procedure, not at the front"


def test_a_prefix_hashes_the_bytes_actually_sent():
    from engine.prefix import Prefix

    prefix = Prefix.for_skill(skill="s", system="sys", procedure="proc",
                              tools=[{"name": "read_file", "parameters": {}}])
    assert prefix.compose(tail="tail").startswith(prefix.text())
    assert prefix.chars == len(prefix.text())


def test_reordered_tools_hash_the_same():
    """The same tools in a different order are different bytes to a provider, and would miss."""
    from engine.prefix import Prefix

    build = lambda order: Prefix.for_skill(  # noqa: E731
        skill="s", system="sys", procedure="proc",
        tools=[{"name": n, "parameters": {}} for n in order])
    assert build(["read_file", "write_file"]).prefix_hash == \
        build(["write_file", "read_file"]).prefix_hash


def test_a_prefix_refuses_an_agent_name_in_the_system_prompt():
    """The specific mistake that cost a swarm 58%, refused loudly rather than tolerated."""
    from engine.prefix import Prefix, PrefixError

    with pytest.raises(PrefixError, match="agent name"):
        Prefix.for_skill(skill="s", system="You are Sana, operating as reviewer.",
                         procedure="proc", agent_name="Sana")


def test_a_prefix_diff_names_the_reason():
    """'The cache stopped working' is not actionable; 'the procedure changed' is."""
    from engine.prefix import Prefix

    base = Prefix.for_skill(skill="s", system="sys", procedure="proc")
    assert base.diff(None).reasons == ["cold_start"]
    changed = Prefix.for_skill(skill="s", system="sys", procedure="different")
    assert changed.diff(base).reasons == ["procedure"]


def test_an_unchanged_prefix_reports_no_change():
    from engine.prefix import Prefix

    a = Prefix.for_skill(skill="s", system="sys", procedure="proc")
    b = Prefix.for_skill(skill="s", system="sys", procedure="proc")
    assert a.diff(b).changed is False


def test_the_swarm_prompt_alignment_is_worth_the_measured_saving(source, config):
    """The claim, as arithmetic: a 3-voter swarm should cost well under the unaligned price."""
    from engine.gateway import Gateway
    from engine.providers.base import Usage

    gateway = Gateway(config, {}, None)
    builder = PromptBuilder()
    bundle = source.load("code-reviewer")
    sana = builder.node_prompt(bundle, TaskContext(node_id="r", instruction="Review it"),
                               agent_name="Sana", agent_skill="code-reviewer")
    rita = builder.node_prompt(bundle, TaskContext(node_id="r", instruction="Review it"),
                               agent_name="Rita", agent_skill="code-reviewer")
    common = 0
    for left, right in zip(sana.text, rita.text):
        if left != right:
            break
        common += 1

    total = 20_000
    shared = int(total * common / len(sana.text))
    unaligned = sum(gateway.compute_cost("deepseek", "deepseek-chat", Usage(
        prompt_tokens=total, completion_tokens=300)).usd for _ in range(3))
    aligned = gateway.compute_cost("deepseek", "deepseek-chat", Usage(
        prompt_tokens=total, completion_tokens=300)).usd
    for _ in range(2):
        aligned += gateway.compute_cost("deepseek", "deepseek-chat", Usage(
            prompt_tokens=total, completion_tokens=300,
            cache_hit_tokens=shared, cache_miss_tokens=total - shared)).usd
    assert aligned < unaligned * 0.6, (
        f"aligned ${aligned:.5f} vs unaligned ${unaligned:.5f} — the swarm is not sharing enough "
        "to get the cache discount"
    )


# ── the prefix is PINNED for a run, not merely recomputed ────────────────────
#
# `prefix.py` makes a prefix computed correctly; these guard that it is *held*. The gap they close was
# measured: the executor loads the skill bundle on every node, and the skill source re-parses when a
# SKILL.md's hash changes — so an edit mid-run silently changed the prefix for every later node, and
# the only symptom was a larger bill.


def test_a_pinned_prefix_is_the_same_object_on_re_ask():
    """Re-asking must be free, and must not be able to drift."""
    from engine.pinning import PrefixPins

    pins = PrefixPins(run_id="r")
    first = pins.get_or_pin(skill="s", system="sys", procedure="sop", tools=[])
    second = pins.get_or_pin(skill="s", system="sys", procedure="sop", tools=[])
    assert first is second


def test_an_edit_mid_run_is_detected_with_both_hashes():
    """'The cache stopped working' is not actionable; two hashes and a field name are."""
    from engine.pinning import PrefixPins

    pins = PrefixPins(run_id="r")
    pinned = pins.get_or_pin(skill="s", system="sys", procedure="sop v1", tools=[])
    drift = pins.check(skill="s", system="sys", procedure="sop v2", tools=[])
    assert drift.changed
    assert drift.reasons == ["procedure"]
    assert drift.pinned_hash == pinned.prefix_hash
    assert drift.current_hash != pinned.prefix_hash
    assert "procedure" in drift.reason and drift.pinned_hash in drift.reason


def test_the_pinned_bytes_survive_a_detected_edit():
    """Detection is not permission: the run keeps sending what it pinned until told otherwise."""
    from engine.pinning import PrefixPins

    pins = PrefixPins(run_id="r")
    pinned = pins.get_or_pin(skill="s", system="sys", procedure="sop v1", tools=[])
    pins.check(skill="s", system="sys", procedure="sop v2", tools=[])
    assert pins.pinned(skill="s", tools=[]) is pinned
    assert pins.pinned(skill="s", tools=[]).procedure == "sop v1"


def test_an_unchanged_prefix_reports_no_drift():
    from engine.pinning import PrefixPins

    pins = PrefixPins(run_id="r")
    pins.get_or_pin(skill="s", system="sys", procedure="sop", tools=[])
    assert pins.check(skill="s", system="sys", procedure="sop", tools=[]).changed is False


def test_a_change_can_be_accepted_deliberately():
    from engine.pinning import PrefixPins

    pins = PrefixPins(run_id="r")
    pins.get_or_pin(skill="s", system="sys", procedure="sop v1", tools=[])
    updated = pins.accept_change(skill="s", system="sys", procedure="sop v2", tools=[])
    assert updated.procedure == "sop v2"
    assert pins.check(skill="s", system="sys", procedure="sop v2", tools=[]).changed is False


def test_strict_mode_refuses_instead_of_reporting():
    from engine.pinning import PrefixPinError, PrefixPins

    pins = PrefixPins(run_id="r", strict=True)
    pins.get_or_pin(skill="s", system="sys", procedure="sop", tools=[])
    with pytest.raises(PrefixPinError, match="changed"):
        pins.check(skill="s", system="sys", procedure="different", tools=[])


def test_the_pin_key_covers_the_skill_and_the_tool_names():
    """Keyed on identity, not on the text being pinned — otherwise a pin could never be matched."""
    from engine.pinning import PrefixPins

    key = PrefixPins.key_for("code-reviewer", [{"name": "write_file"}, {"name": "read_file"}])
    assert key == "code-reviewer|read_file,write_file", "tool names are sorted, so order is not identity"
    assert PrefixPins.key_for("other-skill", [{"name": "read_file"}]) != key


def test_a_different_skill_is_a_different_pin():
    from engine.pinning import PrefixPins

    pins = PrefixPins(run_id="r")
    a = pins.get_or_pin(skill="a", system="sys", procedure="p", tools=[])
    b = pins.get_or_pin(skill="b", system="sys", procedure="p", tools=[])
    assert a is not b
    assert pins.summary()["pinned"].keys() == {"a|", "b|"}


def test_the_summary_carries_the_drift_and_the_provider_cache_together():
    """They are only useful together: a hit rate explains the bill, the drift explains the hit rate."""
    from engine.pinning import PrefixPins

    pins = PrefixPins(run_id="r1")
    pins.get_or_pin(skill="s", system="sys", procedure="v1", tools=[])
    pins.check(skill="s", system="sys", procedure="v2", tools=[])
    summary = pins.summary(cache={"cache_hit_rate": 0.4, "cache_reported": True})
    assert summary["drifted"] is True
    assert summary["run_id"] == "r1"
    assert summary["cache"]["cache_hit_rate"] == 0.4
    assert summary["drift"][0]["reasons"] == ["procedure"]


def test_drift_is_recorded_across_a_run():
    from engine.pinning import PrefixPins

    pins = PrefixPins(run_id="r")
    pins.get_or_pin(skill="s", system="sys", procedure="v1", tools=[])
    pins.check(skill="s", system="sys", procedure="v2", tools=[])
    pins.check(skill="s", system="sys", procedure="v3", tools=[])
    assert len(pins.drift()) == 2, "a second edit is a second event, not a replacement"


# ── native window enrichment for an OpenAI-compatible host ───────────────────


def test_an_openai_listing_can_be_enriched_with_a_real_window():
    """A host may serve the OpenAI model list *and* a native `/api/show` that reports the true window.

    Ollama's cloud is exactly that: `https://ollama.com/v1` lists models with ids only, while
    `https://ollama.com/api/show` reports the context length. Without asking, every model lands with
    an unknown window and hiring an agent on one is refused — a working endpoint described as
    unbindable.
    """
    from engine.catalog import _looks_like_ollama, _origin_of

    assert _looks_like_ollama("https://ollama.com/v1")
    assert _looks_like_ollama("http://localhost:11434")
    # A well-known cloud host that is not Ollama is skipped: N wasted round trips per refresh.
    assert not _looks_like_ollama("https://api.openai.com/v1")
    assert not _looks_like_ollama("https://api.groq.com/openai/v1")
    # The native endpoint is at the host root, not under the API prefix.
    assert _origin_of("https://ollama.com/v1") == "https://ollama.com"
    assert _origin_of("http://localhost:11434") == "http://localhost:11434"


def test_the_native_window_probe_can_be_turned_off():
    """Configurable, because on a metered or slow host N extra requests per refresh may not be worth
    real windows."""
    from engine.catalog import ModelCatalog
    from engine.config import Config, ProviderConfig

    spec = ProviderConfig(id="p", kind="openai", base_url="https://ollama.com/v1")
    cfg = Config(providers={"p": spec}, known_models={},
                 catalog={"probe_native_windows": False})
    assert ModelCatalog(cfg, {}).probe_native_windows is False


def test_a_configured_only_provider_is_not_labelled_twice():
    """`configured+configured` was the doubled label: `configured` already means the list came from
    configuration, so the `+configured` suffix must not be applied to it again. A status a person
    learns to ignore is one they will miss the real state behind."""
    from engine.catalog import ModelCatalog
    from engine.config import Config, ProviderConfig

    spec = ProviderConfig(id="p", kind="openai", base_url="https://api.openai.com/v1")
    # No adapter built, so discovery never runs and the curated table supplies the list.
    catalog = ModelCatalog(Config(providers={"p": spec}, known_models={}, catalog={}), {})
    catalog.list_models(provider_id="p", refresh=True)
    status = catalog.status()["p"]["status"]
    assert status == "configured", status
    assert "+configured+configured" not in status


def test_a_failed_probe_keeps_both_facts():
    """A down provider genuinely has two: the probe failed, and the list came from configuration."""
    from engine.catalog import ModelCatalog
    from engine.config import Config, ProviderConfig
    from engine.providers.registry import build_provider

    spec = ProviderConfig(id="dead", kind="openai", base_url="http://127.0.0.1:9/v1",
                          api_key="k" * 20)
    catalog = ModelCatalog(Config(providers={"dead": spec}, known_models={},
                                  catalog={"offline_fallback": True}),
                           {"dead": build_provider(spec)})
    catalog.list_models(provider_id="dead", refresh=True)
    status = catalog.status()["dead"]["status"]
    assert status == "down+configured", status
