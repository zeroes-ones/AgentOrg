#!/usr/bin/env python3
"""Phase 6 orchestrator tests — the run lifecycle, Owner commands and resume.

The emphasis is on what the Owner's authority rests on: nothing executes unapproved, a gate parks
rather than fails, a rejection is recorded with its reason, and a killed run resumes at its last
transition rather than at the start.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.bus import EventBus
from engine.config import load
from engine.library import resolve
from engine.orchestrator import Orchestrator, OrchestratorError, Run, RunPhase
from engine.planner import emit_safe_yaml
from engine.state import Workspace


#: A graph with one skill node and a human gate: the smallest thing that reaches a gate.
MANIFEST = {
    "name": "gateprobe",
    "version": "1.0.0",
    "description": "Orchestrator probe",
    "payloads": {"handoff-v1": ["status", "summary"]},
    "start": "dev",
    "nodes": [{"id": "dev", "skill": "backend-developer", "outputs": ["change"]}],
    "gates": [{"id": "release", "type": "gate", "kind": "human", "requires": ["change"],
               "description": "Owner release approval"}],
    "edges": [{"from": "dev", "to": "release", "when": "dev.status == done",
               "payload": "handoff-v1"}],
    "end": ["release"],
}

#: A stub executor that satisfies the developer contract and parks the gate, so no provider is needed.
STUB = '''CRITERIA = [
    "Every accepted finding addressed with a concrete diff",
    "No finding silently dropped",
    "Open items declared in open questions rather than hidden",
]


def execute_node(node_id, state, ctx):
    if node_id == "release":
        return {"status": "needs_review", "verdict": "awaiting_owner",
                "summary": "human gate reached", "evidence": ["gate:release"]}
    return {"status": "done", "verdict": "ok", "summary": "implemented",
            "evidence": ["src/app.py#abc"], "criteria_met": list(CRITERIA),
            "artifacts": [{"name": "change", "path": "src/app.py", "sha": "abc123",
                           "type": "change"}]}
'''


@pytest.fixture
def config():
    return load()


@pytest.fixture
def library():
    return resolve()


@pytest.fixture
def stack(tmp_path, config, library):
    """An orchestrator over a temp project root, with the stub executor written for it."""
    root = tmp_path / "projects"
    ws = Workspace.for_project("gateprobe", root=root)
    ws.ensure()
    (ws.path / "gateprobe.yaml").write_text(emit_safe_yaml(MANIFEST))
    (ws.path / "stub.py").write_text(STUB)
    bus = EventBus(run_id="r", history_size=500)
    orch = Orchestrator(config=config, library=library, workspace=ws, bus=bus)
    return orch, ws, bus


def _run_to_gate(orch, ws):
    """Adopt, approve and execute the fixture graph so it parks at the gate."""
    run = orch.adopt(ws.path / "gateprobe.yaml", slug="gateprobe")
    orch.approve(run)
    orch.execute(run, executor=ws.path / "stub.py")
    return run


# ── adopting and approving ───────────────────────────────────────────────────


def test_adopting_an_existing_manifest_does_not_plan_one(stack):
    """`prepare` authors a graph; `adopt` runs one that exists. They must not be the same method."""
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml", slug="gateprobe")
    assert run.plan is None, "an adopted manifest is not a plan"
    assert run.manifest_path.is_file()
    assert run.phase is RunPhase.AWAITING_APPROVAL


def test_adopting_under_a_different_slug_renames_the_manifest_inside(stack):
    """The library requires the filename to equal the manifest name, or the runner rejects it."""
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml", slug="renamed")
    text = run.manifest_path.read_text()
    assert "name: renamed" in text
    assert run.manifest_path.name == "renamed.yaml"


def test_adopting_a_missing_manifest_is_refused(stack):
    orch, ws, _ = stack
    with pytest.raises(OrchestratorError, match="no manifest at"):
        orch.adopt(ws.path / "absent.yaml")


def test_adopting_an_invalid_manifest_is_refused(stack):
    """'I wrote it by hand' is not evidence that it is executable."""
    orch, ws, _ = stack
    broken = ws.path / "broken.yaml"
    broken.write_text("name: broken\nversion: \"1.0.0\"\nstart: nowhere\nnodes: []\n")
    with pytest.raises(OrchestratorError, match="invalid"):
        orch.adopt(broken)


def test_nothing_executes_before_approval(stack):
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    with pytest.raises(OrchestratorError, match="only a ready, paused or running run"):
        orch.execute(run, executor=ws.path / "stub.py")


def test_approval_marks_the_run_ready(stack):
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    orch.approve(run)
    assert run.phase is RunPhase.READY


def test_approval_emits_its_event(stack):
    orch, ws, bus = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    orch.approve(run)
    assert any(e.type_value == "manifest.approved" for e in bus.history())


# ── planning a goal ──────────────────────────────────────────────────────────


def test_prepare_plans_binds_and_reports_gaps(stack):
    """The Owner approves a graph, and sees what it needs before approving it."""
    orch, _, _ = stack
    run = orch.prepare("Build a booking API with auth and payments", slug="booking")
    assert run.phase is RunPhase.AWAITING_APPROVAL
    assert run.plan is not None and run.plan.validation.valid
    assert run.manifest_path.is_file()
    assert run.bindings, "a planned run must be bound to the roster"
    assert run.staffing_gaps, "the default company does not staff every skill the planner emits"
    assert all("skill" in gap for gap in run.staffing_gaps)


def test_prepare_reports_gaps_to_the_ui(stack):
    orch, _, bus = stack
    orch.prepare("Build a booking API with auth and payments", slug="booking2")
    proposed = [e for e in bus.history() if e.type_value == "manifest.proposed"]
    assert proposed and "staffing_gaps" in proposed[-1].payload


def test_prepare_refuses_an_empty_goal(stack):
    orch, _, _ = stack
    with pytest.raises(OrchestratorError, match="cannot plan"):
        orch.prepare("   ")


# ── executing to a gate ──────────────────────────────────────────────────────


def test_a_run_reaching_a_human_gate_parks_rather_than_fails(stack):
    """A run at a gate is successful up to that point; reporting it as a failure would look broken."""
    orch, ws, _ = stack
    run = _run_to_gate(orch, ws)
    assert run.phase is RunPhase.AWAITING_GATE
    assert run.waiting is True
    assert run.gate is not None
    assert run.gate.gate_id == "release"
    assert run.gate.kind == "human"
    assert run.gate.requires == ["change"]
    assert "change" in run.gate.present


def test_the_gate_carries_a_dossier(stack):
    """The Owner needs to see what led here, not just that a gate was reached."""
    orch, ws, _ = stack
    run = _run_to_gate(orch, ws)
    assert run.gate.dossier["node"] == "release"
    assert "attempts" in run.gate.dossier


def test_the_run_records_its_node_outcomes(stack):
    orch, ws, _ = stack
    run = _run_to_gate(orch, ws)
    assert run.outcome["nodes"]["dev"]["status"] == "done"
    assert "change" in run.outcome["artifacts"]


# ── Owner decisions ──────────────────────────────────────────────────────────


def test_approving_a_gate_continues_and_records_the_note(stack):
    """An approval note is guidance, so it becomes an instruction for the next nodes."""
    orch, ws, _ = stack
    run = _run_to_gate(orch, ws)
    orch.decide(True, run=run, note="ship it, but watch the migration")
    assert run.phase is RunPhase.READY
    assert run.gate is None
    assert "ship it, but watch the migration" in run.instructions
    assert run.decisions[-1]["approved"] is True


def test_rejecting_a_gate_parks_the_run_with_the_reason(stack):
    """A rejection the agents cannot read is one they will re-attempt identically."""
    orch, ws, _ = stack
    run = _run_to_gate(orch, ws)
    orch.decide(False, run=run, note="the migration needs a rollback plan")
    assert run.phase is RunPhase.PAUSED
    assert "the migration needs a rollback plan" in run.instructions[-1]


def test_deciding_without_a_gate_is_refused(stack):
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    with pytest.raises(OrchestratorError, match="no gate to decide"):
        orch.decide(True, run=run)


def test_a_decision_emits_its_event(stack):
    orch, ws, bus = stack
    run = _run_to_gate(orch, ws)
    orch.decide(True, run=run)
    decisions = [e for e in bus.history() if e.type_value == "human.decision"]
    assert decisions and decisions[-1].payload["approved"] is True


# ── Owner instructions ───────────────────────────────────────────────────────


def test_an_instruction_is_recorded_as_guidance(stack):
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    orch.instruct("prefer no new dependencies", run=run)
    assert run.instructions == ["prefer no new dependencies"]
    assert run.constraints == []


def test_a_constraint_is_recorded_separately_so_it_survives(stack):
    """A constraint is preserved verbatim across every compaction and rotation; an instruction is not."""
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    orch.instruct("NEVER log the raw auth token", run=run, as_constraint=True)
    assert run.constraints == ["NEVER log the raw auth token"]
    assert run.instructions == []


def test_an_empty_instruction_is_refused(stack):
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    with pytest.raises(OrchestratorError, match="cannot be empty"):
        orch.instruct("   ", run=run)


# ── Owner control of the org ─────────────────────────────────────────────────


def test_reassign_refuses_an_agent_without_the_skill(stack):
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    pm = next(a for a in orch.org.agents.values() if "product-manager" in a.skills)
    with pytest.raises(OrchestratorError, match="does not hold"):
        orch.reassign("dev", pm.id, run=run)


def test_reassign_refuses_an_unknown_node(stack):
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    developer = next(a for a in orch.org.agents.values() if "backend-developer" in a.skills)
    with pytest.raises(OrchestratorError, match="not in this run's plan"):
        orch.reassign("nonexistent", developer.id, run=run)


def test_reassign_pins_the_node_and_emits_an_override(stack):
    orch, ws, bus = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    developer = next(a for a in orch.org.agents.values() if "backend-developer" in a.skills)
    orch.reassign("dev", developer.id, run=run)
    assert run.bindings["dev"]["pinned_id"] == developer.id
    assert any(e.type_value == "route.overridden" for e in bus.history())


def test_takeover_records_the_owner_as_the_actor(stack):
    """The Owner is an agent, so a takeover uses the same machinery as any assignment."""
    orch, ws, bus = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    orch.takeover("dev", run=run)
    assert run.decisions[-1]["action"] == "takeover"
    assert run.decisions[-1]["by"] == orch.org.owner().id
    assert any(e.type_value == "human.takeover" for e in bus.history())


# ── resuming ─────────────────────────────────────────────────────────────────


def test_the_checkpoint_records_the_phase(stack):
    orch, ws, _ = stack
    _run_to_gate(orch, ws)
    checkpoint = json.loads(ws.checkpoint_path.read_text())
    assert checkpoint["phase"] == "awaiting_gate"
    assert "org" in checkpoint
    assert "ledger" in checkpoint


def test_a_run_reloads_at_the_phase_it_reached(stack):
    """A killed process must resume at its last transition, not at the start."""
    orch, ws, _ = stack
    _run_to_gate(orch, ws)

    fresh = Orchestrator(config=orch.config, library=orch.library, workspace=ws)
    reloaded = fresh.load("gateprobe")
    assert reloaded is not None
    assert reloaded.phase is RunPhase.AWAITING_GATE
    assert reloaded.gate is not None and reloaded.gate.gate_id == "release"


def test_loading_a_run_that_does_not_exist_is_none(stack):
    orch, _, _ = stack
    assert orch.load("never-ran") is None


def test_resume_clears_the_gate_and_readies_the_run(stack):
    orch, ws, _ = stack
    run = _run_to_gate(orch, ws)
    orch.resume(run)
    assert run.phase is RunPhase.READY
    assert run.gate is None


def test_abort_keeps_the_checkpoint(stack):
    """A killed run must be resumable or inspectable, not lost."""
    orch, ws, bus = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    orch.abort(run)
    assert run.phase is RunPhase.ABORTED
    assert ws.checkpoint_path.is_file()
    assert any(e.type_value == "run.aborted" for e in bus.history())


# ── status ───────────────────────────────────────────────────────────────────


def test_status_reports_everything_the_ui_needs(stack):
    orch, ws, _ = stack
    _run_to_gate(orch, ws)
    status = orch.status()
    for key in ("phase", "running", "liveness", "org", "policy", "cost", "gate",
                "staffing_gaps", "instructions", "constraints"):
        assert key in status, f"{key} missing from status"
    assert status["phase"] == "awaiting_gate"
    assert status["org"], "the roster must be visible"


def test_status_without_a_run_is_idle(stack):
    orch, _, _ = stack
    assert orch.status()["phase"] == "idle"


def test_pause_and_abort_without_a_run_are_refused(stack):
    orch, _, _ = stack
    assert orch.pause() is False


# ── phases ───────────────────────────────────────────────────────────────────


def test_waiting_phases_are_distinct_from_terminal_ones():
    assert RunPhase.AWAITING_GATE.waiting and not RunPhase.AWAITING_GATE.terminal
    assert RunPhase.AWAITING_HUMAN.waiting and not RunPhase.AWAITING_HUMAN.terminal
    assert RunPhase.PAUSED.waiting
    assert RunPhase.DONE.terminal and not RunPhase.DONE.waiting
    assert RunPhase.FAILED.terminal
    assert not RunPhase.RUNNING.waiting and not RunPhase.RUNNING.terminal


def test_a_run_round_trips_through_its_dict(stack):
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    orch.instruct("keep it small", run=run, as_constraint=True)
    restored = Run.from_dict(run.as_dict(), workspace=ws)
    assert restored.slug == run.slug
    assert restored.constraints == run.constraints
    assert restored.phase is run.phase


def test_a_run_tolerates_an_unknown_field_on_reload(stack):
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    data = run.as_dict()
    data["future_field"] = {"nested": True}
    assert Run.from_dict(data, workspace=ws).slug == run.slug
