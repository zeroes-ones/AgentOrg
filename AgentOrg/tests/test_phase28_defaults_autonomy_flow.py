#!/usr/bin/env python3
"""Phase 28 tests — the default pair, goal autonomy, auto-staffing and the flow board.

Four features, one theme: the person asked for an agent org they can *leave running*. That needs three
things the engine did not have — one answer to "which model do my people run on", an authority model
where a human is involved only when chosen, and one board that shows who is working on what and what
crossed between them.

So these tests assert the *decisions*, not the plumbing: the default resolves in one place, a goal
passes the gates the org can decide and never a terminal one, a missing skill is staffed on the default
model, and the board names each node's owner and its information flow.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import DefaultsConfig, GoalConfig, load, set_autonomy, set_defaults
from engine.flow import build_flow
from engine.goal import Goal, GoalPolicy
from engine.orchestrator import GateRequest, Orchestrator
from engine.state import Workspace

EXAMPLE = ROOT / "credentials.example.json"


# ── the default pair ─────────────────────────────────────────────────────────


def test_the_declared_default_wins():
    cfg = load(EXAMPLE)
    provider, model, reason = cfg.default_pair()
    assert (provider, model) == ("ollama", "qwen2.5-coder:7b")
    assert "configured" in reason


def test_a_default_pointing_at_a_missing_provider_degrades_with_a_reason():
    """A stale pointer must not brick the engine; it resolves to a usable provider and says why."""
    cfg = load(EXAMPLE)
    cfg.default = DefaultsConfig(provider="gone", model="gone-model")
    provider, model, reason = cfg.default_pair()
    assert provider in cfg.providers
    assert provider != "gone"
    assert reason, "the resolution must state why it differs from the file"


def test_the_window_override_makes_an_unprobed_default_bindable():
    """A declared-but-unprobed model is the common first-run failure; the override fixes it."""
    cfg = load(EXAMPLE)
    cfg.default = DefaultsConfig(provider="ollama", model="not-in-any-catalog",
                                 context_window=65536)
    spec = cfg.default_model_spec()
    assert spec.model_id == "not-in-any-catalog"
    assert spec.context_window == 65536
    assert spec.source == "config"


def test_an_alias_in_the_default_is_expanded():
    cfg = load(EXAMPLE)
    cfg.default = DefaultsConfig(provider="anthropic", model="claude-sonnet")
    provider, model, _ = cfg.default_pair()
    assert provider == "anthropic"
    assert model == "claude-sonnet-4-20250514"


def test_set_defaults_merges_and_preserves_everything_else(tmp_path):
    """The one write rule that matters: a default must never cost you your keys or your policy."""
    path = tmp_path / "credentials.json"
    original = json.loads(EXAMPLE.read_text())
    path.write_text(json.dumps(original))
    set_defaults(path, provider="openai", model="gpt-4o-mini")
    after = json.loads(path.read_text())
    assert after["defaults"]["provider"] == "openai"
    assert after["defaults"]["model"] == "gpt-4o-mini"
    assert set(after["providers"]) == set(original["providers"])
    assert after["policy"] == original["policy"]


def test_set_defaults_writes_nothing_world_readable(tmp_path):
    import os

    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(json.loads(EXAMPLE.read_text())))
    set_defaults(path, model="gpt-4o-mini")
    assert os.stat(path).st_mode & 0o077 == 0


def test_set_defaults_refuses_to_invent_a_file(tmp_path):
    with pytest.raises(Exception, match="no credentials file"):
        set_defaults(tmp_path / "absent.json", provider="openai")


def test_defaults_config_rejects_an_impossible_temperature():
    with pytest.raises(Exception):
        DefaultsConfig(temperature=9.0)


# ── goal autonomy ────────────────────────────────────────────────────────────


def test_a_new_goal_is_autonomous_by_default():
    """The polarity the product chose: a human is involved only if one was asked for."""
    goal = Goal.new("ship the thing")
    assert goal.policy.auto_approve is True
    assert goal.policy.auto_hire is True
    assert goal.policy.human_gate is False


def test_a_human_gate_narrows_every_automatic_decision():
    policy = GoalPolicy(human_gate=True)
    effective = policy.effective()
    assert effective.auto_approve is False
    assert effective.auto_hire is False


def test_a_legacy_goal_document_loads_with_autonomous_defaults():
    """A goal.json written before this feature must keep working, not fail to load."""
    goal = Goal.new("legacy")
    document = goal.as_dict()
    document.pop("policy")
    loaded = Goal.from_dict(document)
    assert loaded.policy.auto_approve is True


def test_goal_config_defaults_are_autonomous():
    cfg = GoalConfig()
    assert cfg.auto_pass_auto_gates is True
    assert cfg.auto_hire_missing is True
    assert cfg.persist_auto_hires is False


def test_goal_config_rejects_an_out_of_range_tier():
    with pytest.raises(Exception):
        GoalConfig(auto_hire_max_tier=5)


def test_set_autonomy_writes_only_the_named_keys(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(json.loads(EXAMPLE.read_text())))
    set_autonomy(path, goal={"auto_pass_auto_gates": False})
    cfg = load(path)
    assert cfg.goal.auto_pass_auto_gates is False
    # The keys not named are untouched by this write.
    assert cfg.goal.auto_hire_missing is True


# ── the gate policy, unit-tested through the orchestrator ────────────────────


@pytest.fixture
def orch(tmp_path):
    ws = Workspace.for_project("flowprobe", root=tmp_path / "projects")
    ws.ensure()
    return Orchestrator(config=load(EXAMPLE), library=_library(), workspace=ws)


def _library():
    from engine.library import resolve

    return resolve()


def _gate(kind: str) -> GateRequest:
    return GateRequest(gate_id="g", kind=kind, reason="test", requires=["change"], present=["change"])


def test_a_terminal_gate_is_never_auto_approved(orch):
    """`kind: human` is release/close/spend authority. Autonomy does not extend to it."""
    assert orch._gate_is_auto_approvable(_gate("human")) is False


def test_an_agent_gate_is_auto_approvable(orch):
    """A bounded reroute is a decision the runner already computed; the goal records it."""
    assert orch._gate_is_auto_approvable(_gate("agent")) is True


def test_a_policy_gate_respects_the_configs_own_answer(orch):
    """A `policy` gate is passed only when the policy matrix permits action for escalation.

    The default `R-ESCALATE` is `confirm`, so an escalation is *not* auto-approvable regardless of the
    goal — the documented safety floor stays intact. `allow_autonomous_escalation` is the one opt-in.
    """
    assert orch._gate_is_auto_approvable(_gate("policy")) is False


def test_the_escalation_floor_lifts_only_on_an_explicit_opt_in(orch):
    """The floor is resolved, not just validated at set time, so the only real opt-in is the flag."""
    orch.policy.allow_autonomous_escalation = True
    orch.policy.defaults["R-ESCALATE"] = "auto"
    assert orch._gate_is_auto_approvable(_gate("policy")) is True


def test_an_unknown_gate_kind_defaults_to_not_approvable(orch):
    """A gate type this build does not understand must not be passed blind."""
    assert orch._gate_is_auto_approvable(_gate("mystery")) is False


def test_an_agent_gate_with_no_route_is_left_for_the_owner(orch):
    """Approving with nothing to act on would loop; the Owner must see it instead."""
    run = _stub_run(orch)
    gate = GateRequest(gate_id="reroute", kind="agent", reason="exhausted", requires=[],
                       present=[], dossier={})
    run.gate = gate
    assert orch._gate_has_a_route(run, gate) is False
    gate.requires = ["developer"]
    assert orch._gate_has_a_route(run, gate) is True


def _stub_run(orch):
    from engine.orchestrator import Run, RunPhase

    return Run(run_id="r1", slug="flowprobe", goal="g", workspace=orch.workspace,
               phase=RunPhase.AWAITING_GATE, org=orch.org, ledger=orch.ledger)


def test_a_human_gate_policy_is_not_auto_passed(orch):
    run = _stub_run(orch)
    run.gate = _gate("agent")
    policy = GoalPolicy(human_gate=True).effective()
    assert orch._auto_pass(run, policy) is False
    assert run.gate is not None, "the gate must still be waiting"


def test_auto_pass_records_who_decided(orch):
    """`by=goal` and `by=owner` are different facts; conflating them would make the trail lie."""
    run = _stub_run(orch)
    run.gate = _gate("agent")
    assert orch._auto_pass(run, GoalPolicy().effective()) is True
    assert run.decisions[-1]["by"] == "goal"
    assert run.gate is None


# ── the posture, and the terminal release ────────────────────────────────────


def test_an_unattended_goal_is_the_default_posture():
    """The one word that answers "do I have to be here for this to finish?"."""
    from engine.goal import Posture

    assert GoalPolicy().posture is Posture.UNATTENDED
    assert GoalPolicy().unattended is True


def test_the_legacy_human_gate_flag_maps_onto_the_posture():
    """An old goal.json, or an old CLI flag, must mean what it meant: a human is involved."""
    from engine.goal import Posture

    assert GoalPolicy(human_gate=True).posture is Posture.SUPERVISED
    assert GoalPolicy(posture=Posture.SUPERVISED).human_gate is True
    # And a document that carries the flag but no posture resolves the same way.
    assert GoalPolicy.from_dict({"human_gate": True}).posture is Posture.SUPERVISED
    assert GoalPolicy.from_dict({"posture": "supervised"}).human_gate is True


def test_the_posture_and_the_legacy_flag_round_trip_through_a_goal_document():
    from engine.goal import Posture

    for posture in (Posture.UNATTENDED, Posture.SUPERVISED):
        goal = Goal.new("round trip", policy=GoalPolicy(posture=posture))
        loaded = Goal.from_dict(goal.as_dict())
        assert loaded.policy.posture is posture
        assert loaded.policy.human_gate == (posture is Posture.SUPERVISED)


def test_an_unknown_posture_is_refused_rather_than_guessed():
    """A typo must not silently become "supervised" or, worse, "unattended"."""
    with pytest.raises(Exception, match="posture"):
        GoalPolicy(posture="cowboy")
    with pytest.raises(Exception, match="posture"):
        GoalPolicy.from_dict({"posture": "cowboy"})


def test_goal_config_declares_the_documented_max_rounds_and_posture():
    """`max_rounds` was documented and read but never declarable; `default_posture` is new."""
    cfg = GoalConfig()
    assert cfg.max_rounds >= 1
    assert cfg.default_posture == "unattended"
    with pytest.raises(Exception):
        GoalConfig(default_posture="cowboy")
    with pytest.raises(Exception):
        GoalConfig(max_rounds=0)


def _terminal_gate(orch, requires=None, present=None):
    """A terminal gate whose evidence lives in the run's node records, as the planner emits it."""
    return GateRequest(gate_id="human-gate", kind="human", reason="release",
                       requires=list(requires if requires is not None else ["reviewer.summary"]),
                       present=list(present or []))


