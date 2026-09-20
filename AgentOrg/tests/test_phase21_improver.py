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

import difflib
import hashlib
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
    """A stub outcome with the fields the real `Outcome` carries.

    `error` in particular: the baseline gate formats a failing outcome's error, so a stub without one
    would make the comparison raise and the failure would be about the stub rather than the rule.
    """

    def __init__(self, name, passed, error=""):
        self.name, self.passed, self.error = name, passed, error
        self.check, self.detail = "stub", ""


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


# ── validate: the patch is applied to a scratch copy, and only there ─────────
#
# The loop could not promote anything real until these existed: the suite ran against the tree as it
# stood, so no patch could be said to have *caused* anything. These tests pin the two halves of the
# fix — the patch really is applied somewhere, and that somewhere is never the working tree.


def _mini_repo(tmp_path, *, body="value = 1\n"):
    """A throwaway tree for the loop to copy: one file a patch can aim at, and nothing else."""
    repo = tmp_path / "mini"
    (repo / "engine").mkdir(parents=True, exist_ok=True)
    (repo / "engine" / "prompts.py").write_text(body, encoding="utf-8")
    return repo


def _patch(old, new):
    """A unified diff of the shape the improver drafts: one hunk, one file, `b/` prefixed."""
    return ("--- a/engine/prompts.py\n+++ b/engine/prompts.py\n"
            "@@ -1,1 +1,1 @@\n"
            f"-{old}\n"
            f"+{new}\n")


