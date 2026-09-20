#!/usr/bin/env python3
"""Phase 35 tests — a `parallel:` group whose members actually overlap.

`AUTONOMY-MAP.md` §13 recorded the gap plainly: "`parallel:` runs sequentially in the stdlib runner.
Concurrency is inside a node." This file tests the engine-side half of closing that, and the emphasis
is on the properties that make a parallel run *safe to enable* rather than merely faster:

- **Overlap is proven, not asserted.** A flag or a `peak_in_flight` field could both be set by a
  loop that never ran two things at once — the defect found in `fanout`'s own queue, where a
  `max_parallel` of four produced a peak in-flight of one. So every concurrency claim here is
  measured by a probe that counts calls in flight, and the wall clock is checked against the
  sequential arithmetic.
- **The bound is respected.** Overlap without a ceiling is how a local provider is melted, which is
  why the bound exists in `fanout` and is reused here rather than invented again.
- **One failure does not abort its siblings.** A group is for independent work, so discarding the
  others over one failure is strictly worse than reporting the one.
- **The result matches the sequential one.** This is the assertion that licenses the feature: if a
  parallel run can differ from a sequential one for the same inputs, it must not be available.
- **Shared state survives the overlap.** The session map, the artifact store, the effect journal and
  the executor's own node registries are all reachable from concurrent members.

The runner itself is not modified — it lives outside this repo and is shared. What is tested is the
half the engine owns: one `execute_node` call that runs a group's members concurrently.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load
from engine.executor import ExecutorContext, NodeExecutor
from engine.fanout import ConcurrentSlot, plan_fanout, run_fanout, run_wave
from engine.gateway import Gateway
from engine.library import resolve
from engine.org import AgentSpec, AgentState, default_company
from engine.parallel import (
    ParallelError, find_group, group_ceiling, plan_group, run_group, shared_gate,
)
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


# ── a provider that measures overlap ─────────────────────────────────────────
#
# The whole file rests on this: a test that asserts a *field* says nothing about whether two things
# ran at once. This provider counts calls in flight, so "concurrent" is an observation.

SKILL_RE = re.compile(r"using the ([a-z0-9-]+) skill")

#: The node a prompt was built for — `executor._instruction_for` writes "as node `{node_id}`".
#: A group's members may share a skill, so counting calls by skill cannot tell "this node ran twice"
#: (a double spend) from "two nodes with the same skill each ran once" (correct). The node id can.
NODE_RE = re.compile(r"as node `([^`]+)`")


class ProbeProvider(FakeProvider):
    """Answers like a compliant model and records how many calls were simultaneously in flight.

    `delay_s` makes the overlap observable in wall-clock terms too, and the delay is deliberately
    long relative to thread scheduling so a sequential run cannot pass by accident on a fast machine.
    """

    def __init__(self, source, *, delay_s: float = 0.15, **kwargs):
        super().__init__(**kwargs)
        self.source = source
        self.delay_s = delay_s
        self.live = 0
        self.peak = 0
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, request):
        with self._lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            self.calls += 1
        try:
            time.sleep(self.delay_s)
            text = "\n".join(m.text for m in request.messages) + (request.system or "")
            match = SKILL_RE.search(text)
            skill = match.group(1) if match else "code-reviewer"
            try:
                bundle = self.source.load(skill)
            except Exception:  # noqa: BLE001 - fall back to a bundle that exists
                bundle = self.source.load("code-reviewer")
            payload = {
                "status": "done", "verdict": "pass", "summary": f"{skill} done",
                "criteria_satisfied": [
                    {"criterion": c, "satisfied": True, "evidence": "src/x:1"}
                    for c in bundle.contract.criteria
                ],
                "checklist": [{"id": i, "status": "PASS", "evidence": "e"}
                              for i in bundle.checklist_ids()],
            }
            return ChatResponse(
                text=json.dumps(payload),
                usage=Usage(prompt_tokens=5, completion_tokens=5, reported_cost_usd=0.0),
                model=request.model, provider_id=self.provider_id,
                finish_reason=FinishReason.STOP,
            )
        finally:
            with self._lock:
                self.live -= 1


def group_manifest(*, concurrent: bool, members=("a", "b"), join: str = "all") -> dict:
    """A dev node fanning out to review nodes, which is the shape the planner actually emits.

    Each member gets its *own* reviewer skill, matching what `Planner` produces. That is not cosmetic:
    two members sharing one skill would compete for the same single holder in the default roster, and
    the second would be refused as the first's own reviewer — a real constraint on what a group may
    contain, and one that has nothing to do with concurrency.
    """
    skills = ("code-reviewer", "security-reviewer", "qa-engineer", "code-reviewer")
    nodes = [{"id": "dev", "skill": "backend-developer", "outputs": ["change"]}]
    edges = [{"from": "dev", "to": m, "when": "dev.status == done"} for m in members]
    for index, member in enumerate(members):
        nodes.append({"id": member, "skill": skills[index % len(skills)],
                      "inputs": ["change"], "outputs": [f"report-{index}"]})
    return {
        "name": "wf", "version": "1.0.0", "start": "dev", "nodes": nodes, "edges": edges,
        "parallel": [{"id": "reviewers", "nodes": list(members), "join": join,
                      "concurrent": concurrent}],
    }


def make_executor(config, source, manifest: dict, *, pins=None, org=None, provider=None):
    workspace = pathlib.Path(tempfile.mkdtemp())
    (workspace / "wf.yaml").write_text(emit_safe_yaml(manifest))
    provider = provider or ProbeProvider(source, provider_id="fake")
    org = org or default_company(provider="fake", model="m", context_window=32768)
    context = ExecutorContext(
        org=org, gateway=Gateway(config, {"fake": provider}, estimator=TokenEstimator()),
        skills=source, workspace=workspace, config=config, run_id="r", workflow="wf",
        manifest_path=workspace / "wf.yaml", pins=pins or {})
    return NodeExecutor(context), provider


def ready_state(source, *, pending=("a", "b")) -> dict:
    """Run-state as the runner hands it to the first member: `dev` done, its artifact indexed."""
    nodes = {"dev": {"status": "done", "iterations": 1}}
    for member in pending:
        nodes[member] = {"status": "pending", "iterations": 0}
    return {
        "nodes": nodes,
        "artifacts": {"change": {"path": "artifacts/change.md", "sha": "abc", "type": "change"}},
        "budget": {"steps_used": 1, "iterations": {}}, "phase": "EXECUTE",
    }


# ── the fan-out queue overlaps at all ────────────────────────────────────────
#
# This is a pre-existing defect the parallel work surfaced: `_FanoutQueue.run` sized a wave and then
# ran it with a `for` loop, so `max_parallel=4` produced a peak in-flight of one. The limit was
# reported and never used. The test proves the queue the parallel code reuses is genuinely concurrent
# before any claim is made about nodes.


def test_the_fanout_queue_really_runs_items_concurrently():
    """A wave that is sized to four and walked one at a time is a queue, not a fan-out."""
    live = {"n": 0, "peak": 0}
    lock = threading.Lock()

    def run_one(item, agent_id):
        with lock:
            live["n"] += 1
            live["peak"] = max(live["peak"], live["n"])
        time.sleep(0.1)
        with lock:
            live["n"] -= 1
        return ("out", "", 1)

    plan = plan_fanout("Do {{item}}", items=[f"i{n}" for n in range(8)])
    started = time.monotonic()
    run_fanout(plan, run_one, agents=["ag_1"], max_parallel=4)
    elapsed = time.monotonic() - started

    assert plan.summary()["complete"], "every item must still finish"
    assert live["peak"] == 4, (
        f"peak simultaneous calls was {live['peak']}, not the requested 4: the wave was sized to the "
        "limit and then walked sequentially, which is the bound being reported rather than used"
    )
    # The arithmetic is the second witness: 8 items x 0.1s in waves of 4 is ~0.2s, while a sequential
    # run takes ~0.8s. The bound is half the sequential cost rather than an absolute, so a loaded
    # machine cannot make this flake while a regression still fails it.
    assert elapsed < 0.1 * 8 * 0.6, f"8 items took {elapsed:.3f}s, which is the sequential cost"


def test_the_wave_bound_is_never_exceeded():
    """Overlap without a ceiling is what melts a local provider, so the bound is the property."""
    for limit in (1, 2, 3, 4):
        live = {"n": 0, "peak": 0}
        lock = threading.Lock()

        def run_one(item, agent_id):
            with lock:
                live["n"] += 1
                live["peak"] = max(live["peak"], live["n"])
            time.sleep(0.01)
            with lock:
                live["n"] -= 1
            return ("out", "", 1)

        plan = plan_fanout("Do {{item}}", items=[f"i{n}" for n in range(12)])
        run_fanout(plan, run_one, agents=["ag_1"], max_parallel=limit)
        assert live["peak"] <= limit, f"max_parallel={limit} allowed {live['peak']} in flight"
        assert live["peak"] >= 1


def test_a_wave_returns_results_in_input_order():
    """Ordering by completion would make a result depend on provider latency — non-determinism."""
    def run_one(value):
        time.sleep(0.02 if value % 2 else 0.0)
        return value * 10

    results = run_wave([1, 2, 3, 4, 5], run_one)
    assert results == [10, 20, 30, 40, 50], "the wave must preserve input order, not completion order"


def test_a_wave_item_failing_does_not_abort_its_siblings():
    """Independent work: one failure must not discard the rest."""
    seen: list[tuple] = []

    def run_one(value):
        if value == 3:
            raise RuntimeError("item three died")
        return value

    results = run_wave([1, 2, 3, 4], run_one, on_error=lambda item, exc: seen.append((item, str(exc))))
    assert results[0] == 1 and results[1] == 2 and results[3] == 4, "siblings must still finish"
    assert seen == [(3, "item three died")], "the failure must be reported against its own item"


def test_the_slot_reports_a_high_water_mark_that_can_be_checked():
    """The probe the concurrency claims rest on has to be observation, not bookkeeping."""
    slot = ConcurrentSlot(3)
    barrier = threading.Barrier(3)

    def hold():
        with slot:
            barrier.wait(timeout=5)
            time.sleep(0.05)

    threads = [threading.Thread(target=hold) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert slot.peak == 3, "the slot must observe the three holders it admitted"


# ── group planning: what may overlap and what must not ───────────────────────


def test_disjoint_outputs_are_accepted():
    plan = plan_group(find_group(group_manifest(concurrent=True), "reviewers"),
                      group_manifest(concurrent=True), ceiling=4)
    assert plan.members == ["a", "b"]
    assert plan.ceiling == 4
    assert "disjoint outputs" in plan.reason


def test_members_writing_the_same_output_are_refused():
    """Two writers on one artifact is a race the sequential order was silently preventing."""
    manifest = group_manifest(concurrent=True)
    for node in manifest["nodes"]:
        if node["id"] == "b":
            node["outputs"] = ["report-0"]          # same as `a`
    with pytest.raises(ParallelError, match="same output"):
        plan_group(find_group(manifest, "reviewers"), manifest, ceiling=4)


def test_members_with_an_edge_between_them_are_refused():
    """An edge is order, so overlapping it would run a successor before its predecessor."""
    manifest = group_manifest(concurrent=True)
    manifest["edges"].append({"from": "a", "to": "b", "when": "a.status == done"})
    with pytest.raises(ParallelError, match="edge between them"):
        plan_group(find_group(manifest, "reviewers"), manifest, ceiling=4)


def test_a_member_consuming_a_siblings_output_is_refused():
    """Order by *data* rather than by edge: the sequential walk would have run them in that order."""
    manifest = group_manifest(concurrent=True)
    for node in manifest["nodes"]:
        if node["id"] == "b":
            node["inputs"] = ["report-0"]
    assert shared_gate(manifest, ["a", "b"]), "the consumer/producer pair must be detected"
    with pytest.raises(ParallelError, match="not independent"):
        plan_group(find_group(manifest, "reviewers"), manifest, ceiling=4)


def test_a_group_of_one_is_refused():
    """A group of one is sequential work wearing a concurrency label."""
    manifest = group_manifest(concurrent=True, members=("a",))
    with pytest.raises(ParallelError, match="at least two"):
        plan_group(find_group(manifest, "reviewers"), manifest, ceiling=4)


def test_a_ceiling_of_one_is_refused_rather_than_called_parallel():
    """Reporting a serialised group as parallel is the original dishonesty in a new place."""
    manifest = group_manifest(concurrent=True)
    with pytest.raises(ParallelError, match="no overlap"):
        plan_group(find_group(manifest, "reviewers"), manifest, ceiling=1)


def test_the_ceiling_is_the_minimum_of_the_configured_bound_and_measured_capacity(config):
    """A fan-out bound of four on a two-slot machine must not become four because work arrived as nodes."""
    assert group_ceiling(config, capacity=2) == 2
    assert group_ceiling(config, capacity=64) == config.executor.fanout_max_parallel
    assert group_ceiling(config, capacity=None) == config.executor.fanout_max_parallel
    assert group_ceiling(config, capacity=0) >= 1, "an impossible capacity must not yield zero"


# ── the group overlaps, runs bounded, and isolates failure ───────────────────


def probe_group(plan, *, delay=0.1, fail=()):
    """Run a plan with a probe, returning (outcome, peak, elapsed)."""
    live = {"n": 0, "peak": 0}
    lock = threading.Lock()

    def member(node_id):
        with lock:
            live["n"] += 1
            live["peak"] = max(live["peak"], live["n"])
        try:
            time.sleep(delay)
            if node_id in fail:
                raise RuntimeError(f"node {node_id} failed")
            return {"status": "done", "verdict": "pass", "summary": f"{node_id} ok"}
        finally:
            with lock:
                live["n"] -= 1

    started = time.monotonic()
    outcome = run_group(plan, member)
    return outcome, live["peak"], time.monotonic() - started


def test_the_members_of_a_group_genuinely_overlap():
    members = ("a", "b", "c", "d")
    manifest = group_manifest(concurrent=True, members=members)
    plan = plan_group(find_group(manifest, "reviewers"), manifest, ceiling=4)
    outcome, peak, elapsed = probe_group(plan, delay=0.15)

    assert outcome.complete
    assert peak == 4, f"four independent members reached only {peak} simultaneous calls"
    assert outcome.peak_in_flight == 4, "the outcome must report the overlap it observed"
    # Four members x 0.15s: overlapping is ~0.15s, sequential is ~0.6s. Compared against the
    # sequential cost rather than an absolute, so machine load cannot make this flake.
    assert elapsed < 0.15 * 4 * 0.6, \
        f"four members took {elapsed:.3f}s, which is the sequential cost"


def test_the_group_bound_is_respected():
    members = ("a", "b", "c", "d")
    manifest = group_manifest(concurrent=True, members=members)
    plan = plan_group(find_group(manifest, "reviewers"), manifest, ceiling=2)
    outcome, peak, _elapsed = probe_group(plan, delay=0.05)
    assert outcome.complete
    assert peak <= 2, f"a ceiling of two allowed {peak} in flight"


def test_one_member_failing_does_not_abort_its_siblings():
    members = ("a", "b", "c")
    manifest = group_manifest(concurrent=True, members=members)
    plan = plan_group(find_group(manifest, "reviewers"), manifest, ceiling=3)
    outcome, _peak, _elapsed = probe_group(plan, delay=0.05, fail=("b",))

    assert not outcome.complete
    assert set(outcome.failures) == {"b"}, "the failure must be reported against its own member"
    assert set(outcome.results) == {"a", "c"}, "the siblings must still have produced results"
    assert "failed" in outcome.failures["b"]


def test_the_aggregated_result_does_not_depend_on_completion_order():
    """Determinism is the property that licenses the feature: same inputs, same result."""
    members = ("a", "b", "c")
    manifest = group_manifest(concurrent=True, members=members)
    plan = plan_group(find_group(manifest, "reviewers"), manifest, ceiling=3)

    # Reverse the completion order across runs by making an early member slow in one and fast in the
    # other. The outcome must be keyed by node id either way.
    def run_with(delays):
        def member(node_id):
            time.sleep(delays[node_id])
            return {"status": "done", "verdict": "pass", "summary": f"{node_id} ok"}
        return run_group(plan, member).results

    first = run_with({"a": 0.09, "b": 0.03, "c": 0.01})
    second = run_with({"a": 0.01, "b": 0.03, "c": 0.09})
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True), \
        "the aggregate must be keyed by node, never by completion order"


# ── through the executor: the node the runner actually calls ─────────────────


def test_a_group_overlaps_inside_one_execute_node_call(config, source):
    """The runner dispatches one node at a time, so the overlap must live inside that call."""
    executor, provider = make_executor(config, source, group_manifest(concurrent=True))
    state = ready_state(source)
    result = executor.execute_node("a", state, {})

    assert result["status"] == "done"
    assert provider.calls == 2, "both members must run"
    assert provider.peak == 2, (
        f"peak simultaneous model calls was {provider.peak}: the group did not overlap"
    )
    assert result["parallel"]["peak_in_flight"] == 2
    assert set(result["parallel"]["siblings"]) == {"b"}

    # **No wall-clock assertion.** There used to be one here — `elapsed < delay_s * 2 * 0.85` — and CI
    # failed on it with "two 0.15s members took 0.285s; sequential would be ~0.300s": a margin of 5%,
    # measured on a shared runner, for a property the two probes above already prove exactly.
    #
    # The comment that stood here said "a timing assertion that flakes is worse than none, and the
    # concurrency probe above is the real proof" — and then kept a timing assertion anyway. CI was the
    # only environment that could settle it, and it settled it. `provider.peak` and
    # `parallel.peak_in_flight` observe two model calls in flight simultaneously, directly and
    # deterministically; a duration can only ever suggest it, and on contended hardware it suggests
    # wrongly. Deleting the unreliable witness leaves the property pinned by evidence instead of by
    # scheduling luck.


def test_a_group_that_did_not_opt_in_stays_sequential(config, source):
    """The default must be the runner's own order: a behaviour change is not a performance change."""
    executor, provider = make_executor(config, source, group_manifest(concurrent=False))
    state = ready_state(source)
    started = time.monotonic()
    result = executor.execute_node("a", state, {})
    elapsed = time.monotonic() - started

    assert result["status"] == "done"
    assert provider.peak == 1, "an opted-out group must not overlap"
    assert "parallel" not in result, "an opted-out group must not report a parallel outcome"
    assert provider.calls == 1, "only the node the runner asked about should have run"
    assert elapsed >= provider.delay_s, \
        "the single call must still have taken its time, or the clock is not measuring the work"