def _run_with_evidence(orch, *, nodes, stop_reason=""):
    run = _stub_run(orch)
    run.outcome = {"nodes": nodes, "artifacts": []}
    run.stop_reason = stop_reason
    return run


def test_a_supervised_goal_still_parks_at_the_terminal_gate(orch):
    """The safety floor, asserted rather than documented: choosing `supervised` parks every gate."""
    run = _run_with_evidence(orch, nodes={"reviewer": {"status": "done", "summary": "all good"}})
    run.gate = _terminal_gate(orch)
    policy = GoalPolicy(posture="supervised").effective()
    assert orch._auto_pass(run, policy) is False
    assert run.gate is not None, "a supervised gate must still be waiting for the Owner"


def test_an_unattended_goal_releases_the_terminal_gate_when_the_evidence_is_present(orch):
    """The whole point: a goal can finish with nobody watching — with evidence, and on the record."""
    run = _run_with_evidence(orch, nodes={"reviewer": {"status": "done", "summary": "reviewed, clean"}})
    run.gate = _terminal_gate(orch)
    assert orch._auto_pass(run, GoalPolicy().effective()) is True
    assert run.gate is None, "the gate was released, so the run is ready to advance"
    assert run.decisions[-1]["by"] == "goal"
    # The release is a recorded decision, so "who released this, and why" is answerable afterwards.
    recorded = orch.ledger.current("human-gate")
    assert recorded is not None and recorded.choice == "released" and recorded.by == "goal"


