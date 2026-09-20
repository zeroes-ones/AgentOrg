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


@pytest.fixture
def workspace(tmp_path):
    """A bare project workspace, for the tests that only inspect the on-disk layout.

    Separate from `stack` because these assert the *files*, not a run: which file is the orchestrator's
    checkpoint and which is the runner's. It was missing, and the runner used here treats an unknown
    fixture as a skip rather than an error — so two tests pinning the two-writer guarantee were being
    skipped silently, which is worse than failing.
    """
    ws = Workspace.for_project("gateprobe", root=tmp_path / "projects")
    ws.ensure()
    return ws


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
    """The Owner approves a graph, and sees what it needs before approving it.

    `auto_staff=False` forces the gap to be *reported* rather than filled, which is the honest
    "show me what is missing" mode — and the one an Owner who declined auto-hiring gets. The
    auto-staffing path is asserted separately.
    """
    orch, _, _ = stack
    run = orch.prepare("Build a booking API with auth and payments", slug="booking",
                       auto_staff=False)
    assert run.phase is RunPhase.AWAITING_APPROVAL
    assert run.plan is not None and run.plan.validation.valid
    assert run.manifest_path.is_file()
    assert run.bindings, "a planned run must be bound to the roster"
    assert run.staffing_gaps, "the default company does not staff every skill the planner emits"
    assert all("skill" in gap for gap in run.staffing_gaps)


def test_prepare_staffs_the_gaps_by_default(stack):
    """The other half: with auto-staffing on (the default), a gap is filled, not just reported.

    The plan was already shown to need a capability nobody holds; the engine creates a helper for it
    on the default model, so the graph the Owner approves is runnable instead of stalling three nodes
    in. A plan whose gaps are all filled reports none.
    """
    orch, _, bus = stack
    run = orch.prepare("Build a booking API with auth and payments", slug="booking-auto")
    proposed = [e for e in bus.history() if e.type_value == "manifest.proposed"]
    assert proposed, "the graph is still proposed for approval"
    staffed = proposed[-1].payload.get("auto_staffed") or []
    assert staffed, "a gap nobody holds must be staffed on the default model"
    assert all("skill" in entry for entry in staffed)
    assert run.staffing_gaps == [], "the gaps were filled, so none remain"


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


# ── why a run stopped: the difference between "blocked" and understanding ─────


def test_stop_reason_explains_a_guardrail_block():
    """The reported failure: a run ended `pm = blocked / guardrail-blocked` with no explanation.

    The runner logs the reason; `_derive_stop_reason` must lift it into one actionable line.
    """
    from engine.orchestrator import _derive_stop_reason

    class Outcome:
        killed = False
        error = ""

    state = {
        "outcome": "guardrail-block", "phase": "escalated",
        "nodes": {"pm": {"status": "blocked", "verdict": "guardrail-blocked",
                         "summary": "instruction-shaped phrase at summary"}},
        "log": [{"step": 1, "node": "pm", "action": "guardrail",
                 "detail": "a hand-off payload was blocked"}],
    }
    reason = _derive_stop_reason(state, Outcome(), {"outcome": "guardrail-block"})
    assert "pm" in reason
    assert "guardrail" in reason
    assert "blocked" in reason


def test_stop_reason_names_a_blocked_node_and_its_own_words():
    from engine.orchestrator import _derive_stop_reason

    class Outcome:
        killed = False
        error = ""

    state = {
        "outcome": "incomplete", "nodes": {
            "architect": {"status": "blocked", "verdict": "missing_prerequisites",
                          "summary": "needs the product spec first"}},
        "log": [],
    }
    reason = _derive_stop_reason(state, Outcome(), {})
    assert "architect" in reason and "missing_prerequisites" in reason
    assert "needs the product spec first" in reason


def test_stop_reason_is_empty_for_a_clean_run():
    from engine.orchestrator import _derive_stop_reason

    class Outcome:
        killed = False
        error = ""

    assert _derive_stop_reason({"outcome": "complete", "log": [], "nodes": {}}, Outcome(), {}) == ""


def test_settle_preserves_the_node_summary(stack):
    """A node's summary is why it is not done; keeping only status/verdict loses the reason."""
    orch, ws, _ = stack
    run = _run_to_gate(orch, ws)
    record = (run.outcome.get("nodes") or {}).get("dev") or {}
    assert "summary" in record, "the per-node summary must survive settling"
    assert record["summary"], "the stub reported a summary; it must not be dropped"