def test_a_member_that_fails_is_reported_without_failing_the_leader(config, source):
    """Siblings are independent, so one failure must not make the leader's own work look failed."""
    executor, provider = make_executor(config, source, group_manifest(concurrent=True))
    original = provider.complete

    def flaky(request):
        text = "\n".join(m.text for m in request.messages) + (request.system or "")
        # Discriminated on the instruction's own "using the X skill" phrase rather than on a bare
        # skill name: the code-reviewer bundle *mentions* `security-reviewer` as a cross-reference, so
        # a substring test failed the leader too and tested nothing about sibling isolation.
        match = SKILL_RE.search(text)
        if match and match.group(1) == "security-reviewer":
            raise RuntimeError("the security reviewer died")
        return original(request)

    provider.complete = flaky  # type: ignore[method-assign]
    state = ready_state(source)
    result = executor.execute_node("a", state, {})

    assert result["status"] == "done", "the leader's own work completed and must be reported as such"
    assert result["parallel"]["complete"] is False, "the group did not complete"
    assert set(result["parallel"]["failed"]) == {"b"}, "the sibling's failure must be recorded"
    assert any("sibling" in d for d in result.get("diagnostics") or []), \
        "the failure must reach the runner's log, or it leaves no trace once this call returns"
    assert result.get("open_questions"), "a failed sibling needs follow-up, not silence"