def test_a_release_with_no_evidence_parks_instead(orch):
    """An approval of nothing is not an approval: a gate that cannot show its evidence waits."""
    run = _run_with_evidence(orch, nodes={"reviewer": {"status": "pending", "summary": ""}})
    run.gate = _terminal_gate(orch)
    assert orch._auto_pass(run, GoalPolicy().effective()) is False
    assert run.gate is not None
    assert orch.ledger.current("human-gate") is None, "nothing may be recorded for a refused release"


def test_a_gate_that_declares_nothing_is_not_trivially_releasable(orch):
    """`requires: []` is not evidence that the work is done, so it is treated as incomplete."""
    run = _run_with_evidence(orch, nodes={"reviewer": {"status": "done", "summary": "done"}})
    run.gate = _terminal_gate(orch, requires=[])
    assert orch._auto_pass(run, GoalPolicy().effective()) is False
    assert run.gate is not None


def test_a_guardrail_block_is_never_released_by_a_goal(orch):
    """Autonomy may decide work is done; it may not decide a safety control that fired was wrong."""
    run = _run_with_evidence(
        orch, nodes={"reviewer": {"status": "done", "summary": "reviewed"}},
        stop_reason="pm: a hand-off payload was blocked by the edge guardrail")
    run.gate = _terminal_gate(orch)
    assert orch._auto_pass(run, GoalPolicy().effective()) is False
    assert run.gate is not None
    assert orch.ledger.current("human-gate") is None


def test_a_contract_violation_is_never_released_by_a_goal(orch):
    run = _run_with_evidence(
        orch, nodes={"reviewer": {"status": "done", "summary": "reviewed"}},
        stop_reason="developer: a node's completion contract was violated")
    run.gate = _terminal_gate(orch)
    assert orch._auto_pass(run, GoalPolicy().effective()) is False
    assert run.gate is not None


def test_a_blocked_node_parks_the_release(orch):
    """A blocked node is a stated, concrete failure — releasing over it would record a false "done"."""
    run = _run_with_evidence(orch, nodes={
        "reviewer": {"status": "done", "summary": "reviewed"},
        "developer": {"status": "blocked", "summary": "cannot proceed"}})
    run.gate = _terminal_gate(orch)
    assert orch._auto_pass(run, GoalPolicy().effective()) is False
    assert run.gate is not None


def test_an_artifact_requirement_is_resolved_against_the_artifact_index(orch):
    """The other evidence shape: a gate requiring an artifact name, as the library's own gate does."""
    run = _stub_run(orch)
    run.outcome = {"nodes": {}, "artifacts": ["review-report"]}
    run.gate = _terminal_gate(orch, requires=["review-report"])
    assert orch._auto_pass(run, GoalPolicy().effective()) is True
    assert run.gate is None


def test_an_unrecordable_release_parks_rather_than_releasing_unrecorded(orch):
    """If the ledger refuses the record, the release must not happen silently."""
    from engine.org.ledger import LedgerError

    run = _run_with_evidence(orch, nodes={"reviewer": {"status": "done", "summary": "reviewed"}})
    run.gate = _terminal_gate(orch)

    def _refuse(*_args, **_kwargs):
        raise LedgerError("this gate already holds a decision")

    orch.ledger.record = _refuse
    assert orch._auto_pass(run, GoalPolicy().effective()) is False
    assert run.gate is not None, "an unreleasable gate must stay parked for the Owner"


