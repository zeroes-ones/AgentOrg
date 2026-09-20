#!/usr/bin/env python3
"""Phase 13 tests — fan-out: split one job across N agents.

This is the *second* swarm primitive, and the distinction is the whole point of the file:

- a **vote** (`BindingPolicy.SWARM`) has N agents answer one question and takes the majority;
- a **fan-out** gives N agents N different pieces of one job and keeps every result.

Conflating them would lose a guarantee in one direction or the other, so the tests below assert both
that fan-out spreads work and that it does not quietly become a vote.

The failure modes worth guarding are the ones that cost real money to discover at runtime: a template
missing `{{item}}` sends N identical prompts, and two items that expand alike duplicate a call.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load
from engine.executor import ExecutorContext, NodeExecutor
from engine.fanout import (
    MAX_ITEMS, FanoutError, PLACEHOLDER, plan_fanout, run_fanout,
)
from engine.gateway import Gateway
from engine.library import resolve
from engine.org import default_company
from engine.planner import emit_safe_yaml
from engine.providers.base import ChatResponse, FinishReason, Usage
from engine.providers.fake import FakeProvider
from engine.skills import FilesystemSkillSource
from engine.tokens import TokenEstimator


@pytest.fixture(scope="module")
def config():
    return load()


@pytest.fixture(scope="module")
def source():
    return FilesystemSkillSource(resolve())


# ── the plan refuses what would waste money ──────────────────────────────────


def test_a_template_without_the_placeholder_is_refused():
    """Otherwise every subagent gets the same prompt: N times the cost for one answer."""
    with pytest.raises(FanoutError, match=PLACEHOLDER):
        plan_fanout("Review the file", items=["a", "b"])


def test_a_fanout_of_one_item_is_refused():
    """One item is a sequential call wearing a swarm label."""
    with pytest.raises(FanoutError, match="sequential"):
        plan_fanout("Review {{item}}", items=["only"])


def test_two_items_expanding_alike_are_refused_with_the_collision_named():
    with pytest.raises(FanoutError, match="0 and 1"):
        plan_fanout("Review {{item}}", items=["a", "a"])


def test_the_item_ceiling_is_enforced():
    with pytest.raises(FanoutError, match=str(MAX_ITEMS)):
        plan_fanout("Review {{item}}", items=[f"f{i}" for i in range(MAX_ITEMS + 1)])


def test_an_empty_item_is_refused():
    with pytest.raises(FanoutError, match="empty"):
        plan_fanout("Review {{item}}", items=["a", "  "])


def test_a_resume_only_fanout_is_allowed():
    """Resuming existing subagents is a legitimate fan-out of zero new items."""
    plan = plan_fanout("continue", [], resume=["ag_1", "ag_2"])
    assert len(plan) == 0


# ── the plan expands correctly ───────────────────────────────────────────────


def test_each_item_gets_its_own_prompt():
    plan = plan_fanout("Review {{item}} for regressions.", items=["src/a.ts", "src/b.ts"])
    assert [i.prompt for i in plan.items] == [
        "Review src/a.ts for regressions.",
        "Review src/b.ts for regressions.",
    ]


def test_batching_bounds_the_burst():
    plan = plan_fanout("Review {{item}}", items=[f"f{i}" for i in range(7)])
    assert [len(b) for b in plan.batches(max_parallel=3)] == [3, 3, 1]


def test_the_largest_batch_never_exceeds_the_limit():
    plan = plan_fanout("Review {{item}}", items=[f"f{i}" for i in range(10)])
    for max_parallel in (1, 2, 3, 4, 16):
        assert max(len(b) for b in plan.batches(max_parallel=max_parallel)) <= max_parallel


# ── running: order, errors, distribution ─────────────────────────────────────


def test_results_are_indexed_by_item_regardless_of_completion_order():
    plan = plan_fanout("Do {{item}}", items=["a", "b", "c"])

    def run_out_of_order(item, agent_id):
        return (f"out-{item.item}", "", 1)

    run_fanout(plan, run_out_of_order, agents=["ag_1"])
    assert [i.item for i in plan.items] == ["a", "b", "c"]
    assert [i.output for i in plan.items] == ["out-a", "out-b", "out-c"]


def test_one_failing_item_does_not_lose_the_others():
    """A fan-out is for independent work, so a single failure must not discard 19 good results."""
    plan = plan_fanout("Do {{item}}", items=["a", "b", "c"])

    def explode_on_b(item, agent_id):
        if item.item == "b":
            raise RuntimeError("provider exploded")
        return (f"out-{item.item}", "", 1)

    run_fanout(plan, explode_on_b, agents=["ag_1"])
    summary = plan.summary()
    assert summary["succeeded"] == 2
    assert summary["failed"] == 1
    assert summary["failures"][0]["item"] == "b"
    assert "exploded" in summary["failures"][0]["error"]


def test_a_reported_error_is_distinct_from_an_exception():
    """A subagent that ran and failed reports it; one that crashed raises. Both are failures."""
    plan = plan_fanout("Do {{item}}", items=["a", "b"])
    run_fanout(plan, lambda item, agent: ("" if item.item == "b" else "ok",
                                          "" if item.item != "b" else "the model refused", 1),
               agents=["ag_1"])
    assert plan.summary()["failed"] == 1
    assert plan.summary()["failures"][0]["error"] == "the model refused"


def test_work_is_distributed_across_the_pool():
    plan = plan_fanout("Do {{item}}", items=[f"i{n}" for n in range(4)])
    run_fanout(plan, lambda item, agent: ("ok", "", 0), agents=["ag_1", "ag_2"])
    used = {i.agent_id for i in plan.items}
    assert used == {"ag_1", "ag_2"}, "work must spread, not pile on the first agent"


def test_a_fanout_with_no_agents_is_refused():
    plan = plan_fanout("Do {{item}}", items=["a", "b"])
    with pytest.raises(FanoutError, match="agent"):
        run_fanout(plan, lambda i, a: ("", "", 0), agents=[])


# ── fan-out reaches execution, and is not a vote ─────────────────────────────

class RecordingProvider(FakeProvider):
    """Records every prompt it is asked, so a test can prove what each subagent received."""

    def __init__(self, criteria, checklist, **kwargs):
        super().__init__(**kwargs)
        self.criteria = criteria
        self.checklist = checklist
        self.prompts: list[str] = []

    def complete(self, request):
        self.prompts.append("\n".join(m.text for m in request.messages))
        payload = {
            "status": "done", "verdict": "pass", "summary": "reviewed",
            "criteria_satisfied": [{"criterion": c, "satisfied": True, "evidence": "src/x:1"}
                                   for c in self.criteria],
            "checklist": [{"id": i, "status": "PASS", "evidence": "e"} for i in self.checklist],
            "artifacts": [{"type": "review-report", "path": "artifacts/r.md", "content": "ok"}],
        }
        return ChatResponse(text=json.dumps(payload),
                            usage=Usage(prompt_tokens=5, completion_tokens=5,
                                        reported_cost_usd=0.0),
                            model=request.model, provider_id=self.provider_id,
                            finish_reason=FinishReason.STOP)


def make_fanout_executor(config, source, manifest_nodes, *, parallel=None):
    workspace = pathlib.Path(tempfile.mkdtemp())
    manifest = {"name": "wf", "version": "1.0.0", "start": manifest_nodes[0]["id"],
                "nodes": manifest_nodes}
    (workspace / "wf.yaml").write_text(emit_safe_yaml(manifest))
    bundle = source.load("code-reviewer")
    provider = RecordingProvider(list(bundle.contract.criteria), bundle.checklist_ids(),
                                 provider_id="fake")
    org = default_company(provider="fake", model="m", context_window=32768)
    context = ExecutorContext(
        org=org, gateway=Gateway(config, {"fake": provider}, estimator=TokenEstimator()),
        skills=source, workspace=workspace, config=config, run_id="r", workflow="wf",
        manifest_path=workspace / "wf.yaml")
    if parallel is not None:
        config.executor.fanout_max_parallel = parallel
    return NodeExecutor(context), provider


def test_a_node_declaring_fanout_splits_across_items(config, source):
    executor, provider = make_fanout_executor(config, source, [{
        "id": "reviewall", "skill": "code-reviewer", "phase": "REVIEW",
        "fanout": "Review {{item}} for regressions.",
        "items": ["src/a.ts", "src/b.ts", "src/c.ts"],
        "inputs": [], "outputs": ["review-report"],
    }])
    state = {"nodes": {"reviewall": {"status": "pending"}}, "artifacts": {},
             "budget": {"steps_used": 0, "iterations": {}}}
    result = executor.execute_node("reviewall", state, {})

    assert result["status"] == "done"
    assert result["fanout"]["count"] == 3
    assert len(provider.prompts) == 3, "one call per item"
    for item in ("src/a.ts", "src/b.ts", "src/c.ts"):
        assert any(f"Review {item} for regressions" in p for p in provider.prompts), \
            f"{item} never reached a subagent"


def test_a_partial_fanout_is_not_reported_as_done(config, source):
    """Reporting a half-reviewed change set as done is the dishonesty the contract exists to stop."""
    executor, provider = make_fanout_executor(config, source, [{
        "id": "reviewall", "skill": "code-reviewer", "phase": "REVIEW",
        "fanout": "Review {{item}}.", "items": ["src/a.ts", "src/b.ts"],
        "inputs": [], "outputs": ["review-report"],
    }])

    original = provider.complete
    calls = {"n": 0}

    def fail_the_second(request):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the second subagent died")
        return original(request)

    provider.complete = fail_the_second  # type: ignore[method-assign]
    state = {"nodes": {"reviewall": {"status": "pending"}}, "artifacts": {},
             "budget": {"steps_used": 0, "iterations": {}}}
    result = executor.execute_node("reviewall", state, {})

    assert result["status"] == "needs_review"
    assert result["fanout"]["complete"] is False
    assert result["fanout"]["failed"] == 1
    assert result["open_questions"], "a failed item must be surfaced, not swallowed"


def test_a_fanout_is_not_a_vote(config, source):
    """The two primitives must not collapse into each other: this one keeps every result."""
    executor, provider = make_fanout_executor(config, source, [{
        "id": "reviewall", "skill": "code-reviewer", "phase": "REVIEW",
        "fanout": "Review {{item}}.", "items": ["a", "b", "c", "d"],
        "inputs": [], "outputs": ["review-report"],
    }])
    state = {"nodes": {"reviewall": {"status": "pending"}}, "artifacts": {},
             "budget": {"steps_used": 0, "iterations": {}}}
    result = executor.execute_node("reviewall", state, {})
    # A vote would report a tally and a quorum; a fan-out reports every item's outcome.
    assert "swarm" not in result
    assert result["fanout"]["succeeded"] == 4
    assert len(result["fanout"]["items"]) == 4


def test_an_invalid_fanout_is_refused_before_any_call(config, source):
    """A malformed fan-out is a manifest defect, and discovering it at runtime costs N calls."""
    executor, provider = make_fanout_executor(config, source, [{
        "id": "reviewall", "skill": "code-reviewer", "phase": "REVIEW",
        "fanout": "Review the file, no placeholder here.",
        "items": ["a", "b"], "inputs": [], "outputs": ["review-report"],
    }])
    state = {"nodes": {"reviewall": {"status": "pending"}}, "artifacts": {},
             "budget": {"steps_used": 0, "iterations": {}}}
    with pytest.raises(Exception, match="invalid fan-out"):
        executor.execute_node("reviewall", state, {})
    assert provider.prompts == [], "nothing may be called when the plan is invalid"


def test_the_fanout_parallel_knob_is_real(config):
    """The regression: a knob read via getattr on a section that did not exist is always default."""
    assert hasattr(config, "executor")
    assert config.executor.fanout_max_parallel == 4


# ── the queue adapts to a provider that pushes back ──────────────────────────


def test_a_rate_limit_halves_the_limit_and_requeues_the_item():
    """A fixed batch would send the next four straight into the same 429."""
    from engine.fanout import run_fanout

    events: list[tuple[str, dict]] = []
    plan = plan_fanout("Do {{item}}", items=[f"i{n}" for n in range(8)])
    calls = {"n": 0}

    def runner(item, agent_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 too many requests")
        return (f"out-{item.item}", "", 1)

    run_fanout(plan, runner, agents=["ag_1", "ag_2"], max_parallel=4,
               on_event=lambda k, p: events.append((k, p)))

    backpressure = [p for k, p in events if k == "fanout.backpressure"]
    assert backpressure, "a 429 must be reported as backpressure"
    assert backpressure[0]["new_limit"] < backpressure[0]["previous_limit"]
    assert backpressure[0]["requeued"] == 0, "the refused item is retried, not failed"


def test_a_rate_limited_item_is_retried_rather_than_failed():
    """A limit is the provider's state, not the item's fault."""
    from engine.fanout import run_fanout

    plan = plan_fanout("Do {{item}}", items=["a", "b"])
    calls = {"n": 0}

    def runner(item, agent_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limit reached")
        return ("ok", "", 1)

    run_fanout(plan, runner, agents=["ag_1"], max_parallel=2)
    assert plan.summary()["succeeded"] == 2, "the throttled item must still complete"


def test_the_limit_recovers_one_slot_at_a_time():
    """Jumping straight back to full concurrency reproduces the 429."""
    from engine.fanout import run_fanout

    events: list[tuple[str, dict]] = []
    plan = plan_fanout("Do {{item}}", items=[f"i{n}" for n in range(12)])
    calls = {"n": 0}

    def runner(item, agent_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429")
        return ("ok", "", 1)

    run_fanout(plan, runner, agents=["ag_1"], max_parallel=4,
               on_event=lambda k, p: events.append((k, p)))
    recovered = [p["new_limit"] for k, p in events if k == "fanout.recovered"]
    assert recovered, "the limit must recover after sustained success"
    # One step at a time: never a jump back to the ceiling in a single recovery.
    for previous, current in zip([2] + recovered, recovered):
        assert current - previous <= 1, f"recovery jumped from {previous} to {current}"


def test_a_permanently_throttled_item_gives_up_rather_than_looping():
    """An item that can never run must surface, not retry forever."""
    from engine.fanout import RATE_LIMIT_MAX_REQUEUES, run_fanout

    plan = plan_fanout("Do {{item}}", items=["a", "b"])
    run_fanout(plan, lambda i, a: (_ for _ in ()).throw(RuntimeError("429 rate limit")),
               agents=["ag_1"], max_parallel=2)
    summary = plan.summary()
    assert summary["failed"] == 2
    assert str(RATE_LIMIT_MAX_REQUEUES) in summary["failures"][0]["error"]


def test_a_rate_limit_message_in_a_returned_error_is_prose_not_a_signal():
    """F1, the load-bearing case: a *returned* error string is the item's own summary.

    The executor reports a non-done item as `(..., summary, ...)` (`executor.py:1695-1697`), so the
    old substring test read the **model's prose** as a provider limit. A reviewer writing "line 429
    changed" or "no rate limit issue found" was re-queued and re-executed in full, after the first
    attempt had already charged the budget, appended the session turn and persisted its artifacts —
    and the second run reported success, so the double spend was invisible.
    """
    from engine.fanout import run_fanout

    plan = plan_fanout("Do {{item}}", items=["a", "b", "c"])
    calls = {"n": 0}

    def runner(item, agent_id):
        calls["n"] += 1
        return "", f"reviewed {item.item}: line 429 changed; no rate limit issue found", 5

    run_fanout(plan, runner, agents=["ag_1"], max_parallel=3)
    assert calls["n"] == 3, "each item must be attempted exactly once"
    assert plan.summary()["failed"] == 3, "prose is a failure, not a provider limit"


def test_a_typed_limit_flag_on_a_returned_outcome_is_honoured():
    """The typed channel: a caller that knows the provider refused says so, without raising."""
    from engine.fanout import ItemOutcome, run_fanout

    plan = plan_fanout("Do {{item}}", items=["a", "b"])
    calls = {"n": 0}

    def runner(item, agent_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return ItemOutcome(error="the provider refused the call", limit_hit=True)
        return ItemOutcome(output="ok", tokens=2)

    run_fanout(plan, runner, agents=["ag_1"], max_parallel=2)
    assert plan.summary()["succeeded"] == 2, "a typed limit is retried, not reported as a failure"
    assert calls["n"] == 3


def test_a_four_tuple_limit_flag_is_honoured():
    """The positional form of the same flag — the shape the executor can return with one change."""
    from engine.fanout import run_fanout

    plan = plan_fanout("Do {{item}}", items=["a", "b"])
    calls = {"n": 0}

    def runner(item, agent_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return "", "HTTP 429: too many requests", 0, True
        return "ok", "", 1, False

    run_fanout(plan, runner, agents=["ag_1"], max_parallel=2)
    assert plan.summary()["succeeded"] == 2
    assert calls["n"] == 3


def test_an_exception_carrying_a_typed_kind_is_a_rate_limit():
    """A `GatewayError`'s kind decides, whatever its message says — that is the transport's own word."""
    from engine.fanout import run_fanout
    from engine.providers.base import ErrorKind, GatewayError

    plan = plan_fanout("Do {{item}}", items=["a", "b"])
    calls = {"n": 0}

    def runner(item, agent_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GatewayError(ErrorKind.RATE_LIMIT, "slow down")
        return "ok", "", 1

    run_fanout(plan, runner, agents=["ag_1"], max_parallel=2)
    assert plan.summary()["succeeded"] == 2


def test_a_typed_kind_that_is_not_a_limit_is_not_retried():
    """The other half of typing: a server error whose text mentions 429 is not backpressure."""
    from engine.fanout import run_fanout
    from engine.providers.base import ErrorKind, GatewayError

    plan = plan_fanout("Do {{item}}", items=["a", "b"])
    calls = {"n": 0}

    def runner(item, agent_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GatewayError(ErrorKind.SERVER, "upstream said 429 in a log line")
        return "ok", "", 1

    run_fanout(plan, runner, agents=["ag_1"], max_parallel=2)
    assert plan.summary()["succeeded"] == 1, "a typed non-limit must not be retried"


def test_the_rate_limit_retry_budget_is_per_item():
    """F2: the counter was one shared field, so a later item was failed on its *first* refusal.

    Every item here is refused exactly once and succeeds on its own retry. With a global budget the
    sixth requeue event in the fan-out tripped the cap and items 6, 7 and 8 were failed without ever
    being retried — work lost, and reported against the items rather than the provider.
    """
    from engine.fanout import run_fanout

    plan = plan_fanout("Do {{item}}", items=[f"i{n}" for n in range(8)])
    seen: dict[str, int] = {}

    def runner(item, agent_id):
        seen[item.item] = seen.get(item.item, 0) + 1
        if seen[item.item] == 1:
            raise RuntimeError("HTTP 429: rate limit reached")
        return "ok", "", 1

    run_fanout(plan, runner, agents=["ag_1"], max_parallel=4)
    summary = plan.summary()
    assert summary["failures"] == [], "each item carries its own retry budget"
    assert summary["succeeded"] == 8


def test_a_provider_throttling_the_whole_wave_stops_the_fanout(monkeypatch):
    """The wave's own budget: a provider refusing the job is not 128 unlucky items.

    Without it, the per-item bound alone would permit five refusals for each of 128 items — 640
    refusals aimed at a provider that has already said no. The wave stops instead, and every item it
    abandons is reported as *the provider's* throttle, naming the provider rather than the item.
    """
    import engine.fanout as fanout
    from engine.fanout import run_fanout

    monkeypatch.setattr(fanout, "RATE_LIMIT_MAX_WAVE_REQUEUES", 3)
    events: list[tuple[str, dict]] = []
    plan = plan_fanout("Do {{item}}", items=[f"i{n}" for n in range(6)])

    def runner(item, agent_id):
        raise RuntimeError("429 too many requests")

    run_fanout(plan, runner, agents=["ag_1"], max_parallel=1,
               on_event=lambda k, p: events.append((k, p)))
    summary = plan.summary()
    assert summary["failed"] == 6
    assert summary["complete"] is False
    assert summary["succeeded"] == 0
    throttled = [p for k, p in events if k == "fanout.throttled"]
    assert throttled, "the wave must report that it abandoned the work"
    assert throttled[0]["abandoned"] == 6, "no item is left claiming to have a result"
    for failure in summary["failures"]:
        assert "throttl" in failure["error"], \
            f"the provider must be named, not the item: {failure['error']}"


def test_item_artifacts_and_usage_reach_the_plan_summary():
    """F4: a fan-out node's result is the sum of its items, so the items must be able to say so."""
    from engine.fanout import ItemOutcome, run_fanout

    plan = plan_fanout("Do {{item}}", items=["a", "b"])
    run_fanout(
        plan,
        lambda item, agent: ItemOutcome(
            output=f"did {item.item}", tokens=5,
            artifacts=[{"name": "change", "path": f"src/{item.item}.py", "type": "change"}],
            usage={"tokens_in": 3, "tokens_out": 2, "cost_usd": 0.01},
        ),
        agents=["ag_1"],
    )
    summary = plan.summary()
    assert [a["path"] for a in summary["artifacts"]] == ["src/a.py", "src/b.py"]
    assert summary["usage"] == {"tokens_in": 6, "tokens_out": 4, "cost_usd": 0.02}
    assert plan.items[0].artifacts[0]["path"] == "src/a.py"
    assert plan.items[0].as_dict()["usage"]["tokens_in"] == 3