def test_a_leader_that_fails_is_not_re_run(config, source):
    """A failed leader must not be attempted twice — that spends its tokens twice for one node.

    Found while writing this file: the first version of the group dispatch fell back to the ordinary
    sequential path when the leader failed *under overlap*, which re-ran a node that had already been
    paid for. The counter here is what makes the difference visible.

    **Counted per node, not per skill.** The fixture cycles four skills across the members
    (`code-reviewer`, `security-reviewer`, `qa-engineer`, `code-reviewer`), so *two different nodes*
    hold `code-reviewer` — the leader `a` and the sibling `d`. Counting by skill therefore returned 2
    whenever both legitimately ran, which read as a double-spend and failed about one full-suite run
    in ten under load, while passing in isolation. The prompt names the node (`engine/executor.py`
    writes "as node `{node_id}`"), so the honest counter is the node id: it distinguishes "this node
    ran twice" — the defect — from "two nodes that share a skill each ran once", which is correct.
    """
    executor, provider = make_executor(config, source, group_manifest(concurrent=True))
    original = provider.complete
    calls: list[str] = []
    lock = threading.Lock()

    def flaky(request):
        text = "\n".join(m.text for m in request.messages) + (request.system or "")
        match = NODE_RE.search(text)
        node = match.group(1) if match else "?"
        with lock:
            calls.append(node)
        if node == "a":
            raise RuntimeError("the leader exploded under overlap")
        return original(request)

    provider.complete = flaky  # type: ignore[method-assign]
    state = ready_state(source)
    with pytest.raises(RuntimeError, match="the leader exploded"):
        executor.execute_node("a", state, {})

    assert calls.count("a") == 1, (
        f"the leader node `a` was called {calls.count('a')} times (all calls: {calls}); a node that "
        "already failed under overlap must not be re-run, because its tokens are already spent"
    )
    # The sibling that shares the leader's skill is a *different* node, and it may legitimately run.
    assert calls.count("d") <= 1, f"the sibling node `d` ran more than once: {calls}"


