#!/usr/bin/env python3
"""improver.py — find the system's own defects, draft fixes, prove them, and stop.

WHY THIS EXISTS
---------------
The master design deferred this explicitly (`DESIGN.md` §11): *"Self-improvement is deferred. Recall is
context-only; there is no trace→draft→promote loop."* Every piece the loop needs already existed —
a behavioural suite with a frozen baseline, per-node telemetry, memory with a poisoning guard, an
effect journal, a Goal runtime to drive it — so the gap was never capability. It was the **loop**, and
its **gate**.

This module is that loop. It has four stages, and separating them is the whole design, because
"an agent that fixes the app itself" hides four problems that fail differently:

- **Q1 detect** — *where is the defect?* Answered by **measurement**, never by asking a model to look
  for bugs. A finding without evidence does not exist, because a model told to find problems will
  always find some.
- **Q2 draft** — *what is the fix?* A model may be involved here, but the output is a **diff plus a
  rationale plus the evidence**, and it is only ever a proposal.
- **Q3 validate** — *did it help?* The eval suite, compared against the frozen baseline as a **delta**.
  A change that is merely not-worse is not an improvement.
- **Q4 promote** — *may it be applied?* **The Owner.** This module never applies anything. Not
  conditionally, not with a flag. See `SAFETY_SURFACES`.

DESIGN
------
- **The boundary is code, not config.** An improver that can edit its own eval gate can make anything
  pass, so a system whose safety is enforced by code it can rewrite does not have that property. The
  refused list is hard-coded; a config knob would imply a supported alternative and there is none.
- **Validation patches a copy, never the tree.** A proposal that carries a patch is applied to a
  throwaway copy of the tree and the suite runs *there* — so the proposal can be shown to have
  *caused* a flip, and validating a proposal cannot be the thing that changes the code being judged.
  A proposal carrying no patch has nothing to run *with*; the suite still runs, because the honest
  result of doing nothing is worth recording, and the detail says so.
- **An improvement is a delta the patch caused.** The named scenario must have been failing in the
  frozen baseline, failing in the copy *before* the patch, and passing *after* it. "It passes now" is
  true of every proposal, including one that changes nothing, and a flip the patch did not cause is
  somebody else's fix.
- **A refusal names the path.** An improver that quietly discards work teaches nobody anything.
- **Rejections are recorded**, so the same proposal is not re-litigated every cycle.
- **Promotion is a file, not an action.** A proposal is written where a person can read and diff it.

Usage:
    improver = Improver(workspace=ws, library=lib)
    findings = improver.detect()
    for finding in findings:
        proposal = improver.draft(finding)
        if proposal is not None:
            improver.validate(proposal)          # fills in the proof
            improver.promote(proposal)           # writes it out for the Owner
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "ImproverError", "Finding", "Proposal", "Validation", "Improver",
    "SAFETY_SURFACES", "is_safety_surface", "PROPOSALS_DIRNAME", "PROPOSAL_VERSION",
    "SCRATCH_EXCLUDES", "SCRATCH_TIMEOUT_S",
]

#: Where promoted proposals wait for the Owner. A directory rather than a queue, because a proposal is
#: a diff a person has to *read*, and the tool for reading a diff is already on their machine.
PROPOSALS_DIRNAME = "proposals"
PROPOSAL_VERSION = "1.0.0"

#: What a scratch copy leaves out, and why each entry is safe to leave out.
#:
#: Validation copies the tree so the suite can be run against a *patched* copy, which means the copy
#: needs everything the suite reads and nothing else:
#:
#: - `.git`         — history and objects. The copy is not a repository, and a patch is not a commit;
#:                    `git apply` works on files, so nothing here is needed.
#: - `.build`       — Swift build output (`macos/.build` is ~640 MB on this checkout alone).
#: - `projects/`    — the user's own workspaces, including a *live* run's traces and artifacts. A
#:                    validation must not read the state of the run it is judging.
#: - `.agent_state` — this engine's spans, proposals and journals, for the same reason.
#: - caches and stale bytecode: `.agent_cache`, `.pytest_cache`, `.ruff_cache`, `__pycache__`.
#:
#: Measured on this checkout: the copy is 248 files / 6.7 MB and takes ~0.17 s to make. The tree it
#: copies is 663 MB, of which `macos/.build` alone is 639 MB — so the exclusions are what make a
#: copy-per-validation affordable rather than a reason to skip the copy and patch in place.
SCRATCH_EXCLUDES: tuple[str, ...] = (
    ".git", ".build", "projects", ".agent_cache", ".agent_state",
    ".pytest_cache", ".ruff_cache", "__pycache__",
)

#: How long a scratch suite run may take before it is declared unrunnable.
#:
#: The suite takes ~2 s here, so this is a ceiling for a *hung* run rather than a budget. A run that
#: hits it is reported as one that could not run — never as a pass, which is the same rule the suite
#: itself follows for a scenario that cannot run.
SCRATCH_TIMEOUT_S = 900

#: The machinery that judges this loop. **Refused, always, and not configurable.**
#:
#: The argument is short: an improver that can edit its own eval gate can make anything pass. A system
#: whose safety property is enforced by code the system may rewrite does not have that property. So this
#: is a hard-coded list rather than a setting — a setting implies a supported alternative, and there is
#: none.
#:
#: A proposal touching any of these is rejected **at draft time**, with the path named, because a
#: silently discarded proposal teaches nobody anything.
SAFETY_SURFACES: tuple[str, ...] = (
    "engine/evals/",              # the gate that validates proposals
    "engine/guardrail.py",        # the safety floor
    "engine/goal.py",             # the budget and continuation runtime
    "engine/host.py",             # supervision of the runner
    "engine/config.py",           # budget, policy and redaction live here
    "engine/improver.py",         # this module: it may not rewrite its own rules
    # The applier — the *only* module that edits a tree on a proposal's behalf, and therefore the one
    # a proposal must never be able to aim at. Without this line the boundary is a formality: the
    # loop could draft a patch to the code that decides whether a patch may land.
    "engine/proposals.py",
    "credentials.json",           # secrets
    "macos/Sources/AgentOrgKit/ConsoleAppDelegate.swift",   # what keeps the engine alive
    "macos/Sources/AgentOrg/App.swift",                     # the scene and lifecycle
)


def is_safety_surface(path: str) -> bool:
    """Whether a path is part of the machinery that judges this loop.

    Normalised so `./engine/evals/runner.py`, `engine/evals/runner.py` and an absolute path all match:
    the check has to be about *what the file is*, not how it was spelled, or it is a check that
    a slightly different path defeats.
    """
    text = str(path or "").strip().lstrip("./")
    if not text:
        return False
    # Match on the tail so an absolute path matches its repo-relative form.
    for surface in SAFETY_SURFACES:
        if text == surface or text.startswith(surface) or text.endswith("/" + surface):
            return True
        # A directory entry (`engine/evals/`) must match a file inside it.
        if surface.endswith("/") and f"/{surface}" in f"/{text}":
            return True
    return False


class ImproverError(RuntimeError):
    """An improver step that could not be taken, named so the reason is actionable."""


# ── findings ─────────────────────────────────────────────────────────────────


@dataclass
class Finding:
    """One measured defect, with the evidence that produced it.

    **A finding is not a defect.** It is a *measurement* — "this node was rejected three times with the
    same verdict" — and the evidence field is what keeps it honest. A detector that can produce an
    opinion ("the reviewer seems weak") is a detector that produces imaginary bugs, which is why every
    kind below is derived from data the engine already emits.
    """

    kind: str
    #: Which surface it points at, repo-relative. Used for the safety check.
    path: str = ""
    subject: str = ""
    detail: str = ""
    #: The measurement: numbers, ids, counts. Never prose.
    evidence: dict[str, Any] = field(default_factory=dict)
    #: Which eval scenario the fix would be expected to flip, when one is known.
    scenario: str = ""
    severity: str = "minor"          # minor | major | critical
    at: str = ""

    #: The kinds this module can produce, and nothing else. A closed vocabulary because a free-text kind
    #: cannot be counted, filtered or remembered across cycles.
    KINDS = (
        "repeated_rejection",        # the same node rejected the same way, repeatedly
        "stagnant_loop",             # a bounded loop that never converged
        "budget_burn",               # a node whose cost is far above its peers
        "unbinding_skills",          # a plan needing a skill nobody holds
        "cache_instability",         # a prefix that keeps changing
        "failing_agent",             # an agent whose health has degraded
    )

    def __post_init__(self) -> None:
        if not self.kind:
            raise ImproverError("a finding must name its kind")
        if self.kind not in self.KINDS:
            raise ImproverError(
                f"unknown finding kind {self.kind!r}; the vocabulary is closed so findings can be "
                f"counted and remembered: {', '.join(self.KINDS)}")
        if not self.at:
            self.at = _iso_now()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "path": self.path, "subject": self.subject,
            "detail": self.detail, "evidence": dict(self.evidence),
            "scenario": self.scenario, "severity": self.severity, "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        return cls(kind=str(data.get("kind") or ""), path=str(data.get("path") or ""),
                   subject=str(data.get("subject") or ""), detail=str(data.get("detail") or ""),
                   evidence=dict(data.get("evidence") or {}), scenario=str(data.get("scenario") or ""),
                   severity=str(data.get("severity") or "minor"), at=str(data.get("at") or ""))

    def summary(self) -> str:
        where = f" {self.subject}" if self.subject else ""
        return f"{self.kind}{where}: {self.detail}"


# ── proposals ────────────────────────────────────────────────────────────────


@dataclass
class Validation:
    """What running the suite with and without the proposal actually showed.

    Both halves are required. "Not worse" is not an improvement, and calling it one is how a
    self-improving system drifts.

    `patched_in_scratch` is separate from `ran` on purpose. `ran` means the suite produced a result;
    `patched_in_scratch` means there was a patch, it was applied to a **copy** of the tree, and the
    suite ran against that copy. Only the second can evidence that a proposal changed anything, and a
    result without it is a run of the tree as it already stands.
    """

    ran: bool = False
    regressions: list[str] = field(default_factory=list)
    improved: list[str] = field(default_factory=list)
    #: Set when the Python suite cannot judge the change at all — Swift, docs, UI.
    unvalidatable: str = ""
    #: Whether the patch was applied to a scratch copy and the suite run there. The working tree is
    #: never written to; see `Improver._validate_patch`.
    patched_in_scratch: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        """A proposal may be promoted only on *no regression* **and** a named improvement."""
        return self.ran and not self.regressions and bool(self.improved)

    def as_dict(self) -> dict[str, Any]:
        return {"ran": self.ran, "regressions": list(self.regressions),
                "improved": list(self.improved), "unvalidatable": self.unvalidatable,
                "patched_in_scratch": self.patched_in_scratch,
                "detail": self.detail, "ok": self.ok}


@dataclass
class Proposal:
    """A drafted fix, its rationale, and the proof that it helps.

    Deliberately a **description of a change**, not the change. This module never writes to a source
    file; it writes a proposal. The distinction is the entire safety story, so it is enforced by there
    being no code path here that edits the tree.
    """

    proposal_id: str
    finding: Finding
    rationale: str = ""
    #: Unified-diff-shaped text, or `None` when the change is described rather than patched.
    patch: str = ""
    #: Files the change would touch, repo-relative. Checked against `SAFETY_SURFACES`.
    touches: list[str] = field(default_factory=list)
    validation: Validation = field(default_factory=Validation)
    state: str = "drafted"           # drafted | refused | rejected | promoted
    refusal: str = ""
    at: str = ""
    version: str = PROPOSAL_VERSION

    def __post_init__(self) -> None:
        if not self.at:
            self.at = _iso_now()

    @property
    def promotes(self) -> bool:
        """Whether this may be shown to the Owner as ready."""
        return self.state == "promoted"

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_version": self.version,
            "proposal_id": self.proposal_id,
            "state": self.state,
            "refusal": self.refusal,
            "rationale": self.rationale,
            "patch": self.patch,
            "touches": list(self.touches),
            "validation": self.validation.as_dict(),
            "finding": self.finding.as_dict(),
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Proposal":
        validation = Validation(**{
            k: v for k, v in (data.get("validation") or {}).items()
            if k in ("ran", "regressions", "improved", "unvalidatable", "patched_in_scratch",
                     "detail")})
        return cls(
            proposal_id=str(data.get("proposal_id") or ""),
            finding=Finding.from_dict(data.get("finding") or {}),
            rationale=str(data.get("rationale") or ""),
            patch=str(data.get("patch") or ""),
            touches=[str(t) for t in (data.get("touches") or [])],
            validation=validation,
            state=str(data.get("state") or "drafted"),
            refusal=str(data.get("refusal") or ""),
            at=str(data.get("at") or ""),
            version=str(data.get("proposal_version") or PROPOSAL_VERSION))

    def render(self) -> str:
        """The human-readable form written to the proposals directory.

        A proposal a person cannot read at a glance is a proposal they will approve without reading,
        which defeats the gate. So: what was found, what would change, what it would improve, and what
        it would break.
        """
        lines = [
            f"# Proposal {self.proposal_id}",
            "",
            f"- **state:** {self.state}",
            f"- **found:** {self.finding.summary()}",
            f"- **severity:** {self.finding.severity}",
            f"- **drafted:** {self.at}",
            "",
            "## Why",
            "",
            self.rationale or "(no rationale given — a proposal without one should not be approved)",
            "",
            "## Evidence",
            "",
            "```json",
            json.dumps(self.finding.evidence, indent=2, sort_keys=True),
            "```",
            "",
            "## What would change",
            "",
        ]
        if self.touches:
            for path in self.touches:
                lines.append(f"- `{path}`")
        else:
            lines.append("(no file paths declared — treat this as a description, not a patch)")
        lines += ["", "## Validation", ""]
        lines.append(f"- ran: {self.validation.ran}")
        if self.validation.patched_in_scratch:
            lines.append("- applied to a **scratch copy** of the tree, and the suite ran there; the "
                         "working tree was not written to")
        lines.append(f"- improves: {', '.join(self.validation.improved) or '(none demonstrated)'}")
        lines.append(f"- regressions: {', '.join(self.validation.regressions) or '(none)'}")
        if self.validation.unvalidatable:
            lines.append(f"- **cannot be validated here:** {self.validation.unvalidatable}")
        if self.validation.detail:
            lines.append(f"- {self.validation.detail}")
        if self.patch:
            lines += ["", "## Patch", "", "```diff", self.patch.rstrip(), "```"]
        lines += ["", "---", ""]
        lines.append(self._closing())
        return "\n".join(lines) + "\n"

    def _closing(self) -> str:
        """What the last line says about the state — and it must not contradict it.

        The default is the boundary working: *nothing has been applied*. But a proposal that a person
        has since accepted or applied keeps its file, and a fixed sentence would then tell its reader
        the tree is untouched while the change is in it. The words follow the state, because a
        proposal's one job is to be honest about what has happened to it.
        """
        if self.state == "applied":
            return ("**This change has been applied to the tree**, verified by the project's own test "
                    "suite before and after. Undo it with `proposals undo`.")
        if self.state == "accepted":
            return ("**Accepted — nothing has been applied.** Applying it is a separate step "
                    "(`proposals apply`), and it is refused unless the suite demonstrates an "
                    "improvement and no regression.")
        if self.state == "rejected":
            return ("**Rejected**, and the reason is recorded so the same finding is not re-drafted "
                    "every cycle. Nothing has been applied.")
        return ("**Nothing has been applied.** This loop never edits the tree. Read the patch, then "
                "apply it yourself if you agree — or delete this file.")


# ── the loop ─────────────────────────────────────────────────────────────────


@dataclass
class Improver:
    """Detect, draft, validate, promote — and stop.

    Parameters
    ----------
    workspace:
        The project whose traces and memory are read. A `Workspace` or a plain path.
    memory:
        The memory store, for run history. Optional: with none, the trace-derived detectors still work.
    telemetry_path:
        The spans file to read. Defaults to the workspace's own.
    run_suite:
        Injected, so the validator can be tested without running 17 scenarios. Defaults to the real
        suite. It runs the suite **as it stands**, for a proposal that carries no patch.
    draft_fn:
        Injected, so a test can supply a proposal without a model. With none, `draft` produces a
        *described* proposal from the finding alone — which is still useful, and still gated.
    repo_root:
        The tree a proposal's patch is written against, and the tree a scratch copy copies. Defaults to
        the checkout this module lives in, because that is the tree a proposal describes.
    scratch_suite:
        How the suite is run *inside a scratch copy*. Defaults to a subprocess of this repo's own
        runner with the copy as its working directory — out of process on purpose, because an
        in-process run imports the engine already loaded in `sys.path` and would keep exercising the
        original tree however the working directory were set.
    """

    workspace: Any
    memory: Any = None
    telemetry_path: Path | None = None
    run_suite: Callable[..., Any] | None = None
    draft_fn: Callable[[Finding], tuple[str, str, list[str]]] | None = None
    repo_root: Path | None = None
    scratch_suite: Callable[[Path], Any] | None = None
    _counter: int = 0

    # ── Q1: detect ──────────────────────────────────────────────────────────

    def detect(self, *, min_repeats: int = 2, cost_outlier: float = 3.0) -> list[Finding]:
        """Read the evidence the engine already produces and return findings.

        **Measurement, not opinion.** Every finding below is derived from a recorded fact, because the
        alternative — a model asked to find problems — reliably invents them. A defect that leaves no
        trace is outside this loop's reach, and this loop says so rather than guessing.
        """
        findings: list[Finding] = []
        spans = self._spans()
        findings.extend(self._repeated_rejections(spans, min_repeats=min_repeats))
        findings.extend(self._stagnant_loops(spans))
        findings.extend(self._cost_outliers(spans, factor=cost_outlier))
        findings.extend(self._failing_agents(spans))
        findings.extend(self._unbinding_skills(spans))
        return findings

    def _spans(self) -> list[dict[str, Any]]:
        """The recorded spans, newest last. An absent file is an empty history, not an error."""
        path = self.telemetry_path
        if path is None:
            base = getattr(self.workspace, "state_dir", None)
            path = Path(base) / "telemetry" / "spans.jsonl" if base else None
        if path is None or not Path(path).is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def _repeated_rejections(self, spans: list[dict[str, Any]], *, min_repeats: int) -> list[Finding]:
        """A node rejected the same way more than once.

        The signature of a *procedure* problem rather than a worker problem: the reviewer keeps saying
        the same thing, so either the skill does not require it or the producer cannot see the rule. The
        fix is in the skill or the prompt, not in the agent's effort.
        """
        counts: dict[tuple[str, str], int] = {}
        skills: dict[tuple[str, str], str] = {}
        for span in spans:
            verdict = str(span.get("verdict") or "")
            if verdict not in ("changes_requested", "rejected", "fail"):
                continue
            node = str(span.get("node") or (span.get("attributes") or {}).get("node") or "")
            if not node:
                continue
            key = (node, verdict)
            counts[key] = counts.get(key, 0) + 1
            for owner, digest in (span.get("skill_hashes") or {}).items():
                skills[key] = str(owner or digest)

        out: list[Finding] = []
        for (node, verdict), count in sorted(counts.items(), key=lambda kv: -kv[1]):
            if count < min_repeats:
                continue
            out.append(Finding(
                kind="repeated_rejection",
                path=".agentorg/skills/",
                subject=node,
                detail=(f"node {node!r} produced {verdict!r} {count} time(s); a repeated verdict is a "
                        "property of the procedure, not of the attempt"),
                evidence={"node": node, "verdict": verdict, "count": count,
                          "skill": skills.get((node, verdict), "")},
                severity="major" if count >= 3 else "minor",
                scenario="skill-enforceability"))
        return out

    def _stagnant_loops(self, spans: list[dict[str, Any]]) -> list[Finding]:
        """A node that used its whole iteration budget without converging.

        A bounded loop that always exhausts its bound is not "working hard"; it is a bound that is
        wrong, or a task the node cannot do. Both are fixable, and neither is visible from a pass rate.
        """
        out: list[Finding] = []
        for span in spans:
            iterations = int(span.get("iterations") or 0)
            status = str(span.get("status") or "")
            verdict = str(span.get("verdict") or "")
            if iterations >= 3 and verdict in ("changes_requested", "needs_review", "rejected"):
                node = str(span.get("node") or (span.get("attributes") or {}).get("node") or "?")
                out.append(Finding(
                    kind="stagnant_loop",
                    path=".agentorg/skills/",
                    subject=node,
                    detail=(f"node {node!r} used {iterations} iteration(s) and still ended "
                            f"{verdict!r}; the loop is turning without converging"),
                    evidence={"node": node, "iterations": iterations, "status": status,
                              "verdict": verdict},
                    severity="major",
                    scenario="loop-termination"))
        return out

    def _cost_outliers(self, spans: list[dict[str, Any]], *, factor: float) -> list[Finding]:
        """A node whose measured cost is far above its peers.

        Uses only *measured* costs: an unmeasured node is excluded rather than treated as zero, so the
        detector cannot invent an outlier out of missing data — the same rule the ledger follows.
        """
        costs: dict[str, list[float]] = {}
        for span in spans:
            cost = span.get("cost_usd")
            if cost is None or not span.get("cost_measured"):
                continue
            node = str(span.get("node") or (span.get("attributes") or {}).get("node") or "?")
            costs.setdefault(node, []).append(float(cost))
        # The comparison base is the **median of the per-node medians**, not their mean. A mean is
        # dragged upward by the very outlier it is meant to detect: with costs of 0.01, 0.02 and 0.50 the
        # mean is 0.177, so the 0.50 node looks like 2.8x — under any useful threshold, and the detector
        # finds nothing. The median of {0.01, 0.02, 0.50} is 0.02, so the same node reads as 25x and is
        # caught. This was a real bug found by the test, and the fix is to compare against a statistic
        # the outlier cannot move.
        #
        # Three nodes minimum: with two, the median of the medians *is* the outlier, so the comparison
        # finds nothing — and reporting nothing as though it were a result is worse than declining.
        if len(costs) < 3:
            return []
        medians = {node: sorted(v)[len(v) // 2] for node, v in costs.items() if v}
        if not medians:
            return []
        ordered = sorted(medians.values())
        overall = ordered[len(ordered) // 2]
        if overall <= 0:
            return []
        out: list[Finding] = []
        for node, value in sorted(medians.items()):
            if value >= overall * factor:
                out.append(Finding(
                    kind="budget_burn",
                    path="engine/prompts.py",
                    subject=node,
                    detail=(f"node {node!r} costs ~${value:.4f} against a suite median of "
                            f"~${overall:.4f} ({value / overall:.1f}x)"),
                    evidence={"node": node, "median_usd": value, "suite_median_usd": overall,
                              "factor": round(value / overall, 2)},
                    severity="major"))
        return out

    def _failing_agents(self, spans: list[dict[str, Any]]) -> list[Finding]:
        """An agent whose recorded outcomes are mostly failures.

        The health system already tracks this; the improver reads the same evidence so the two cannot
        disagree about who is struggling.
        """
        outcomes: dict[str, list[bool]] = {}
        for span in spans:
            agent = str(span.get("agent_id") or span.get("agent") or "")
            if not agent:
                continue
            verdict = str(span.get("verdict") or span.get("status") or "")
            ok = verdict in ("pass", "approved", "done", "complete")
            outcomes.setdefault(agent, []).append(ok)
        out: list[Finding] = []
        for agent, results in sorted(outcomes.items()):
            if len(results) < 3:
                continue                    # not enough evidence to call it a pattern
            rate = sum(results) / len(results)
            if rate <= 0.34:
                out.append(Finding(
                    kind="failing_agent",
                    path=".agentorg/roster.json",
                    subject=agent,
                    detail=(f"agent {agent} succeeded {sum(results)}/{len(results)} time(s); a model or "
                            "level change is the cheap fix before any retraining story"),
                    evidence={"agent": agent, "attempts": len(results),
                              "successes": sum(results), "success_rate": round(rate, 3)},
                    severity="major"))
        return out

    def _unbinding_skills(self, spans: list[dict[str, Any]]) -> list[Finding]:
        """A plan that needed a skill the roster does not staff.

        Detected from a gate or a staffing gap recorded in a span, so it is a fact about a run rather
        than a guess about a roster.
        """
        out: list[Finding] = []
        for span in spans:
            attrs = span.get("attributes") or {}
            gaps = attrs.get("staffing_gaps") or []
            if not gaps:
                continue
            for gap in gaps:
                skill = str((gap or {}).get("skill") or "(unknown)")
                out.append(Finding(
                    kind="unbinding_skills",
                    path=".agentorg/roster.json",
                    subject=skill,
                    detail=(f"a plan required {skill!r}, which no agent holds; the run stalls at "
                            "binding time rather than at the cause"),
                    evidence={"skill": skill, "node": (gap or {}).get("node_id"),
                              "reason": (gap or {}).get("reason")},
                    severity="major",
                    scenario="planner-reports-gaps"))
        return out

    # ── Q2: draft ───────────────────────────────────────────────────────────

    def draft(self, finding: Finding) -> Proposal | None:
        """Turn a finding into a proposal, or refuse it here.

        The safety check runs **first and unconditionally**, before any model is involved: a finding
        that points at the judging machinery is refused whatever a model would have said about it.
        """
        self._counter += 1
        proposal = Proposal(proposal_id=f"prop_{self._counter:04d}", finding=finding)

        rationale, patch, touches = "", "", []
        if self.draft_fn is not None:
            try:
                rationale, patch, touches = self.draft_fn(finding)
            except Exception as exc:  # noqa: BLE001 - a drafting failure is a refused proposal
                proposal.state = "refused"
                proposal.refusal = f"drafting failed: {type(exc).__name__}: {exc}"
                return proposal

        # A finding that declares no path still gets the finding's own path checked.
        candidate_paths = list(touches) or ([finding.path] if finding.path else [])
        for path in candidate_paths:
            if is_safety_surface(path):
                proposal.state = "refused"
                proposal.touches = candidate_paths
                proposal.refusal = (
                    f"refused: {path} is part of the machinery that judges this loop. A system whose "
                    "safety is enforced by code it can rewrite does not have that property, so this "
                    "boundary is not configurable.")
                return proposal

        proposal.rationale = rationale or self._default_rationale(finding)
        proposal.patch = patch
        proposal.touches = candidate_paths
        proposal.state = "drafted"
        return proposal

    def _default_rationale(self, finding: Finding) -> str:
        """A rationale derived from the finding, for when no model is wired in.

        Deliberately plain and evidence-bound: it restates what was measured and what kind of change
        that implies, rather than asserting a fix. A proposal whose *description* is a guess is still
        worth showing, because the evidence and the gate are what make it safe.
        """
        return (
            f"Measured: {finding.detail}\n\n"
            f"Evidence: {json.dumps(finding.evidence, sort_keys=True)}\n\n"
            "This is a proposal, not a fix. Nothing has been applied.")

    # ── Q3: validate ────────────────────────────────────────────────────────

    def validate(self, proposal: Proposal) -> Validation:
        """Run the suite **with the proposal's patch applied to a scratch copy**, and compare.

        Three outcomes, and the difference between them is the point:

        - **A proposal carrying a patch** has that patch applied to a throwaway copy of the tree, and
          the suite runs there — twice, once as the tree stands and once with the patch — so a flip can
          be attributed to *this* patch. The working tree is never written to: validating a proposal
          must not be the thing that changes the code being judged.
        - **A proposal carrying no patch** cannot be run *with* anything. The suite still runs, because
          the honest result of doing nothing is worth recording, and the detail says plainly that a run
          of the unchanged tree cannot evidence a fix.
        - **A patch that cannot be applied** — none to apply, aimed at a safety surface, or not applying
          cleanly — is refused with the reason named, before a scratch copy or a suite run is spent.

        Either way the claim is a *delta the patch caused*, never a threshold: no regression against the
        frozen baseline, and the scenario the finding names must have been failing in that baseline,
        failing in the copy before the patch, and passing after it.
        """
        validation = Validation()
        if proposal.state == "refused":
            validation.detail = "not validated: the proposal was refused at draft time"
            proposal.validation = validation
            return validation

        # A change the Python suite cannot judge. Saying so is the honest outcome; implying it is safe
        # because it could not be checked is how an unvalidated change gets promoted.
        if any(_is_swift_or_docs(p) for p in proposal.touches):
            validation.unvalidatable = (
                "This touches Swift or documentation, which the Python behavioural suite does not "
                "exercise. The app build is the only check and this loop does not run it.")
            validation.detail = "unvalidated by design — review it as you would any other patch"
            proposal.validation = validation
            return validation

        baseline = self._baseline()

        if proposal.patch.strip():
            self._validate_patch(proposal, validation, baseline)
            proposal.validation = validation
            return validation

        runner = self.run_suite or self._default_run_suite()
        if runner is None:
            validation.detail = "no behavioural suite is available to validate against"
            proposal.validation = validation
            return validation
        self._validate_unchanged(proposal, validation, runner, baseline)
        proposal.validation = validation
        return validation

    def _validate_unchanged(self, proposal: Proposal, validation: Validation, runner: Callable[..., Any],
                            baseline: dict[str, Any] | None) -> None:
        """A proposal with no patch: run the suite as it stands, and say what that can mean.

        Kept rather than short-circuited because "nothing was demonstrated" is a result worth
        recording, and a described proposal is still shown to the Owner (`promote` writes it out;
        `proposals.apply` refuses it for carrying no patch). There is nothing to apply, so no scratch
        copy is built and nothing here can be read as *caused* by the proposal.
        """
        try:
            result = runner()
        except Exception as exc:  # noqa: BLE001 - a suite that cannot run is not a passed suite
            validation.detail = f"the suite could not run: {type(exc).__name__}: {exc}"
            return

        validation.ran = True
        validation.regressions = self._regressions(result)
        validation.improved = self._improvements(result, proposal.finding, baseline)
        if validation.improved or validation.regressions:
            validation.detail = (f"{len(validation.improved)} improvement(s), "
                                 f"{len(validation.regressions)} regression(s)")
        else:
            validation.detail = (
                "no patch to apply, so nothing this proposal does can be shown to have changed the "
                "suite; " + self._no_improvement_detail(proposal.finding, baseline))

    def _validate_patch(self, proposal: Proposal, validation: Validation,
                        baseline: dict[str, Any] | None) -> None:
        """Apply the patch to a scratch copy, run the suite there twice, and fill in the proof.

        The copy is made before anything is checked and removed in a `finally`, so a patch that cannot
        be applied, a suite that raises, and a clean run all leave the filesystem as they found it. Two
        runs rather than one because a flip has to be *caused*: the first run is the tree as it stands,
        and a scenario that already passed there cannot have been fixed by this patch.
        """
        # Lazy: `engine/proposals.py` imports this module. Its `git apply` and its patch-path reader
        # are reused rather than re-implemented, because a second answer to "what does this patch
        # touch" or "does it apply" is a second answer that can disagree with the one that writes bytes.
        from .proposals import _git, _paths_in_patch, _tail

        paths = _paths_in_patch(proposal.patch) or [str(t) for t in proposal.touches]
        for path in paths:
            if is_safety_surface(path):
                validation.detail = (
                    f"refused: the patch aims at {path}, which is part of the machinery that judges "
                    "this loop. That boundary is not configurable, and the patch was not applied — not "
                    "even to a scratch copy.")
                return

        parent = Path(tempfile.mkdtemp(prefix="agentorg-scratch-"))
        root = parent / "tree"
        patch_file = parent / "proposal.diff"
        try:
            # `symlinks=False` dereferences: if a link ever appears in the tree, the copy holds real
            # bytes, so a patch applied in the copy cannot write through to the original file.
            shutil.copytree(self.repo(), root,
                            ignore=shutil.ignore_patterns(*SCRATCH_EXCLUDES), symlinks=False)
            patch_file.write_text(
                proposal.patch if proposal.patch.endswith("\n") else proposal.patch + "\n",
                encoding="utf-8")

            suite = self.scratch_suite or self._scratch_suite_result
            before = suite(root)

            check = _git(root, "apply", "--check", "--verbose", str(patch_file))
            if check.returncode == 127:
                validation.detail = (
                    f"refused: git could not be run, so the patch can be neither checked nor applied "
                    f"({_tail(check.stderr)})")
                return
            if check.returncode != 0:
                validation.detail = (
                    "refused: the patch does not apply to a copy of the current tree — it has drifted "
                    f"since it was drafted. git said: {_tail(check.stderr)}")
                return
            applied = _git(root, "apply", str(patch_file))
            if applied.returncode != 0:
                validation.detail = (
                    "refused: the patch passed its check but git could not apply it "
                    f"({_tail(applied.stderr or applied.stdout)})")
                return

            after = suite(root)
        except Exception as exc:  # noqa: BLE001 - a copy or suite that fails is not a passed suite
            validation.detail = (f"the patch was not validated: {type(exc).__name__}: {exc}")
            return
        finally:
            shutil.rmtree(parent, ignore_errors=True)

        validation.ran = True
        validation.patched_in_scratch = True
        validation.regressions = self._regressions(after)
        validation.improved = self._flips(after, before, proposal.finding, baseline)
        validation.detail = self._patch_detail(proposal.finding, baseline, before,
                                               validation.improved, validation.regressions)

    def repo(self) -> Path:
        """The tree a patch is written against: this package's checkout, unless one was injected.

        A field rather than a constant because a test needs to point the loop at a throwaway tree; the
        default is the checkout the running engine lives in, which is the tree a proposal describes.
        """
        if self.repo_root is not None:
            return Path(self.repo_root)
        return Path(__file__).resolve().parent.parent

    def _scratch_suite_result(self, root: Path) -> Any:
        """Run the behavioural suite inside `root` — out of process, with the copy as the cwd.

        Out of process on purpose: the suite imports the engine from `sys.path`, so an in-process run
        would keep exercising *this* tree whatever directory it was pointed at, and the copy would be
        decorated rather than tested. A subprocess started in the copy imports the copy's engine, which
        is the only run that can show what the patch does.
        """
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "engine.evals.runner", "--json", "--no-gate"],
                cwd=str(root), capture_output=True, text=True, timeout=SCRATCH_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ImproverError(f"the suite could not be run in the scratch copy: {exc}") from exc
        try:
            payload = json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            payload = {}
        if not (payload.get("results") or {}):
            # An unparseable or empty result is not "the suite ran and found no scenarios": it is a
            # suite that never got far enough to run one — a patch that broke the engine's import, for
            # instance. Reading it as a run would report lost coverage for a cause it never named, and
            # the cause is the useful part: git's own output and Python's traceback name it.
            from .proposals import _tail

            raise ImproverError(
                "the suite ran no scenario in the scratch copy; stderr: " + _tail(proc.stderr))
        return _result_from_payload(payload)

    def _default_run_suite(self) -> Callable[..., Any] | None:
        try:
            from .evals.runner import run_suite
        except Exception:  # noqa: BLE001 - no suite means nothing to claim
            return None
        return run_suite

    @staticmethod
    def _baseline() -> dict[str, Any] | None:
        """The frozen baseline, or `None` when it cannot be read.

        `None` is a distinct outcome from an empty baseline: it means no comparison is possible at all,
        and the callers must not launder that into "nothing improved" (nor into "nothing regressed").
        """
        try:
            from .evals.runner import BASELINE_PATH
            return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable baseline is a missing comparison, not a pass
            return None

    @staticmethod
    def _regressions(result: Any) -> list[str]:
        """Regressions against the frozen baseline, including lost coverage.

        A comparison that could not be *made* is reported as a regression rather than as an absence of
        one: "the check could not run" and "nothing regressed" are different claims, and the second is
        the one that lets a change through. An unreadable baseline stays the exception, because a
        baseline that is not there means no comparison exists at all — and `improved` is empty without
        one, so nothing can be promoted on the strength of that.
        """
        try:
            from .evals.runner import compare_to_baseline, BASELINE_PATH
            baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - no baseline means no comparison, and none is reported
            return []
        try:
            return compare_to_baseline(result, baseline)
        except Exception as exc:  # noqa: BLE001 - a comparison that raised is not a passed check
            return [f"the run could not be compared against the baseline: {type(exc).__name__}: {exc}"]

    @staticmethod
    def _improvements(result: Any, finding: Finding,
                      baseline: dict[str, Any] | None) -> list[str]:
        """Scenarios the baseline recorded **failing** that now pass, bounded to the finding's subject.

        A flip *against the frozen baseline*, not merely a passing scenario. `baseline.json` records all
        17 scenarios passing, so "it passes now" is true of every proposal, including one with an empty
        diff; counting that as an improvement stamps the unchanged tree as a fix.

        Bounded deliberately to the scenario the finding points at, because "some other scenario got
        better" is not evidence that *this* change fixed *this* problem.

        Used for a proposal that carries **no patch**: with nothing applied, the run cannot be said to
        have caused anything, so the baseline is the only thing a flip could be measured against.
        """
        scenario = finding.scenario
        if not scenario or not baseline:
            return []
        was = (baseline.get("results") or {}).get(scenario)
        if not was or was.get("passed"):
            return []
        if _passed(result, scenario):
            return [scenario]
        return []

    @staticmethod
    def _flips(after: Any, before: Any, finding: Finding,
               baseline: dict[str, Any] | None) -> list[str]:
        """The named scenario, when the proposed patch can be shown to have *caused* it to flip.

        Three conditions, all required:

        1. the frozen baseline recorded it **failing** — because "it passes now" is true of every
           proposal, including one that changes nothing;
        2. it failed in the scratch copy **before** the patch was applied, so the flip is this patch's
           doing rather than something already sitting in the tree;
        3. it passes **after** the patch.

        Dropping (2) would credit a patch with a fix somebody else already made, which is the same
        misattribution as counting an already-passing scenario.
        """
        scenario = finding.scenario
        if not scenario or not baseline:
            return []
        was = (baseline.get("results") or {}).get(scenario)
        if not was or was.get("passed"):
            return []
        if not _failed(before, scenario):
            return []
        return [scenario] if _passed(after, scenario) else []

    def _patch_detail(self, finding: Finding, baseline: dict[str, Any] | None, before: Any,
                      improvements: list[str], regressions: list[str]) -> str:
        """What the two scratch runs showed, named rather than asserted.

        When neither an improvement nor a regression holds, the reason has to say *which* of the
        required conditions failed — otherwise "nothing to promote" reads as a formality rather than as
        a diagnosis.
        """
        if improvements or regressions:
            return (f"{len(improvements)} improvement(s), {len(regressions)} regression(s) — the patch "
                    "was applied to a scratch copy of the tree and the suite ran there; the working "
                    "tree was not written to")
        return ("the patch was applied to a scratch copy of the tree and the suite ran there, and "
                "nothing flipped: " + self._no_flip_detail(finding, baseline, before))

    @staticmethod
    def _no_flip_detail(finding: Finding, baseline: dict[str, Any] | None, before: Any) -> str:
        """Why the patched run evidenced no improvement, named case by case."""
        trailing = "nothing to promote"
        scenario = finding.scenario
        if not scenario:
            return (f"the finding names no scenario to improve, so a run cannot evidence a fix; "
                    f"{trailing}")
        if baseline is None:
            return (f"the baseline cannot be read, so no failing-to-passing flip for {scenario!r} can "
                    f"be shown and none is claimed; {trailing}")
        was = (baseline.get("results") or {}).get(scenario)
        if was is None:
            return (f"the baseline records no {scenario!r}, so a run cannot evidence a fix; {trailing}")
        if was.get("passed"):
            return (f"the baseline already records {scenario!r} as passing, so this patch cannot have "
                    f"flipped it — the patch has demonstrated nothing; {trailing}")
        if not _failed(before, scenario):
            return (f"{scenario!r} already passes against the unpatched tree, so the flip is not this "
                    f"patch's doing; {trailing}")
        return (f"{scenario!r} was failing at baseline and still fails with the patch applied, so "
                f"nothing flipped; {trailing}")

    @staticmethod
    def _no_improvement_detail(finding: Finding, baseline: dict[str, Any] | None) -> str:
        """Why nothing can have improved, named rather than merely asserted.

        The old detail read "the suite is unchanged", which sounds neutral. The honest reading is
        stronger: a run of the *unchanged* tree cannot evidence a fix at all, and when the baseline
        already records the named scenario as passing there is no flip left to observe. Saying which of
        those happened is what stops "nothing to promote" from reading as a formality.
        """
        trailing = "the suite is unchanged by this proposal, so there is nothing to promote"
        scenario = finding.scenario
        if not scenario:
            return f"the finding names no scenario to improve, so a run cannot evidence a fix; {trailing}"
        if baseline is None:
            return (f"the baseline cannot be read, so no failing-to-passing flip for {scenario!r} can "
                    f"be shown and none is claimed; {trailing}")
        was = (baseline.get("results") or {}).get(scenario)
        if was is None:
            return (f"the baseline records no {scenario!r}, so a run of the unchanged tree cannot "
                    f"evidence a fix; {trailing}")
        if was.get("passed"):
            return (f"the baseline already records {scenario!r} as passing, so a run of the unchanged "
                    f"tree cannot evidence a fix — this proposal has demonstrated nothing; {trailing}")
        return f"{scenario!r} was failing at baseline and does not pass now, so nothing flipped; {trailing}"

    # ── Q4: promote (write it out; never apply) ─────────────────────────────

    def promote(self, proposal: Proposal) -> Path | None:
        """Write a validated proposal where the Owner can read it. **Applies nothing.**

        Returns None when the proposal has no right to be shown: refused, regressed, or unvalidated.
        Returning None rather than raising keeps the caller simple and, more importantly, means the
        only way into the proposals directory is a proposal that earned it.
        """
        if proposal.state == "refused":
            self._record_rejection(proposal)
            return None
        if proposal.validation.unvalidatable:
            # Shown, but labelled: a Swift change cannot be validated here, and saying so is the honest
            # outcome rather than implying it is safe.
            proposal.state = "promoted"
            return self._write(proposal)
        if not proposal.validation.ok:
            proposal.state = "rejected"
            self._record_rejection(proposal)
            return None
        proposal.state = "promoted"
        return self._write(proposal)

    def _write(self, proposal: Proposal) -> Path:
        """"Write the proposal atomically, beside the run state.

        Temp-then-`os.replace`, like every other write here: a half-written proposal is a file a person
        may act on.
        """
        directory = self.proposals_dir()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{proposal.proposal_id}-{proposal.finding.kind}.md"
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        try:
            tmp.write_text(proposal.render(), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ImproverError(f"cannot write the proposal to {target}: {exc}") from exc
        # The machine-readable twin, so the console can list proposals without parsing markdown.
        meta = target.with_suffix(".json")
        meta.write_text(json.dumps(proposal.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return target

    def proposals_dir(self) -> Path:
        """Where proposals wait for the Owner: `.agent_state/proposals/`."""
        base = getattr(self.workspace, "state_dir", self.workspace)
        return Path(base) / PROPOSALS_DIRNAME

    # ── rejection memory ────────────────────────────────────────────────────

    def _record_rejection(self, proposal: Proposal) -> None:
        """Remember a refusal or rejection, so the same proposal is not re-litigated every cycle.

        Recorded by *(kind, subject)* rather than by proposal id: the id changes every cycle, so keying
        on it would remember nothing and the loop would re-propose the same thing forever.
        """
        path = self.proposals_dir() / "rejected.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "at": _iso_now(),
            "kind": proposal.finding.kind,
            "subject": proposal.finding.subject,
            "path": proposal.finding.path,
            "state": proposal.state,
            "reason": proposal.refusal or proposal.validation.detail
                      or "; ".join(proposal.validation.regressions) or "no improvement demonstrated",
        }
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            pass

    def already_rejected(self, finding: Finding) -> bool:
        """Whether this exact *finding* has been refused or rejected before."""
        path = self.proposals_dir() / "rejected.jsonl"
        if not path.is_file():
            return False
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if (rec.get("kind") == finding.kind and rec.get("subject") == finding.subject):
                    return True
        except (OSError, json.JSONDecodeError):
            return False
        return False

    # ── the cycle ───────────────────────────────────────────────────────────

    def run_once(self, *, skip_rejected: bool = True) -> list[Proposal]:
        """One full pass: detect, draft each finding, validate, and promote what earned it.

        Returns every proposal considered — promoted, rejected and refused alike — because the refusals
        are the interesting part. A cycle that reports only its successes hides the boundary working.
        """
        considered: list[Proposal] = []
        for finding in self.detect():
            if skip_rejected and self.already_rejected(finding):
                continue
            proposal = self.draft(finding)
            if proposal is None:
                continue
            self.validate(proposal)
            self.promote(proposal)
            considered.append(proposal)
        return considered


def _is_swift_or_docs(path: str) -> bool:
    """Whether a path is something the Python suite cannot judge."""
    text = str(path or "").lower()
    return text.endswith((".swift", ".md", ".plist")) or text.startswith("macos/")


def _result_from_payload(payload: dict[str, Any]) -> Any:
    """A suite's `--json` output as the real `ScenarioResult`, so the gate's own comparison runs.

    Rebuilt as the actual dataclass rather than a look-alike because `compare_to_baseline` and the
    improvement rule are the gate: a private copy of the result type would let the two drift, and a
    validation that compared with a different function from the one that ships is not the same check.
    """
    from .evals.runner import Outcome, ScenarioResult

    result = ScenarioResult()
    for name, entry in sorted((payload.get("results") or {}).items()):
        entry = entry or {}
        result.outcomes.append(Outcome(
            name=str(name), passed=bool(entry.get("passed")),
            check=str(entry.get("check") or ""), detail=str(entry.get("detail") or ""),
            error=str(entry.get("error") or ""),
            duration_ms=float(entry.get("duration_ms") or 0.0)))
    return result


def _outcome(result: Any, name: str) -> Any:
    """The named scenario's outcome in a suite result, or `None` when it did not run."""
    for outcome in getattr(result, "outcomes", []) or []:
        if outcome.name == name:
            return outcome
    return None


def _passed(result: Any, name: str) -> bool:
    outcome = _outcome(result, name)
    return bool(outcome is not None and getattr(outcome, "passed", False))


def _failed(result: Any, name: str) -> bool:
    """Whether the scenario ran *and* did not pass. Missing is not failing: a scenario that did not run
    has demonstrated nothing, and crediting it would be coverage invented out of absence."""
    outcome = _outcome(result, name)
    return bool(outcome is not None and not getattr(outcome, "passed", False))


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