def test_a_failed_items_artifacts_are_not_counted_as_the_waves_own():
    """A failure's output is not evidence: only successful items may contribute."""
    from engine.fanout import ItemOutcome, run_fanout

    plan = plan_fanout("Do {{item}}", items=["a", "b"])

    def runner(item, agent_id):
        if item.item == "b":
            return ItemOutcome(error="the model refused",
                               artifacts=[{"path": "src/b.py"}],
                               usage={"tokens_in": 1})
        return ItemOutcome(output="ok", artifacts=[{"path": "src/a.py"}],
                           usage={"tokens_in": 4})

    run_fanout(plan, runner, agents=["ag_1"])
    summary = plan.summary()
    assert [a["path"] for a in summary["artifacts"]] == ["src/a.py"]
    assert summary["usage"]["tokens_in"] == 4


def test_a_real_fanout_does_not_re_execute_an_item_whose_summary_mentions_429(config, source):
    """F1 end to end, through the executor's own `_one` (`executor.py:1695-1697`).

    Three items, each reporting `needs_review` with a summary containing "429". Before the fix each
    item was re-queued and run a second time — six model calls, the budget charged twice, two session
    turns per item. After the fix: three calls, and the node honestly reports `needs_review`.
    """
    executor, provider = make_fanout_executor(config, source, [{
        "id": "reviewall", "skill": "code-reviewer", "phase": "REVIEW",
        "fanout": "Review {{item}} for regressions.",
        "items": ["src/a.ts", "src/b.ts", "src/c.ts"],
        "inputs": [], "outputs": ["review-report"],
    }])
    bundle = source.load("code-reviewer")
    calls = {"n": 0}

    def not_done(request):
        calls["n"] += 1
        payload = {
            "status": "needs_review", "verdict": "changes_requested",
            "summary": f"line 429 changed in the file under review (attempt {calls['n']})",
            "criteria_satisfied": [{"criterion": c, "satisfied": True, "evidence": "src/x:1"}
                                   for c in bundle.contract.criteria],
            "checklist": [{"id": i, "status": "PASS", "evidence": "e"}
                          for i in bundle.checklist_ids()],
        }
        return ChatResponse(text=json.dumps(payload),
                            usage=Usage(prompt_tokens=5, completion_tokens=5,
                                        reported_cost_usd=0.0),
                            model=request.model, provider_id=provider.provider_id,
                            finish_reason=FinishReason.STOP)

    provider.complete = not_done  # type: ignore[method-assign]
    state = {"nodes": {"reviewall": {"status": "pending"}}, "artifacts": {},
             "budget": {"steps_used": 0, "iterations": {}}}
    result = executor.execute_node("reviewall", state, {})

    assert calls["n"] == 3, f"3 items, 3 calls; got {calls['n']} — an item was re-executed"
    assert result["status"] == "needs_review"
    assert result["fanout"]["failed"] == 3
    assert result["fanout"]["complete"] is False