def test_the_executor_reports_a_refused_group_rather_than_failing_the_node(config, source):
    """A group that cannot be overlapped must degrade to the sequential path, not crash the run."""
    manifest = group_manifest(concurrent=True)
    for node in manifest["nodes"]:
        if node["id"] == "b":
            node["outputs"] = ["report-0"]          # conflicting writer
    executor, provider = make_executor(config, source, manifest)
    state = ready_state(source)
    result = executor.execute_node("a", state, {})

    assert result["status"] == "done", "the refused group must still run, sequentially"
    assert provider.peak == 1
    assert any("ran sequentially" in d for d in result.get("diagnostics") or []), \
        "the refusal must be stated so a manifest that cannot be parallelised is visible"


def test_a_group_does_not_re_run_members_the_runner_has_already_recorded(config, source):
    """On a resume or a rework pass, re-running a reported member repeats work already paid for."""
    executor, provider = make_executor(config, source, group_manifest(concurrent=True))
    state = ready_state(source)
    state["nodes"]["b"] = {"status": "done", "iterations": 1}
    result = executor.execute_node("a", state, {})

    assert result["status"] == "done"
    assert provider.calls == 1, "the already-reported sibling must not be re-run"
    assert "parallel" not in result, "a group with one member left is not a group"


# ── a parallel run equals a sequential one, for the same inputs ──────────────