def _fingerprint(root):
    """Every file under `root` by sha256, so a write anywhere shows up as a difference."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _finding(scenario="loop-termination"):
    return Finding(kind="stagnant_loop", path="engine/prompts.py", subject="reviser",
                   detail="the loop turns without converging", scenario=scenario)


def test_a_patch_that_flips_a_named_scenario_is_applied_in_a_scratch_copy(tmp_path, monkeypatch):
    """The load-bearing behaviour: the patch is applied to a real copy, the suite runs against the
    patched code *there*, and only then is a flip claimed.

    The stub suite reads the copy's own file, so this cannot pass on the machinery merely existing —
    a flip is reported only if the patch actually landed in the copy.
    """
    _baseline_file(tmp_path, monkeypatch, {"loop-termination": {"passed": False}})
    repo = _mini_repo(tmp_path, body="value = 1\n")
    seen: list[pathlib.Path] = []

    def suite(root):
        seen.append(pathlib.Path(root))
        patched = (pathlib.Path(root) / "engine" / "prompts.py").read_text(
            encoding="utf-8") == "value = 2\n"
        return _Result([_Outcome("loop-termination", patched)])

    improver = Improver(workspace=_workspace(tmp_path), repo_root=repo, scratch_suite=suite)
    improver.draft_fn = lambda f: ("restore the loop bound", _patch("value = 1", "value = 2"),
                                   ["engine/prompts.py"])
    proposal = improver.draft(_finding())
    validation = improver.validate(proposal)

    assert validation.ran and validation.patched_in_scratch
    assert validation.improved == ["loop-termination"]
    assert validation.regressions == []
    assert validation.ok
    assert len(seen) == 2, "the suite runs once without the patch and once with it"
    assert seen[0] == seen[1], "both runs are the same scratch copy"
    assert not seen[0].exists(), "the copy is removed when validation finishes"
    assert (repo / "engine" / "prompts.py").read_text(encoding="utf-8") == "value = 1\n", (
        "only the copy was written to")

    path = improver.promote(proposal)
    assert path is not None
    assert "scratch copy" in path.read_text(encoding="utf-8"), (
        "a reader has to be told the suite ran against a patched copy")


def test_a_proposal_with_no_patch_builds_no_scratch_copy(tmp_path, monkeypatch):
    """Nothing to apply means nothing can have been caused, so no copy is built and the reason is
    named rather than left as a bare "no improvement"."""
    # A stub baseline with only the named scenario in it: the real one has 17 entries, and a run that
    # covers one of them would be read as 16 lost-coverage regressions rather than as this check.
    _baseline_file(tmp_path, monkeypatch, {"loop-termination": {"passed": True}})

    def _never(root):
        raise AssertionError("a scratch copy was built for a proposal carrying no patch")

    improver = Improver(workspace=_workspace(tmp_path), scratch_suite=_never,
                        run_suite=lambda **k: _Result([_Outcome("loop-termination", True)]))
    proposal = improver.draft(_finding())          # no draft_fn: described, not patched
    assert proposal.patch == ""

    validation = improver.validate(proposal)
    assert validation.ran and not validation.patched_in_scratch
    assert validation.improved == []
    assert not validation.ok
    assert "no patch to apply" in validation.detail
    assert improver.promote(proposal) is None


def test_a_patch_aimed_at_a_safety_surface_is_refused_at_validation_too(tmp_path):
    """The patch is what lands, so the patch is what the boundary is checked against. A `touches` list
    that disagreed with the diff would be exactly the disagreement a check must not trust — and the
    refusal must come before a copy is made, not after."""
    def _never(root):
        raise AssertionError("a scratch copy was built for a patch aimed at the judging machinery")

    improver = Improver(workspace=_workspace(tmp_path), repo_root=_mini_repo(tmp_path),
                        scratch_suite=_never)
    proposal = improver.draft(_finding())
    assert proposal.state == "drafted", "the draft check passes a path that looks ordinary"
    assert proposal.touches == ["engine/prompts.py"]
    proposal.patch = ("--- a/engine/evals/runner.py\n+++ b/engine/evals/runner.py\n"
                      "@@ -1,1 +1,1 @@\n-CHECKS = {}\n+CHECKS = None\n")

    validation = improver.validate(proposal)
    assert not validation.ran and not validation.patched_in_scratch
    assert "engine/evals/runner.py" in validation.detail
    assert "not configurable" in validation.detail
    assert not validation.ok
    assert improver.promote(proposal) is None
    assert proposal.state == "rejected"


def test_a_patch_that_does_not_apply_is_refused_intact(tmp_path):
    """A drifted patch is refused whole rather than half-applied, and the copy goes either way."""
    repo = _mini_repo(tmp_path, body="value = 7\n")
    seen: list[pathlib.Path] = []

    def suite(root):
        seen.append(pathlib.Path(root))
        return _Result([_Outcome("loop-termination", False)])

    improver = Improver(workspace=_workspace(tmp_path), repo_root=repo, scratch_suite=suite)
    improver.draft_fn = lambda f: ("restore the loop bound", _patch("value = 1", "value = 2"),
                                   ["engine/prompts.py"])
    validation = improver.validate(improver.draft(_finding()))

    assert not validation.patched_in_scratch and not validation.ok
    assert "does not apply" in validation.detail
    assert (repo / "engine" / "prompts.py").read_text(encoding="utf-8") == "value = 7\n"
    assert seen and not seen[0].exists()


def test_a_patch_that_breaks_the_engine_is_not_read_as_a_run(tmp_path):
    """A patch that stops the suite from running at all is reported as *unmeasurable*, not as a run
    with no failures — the suite's own rule, applied to the scratch copy. Read as a run it would look
    like lost coverage, and the cause (the patch broke the engine) would go unnamed."""
    target = ROOT / "engine" / "planner.py"
    original = target.read_text(encoding="utf-8")
    changed = original + "def broken(:\n"
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True),
                                         changed.splitlines(keepends=True),
                                         fromfile="a/engine/planner.py",
                                         tofile="b/engine/planner.py"))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    improver = Improver(workspace=_workspace(tmp_path))     # the real repo and the real runner
    proposal = improver.draft(_finding())
    proposal.patch = patch
    validation = improver.validate(proposal)

    assert not validation.ran and not validation.patched_in_scratch
    assert not validation.ok
    assert "not validated" in validation.detail
    assert "no scenario" in validation.detail, "the run says what it could not do"
    assert "SyntaxError" in validation.detail, "the reason the suite never ran is named"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest


def test_a_flip_the_patch_did_not_cause_is_not_an_improvement(tmp_path, monkeypatch):
    """A baseline can be stale. When the named scenario already passes without the patch, the patch
    has changed nothing about it and must not be credited — the same misattribution as counting an
    already-passing scenario."""
    _baseline_file(tmp_path, monkeypatch, {"loop-termination": {"passed": False}})
    improver = Improver(workspace=_workspace(tmp_path), repo_root=_mini_repo(tmp_path),
                        scratch_suite=lambda root: _Result([_Outcome("loop-termination", True)]))
    improver.draft_fn = lambda f: ("tidy a comment", _patch("value = 1", "value = 2"),
                                   ["engine/prompts.py"])
    validation = improver.validate(improver.draft(_finding()))

    assert validation.patched_in_scratch
    assert validation.improved == []
    assert not validation.ok
    assert "already passes against the unpatched tree" in validation.detail


def test_a_patch_that_regresses_the_suite_is_not_promoted(tmp_path, monkeypatch):
    """The regression gate is applied to the run *in the copy*: a patch that trades one scenario for
    another is refused, whatever it claims to improve, because the gate compares against the frozen
    baseline rather than against the proposal's own story."""
    _baseline_file(tmp_path, monkeypatch, {"loop-termination": {"passed": True}})
    calls = {"n": 0}

    def suite(root):
        calls["n"] += 1
        return _Result([_Outcome("loop-termination", calls["n"] == 1)])

    improver = Improver(workspace=_workspace(tmp_path), repo_root=_mini_repo(tmp_path),
                        scratch_suite=suite)
    improver.draft_fn = lambda f: ("break something else", _patch("value = 1", "value = 2"),
                                   ["engine/prompts.py"])
    proposal = improver.draft(_finding())
    validation = improver.validate(proposal)

    assert validation.patched_in_scratch
    assert any("loop-termination" in r for r in validation.regressions)
    assert validation.improved == []
    assert not validation.ok
    assert "1 regression(s)" in validation.detail
    assert improver.promote(proposal) is None
    assert proposal.state == "rejected"