# ── one session, two items in flight (F6) ────────────────────────────────────


def _two_sessions():
    """The shape a rotation leaves behind: the sealed session and the one that continues it."""
    from engine.context.session import Session

    sealed = Session(agent_id="ag_1", node_id="reviewall", window=32768)
    fresh = Session(agent_id="ag_1", node_id="reviewall", window=32768, index=2)
    sealed.adopt_successor(fresh)
    return sealed, fresh


def test_a_rotation_does_not_fail_a_concurrent_append():
    """F6: every fan-out item on one agent shares one `Session` (`executor.py:1994`).

    One item rotates it — seal, mark the handoff, close, the three calls `_rotate` makes
    (`executor.py:2125-2127`) — while a sibling is still mid-exchange. The sibling's turn then lands
    on a closed transcript. Before: `SessionError`, the item was marked failed and its artifacts were
    never persisted, so a rotation by one item silently destroyed another item's finished work.
    """
    from engine.fanout import run_fanout

    sealed, fresh = _two_sessions()
    plan = plan_fanout("Do {{item}}", items=["a", "b"])

    def runner(item, agent_id):
        if item.item == "b":
            sealed.seal(reason="attention decayed")
            sealed.mark_handoff()
            sealed.close()
        sealed.append_text("user", f"work for {item.item}")
        return "ok", "", 1

    run_fanout(plan, runner, agents=["ag_1"], max_parallel=2)
    assert plan.summary()["failures"] == [], "a sibling's rotation must not fail an item"
    assert len(sealed.turns) + len(fresh.turns) == 2, "neither item's turn may be dropped"
    assert any("work for b" in turn.text for turn in fresh.turns), \
        "a turn that arrives after the rotation belongs to the successor"


