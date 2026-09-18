#!/usr/bin/env python3
"""Phase 23 tests — the Mission: a durable *why* above the goals that serve it.

A Goal answers "what is being worked on right now". A Mission answers "what is all this for, which
step are we on, and what is next" — the level that makes a long-running org legible as one effort
rather than a sequence of unrelated objectives.

So these tests assert the hierarchy's safety properties first (a mission never spends, never
re-arms itself, never contradicts its own list) and its bookkeeping second.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.mission import (
    MISSION_VERSION,
    Mission,
    MissionError,
    MissionState,
    Objective,
    ObjectiveState,
)
from engine.state import Workspace


@pytest.fixture
def workspace(tmp_path):
    ws = Workspace.for_project("mission-probe", root=tmp_path / "projects")
    ws.ensure()
    return ws


# ── construction ─────────────────────────────────────────────────────────────


def test_a_mission_needs_a_statement():
    with pytest.raises(MissionError, match="needs a statement"):
        Mission.new("   ")


def test_a_new_mission_is_unarmed():
    """Stating a purpose must not begin working it — the same rule `Goal` draws for its objective."""
    mission = Mission.new("ship it")
    assert mission.armed is False
    assert mission.state() is MissionState.EMPTY


def test_an_objective_needs_text_and_refuses_a_duplicate():
    mission = Mission.new("ship it")
    mission.add_objective("get auth green")
    with pytest.raises(MissionError, match="already"):
        mission.add_objective("get auth green")


def test_objectives_can_be_inserted_at_a_position():
    mission = Mission.new("ship it")
    mission.add_objective("one")
    mission.add_objective("three")
    mission.add_objective("two", at=1)
    assert [o.text for o in mission.objectives] == ["one", "two", "three"]


# ── arming, pausing, and the derived state ───────────────────────────────────


def test_a_mission_with_no_objectives_cannot_be_worked():
    with pytest.raises(MissionError, match="no objectives"):
        Mission.new("ship it").arm()


def test_arming_and_activating_make_it_live():
    mission = Mission.new("ship it")
    mission.add_objective("get auth green")
    mission.arm(by="test")
    mission.activate()
    assert mission.state() is MissionState.ACTIVE
    assert mission.state().is_live


def test_an_unarmed_mission_with_an_active_objective_is_paused_not_active():
    """A loaded mission is paused; deriving state from `armed` keeps that honest."""
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.activate()
    # Never armed, so it is paused even though an objective is "active".
    assert mission.state() is MissionState.PAUSED


def test_an_armed_mission_with_pending_work_is_active():
    """`mission arm` must not look like a no-op: armed with work left is active."""
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.arm()
    assert mission.state() is MissionState.ACTIVE


def test_only_one_objective_is_active_at_a_time():
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.add_objective("b")
    mission.activate(0)
    mission.activate(1)
    assert mission.active_index() == 1
    assert mission.objectives[0].state is ObjectiveState.PENDING


# ── progress and advance ─────────────────────────────────────────────────────


def test_advance_finishes_the_active_step_and_moves_to_the_next():
    mission = Mission.new("ship it")
    for text in ("a", "b", "c"):
        mission.add_objective(text)
    mission.arm()
    mission.activate()
    nxt = mission.advance(summary="a done")
    assert nxt is not None and nxt.text == "b"
    assert mission.objectives[0].state is ObjectiveState.DONE
    assert mission.objectives[0].summary == "a done"


def test_a_finished_mission_is_completed():
    mission = Mission.new("ship it")
    mission.add_objective("only")
    mission.arm()
    mission.activate()
    assert mission.advance(summary="done") is None
    assert mission.state() is MissionState.COMPLETED


def test_a_skipped_objective_does_not_hold_up_completion():
    """A skipped step must not make a finished mission look part-done."""
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.add_objective("b")
    mission.arm()
    mission.mark(0, "skipped")
    mission.mark(1, "done")
    assert mission.state() is MissionState.COMPLETED
    progress = mission.progress()
    assert progress["total"] == 1 and progress["done"] == 1


def test_a_blocked_objective_blocks_the_mission_once_nothing_is_pending():
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.arm()
    mission.activate()
    mission.mark(0, "blocked", summary="needs a credential")
    assert mission.state() is MissionState.BLOCKED
    assert mission.objectives[0].blocked_reason == "needs a credential"


def test_progress_reports_fraction_and_next():
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.add_objective("b")
    mission.arm()
    mission.activate()
    mission.mark(0, "done")
    progress = mission.progress()
    assert progress["done"] == 1 and progress["total"] == 2
    assert progress["fraction"] == 0.5
    assert progress["next"] == "b"


def test_an_unknown_state_is_refused_with_the_choices():
    mission = Mission.new("ship it")
    mission.add_objective("a")
    with pytest.raises(MissionError, match="pending|active|done"):
        mission.mark(0, "finished")


# ── the safety properties ────────────────────────────────────────────────────


def test_a_mission_does_not_spend_and_does_not_carry_a_budget():
    """Arming a mission must not arm a goal: the spend decision stays one level down."""
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.arm()
    assert not hasattr(mission, "token_budget")


def test_a_mission_round_trips_through_its_dict(workspace):
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.add_objective("b")
    mission.arm()
    mission.activate()
    restored = Mission.from_dict(mission.as_dict())
    assert restored.statement == mission.statement
    assert [o.text for o in restored.objectives] == ["a", "b"]
    assert restored.objective_now().text == "a"


def test_a_mission_tolerates_an_unknown_field_on_reload():
    mission = Mission.new("ship it")
    data = mission.as_dict()
    data["future_field"] = {"nested": True}
    assert Mission.from_dict(data).statement == "ship it"


def test_a_version_mismatch_is_refused():
    data = Mission.new("ship it").as_dict()
    data["mission_version"] = "99.0.0"
    with pytest.raises(MissionError, match="not compatible"):
        Mission.from_dict(data)


# ── persistence ──────────────────────────────────────────────────────────────


def test_save_then_load_returns_it_disarmed(workspace):
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.arm(by="cli")
    mission.save(workspace)

    loaded = Mission.load(workspace)
    assert loaded is not None
    assert loaded.statement == "ship it"
    # The single most important property: reading is not resuming.
    assert loaded.armed is False
    assert loaded.pause_reason == "restored"


def test_loading_a_workspace_with_no_mission_is_none(workspace):
    assert Mission.load(workspace) is None


def test_a_corrupt_mission_file_is_refused_not_ignored(workspace):
    Mission.path_for(workspace).write_text("{ not json", encoding="utf-8")
    with pytest.raises(MissionError, match="corrupt"):
        Mission.load(workspace)


def test_public_exposes_the_derived_shape_the_ui_reads(workspace):
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.arm()
    mission.activate()
    public = mission.public()
    for key in ("statement", "state", "live", "progress", "now", "objectives", "counts"):
        assert key in public
    assert public["now"]["text"] == "a"
    assert public["counts"]["total"] == 1


def test_clearing_keeps_the_history(workspace):
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.arm()
    mission.clear()
    assert mission.statement == ""
    assert mission.objectives == []
    assert any(entry["kind"] == "cleared" for entry in mission.history)


def test_objective_serialisation_is_stable():
    objective = Objective(text="a", state=ObjectiveState.ACTIVE)
    assert Objective.from_dict(objective.as_dict()).text == "a"
    assert MISSION_VERSION


# ── the orchestrator's mission API ───────────────────────────────────────────


@pytest.fixture
def orchestrator(workspace):
    from engine.bus import EventBus
    from engine.config import load
    from engine.library import resolve
    from engine.orchestrator import Orchestrator

    return Orchestrator(config=load(), library=resolve(), workspace=workspace,
                        bus=EventBus(run_id="mission", history_size=200))


def test_the_orchestrator_sets_arms_and_starts_a_mission(orchestrator):
    orchestrator.mission_set("ship the MVP", objectives=["auth green", "pagination"])
    orchestrator.mission_arm()
    status = orchestrator.mission_status()
    assert status["state"] == "active"
    assert status["counts"]["total"] == 2

    started = orchestrator.mission_start(index=0, armed=False)
    assert started["objective"]["text"] == "auth green"
    # The mission does not arm the goal itself: `armed=False` means no spend begins.
    assert started["goal"]["live"] is False
    assert orchestrator.mission_status()["now"]["text"] == "auth green"


def test_a_mission_progresses_when_its_goal_reports_complete(orchestrator):
    """The autonomy the hierarchy exists for: a finished step advances the mission on its own."""
    orchestrator.mission_set("ship it", objectives=["a", "b"])
    orchestrator.mission_arm()
    orchestrator.mission_start(index=0, armed=True)
    # The goal reports complete, as `update_goal(complete)` would.
    orchestrator._goal.complete("a is done")
    orchestrator.mission_sync()
    mission = orchestrator.mission()
    assert mission.objectives[0].state is ObjectiveState.DONE
    assert mission.objective_now().text == "b"


def test_a_mission_blocks_when_its_goal_reports_blocked(orchestrator):
    orchestrator.mission_set("ship it", objectives=["a"])
    orchestrator.mission_arm()
    orchestrator.mission_start(index=0, armed=True)
    orchestrator._goal.block("needs a credential")
    orchestrator.mission_sync()
    mission = orchestrator.mission()
    assert mission.objectives[0].state is ObjectiveState.BLOCKED
    assert mission.state() is MissionState.BLOCKED


def test_an_open_mission_is_not_clobbered_by_a_new_statement(orchestrator):
    orchestrator.mission_set("ship it", objectives=["a"])
    orchestrator.mission_arm()
    with pytest.raises(Exception):
        orchestrator.mission_set("a completely different mission")


def test_mission_status_is_empty_before_a_mission_is_set(orchestrator):
    status = orchestrator.mission_status()
    assert status["state"] == "empty"
    assert status["objectives"] == []


def test_mission_state_terminal_and_open():
    """`terminal`/`is_open` classify the derived states — used by the set guard."""
    assert MissionState.COMPLETED.terminal and not MissionState.COMPLETED.is_open
    assert MissionState.BLOCKED.terminal
    assert MissionState.EMPTY.terminal
    assert MissionState.ACTIVE.is_open and not MissionState.ACTIVE.terminal
    assert MissionState.PAUSED.is_open


def test_the_state_property_is_accessible_on_a_derived_state(workspace):
    """The `mission_set` guard calls `existing.state().terminal`; the property must exist."""
    mission = Mission.new("ship it")
    mission.add_objective("a")
    mission.arm()
    assert mission.state().terminal is False
    mission.mark(0, "done")
    assert mission.state() is MissionState.COMPLETED
    assert mission.state().terminal is True