def test_gate_evidence_separates_missing_from_present(orch):
    """The report the refusal is based on, checked directly so the reason is never a guess."""
    run = _run_with_evidence(orch, nodes={
        "one": {"status": "done", "summary": "wrote something"},
        "two": {"status": "pending", "summary": ""}})
    report = orch._gate_evidence(run, _terminal_gate(orch, requires=["one.summary", "two.summary"]))
    assert report["present"] == ["one.summary"]
    assert report["missing"] == ["two.summary"]
    assert report["complete"] is False


def test_a_released_gate_is_not_detected_as_still_waiting(orch):
    """A decided gate must stop being a gate.

    The runner keeps a gate node's verdict (`awaiting_owner`) even after a release marks its status
    `done`. A detector keyed on the verdict alone therefore re-parked a released run on the very gate
    that had just been approved, so an unattended goal looped until `max_rounds` and never finished —
    while the ledger correctly recorded a release that the engine then ignored.
    """
    state = {"nodes": {"release": {"status": "done", "verdict": "awaiting_owner"}}}
    assert orch._detect_gate(state) is None, "a released gate is not waiting on anyone"

    # The undecided gate is still detected, so the fix narrows the check rather than removing it.
    pending = {"nodes": {"release": {"status": "needs_review", "verdict": "awaiting_owner"}}}
    detected = orch._detect_gate(pending)
    assert detected is not None and detected.gate_id == "release"


def test_a_continuation_keeps_the_executor_the_run_started_with():
    """A resume must not silently switch executors.

    `resume_run` did not accept `extra_args`, so every continuation round after the first dropped the
    caller's `--executor` and spawned the generated plugin instead. A run under a stub executor
    therefore proved one thing in round one and something else in every later round.
    """
    import inspect

    from engine.host import RunnerHost

    signature = inspect.signature(RunnerHost.resume_run)
    assert "extra_args" in signature.parameters, (
        "resume_run must thread extra_args through, or a continuation loses its executor override")


# ── the flow board ───────────────────────────────────────────────────────────


@pytest.fixture
def flow_workspace(tmp_path):
    ws = Workspace.for_project("flowboard", root=tmp_path / "projects")
    ws.ensure()
    return ws


def _state(ws) -> pathlib.Path:
    return ws.state_dir


def test_a_fresh_workspace_yields_an_empty_board(flow_workspace):
    board = build_flow(flow_workspace)
    assert board["rows"] == []
    assert board["counts"]["total"] == 0
    assert "No work is assigned yet" in board["headline"]


def test_the_board_names_each_node_owner_from_the_bindings(flow_workspace):
    _state(flow_workspace).joinpath("run_state.json").write_text(json.dumps({
        "run_id": "run_1", "slug": "flowboard", "phase": "running",
        "nodes": {"dev": {"status": "done", "verdict": "ok"},
                  "review": {"status": "running"}},
        "bindings": {"dev": {"agents": ["ag_a"], "reason": "Alice chosen"},
                     "review": {"agents": ["ag_b"], "reason": "Sana chosen"}},
    }))
    board = build_flow(flow_workspace, org={"agents": [
        {"id": "ag_a", "name": "Alice", "title": "Backend Developer"},
        {"id": "ag_b", "name": "Sana", "title": "Code Reviewer"},
    ]})
    rows = {row["node_id"]: row for row in board["rows"]}
    assert rows["dev"]["agent_name"] == "Alice"
    assert rows["review"]["agent_name"] == "Sana"
    assert rows["dev"]["status"] == "done"
    assert board["counts"]["done"] == 1
    assert board["counts"]["working"] == 1


def test_the_board_reads_bindings_from_the_node_bind_diagnostics(flow_workspace):
    """The library's checkpoint has no bindings; the engine emits them as diagnostics."""
    _state(flow_workspace).joinpath("run_state.json").write_text(json.dumps({
        "nodes": {"dev": {"status": "done"}},
        "bindings": None,
    }))
    _state(flow_workspace).joinpath("diagnostics.jsonl").write_text(
        json.dumps({"event": "node.bind", "node_id": "dev", "agent_id": "ag_a",
                    "detail": {"policy": "load-balanced", "reason": "Alice chosen"}}) + "\n")
    board = build_flow(flow_workspace, org={"agents": [{"id": "ag_a", "name": "Alice"}]})
    assert board["rows"][0]["agent_name"] == "Alice"


def test_a_handoff_shows_what_moved_between_two_agents(flow_workspace):
    _state(flow_workspace).joinpath("trace.jsonl").write_text(
        json.dumps({"type": "handoff.fulfilled",
                    "payload": {"handoff_id": "h1", "from_node": "dev", "to_node": "review",
                                "from_agent": "Alice", "to_agent": "Sana",
                                "summary": "the change is ready", "artifacts": ["change"]},
                    "ts": "2026-01-01T00:00:00.000Z"}) + "\n")
    board = build_flow(flow_workspace)
    assert len(board["handoffs"]) == 1
    handoff = board["handoffs"][0]
    assert (handoff["from_agent"], handoff["to_agent"]) == ("Alice", "Sana")
    assert handoff["state"] == "fulfilled"
    assert handoff["tone"] == "good"


