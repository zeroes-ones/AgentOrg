#!/usr/bin/env python3
"""Phase 41 tests — an unattended goal finishes alone instead of parking on a recoverable refusal.

The reported defect, reproduced against `Projects/Ideas` minutes before this file was written:

    python3 -m engine.cli run --project .../Ideas --goal "Produce deep research documentation …" \\
        --posture unattended --slug wide-market-deep-research

    outcome : gated   phase : awaiting_gate
    GATE: pm — handoff propose refused: R6: 9 open questions exceed the 3 ceiling.

One node had run. Every other node was still `pending`. A payload problem — nine open questions
where the contract allows three — had parked the whole run on a person, in a run explicitly told it
would not need one.

Four facts composed into that, and each is asserted below rather than described:

1. The gate detector minted a **human gate from any node whose status was `needs_review`**, and read
   the manifest only for the wording of a gate it had already decided to raise. A contract refusal
   and a declared human gate were therefore indistinguishable to it.
2. The node was **not inside the manifest's rework loop**, so the runner's retry machinery never saw
   it: outside a loop a contract violation escalated on the first refusal.
3. There was **no retry at all**, so nothing carried the fired rule back to the node — and a retry
   that repeats the identical prompt fails identically, which is why a window without that feedback
   would be a slower way to fail.
4. Even with a window, R6 counts the questions the run has **accumulated**, not just the ones this
   attempt raised. A retry that left the discarded attempt's questions in run-state would be refused
   by the same rule with the same number for ever.

The rules are correct and stay correct. What changes is *who recovers*: an unattended goal repairs
its own payload within a bound; a supervised one still asks; and a genuine contract violation still
parks, with the rule named, after the bound is spent.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load
from engine.goal import Goal, GoalPolicy
from engine.library import resolve
from engine.library import LibraryError
from engine.orchestrator import GateRequest, Orchestrator, Run, RunPhase
from engine.org.handoff import MAX_OPEN_QUESTIONS
from engine.planner import emit_safe_yaml
from engine.state import Workspace

EXAMPLE = ROOT / "credentials.example.json"


# ── fixtures: a two-node graph where the first node can be made to refuse ─────
#
# `pm` is an ordinary work node with one outgoing edge guarded by `pm.status == done`; `release` is
# the one *declared* human gate. That is the composed plan's shape in miniature, and it is what makes
# every assertion below non-vacuous: the same `needs_review` status must read differently depending
# on which of the two nodes carries it.

MANIFEST = {
    "name": "autonomous-recovery",
    "version": "1.0.0",
    "description": "A run that must recover from its own refused payload",
    "payloads": {"handoff-v1": ["status", "summary"]},
    "start": "pm",
    "nodes": [{"id": "pm", "skill": "product-manager", "outputs": ["product-spec"]}],
    "gates": [{"id": "release", "type": "gate", "kind": "human", "requires": ["product-spec"],
               "description": "Owner release approval"}],
    "edges": [{"from": "pm", "to": "release", "when": "pm.status == done",
               "payload": "handoff-v1"}],
    "end": ["release"],
}

#: The refusal the real run produced, verbatim in shape: the rule, the count, and the ceiling the
#: contract declares. The count is derived from `MAX_OPEN_QUESTIONS` rather than written as 9, so this
#: fixture cannot drift away from the rule it is exercising.
def _refusal(count: int) -> str:
    return (f"handoff propose refused: R6: {count} open questions exceed the "
            f"{MAX_OPEN_QUESTIONS} ceiling. Resolve them, or escalate to the Owner — do not hand off "
            "compounding uncertainty.")


#: A stub executor that refuses `pm` until run-state's questions are at or below the ceiling, then
#: reports `done` — the honest shape of a node that fixes its payload. `release` is the declared gate.
STUB = '''import json


def execute_node(node_id, state, ctx):
    if node_id == "release":
        return {"status": "needs_review", "verdict": "awaiting_owner",
                "summary": "human gate reached", "evidence": ["gate:release"]}
    rework = state.get("_contract_rework") or {}
    open_questions = state.get("open_questions") or []
    if len(open_questions) > %(limit)d and not rework:
        raise SystemExit
    if len(open_questions) > %(limit)d:
        return {"status": "needs_review", "verdict": "contract-violation",
                "summary": "handoff propose refused: R6: %%d open questions exceed the %(limit)d "
                           "ceiling" %% len(open_questions)}
    return {"status": "done", "verdict": "ok", "summary": "spec produced",
            "evidence": ["docs/spec.md#abc"],
            "artifacts": [{"name": "product-spec", "path": "docs/spec.md", "sha": "abc",
                           "type": "doc"}]}
'''

#: The refusal is produced by the *node*, which is the path the real run took: the executor's handoff
#: layer refuses the payload and merges the rule into the node's result. The runner sees a
#: `contract-violation` verdict with no `contract` log action of its own — which is why the runner's
#: recovery has to read the result as well as its own contract check.


@pytest.fixture
def config():
    return load(EXAMPLE)


@pytest.fixture
def library():
    return resolve()


def _project(tmp_path, *, refuser: bool = True, extra_questions: int = 5):
    """A workspace with the fixture manifest and a stub executor that can refuse once."""
    root = tmp_path / "projects"
    ws = Workspace.for_project("autonomous-recovery", root=root)
    ws.ensure()
    (ws.path / "autonomous-recovery.yaml").write_text(emit_safe_yaml(MANIFEST))
    (ws.path / "stub.py").write_text(STUB % {"limit": MAX_OPEN_QUESTIONS})
    return ws


def _orch(config, library, ws):
    return Orchestrator(config=config, library=library, workspace=ws)


def _gate_for(orch, ws, *, node: str = "pm", verdict: str, status: str = "needs_review",
              phase: str = "escalated") -> GateRequest | None:
    """Run the detector over a checkpoint in which `node` carries the given status.

    `node` is named rather than fixed, because the whole change is that the *same* status reads
    differently depending on which node carries it: an ordinary work node versus a declared gate.
    `phase` is named for the same reason — a refused node that the rework is still working through
    is not a stop, and `execute` is the runner's own word for "still running".
    """
    orch._run = orch.adopt(ws.path / "autonomous-recovery.yaml", slug="autonomous-recovery")
    return orch._detect_gate({"phase": phase,
                              "nodes": {node: {"status": status, "verdict": verdict}}})


# ── 1. a stalled node is not a gate ──────────────────────────────────────────


def test_a_contract_refusal_at_an_ordinary_node_is_not_a_human_gate(config, library, tmp_path):
    """The defect itself. `needs_review` at a node that is not a declared gate is a stalled node.

    Before this, the detector raised a `kind: human` gate from any `needs_review` record and used the
    manifest only to word it — so a fixable payload problem at node one reached a person as a
    decision, in a run that had been told it would not need one.

    The node must still be *running* here. A refusal the bounded rework is actively repairing is not
    a stop, and gating it would gate a run that is about to fix itself.
    """
    ws = _project(tmp_path)
    orch = _orch(config, library, ws)
    detected = _gate_for(orch, ws, node="pm", verdict="contract-violation", phase="execute")
    assert detected is None or detected.kind != "human", (
        "a refused payload being repaired must not park the run as a human gate")


def test_a_stopped_contract_refusal_still_parks_as_a_human_gate(config, library, tmp_path):
    """The honest failure, preserved: once the run has stopped, the refusal is a person's decision.

    This is the behaviour the track required be kept. The window changes *who recovers while the run
    is moving*; it does not turn a genuine, unresolved contract violation into a silent pass. The
    gate is named for the node and carries the rule, exactly as before.
    """
    ws = _project(tmp_path)
    orch = _orch(config, library, ws)
    orch._run = orch.adopt(ws.path / "autonomous-recovery.yaml", slug="autonomous-recovery")
    detected = orch._detect_gate({
        "phase": "escalated",
        "nodes": {"pm": {"status": "needs_review", "verdict": "contract-violation",
                         "summary": _refusal(9)}},
        "log": [{"node": "pm", "action": "escalate",
                 "detail": "contract rework exhausted after 3 attempt(s)"}],
    })
    assert detected is not None, "a refusal that ended the run must still reach a person"
    assert detected.gate_id == "pm"
    assert detected.kind == "human"
    assert "R6" in detected.reason or "open questions" in detected.reason


def test_a_stopped_refusal_reported_by_the_node_itself_still_parks(config, library, tmp_path):
    """The route the real run took: the node reports the refusal, so the log holds no `contract` entry.

    A check keyed on the runner's own log alone would have missed the case this whole track exists
    for — the log of the reproduced run held a single `done` entry.
    """
    ws = _project(tmp_path)
    orch = _orch(config, library, ws)
    orch._run = orch.adopt(ws.path / "autonomous-recovery.yaml", slug="autonomous-recovery")
    detected = orch._detect_gate({
        "phase": "escalated",
        "nodes": {"pm": {"status": "needs_review", "verdict": "contract-violation",
                         "summary": _refusal(9)}},
        "log": [{"node": "pm", "action": "done", "detail": "verdict=contract-violation"}],
    })
    assert detected is not None and detected.gate_id == "pm"


def test_a_declared_human_gate_still_parks(config, library, tmp_path):
    """The regression guard: a genuinely declared gate is still a question for a person."""
    ws = _project(tmp_path)
    orch = _orch(config, library, ws)
    detected = _gate_for(orch, ws, node="release", verdict="awaiting_owner")
    assert detected is not None, "a declared human gate must still park the run"
    assert detected.kind == "human"
    assert detected.gate_id == "release"


def test_the_same_status_reads_differently_at_a_gate_than_at_a_work_node(config, library, tmp_path):
    """The distinction under test, asserted directly rather than inferred from two fixtures.

    `needs_review` with no verdict is ambiguous on its own — a model saying "I am not done", an
    uncovered criterion, and a refused handoff all produce it. While the run is still moving, what
    resolves it is the manifest: a gate node parks, an ordinary work node does not.
    """
    ws = _project(tmp_path)
    orch = _orch(config, library, ws)
    at_gate = _gate_for(orch, ws, node="release", verdict="", phase="execute")
    at_work = _gate_for(orch, ws, node="pm", verdict="", phase="execute")
    assert at_gate is not None and at_gate.gate_id == "release"
    assert at_work is None, "the same status at a work node is a stall, not a question"


def test_a_released_gate_is_still_not_waiting(config, library, tmp_path):
    """The prior fix must survive this one: `done` beats the verdict it still carries."""
    ws = _project(tmp_path)
    orch = _orch(config, library, ws)
    orch._run = orch.adopt(ws.path / "autonomous-recovery.yaml", slug="autonomous-recovery")
    assert orch._detect_gate({"nodes": {"release": {"status": "done",
                                                    "verdict": "awaiting_owner"}}}) is None


# ── 2. the posture decides the window ────────────────────────────────────────


def test_an_unattended_goal_gets_a_rework_window(config, library, tmp_path):
    ws = _project(tmp_path)
    orch = _orch(config, library, ws)
    orch._goal = Goal.new("produce the spec", policy=GoalPolicy(posture="unattended"))
    assert orch._contract_rework_attempts(None) == config.executor.contract_rework > 0


def test_a_supervised_goal_gets_no_window(config, library, tmp_path):
    """`--posture supervised` must park immediately, exactly as it does today."""
    ws = _project(tmp_path)
    orch = _orch(config, library, ws)
    orch._goal = Goal.new("produce the spec", policy=GoalPolicy(posture="supervised"))
    assert orch._contract_rework_attempts(None) == 0


def test_the_window_width_comes_from_the_config(config, library, tmp_path):
    """One number to tune, in the section every other node-execution knob lives in."""
    assert config.executor.contract_rework >= 1
    ws = _project(tmp_path)
    orch = _orch(config, library, ws)
    orch._goal = Goal.new("produce the spec", policy=GoalPolicy(posture="unattended"))
    config.executor.contract_rework = 5
    try:
        assert orch._contract_rework_attempts(None) == 5
    finally:
        config.executor.contract_rework = 3


def test_the_config_refuses_a_negative_window():
    from engine.config import ConfigError, ExecutorConfig

    with pytest.raises(ConfigError, match="contract_rework"):
        ExecutorConfig(contract_rework=-1)


# ── 3. the runner grants a bounded window, and carries the rule back ─────────


@pytest.fixture(scope="module")
def runner():
    """The library's own runner, loaded the way the host loads it."""
    import importlib.util

    path = resolve().files.runner
    spec = importlib.util.spec_from_file_location("_phase41_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _substantiated() -> dict:
    """A result that satisfies `idea-to-spec`'s own completion contract.

    Needed because the runner's contract check and the handoff contract are *two* refusal sources. A
    stub that breaks both at once would let the window pass while proving the wrong one — and R6 is a
    handoff rule, so it is the only one this file means to exercise here.
    """
    from engine.library import resolve as _resolve
    from engine.skills import FilesystemSkillSource

    criteria = FilesystemSkillSource(_resolve()).load("idea-to-spec").contract.criteria
    return {"evidence": ["checked"],
            "criteria_met": [f"c{i}" for i in range(1, len(criteria) + 1)]}


def _run_stub(runner, manifest, executor, *, rework: int, max_steps: int = 200):
    """Drive the library runner directly, as `RunnerHost` does — same contract, no subprocess."""
    text = json.dumps(manifest, sort_keys=True)
    state = runner.fresh_state(manifest, text, max_steps)
    stub = type("E", (), {"execute_node": staticmethod(executor)})()
    machine = runner.Runner(manifest, stub, state, max_steps, enforce_contracts=True,
                            contract_rework=rework)
    return machine.run(), state


def _always_refusing(limit: int):
    substantiated = _substantiated()

    def execute(node_id, state, ctx):
        state["open_questions"] = [{"question": f"q{i}"} for i in range(limit + 2)]
        return {"status": "needs_review", "verdict": "contract-violation",
                "summary": _refusal(len(state["open_questions"])), **substantiated}

    return execute


def runner_manifest(name: str, node: str) -> dict:
    return {"name": name, "version": "1.0.0", "description": "rework probe",
            "payloads": {"handoff-v1": ["status", "summary"]},
            "start": node,
            "nodes": [{"id": node, "skill": "idea-to-spec"}],
            "edges": [], "loops": [], "gates": [], "parallel": [],
            "end": [node]}


def test_a_contract_refusal_outside_a_loop_is_retried(runner):
    """The core of the track: the refusal is repaired rather than escalated."""
    substantiated = _substantiated()

    def seed_then_run(node_id, state, ctx):
        if not ctx.get("contract_rework"):
            state["open_questions"] = [{"question": f"q{i}"} for i in range(MAX_OPEN_QUESTIONS + 2)]
            return {"status": "needs_review", "verdict": "contract-violation",
                    "summary": _refusal(len(state["open_questions"])), **substantiated}
        del state["open_questions"][MAX_OPEN_QUESTIONS:]
        return {"status": "done", "verdict": "pass", **substantiated}

    summary, state = _run_stub(runner, runner_manifest("t-rework-ok", "pm"),
                               seed_then_run, rework=2)
    assert summary["outcome"] == "complete"
    assert state["nodes"]["pm"]["status"] == "done"


def test_the_rework_window_is_bounded_and_then_escalates(runner):
    """The honest failure: a genuine violation still parks, after the bound is spent, rule named."""
    summary, state = _run_stub(runner, runner_manifest("t-rework-bound", "pm"),
                               _always_refusing(MAX_OPEN_QUESTIONS), rework=2)
    reworks = [e for e in state["log"] if e.get("action") == "contract-rework"]
    assert len(reworks) == 2, f"the window must be bounded, got {len(reworks)} attempts"
    assert summary["outcome"] == "contract-violation"
    assert state["nodes"]["pm"]["status"] == "needs_review"
    escalated = [e for e in state["log"] if e.get("action") == "escalate"]
    assert escalated, "exhaustion must be logged as an escalate, so the stop reason has a cause"


def test_a_window_of_zero_retries_nothing(runner):
    """`0` is exactly the old behaviour — the posture switch has to be able to turn it off."""
    summary, state = _run_stub(runner, runner_manifest("t-rework-off", "pm"),
                               _always_refusing(MAX_OPEN_QUESTIONS), rework=0)
    assert [e for e in state["log"] if e.get("action") == "contract-rework"] == []
    assert summary["outcome"] == "contract-violation"


def test_the_retry_is_told_the_rule_and_which_questions_broke_the_ceiling(runner):
    """A retry that repeats the identical prompt fails identically, so the rule must travel.

    This is the half that makes the window worth having: the refusal's own words — the rule id, the
    ceiling, and the questions that exceeded it — reach the node's next prompt.
    """
    substantiated = _substantiated()
    seen: dict = {}

    def capture(node_id, state, ctx):
        if ctx.get("contract_rework"):
            seen.update(ctx["contract_rework"])
        else:
            state["open_questions"] = [{"question": f"q{i}"} for i in range(MAX_OPEN_QUESTIONS + 2)]
        if not ctx.get("contract_rework"):
            return {"status": "needs_review", "verdict": "contract-violation",
                    "summary": _refusal(len(state["open_questions"])), **substantiated}
        del state["open_questions"][MAX_OPEN_QUESTIONS:]
        return {"status": "done", "verdict": "pass", **substantiated}

    summary, _ = _run_stub(runner, runner_manifest("t-rework-context", "pm"), capture, rework=2)
    assert seen.get("rule") == "R6"
    assert seen.get("open_question_limit") == MAX_OPEN_QUESTIONS
    assert seen.get("open_questions"), "the retry must be told which questions to cut down"
    assert summary["outcome"] == "complete"


def test_the_discarded_attempts_questions_are_withdrawn_before_the_retry(runner):
    """R6 counts the run's accumulated pile, so a retry that kept them would fail for ever.

    The window exists to let a node *reduce* the list. If the refusal's own questions stayed in
    run-state, the retry would face the identical number and the identical rule — a loop, not a
    rework — which is why the withdrawal is part of the mechanism rather than a courtesy.
    """
    substantiated = _substantiated()
    seen: dict = {}

    def capture(node_id, state, ctx):
        if ctx.get("contract_rework"):
            seen["questions_at_retry"] = list(state.get("open_questions") or [])
        else:
            state["open_questions"] = [{"question": f"q{i}"} for i in range(MAX_OPEN_QUESTIONS + 4)]
            return {"status": "needs_review", "verdict": "contract-violation",
                    "summary": _refusal(len(state["open_questions"])), **substantiated}
        return {"status": "done", "verdict": "pass", **substantiated}

    summary, state = _run_stub(runner, runner_manifest("t-rework-withdraw", "pm"),
                               capture, rework=2)
    assert seen["questions_at_retry"] == [], (
        "the refused attempt's own questions must be withdrawn before the retry runs")
    assert summary["outcome"] == "complete"
    assert len(state.get("open_questions") or []) <= MAX_OPEN_QUESTIONS


def test_the_repair_context_is_cleared_so_it_cannot_leak_to_the_next_node(runner):
    """A stale repair block would tell an innocent node to fix a refusal that was never about it."""
    substantiated = _substantiated()

    def capture(node_id, state, ctx):
        if not ctx.get("contract_rework"):
            state["open_questions"] = [{"question": f"q{i}"} for i in range(MAX_OPEN_QUESTIONS + 2)]
            return {"status": "needs_review", "verdict": "contract-violation",
                    "summary": _refusal(len(state["open_questions"])), **substantiated}
        del state["open_questions"][MAX_OPEN_QUESTIONS:]
        return {"status": "done", "verdict": "pass", **substantiated}

    _, state = _run_stub(runner, runner_manifest("t-rework-clear", "pm"), capture, rework=2)
    assert "_contract_rework" not in state


def test_a_loop_member_is_unaffected_by_the_outside_the_loop_window(runner):
    """The loop's own machinery already retried; the new window must not double-drive it."""
    calls = {"n": 0}

    def never(node_id, state, ctx):
        calls["n"] += 1
        return {"status": "done", "verdict": "changes_requested", "evidence": [f"pass-{ctx['pass']}"]}

    manifest = {"name": "t-rework-loop", "version": "1.0.0", "description": "loop",
                "payloads": {"handoff-v1": ["status"]}, "start": "a",
                "nodes": [{"id": "a", "skill": "idea-to-spec"},
                          {"id": "b", "skill": "code-reviewer"}],
                "edges": [], "parallel": [], "gates": [],
                "loops": [{"id": "rf", "nodes": ["a"], "exit_when": "a.verdict == pass",
                           "max_iterations": 2}],
                "end": ["a"]}
    summary, state = _run_stub(runner, manifest, never, rework=2, max_steps=50)
    assert not [e for e in state["log"] if e.get("action") == "contract-rework"], (
        "a loop member's retries are the loop's, not the outside-the-loop window's")


# ── 4. the refusal still reaches the stop reason ─────────────────────────────


def test_the_stop_reason_still_names_the_rule_after_exhaustion(runner):
    """Keeping the honest failure means the reason survives, not just the phase."""
    from engine.orchestrator import _derive_stop_reason

    class Outcome:
        killed = False
        error = ""

    summary, state = _run_stub(runner, runner_manifest("t-rework-reason", "pm"),
                               _always_refusing(MAX_OPEN_QUESTIONS), rework=2)
    state["outcome"] = summary["outcome"]
    reason = _derive_stop_reason(state, Outcome(), {})
    assert "pm" in reason
    assert "contract" in reason or "R6" in reason


# ── 5. the executor renders the refusal into the prompt ──────────────────────


def test_the_prompt_carries_the_contract_repair_block():
    """The refusal has to reach the model, or the window spends its budget re-proving attempt one."""
    from engine.prompts import PromptBuilder, TaskContext
    from engine.skills import FilesystemSkillSource
    from engine.library import resolve as _resolve

    bundle = FilesystemSkillSource(_resolve()).load("idea-to-spec")
    task = TaskContext(
        node_id="pm", instruction="Produce the spec.", attempt=2, max_attempts=2,
        contract_rework={"reason": _refusal(9), "rule": "R6", "attempt": 1, "max_attempts": 2,
                         "open_question_limit": MAX_OPEN_QUESTIONS,
                         "open_questions": ["who is the buyer?", "what is the price?"]})
    prompt = PromptBuilder().node_prompt(bundle, task)
    assert "CONTRACT REPAIR" in prompt.body
    assert "R6" in prompt.body
    assert "who is the buyer?" in prompt.body, "the questions must be named, not just counted"


def test_no_repair_block_appears_on_a_first_attempt():
    from engine.prompts import PromptBuilder, TaskContext
    from engine.skills import FilesystemSkillSource
    from engine.library import resolve as _resolve

    bundle = FilesystemSkillSource(_resolve()).load("idea-to-spec")
    task = TaskContext(node_id="pm", instruction="Produce the spec.")
    prompt = PromptBuilder().node_prompt(bundle, task)
    assert "CONTRACT REPAIR" not in prompt.body


def test_a_completion_refusal_is_not_described_as_a_handoff_refusal():
    """The real reproduced run was refused by the *completion* contract, not by a handoff rule.

    A repair block that told that node it had "failed the handoff contract" would send it to fix a
    payload that was never the problem — the wrong half of the contract, named confidently. The two
    sources are distinguishable from the refusal text itself: a handoff rule names a rule id
    (`R6: …`), the completion contract names its own trailer fields (`completion.evidence …`).
    """
    from engine.prompts import PromptBuilder, TaskContext
    from engine.skills import FilesystemSkillSource
    from engine.library import resolve as _resolve

    bundle = FilesystemSkillSource(_resolve()).load("idea-to-spec")
    task = TaskContext(
        node_id="pm", instruction="Produce the spec.", attempt=2, max_attempts=2,
        contract_rework={
            "reason": "completion.evidence is 'required' but the node reported no evidence; "
                      "completion.criteria declares 3 criteria (c1, c2, c3) but the node reported "
                      "no criteria_met coverage",
            "rule": "", "attempt": 1, "max_attempts": 3})
    body = PromptBuilder().node_prompt(bundle, task).body
    assert "CONTRACT REPAIR" in body
    assert "completion.evidence" in body, "the offending clause must travel verbatim"
    assert "criteria_satisfied" in body, "the fix is a trailer edit, so the field must be named"
    assert "handoff contract" not in body, (
        "naming the wrong contract sends the node to fix a payload that was never the problem")


def test_the_executor_reads_the_repair_context_from_run_state():
    """The runner hands the executor `(node_id, state, ctx)`; only `state` is guaranteed to be the
    run's own record, which is why the context is written there as well as into `ctx`."""
    from engine.executor import NodeExecutor

    assert NodeExecutor._rework_context(None, {"_contract_rework": {"rule": "R6"}}) == {"rule": "R6"}
    assert NodeExecutor._rework_context(None, {}) == {}
    assert NodeExecutor._rework_context(None, {"_contract_rework": "not-a-mapping"}) == {}


# ── 6. the library's own capability assertion still passes ───────────────────


def test_the_host_asserts_the_rework_flag_the_runner_now_needs(library):
    """The flag is in the asserted surface, so a library that drops it fails at startup rather than
    silently degrading to a window that never opens."""
    library.assert_capabilities()
    assert "--contract-rework" in library.files.runner.read_text(encoding="utf-8")


def test_the_host_only_passes_the_flag_when_a_window_is_wanted(config, library, tmp_path):
    from engine.host import RunnerHost

    ws = _project(tmp_path)
    host = RunnerHost(config=config, library=library, workspace=ws)
    assert host.contract_rework() == config.executor.contract_rework
    assert host.with_contract_rework(0).contract_rework() == 0
    assert host.with_contract_rework(4).contract_rework() == 4


# ── 7. a stopped run says *why* it was stopped ───────────────────────────────
#
# Reproduced with a real goal against a local model:
#
#     python3 -u -m engine.cli run --goal "create a file notes.md …" --posture unattended
#     outcome : failed   phase : aborted   stopped : the run was aborted
#
# 901 seconds, no cause, no node named, no stderr — and `diagnostics.jsonl` holding only
# `run.prepared`, `run.approved`, `node.bind`, `node.tools.start` and `run.phase: aborted`. The host
# had stopped the run on the stall window (`stall_timeout_s: 900.0`), which is a *correct* decision for
# a 7B model slower than 900 s per reply. What was wrong is that four different events set the same
# `killed` flag and the summary had one sentence for all of them, so the person could not tell whether
# to raise the window, change the model, or look at their own abort.
#
# These are unit tests on the derivation: the sentence is what travels to the checkpoint, the CLI and
# the diagnostics log, and it is the thing that was useless.


class _StoppedOutcome:
    """The shape `_derive_stop_reason` reads: a `RunOutcome` after the host stopped the run."""

    def __init__(self, *, termination: str, detail: str = "") -> None:
        self.killed = True
        self.error = ""
        self.termination = termination
        self.termination_detail = detail


def _stopped_reason(termination: str, detail: str = "") -> str:
    from engine.orchestrator import _derive_stop_reason

    # An empty runner checkpoint: the state a stalled run leaves, because it never finished a node.
    return _derive_stop_reason({}, _StoppedOutcome(termination=termination, detail=detail), {})


def test_a_stall_abort_names_the_stall_and_the_window_that_expired():
    """The reason has to say *what* stopped the run and *which* window ran out.

    "the run was aborted" sends the reader to the checkpoint to guess. Naming the watchdog and the
    window is the difference between "raise `concurrency.stall_timeout_s`", "this model is too slow"
    and "I pressed Stop" — three different actions for three different people.
    """
    reason = _stopped_reason(
        "wedged",
        "no checkpoint, trace or output for 1800s, past the 1800s stall window "
        "(concurrency.stall_timeout_s)")

    assert "watchdog" in reason
    assert "1800" in reason, "the window that expired is the number a person has to change"
    assert "concurrency.stall_timeout_s" in reason, (
        "the setting has to be named, or the person still cannot find it")
    assert "Owner" not in reason, "a stall is not a person's decision"


def test_an_owner_abort_and_a_stall_give_different_reasons():
    """The defect in one assertion: four causes, one sentence.

    A person who pressed Stop and a person whose run was reaped by the watchdog had byte-identical
    summaries. They need opposite things — one wants to know their own action took effect, the other
    wants to know why a run they left alone vanished.
    """
    stall = _stopped_reason("wedged", "no checkpoint, trace or output for 1800s")
    owner = _stopped_reason("aborted")
    shutdown = _stopped_reason("shutdown", "the engine shut down and stopped the run")

    assert len({stall, owner, shutdown}) == 3, (
        f"each cause must read differently; got {stall!r} / {owner!r} / {shutdown!r}")
    assert "Owner" in owner
    assert "watchdog" not in owner
    assert "shut down" in shutdown


def test_a_shutdown_is_not_described_as_the_owners_decision():
    """The engine's own reap is the case a person is most likely to misread as "my run crashed".

    It is neither: the run was stopped because the process it lived in went away, and saying so is
    what stops someone hunting a bug in their plan for a run that never failed.
    """
    reason = _stopped_reason("shutdown", "the engine shut down and stopped the run")
    assert "aborted by the Owner" not in reason
    assert "watchdog" not in reason
    assert "shut down" in reason


def test_a_runner_that_died_before_it_could_be_signalled_is_not_called_an_abort():
    """`died` is the honest word when the process was gone before the signal could land.

    Reported as an abort, it would name an action nobody took — and the summary is a claim about what
    happened, so it must not invent one.
    """
    reason = _stopped_reason("died", "the runner exited before it could be signalled")
    assert "on its own" in reason
    assert "aborted by the Owner" not in reason


def test_a_kill_with_no_recorded_cause_keeps_the_old_wording():
    """A run stopped by a host that recorded nothing must not have a cause invented for it.

    The fallback is the phrase this whole change exists to stop *being* the answer — but it is the
    correct answer for an outcome that genuinely carries no cause (an older host, a test double), and
    a summary that guessed would be worse than one that admits it does not know.
    """
    assert _stopped_reason("") == "the run was aborted"


def test_a_stall_still_stops_the_run_and_the_phase_says_so(config, library, tmp_path):
    """The change is about *reporting* the stop, not about softening it.

    A stall still ends the run: the phase is `aborted`, the outcome is neither `ok` nor `gated`, and
    the completion contract is untouched. Without this assertion the fix could be mistaken for
    patience with bad work — which is the one thing it must not be.
    """
    from engine.host import RunnerState, RunOutcome

    ws = _project(tmp_path)
    orch = _orch(config, library, ws)
    run = orch.adopt(ws.path / "autonomous-recovery.yaml", slug="autonomous-recovery")
    orch._run = run
    outcome = RunOutcome(
        run_id=run.run_id, state=RunnerState.FAILED, exit_code=-9, killed=True,
        termination="wedged",
        termination_detail=("no checkpoint, trace or output for 1800s, past the 1800s stall window "
                            "(concurrency.stall_timeout_s)"))
    orch._settle(run, outcome)

    assert run.phase is RunPhase.ABORTED, "a stalled run must still be stopped"
    assert not outcome.ok and outcome.broken
    assert run.outcome["termination"] == "wedged"
    assert "watchdog" in run.stop_reason and "1800" in run.stop_reason
    # And the diagnostics log carries it, which is the channel the real run left empty.
    records = orch.diagnostics.tail(event="run.phase")
    assert records and records[-1]["detail"].get("termination") == "wedged"


def test_a_watchdog_stop_reaches_the_diagnostics_log_and_not_only_the_trace(config, library, tmp_path):
    """The gap the real run exposed: the host reports on the *bus*, which writes the trace.

    `diagnostics.jsonl` is the other file a person reads, and it is where a run explains itself — the
    `run.phase` line lives there. The 901-second run's log held `run.prepared`, `run.approved`, a
    `node.bind`, a `node.tools.start` and `run.phase: aborted`, and nothing that said a watchdog had
    fired: `watchdog.stall` and `run.terminating` went to `trace.jsonl` only. So the channel that is
    supposed to be the explanation was the one channel the explanation never reached, and the person
    had to know to open the *other* file — and read a trace line with no window in it — to find out.
    """
    ws = _project(tmp_path)
    orch = _orch(config, library, ws)
    run = orch.adopt(ws.path / "autonomous-recovery.yaml", slug="autonomous-recovery")
    orch._run = run
    handler = orch._host_event(run, None)
    handler("watchdog.stall", {"run_id": run.run_id, "pid": 4242, "liveness": "wedged",
                               "silence_s": 1800.4, "stall_timeout_s": 1800.0})
    handler("run.terminating", {"run_id": run.run_id, "pid": 4242, "force": True, "cause": "wedged"})

    log = ws.state_dir / "diagnostics.jsonl"
    written = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    events = [record["event"] for record in written]
    assert "watchdog.stall" in events, "the watchdog has to say so in the file a person reads"
    assert "run.terminating" in events

    stall = next(record for record in written if record["event"] == "watchdog.stall")
    assert stall["level"] == "warning", "a run being stopped is not routine"
    assert stall["detail"]["stall_timeout_s"] == 1800.0, (
        "the window is what the reader needs in order to change it")