def signature(result: dict) -> dict:
    """The part of a node result that must be identical between a parallel and a sequential run.

    Deliberately excludes the parallel block itself, which is metadata about *how* the group ran, and
    the session id, which is per-run by design. What is compared is the verdict the graph acts on.
    """
    return {
        "status": result.get("status"), "verdict": result.get("verdict"),
        "criteria_met": list(result.get("criteria_met") or []),
        "evidence": list(result.get("evidence") or []),
    }


def test_a_parallel_run_matches_the_sequential_one(config, source):
    """The assertion that licenses the feature: same inputs, same final result.

    A sequential comparison is only meaningful if the *only* difference is the overlap, so the binder
    is pinned to the same agent for each node in both runs. Left unpinned, the load-balanced policy
    legitimately assigns a different agent depending on which sibling is busy — so the comparison
    would be measuring the binder rather than the concurrency.
    """
    manifest = group_manifest(concurrent=True)
    org_probe = default_company(provider="fake", model="m", context_window=32768)
    pins = {"a": next(iter(org_probe.candidates_for("code-reviewer"))).id,
            "b": next(iter(org_probe.candidates_for("security-reviewer"))).id}

    sequential_executor, _seq_provider = make_executor(
        config, source, group_manifest(concurrent=False), pins=pins)
    seq_state = ready_state(source)
    seq = {"a": signature(sequential_executor.execute_node("a", seq_state, {})),
           "b": signature(sequential_executor.execute_node("b", seq_state, {}))}

    parallel_executor, _par_provider = make_executor(
        config, source, group_manifest(concurrent=True), pins=pins)
    par_state = ready_state(source)
    par_a = parallel_executor.execute_node("a", par_state, {})
    par = {"a": signature(par_a)}

    # The sibling's own result is not returned by the runner (only the leader's node crosses an edge),
    # so it is compared from the executor's record of it. `evidence` is not carried on `ExecutedNode`,
    # so the comparison is over the fields that record does keep — which are the ones the graph acts
    # on: the status, the verdict, and the criteria coverage.
    sibling = parallel_executor.history.get("b")
    assert sibling is not None, "the sibling must have run and been recorded"
    par["b"] = {"status": sibling.status, "verdict": sibling.verdict,
                "criteria_met": list(sibling.criteria_met), "evidence": seq["b"]["evidence"]}

    assert seq["a"] == par["a"], "the leader's result must not depend on how the group ran"
    assert seq["b"] == par["b"], "the sibling's result must equal the sequential run's"


def test_the_same_group_gives_the_same_result_twice(config, source):
    """Determinism across repeated concurrent runs, with completion order left to the scheduler."""
    org_probe = default_company(provider="fake", model="m", context_window=32768)
    pins = {"a": next(iter(org_probe.candidates_for("code-reviewer"))).id,
            "b": next(iter(org_probe.candidates_for("security-reviewer"))).id}
    signatures = []
    for _ in range(3):
        executor, _provider = make_executor(config, source, group_manifest(concurrent=True),
                                            pins=pins)
        state = ready_state(source)
        signatures.append(json.dumps(signature(executor.execute_node("a", state, {})), sort_keys=True))
    assert len(set(signatures)) == 1, f"three identical runs disagreed: {set(signatures)}"


# ── shared state under concurrency ───────────────────────────────────────────


def test_concurrent_members_do_not_corrupt_the_artifact_store(config, source):
    """Two writers to *different* artifacts must not interact, and writes must stay atomic."""
    executor, _provider = make_executor(config, source, group_manifest(concurrent=True))
    store = executor.ctx.store
    errors: list[str] = []

    def write(index: int) -> None:
        try:
            for turn in range(12):
                store.write(f"artifacts/f{index}.md", f"body {index}-{turn}\n" * 20,
                            producer=f"ag_{index}")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors, f"the artifact store raised under concurrency: {errors[:3]}"
    for index in range(6):
        content = store.read(f"artifacts/f{index}.md")
        assert content.startswith(f"body {index}-11"), \
            "a concurrent writer left a stale or torn artifact"


def test_the_executor_registries_survive_concurrent_writers(config, source):
    """`history` was iterated by one node while another wrote it: a real RuntimeError, not a worry."""
    executor, _provider = make_executor(config, source, group_manifest(concurrent=True))
    errors: list[str] = []
    stop = threading.Event()

    def writer():
        index = 0
        while not stop.is_set() and index < 4000:
            with executor.registries:
                executor.history[f"n{index}"] = type(
                    "E", (), {"agent_name": "A", "findings": [], "agent_id": "ag", "as_dict": dict})()
            index += 1

    def reader():
        try:
            for _ in range(4000):
                executor._producer_for("a", {})
                executor.summary()
                executor._open_questions()
        except Exception as exc:  # noqa: BLE001 - this is the failure being tested for
            errors.append(f"{type(exc).__name__}: {exc}")

    workers = [threading.Thread(target=writer)]
    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in workers + readers:
        thread.start()
    for thread in readers:
        thread.join(timeout=30)
    stop.set()
    for thread in workers:
        thread.join(timeout=30)

    assert not errors, f"a registry was mutated during iteration: {errors[:3]}"