def test_a_breached_handoff_is_visible_and_counted(flow_workspace):
    _state(flow_workspace).joinpath("trace.jsonl").write_text("\n".join([
        json.dumps({"type": "handoff.proposed", "payload": {"handoff_id": "h1",
                                                            "from_node": "dev", "to_node": "review"}}),
        json.dumps({"type": "handoff.breached", "payload": {"handoff_id": "h1",
                                                            "status": "blocked"}}),
    ]) + "\n")
    board = build_flow(flow_workspace)
    assert board["handoffs"][0]["state"] == "breached"
    assert board["handoffs"][0]["tone"] == "bad"
    assert board["handoffs"][0]["breaches"] == 1


def test_a_node_gets_its_received_and_sent_edges(flow_workspace):
    _state(flow_workspace).joinpath("run_state.json").write_text(json.dumps({"nodes": {}}))
    _state(flow_workspace).joinpath("trace.jsonl").write_text(
        json.dumps({"type": "handoff.fulfilled",
                    "payload": {"handoff_id": "h1", "from_node": "dev", "to_node": "review",
                                "from_agent": "Alice", "to_agent": "Sana"}}) + "\n")
    board = build_flow(flow_workspace)
    rows = {row["node_id"]: row for row in board["rows"]}
    assert rows["dev"]["sent_to"] == "Sana"
    assert rows["review"]["received_from"] == "Alice"


def test_a_stuck_node_says_why_in_the_headline(flow_workspace):
    _state(flow_workspace).joinpath("run_state.json").write_text(json.dumps({
        "phase": "escalated",
        "nodes": {"pm": {"status": "needs_review", "verdict": "contract-violation",
                         "summary": "declared criteria not covered: c1"}},
    }))
    board = build_flow(flow_workspace)
    assert "pm is stuck" in board["headline"]
    assert "c1" in board["headline"]
    assert board["counts"]["stuck"] == 1


def test_a_torn_trace_line_does_not_break_the_board(flow_workspace):
    _state(flow_workspace).joinpath("trace.jsonl").write_text(
        '{"type": "handoff.proposed", "payload": {"handoff_id": "h1"}}\n'
        '{"type": "handoff.proposed", "payl\n')
    board = build_flow(flow_workspace)
    assert len(board["handoffs"]) == 1


# ── auto-staffing: "if the person does not exist, create one" ────────────────


def _staff_orch(tmp_path):
    ws = Workspace.for_project("staffprobe", root=tmp_path / "projects")
    ws.ensure()
    return Orchestrator(config=load(EXAMPLE), library=_library(), workspace=ws)


def _plan(*skills):
    manifest = {"nodes": [{"id": f"n{i}", "skill": s} for i, s in enumerate(skills)]}
    return type("P", (), {"manifest": manifest})()


def _awaiting_run(orch):
    from engine.orchestrator import Run, RunPhase

    return Run(run_id="r1", slug="staffprobe", goal="g", workspace=orch.workspace,
               phase=RunPhase.AWAITING_APPROVAL, org=orch.org, ledger=orch.ledger)


def test_a_missing_skill_is_staffed_on_the_default_model(tmp_path):
    orch = _staff_orch(tmp_path)
    run = _awaiting_run(orch)
    before = len(orch.org.agents)
    result = orch._auto_staff(_plan("backend-developer", "no-such-skill-anywhere"),
                              enabled=None, run=run)
    assert [c["skill"] for c in result["created"]] == ["no-such-skill-anywhere"]
    assert result["gaps"] == [], "the gap must be closed, not merely reported"
    assert len(orch.org.agents) == before + 1
    provider, model, _ = orch.config.default_pair()
    assert result["created"][0]["provider"] == provider
    assert result["created"][0]["model"] == model


def test_an_auto_created_helper_is_ephemeral_by_default(tmp_path):
    orch = _staff_orch(tmp_path)
    run = _awaiting_run(orch)
    orch._auto_staff(_plan("no-such-skill-anywhere"), enabled=None, run=run)
    helper = next(a for a in orch.org.agents.values() if a.skills == ["no-such-skill-anywhere"])
    assert helper.origin == "ephemeral", "the default leaves no roster entry"


def test_a_goal_can_forbid_auto_staffing(tmp_path):
    orch = _staff_orch(tmp_path)
    run = _awaiting_run(orch)
    result = orch._auto_staff(_plan("no-such-skill-anywhere"), enabled=False, run=run)
    assert result["created"] == []
    assert [g["skill"] for g in result["gaps"]] == ["no-such-skill-anywhere"]


def test_an_existing_holder_is_preferred_over_creating_a_person(tmp_path):
    orch = _staff_orch(tmp_path)
    run = _awaiting_run(orch)
    result = orch._auto_staff(_plan("backend-developer"), enabled=None, run=run)
    assert result["created"] == [], "a capability someone already holds is never re-created"


def test_the_helper_name_is_unique_and_readable(tmp_path):
    orch = _staff_orch(tmp_path)
    assert orch._helper_name("code-reviewer") == "CodeReviewer"
    orch.org.hire  # the roster refuses duplicates; the name must not collide
    first = orch._helper_name("code-reviewer")
    assert not any(a.name == first for a in orch.org.agents.values())