def test_run_as_dict_carries_the_stop_reason(stack):
    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml")
    run.stop_reason = "dev is blocked (guardrail-blocked)"
    assert Run.from_dict(run.as_dict(), workspace=ws).stop_reason == run.stop_reason


# ── an attached project keeps its own state ──────────────────────────────────


def test_a_run_on_an_attached_project_writes_state_into_it(tmp_path, config, library):
    """The split-brain bug: run state landed in a sibling `root/<slug>` dir while the trace landed in
    the attached folder — so `status`, reading the folder, saw no run and showed an idle project."""
    project = tmp_path / "MyProject"
    project.mkdir()
    (project / ".git").mkdir()
    ws = Workspace.attach(project)
    ws.ensure()
    orch = Orchestrator(config=config, library=library, workspace=ws,
                        bus=EventBus(run_id="r", history_size=200))

    run = orch.prepare("use the CEO skill and capture market")

    # The checkpoint, the manifest and the state directory are all inside the attached folder.
    assert run.workspace.path == project.resolve()
    assert run.workspace.checkpoint_path.is_file()
    assert run.manifest_path.parent == project.resolve()
    # And nothing leaked into a sibling directory named after the slug.
    assert not (tmp_path / run.slug).exists()


def test_the_runner_checkpoint_and_the_orchestrator_checkpoint_are_different_files(workspace):
    """They must not share `run_state.json`, or continuation restarts the whole graph.

    The bug this pins is the worst kind: both sides wrote `run_state.json`, in turn, with
    incompatible shapes. The runner writes `{workflow, manifest_sha, nodes}`; the orchestrator writes
    `{run_id, phase, gate, outcome, …}`. So after the orchestrator's post-run write, the runner's
    `load_state` found no `workflow`/`manifest_sha` and returned None — meaning **every continuation
    restarted from scratch** and re-ran the node that had just failed. Approving a gate therefore
    re-hit the identical contract violation and parked again, for ever. That is precisely the
    "it keeps rejecting at some point" report.
    """
    assert workspace.checkpoint_path != workspace.runner_state_path
    assert workspace.runner_state_path.name == "runner_state.json"
    assert workspace.checkpoint_path.name == "run_state.json"


def test_the_orchestrator_write_does_not_destroy_the_runners_checkpoint(workspace, stack):
    """The property that makes resume possible: one writer per file."""
    import json

    orch, ws, _ = stack
    ws.runner_state_path.write_text(json.dumps({
        "workflow": "gateprobe", "manifest_sha": "abc123",
        "nodes": {"dev": {"status": "done", "verdict": "ok"}}}))

    run = orch.adopt(ws.path / "gateprobe.yaml", slug="gateprobe")
    orch._persist(run)          # the orchestrator's own checkpoint write

    runner = json.loads(ws.runner_state_path.read_text())
    assert runner["workflow"] == "gateprobe", "the runner's checkpoint must survive intact"
    assert runner["manifest_sha"] == "abc123"
    assert "dev" in runner["nodes"], "the completed node must still be recorded, or resume redoes it"
    # And the orchestrator's own file was written.
    assert json.loads(ws.checkpoint_path.read_text())["run_id"] == run.run_id


def test_approving_the_start_node_advances_the_run_instead_of_reparking(stack):
    """The deepest defect behind "it keeps rejecting at some point".

    Three faults hid behind one symptom. (1) The orchestrator and the runner shared `run_state.json`,
    so the orchestrator's write destroyed the runner's `{workflow, manifest_sha, nodes}` shape and
    every continuation restarted the graph. (2) With that fixed, the runner's frontier is `[start]`
    unless `start` is terminal — and an *approved* start node is terminal — so a resume had no
    frontier and re-escalated. (3) Repointing `start` alone makes the earlier nodes unreachable from
    `start`, which the library's validator refuses (`nodes not reachable from start`), surfacing as
    `the runner exited 1`.

    The fix releases the node in the checkpoint, moves the manifest's `start` to its successor, and
    prunes the finished node so the graph stays reachable. This test pins the manifest half, because
    that is the part that must satisfy the library's own validator.
    """
    import re

    orch, ws, _ = stack
    manifest = ws.path / "gateprobe.yaml"
    text = manifest.read_text()
    assert re.search(r"^start:\s*dev\s*$", text, re.M), "the fixture starts at dev"

    orch._advance_manifest_past(manifest, released="dev", successor="release")
    rewritten = manifest.read_text()
    assert re.search(r"^start:\s*release\s*$", rewritten, re.M), "start must move to the successor"
    assert "- id: dev" not in rewritten, "the finished node must be pruned for reachability"
    assert "- from: dev" not in rewritten, "and its edges with it"
    assert "- id: release" in rewritten, "the successor must survive"