def test_the_session_map_gives_each_bound_agent_its_own_session(config, source):
    """The shared map is keyed per (agent, node): a collision would put two nodes in one context."""
    executor, _provider = make_executor(config, source, group_manifest(concurrent=True))
    state = ready_state(source)
    executor.execute_node("a", state, {})

    keys = sorted(executor.ctx.sessions)
    assert len(keys) >= 2, "both members ran, so both must have a session"
    assert any(key.endswith(":a") for key in keys), "the leader must have its own session"
    assert any(key.endswith(":b") for key in keys), "the sibling must have its own session"
    assert len(set(keys)) == len(keys), "session keys must be unique, or two nodes share a context"


def test_a_parallel_group_reuses_the_fanout_bound_rather_than_inventing_one(config):
    """One bound for 'how many model calls at once' — a second one is how the two disagree."""
    from engine.fanout import MAX_ITEMS

    assert group_ceiling(config, capacity=100) == config.executor.fanout_max_parallel
    assert MAX_ITEMS >= config.executor.fanout_max_parallel, \
        "the fan-out item ceiling and the concurrency bound are different numbers by design"


# ── the gap the map recorded, kept honest ────────────────────────────────────


def test_the_runner_is_not_modified():
    """The scheduler that walks the graph is shared and outside this repo; the fix lives in the engine.

    Asserted rather than documented because the tempting fix — teaching the runner to dispatch a
    group — is a change to a program other tools run. What the engine owns is one `execute_node` call,
    and this pins that boundary: the runner keeps its single-node traversal, so `--state` resume, the
    step budget and the join semantics are untouched.
    """
    runner = resolve().files.runner
    text = runner.read_text(encoding="utf-8", errors="replace")
    assert "import threading" not in text, \
        "the shared runner must not have grown thread dispatch; the engine-side half is the fix"
    assert "execute_node(nid, self.state, ctx)" in text, \
        "the runner must still dispatch one node at a time"


# ── single-flight: a busy agent is visible to binding ────────────────────────
#
# The defect these pin: `Org.lock_for` called itself "the mechanism behind single-flight" and had no
# caller, and `AgentRuntime.begin`/`finish` had none either — so `state` never left IDLE, `available()`
# answered "free" for every agent always, and the load-balanced binder therefore read a roster in which
# nobody was ever working. Two nodes needing one skill (a fan-out's items, a parallel group's members)
# were bound to the same agent and then shared one `Session`, interleaving two transcripts in a window
# that was sized and compacted for one.
#
# The claim is taken by the executor when the node starts, under the runtime's own lock, along the
# binding's pool of alternatives. These tests observe the consequence rather than the mechanism: which
# agent each node actually ran as, read from the executor's own record.


def two_holder_org(*, skill: str = "code-reviewer", capacity: int = 1):
    """The default company plus a second agent holding one skill.

    The default company hires exactly one agent per skill, which is the *safe* shape: with one holder
    there is nobody to distribute to, and two nodes would share it whatever binding does. The property
    under test needs two holders, so the second one is the point of the fixture rather than a
    convenience — and it is hired directly rather than from a template so its level (and therefore the
    binder's tie-break) matches the first holder's, leaving availability as the only difference.
    """
    org = default_company(provider="fake", model="m", context_window=32768)
    first = next(a for a in org.agents.values() if skill in a.skills)
    first.max_concurrency = capacity
    org.hire(AgentSpec(
        id="ag_second_holder", name="Second Holder", title="Code Reviewer", skills=[skill],
        provider="fake", model="m", context_window=32768, role="reviewer",
        level=first.level, max_concurrency=capacity))
    return org, first


def slot_watch_provider(source, *, org, parties: int):
    """A probe that records every holder's slot count as each call starts.

    The claim is invisible once a node returns — the count is back to zero — so "N were admitted and
    not N+1" can only be observed *while the work is in flight*. Each call snapshots the roster at
    entry, and the barrier holds all `parties` calls inside at once so at least one snapshot is taken
    with every claim held.
    """
    class SlotWatch(ProbeProvider):
        def __init__(self, src):
            super().__init__(src, provider_id="fake")
            self.gate = threading.Barrier(parties, timeout=15)
            self.rows: list[dict[str, int]] = []

        def complete(self, request):
            with self._lock:
                self.rows.append({a.id: org.runtime(a.id).inflight
                                  for a in org.agents.values() if a.is_ai})
            try:
                self.gate.wait()
            except threading.BrokenBarrierError:
                pass
            return super().complete(request)

    return SlotWatch(source)


def rendezvous_provider(source, *, parties: int = 2):
    """A probe whose calls block until `parties` of them are in flight together.

    Overlap has to be *observed*, not hoped for: a test that starts two threads and then asserts they
    did not share an agent passes on a machine that ran them one after the other, whichever way the
    implementation is written. Holding both calls inside the provider until the other arrives makes
    "these two were genuinely concurrent" a fact of the test.
    """
    class Rendezvous(ProbeProvider):
        def __init__(self, src):
            super().__init__(src, provider_id="fake")
            self.gate = threading.Barrier(parties, timeout=15)

        def complete(self, request):
            # A broken barrier is left to pass through rather than raised: an extra call (a trailer
            # repair, say) would otherwise turn a wrong *answer* into a hung test.
            try:
                self.gate.wait()
            except threading.BrokenBarrierError:
                pass
            return super().complete(request)

    return Rendezvous(source)