def test_the_board_reads_node_results_from_the_engines_outcome(flow_workspace):
    """The engine writes node results under `outcome.nodes`; the board must read that.

    It read only a top-level `nodes` key — which is the *library runner's* checkpoint shape, a
    different file — so a run that had just produced a PRD reported "No work is assigned yet" on the
    board while its own status showed the node and its artifact. A board that contradicts the run is
    worse than one that shows nothing.
    """
    _state(flow_workspace).joinpath("run_state.json").write_text(json.dumps({
        "run_id": "run_1", "slug": "flowboard", "phase": "ready",
        "outcome": {"nodes": {"pm": {"status": "needs_review", "verdict": "changes_requested",
                                     "summary": "produced the PRD"}}},
        "bindings": {"pm": {"agents": ["ag_a"], "reason": "Priya chosen"}},
    }))
    board = build_flow(flow_workspace, org={"agents": [{"id": "ag_a", "name": "Priya"}]})
    rows = {row["node_id"]: row for row in board["rows"]}
    assert "pm" in rows, "the node the run reported must appear on the board"
    assert rows["pm"]["agent_name"] == "Priya"
    assert rows["pm"]["verdict"] == "changes_requested"
    assert board["counts"]["stuck"] == 1


# ── a durable auto-hire must actually be written, and to the *project* ───────
#
# Two real defects, both found by running a goal end-to-end. `_is_owner_hired` filtered on
# `origin == "owner"`, so a `origin="goal"` helper was dropped and the roster was written EMPTY while
# the log said "persisted 5 helper(s)". And `_roster_root` used `workspace.root`, which for an
# *attached* project is the project's *parent* — so the file landed beside the repository
# (`/tmp/.agentorg`) where nothing would read it again.


def test_a_goal_created_helper_is_persisted_not_dropped(tmp_path):
    """`persist_hires` must write the helper, not an empty roster."""
    from engine.people import _is_owner_hired

    assert _is_owner_hired({"origin": "goal", "kind": "ai"}), "a goal-created hire is durable"
    assert _is_owner_hired({"origin": "owner", "kind": "ai"})
    assert not _is_owner_hired({"origin": "ephemeral", "kind": "ai"}), "ephemeral stays ephemeral"
    assert not _is_owner_hired({"origin": "goal", "kind": "human"})


def test_the_helper_roster_lands_in_the_project_not_its_parent(tmp_path):
    """For an attached workspace, `root` is the parent — the roster must not go there."""
    from engine.goal import Goal, GoalPolicy
    from engine.orchestrator import Run, RunPhase
    from engine.state import Workspace

    project = tmp_path / "myapp"
    project.mkdir()
    ws = Workspace.attach(str(project))
    # Resolved on both sides: macOS reaches the temp dir through the /private symlink.
    assert ws.root == tmp_path.resolve(), "an attached workspace's root is its parent (the premise)"
    orch = Orchestrator(config=load(EXAMPLE), library=_library(), workspace=ws)
    # An armed goal whose policy asks for durable hires — the real path a person's `--persist-hires`
    # takes. `enabled=None` means "ask the goal", so this exercises the goal policy rather than the
    # config default.
    orch._goal = Goal.new("staff the gap", policy=GoalPolicy(persist_hires=True))
    run = Run(run_id="r1", slug="myapp", goal="g", workspace=ws, phase=RunPhase.AWAITING_APPROVAL,
              org=orch.org, ledger=orch.ledger)
    plan = _plan("ux-researcher")
    result = orch._auto_staff(plan, enabled=None, run=run)
    assert result["created"], "the gap must be staffed"
    assert result["created"][0]["persisted"] is True
    roster = project / ".agentorg" / "roster.json"
    assert roster.is_file(), "the roster must land in the project, not beside it"
    assert not (tmp_path / ".agentorg" / "roster.json").exists()
    document = json.loads(roster.read_text())
    assert [a["name"] for a in document["agents"]] == [result["created"][0]["agent"]]
    assert document["agents"][0]["origin"] == "goal"


def test_an_attached_workspace_finds_its_run_by_slug(tmp_path):
    """`load(slug)` must find the run on an *attached* project, or every gate command breaks.

    For an attached workspace the slug is the folder name and the run's slug is the workflow's, so
    resolving `root/slug` pointed at a managed directory beside the repository that does not exist.
    `load` returned None and `decide`, `status` and `flow` all reported "no run found" for a run
    sitting right there — a real `decide --approve` failed this way, leaving a gate unresolvable.
    """
    from engine.orchestrator import Run, RunPhase
    from engine.state import Workspace

    project = tmp_path / "myapp"
    project.mkdir()
    ws = Workspace.attach(str(project))
    orch = Orchestrator(config=load(EXAMPLE), library=_library(), workspace=ws)
    run = Run(run_id="r1", slug="some-workflow", goal="g", workspace=ws,
              phase=RunPhase.AWAITING_APPROVAL, org=orch.org, ledger=orch.ledger)
    orch._persist(run)
    # By the workflow's slug, by the folder's slug, and with no slug at all.
    assert orch.load("some-workflow") is not None, "the workflow slug must resolve"
    assert orch.load("myapp") is not None, "the folder slug must resolve"
    assert orch.load(None) is not None

# ── the whole point, end to end: a goal that finishes with nobody watching ────


#: A graph with one producing node and a terminal human gate — the smallest shape that can either
#: finish alone or park. `execute_node` satisfies the producer's declared checklist (the runner
#: refuses `criteria_met` that names criteria the skill never declared) and parks the gate.
_AUTOPROBE_MANIFEST = {
    "name": "autoprobe",
    "version": "1.0.0",
    "description": "Unattended probe",
    "payloads": {"handoff-v1": ["status", "summary"]},
    "start": "dev",
    "nodes": [{"id": "dev", "skill": "backend-developer", "outputs": ["change"]}],
    "gates": [{"id": "release", "type": "gate", "kind": "human", "requires": ["change"],
               "description": "Owner release approval"}],
    "edges": [{"from": "dev", "to": "release", "when": "dev.status == done",
               "payload": "handoff-v1"}],
    "end": ["release"],
}

