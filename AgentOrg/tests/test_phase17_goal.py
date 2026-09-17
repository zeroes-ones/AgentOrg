#!/usr/bin/env python3
"""Phase 17 tests — the Goal runtime: the loop that continues past a model finishing.

Everything else in the engine is bounded. A Goal is the exception, and the whole safety argument for
allowing an unbounded loop rests on four properties, so those are what these tests pin:

1. **Durability is the file, activation is explicit.** A goal restored from disk is *disarmed*: it can
   never re-arm itself because a process restarted. An unattended loop that resumes itself on boot is
   spend nobody authorised, and that is the failure this friction exists to prevent.
2. **Completion is the agent's call, not the host's.** `update_goal` is the only authority, and it is
   advertised only while a goal is armed.
3. **No ceiling by default, but never blind.** Spend is always accumulated and reported.
4. **A budget is sliced, not reset.** `resume` grants a fresh slice while the total stays visible.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import Config, GoalConfig, load
from engine.goal import Goal, GoalDecision, GoalError, GoalSpend, GoalState
from engine.state import Workspace
from engine.tools import ToolRegistry


# ── the model ────────────────────────────────────────────────────────────────


def _ws(tmp_path, slug="goalws"):
    ws = Workspace.for_project(slug, root=tmp_path / "projects")
    ws.ensure()
    return ws


def test_a_new_goal_is_not_armed(tmp_path):
    """Creating and arming are separate acts: an objective alone must not start spending."""
    goal = Goal.new("Add pagination")
    assert goal.state is GoalState.CLEARED
    assert not goal.state.is_live


def test_a_goal_needs_an_objective():
    with pytest.raises(GoalError, match="needs an objective"):
        Goal.new("   ")


def test_arm_makes_the_loop_live():
    goal = Goal.new("Add pagination")
    goal.arm(by="test")
    assert goal.state.is_live
    assert goal.armed_by == "test"
    assert goal.armed_at


def test_load_returns_a_goal_disarmed(tmp_path):
    """The single most important property in this module.

    A saved goal that is armed must come back **paused**, so continuing costs an explicit `resume`.
    """
    ws = _ws(tmp_path)
    goal = Goal.new("Add pagination")
    goal.arm(by="cli")
    goal.save(ws)

    reloaded = Goal.load(ws)
    assert reloaded.state is GoalState.PAUSED
    assert reloaded.pause_reason == "restored"
    assert not reloaded.state.is_live


def test_load_returns_none_without_a_goal(tmp_path):
    """None rather than an empty goal: 'no objective' and 'not running' are different things."""
    assert Goal.load(_ws(tmp_path)) is None


def test_a_corrupt_goal_is_refused_not_guessed(tmp_path):
    """Resuming from unreadable state is how a run inherits an objective nobody set."""
    ws = _ws(tmp_path)
    Goal.path_for(ws).write_text("{not json")
    with pytest.raises(GoalError, match="corrupt"):
        Goal.load(ws)


def test_completion_records_the_summary():
    goal = Goal.new("Add pagination")
    goal.arm()
    goal.complete("cursor pagination added, 12 tests pass")
    assert goal.state is GoalState.COMPLETED
    assert "12 tests pass" in goal.summary
    # A finished goal is still *open*, which is what makes `resume` meaningful for it.
    assert goal.state.is_open


def test_blocked_is_a_distinct_verdict():
    """A blocker is not a completion and not a failure — it is a question for the user."""
    goal = Goal.new("Add pagination")
    goal.arm()
    goal.block("needs the staging database credentials")
    assert goal.state is GoalState.BLOCKED
    assert "credentials" in goal.blocked_reason


def test_clear_forgets_the_objective_but_keeps_history():
    goal = Goal.new("Add pagination")
    goal.arm()
    goal.clear()
    assert goal.state is GoalState.CLEARED
    assert goal.objective == ""
    assert goal.history


# ── budget ───────────────────────────────────────────────────────────────────


def test_budget_is_off_by_default():
    goal = Goal.new("Add pagination")
    goal.arm()
    goal.record_round(tokens=10_000_000)
    assert goal.token_budget == 0
    assert not goal.budget_reached()


def test_a_positive_budget_bounds_the_slice():
    goal = Goal.new("Add pagination", token_budget=1000)
    goal.arm()
    goal.record_round(tokens=400)
    assert not goal.budget_reached()
    goal.record_round(tokens=800)
    assert goal.budget_reached()


def test_resume_grants_a_fresh_slice_but_keeps_the_total():
    """Per-slice bounding without losing sight of the total spend."""
    goal = Goal.new("Add pagination", token_budget=1000)
    goal.arm()
    goal.record_round(tokens=1200, cost_usd=0.05)
    assert goal.budget_reached()

    goal.pause(reason="budget_spend")
    goal.arm(by="resume")
    assert goal.slice_spend.tokens == 0, "resume must grant a fresh slice"
    assert goal.spend.tokens == 1200, "cumulative spend must survive a resume"
    assert goal.spend.cost_usd == pytest.approx(0.05)


def test_spend_survives_a_save_and_reload(tmp_path):
    ws = _ws(tmp_path)
    goal = Goal.new("Add pagination", token_budget=5000)
    goal.arm()
    goal.record_round(tokens=700, requests=2, cost_usd=0.02)
    goal.save(ws)

    reloaded = Goal.load(ws)
    assert reloaded.spend.tokens == 700
    assert reloaded.spend.requests == 2
    assert reloaded.spend.cost_usd == pytest.approx(0.02)


def test_goal_spend_add_accumulates():
    spend = GoalSpend()
    spend.add(tokens=10, requests=1, cost_usd=0.01, rounds=1)
    spend.add(tokens=5, requests=1, cost_usd=0.02, rounds=1)
    assert (spend.tokens, spend.requests, spend.rounds) == (15, 2, 2)
    assert spend.cost_usd == pytest.approx(0.03)


# ── config ───────────────────────────────────────────────────────────────────


def test_goal_config_defaults_are_off():
    cfg = GoalConfig()
    assert cfg.token_budget == 0
    assert cfg.repeat_call_reminders == (3, 5, 8)
    assert cfg.auto_pass_auto_gates is False


def test_goal_config_rejects_a_negative_budget():
    with pytest.raises(Exception):
        GoalConfig(token_budget=-1)


def test_goal_config_is_part_of_the_loaded_config():
    assert isinstance(load().goal, GoalConfig)


def test_goal_config_accepts_a_ceiling():
    cfg = GoalConfig(token_budget=2_000_000)
    assert cfg.token_budget == 2_000_000


# ── the verdict channel ──────────────────────────────────────────────────────


def test_a_decision_is_consumed_on_read(tmp_path):
    """A decision authorises exactly one transition; a leftover file must not stop the next round."""
    ws = _ws(tmp_path)
    GoalDecision(verdict="complete", summary="done").save(ws)
    first = GoalDecision.consume(ws)
    assert first is not None and first.verdict == "complete"
    assert GoalDecision.consume(ws) is None, "a consumed decision must not be readable again"


def test_an_unknown_verdict_is_not_a_decision(tmp_path):
    ws = _ws(tmp_path)
    GoalDecision.path_for(ws).write_text('{"verdict": "maybe", "summary": "x"}')
    assert GoalDecision.consume(ws) is None


# ── the tool ─────────────────────────────────────────────────────────────────


def test_update_goal_is_advertised_only_when_armed(tmp_path):
    """A capability not in play should not be in the prompt — and the tool block feeds the prefix."""
    ws = _ws(tmp_path)
    without = ToolRegistry(workspace_root=ws.path)
    assert "update_goal" not in without.names()

    with_goal = ToolRegistry(workspace_root=ws.path, goal_workspace=ws)
    assert "update_goal" in with_goal.names()


def test_update_goal_writes_a_consumable_decision(tmp_path):
    ws = _ws(tmp_path)
    registry = ToolRegistry(workspace_root=ws.path, goal_workspace=ws)
    result = registry.call("update_goal", {"verdict": "complete", "summary": "shipped"})
    assert result.ok

    decision = GoalDecision.consume(ws)
    assert decision is not None
    assert decision.verdict == "complete"
    assert decision.summary == "shipped"


def test_update_goal_requires_a_summary(tmp_path):
    """A completion with no summary is the silent-done failure the design refuses."""
    ws = _ws(tmp_path)
    registry = ToolRegistry(workspace_root=ws.path, goal_workspace=ws)
    result = registry.call("update_goal", {"verdict": "complete", "summary": "  "})
    assert not result.ok
    assert "summary" in result.text


def test_update_goal_refuses_a_bad_verdict(tmp_path):
    ws = _ws(tmp_path)
    registry = ToolRegistry(workspace_root=ws.path, goal_workspace=ws)
    result = registry.call("update_goal", {"verdict": "probably", "summary": "x"})
    assert not result.ok
    assert "complete" in result.text and "blocked" in result.text


# ── the orchestrator surface ─────────────────────────────────────────────────


def _orchestrator(tmp_path, slug="gorch"):
    from engine.library import resolve
    from engine.orchestrator import Orchestrator

    ws = _ws(tmp_path, slug)
    return Orchestrator(config=load(), library=resolve(), workspace=ws), ws


def test_orchestrator_starts_with_no_goal(tmp_path):
    orch, _ = _orchestrator(tmp_path)
    assert orch.goal() is None
    assert orch.goal_status()["state"] == "cleared"


def test_orchestrator_goal_lifecycle(tmp_path):
    orch, _ = _orchestrator(tmp_path)
    orch.goal_set("Add pagination", by="test")
    assert orch.goal_status()["live"] is True

    orch.goal_pause()
    assert orch.goal_status()["pause_reason"] == "manual"

    orch.goal_resume()
    assert orch.goal_status()["state"] == "armed"

    orch.goal_clear()
    assert orch.goal_status()["objective"] == ""


def test_orchestrator_reloads_the_goal_disarmed(tmp_path):
    """Proven through the real class, because that is where the safety property has to hold."""
    from engine.library import resolve
    from engine.orchestrator import Orchestrator

    orch, ws = _orchestrator(tmp_path)
    orch.goal_set("Add pagination", by="test")
    assert orch.goal_status()["live"] is True

    fresh = Orchestrator(config=load(), library=resolve(), workspace=ws)
    status = fresh.goal_status()
    assert status["state"] == "paused"
    assert status["pause_reason"] == "restored"
    assert status["live"] is False


def test_orchestrator_goal_is_reported_by_status(tmp_path):
    """The console reads the goal from the status it already polls."""
    orch, _ = _orchestrator(tmp_path)
    orch.goal_set("Add pagination")
    assert orch.status()["goal"]["objective"] == "Add pagination"


def test_requiring_a_goal_that_does_not_exist_is_an_error(tmp_path):
    from engine.orchestrator import OrchestratorError

    orch, _ = _orchestrator(tmp_path)
    with pytest.raises(OrchestratorError, match="no goal"):
        orch.goal_pause()


def test_the_run_context_carries_the_goal(tmp_path):
    """The executing subprocess only learns about the goal through the context document."""
    from engine.runcontext import RunContext

    context = RunContext(goal_active=True, goal_objective="Add pagination")
    round_tripped = RunContext.from_dict(context.as_dict())
    assert round_tripped.goal_active is True
    assert round_tripped.goal_objective == "Add pagination"