def test_a_comparison_that_cannot_be_made_is_not_a_pass(tmp_path):
    """The regression check is part of the gate, so a comparison that raised must not read as "nothing
    regressed" — swallowing it is the one direction that lets a change through unexamined."""
    class _Thin:
        name, passed = "loop-termination", False       # enough to break the comparison's formatting

    improver = Improver(workspace=_workspace(tmp_path),
                        run_suite=lambda **k: _Result([_Thin()]))
    finding = Finding(kind="stagnant_loop", path=".agentorg/skills/", subject="x", detail="y",
                      scenario="loop-termination")
    validation = improver.validate(improver.draft(finding))
    assert validation.regressions, "a comparison that could not run must be reported"
    assert "could not be compared" in validation.regressions[0]
    assert not validation.ok


def test_the_scratch_copy_is_removed_even_when_the_suite_raises(tmp_path, monkeypatch):
    """Every path out deletes the temporary copy, including the one where the suite itself breaks."""
    _baseline_file(tmp_path, monkeypatch, {"loop-termination": {"passed": False}})
    seen: list[pathlib.Path] = []

    def suite(root):
        seen.append(pathlib.Path(root))
        raise RuntimeError("the suite is broken")

    improver = Improver(workspace=_workspace(tmp_path), repo_root=_mini_repo(tmp_path),
                        scratch_suite=suite)
    improver.draft_fn = lambda f: ("restore the loop bound", _patch("value = 1", "value = 2"),
                                   ["engine/prompts.py"])
    validation = improver.validate(improver.draft(_finding()))

    assert seen, "the copy was made and the suite was run in it"
    assert not validation.ran and not validation.patched_in_scratch
    assert not validation.ok
    assert "broken" in validation.detail
    assert not seen[0].exists(), "the copy survives a suite that failed"
    assert not seen[0].parent.exists(), "the temp directory goes too, not just the tree"


def test_a_validation_run_leaves_the_tree_it_copied_byte_identical(tmp_path, monkeypatch):
    """The claim that makes validating safe, asserted rather than assumed: the tree the patch
    describes is byte-for-byte what it was, whatever the run decided."""
    _baseline_file(tmp_path, monkeypatch, {"loop-termination": {"passed": False}})
    repo = _mini_repo(tmp_path, body="value = 1\n")
    (repo / "engine" / "other.py").write_text("other = 1\n", encoding="utf-8")
    before = _fingerprint(repo)

    improver = Improver(workspace=_workspace(tmp_path), repo_root=repo,
                        scratch_suite=lambda root: _Result([_Outcome("loop-termination", True)]))
    improver.draft_fn = lambda f: ("restore the loop bound", _patch("value = 1", "value = 2"),
                                   ["engine/prompts.py"])
    validation = improver.validate(improver.draft(_finding()))

    assert validation.patched_in_scratch, "the run has to have written somewhere, or it proves nothing"
    assert _fingerprint(repo) == before, "a validation wrote to the tree it was validating"


def test_a_real_validation_runs_against_a_copy_of_the_repo(tmp_path):
    """The real tree, the real suite, the real runner — and the patch applied somewhere that is not
    the repo. The file it names is byte-identical afterwards and the suite still passes in the copy,
    which is what "validated in a scratch copy" has to mean to be worth anything.

    The patch is built from the file's own bytes so it applies whatever else is in flight; a
    hand-written hunk would drift and the test would fail for the wrong reason.
    """
    target = ROOT / "engine" / "prompts.py"
    original = target.read_text(encoding="utf-8")
    assert original.endswith("\n"), "the fixture assumes a newline-terminated file"
    changed = original + "# a proposal-shaped change: a comment no scenario can notice\n"
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True),
                                         changed.splitlines(keepends=True),
                                         fromfile="a/engine/prompts.py",
                                         tofile="b/engine/prompts.py"))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    improver = Improver(workspace=_workspace(tmp_path))
    proposal = improver.draft(_finding())
    proposal.patch = patch
    validation = improver.validate(proposal)

    assert validation.ran and validation.patched_in_scratch, validation.detail
    assert validation.regressions == [], validation.detail
    assert validation.improved == [], "the shipped baseline already records every scenario passing"
    assert not validation.ok
    assert "nothing flipped" in validation.detail
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest, (
        "the working tree was written to by a validation")


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
