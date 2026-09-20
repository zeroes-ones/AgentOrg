#!/usr/bin/env python3
"""proposals.py — the part of the self-improvement loop a person drives.

WHY THIS EXISTS
---------------
`engine/improver.py` stops on purpose, and its docstring says why: the machinery that judges a
proposal is code the proposing system could rewrite, so a loop that applied its own changes would
have no gate at all. That argument is sound and this module does not weaken it — `SAFETY_SURFACES`
still refuses, and `engine/proposals.py` is itself on that list, because a proposal must not be able
to aim at the applier either.

What was missing was not permission but *progress*. The loop measured a defect, wrote a patch into
`.agent_state/proposals/`, and handed the person a file and a sentence: "apply it yourself if you
agree." A detection that cannot be acted on in the tool that detected it is a report, not a loop.
So this module gives a proposal a **lifecycle a person can drive**:

    drafted ──promote──> promoted ──accept──> accepted ──apply──> applied ──undo──> accepted
       │                    │                   │
       └────reject──────────┴───────────────────┴──────> rejected   (with a reason, remembered)

`promoted` is not this module's state — it is what `Improver.promote` leaves a validated proposal in,
which makes it the commonest state in the directory. It is drawn here because it is *live*: a person
can accept it, reject it, or apply it directly (the act of applying is a decision, and requiring a
button press after the person already typed `apply` would only hide the applier on the proposals the
loop just produced).

Three of those four transitions are bookkeeping and cannot damage anything. The fourth — `apply` —
edits the working tree, so it is the only one with a safety story, and the story is *evidence, not
trust*:

- **It refuses without a demonstrated improvement.** `Validation.ok` is required whatever the person
  decided, because "the Owner agreed" and "the suite says this helps" are different claims and a
  patch needs both.
- **It refuses a proposal nothing can be applied from** — one a person already rejected, or one that
  has already landed and needs `undo` first.
- **It runs the project's own tests before and after**, by the repo's standard command rather than a
  private one, and a regression **reverts** rather than reports. An applied change that made things
  worse and stayed applied is the failure this exists to prevent.
- **A run that could not be read is not a passing run**, in both directions: an unreadable baseline
  refuses before anything is written, and a suite that cannot run *after* the change is reverted,
  because "no summary line" is not "zero failures".
- **The patch is checked before anything is written** (`git apply --check`), so a stale or
  conflicting patch is refused intact rather than half-applied.
- **The original bytes are kept**, so undo is exact rather than a reverse patch that may itself
  conflict. What cannot be undone is not applied.

Honest limits, stated rather than implied:

- **A proposal whose patch is empty is never applied.** The improver's own drafts are descriptions
  ("this node is slow; the fix is in the prompt"), not diffs. Those get accept/reject and keep the
  hand-off, and saying so is the point — inventing a patch to make the button work would be worse
  than the button doing nothing.
- **`Validation.unvalidatable` blocks apply.** A Swift or docs change is not judged by the Python
  suite, so no amount of accepting makes it verified.
- **Deletion of a proposal file is not offered here.** A rejected proposal is recorded in
  `rejected.jsonl` and its file removed, because leaving it listed would re-ask a settled question
  every poll.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .improver import (
    PROPOSALS_DIRNAME,
    Improver,
    ImproverError,
    Proposal,
    is_safety_surface,
)

__all__ = [
    "ProposalLifecycleError", "ApplyOutcome", "ProposalStore", "ProposalError", "TestRun",
    "STATES", "LIVE_STATES", "annotate",
]


def annotate(entries: list[dict[str, Any]], store: "ProposalStore") -> list[dict[str, Any]]:
    """Add each proposal's lifecycle to a listing entry, in place.

    One function, called by both the CLI's listing and the console's, because the app has to decide
    whether to *offer* an Apply button and the rule that decides is `can_apply`. A Swift copy of that
    rule would drift from this one the first time the guard changed, and the drift would show up as a
    button that is offered and then refuses — which reads as a broken app rather than a working gate.
    """
    for entry in entries:
        try:
            proposal = Proposal.from_dict(entry)
        except Exception:  # noqa: BLE001 - an entry that cannot be read is shown as it is
            continue
        entry["lifecycle"] = {
            "state": proposal.state,
            "live_states": list(LIVE_STATES),
            "can_apply": store.can_apply(proposal),
            "why_not": store.why_not(proposal),
            "has_patch": bool(proposal.patch.strip()),
        }
    return entries

#: The lifecycle states, in the order a proposal moves through them. `drafted`, `refused` and
#: `promoted` are the improver's own (`Proposal.state`, `promote()`); the rest are this module's. A
#: closed list because the state is what the app filters on, and a free-text state cannot be counted
#: or offered as a button.
#:
#: `promoted` is here because that is what a *freshly drafted and validated* proposal actually is —
#: omitting it would have made the commonest case in the directory the one state the lifecycle
#: refused, which is the sort of gap only a test against the real writer finds.
STATES: tuple[str, ...] = ("drafted", "refused", "promoted", "accepted", "rejected", "applied")

#: The states a person can still act on: everything that has neither landed nor been settled.
LIVE_STATES: tuple[str, ...] = ("drafted", "promoted", "accepted")


class ProposalLifecycleError(RuntimeError):
    """A lifecycle step that could not be taken, named so the reason is actionable."""


#: Alias kept short for the callers that read it in a traceback.
ProposalError = ProposalLifecycleError


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


@dataclass
class ApplyOutcome:
    """What applying a proposal actually did, including what it refused to do.

    The refusals carry the same weight as the success: "applied nothing, because the suite got worse"
    is a *result*, and a caller that showed only successes would teach its reader that the guard
    never fires.
    """

    applied: bool = False
    proposal_id: str = ""
    files: list[str] = field(default_factory=list)
    #: The suite's verdict, before and after. Both are recorded even when the change is reverted, so
    #: the person can see what the regression was rather than being told one existed.
    tests_before: dict[str, Any] = field(default_factory=dict)
    tests_after: dict[str, Any] = field(default_factory=dict)
    reverted: bool = False
    refused: str = ""
    #: Where the pre-apply copy of each touched file went, for a person who wants to look.
    backup_dir: str = ""
    detail: str = ""

    @property
    def touched_the_tree(self) -> bool:
        """Whether any file was written at all — including one that was written and then reverted."""
        return bool(self.files)

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied, "proposal_id": self.proposal_id, "files": list(self.files),
            "tests_before": dict(self.tests_before), "tests_after": dict(self.tests_after),
            "reverted": self.reverted, "refused": self.refused, "backup_dir": self.backup_dir,
            "detail": self.detail,
        }


@dataclass
class ProposalStore:
    """The proposals directory, read and written as a lifecycle rather than a listing.

    Parameters
    ----------
    workspace:
        A `Workspace`, or a plain path, exactly as `Improver` takes it.
    repo_root:
        The tree a patch is applied to and whose tests are run. Defaults to the current repo — the
        one the running engine lives in, because that is the tree the proposals describe.
    run_tests:
        Injected so the guard can be tested without running the whole suite twice. Defaults to this
        repo's own `run_tests.py`.
    """

    workspace: Any
    repo_root: Path | None = None
    run_tests: Callable[..., Any] | None = None

    # ── reading ─────────────────────────────────────────────────────────────

    @property
    def directory(self) -> Path:
        base = getattr(self.workspace, "state_dir", self.workspace)
        return Path(base) / PROPOSALS_DIRNAME

    def improv(self) -> Improver:
        """An `Improver` over the same workspace, so both agree on where the files live."""
        return Improver(workspace=self.workspace)

    def load(self, proposal_id: str) -> Proposal:
        """One proposal, by id. Raises rather than returning None: a missing id is a typo to report."""
        path = self._json_path(proposal_id)
        if path is None:
            live = ", ".join(sorted(self._ids())) or "(none)"
            raise ProposalLifecycleError(
                f"no proposal {proposal_id!r} in {self.directory}. Known ids: {live}")
        try:
            return Proposal.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProposalLifecycleError(f"cannot read {path}: {exc}") from exc

    def list(self) -> list[Proposal]:
        """Every proposal on disk, newest first — the same order and the same files the listing uses."""
        out: list[Proposal] = []
        if not self.directory.is_dir():
            return out
        for path in sorted(self.directory.glob("*.json")):
            try:
                out.append(Proposal.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                continue
        out.sort(key=lambda p: p.at, reverse=True)
        return out

    def decisions(self) -> list[dict[str, Any]]:
        """Every accept/reject this module has recorded, newest last, for the panel to show."""
        return self._read_journal()

    # ── transitions that cannot damage anything ──────────────────────────────

    def accept(self, proposal_id: str, *, by: str = "owner") -> Proposal:
        """Mark a proposal as agreed. **Writes nothing to the tree.**

        Kept apart from `apply` deliberately: agreeing with a proposal and letting it edit the
        working tree are two decisions, and collapsing them into one button is how a person applies
        something they only meant to say yes to.
        """
        proposal = self.load(proposal_id)
        if proposal.state == "refused":
            raise ProposalLifecycleError(
                f"{proposal_id} was refused at draft time; there is nothing to accept: "
                f"{proposal.refusal}")
        if proposal.state == "applied":
            raise ProposalLifecycleError(f"{proposal_id} has already been applied")
        if proposal.state == "rejected":
            raise ProposalLifecycleError(
                f"{proposal_id} was rejected; re-run the cycle to draft a fresh proposal rather "
                "than reopening this one")
        proposal.state = "accepted"
        self._write(proposal)
        self._journal({"at": _iso_now(), "proposal_id": proposal_id, "decision": "accepted",
                       "by": by, "kind": proposal.finding.kind,
                       "subject": proposal.finding.subject})
        return proposal

    def reject(self, proposal_id: str, *, reason: str = "", by: str = "owner") -> Proposal:
        """Mark a proposal as not wanted, with the reason that made it so.

        The reason travels into `rejected.jsonl`, which `Improver.already_rejected` reads — so a
        rejection is what stops the same *finding* being re-drafted every cycle. That is the only
        reason this records rather than just deletes: a rejection nobody can read is a loop that
        re-litigates the same decision forever.
        """
        proposal = self.load(proposal_id)
        if proposal.state == "applied":
            raise ProposalLifecycleError(
                f"{proposal_id} has already been applied; undo it before rejecting it")
        proposal.state = "rejected"
        proposal.refusal = reason or proposal.refusal or "rejected by the Owner"
        self._write(proposal)
        self._record_rejection(proposal, reason or "rejected by the Owner")
        self._journal({"at": _iso_now(), "proposal_id": proposal_id, "decision": "rejected",
                       "by": by, "reason": reason, "kind": proposal.finding.kind,
                       "subject": proposal.finding.subject})
        return proposal

    # ── the one transition that edits the tree ───────────────────────────────

    def apply(self, proposal_id: str, *, by: str = "owner", force: bool = False) -> ApplyOutcome:
        """Apply a live proposal, verified by the project's own tests and reversible.

        The order is the safety story, so it is not rearranged for convenience:

        1. **Refuse a settled proposal.** One a person rejected, or one that has already landed and
           needs `undo`, is not something to write. Everything still live may be applied —
           `promoted` included, since that is the state the loop's own writer leaves a validated
           proposal in and the command itself is the person's decision.
        2. **Refuse unless the change is verified.** `Validation.ok` is required — no demonstrated
           improvement means no edit, whatever the Owner pressed. `ok` is only ever set by a validation
           that applied *this* patch to a scratch copy and saw a scenario the baseline recorded failing
           flip to passing there, so it also means the patch exists and lands. `unvalidatable` refuses
           outright, because "the suite could not judge it" must never read as "the suite passed it".
        3. **Refuse unless a real patch exists.** A described proposal is not a patch, and pretending
           otherwise would apply nothing while reporting success.
        4. **Refuse anything aimed at the judging machinery**, re-checked here rather than trusted
           from draft time — the file list could have been edited on disk since.
        5. **Check the patch applies cleanly** (`git apply --check`) before writing a byte.
        6. **Run the tests before and after**, and revert on regression. A run that could not be read
           counts as neither: an unreadable baseline refuses here, and an unreadable run afterwards is
           reverted, because a missing summary line is not zero failures.
        """
        proposal = self.load(proposal_id)
        outcome = ApplyOutcome(proposal_id=proposal_id)

        refusal = self._why_not(proposal, force=force)
        if refusal:
            outcome.refused = refusal
            self._journal({"at": _iso_now(), "proposal_id": proposal_id, "decision": "apply_refused",
                           "by": by, "reason": refusal, "kind": proposal.finding.kind,
                           "subject": proposal.finding.subject})
            return outcome

        repo = self._repo()
        patch_path = self._patch_path(proposal)
        if patch_path is None:
            outcome.refused = (
                "the patch could not be written to a scratch file, so nothing was applied")
            return outcome

        # The patch is applied *forwarded* from the proposal's own text, in the repo root, so the
        # paths in it are repo-relative and match `touches` — the same contract the `.md` shows.
        check = _git(repo, "apply", "--check", "--verbose", str(patch_path))
        if check.returncode != 0:
            outcome.refused = (
                "the patch does not apply to the current tree — it has drifted since it was drafted. "
                f"Re-run the cycle to draft a fresh one. git said: {_tail(check.stderr)}")
            return outcome

        before = self._tests()
        outcome.tests_before = before.as_dict()
        # `ran` is checked *before* `failed`, and both are checked: a suite with no summary line has
        # zero failures and is not a passing suite (`TestRun`'s own docstring), so treating it as one
        # here would apply a change nothing measured. Nothing has been written at this point.
        if not before.ran:
            outcome.refused = (
                "the suite could not run before this change, so the change cannot be verified and "
                f"nothing was applied. {before.detail}")
            return outcome
        if before.failed:
            outcome.refused = (
                f"the suite was already failing ({before.failed} failure(s)) before this change, so "
                "there is no baseline to judge it against. Fix the tree first.")
            return outcome

        touched = self._touched_files(proposal)
        backup = self._backup(touched, proposal_id)
        outcome.backup_dir = str(backup)
        applied = _git(repo, "apply", str(patch_path))
        if applied.returncode != 0:
            outcome.refused = f"git apply failed: {_tail(applied.stderr or applied.stdout)}"
            self._restore(backup)
            return outcome
        outcome.files = sorted(touched)

        after = self._tests()
        outcome.tests_after = after.as_dict()
        if not after.ran:
            # The mirror of the baseline check, and the reason it is a *revert* rather than a success:
            # a run the parser could not read leaves the change unverified, and an unverified change
            # must not stay in the tree. Same revert-from-saved-bytes path as a regression, so the
            # tree is exactly as it was.
            self._restore(backup)
            outcome.reverted = True
            outcome.detail = (
                "reverted: the suite could not run after the change, so the change is unverified and "
                f"was not left in the tree. {after.detail}")
            self._journal({"at": _iso_now(), "proposal_id": proposal_id, "decision": "apply_reverted",
                           "by": by, "reason": outcome.detail, "files": outcome.files,
                           "kind": proposal.finding.kind, "subject": proposal.finding.subject})
            return outcome
        if after.failed:
            # A regression is reverted, not reported. An applied change that made the tree worse and
            # stayed applied is the failure this whole guard exists to prevent — and the person is
            # told *what* regressed, so a revert is information rather than a mystery.
            self._restore(backup)
            outcome.reverted = True
            outcome.detail = (
                f"reverted: the suite went from {before.passed} passed to {after.passed} passed "
                f"({after.failed} failure(s)). The tree is back exactly as it was, from the saved "
                "copy — not from a reverse patch, which can itself conflict.")
            self._journal({"at": _iso_now(), "proposal_id": proposal_id, "decision": "apply_reverted",
                           "by": by, "reason": outcome.detail, "files": outcome.files,
                           "kind": proposal.finding.kind, "subject": proposal.finding.subject})
            return outcome

        proposal.state = "applied"
        self._write(proposal)
        outcome.applied = True
        outcome.detail = (
            f"applied to {len(outcome.files)} file(s); the suite ran ({before.passed} -> "
            f"{after.passed} passed, 0 failures). Undo with `proposals undo {proposal_id}`.")
        self._journal({"at": _iso_now(), "proposal_id": proposal_id, "decision": "applied", "by": by,
                       "files": outcome.files, "backup_dir": str(backup),
                       "kind": proposal.finding.kind, "subject": proposal.finding.subject})
        return outcome

    def undo(self, proposal_id: str, *, by: str = "owner") -> ApplyOutcome:
        """Put an applied proposal's files back from the copy taken before it was applied.

        Exact rather than a reverse patch: the saved bytes are what was there, so undo cannot itself
        conflict the way `git apply -R` can once anything else has touched the file.
        """
        proposal = self.load(proposal_id)
        outcome = ApplyOutcome(proposal_id=proposal_id)
        if proposal.state != "applied":
            outcome.refused = f"{proposal_id} is {proposal.state!r}, not applied — nothing to undo"
            return outcome
        backup = self._backup_dir(proposal_id)
        if not backup.is_dir():
            outcome.refused = (
                f"the pre-apply copy of {proposal_id} is gone ({backup}), so the change cannot be "
                "undone exactly. Revert it by hand; this tool will not guess.")
            return outcome
        restored = self._restore(backup)
        outcome.files = sorted(restored)
        outcome.applied = False
        outcome.reverted = True
        outcome.detail = f"restored {len(restored)} file(s) from {backup}"
        proposal.state = "accepted"
        self._write(proposal)
        self._journal({"at": _iso_now(), "proposal_id": proposal_id, "decision": "undone", "by": by,
                       "files": outcome.files, "kind": proposal.finding.kind,
                       "subject": proposal.finding.subject})
        return outcome

    # ── the refusals, in one place so the CLI and the app cannot disagree ────

    def _why_not(self, proposal: Proposal, *, force: bool = False) -> str:
        """Why this proposal may not be applied, or an empty string when it may.

        Pure and side-effect free, so the app can ask the same question to decide whether to offer
        the button — the two surfaces then describe one rule rather than two.
        """
        # **Every live state, not just `accepted`.** `promoted` is what the improver's own writer
        # leaves a validated proposal in — the commonest state in the directory — so a gate of
        # `("accepted", "drafted")` refused the one case the lifecycle exists for, and offered the
        # button on `drafted`, the *less* proven of the two. What may not be applied is a *settled*
        # proposal: a person rejected it, or it has already landed. Widening this does not hand the
        # loop a way to apply its own change — nothing in `engine/` calls `apply` except the person's
        # own command (the CLI's `proposals apply`, the console's `proposal_apply`) — and the
        # evidence checks below are unchanged, so a promoted proposal still needs `Validation.ok`.
        if proposal.state == "applied":
            return (f"{proposal.proposal_id} has already been applied; `proposals undo` it before "
                    "applying it again")
        if proposal.state == "rejected":
            return (f"{proposal.proposal_id} was rejected — re-run the cycle to draft a fresh "
                    "proposal rather than applying a settled one")
        if proposal.state not in LIVE_STATES:
            # A closed list, so this is only reachable from a hand-edited file. Refused rather than
            # guessed, because "a state this module has never heard of" is not a licence to write.
            return (f"{proposal.proposal_id} is in state {proposal.state!r}, which the lifecycle "
                    f"does not know ({', '.join(LIVE_STATES)} are the states it can apply from)")
        if not proposal.patch.strip():
            return ("this proposal carries no patch — it is a description, not a diff. Nothing can "
                    "be applied automatically; read it and make the change yourself.")
        if proposal.validation.unvalidatable and not force:
            return ("the suite cannot judge this change, so applying it would be unverified: "
                    f"{proposal.validation.unvalidatable}")
        if not proposal.validation.ok and not force:
            return ("the suite demonstrated no improvement for this change "
                    f"({proposal.validation.detail or 'no validation recorded'}), and a change that "
                    "is merely not-worse is not an improvement. Re-run the cycle, or reject it.")
        for path in self._touched_files(proposal):
            if is_safety_surface(path):
                return (f"refused: {path} is part of the machinery that judges this loop. This "
                        "boundary is not configurable — see `improver.SAFETY_SURFACES`.")
        return ""

    def can_apply(self, proposal: Proposal, *, force: bool = False) -> bool:
        """Whether `apply` would proceed. Asked by the panel so it offers only what will work."""
        return not self._why_not(proposal, force=force)

    def why_not(self, proposal: Proposal, *, force: bool = False) -> str:
        """The public spelling of the refusal — the panel shows these words verbatim."""
        return self._why_not(proposal, force=force)

    # ── files ───────────────────────────────────────────────────────────────

    def _ids(self) -> set[str]:
        if not self.directory.is_dir():
            return set()
        return {p.stem.split("-")[0] for p in self.directory.glob("*.json")}

    def _json_path(self, proposal_id: str) -> Path | None:
        if not self.directory.is_dir():
            return None
        for path in self.directory.glob("*.json"):
            if path.stem.split("-")[0] == proposal_id:
                return path
        return None

    def _patch_path(self, proposal: Proposal) -> Path | None:
        """The proposal's patch as a scratch file, because `git apply` reads a file or stdin.

        Written under the proposals directory rather than `/tmp` so it travels with the proposal and
        a failed apply leaves behind the exact bytes that failed.
        """
        if not proposal.patch.strip():
            return None
        directory = self.directory / "patches"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{proposal.proposal_id}.diff"
        path.write_text(proposal.patch if proposal.patch.endswith("\n") else proposal.patch + "\n",
                        encoding="utf-8")
        return path

    def _touched_files(self, proposal: Proposal) -> list[str]:
        """The repo-relative paths this proposal would write, derived from the patch when it can be.

        The patch is the source of truth, not the `touches` list: a file list that disagrees with the
        diff is exactly what a safety check must not trust, and the diff is what actually lands.
        """
        paths = _paths_in_patch(proposal.patch)
        return paths or [str(t) for t in proposal.touches]

    def _repo(self) -> Path:
        if self.repo_root is not None:
            return Path(self.repo_root)
        return Path(__file__).resolve().parent.parent

    def _backup_dir(self, proposal_id: str) -> Path:
        return self.directory / "applied" / proposal_id

    def _backup(self, paths: Iterable[str], proposal_id: str) -> Path:
        """Copy every touched file aside before the patch runs, so undo is exact.

        Written before the patch rather than after: a backup taken afterwards would record the
        changed file, which is the one thing an undo must not restore.
        """
        repo = self._repo()
        directory = self._backup_dir(proposal_id)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        manifest: list[dict[str, Any]] = []
        for rel in paths:
            source = repo / rel
            entry = {"path": rel, "existed": source.is_file()}
            if source.is_file():
                target = directory / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            manifest.append(entry)
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True),
                                                 encoding="utf-8")
        return directory

    def _restore(self, backup: Path) -> list[str]:
        """Put the backed-up files back, and remove files the patch created.

        A created file has no saved copy, so restoring means deleting it — leaving it behind would
        make "undo" leave the tree different from how it started, in the one direction nobody checks.
        """
        repo = self._repo()
        manifest_path = backup / "manifest.json"
        if not manifest_path.is_file():
            return []
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        restored: list[str] = []
        for entry in manifest:
            rel = str(entry.get("path") or "")
            if not rel:
                continue
            target = repo / rel
            if entry.get("existed"):
                source = backup / rel
                if source.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    restored.append(rel)
            else:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                restored.append(rel)
        return restored

    def _write(self, proposal: Proposal) -> None:
        """Rewrite the JSON twin, and the readable `.md`, from the same object.

        Both, always: the panel reads the JSON and the person reads the markdown, and a state that
        changed in one and not the other is a proposal that says two different things about itself.
        """
        path = self._json_path(proposal.proposal_id)
        if path is None:
            kind = proposal.finding.kind or "proposal"
            path = self.directory / f"{proposal.proposal_id}-{kind}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(proposal.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        md = path.with_suffix(".md")
        md_tmp = md.with_name(md.name + f".tmp.{os.getpid()}")
        md_tmp.write_text(proposal.render(), encoding="utf-8")
        os.replace(md_tmp, md)

    def _record_rejection(self, proposal: Proposal, reason: str) -> None:
        """Append to `rejected.jsonl` in the improver's own format.

        The same file the improver writes, with the same keys, because `already_rejected` matches on
        *(kind, subject)* — a rejection recorded anywhere else would be remembered by nobody and the
        same finding would come back every cycle.
        """
        path = self.directory / "rejected.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "at": _iso_now(),
            "kind": proposal.finding.kind,
            "subject": proposal.finding.subject,
            "path": proposal.finding.path,
            "state": "rejected",
            "reason": reason,
        }
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            pass

    def _journal(self, record: dict[str, Any]) -> None:
        path = self.directory / "decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except OSError:
            pass

    def _read_journal(self, *, limit: int = 40) -> list[dict[str, Any]]:
        path = self.directory / "decisions.jsonl"
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                out.append(entry)
        return out[-limit:]

    # ── the suite ───────────────────────────────────────────────────────────

    def _tests(self) -> "TestRun":
        """Run the project's own suite. `run_tests.py` at the repo root is the documented command."""
        runner = self.run_tests
        if runner is not None:
            result = runner()
            return result if isinstance(result, TestRun) else TestRun.from_any(result)
        script = self._repo() / "run_tests.py"
        if not script.is_file():
            return TestRun(ran=False, detail=(
                f"no {script.name} at {self._repo()}, so the change cannot be verified and will not "
                "be applied"))
        try:
            proc = subprocess.run([sys.executable, str(script), "-q"], cwd=str(self._repo()),
                                  capture_output=True, text=True, timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return TestRun(ran=False, detail=f"the suite could not run: {exc}")
        return TestRun.from_output(proc.stdout + proc.stderr, proc.returncode)


@dataclass
class TestRun:
    """One suite run's verdict: how many passed, how many failed, and whether it ran at all.

    `ran` is separate from `failed == 0` on purpose. A suite that could not start has no failures and
    is not a passed suite; conflating the two is how an unverified change gets applied.
    """

    ran: bool = False
    passed: int = 0
    failed: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.ran and self.failed == 0

    def as_dict(self) -> dict[str, Any]:
        return {"ran": self.ran, "passed": self.passed, "failed": self.failed,
                "ok": self.ok, "detail": self.detail}

    @classmethod
    def from_output(cls, text: str, returncode: int) -> "TestRun":
        """Parse `N passed, M failed` — the runner's own summary line and the pytest default."""
        import re

        match = re.search(r"(\d+)\s+passed,\s+(\d+)\s+failed", text or "")
        if not match:
            # pytest's own spelling, so a repo that swaps runners is not silently unverified.
            passed = re.search(r"(\d+)\s+passed", text or "")
            failed = re.search(r"(\d+)\s+failed", text or "")
            if passed or failed:
                return cls(ran=True, passed=int(passed.group(1)) if passed else 0,
                           failed=int(failed.group(1)) if failed else 0,
                           detail=f"exit code {returncode}")
            return cls(ran=False, detail=(
                f"the suite produced no summary line (exit code {returncode}); a run that cannot be "
                "read is not a passed run"))
        return cls(ran=True, passed=int(match.group(1)), failed=int(match.group(2)),
                   detail=f"exit code {returncode}")

    @classmethod
    def from_any(cls, result: Any) -> "TestRun":
        """Adapt a stubbed result, so a test can inject a verdict without a private class."""
        if isinstance(result, dict):
            return cls(ran=bool(result.get("ran", True)), passed=int(result.get("passed") or 0),
                       failed=int(result.get("failed") or 0), detail=str(result.get("detail") or ""))
        passed = int(getattr(result, "passed", 0) or 0)
        failed = int(getattr(result, "failed", 0) or 0)
        return cls(ran=bool(getattr(result, "ran", True)), passed=passed, failed=failed,
                   detail=str(getattr(result, "detail", "") or ""))


def _paths_in_patch(patch: str) -> list[str]:
    """The `+++ b/<path>` side of a unified diff — where a change lands.

    The **destination** side, not the source: a rename or a new file has no source path, and a
    creation has `--- /dev/null`. Reading the source side would under-report exactly the files the
    safety check most needs to see.
    """
    out: list[str] = []
    for line in (patch or "").splitlines():
        if not line.startswith("+++ "):
            continue
        raw = line[4:].strip()
        if raw.startswith("\t"):                  # some emitters separate the path with a tab
            raw = raw.split("\t", 1)[0]
        if raw == "/dev/null":
            continue
        if raw.startswith("b/"):
            raw = raw[2:]
        if raw and raw not in out:
            out.append(raw)
    return out


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run git in the repo. `git apply` rather than `patch(1)` because the patch is git-shaped."""
    try:
        return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                              timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 127, "", str(exc))


def _tail(text: str, limit: int = 300) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[-limit:] if len(cleaned) > limit else cleaned