def test_a_closed_session_with_no_successor_still_refuses_turns():
    """The boundary of the forwarding rule: nothing continues it, so its transcript is final.

    This is what keeps `session.append` an error rather than a silent no-op for a caller with no
    rotation to follow (`tests/test_phase5_context.py` pins the same contract).
    """
    from engine.context.session import Session, SessionError

    session = Session(agent_id="ag_1", window=1000)
    session.seal(reason="rotation")
    session.mark_handoff()
    session.close()
    with pytest.raises(SessionError, match="no rotation continued it"):
        session.append_text("user", "late")
    with pytest.raises(SessionError, match="no rotation continued it"):
        session.append_text("user", "late")


def test_a_rotation_by_one_iteration_does_not_fail_its_twin():
    """Two items that both decide to rotate the same session: the first reason stands, neither fails."""
    import threading

    from engine.context.session import Session

    session = Session(agent_id="ag_1", window=1000)
    session.seal(reason="first")
    outcome: list[object] = []

    def sibling_rotation():
        try:
            sealed = session.seal(reason="second")
            session.mark_handoff()
            session.close()
            outcome.append(sealed)
        except Exception as exc:  # noqa: BLE001 - the point is that this must not happen
            outcome.append(exc)

    thread = threading.Thread(target=sibling_rotation)
    thread.start()
    thread.join()
    assert outcome == [False], f"a sibling's rotation must be refused, not fatal: {outcome}"
    assert session.rotation_reason == "first", "the first rotation's reason is the one that counts"


def test_concurrent_appends_do_not_lose_or_interleave_turns():
    """One session, eight writers: every turn is kept exactly once and the counts stay coherent."""
    import threading

    from engine.context.session import Session

    session = Session(agent_id="ag_1", window=10 ** 6)
    writers = 8
    per_writer = 25
    barrier = threading.Barrier(writers)

    def write(worker: int) -> None:
        barrier.wait()
        for n in range(per_writer):
            session.append_text("user", f"{worker}:{n}")
            session.pin(f"NEVER constraint {worker}:{n}")

    threads = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert session.turns_count == writers * per_writer
    assert len(session.pinned) == writers * per_writer, "a pin must not be lost to a race"
    assert session.history_tokens == sum(t.tokens for t in session.turns)
    summary = session.as_dict(include_turns=False)
    assert summary["turns"] == session.turns_count
    assert summary["pinned_count"] == len(session.pinned)
    full = session.as_dict()
    assert len(full["turns"]) == session.turns_count
    assert len(full["pinned"]) == len(session.pinned)
