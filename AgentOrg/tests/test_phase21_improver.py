#!/usr/bin/env python3
"""Phase 21 tests — the self-improvement loop, and the boundary that makes it safe.

Four stages, and the tests are organised around the claim each one makes:

1. **Detect** — Q1 is *measurement*, not opinion. Every finding is derived from recorded evidence, so
   a model asked to "find bugs" cannot inject imaginary ones. The closed kind vocabulary is what makes
   findings countable and memorable across cycles.
2. **Draft** — Q2 produces a proposal, and the safety check runs **before any model involvement**. A
   finding pointing at the judging machinery is refused whatever a model would have said.
3. **Validate** — Q3 is a baseline *delta*. A change that is merely not-worse is not an improvement.
4. **Promote** — Q4 is the Owner's. Nothing is applied, ever, by any code path in this module.

The boundary tests are the important ones: an improver that can edit its own eval gate can make
anything pass, so a system whose safety is enforced by code it may rewrite does not have that property.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.improver import (
    SAFETY_SURFACES,
    Finding,
    Improver,
    ImproverError,
    Proposal,
    Validation,
    is_safety_surface,
)
from engine.state import Workspace


def _workspace(tmp_path, slug="improver"):
    ws = Workspace.for_project(slug, root=tmp_path / "projects")
    ws.ensure()
    return ws


def _spans(ws, rows):
    """Write a spans file, the way the telemetry exporter does."""
    path = ws.state_dir / "telemetry" / "spans.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


# ── the boundary ─────────────────────────────────────────────────────────────


def test_the_judging_machinery_is_a_safety_surface():
    """Every path that decides whether a proposal is good must be refused."""
    for path in ("engine/evals/runner.py", "engine/guardrail.py", "engine/goal.py",
                 "engine/host.py", "engine/config.py", "engine/improver.py",
                 "credentials.json"):
        assert is_safety_surface(path), f"{path} must be refused"


def test_the_app_lifecycle_is_a_safety_surface():
    """The delegate and the scene are what keep the engine alive; editing them kills the app."""
    assert is_safety_surface("macos/Sources/AgentOrgKit/ConsoleAppDelegate.swift")
    assert is_safety_surface("macos/Sources/AgentOrg/App.swift")


def test_a_safety_path_cannot_be_disguised():
    """A check that a slightly different spelling defeats is not a check."""
    assert is_safety_surface("./engine/evals/runner.py")
    assert is_safety_surface("/Users/me/proj/engine/config.py")
    assert is_safety_surface("engine/evals/")
    assert is_safety_surface("engine/evals/scenarios.json")


def test_the_ordinary_surfaces_are_allowed():
    """The boundary must not be so broad that nothing can ever be improved."""
    for path in (".agentorg/skills/reviewer/SKILL.md", "engine/prompts.py",
                 "engine/cache.py", ".agentorg/roster.json", "docs/USAGE.md"):
        assert not is_safety_surface(path), f"{path} should be improvable"


def test_a_proposal_aimed_at_the_gate_is_refused_with_the_path_named():
    """Refused before any model is involved — and *named*, because a silently discarded proposal
    teaches nobody anything."""
    improver = Improver(workspace=pathlib.Path("/tmp"))
    # A draft function that would eagerly edit the gate if it were permitted to.
    improver.draft_fn = lambda f: ("tighten the gate", "--- a\n+++ b\n", ["engine/evals/runner.py"])
    finding = Finding(kind="stagnant_loop", path="engine/evals/runner.py", subject="gate",
                      detail="a scenario keeps failing")
    proposal = improver.draft(finding)
    assert proposal.state == "refused"
    assert "engine/evals/runner.py" in proposal.refusal
    assert "not configurable" in proposal.refusal


def test_a_refused_proposal_is_never_written(tmp_path):
    """The only way into the proposals directory is a proposal that earned it."""
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws)
    improver.draft_fn = lambda f: ("", "", ["engine/guardrail.py"])
    finding = Finding(kind="repeated_rejection", path="engine/guardrail.py", subject="x", detail="y")
    proposal = improver.draft(finding)
    assert improver.promote(proposal) is None
    assert not list(improver.proposals_dir().glob("*.md"))


def test_the_safety_list_is_code_not_configuration():
    """A setting would imply a supported alternative. There is none, so it is a module constant."""
    assert isinstance(SAFETY_SURFACES, tuple)
    assert any("evals" in s for s in SAFETY_SURFACES)
    # And it is not reachable through the improver's constructor, so it cannot be overridden by data.
    assert "surfaces" not in Improver.__dataclass_fields__
    assert "safety_surfaces" not in Improver.__dataclass_fields__


# ── detect: measurement, not opinion ─────────────────────────────────────────


def test_detection_needs_evidence(tmp_path):
    """An empty history produces no findings. A detector that always finds something is a detector
    that invents bugs."""
    assert Improver(workspace=_workspace(tmp_path)).detect() == []


def test_a_finding_kind_must_be_known():
    """A closed vocabulary, so findings can be counted, filtered and remembered across cycles."""
    with pytest.raises(ImproverError, match="unknown finding kind"):
        Finding(kind="vibes", detail="the reviewer seems weak")


def test_a_repeated_rejection_is_found(tmp_path):
    """A repeated verdict is a property of the *procedure*, not of the attempt."""
    ws = _workspace(tmp_path)
    _spans(ws, [{"node": "fixer", "verdict": "changes_requested"}] * 3)
    findings = Improver(workspace=ws).detect()
    assert [f.kind for f in findings] == ["repeated_rejection"]
    assert findings[0].evidence["count"] == 3
    assert findings[0].severity == "major"


def test_a_single_rejection_is_not_a_pattern(tmp_path):
    """One rejection is normal work. Calling it a defect is how a detector becomes noise."""
    ws = _workspace(tmp_path)
    _spans(ws, [{"node": "fixer", "verdict": "changes_requested"}])
    assert Improver(workspace=ws).detect() == []


def test_a_stagnant_loop_is_found(tmp_path):
    ws = _workspace(tmp_path)
    _spans(ws, [{"node": "reviser", "verdict": "needs_review", "iterations": 4}])
    kinds = [f.kind for f in Improver(workspace=ws).detect()]
    assert "stagnant_loop" in kinds


def test_a_cost_outlier_is_found(tmp_path):
    ws = _workspace(tmp_path)
    _spans(ws, [
        {"node": "cheap", "verdict": "pass", "cost_usd": 0.01, "cost_measured": True},
        {"node": "cheap", "verdict": "pass", "cost_usd": 0.01, "cost_measured": True},
        {"node": "middle", "verdict": "pass", "cost_usd": 0.02, "cost_measured": True},
        {"node": "pricey", "verdict": "pass", "cost_usd": 0.50, "cost_measured": True},
    ])
    findings = [f for f in Improver(workspace=ws).detect() if f.kind == "budget_burn"]
    assert findings and findings[0].subject == "pricey"


def test_two_nodes_cannot_contain_an_outlier(tmp_path):
    """With two nodes the median of the medians *is* the outlier, so the comparison would find nothing —
    and reporting nothing as though it were a result is worse than declining to judge."""
    ws = _workspace(tmp_path)
    _spans(ws, [
        {"node": "cheap", "verdict": "pass", "cost_usd": 0.01, "cost_measured": True},
        {"node": "pricey", "verdict": "pass", "cost_usd": 0.50, "cost_measured": True},
    ])
    assert [f for f in Improver(workspace=ws).detect() if f.kind == "budget_burn"] == []


def test_an_unmeasured_cost_is_not_treated_as_zero(tmp_path):
    """The ledger's own rule: `None` means unmeasured, never zero. Treating it as zero would invent an
    outlier out of missing data."""
    ws = _workspace(tmp_path)
    _spans(ws, [
        {"node": "a", "verdict": "pass", "cost_usd": None, "cost_measured": False},
        {"node": "b", "verdict": "pass", "cost_usd": None, "cost_measured": False},
    ])
    assert [f for f in Improver(workspace=ws).detect() if f.kind == "budget_burn"] == []


def test_a_failing_agent_is_found(tmp_path):
    ws = _workspace(tmp_path)
    _spans(ws, [{"node": "n", "verdict": "fail", "agent_id": "ag_bad"}] * 3)
    findings = [f for f in Improver(workspace=ws).detect() if f.kind == "failing_agent"]
    assert findings and findings[0].subject == "ag_bad"
    assert findings[0].evidence["success_rate"] == 0.0


def test_an_agent_with_too_few_attempts_is_not_judged(tmp_path):
    """Three attempts is the floor; below it there is no pattern to see."""
    ws = _workspace(tmp_path)
    _spans(ws, [{"node": "n", "verdict": "fail", "agent_id": "ag_new"}] * 2)
    assert [f for f in Improver(workspace=ws).detect() if f.kind == "failing_agent"] == []


def test_a_staffing_gap_is_found(tmp_path):
    ws = _workspace(tmp_path)
    _spans(ws, [{"node": "g", "verdict": "pass",
                 "attributes": {"staffing_gaps": [{"skill": "devops-engineer", "node_id": "g"}]}}])
    findings = [f for f in Improver(workspace=ws).detect() if f.kind == "unbinding_skills"]
    assert findings and findings[0].subject == "devops-engineer"


def test_detection_survives_a_corrupt_spans_file(tmp_path):
    """A torn trace line must not stop the loop; the rest of the history is still evidence."""
    ws = _workspace(tmp_path)
    path = ws.state_dir / "telemetry" / "spans.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{not json\n"
        + json.dumps({"node": "fixer", "verdict": "changes_requested"}) + "\n"
        + json.dumps({"node": "fixer", "verdict": "changes_requested"}) + "\n",
        encoding="utf-8")
    assert [f.kind for f in Improver(workspace=ws).detect()] == ["repeated_rejection"]


# ── validate: a delta, not a threshold ───────────────────────────────────────


class _Outcome:
    def __init__(self, name, passed):
        self.name, self.passed = name, passed


class _Result:
    def __init__(self, outcomes):
        self.outcomes = outcomes


def test_an_unchanged_tree_is_not_an_improvement_against_the_real_baseline(tmp_path):
    """The real suite, against the real baseline: an unchanged tree produces no regressions — and no
    improvement either, because every scenario in `baseline.json` already passes. Calling the scenario
    the finding names an improvement here is exactly how an empty diff gets stamped as a fix."""
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws)          # defaults to the real suite
    finding = Finding(kind="stagnant_loop", path=".agentorg/skills/", subject="reviser",
                      detail="never converges", scenario="loop-termination")
    proposal = improver.draft(finding)
    validation = improver.validate(proposal)
    assert validation.ran
    assert validation.regressions == []
    assert validation.improved == []
    assert not validation.ok
    assert "already records 'loop-termination' as passing" in validation.detail
    assert improver.promote(proposal) is None


def _baseline_file(tmp_path, monkeypatch, results):
    """Point the real baseline path at a stub, so the failing→passing rule is tested deterministically."""
    from engine.evals import runner

    path = tmp_path / "stub-baseline.json"
    path.write_text(json.dumps({"results": results}), encoding="utf-8")
    monkeypatch.setattr(runner, "BASELINE_PATH", path)


def test_an_improvement_requires_a_failing_to_passing_flip(tmp_path, monkeypatch):
    """The rule the module claims: a scenario counts as improved only when the baseline recorded it as
    failing and the run now passes it. A passing baseline is evidence of nothing."""
    _baseline_file(tmp_path, monkeypatch, {"loop-termination": {"passed": False}})
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws,
                        run_suite=lambda **k: _Result([_Outcome("loop-termination", True)]))
    finding = Finding(kind="stagnant_loop", path=".agentorg/skills/", subject="reviser",
                      detail="x", scenario="loop-termination")
    proposal = improver.draft(finding)
    validation = improver.validate(proposal)
    assert validation.improved == ["loop-termination"]
    assert validation.regressions == []
    assert validation.ok
    assert improver.promote(proposal) is not None


def test_the_converse_passing_baseline_yields_no_improvement(tmp_path, monkeypatch):
    """The same passing result, against a baseline that already had the scenario passing. Nothing
    flipped, so nothing is promoted — this is the exact case the old code called an improvement."""
    _baseline_file(tmp_path, monkeypatch, {"loop-termination": {"passed": True}})
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws,
                        run_suite=lambda **k: _Result([_Outcome("loop-termination", True)]))
    finding = Finding(kind="stagnant_loop", path=".agentorg/skills/", subject="reviser",
                      detail="x", scenario="loop-termination")
    proposal = improver.draft(finding)
    validation = improver.validate(proposal)
    assert validation.improved == []
    assert not validation.ok
    assert improver.promote(proposal) is None
    assert proposal.state == "rejected"


def test_an_unreadable_baseline_evidences_no_improvement(tmp_path, monkeypatch):
    """No baseline means no comparison. A run that cannot be compared must not be read as a pass — and
    regressions stay unreported, which is the opposite of a claim that anything improved."""
    from engine.evals import runner

    monkeypatch.setattr(runner, "BASELINE_PATH", tmp_path / "does-not-exist.json")
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws,
                        run_suite=lambda **k: _Result([_Outcome("loop-termination", True)]))
    finding = Finding(kind="stagnant_loop", path=".agentorg/skills/", subject="reviser",
                      detail="x", scenario="loop-termination")
    proposal = improver.draft(finding)
    validation = improver.validate(proposal)
    assert validation.improved == []
    assert validation.regressions == []
    assert not validation.ok
    assert "baseline cannot be read" in validation.detail


def test_not_worse_is_not_an_improvement(tmp_path):
    """A change that leaves the suite unchanged has demonstrated nothing, so it is not promoted. This
    is the rule that stops a self-improving system drifting on 'no harm done'.

    The stub reports the *whole* suite as unchanged (every baseline scenario present and passing), so
    lost-coverage does not fire — the real gate treating a missing scenario as a regression is tested
    separately below.
    """
    import json as _json
    from engine.evals.runner import BASELINE_PATH

    baseline = _json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    unchanged = _Result([_Outcome(name, True) for name in (baseline.get("results") or {})])

    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws, run_suite=lambda **k: unchanged)
    finding = Finding(kind="stagnant_loop", path=".agentorg/skills/", subject="not-a-scenario",
                      detail="y", scenario="a-scenario-that-did-not-flip")
    proposal = improver.draft(finding)
    validation = improver.validate(proposal)
    assert validation.ran and not validation.regressions
    assert validation.improved == []
    assert not validation.ok
    assert improver.promote(proposal) is None
    assert proposal.state == "rejected"
    assert "nothing to promote" in validation.detail


def test_lost_coverage_counts_as_a_regression(tmp_path):
    """A scenario that stopped running is a regression, not an absence of news — the baseline gate's
    own rule, inherited here rather than re-decided."""
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws, run_suite=lambda **k: _Result([_Outcome("one", True)]))
    finding = Finding(kind="stagnant_loop", path=".agentorg/skills/", subject="x", detail="y")
    proposal = improver.draft(finding)
    validation = improver.validate(proposal)
    assert any("no longer runs" in r for r in validation.regressions)
    assert not validation.ok


def test_a_regression_blocks_promotion(tmp_path):
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws, run_suite=lambda **k: _Result([_Outcome("loop-termination",
                                                                             True)]))
    # No baseline entry for the scenario, so the real gate reports no regression; force one instead by
    # stubbing the comparison, which is the only way to test the blocking rule deterministically.
    improver._regressions = staticmethod(lambda result: ["gate-integrity: was passing, now failing"])
    finding = Finding(kind="stagnant_loop", path=".agentorg/skills/", subject="x", detail="y",
                      scenario="loop-termination")
    proposal = improver.draft(finding)
    validation = improver.validate(proposal)
    assert validation.regressions
    assert not validation.ok
    assert improver.promote(proposal) is None


def test_a_swift_change_is_labelled_unvalidatable_rather_than_implied_safe(tmp_path):
    """The Python suite cannot judge Swift. Saying so is the honest outcome; implying safety because
    something could not be checked is how an unvalidated change gets promoted."""
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws)
    finding = Finding(kind="repeated_rejection", path="macos/Sources/AgentOrg/ConsoleView.swift",
                      subject="layout", detail="x")
    proposal = improver.draft(finding)
    validation = improver.validate(proposal)
    assert not validation.ran
    assert validation.unvalidatable
    assert not validation.ok
    # It is shown, because a draft Swift fix is still worth reading — but labelled.
    assert improver.promote(proposal) is not None
    assert proposal.state == "promoted"


def test_a_suite_that_cannot_run_is_not_a_passed_suite(tmp_path):
    def _explode(**kwargs):
        raise RuntimeError("the suite is broken")

    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws, run_suite=_explode)
    finding = Finding(kind="stagnant_loop", path=".agentorg/skills/", subject="x", detail="y")
    proposal = improver.draft(finding)
    validation = improver.validate(proposal)
    assert not validation.ran
    assert not validation.ok
    assert "broken" in validation.detail


# ── promote: the Owner decides ───────────────────────────────────────────────


def test_a_validated_proposal_is_written_where_a_person_can_read_it(tmp_path, monkeypatch):
    """A proposal that earned promotion is written where a person can read it — so the run has to
    demonstrate the flip (`loop-termination` failing at baseline, passing now); a merely unchanged tree
    is not promoted at all."""
    _baseline_file(tmp_path, monkeypatch, {"loop-termination": {"passed": False}})
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws,
                        run_suite=lambda **k: _Result([_Outcome("loop-termination", True)]))
    finding = Finding(kind="stagnant_loop", path=".agentorg/skills/", subject="reviser",
                      detail="node 'reviser' used 4 iterations and still ended needs_review",
                      evidence={"node": "reviser", "iterations": 4},
                      scenario="loop-termination", severity="major")
    proposal = improver.draft(finding)
    improver.validate(proposal)
    path = improver.promote(proposal)
    assert path is not None and path.is_file()
    text = path.read_text(encoding="utf-8")
    # The four things a person needs to decide: what, why, what it improves, and what it changes.
    assert "stagnant_loop" in text
    assert "loop-termination" in text
    assert ".agentorg/skills/" in text
    assert "Nothing has been applied" in text


def test_the_proposal_says_plainly_that_nothing_was_applied(tmp_path):
    """The gate only works if the reader knows the tree is untouched and the decision is theirs."""
    proposal = Proposal(proposal_id="p1", finding=Finding(kind="failing_agent", detail="x"))
    assert "Nothing has been applied" in proposal.render()
    assert "never edits the tree" in proposal.render()


def test_nothing_in_the_module_writes_to_a_source_file(tmp_path):
    """The central claim, as a test: the improver's only write targets are its own proposals.

    Checked by running a full cycle and asserting that every file it created lives under
    `.agent_state/proposals/` — so a future change that starts editing the tree fails here.
    """
    ws = _workspace(tmp_path)
    _spans(ws, [{"node": "fixer", "verdict": "changes_requested"}] * 3)
    before = {p for p in ws.path.rglob("*") if p.is_file()}
    improver = Improver(workspace=ws)
    improver.run_once()
    after = {p for p in ws.path.rglob("*") if p.is_file()}
    created = after - before
    for path in created:
        assert improver.proposals_dir() in path.parents, \
            f"a cycle created {path}, which is outside the proposals directory"
    # And nothing outside the workspace was touched: the repo's own files are untouched by definition
    # because the workspace is a temp directory, which is the point of the assertion above.


def test_a_rejection_is_remembered_so_it_is_not_re_litigated(tmp_path):
    """Recorded by (kind, subject) — the proposal id changes every cycle, so keying on it would
    remember nothing and the loop would re-propose the same thing forever."""
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws, run_suite=lambda **k: _Result([_Outcome("other", True)]))
    finding = Finding(kind="stagnant_loop", path=".agentorg/skills/", subject="reviser",
                      detail="x", scenario="loop-termination")
    proposal = improver.draft(finding)
    improver.validate(proposal)
    assert improver.promote(proposal) is None
    assert improver.already_rejected(finding)


def test_a_rejected_finding_is_skipped_on_the_next_cycle(tmp_path):
    ws = _workspace(tmp_path)
    _spans(ws, [{"node": "reviser", "verdict": "needs_review", "iterations": 4}])
    improver = Improver(workspace=ws, run_suite=lambda **k: _Result([_Outcome("other", True)]))
    first = improver.run_once()
    assert first, "the first cycle should consider something"
    second = improver.run_once()
    assert second == [], "a rejected finding must not be re-proposed every cycle"


def test_the_cycle_reports_refusals_as_well_as_promotions(tmp_path):
    """A cycle that reported only its successes would hide the boundary working."""
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws)
    improver.draft_fn = lambda f: ("", "", ["engine/evals/runner.py"])
    considered: list[Proposal] = []
    for finding in improver.detect() or []:
        p = improver.draft(finding)
        if p is not None:
            considered.append(p)
    # With no spans there are no findings; drive one directly to prove a refusal is surfaced.
    p = improver.draft(Finding(kind="stagnant_loop", path="engine/evals/runner.py",
                               subject="gate", detail="x"))
    assert p.state == "refused"
    assert p.refusal
