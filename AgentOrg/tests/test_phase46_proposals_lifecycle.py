#!/usr/bin/env python3
"""Phase 46 — the proposal lifecycle: the self-checks can now move forward on a fix.

WHY THIS EXISTS
---------------
`engine/improver.py` deliberately never applies anything, and that is a property worth keeping: an
improver that can rewrite the gate that judges it can make anything pass. But the loop's whole output
was a patch in a directory and a sentence telling the person to apply it themselves, and the complaint
was exactly that — *"self-checks don't have to move forward on fixes and improve."* A detection nobody
can act on is a report, not a loop.

So the fix is a **lifecycle a person drives**, not permission for the loop to edit the tree:

    drafted ──promote──> promoted ──accept──> accepted ──apply──> applied ──undo──> accepted
       │                     │                  │
       └─────reject──────────┴──────────────────┴──────> rejected (with the reason, remembered)

`promoted` is not this module's state: it is what the loop's own writer leaves a validated proposal in,
which makes it the commonest state in the directory, and it is *live* — a person can accept it, reject
it, or apply it. `accept` and `reject` are bookkeeping. `apply` is the one transition that touches the
tree, and it earns the right to: a demonstrated improvement is required, the project's own suite runs
before and after, and a regression **reverts** from a saved copy rather than reporting and leaving the
damage. A proposal a person already settled — rejected, or applied and not yet undone — is refused
before those rules are reached, so the refusal names the state rather than something that is not the
problem.

What these tests pin, in order of how much they matter:

1. **The loop still refuses to aim at its own judge** — and `engine/proposals.py`, the applier itself,
   is now on that list. A boundary you can edit is not a boundary.
2. **A rejection is remembered** in the improver's own `rejected.jsonl` format, or the same finding
   comes back every cycle and nothing ever moves forward.
3. **Apply refuses without evidence** — no patch, no demonstrated improvement, a change the suite
   cannot judge, or a suite that cannot run at all. None of those is "safe because it could not be
   checked", and an unreadable run is neither a pass nor a verdict of zero failures.
4. **Apply reverts on regression**, from the saved bytes, so the tree is exactly as it was.
5. **None of the bookkeeping states writes to a source file**, asserted by fingerprinting the whole
   repo before and after.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.improver import (
    PROPOSALS_DIRNAME,
    SAFETY_SURFACES,
    Finding,
    Improver,
    Proposal,
    Validation,
    is_safety_surface,
)
from engine.proposals import (
    LIVE_STATES,
    STATES,
    ProposalLifecycleError,
    ProposalStore,
    TestRun,
    annotate,
)
from engine.state import Workspace


# ── fixtures ─────────────────────────────────────────────────────────────────


def _workspace(tmp_path, slug="lifecycle"):
    ws = Workspace.for_project(slug, root=tmp_path / "projects")
    ws.ensure()
    return ws


def _repo(tmp_path, *, body="value = 1\n", name="module.py", tests_pass=True):
    """A throwaway git repo with one source file and a `run_tests.py`, the applier's two contracts.

    A real repo rather than a stub because `apply` shells out to `git apply`: a test that mocked the
    patch tool would prove nothing about the thing that actually edits files.
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(body, encoding="utf-8")
    summary = "1 passed, 0 failed" if tests_pass else "1 passed, 1 failed"
    (repo / "run_tests.py").write_text(
        "import sys\n"
        f"print({summary!r})\n"
        f"sys.exit({'0' if tests_pass else '1'})\n",
        encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


def _patch(name, old, new):
    """A unified diff of the shape the improver drafts: one hunk, one file, `b/` prefixed."""
    return (f"--- a/{name}\n+++ b/{name}\n"
            "@@ -1,1 +1,1 @@\n"
            f"-{old}\n"
            f"+{new}\n")


def _seed(store, tmp_path, *, pid="prop_0001", patch=None, state="promoted",
          improved=("loop-termination",), unvalidatable="", touches=None, kind="stagnant_loop"):
    """A proposal on disk shaped exactly as `Improver._write` leaves one.

    Written through a real `Proposal` and the improver's own writer, so the file layout the lifecycle
    reads is the layout the loop produces — not a hand-copied directory of JSON that could drift.

    Returns `(proposal, path)` where **`path` is the JSON record**, not the `.md` beside it.
    `Improver._write` returns the markdown on purpose — that is the file a person reads and what
    `promote` hands back (`tests/test_phase21_improver.py` pins its text) — but a caller seeding a
    proposal wants the machine-readable twin, because every assertion after this point is about the
    *record*: its state, its validation, and the state a person edits before applying. Returning the
    prose made `json.loads` fail on a markdown heading, which is how the ambiguity surfaced.
    """
    proposal = Proposal(
        proposal_id=pid,
        finding=Finding(kind=kind, path="engine/x.py", subject="reviser",
                        detail="never converged", evidence={"iterations": 4},
                        scenario="loop-termination"),
        rationale="the loop turns without converging",
        patch=patch if patch is not None else _patch("module.py", "value = 1", "value = 2"),
        touches=list(touches if touches is not None else ["module.py"]),
        validation=Validation(ran=not unvalidatable, improved=list(improved),
                              unvalidatable=unvalidatable),
        state=state,
    )
    improver = Improver(workspace=store.workspace)
    path = improver._write(proposal).with_suffix(".json")   # noqa: SLF001 - the loop's writer
    return proposal, path


# ── the boundary, unchanged and now including the applier ────────────────────


def test_the_applier_is_itself_a_safety_surface():
    """A proposal that could aim at the code deciding whether a proposal may land is not gated.

    This is the one line that keeps the lifecycle from undoing the improver's whole argument: the
    applier is exactly the machinery that judges the loop, so it is refused like the eval gate is.
    """
    assert is_safety_surface("engine/proposals.py")
    assert "engine/proposals.py" in SAFETY_SURFACES


def test_a_draft_aimed_at_the_applier_is_refused(tmp_path):
    ws = _workspace(tmp_path)
    improver = Improver(workspace=ws)
    improver.draft_fn = lambda f: ("loosen the guard", _patch("engine/proposals.py", "a", "b"),
                                   ["engine/proposals.py"])
    proposal = improver.draft(Finding(kind="stagnant_loop", path="engine/proposals.py",
                                      subject="applier", detail="x"))
    assert proposal.state == "refused"
    assert "engine/proposals.py" in proposal.refusal


def test_apply_re_checks_the_boundary_rather_than_trusting_draft_time(tmp_path):
    """The file list on disk is data, and data can be edited between drafting and applying."""
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path))
    _, path = _seed(store, tmp_path, patch=_patch("engine/config.py", "budget = 1", "budget = 2"),
                    touches=["engine/config.py"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["state"] = "accepted"
    path.write_text(json.dumps(payload), encoding="utf-8")

    outcome = store.apply("prop_0001")
    assert outcome.applied is False
    assert "engine/config.py" in outcome.refused
    assert "not configurable" in outcome.refused


def test_the_seed_hands_back_the_record_with_the_prose_beside_it(tmp_path):
    """`_seed` is the writer every other test reads through, so its return value is a contract.

    It hands back the proposal's **record** — the JSON twin of the pair `Improver._write` writes —
    because that is what the lifecycle reads and what the boundary test above rewrites to move a
    proposal into a state. `_write` itself keeps returning the `.md`: that is the file a person opens
    and what `promote` hands back (`tests/test_phase21_improver.py` asserts the markdown's own text),
    so changing it would break a documented caller to fix a test helper. That the *pair* exists is the
    second half of this assertion — a record with no prose, or prose with no record, is a proposal that
    says two different things about itself.
    """
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path))
    _, path = _seed(store, tmp_path)

    assert path.suffix == ".json", "the record, so `json.loads(path.read_text())` is what it says"
    assert path.parent == store.workspace.state_dir / PROPOSALS_DIRNAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["proposal_id"] == "prop_0001" and payload["state"] == "promoted"
    # The prose the state's words live in, beside it and readable — `accept` rewrites both.
    assert "Nothing has been applied" in path.with_suffix(".md").read_text(encoding="utf-8")