def test_releasing_a_gate_marks_the_node_done_for_downstream(stack):
    """`done`, not `needs_review`: the Owner accepted the work, so downstream may rely on it."""
    import json

    orch, ws, _ = stack
    run = orch.adopt(ws.path / "gateprobe.yaml", slug="gateprobe")
    orch.approve(run)
    orch.execute(run, executor=ws.path / "stub.py")
    assert run.gate is not None and run.gate.gate_id == "release"

    # The runner checkpoint carries the node the gate judged, still not-done.
    ws.runner_state_path.write_text(json.dumps({
        "workflow": "gateprobe", "manifest_sha": "x",
        "nodes": {"dev": {"status": "needs_review", "verdict": "awaiting_owner"}}}))
    # The gate's dossier names the node it parked on — that is how the release finds it.
    run.gate.dossier = {"node": "dev"}
    orch._release_node_for_resume(run, run.gate)
    state = json.loads(ws.runner_state_path.read_text())
    assert state["nodes"]["dev"]["status"] == "done"
    assert state["phase"] == "ready"


def test_a_runner_shaped_checkpoint_is_not_a_resumable_run(stack):
    """`status` crashed with a schema error on a workspace whose run_state.json was the runner's.

    Both sides used to write `run_state.json`, so on a workspace that ran before they were separated
    the orchestrator's checkpoint is gone — replaced by the runner's `{workflow, manifest_sha, nodes}`
    shape, which carries no `run_state_version`. The registry refused it with "no migration path for
    run_state from 0.0.0 to 1.0.0", and because `load` raised, `status`, `decide` and `instruct` all
    failed on a project whose node outcomes the Flow panel was displaying without trouble. Returning
    None is the right answer: the document is not ours, so it is not a schema violation to report.
    """
    import json

    orch, ws, _ = stack
    ws.checkpoint_path.write_text(json.dumps({
        "workflow": "gateprobe", "manifest_sha": "a69609c3426b",
        "nodes": {"dev": {"status": "needs_review", "verdict": "contract-violation"}},
    }))

    assert orch.load("gateprobe") is None, "the runner's checkpoint is not our run"


def test_a_genuinely_ours_checkpoint_is_still_version_checked(stack):
    """The runner-shape guard must not swallow a document that *is* ours but from a newer schema."""
    import json

    orch, ws, _ = stack
    # A future checkpoint carries both our markers and an impossible version.
    ws.checkpoint_path.write_text(json.dumps({
        "run_id": "run_x", "run_phase_version": "1.0.0",
        "run_state_version": "9.0.0", "phase": "running",
    }))

    with pytest.raises(OrchestratorError, match="cannot open this run"):
        orch.load("gateprobe")


# ── the goal loop's round spend comes from the cost ledger ────────────────────
#
# `_round_spend` used to ask the *decision* ledger (`self.ledger`, an `org.ledger.Ledger`) for a
# `snapshot()` it does not define, and for keys (`nodes`, `runs`, `tokens`, `cost_usd`) it does not
# write. Every round therefore took the exception branch and reported `0, 0, 0.0` — the exact
# fabricated accounting figure its own docstring forbids. The cost ledger lives in the runner
# subprocess, so these tests drive a real `Gateway` to write its own snapshot into the run's trace,
# which is the durable record the orchestrator can actually read.


