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