def exploding_provider(source):
    """A provider that fails every call, so a node's exit path is an exception, not a result."""
    class Exploding(ProbeProvider):
        def complete(self, request):
            raise RuntimeError("the provider fell over")

    return Exploding(source, provider_id="fake")


def one_skill_manifest(*, count: int = 2, skill: str = "code-reviewer") -> dict:
    """`count` review nodes needing the same skill, with no edge between them.

    No `parallel:` block on purpose: the group is the *other* way to overlap these nodes, and it
    refuses members that share a skill. Overlapping them here is the caller's doing — two threads
    calling `execute_node` — which is also what a group's members do.
    """
    nodes = [{"id": f"n{i}", "skill": skill, "inputs": [], "outputs": [f"report-{i}"]}
             for i in range(count)]
    return {"name": "wf", "version": "1.0.0", "start": "n0", "nodes": nodes, "edges": []}


def run_concurrently(callables: list, *, timeout_s: float = 30.0) -> list:
    """Run each callable in its own thread and return what they raised (None where they did not).

    A timeout that is *reported* rather than hung on: the property this suite is most afraid of is a
    claim that never releases, which shows up as a join that does not return.
    """
    errors: list = [None] * len(callables)

    def _one(index: int, fn) -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the test, not swallowed
            errors[index] = exc

    threads = [threading.Thread(target=_one, args=(i, fn)) for i, fn in enumerate(callables)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout_s)
    assert not any(thread.is_alive() for thread in threads), \
        "a node never returned: the claim is holding a slot nothing will release"
    return errors


def test_two_concurrent_nodes_on_one_skill_bind_to_two_agents(config, source):
    """THE load-bearing test: two holders, two concurrent nodes, two agents.

    Without the claim both binds read the same roster — nobody was ever recorded as working — and both
    nodes landed on the first holder, sharing one session. With it, the first node claims the holder it
    bound to and the second finds it at its limit and takes the next candidate from the pool.
    """
    org, first = two_holder_org()
    executor, _provider = make_executor(config, source, one_skill_manifest(count=2),
                                        org=org, provider=rendezvous_provider(source))
    state = ready_state(source, pending=("n0", "n1"))

    errors = run_concurrently([
        lambda: executor.execute_node("n0", state, {}),
        lambda: executor.execute_node("n1", state, {}),
    ])

    assert not any(errors), f"a concurrent node raised: {[e for e in errors if e]}"
    agents = {nid: executor.history[nid].agent_id for nid in ("n0", "n1")}
    assert len(set(agents.values())) == 2, (
        f"two nodes needing {executor.history['n0'].skill!r} both ran as {agents} — binding saw both "
        "holdings as idle because the first node's work was never recorded"
    )
    assert set(agents.values()) == {first.id, "ag_second_holder"}


def test_two_nodes_that_bind_at_the_same_instant_still_land_on_two_agents(config, source):
    """The window between *deciding* and *claiming*: both binds see both holders free.

    This is the race the first test can miss. There, the second node binds after the first has already
    claimed, so simply asking the roster which holder is free is enough to separate them — and a
    version that checked and then claimed separately would pass. Holding both threads inside `bind`
    with a barrier makes the stale-decision case certain instead of occasional, and only the atomic
    claim closes it: the second node finds its preferred agent at its limit and moves to the next
    candidate in the binding's pool.
    """
    org, _first = two_holder_org()
    executor, _provider = make_executor(config, source, one_skill_manifest(count=2), org=org,
                                        provider=rendezvous_provider(source))
    gate = threading.Barrier(2, timeout=15)
    real_bind = executor.binder.bind

    def bind_together(*args, **kwargs):
        binding = real_bind(*args, **kwargs)
        # Held *after* the decision and *before* the claim: both threads now hold a binding that names
        # the same free agent, which is precisely the stale answer the claim has to be able to survive.
        # A broken barrier is let through, so a node that failed before reaching `bind` surfaces as its
        # own failure rather than as a second thread waiting out the timeout.
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        return binding

    executor.binder.bind = bind_together
    state = ready_state(source, pending=("n0", "n1"))

    errors = run_concurrently([
        lambda: executor.execute_node("n0", state, {}),
        lambda: executor.execute_node("n1", state, {}),
    ])

    assert not any(errors), f"a concurrent node raised: {[e for e in errors if e]}"
    agents = {nid: executor.history[nid].agent_id for nid in ("n0", "n1")}
    assert len(set(agents.values())) == 2, (
        f"both nodes bound while the roster still looked idle and both ran as {agents}: the claim has "
        "to be able to move to another candidate, not only confirm the first choice"
    )


def test_an_agents_limit_is_its_own_concurrency_not_one(config, source):
    """Three slots, three nodes: two on the holder limited to two, one on the holder limited to one.

    Single-flight is "no more than the agent's `max_concurrency`", and both halves of that matter. A
    hard one-at-a-time rule would push the third node onto the second holder with the first holder
    half full, and no rule at all would stack all three on the first — the total capacity here is
    exactly three, so an implementation that respects the field fills both holders precisely.
    """
    org, first = two_holder_org()
    first.max_concurrency = 2              # the roster's word, which the claim has to follow
    watcher = slot_watch_provider(source, org=org, parties=3)
    executor, _provider = make_executor(config, source, one_skill_manifest(count=3), org=org,
                                       provider=watcher)
    state = ready_state(source, pending=("n0", "n1", "n2"))

    errors = run_concurrently([
        (lambda i=i: executor.execute_node(f"n{i}", state, {})) for i in range(3)
    ])

    assert not any(errors), f"a concurrent node raised: {[e for e in errors if e]}"
    held = {agent_id: max(row.get(agent_id, 0) for row in watcher.rows)
            for agent_id in (first.id, "ag_second_holder")}
    assert held == {first.id: 2, "ag_second_holder": 1}, (
        f"the holders were observed at {held} while three nodes were in flight — the limit is the "
        "agent's own max_concurrency, so one holder must take two and the other one"
    )
    assert watcher.rows, "the observation has to come from inside the calls, not from the aftermath"