def _gateway_into_trace(orch, ws, run, *, prompt_tokens, completion_tokens, model="gpt-4o-mini",
                        locality="cloud"):
    """Run one real completion whose ledger snapshot lands in the run's trace.jsonl."""
    from engine.bus import EventBus
    from engine.gateway import Gateway
    from engine.providers.base import ChatRequest, Message, Role
    from engine.providers.fake import FakeProvider, ScriptedReply
    from engine.tokens import TokenEstimator

    bus = EventBus(run_id=run.run_id, trace_path=ws.trace_path)
    provider = FakeProvider(provider_id="fake", locality=locality,
                            script=[ScriptedReply(text="ok", prompt_tokens=prompt_tokens,
                                                  completion_tokens=completion_tokens, model=model)])
    gateway = Gateway(orch.config, {"fake": provider}, estimator=TokenEstimator(),
                      bus=bus, run_id=run.run_id)
    gateway.complete(
        ChatRequest(model=model, messages=[Message.text_message(Role.USER, "hi")]),
        provider_id="fake", agent_id="ag_1", node_id="dev",
    )
    bus.close()
    return gateway


def _adopted_run(orch, ws):
    from engine.goal import Goal

    run = orch.adopt(ws.path / "gateprobe.yaml", slug="gateprobe")
    orch._goal = Goal.new("keep going")
    orch._goal.arm()
    return run


def test_round_spend_reports_the_amount_the_ledger_charged(stack):
    """A run that spent a known amount reports that amount — not the zero the exception branch gave."""
    orch, ws, _ = stack
    run = _adopted_run(orch, ws)
    gateway = _gateway_into_trace(orch, ws, run, prompt_tokens=1000, completion_tokens=500)

    tokens, requests, cost, unknown = orch._round_spend(run)
    assert (tokens, requests, unknown) == (1500, 1, 0)
    assert cost == pytest.approx(gateway.ledger.total_usd)
    assert cost > 0, "a run that spent money must not report zero"


def test_round_spend_is_a_delta_against_what_the_goal_already_recorded(stack):
    """`Goal.record_round` accumulates, so a round reports only its own share."""
    orch, ws, _ = stack
    run = _adopted_run(orch, ws)
    _gateway_into_trace(orch, ws, run, prompt_tokens=1000, completion_tokens=500)
    first = orch._round_spend(run)
    orch._goal.record_round(tokens=first[0], requests=first[1], cost_usd=first[2])
    assert orch._round_spend(run) == (0, 0, 0.0, 0), "already-accounted spend must not be counted twice"


def test_round_spend_distinguishes_unreported_from_zero(stack):
    """No readable trace is "unknown"; a readable run that made no call is a genuine zero."""
    orch, ws, _ = stack
    run = _adopted_run(orch, ws)
    ws.trace_path.unlink(missing_ok=True)
    assert orch._round_spend(run) == (0, 0, None, 0)

    ws.trace_path.parent.mkdir(parents=True, exist_ok=True)
    ws.trace_path.write_text("", encoding="utf-8")
    assert orch._round_spend(run) == (0, 0, 0.0, 0)


def test_an_unmeasured_call_makes_the_round_cost_a_floor_not_a_total(stack):
    """The provider reported nothing, so the figure is unknown — and the goal must carry that."""
    orch, ws, _ = stack
    run = _adopted_run(orch, ws)
    _gateway_into_trace(orch, ws, run, prompt_tokens=None, completion_tokens=None)

    tokens, requests, cost, unknown = orch._round_spend(run)
    assert (tokens, requests, cost, unknown) == (0, 1, 0.0, 1)
    orch._goal.record_round(tokens=tokens, requests=requests, cost_usd=cost,
                            unknown_cost_calls=unknown)
    assert orch._goal.spend.cost_complete is False, "an unreported cost is not a complete total"
    assert orch._goal.spend.as_dict()["cost_complete"] is False


def test_a_genuine_no_spend_round_stays_complete(stack):
    """The counterpart: nothing spent is a known total, not a reporting gap."""
    orch, ws, _ = stack
    run = _adopted_run(orch, ws)
    ws.trace_path.unlink(missing_ok=True)
    ws.trace_path.parent.mkdir(parents=True, exist_ok=True)
    ws.trace_path.write_text("", encoding="utf-8")
    tokens, requests, cost, unknown = orch._round_spend(run)
    orch._goal.record_round(tokens=tokens, requests=requests, cost_usd=cost,
                            unknown_cost_calls=unknown)
    assert orch._goal.spend.cost_complete is True
    assert orch._goal.spend.cost_usd == 0.0