# ── accept: a decision, not an edit ──────────────────────────────────────────


def test_the_states_come_from_the_module_that_owns_them():
    """No hand-copied list: the lifecycle's states are asserted against the object that defines them,
    and every one of the improver's own states is still reachable through this module."""
    assert set(STATES) >= {"drafted", "accepted", "rejected", "applied"}
    # The improver's own vocabulary must be a subset, so a state it starts writing cannot be one the
    # lifecycle has never heard of.
    from engine.improver import Proposal as _P

    drafted = _P(proposal_id="p", finding=Finding(kind="failing_agent", detail="x"))
    assert drafted.state in STATES
    assert set(LIVE_STATES) <= set(STATES)
    assert "applied" not in LIVE_STATES, "an applied proposal is not still waiting on a person"


def test_accepting_records_the_decision_and_writes_nothing_to_the_tree(tmp_path):
    repo = _repo(tmp_path)
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=repo)
    _, path = _seed(store, tmp_path)
    before = (repo / "module.py").read_bytes()

    proposal = store.accept("prop_0001")
    assert proposal.state == "accepted"
    assert (repo / "module.py").read_bytes() == before, "accept must not touch a source file"

    # The state survives on disk, in both the JSON the panel reads and the markdown a person reads.
    reloaded = ProposalStore(workspace=store.workspace, repo_root=repo).load("prop_0001")
    assert reloaded.state == "accepted"
    assert "Accepted — nothing has been applied" in path.with_suffix(".md").read_text(
        encoding="utf-8")
    assert [d["decision"] for d in store.decisions()] == ["accepted"]