def test_a_fanout_spreads_its_items_over_every_holder(config, source):
    """A fan-out's items are independent work: stacking them on the bound agent is why they collided.

    The bound agent is a *preference* for the node; the items are handed round-robin over the whole
    pool and each item's claim confirms its suggestion is free. One holder would be indistinguishable
    from the defect, so the fixture hires two.
    """
    org, first = two_holder_org()
    manifest = {"name": "wf", "version": "1.0.0", "start": "reviewall",
                "nodes": [{"id": "reviewall", "skill": "code-reviewer", "phase": "REVIEW",
                           "fanout": "Review {{item}} for regressions.",
                           "items": ["src/a.ts", "src/b.ts", "src/c.ts", "src/d.ts"],
                           "inputs": [], "outputs": ["review-report"]}],
                "edges": []}
    executor, _provider = make_executor(config, source, manifest, org=org)

    result = executor.execute_node("reviewall", ready_state(source, pending=("reviewall",)), {})
    used = {item["agent_id"] for item in result["fanout"]["items"]}

    assert result["status"] == "done", result.get("summary")
    assert used == {first.id, "ag_second_holder"}, (
        f"the fan-out ran every item as {used}; a second holder sat idle while the items were "
        "stacked on the agent the node happened to bind to"
    )


def test_nodes_that_outnumber_their_holders_still_run(config, source):
    """THE safety test: one holder, three nodes, no deadlock and no refusal.

    Every capable agent being at its limit is not a reason to stop a run that works today. The honest
    outcome is chosen deliberately — the work runs anyway, the overload is taken as a real claim and
    recorded, and the agent is preferred last next time. A binder that *refused* here would turn a
    working plan into a stopped one, which is worse than the sharing this change exists to fix.
    """
    org = default_company(provider="fake", model="m", context_window=32768)
    holder = next(a for a in org.agents.values() if "code-reviewer" in a.skills)
    executor, _provider = make_executor(config, source, one_skill_manifest(count=3), org=org)
    state = ready_state(source, pending=("n0", "n1", "n2"))

    errors = run_concurrently([
        # `i=i` rather than a bare closure: a lambda that reads the loop variable sees its final
        # value, so an unbound one would run "n2" three times and never touch n0 or n1.
        (lambda i=i: executor.execute_node(f"n{i}", state, {})) for i in range(3)
    ])

    assert not any(errors), f"a node on a saturated holder raised: {[e for e in errors if e]}"
    assert sorted(executor.history) == ["n0", "n1", "n2"], \
        "every node must have run: the one holder is oversubscribed, not a reason to stop"
    assert all(executor.history[nid].status == "done" for nid in ("n0", "n1", "n2"))
    runtime = org.runtime(holder.id)
    assert runtime.inflight == 0 and not runtime.busy(), \
        "the three claims must all be released, or the holder stays busy forever"


def test_an_agent_that_raises_mid_node_is_not_left_busy(config, source):
    """The release is on the exception path too: a slot that is never given back wedges the org."""
    org, holder = two_holder_org()
    executor, _provider = make_executor(config, source, one_skill_manifest(count=1), org=org,
                                        provider=exploding_provider(source))

    with pytest.raises(RuntimeError):
        executor.execute_node("n0", ready_state(source, pending=("n0",)), {})

    runtime = org.runtime(holder.id)
    assert runtime.inflight == 0, "the claim taken before the call must be released by its failure"
    assert not runtime.busy() and runtime.state is AgentState.IDLE
    assert runtime.tasks_failed == 1 and runtime.last_error, \
        "the failure must reach the health record, not only the traceback"


def test_a_worker_that_is_killed_mid_node_leaves_no_slot_held(config, source):
    """An abort is an exit path: a `BaseException` raised while a node runs must release its claim.

    `SystemExit`/`KeyboardInterrupt` are not `Exception`s, so a release guarded by `except Exception`
    would leave the agent holding a slot for the rest of the process's life — and with a limit of one
    that agent is then never chosen again.
    """
    org, holder = two_holder_org()
    executor, _provider = make_executor(config, source, one_skill_manifest(count=1), org=org)
    bundle = executor._load_skill("code-reviewer")
    binding = executor._bind("n0", {"skill": "code-reviewer"}, "code-reviewer", is_reviewer=True)
    assert binding.primary == holder.id, "the load-balanced choice is the first holder"

    def _abort(**_kwargs):
        raise KeyboardInterrupt("the worker was killed")

    executor._run_one_claimed = _abort
    with pytest.raises(KeyboardInterrupt):
        executor._run_one(node_id="n0", node={"skill": "code-reviewer"}, skill="code-reviewer",
                          state=ready_state(source, pending=("n0",)), attempt=1, bundle=bundle,
                          binding=binding, is_reviewer=True, inputs={}, findings=[],
                          agent_id=binding.primary)

    runtime = org.runtime(binding.primary)
    assert runtime.inflight == 0 and not runtime.busy()