_AUTOPROBE_STUB = '''CRITERIA = ["c1", "c2", "c3"]


def execute_node(node_id, state, ctx):
    if node_id == "release":
        return {"status": "needs_review", "verdict": "awaiting_owner",
                "summary": "human gate reached", "evidence": ["gate:release"]}
    return {"status": "done", "verdict": "ok", "summary": "implemented",
            "evidence": ["src/app.py#abc"], "criteria_met": list(CRITERIA),
            "artifacts": [{"name": "change", "path": "src/app.py", "sha": "abc123",
                           "type": "change"}]}
'''


def _drive_to_completion(tmp_path, posture):
    """Run the probe graph under a goal of the given posture, with the stub executor.

    This is the verification that matters: it exercises `_run_with_goal` → `_auto_pass` →
    `_release_terminal_gate` → `decide` → `_release_node_for_resume` → the next continuation round,
    which is the whole chain the unit tests above only reach one call at a time.
    """
    from engine.bus import EventBus
    from engine.goal import Posture
    from engine.orchestrator import Orchestrator
    from engine.planner import emit_safe_yaml
    from engine.state import Workspace

    ws = Workspace.for_project("autoprobe", root=tmp_path / "projects")
    ws.ensure()
    (ws.path / "autoprobe.yaml").write_text(emit_safe_yaml(_AUTOPROBE_MANIFEST))
    (ws.path / "stub.py").write_text(_AUTOPROBE_STUB)
    config = load(EXAMPLE)
    config.goal.max_rounds = 3
    bus = EventBus(run_id="autoprobe", history_size=4000)
    orch = Orchestrator(config=config, library=_library(), workspace=ws, bus=bus)
    orch.goal_set("ship the probe", armed=True, by="test",
                  policy=GoalPolicy(posture=posture))
    run = orch.adopt(ws.path / "autoprobe.yaml", slug="autoprobe")
    orch.approve(run)
    orch.execute(run, executor=ws.path / "stub.py")
    return orch, run, ws


def test_an_unattended_goal_finishes_the_run_with_no_human(tmp_path):
    """The feature, stated as the user experiences it: point it at a plan and walk away."""
    from engine.goal import Posture
    from engine.orchestrator import RunPhase

    orch, run, _ws = _drive_to_completion(tmp_path, Posture.UNATTENDED)
    assert run.phase is RunPhase.DONE, f"expected a finished run, got {run.phase.value}"
    assert run.gate is None, "the terminal gate was released, so nothing is waiting"

    # And it is on the record as the goal's decision, not as the Owner's.
    decisions = [d for d in run.decisions if d.get("by") == "goal"]
    assert decisions, "an unattended run must record who released the gate"
    recorded = orch.ledger.current("release")
    assert recorded is not None, "the release must be in the ledger, not only in the trace"
    assert (recorded.choice, recorded.by) == ("released", "goal")


def test_a_supervised_goal_parks_the_identical_plan(tmp_path):
    """The floor, verified on the same graph the autonomous case finishes.

    Same manifest, same executor, same goal objective — only the posture differs. If this ever passes
    while the test above fails, or vice versa, the posture is not what is deciding.
    """
    from engine.goal import Posture
    from engine.orchestrator import RunPhase

    orch, run, _ws = _drive_to_completion(tmp_path, Posture.SUPERVISED)
    assert run.phase is RunPhase.AWAITING_GATE, "a supervised goal must park at the terminal gate"
    assert run.gate is not None and run.gate.gate_id == "release"
    assert not [d for d in run.decisions if d.get("by") == "goal"], (
        "a supervised goal must make no decision on the Owner's behalf")
    assert orch.ledger.current("release") is None, "nothing may be released without being asked"


def test_the_unattended_run_stops_at_its_round_cap_rather_than_looping_forever(tmp_path):
    """The bound that makes "no ceiling by default" survivable: `goal.max_rounds` is real.

    It was documented and read by the orchestrator while no config field declared it, so only the
    hardcoded fallback applied and no caller could tighten it. Declaring it is what makes this
    assertion possible at all.
    """
    from engine.goal import Posture

    orch, run, _ws = _drive_to_completion(tmp_path, Posture.UNATTENDED)
    goal = orch.goal()
    assert goal is not None
    assert goal.spend.rounds <= 3, (
        f"the run exceeded its configured round cap: {goal.spend.rounds} rounds")


# ── the tier cap: a knob that must actually bound something ──────────────────


