#!/usr/bin/env python3
"""Phase 5 eval tests — the behavioural suite and its regression gate.

The suite exists to catch the failure unit tests cannot: a run that completes and is wrong. These tests
assert the suite itself is sound — that its scenarios are all implemented, that its gate actually
blocks, and that it cannot silently pass by skipping.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.evals import runner as evals


# ── the suite is sound ───────────────────────────────────────────────────────


def test_the_scenario_file_is_valid_json():
    data = evals.load_scenarios()
    assert data["scenarios"]


def test_every_declared_scenario_is_implemented():
    """A scenario named but unimplemented is a failure, not a skip — a skipped safety check is
    how a suite decays into decoration."""
    declared = set(evals.load_scenarios()["scenarios"])
    implemented = set(evals.CHECKS)
    assert declared == implemented, (
        f"declared but unimplemented: {sorted(declared - implemented)}; "
        f"implemented but undeclared: {sorted(implemented - declared)}"
    )


def test_every_scenario_declares_an_invariant_and_a_why():
    """A scenario that does not state the property it tests cannot be judged."""
    for name, spec in evals.load_scenarios()["scenarios"].items():
        assert spec.get("invariant"), f"{name} has no invariant"
        assert spec.get("why"), f"{name} has no 'why'"
        assert spec.get("severity") in ("critical", "high", "medium"), name


def test_an_undeclared_scenario_fails_rather_than_skipping():
    """The suite must not pass silently when a scenario disappears from the file."""
    result = evals.run_suite(scenarios={"scenarios": {"a-scenario-nobody-implemented": {}}})
    assert result.failed == 1
    assert "no implementation" in result.outcomes[0].error


def test_only_filter_restricts_the_run():
    result = evals.run_suite(only=["loop-termination"])
    assert len(result.outcomes) == 1
    assert result.outcomes[0].name == "loop-termination"


# ── the suite passes ─────────────────────────────────────────────────────────


def test_the_whole_suite_passes():
    """Every invariant the system promises must currently hold."""
    result = evals.run_suite()
    failures = [f"{o.name}: {o.error}" for o in result.outcomes if not o.passed]
    assert not failures, "behavioural scenarios failed:\n" + "\n".join(failures)
    assert result.passed >= 15, f"only {result.passed} scenarios ran"


def test_the_suite_covers_every_category():
    """A category with no scenario is an invariant nobody is checking."""
    categories = {check[2] for check in evals.CHECKS.values()}
    for expected in ("graph", "context", "delegation", "policy", "routing", "health",
                     "independence", "gates", "skills", "memory", "security", "idempotency"):
        assert expected in categories, f"no scenario covers {expected}"


def test_each_scenario_reports_a_detail_rather_than_a_bare_pass():
    """The detail is what makes a passing scenario informative rather than a green tick."""
    result = evals.run_suite()
    for outcome in result.outcomes:
        assert outcome.detail, f"{outcome.name} passed without saying what it verified"


def test_scenarios_are_fast_enough_to_run_routinely():
    """A suite nobody runs because it is slow is a suite nobody runs."""
    result = evals.run_suite()
    total_ms = sum(o.duration_ms for o in result.outcomes)
    assert total_ms < 30_000, f"the suite took {total_ms:.0f}ms"


# ── the regression gate ──────────────────────────────────────────────────────


def test_a_clean_run_matches_the_frozen_baseline():
    """The baseline is what makes a behaviour *change* visible rather than a behaviour being tested."""
    baseline = json.loads(evals.BASELINE_PATH.read_text())
    regressions = evals.compare_to_baseline(evals.run_suite(), baseline)
    assert regressions == [], f"regressions against the baseline: {regressions}"


def test_the_gate_detects_a_scenario_that_stopped_passing():
    """A delta comparison, because a total hides a gain on one scenario and a loss on three."""
    baseline = {"results": {"a": {"passed": True}, "b": {"passed": True}}}
    result = evals.ScenarioResult(outcomes=[
        evals.Outcome(name="a", passed=True, check="x"),
        evals.Outcome(name="b", passed=False, check="x", error="broke"),
    ])
    regressions = evals.compare_to_baseline(result, baseline)
    assert len(regressions) == 1
    assert "b" in regressions[0] and "was passing, now failing" in regressions[0]


def test_the_gate_detects_lost_coverage():
    """A scenario that no longer runs is a regression: the invariant stopped being checked."""
    baseline = {"results": {"a": {"passed": True}, "b": {"passed": True}}}
    result = evals.ScenarioResult(outcomes=[evals.Outcome(name="a", passed=True, check="x")])
    regressions = evals.compare_to_baseline(result, baseline)
    assert any("no longer runs" in r for r in regressions)


def test_an_improvement_is_not_a_regression():
    """A scenario that starts passing is a gain, not a failure of the gate."""
    baseline = {"results": {"a": {"passed": False}}}
    result = evals.ScenarioResult(outcomes=[evals.Outcome(name="a", passed=True, check="x")])
    assert evals.compare_to_baseline(result, baseline) == []


def test_a_new_scenario_is_not_a_regression():
    """Adding a scenario must not fail the gate, or nobody would add one."""
    baseline = {"results": {"a": {"passed": True}}}
    result = evals.ScenarioResult(outcomes=[
        evals.Outcome(name="a", passed=True, check="x"),
        evals.Outcome(name="brand-new", passed=True, check="x"),
    ])
    assert evals.compare_to_baseline(result, baseline) == []


def test_no_baseline_means_no_gate():
    """A first run has nothing to compare against, which is not a failure."""
    assert evals.compare_to_baseline(evals.run_suite(), None) == []
    assert evals.compare_to_baseline(evals.run_suite(), {}) == []


def test_a_subset_run_does_not_fail_on_the_scenarios_it_did_not_run():
    """`--only` is a focused development run, so the scenarios it skipped are not lost coverage."""
    baseline = {"results": {"a": {"passed": True}, "b": {"passed": True}}}
    subset = evals.ScenarioResult(outcomes=[evals.Outcome(name="a", passed=True, check="x")])
    assert evals.compare_to_baseline(subset, baseline) != [], "a full run should flag lost coverage"
    assert evals.compare_to_baseline(subset, baseline, subset=True) == []


def test_a_subset_run_still_reports_a_scenario_that_stopped_passing():
    """Skipping the coverage check must not also skip the regression check."""
    baseline = {"results": {"a": {"passed": True}}}
    subset = evals.ScenarioResult(outcomes=[evals.Outcome(name="a", passed=False, check="x",
                                                          error="broke")])
    regressions = evals.compare_to_baseline(subset, baseline, subset=True)
    assert len(regressions) == 1
    assert "was passing, now failing" in regressions[0]


def test_the_exit_code_blocks_on_a_regression():
    assert evals.EXIT_REGRESSION == 1
    assert evals.EXIT_OK == 0


# ── the baseline file ────────────────────────────────────────────────────────


def test_the_baseline_is_frozen_and_covers_every_scenario():
    baseline = json.loads(evals.BASELINE_PATH.read_text())
    assert baseline["results"], "the baseline is empty"
    assert baseline.get("frozen_at"), "the baseline should record when it was frozen"
    declared = set(evals.load_scenarios()["scenarios"])
    assert set(baseline["results"]) == declared, "the baseline and the scenario file disagree"


def test_freezing_writes_a_readable_baseline(tmp_path):
    result = evals.run_suite(only=["loop-termination"])
    target = evals.freeze_baseline(result, tmp_path / "baseline.json")
    payload = json.loads(target.read_text())
    assert payload["scenarios"] == 1
    assert payload["results"]["loop-termination"]["passed"] is True
    assert payload["frozen_at"]


# ── the scenarios test what they claim ───────────────────────────────────────


def test_constraint_survival_scenario_actually_rotates():
    """A scenario that quietly stops exercising its path is a scenario that stopped testing."""
    detail = evals.check_constraint_survival()
    assert "rotation" in detail
    assert "re-pinned" in detail


def test_reviewer_independence_scenario_shows_a_real_model_difference():
    detail = evals.check_reviewer_independence()
    assert "model" in detail, "independence should be model-level where the roster allows"


def test_skill_enforceability_covers_the_whole_library():
    detail = evals.check_skill_enforceability()
    assert "327" in detail
    assert "CR1-CR14" in detail


def test_gate_integrity_scenario_refuses_rather_than_accepts():
    detail = evals.check_gate_integrity()
    assert "refused" in detail


def test_delegation_scenario_exercises_every_named_invariant():
    detail = evals.check_delegation_safety()
    assert "S1-S5" in detail
    assert "S4" in detail and "S6" in detail