def test_accepting_a_refused_proposal_is_refused_with_the_reason(tmp_path):
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path))
    _seed(store, tmp_path, state="refused")
    with pytest.raises(ProposalLifecycleError, match="refused at draft time"):
        store.accept("prop_0001")


def test_an_unknown_id_names_the_ids_that_exist(tmp_path):
    """"no such proposal" is not a diagnosis; the ids are."""
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path))
    _seed(store, tmp_path, pid="prop_0001")
    with pytest.raises(ProposalLifecycleError, match="prop_0001"):
        store.load("prop_9999")


# ── reject: recorded, so the finding is not re-litigated ─────────────────────


def test_a_rejection_keeps_its_reason_and_stops_the_next_cycle(tmp_path):
    """The rejection has to land in the improver's own `rejected.jsonl`, matched by *(kind, subject)*.

    If it did not, `Improver.already_rejected` would not see it, the same finding would be drafted
    every cycle, and the loop would look busy while never moving — which is the complaint.
    """
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path))
    proposal, _ = _seed(store, tmp_path)
    store.reject("prop_0001", reason="not worth the churn this week")

    reloaded = store.load("prop_0001")
    assert reloaded.state == "rejected"
    assert reloaded.refusal == "not worth the churn this week"

    recorded = (store.directory / "rejected.jsonl")
    assert recorded.is_file()
    entries = [json.loads(line) for line in recorded.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    assert entries[-1]["reason"] == "not worth the churn this week"
    assert entries[-1]["kind"] == proposal.finding.kind
    assert entries[-1]["subject"] == proposal.finding.subject

    # And the improver's own memory reads it, so the decision actually changes the next cycle.
    improver = Improver(workspace=store.workspace)
    assert improver.already_rejected(proposal.finding)


def test_rejecting_uses_the_real_finding_not_a_copy(tmp_path):
    """The reason is stored against the finding the proposal carries, derived from the object rather
    than restated, so a proposal whose finding changes cannot be remembered under the wrong key."""
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path))
    proposal, _ = _seed(store, tmp_path, pid="prop_0042", kind="budget_burn")
    store.reject("prop_0042", reason="r")
    entry = [json.loads(line) for line
             in (store.directory / "rejected.jsonl").read_text(encoding="utf-8").splitlines()][-1]
    assert entry["kind"] == proposal.finding.kind == "budget_burn"
    assert entry["path"] == proposal.finding.path


# ── apply: the one transition that earns the right to edit ───────────────────


def test_apply_needs_a_patch_and_says_so(tmp_path):
    """A described proposal is not a patch. Applying it would mean reporting success for an edit that
    never happened, which is worse than refusing."""
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path))
    _seed(store, tmp_path, patch="", touches=["engine/prompts.py"], state="accepted")
    outcome = store.apply("prop_0001")
    assert outcome.applied is False
    assert outcome.touched_the_tree is False
    assert "no patch" in outcome.refused