def test_a_project_contained_write_is_not_an_elevated_capability():
    """The misclassification that made the tier cap unenforceable.

    `write:` is an elevated marker because an *unscoped* write is a grant the whole filesystem would
    have to be trusted with. But every `write:` scored T3, so a workspace-scoped helper looked
    identical to one requesting `deploy:prod` — and since `goal.auto_hire_max_tier` is configurable
    only up to T2, the engine's own auto-created helper could not be admitted at *any* setting. The
    cap was therefore unimplementable without turning auto-staffing off, which is how it came to be
    documented but never enforced.
    """
    from engine.org.delegation import ApprovalTier, HiringDesk, Requisition

    desk = HiringDesk.__new__(HiringDesk)
    desk.approval_tiers = {}

    def tier(capabilities):
        request = Requisition(requester_id="g", kind="helper", skill="s",
                              capabilities=list(capabilities), needed=["s"],
                              why_existing_insufficient="x", expected_outcome="y")
        return desk.classify_tier(request)[0]

    # A subtree grant is contained — the tool layer refuses absolute, `~` and `..` paths, and
    # anything resolving outside the workspace, so this cannot reach beyond the project.
    assert tier(["read:*", "write:src/**"]) is not ApprovalTier.T3
    assert tier(["read:src/**"]) is not ApprovalTier.T3

    # What is genuinely unbounded or privileged still reaches the Owner.
    for dangerous in (["write:*"], ["write:/etc"], ["write:../etc"], ["write:~"],
                      ["deploy:prod/**"], ["admin:*"], ["exec:bash"]):
        assert tier(dangerous) is ApprovalTier.T3, f"{dangerous} must stay gated"


def test_the_tier_rank_orders_the_tiers_it_caps():
    """The cap compares ordinals, so a tier added below it is admitted and one above is not."""
    from engine.org.delegation import ApprovalTier
    from engine.orchestrator import Orchestrator

    assert Orchestrator._tier_rank(ApprovalTier.T0) == 0
    assert Orchestrator._tier_rank(ApprovalTier.T3) == 3
    # A shape this build does not recognise is treated as the most gated, not the least.
    assert Orchestrator._tier_rank("mystery") == 3


def test_the_default_cap_admits_the_engines_own_helper(tmp_path):
    """The default must let the engine do the job the feature exists for.

    Stated as a test because the tempting "fix" — gate the cap literally — would park every auto-hire
    at the default setting, replacing a dead knob with a broken feature.
    """
    from engine.orchestrator import _AUTO_HELPER_CAPABILITIES

    orch = _staff_orch(tmp_path)
    tier, _why = orch._helper_tier(skill="backend-developer", provider="ollama",
                                   model="qwen2.5-coder:7b", window=32768)
    cap = int(orch.config.goal.auto_hire_max_tier)
    assert orch._tier_rank(tier) <= cap, (
        f"the engine's own helper is {tier.value} (capabilities {list(_AUTO_HELPER_CAPABILITIES)}), "
        f"above the default cap T{cap}: auto-staffing could never run")

    # And the real call still creates the helper.
    run = _awaiting_run(orch)
    result = orch._auto_staff(_plan("no-such-skill-anywhere"), enabled=None, run=run)
    assert [c["skill"] for c in result["created"]] == ["no-such-skill-anywhere"]


def test_a_cap_below_the_helpers_tier_reports_the_gap_instead_of_hiring(tmp_path):
    """The cap must be able to fire, or it is still a dead knob.

    Configured so even a scoped read counts as elevated, which is the only way the engine's helper can
    outrank the cap — that the cap *can* refuse is the property being pinned.
    """
    from engine.bus import EventBus

    ws = Workspace.for_project("capfire", root=tmp_path / "projects")
    ws.ensure()
    config = load(EXAMPLE)
    config.goal.auto_hire_max_tier = 0
    config.delegation.approval_tiers = {"elevated_capability_markers": ["read:", "write:"]}
    bus = EventBus(run_id="r1", history_size=500)
    orch = Orchestrator(config=config, library=_library(), workspace=ws, bus=bus)

    tier, _why = orch._helper_tier(skill="backend-developer", provider="ollama",
                                   model="qwen2.5-coder:7b", window=32768)
    assert orch._tier_rank(tier) > 0, "this config is meant to make the helper outrank the cap"

    run = _awaiting_run(orch)
    before = len(orch.org.agents)
    result = orch._auto_staff(_plan("no-such-skill-anywhere"), enabled=None, run=run)

    assert result["created"] == [], "a helper above the cap must not be created"
    assert len(orch.org.agents) == before
    assert [g["skill"] for g in result["gaps"]] == ["no-such-skill-anywhere"], (
        "the gap must be reported, so it reaches the Owner as work to staff rather than vanishing")

    # And the refusal names the tier and the cap, so "why was this not staffed" has an answer.
    gate = [e for e in bus.history() if e.type.value == "human.gate"]
    assert gate, "a capped auto-hire must surface as something the Owner can see"
    payload = gate[-1].payload
    assert payload["waiting_on"] == "owner"
    assert payload["cap"] == "T0" and payload["tier"] in ("T1", "T2", "T3")


def test_the_classified_tier_and_the_created_helper_describe_the_same_agent(tmp_path):
    """The cap bounds the real hire, so both must read one capability definition.

    A cap computed from one capability set and a helper created with another would bound a fiction —
    which is exactly the drift a duplicated literal invites.
    """
    from engine.orchestrator import _AUTO_HELPER_CAPABILITIES

    orch = _staff_orch(tmp_path)
    run = _awaiting_run(orch)
    orch._auto_staff(_plan("no-such-skill-anywhere"), enabled=None, run=run)
    helper = next(a for a in orch.org.agents.values() if a.skills == ["no-such-skill-anywhere"])
    assert list(helper.capabilities) == list(_AUTO_HELPER_CAPABILITIES)
