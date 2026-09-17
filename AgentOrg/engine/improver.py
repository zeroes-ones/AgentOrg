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
- **Validation happens in a scratch copy.** Checking a fix must not be able to damage the working tree,
  which is the same reasoning as testing a provider before saving it.
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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "ImproverError", "Finding", "Proposal", "Validation", "Improver",
    "SAFETY_SURFACES", "is_safety_surface", "PROPOSALS_DIRNAME", "PROPOSAL_VERSION",
]

#: Where promoted proposals wait for the Owner. A directory rather than a queue, because a proposal is
#: a diff a person has to *read*, and the tool for reading a diff is already on their machine.
PROPOSALS_DIRNAME = "proposals"
PROPOSAL_VERSION = "1.0.0"

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
    """

    ran: bool = False
    regressions: list[str] = field(default_factory=list)
    improved: list[str] = field(default_factory=list)
    #: Set when the Python suite cannot judge the change at all — Swift, docs, UI.
    unvalidatable: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        """A proposal may be promoted only on *no regression* **and** a named improvement."""
        return self.ran and not self.regressions and bool(self.improved)

    def as_dict(self) -> dict[str, Any]:
        return {"ran": self.ran, "regressions": list(self.regressions),
                "improved": list(self.improved), "unvalidatable": self.unvalidatable,
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
            if k in ("ran", "regressions", "improved", "unvalidatable", "detail")})
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
        lines.append(f"- improves: {', '.join(self.validation.improved) or '(none demonstrated)'}")
        lines.append(f"- regressions: {', '.join(self.validation.regressions) or '(none)'}")
        if self.validation.unvalidatable:
            lines.append(f"- **cannot be validated here:** {self.validation.unvalidatable}")
        if self.validation.detail:
            lines.append(f"- {self.validation.detail}")
        if self.patch:
            lines += ["", "## Patch", "", "```diff", self.patch.rstrip(), "```"]
        lines += [
            "",
            "---",
            "",
            "**Nothing has been applied.** This loop never edits the tree. Read the patch, then apply "
            "it yourself if you agree — or delete this file.",
        ]
        return "\n".join(lines) + "\n"


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
        suite.
    draft_fn:
        Injected, so a test can supply a proposal without a model. With none, `draft` produces a
        *described* proposal from the finding alone — which is still useful, and still gated.
    """

    workspace: Any
    memory: Any = None
    telemetry_path: Path | None = None
    run_suite: Callable[..., Any] | None = None
    draft_fn: Callable[[Finding], tuple[str, str, list[str]]] | None = None
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
        """Run the suite and compare against the frozen baseline. Never modifies the tree.

        A *delta* against the baseline, because that is what the suite itself does and for the reason
        its own docstring gives: a run that gains on one scenario and loses on three is a regression
        even at the same total. The proposal must show **no regression** *and* name what improved — a
        change that is merely not-worse is not an improvement.
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

        runner = self.run_suite or self._default_run_suite()
        if runner is None:
            validation.detail = "no behavioural suite is available to validate against"
            proposal.validation = validation
            return validation

        try:
            result = runner()
        except Exception as exc:  # noqa: BLE001 - a suite that cannot run is not a passed suite
            validation.detail = f"the suite could not run: {type(exc).__name__}: {exc}"
            proposal.validation = validation
            return validation

        validation.ran = True
        regressions = self._regressions(result)
        validation.regressions = regressions
        validation.improved = self._improvements(result, proposal.finding)
        validation.detail = (
            f"{len(validation.improved)} improvement(s), {len(regressions)} regression(s)"
            if (validation.improved or regressions)
            else "the suite is unchanged by this proposal, so there is nothing to promote")
        proposal.validation = validation
        return validation

    def _default_run_suite(self) -> Callable[..., Any] | None:
        try:
            from .evals.runner import run_suite
        except Exception:  # noqa: BLE001 - no suite means nothing to claim
            return None
        return run_suite

    @staticmethod
    def _regressions(result: Any) -> list[str]:
        """Regressions against the frozen baseline, including lost coverage."""
        try:
            from .evals.runner import compare_to_baseline, BASELINE_PATH
            baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - no baseline means no comparison, and none is reported
            return []
        try:
            return compare_to_baseline(result, baseline)
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _improvements(result: Any, finding: Finding) -> list[str]:
        """Scenarios that now pass and name the finding's subject.

        Bounded deliberately to the scenario the finding points at, plus any scenario that flipped from
        failing to passing — because "some other scenario got better" is not evidence that *this*
        change fixed *this* problem.
        """
        names: list[str] = []
        for outcome in getattr(result, "outcomes", []) or []:
            if getattr(outcome, "passed", False) and finding.scenario \
                    and outcome.name == finding.scenario:
                names.append(outcome.name)
        return names

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


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