def test_apply_refuses_without_a_demonstrated_improvement(tmp_path):
    """The Owner agreeing and the suite improving are different claims, and a patch needs both."""
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path))
    _seed(store, tmp_path, improved=(), state="accepted")
    outcome = store.apply("prop_0001")
    assert outcome.applied is False
    assert "no improvement" in outcome.refused
    assert (store._repo() / "module.py").read_text(encoding="utf-8") == "value = 1\n"  # noqa: SLF001


def test_a_proposal_the_loop_just_promoted_is_applied_without_a_separate_accept(tmp_path):
    """`promoted` is what `Improver._write` leaves a validated proposal in — the commonest state in
    the directory — so a gate that refused it refused the one case the applier exists for, and offered
    the button on `drafted`, the less proven of the two. The command is the person's decision."""
    repo = _repo(tmp_path)
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=repo,
                          run_tests=lambda: TestRun(ran=True, passed=345, failed=0))
    _seed(store, tmp_path)                              # state="promoted", as the loop writes it

    outcome = store.apply("prop_0001")
    assert outcome.applied is True
    assert (repo / "module.py").read_text(encoding="utf-8") == "value = 2\n"


def test_a_settled_proposal_cannot_be_applied_whatever_the_evidence_says(tmp_path):
    """The other half of the same gate, and the reason it is not simply "anything goes": a proposal a
    person rejected, and one that has already landed, are both refused *before* the evidence rules, so
    the refusal names what to do about the state rather than something that is not the problem."""
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path),
                          run_tests=lambda: TestRun(ran=True, passed=1, failed=0))
    _seed(store, tmp_path, pid="prop_0001", state="rejected")
    _seed(store, tmp_path, pid="prop_0002", state="applied")

    rejected = store.apply("prop_0001")
    assert rejected.applied is False and "rejected" in rejected.refused
    assert store.can_apply(store.load("prop_0001")) is False
    # Undo is the documented next move for an applied one, so the refusal has to say so.
    landed = store.apply("prop_0002")
    assert landed.applied is False and "undo" in landed.refused


def test_apply_refuses_a_change_the_suite_cannot_judge(tmp_path):
    """`unvalidatable` must never read as "the check passed" — it means the check did not happen."""
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path))
    _seed(store, tmp_path, unvalidatable="this touches Swift, which the Python suite does not run")
    outcome = store.apply("prop_0001")
    assert outcome.applied is False
    assert "unverified" in outcome.refused


def test_apply_writes_the_patch_only_after_checking_it_lands(tmp_path):
    """A patch that has drifted is refused intact, not half-applied."""
    repo = _repo(tmp_path)
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=repo)
    # The file no longer contains the line the patch removes, so `git apply --check` fails.
    (repo / "module.py").write_text("value = 99\n", encoding="utf-8")
    _seed(store, tmp_path, state="accepted")

    outcome = store.apply("prop_0001")
    assert outcome.applied is False
    assert outcome.touched_the_tree is False
    assert "does not apply to the current tree" in outcome.refused
    assert (repo / "module.py").read_text(encoding="utf-8") == "value = 99\n"


def test_apply_lands_the_change_and_records_both_suite_runs(tmp_path):
    repo = _repo(tmp_path)
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=repo,
                          run_tests=lambda: TestRun(ran=True, passed=345, failed=0))
    _seed(store, tmp_path, state="accepted")

    outcome = store.apply("prop_0001")
    assert outcome.applied is True
    assert outcome.files == ["module.py"]
    assert (repo / "module.py").read_text(encoding="utf-8") == "value = 2\n"
    assert outcome.tests_before["passed"] == 345 and outcome.tests_after["passed"] == 345
    assert store.load("prop_0001").state == "applied"
    assert "has been applied to the tree" in (
        store.directory / "prop_0001-stagnant_loop.md").read_text(encoding="utf-8")


def test_a_regression_reverts_the_change_from_the_saved_copy(tmp_path):
    """The centrepiece: an edit that made the tree worse must not stay in it.

    The revert is from the bytes saved *before* the patch, not from a reverse diff, because a reverse
    diff can itself conflict once anything else has touched the file.
    """
    repo = _repo(tmp_path)
    calls = {"n": 0}

    def _suite():
        calls["n"] += 1
        # Good before, failing after: the classic regression this guard exists for.
        return TestRun(ran=True, passed=345, failed=0 if calls["n"] == 1 else 2)

    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=repo, run_tests=_suite)
    _seed(store, tmp_path, state="accepted")

    outcome = store.apply("prop_0001")
    assert outcome.applied is False
    assert outcome.reverted is True
    assert outcome.files == ["module.py"], "it did write, and it says so"
    assert (repo / "module.py").read_text(encoding="utf-8") == "value = 1\n"
    assert outcome.tests_before["failed"] == 0 and outcome.tests_after["failed"] == 2
    # Not "applied" on disk: a state that said applied would be a lie about the tree.
    assert store.load("prop_0001").state != "applied"


def test_a_suite_that_cannot_run_is_not_a_passed_suite(tmp_path):
    """No summary line means the run could not be read, which is not the same as zero failures."""
    repo = _repo(tmp_path)
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=repo,
                          run_tests=lambda: TestRun(ran=False, detail="could not start"))
    _seed(store, tmp_path, state="accepted")
    outcome = store.apply("prop_0001")
    assert outcome.applied is False
    assert "cannot be verified" in outcome.refused or "could not run" in outcome.refused
    assert (repo / "module.py").read_text(encoding="utf-8") == "value = 1\n", (
        "an unreadable suite refuses before anything is written")


def test_a_suite_that_cannot_run_after_the_change_is_reverted_not_counted_as_a_pass(tmp_path):
    """The mirror of the baseline check, and the one direction nobody looks.

    A good run before and an unreadable one after is exactly the state in which "no failures" would be
    believed: `after.ran` is False, `after.failed` is 0, and a check written as `if after.failed` would
    leave the change in the tree having verified nothing. It is reverted, from the saved bytes, and
    the outcome says the tree was still written to — a revert is not the same as never writing.
    """
    repo = _repo(tmp_path)
    calls = {"n": 0}

    def _suite():
        calls["n"] += 1
        if calls["n"] == 1:
            return TestRun(ran=True, passed=345, failed=0)
        return TestRun(ran=False, detail="the runner died before printing a summary")

    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=repo, run_tests=_suite)
    _seed(store, tmp_path, state="accepted")

    outcome = store.apply("prop_0001")
    assert outcome.applied is False
    assert outcome.reverted is True
    assert outcome.files == ["module.py"], "it did write, and the revert says so"
    assert outcome.touched_the_tree is True
    assert (repo / "module.py").read_text(encoding="utf-8") == "value = 1\n"
    assert outcome.tests_after["ran"] is False
    assert store.load("prop_0001").state != "applied"


def test_an_already_failing_tree_is_refused_before_anything_is_written(tmp_path):
    """With a red baseline there is nothing to judge *against*, so the guard declines rather than
    applying a change it cannot assess."""
    repo = _repo(tmp_path)
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=repo,
                          run_tests=lambda: TestRun(ran=True, passed=1, failed=1))
    _seed(store, tmp_path, state="accepted")
    outcome = store.apply("prop_0001")
    assert outcome.applied is False
    assert "already failing" in outcome.refused
    assert (repo / "module.py").read_text(encoding="utf-8") == "value = 1\n"


def test_a_new_file_is_removed_by_undo_rather_than_left_behind(tmp_path):
    """Undo must leave the tree *as it was* — including in the direction nobody checks, a file the
    patch created that has no saved copy to restore."""
    repo = _repo(tmp_path)
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=repo,
                          run_tests=lambda: TestRun(ran=True, passed=1, failed=0))
    created = ("--- /dev/null\n+++ b/added.py\n@@ -0,0 +1,1 @@\n+print('new')\n")
    _seed(store, tmp_path, patch=created, touches=["added.py"], state="accepted")

    assert store.apply("prop_0001").applied is True
    assert (repo / "added.py").is_file()

    undone = store.undo("prop_0001")
    assert not (repo / "added.py").exists()
    assert undone.reverted is True
    assert store.load("prop_0001").state == "accepted"


def test_undo_of_a_proposal_that_was_never_applied_says_so(tmp_path):
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path))
    _seed(store, tmp_path)
    outcome = store.undo("prop_0001")
    assert outcome.applied is False
    assert "nothing to undo" in outcome.refused


def test_the_patch_paths_are_read_from_the_diff_not_the_declared_list(tmp_path):
    """The diff is what lands, so the diff is what the safety check reads. A `touches` list that
    disagreed with the patch would be the exact disagreement a check must not trust."""
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=_repo(tmp_path))
    _seed(store, tmp_path, patch=_patch("engine/config.py", "a", "b"), touches=["engine/prompts.py"])
    proposal = store.load("prop_0001")
    assert store._touched_files(proposal) == ["engine/config.py"]  # noqa: SLF001


# ── the "applies nothing" guarantee, on the paths that must keep it ──────────


def _fingerprint(root: pathlib.Path) -> dict[str, int]:
    """Every file under `root` and its size, so a write anywhere is visible as a diff."""
    return {str(p.relative_to(root)): p.stat().st_size
            for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


def test_accept_reject_and_listing_write_nothing_outside_the_proposals_directory(tmp_path):
    """The improver's central claim, extended to the lifecycle: the bookkeeping steps never edit a
    source file. Asserted by fingerprinting the whole repo, so a future change that starts writing
    fails here rather than in review."""
    repo = _repo(tmp_path)
    store = ProposalStore(workspace=_workspace(tmp_path), repo_root=repo)
    _seed(store, tmp_path, pid="prop_0001")
    _seed(store, tmp_path, pid="prop_0002")

    before = _fingerprint(repo)
    store.list()
    store.accept("prop_0001")
    store.reject("prop_0002", reason="no")
    store.undo("prop_0001")
    after = _fingerprint(repo)
    assert before == after, "a bookkeeping step edited a file in the repo"


def test_the_listing_still_says_reading_it_applied_nothing(tmp_path):
    """The field people relied on keeps its meaning: *this read* changed no files."""
    ws = _workspace(tmp_path)
    _seed(ProposalStore(workspace=ws, repo_root=_repo(tmp_path)), tmp_path)
    from engine.cli import _proposals_for

    payload = _proposals_for(Improver(workspace=ws))
    assert payload["applies_changes"] is False
    assert payload["count"] == 1


def test_every_listed_proposal_carries_what_the_panel_needs_to_offer_a_button(tmp_path):
    """The app decides whether to show Apply from `lifecycle.can_apply`, computed here — a Swift copy
    of the rule would show a button that refuses."""
    ws = _workspace(tmp_path)
    store = ProposalStore(workspace=ws, repo_root=_repo(tmp_path))
    _seed(store, tmp_path, pid="prop_0001")                     # has a patch, validated
    _seed(store, tmp_path, pid="prop_0002", patch="")           # described, not a patch

    entries = [{"proposal_id": p.proposal_id, **p.as_dict()} for p in store.list()]
    annotate(entries, store)
    by_id = {e["proposal_id"]: e["lifecycle"] for e in entries}
    assert by_id["prop_0001"]["can_apply"] is True
    assert by_id["prop_0002"]["can_apply"] is False
    assert "no patch" in by_id["prop_0002"]["why_not"]
    for lifecycle in by_id.values():
        assert lifecycle["state"] in STATES
        assert lifecycle["live_states"] == list(LIVE_STATES)


# ── the CLI surface, driven as a subprocess ──────────────────────────────────


def _cli(*args, cwd=ROOT):
    return subprocess.run([sys.executable, "-m", "engine.cli", *args],
                          capture_output=True, text=True, cwd=str(cwd))


def _creds(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({
        "version": "1.0.0",
        "providers": {"ollama": {"kind": "ollama", "base_url": "http://127.0.0.1:9",
                                 "timeout_s": 1, "max_retries": 0}},
        "models": {"known": {"qwen2.5-coder:7b": {"context_window": 32768}}},
        "defaults": {"provider": "ollama", "model": "qwen2.5-coder:7b"}}))
    return path


def test_every_proposals_verb_takes_the_workspace_flags_after_it():
    """`proposals <verb> <id> --slug s --root r` is the order a person types, and every other verb in
    this parser takes its flags there (`pool list`, `status`, `decide`, …). It was a usage error here —
    exit 2, "unrecognized arguments" — because `--slug`/`--root` lived only on the `proposals` noun,
    which is the one order the shipped surface did not accept.

    Both orders must resolve to the same workspace, which is the half that is easy to lose: argparse's
    subparser defaults are copied *over* the parent namespace, so a verb-level `--slug` with a normal
    default would silently reset a slug given before the verb. `argparse.SUPPRESS` is what keeps the
    two orders equivalent, and this asserts it rather than trusting it.
    """
    from engine.cli import build_parser

    parser = build_parser()
    ident = "prop_0001"
    for verb, extra in (("accept", []), ("reject", ["--reason", "r"]), ("apply", []), ("undo", [])):
        after = parser.parse_args(["proposals", verb, ident, *extra, "--slug", "s", "--root", "/r"])
        before = parser.parse_args(["proposals", "--slug", "s", "--root", "/r", verb, ident, *extra])
        assert (after.slug, after.root) == ("s", "/r"), f"{verb} does not take the flags after it"
        assert (before.slug, before.root) == ("s", "/r"), (
            f"{verb} loses a --slug given before it, the argparse subparser-default trap")
        # The id is still the verb's own positional argument, not swallowed by the flags.
        assert after.proposal_id == ident
        assert after.func is before.func is not None


def test_the_lifecycle_reaches_a_proposal_through_the_cli(tmp_path):
    """End to end in a subprocess: listing, reading one in full, accepting, and rejecting."""
    creds_path = _creds(tmp_path)
    root = tmp_path / "projects"
    workspace = Workspace.for_project("lifecls", root=root)
    workspace.ensure()
    store = ProposalStore(workspace=workspace, repo_root=_repo(tmp_path))
    _seed(store, tmp_path, pid="prop_0001")
    _seed(store, tmp_path, pid="prop_0002")

    listing = _cli("--json", "--config", str(creds_path), "proposals", "--slug", "lifecls",
                   "--root", str(root))
    assert listing.returncode == 0, listing.stderr
    payload = json.loads(listing.stdout)
    assert payload["count"] == 2
    assert payload["applies_changes"] is False
    assert all("lifecycle" in entry for entry in payload["proposals"])

    shown = _cli("--json", "--config", str(creds_path), "proposals", "--show", "prop_0001",
                 "--slug", "lifecls", "--root", str(root))
    assert shown.returncode == 0, shown.stderr
    detail = json.loads(shown.stdout)
    assert detail["proposal_id"] == "prop_0001"
    assert detail["patch"], "the full view must carry the patch, or it is not the full view"

    accepted = _cli("--json", "--config", str(creds_path), "proposals", "accept", "prop_0001",
                    "--slug", "lifecls", "--root", str(root))
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["state"] == "accepted"
    assert json.loads(accepted.stdout)["applied"] is False

    rejected = _cli("--json", "--config", str(creds_path), "proposals", "reject", "prop_0002",
                    "--reason", "not this week", "--slug", "lifecls", "--root", str(root))
    assert rejected.returncode == 0, rejected.stderr
    assert json.loads(rejected.stdout)["reason"] == "not this week"

    assert store.load("prop_0001").state == "accepted"
    assert store.load("prop_0002").state == "rejected"


def test_a_refused_lifecycle_step_exits_one_and_explains_itself(tmp_path):
    """A refusal is a check that failed — exit 1 with the reason on stderr, like every other command."""
    creds_path = _creds(tmp_path)
    root = tmp_path / "projects"
    result = _cli("--json", "--config", str(creds_path), "proposals", "accept", "prop_0001",
                  "--slug", "lifecls", "--root", str(root))
    assert result.returncode == 1
    assert result.stderr.strip(), "a refusal must explain itself"
    assert "prop_0001" in result.stderr


def test_the_apply_json_says_whether_the_tree_was_touched_even_when_it_refused(tmp_path):
    """A caller branching on exit 1 still has to know whether anything was written. Here: no."""
    creds_path = _creds(tmp_path)
    root = tmp_path / "projects"
    workspace = Workspace.for_project("lifecls", root=root)
    workspace.ensure()
    repo = _repo(tmp_path)
    store = ProposalStore(workspace=workspace, repo_root=repo)
    _seed(store, tmp_path, patch="", state="accepted")

    result = _cli("--json", "--config", str(creds_path), "proposals", "apply", "prop_0001",
                  "--slug", "lifecls", "--root", str(root))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["files"] == []
    assert "no patch" in payload["refused"]


# ── the console surface, so the two cannot disagree ─────────────────────────


def _console(creds_path, root, slug):
    import io

    from engine.config import load
    from engine.library import resolve
    from engine.serve import Server

    workspace = Workspace.for_project(slug, root=root)
    workspace.ensure()
    return Server(config=load(str(creds_path), warn=False), library=resolve(), workspace=workspace,
                  slug=slug, stdin=io.StringIO(""), stdout=io.StringIO())


def test_the_console_and_the_cli_describe_a_proposal_identically(tmp_path):
    """Two surfaces, one answer. A panel that showed a different lifecycle from the CLI's would teach
    a person to trust neither."""
    creds_path = _creds(tmp_path)
    root = tmp_path / "projects"
    workspace = Workspace.for_project("lifecls", root=root)
    workspace.ensure()
    store = ProposalStore(workspace=workspace, repo_root=_repo(tmp_path))
    _seed(store, tmp_path, pid="prop_0001")

    server = _console(creds_path, root, "lifecls")
    assert hasattr(server, "_cmd_proposal_accept")
    assert hasattr(server, "_cmd_proposal_apply")
    console = server._cmd_proposals({})                      # noqa: SLF001
    cli_payload = json.loads(_cli("--json", "--config", str(creds_path), "proposals",
                                  "--slug", "lifecls", "--root", str(root)).stdout)
    assert set(console) == set(cli_payload)
    assert set(console["proposals"][0]) == set(cli_payload["proposals"][0])
    assert (console["proposals"][0]["lifecycle"]
            == cli_payload["proposals"][0]["lifecycle"])
    assert console["applies_changes"] is False


def test_the_console_runs_the_same_lifecycle(tmp_path):
    """Driven through `handle`, which is how the app actually sends it — including the enum value,
    because that is what the dispatcher interpolates into the method name."""
    from engine.protocol import Command, CommandType

    creds_path = _creds(tmp_path)
    root = tmp_path / "projects"
    workspace = Workspace.for_project("lifecls", root=root)
    workspace.ensure()
    store = ProposalStore(workspace=workspace, repo_root=_repo(tmp_path))
    _seed(store, tmp_path, pid="prop_0001")
    server = _console(creds_path, root, "lifecls")

    acked = server.handle(Command(cmd_id="c1", type=CommandType.PROPOSAL_ACCEPT,
                                  payload={"id": "prop_0001"}))
    assert acked["state"] == "accepted"
    assert acked["applied"] is False
    rejected = server.handle(Command(cmd_id="c2", type=CommandType.PROPOSAL_REJECT,
                                     payload={"id": "prop_0001", "reason": "changed my mind"}))
    assert rejected["state"] == "rejected"
    assert rejected["reason"] == "changed my mind"
    assert store.load("prop_0001").state == "rejected"


def test_a_refusal_from_the_console_raises_rather_than_returning_a_false_success(tmp_path):
    """`mutate` turns a raise into a notice, so a refusal that returned normally would read as done."""
    from engine.protocol import Command, CommandType
    from engine.serve import ServerError

    creds_path = _creds(tmp_path)
    root = tmp_path / "projects"
    workspace = Workspace.for_project("lifecls", root=root)
    workspace.ensure()
    ProposalStore(workspace=workspace, repo_root=_repo(tmp_path))
    server = _console(creds_path, root, "lifecls")
    with pytest.raises(ServerError, match="prop_0001"):
        server.handle(Command(cmd_id="c1", type=CommandType.PROPOSAL_SHOW,
                              payload={"id": "prop_0001"}))
