#!/usr/bin/env python3
"""delegation.py — an agent hiring help, and the invariants that make it safe.

WHY THIS EXISTS
---------------
Agent-recursion is the most dangerous feature in a multi-agent system. The library is blunt
about why: each additional delegation hop compounds hallucination probability by 15–20%, and an
unbounded chain "burns $500 in API tokens in 8 minutes before anyone notices". Meanwhile a
naive "spawn an agent when you need one" design lets an agent tree spend without bound.

So this module is mostly *refusals*, and they are code rather than prompt text:

- **A reuse-first ladder.** Spawning is the last rung. An agent must first show that no existing
  agent could do the work, and it must record that evidence — a request with no ladder evidence
  is rejected outright.
- **Six hard invariants (S1–S6).** Depth cap, cycle detection, budget *partitioning* (never
  creation), least-privilege capabilities, the five-element context pass-through, and lineage.
- **A tiered approval authority.** Cheap and reversible helpers are auto-approved; anything
  durable, privileged or expensive reaches the Owner with the full justification.

DESIGN
------
- **The requisition is the artifact.** It is modelled on a hiring requisition, and per the
  library's recruiting rule it must state a *capability gap and an outcome*, not a wish for
  help. Its completeness is validated, not trusted.
- **Helpers die with their scope; specialists are retired deliberately.** Every extension needs
  a removal path, or an experiment becomes permanent infrastructure.
- **Span of control is capped**, so one agent cannot become a bottleneck holding fifty reports.
- **Lineage is recorded for every spawn**, so an orphaned agent is impossible.

Usage:
    desk = HiringDesk(org, config)
    request = Requisition(...)
    outcome = desk.evaluate(request)              # auto-approve, or gate to the Owner
    if outcome.approved:
        desk.spawn(request, outcome)
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .agent import (
    AgentError,
    AgentKind,
    AgentLevel,
    AgentSpec,
    AgentState,
    Budget,
    new_agent_id,
)
from .handoff import CONTEXT_ELEMENTS, validate_delegation_context
from .roster import Org, OrgError

__all__ = [
    "ApprovalTier",
    "DelegationError",
    "DelegationOutcome",
    "HiringDesk",
    "LadderEvidence",
    "Requisition",
    "RequisitionState",
    "S_INVARIANTS",
]

#: The six invariants, named so a refusal can cite one and an operator can look it up.
S_INVARIANTS: dict[str, str] = {
    "S1": "delegation depth may not exceed the cap (default 3)",
    "S2": "an agent already in the active chain may not be delegated to (cycle detection)",
    "S3": "a child's budget is carved from the parent's remainder, never created",
    "S4": "a child receives an explicit least-privilege capability set, never the parent's",
    "S5": "every delegation carries the five mandatory context elements",
    "S6": "every spawn records its lineage: parent, chain, requisition, approver and tier",
}


class DelegationError(RuntimeError):
    """Raised when a delegation is refused.

    Carries the invariant id so the caller can cite it and the Owner can look it up.
    """

    def __init__(self, message: str, *, invariant: str = "", detail: dict[str, Any] | None = None) -> None:
        self.invariant = invariant
        self.detail = detail or {}
        prefix = f"[{invariant}] " if invariant else ""
        super().__init__(prefix + message)


def _default_elevated_markers() -> tuple[str, ...]:
    """The capability prefixes that force an Owner gate, from the one place that owns the default.

    Imported lazily because `config` imports this package's neighbours: the delegation module must not
    pull the whole config loader in at import time just to read a five-element tuple. Falls back to
    the literal only if the import genuinely fails, so a partially-importable tree still gates machine
    access rather than silently auto-approving it.
    """
    try:
        from ..config import DelegationConfig

        return DelegationConfig().elevated_markers()
    except Exception:  # noqa: BLE001 - a gate must not disappear because an import broke
        return ("write:", "deploy:", "exec:", "admin:", "system:")


class ApprovalTier(str, Enum):
    """Who approves a hire, and why.

    Modelled on an offer-authority band: cheap and reversible is automatic, durable or
    privileged reaches the Owner with a business case.
    """

    T0 = "T0"  # auto: helper, existing skill, within budget, shallow
    T1 = "T1"  # auto + notify: helper, known skill, low budget, read-only
    T2 = "T2"  # Owner gate: specialist, or write/deploy capabilities, or higher budget
    T3 = "T3"  # Owner gate, explicit: new capability domain, elevated privilege, deep


class RequisitionState(str, Enum):
    """Where a requisition is in its lifecycle."""

    REQUESTED = "REQUESTED"
    REJECTED_RUNG = "REJECTED_RUNG"     # the ladder was not walked
    AUTO_APPROVED = "AUTO_APPROVED"
    OWNER_GATE = "OWNER_GATE"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    AMENDED = "AMENDED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    DESTROYED = "DESTROYED"
    RETIREMENT_REVIEW = "RETIREMENT_REVIEW"
    RETIRED = "RETIRED"


@dataclass
class LadderEvidence:
    """What the requester tried before asking to hire.

    This is the field that makes "reuse first" enforceable rather than aspirational. Without it
    an agent can request a new specialist for work an idle colleague could have done, and the
    roster grows while token-per-task climbs.
    """

    reuse_attempted: list[dict[str, str]] = field(default_factory=list)
    wider_agent_attempted: list[dict[str, str]] = field(default_factory=list)
    why_not_self: str = ""

    def complete(self) -> tuple[bool, str]:
        """Whether the ladder was actually walked, and why not.

        At least one of the two rungs must show an attempt, and `why_not_self` must be stated —
        otherwise the request is an unexplained "I need help".
        """
        if not self.reuse_attempted and not self.wider_agent_attempted:
            return False, (
                "no ladder evidence: reuse_attempted and wider_agent_attempted are both empty. "
                "Show which existing agents were tried and why each was rejected, or do the "
                "work yourself."
            )
        if not (self.why_not_self or "").strip():
            return False, (
                "why_not_self is empty. State why you cannot do this work yourself — an "
                "unexplained request is not a justification."
            )
        return True, ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "reuse_attempted": list(self.reuse_attempted),
            "wider_agent_attempted": list(self.wider_agent_attempted),
            "why_not_self": self.why_not_self,
        }


@dataclass
class Requisition:
    """A request to hire a helper or a specialist.

    Modelled on a hiring requisition because the same discipline applies: state the gap, the
    outcome, and what was already tried. A request missing any of those is rejected
    automatically rather than being passed to a human to untangle.
    """

    requester_id: str
    requester_name: str = ""
    kind: str = "helper"                 # "helper" | "specialist"
    skill: str = ""
    provider: str = ""
    model: str = ""
    context_window: int | None = None
    capabilities: list[str] = field(default_factory=list)
    requested_tokens: int = 0
    requested_usd: float = 0.0
    # The capability gap: what is needed and why nothing existing suffices.
    needed: list[str] = field(default_factory=list)
    why_existing_insufficient: str = ""
    expected_outcome: str = ""
    ladder: LadderEvidence = field(default_factory=LadderEvidence)
    # The five-element context pass-through for the delegated work.
    context: dict[str, Any] = field(default_factory=dict)
    # Lineage, set by the desk.
    run_id: str = ""
    node_id: str = ""
    task_id: str = ""
    id: str = ""
    state: RequisitionState = RequisitionState.REQUESTED
    tier: ApprovalTier | None = None
    decided_by: str = ""
    denial_reason: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"req_{os.urandom(4).hex()}"
        if not self.created_at:
            self.created_at = _iso_now()

    # ── completeness ────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """Every reason this requisition is incomplete.

        Returned as a list rather than raised so the caller can report all the problems at once
        — an agent that fixes one and resubmits only to fail on the next wastes a round trip.
        """
        problems: list[str] = []
        if not self.skill.strip():
            problems.append("no skill named: state which capability the new agent must hold")
        if not (self.why_existing_insufficient or "").strip():
            problems.append(
                "capability_gap.why_existing_insufficient is empty: state why no existing "
                "agent can do this"
            )
        if not self.needed:
            problems.append("capability_gap.needed is empty: name the specific capabilities")
        if not (self.expected_outcome or "").strip():
            problems.append(
                "expected_outcome is empty: state the outcome, not the request. A requisition "
                "without an outcome cannot be judged."
            )
        ladder_ok, ladder_why = self.ladder.complete()
        if not ladder_ok:
            problems.append(ladder_why)
        if self.kind not in ("helper", "specialist"):
            problems.append(f"kind must be 'helper' or 'specialist'; got {self.kind!r}")
        if self.requested_usd < 0 or self.requested_tokens < 0:
            problems.append("requested budget cannot be negative")
        return problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "requisition_version": "1.0.0",
            "id": self.id,
            "requester": {"agent_id": self.requester_id, "name": self.requester_name},
            "kind": self.kind,
            "skill": self.skill,
            "provider": self.provider,
            "model": self.model,
            "context_window": self.context_window,
            "capabilities": list(self.capabilities),
            "requested_budget": {"max_tokens": self.requested_tokens, "max_usd": self.requested_usd},
            "capability_gap": {
                "needed": list(self.needed),
                "why_existing_insufficient": self.why_existing_insufficient,
            },
            "ladder_evidence": self.ladder.as_dict(),
            "expected_outcome": self.expected_outcome,
            "trigger": {"run_id": self.run_id, "node_id": self.node_id, "task_id": self.task_id},
            "state": self.state.value,
            "tier": self.tier.value if self.tier else None,
            "decided_by": self.decided_by,
            "denial_reason": self.denial_reason,
            "created_at": self.created_at,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Requisition":
        """Rebuild from a persisted dict."""
        trigger = data.get("trigger") if isinstance(data.get("trigger"), dict) else {}
        gap = data.get("capability_gap") if isinstance(data.get("capability_gap"), dict) else {}
        budget = data.get("requested_budget") if isinstance(data.get("requested_budget"), dict) else {}
        ladder_raw = data.get("ladder_evidence") if isinstance(data.get("ladder_evidence"), dict) else {}
        try:
            state = RequisitionState(str(data.get("state") or "REQUESTED"))
        except ValueError:
            state = RequisitionState.REQUESTED
        tier_raw = data.get("tier")
        try:
            tier = ApprovalTier(str(tier_raw)) if tier_raw else None
        except ValueError:
            tier = None
        return cls(
            requester_id=str((data.get("requester") or {}).get("agent_id") or ""),
            requester_name=str((data.get("requester") or {}).get("name") or ""),
            kind=str(data.get("kind") or "helper"),
            skill=str(data.get("skill") or ""),
            provider=str(data.get("provider") or ""),
            model=str(data.get("model") or ""),
            context_window=data.get("context_window"),
            capabilities=[str(c) for c in (data.get("capabilities") or [])],
            requested_tokens=int(budget.get("max_tokens", 0)),
            requested_usd=float(budget.get("max_usd", 0.0)),
            needed=[str(n) for n in (gap.get("needed") or [])],
            why_existing_insufficient=str(gap.get("why_existing_insufficient") or ""),
            expected_outcome=str(data.get("expected_outcome") or ""),
            ladder=LadderEvidence(
                reuse_attempted=list(ladder_raw.get("reuse_attempted") or []),
                wider_agent_attempted=list(ladder_raw.get("wider_agent_attempted") or []),
                why_not_self=str(ladder_raw.get("why_not_self") or ""),
            ),
            context=data.get("context") if isinstance(data.get("context"), dict) else {},
            run_id=str(trigger.get("run_id") or ""),
            node_id=str(trigger.get("node_id") or ""),
            task_id=str(trigger.get("task_id") or ""),
            id=str(data.get("id") or ""),
            state=state,
            tier=tier,
            decided_by=str(data.get("decided_by") or ""),
            denial_reason=str(data.get("denial_reason") or ""),
            created_at=str(data.get("created_at") or ""),
        )


@dataclass
class DelegationOutcome:
    """The desk's verdict on a requisition."""

    approved: bool
    tier: ApprovalTier
    reason: str
    state: RequisitionState
    agent_id: str | None = None
    needs_owner: bool = False
    amendment: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "tier": self.tier.value,
            "reason": self.reason,
            "state": self.state.value,
            "agent_id": self.agent_id,
            "needs_owner": self.needs_owner,
            "amendment": self.amendment,
        }


@dataclass
class HiringDesk:
    """Evaluates requisitions and spawns agents within the six invariants.

    Parameters
    ----------
    org:
        The roster, for reuse checks, span of control and lineage.
    max_depth:
        Invariant S1's cap. A fourth hop is refused with a clear error rather than allowed to
        compound error and cost.
    span_of_control:
        How many concurrent reports one agent may hold before it must route to a peer instead.
    allow_ephemeral:
        Whether helpers may be spawned at all. A deployment can forbid them entirely.
    approval_tiers:
        Threshold table for the automatic-versus-Owner decision.
    """

    org: Org
    max_depth: int = 3
    span_of_control: int = 5
    allow_ephemeral: bool = True
    budget_share_max: float = 0.50
    approval_tiers: dict[str, Any] = field(default_factory=dict)
    spend_per_hour: dict[str, list[float]] = field(default_factory=dict, repr=False)
    _spawned: dict[str, AgentSpec] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if self.max_depth < 1:
            raise DelegationError("max_depth must be >= 1", invariant="S1")
        if self.span_of_control < 1:
            raise DelegationError("span_of_control must be >= 1")

    # ── the ladder ──────────────────────────────────────────────────────────

    def ladder(self, *, skill: str, requester_id: str) -> dict[str, Any]:
        """Walk the reuse-first ladder and report what exists.

        This is offered to the requesting agent *before* it drafts a requisition, so it can see
        whether an idle colleague already covers the need. Rung order matters: reuse is cheapest
        and carries no hallucination cost, so it is tried first.
        """
        holders = self.org.agents_for_skill(skill)
        available = [a for a in holders
                     if self.org.runtime(a.id).available() and a.id != requester_id]
        return {
            "skill": skill,
            "rung_1_reuse": [
                {"agent_id": a.id, "name": a.name, "state": self.org.runtime(a.id).state.value,
                 "model": a.model, "level": int(a.level)}
                for a in available
            ],
            "rung_2_wider": self._wider_capability_candidates(skill, requester_id),
            "recommendation": (
                "reuse an existing agent (rung 1)" if available else
                "try a wider-capability agent (rung 2)" if self._wider_capability_candidates(skill, requester_id)
                else "no existing agent covers this; a hire may be justified"
            ),
        }

    def _wider_capability_candidates(self, skill: str, requester_id: str) -> list[dict[str, Any]]:
        """Agents that could plausibly cover a skill they do not literally hold.

        A senior agent on a related skill is a genuine alternative to hiring, and the wider-agent
        rung exists so the ladder is not a formality. The heuristic is level plus a shared skill
        family (a common prefix), which is deliberately loose — the point is to surface options,
        and the requesting agent must still justify rejecting them.
        """
        family = skill.split("-")[0]
        out: list[dict[str, Any]] = []
        for spec in self.org.agents.values():
            if spec.id == requester_id or not spec.is_ai:
                continue
            if spec.has_skill(skill):
                continue
            related = [s for s in spec.skills if s.split("-")[0] == family]
            if not related and int(spec.level) < int(AgentLevel.STAFF):
                continue
            out.append({
                "agent_id": spec.id, "name": spec.name, "level": int(spec.level),
                "related_skills": related or list(spec.skills),
                "state": self.org.runtime(spec.id).state.value,
            })
        out.sort(key=lambda e: (-e["level"], e["name"]))
        return out[:5]

    # ── evaluation ──────────────────────────────────────────────────────────

    def evaluate(self, request: Requisition, *,
                 active_chain: Iterable[str] = (), depth: int = 0,
                 parent_budget: Budget | None = None) -> DelegationOutcome:
        """Validate a requisition and classify which approval tier it lands in.

        Refusals happen in the order that gives the most actionable message: completeness first
        (the agent can fix it), then the hard invariants (nobody can), then the tier decision
        (the Owner may).
        """
        # Completeness: an unjustified request is rejected, not forwarded.
        problems = request.validate()
        if problems:
            request.state = RequisitionState.REJECTED_RUNG
            request.decided_by = "validation"
            return DelegationOutcome(
                approved=False, tier=ApprovalTier.T0,
                reason="requisition rejected automatically: " + "; ".join(problems),
                state=RequisitionState.REJECTED_RUNG, needs_owner=False,
            )

        chain = list(active_chain)

        # S1 — depth cap.
        if depth >= self.max_depth:
            raise DelegationError(
                f"delegation depth {depth} has reached the cap {self.max_depth}. The chain is "
                f"{' -> '.join(chain) or '(root)'}. Resolve or escalate with a best-effort answer; "
                "do not delegate further.",
                invariant="S1",
                detail={"depth": depth, "max_depth": self.max_depth, "chain": chain},
            )

        # S2 — cycle detection against the active delegation chain.
        #
        # The chain is the list of *requester ids* already in this delegation path. Refusing when
        # the requester is already present catches A→B→A, which burns budget without progress.
        # Comparing on the skill as well would be wrong: delegating the same capability to a
        # *different* agent is legitimate horizontal fan-out.
        if request.requester_id and request.requester_id in chain:
            raise DelegationError(
                f"cycle detected: {request.requester_name or request.requester_id!r} is already in "
                f"the active delegation chain ({' -> '.join(chain)}). A cycle burns budget without "
                "producing progress.",
                invariant="S2",
                detail={"chain": chain, "requester_id": request.requester_id},
            )

        # S5 — the five-element context pass-through.
        context_verdict = validate_delegation_context(request.context)
        if not context_verdict.ok:
            raise DelegationError(
                f"delegation context is incomplete: {context_verdict.reason}. A delegate without "
                "all five elements re-discovers the problem from scratch.",
                invariant="S5", detail=context_verdict.violations[0].detail,
            )

        # Span of control.
        active_reports = len([
            report for report in self.org.direct_reports(request.requester_id)
            if self.org.runtime(report.id).state not in (AgentState.TERMINATED, AgentState.QUARANTINED)
        ])
        if active_reports >= self.span_of_control:
            raise DelegationError(
                f"{request.requester_name or request.requester_id!r} already holds "
                f"{active_reports} active reports, at the span-of-control cap "
                f"{self.span_of_control}. Route to a peer or escalate instead of widening the "
                "tree.",
                invariant="",
                detail={"active_reports": active_reports, "cap": self.span_of_control},
            )

        # Tier classification runs on the *requested* figures, before partitioning, so the
        # Owner's thresholds compare against what was asked for rather than against the share
        # the parent happened to have left.
        tier, reason = self.classify_tier(request)
        request.tier = tier

        # S3 — budget partitioning, not creation.
        share = self._partition_budget(request, parent_budget)
        request.requested_tokens = share["tokens"]
        request.requested_usd = share["usd"]

        # Helpers may be forbidden outright by configuration.
        if request.kind == "helper" and not self.allow_ephemeral:
            request.state = RequisitionState.DENIED
            return DelegationOutcome(
                approved=False, tier=ApprovalTier.T0,
                reason="ephemeral helpers are disabled by configuration; request a specialist "
                       "instead, which the Owner approves explicitly",
                state=RequisitionState.DENIED, needs_owner=True,
            )

        if tier in (ApprovalTier.T0, ApprovalTier.T1):
            request.state = RequisitionState.AUTO_APPROVED
            request.decided_by = f"policy:{tier.value}"
            # Auto-approval must complete the hire, not merely permit it: returning approved
            # without an agent_id would leave the caller holding a permission with nothing to
            # use it on, and the helper would silently never exist.
            spawn = self._spawn(request, active_chain=chain, depth=depth,
                                parent_budget=parent_budget)
            spawn.reason = f"{reason}. {spawn.reason}"
            return spawn

        # T2/T3 reach the Owner with the full requisition.
        request.state = RequisitionState.OWNER_GATE
        request.decided_by = f"policy:{tier.value}"
        return DelegationOutcome(
            approved=False, tier=tier, reason=reason,
            state=RequisitionState.OWNER_GATE, needs_owner=True,
        )

    def _elevated_capabilities(self, capabilities: list[str],
                               markers: tuple[str, ...]) -> list[str]:
        """Which of these capabilities genuinely reach beyond the project.

        `write:` is in the marker list because an unscoped write is a grant the whole filesystem
        would have to be trusted with, and the tool layer's containment is what makes the distinction
        real rather than cosmetic: `ToolRegistry._resolve` refuses an absolute path, any `..` segment,
        a `~` path, and anything that resolves outside the workspace, so every write a scoped grant
        permits provably lands inside the project.

        Treating *every* `write:` as elevated made a workspace-scoped helper indistinguishable from
        one requesting `deploy:prod` — both scored T3 — which then contradicted
        `goal.auto_hire_max_tier`, whose whole documented job is to cap how risky an auto-created
        helper may be. Since that cap is configurable only up to T2, a T3 classification meant the
        engine's own standard helper could never be admitted at any setting, so the cap could not be
        enforced at all without disabling auto-staffing outright. The two are different risks and the
        tier now says so.
        """
        elevated: list[str] = []
        for capability in capabilities:
            text = str(capability or "")
            if not text.startswith(markers):
                continue
            kind, _, scope = text.partition(":")
            if kind in ("read", "write") and self._scope_is_contained(scope):
                continue
            elevated.append(text)
        return elevated

    @staticmethod
    def _scope_is_contained(scope: str) -> bool:
        """Whether a path scope is bounded to a project subtree rather than everything.

        Contained means the grant names a clean relative subtree (`src`, `src/**`, `src/`). Three
        shapes are deliberately *not* contained, because calling them contained would understate the
        grant: a wildcard or empty scope (`write:*`) is authority over any path the tool layer can
        reach; an absolute path or a `~` path is not a project scope at all; and a `..` segment is a
        traversal. The tool layer refuses the last two outright, so such a grant is inert rather than
        dangerous — but the tier is a statement about what was *asked for*, and asking for a traversal
        deserves the Owner's attention rather than a quiet T0.
        """
        text = str(scope or "").strip()
        stem = text.rstrip("*").rstrip("/")
        if not stem or stem == ".":
            return False
        if stem.startswith(("/", "~")):
            return False
        if ".." in stem.split("/"):
            return False
        return True

    def classify_tier(self, request: Requisition) -> tuple[ApprovalTier, str]:
        """Decide which approval tier a requisition lands in, and why.

        The reasoning is returned so the Owner sees *why* something reached them, and so an
        auto-approval is equally explainable.
        """
        # `system:` belongs in this default, and leaving it out was a real hole: a requisition asking
        # for `system:automation` — running AppleScript on the person's machine — classified **T1**,
        # auto-approved with a notification, because the markers only knew about file and deploy
        # capabilities. An agent could be handed machine access by the delegation desk without anyone
        # deciding it, which is the opposite of what the rest of this design does.
        #
        # Read from `DelegationConfig.elevated_markers()` rather than repeated here, because the two
        # had already drifted: the config carried a `system:`-aware default that nothing consumed, so
        # editing it changed nothing and this literal was the real answer. One definition, one place.
        markers = tuple(self.approval_tiers.get("elevated_capability_markers")
                        or _default_elevated_markers())
        t0_max = int(self.approval_tiers.get("t0_max_tokens", 40_000))
        t1_max = int(self.approval_tiers.get("t1_max_tokens", 80_000))
        t2_max = int(self.approval_tiers.get("t2_max_tokens", 200_000))
        owner_above_usd = float(self.approval_tiers.get("require_owner_above_usd", 0.0))

        elevated = self._elevated_capabilities(request.capabilities, markers)
        if elevated:
            return ApprovalTier.T3, (
                f"requests elevated capabilities {elevated}; these need an explicit "
                "acknowledgment, not an automatic grant"
            )
        if request.kind == "specialist":
            return ApprovalTier.T2, (
                "a permanent specialist adds durable headcount and a standing budget, so the "
                "Owner decides"
            )
        if request.requested_usd > owner_above_usd:
            return ApprovalTier.T2, (
                f"requests ${request.requested_usd:.4f}, above the "
                f"${owner_above_usd:.4f} automatic threshold"
            )
        if request.requested_tokens > t2_max:
            return ApprovalTier.T3, (
                f"requests {request.requested_tokens} tokens, above the {t2_max} explicit-gate "
                "threshold"
            )
        if request.requested_tokens > t1_max:
            return ApprovalTier.T2, (
                f"requests {request.requested_tokens} tokens, above the {t1_max} automatic "
                "threshold"
            )
        if request.requested_tokens > t0_max:
            return ApprovalTier.T1, (
                f"a helper requesting {request.requested_tokens} tokens, within the {t1_max} "
                "limit; auto-approved with notification"
            )
        if any(c.startswith(("read:",)) for c in request.capabilities) or not request.capabilities:
            return ApprovalTier.T0, (
                f"a bounded helper within {t0_max} tokens with read-only or no extra "
                "capabilities; reversible and cheap"
            )
        return ApprovalTier.T1, (
            "a bounded helper within budget; auto-approved with notification"
        )

    def _partition_budget(self, request: Requisition, parent_budget: Budget | None) -> dict[str, Any]:
        """Carve the child's budget out of the parent's remainder (invariant S3).

        This is the invariant most systems get wrong. If spawning *creates* budget, an agent tree
        can spend without bound; if it *partitions*, a run's ceiling holds no matter how the tree
        fans out.
        """
        if parent_budget is None:
            # No parent budget to partition from: the request's own figures stand, but they
            # cannot exceed nothing, so they are clamped to zero rather than invented.
            return {"tokens": 0, "usd": 0.0, "partitioned": False,
                    "note": "no parent budget supplied; the run ceiling is the only bound"}

        available_tokens = parent_budget.remaining_tokens
        available_usd = parent_budget.remaining_usd
        max_tokens = int(available_tokens * self.budget_share_max)
        max_usd = available_usd * self.budget_share_max

        wanted_tokens = request.requested_tokens or max_tokens
        wanted_usd = request.requested_usd or max_usd

        if wanted_tokens > max_tokens or wanted_usd > max_usd:
            raise DelegationError(
                f"requested budget ({wanted_tokens} tokens / ${wanted_usd:.4f}) exceeds the "
                f"share a parent may delegate ({max_tokens} tokens / ${max_usd:.4f}, "
                f"{self.budget_share_max:.0%} of {available_tokens} / ${available_usd:.4f} "
                "remaining). A child is funded from the parent's remainder, never from new "
                "budget.",
                invariant="S3",
                detail={"requested": {"tokens": wanted_tokens, "usd": wanted_usd},
                        "share": {"tokens": max_tokens, "usd": max_usd}},
            )
        return {"tokens": wanted_tokens, "usd": wanted_usd, "partitioned": True,
                "note": f"carved {self.budget_share_max:.0%} maximum from the parent remainder"}

    # ── approval decisions ──────────────────────────────────────────────────

    def approve(self, request: Requisition, *, by: str = "owner",
                amendment: dict[str, Any] | None = None,
                active_chain: Iterable[str] = (), depth: int = 0,
                parent_budget: Budget | None = None) -> DelegationOutcome:
        """Approve a gated requisition and spawn the agent.

        An `amendment` lets the Owner narrow scope before approving — a common and desirable
        outcome, so it is a first-class path rather than a deny-and-resubmit loop.
        """
        if request.state not in (RequisitionState.OWNER_GATE, RequisitionState.REQUESTED):
            if request.state in (RequisitionState.DENIED, RequisitionState.REJECTED_RUNG):
                raise DelegationError(
                    f"requisition {request.id} was {request.state.value} and cannot be approved "
                    "without resubmission"
                )
        if amendment:
            for key, value in amendment.items():
                if hasattr(request, key):
                    setattr(request, key, value)
            request.state = RequisitionState.AMENDED
        problems = request.validate()
        if problems:
            raise DelegationError(
                "cannot approve: the requisition is still incomplete: " + "; ".join(problems)
            )
        request.state = RequisitionState.APPROVED
        request.decided_by = by
        return self._spawn(request, active_chain=list(active_chain), depth=depth,
                           parent_budget=parent_budget)

    def deny(self, request: Requisition, *, by: str = "owner", reason: str) -> DelegationOutcome:
        """Deny a requisition, recording the reason for the requester.

        The reason is mandatory and is fed back to the requesting agent, so it learns which
        requests were unjustified rather than retrying blindly.
        """
        if not (reason or "").strip():
            raise DelegationError(
                "a denial requires a reason; the requester needs to know why so it does not "
                "resubmit the same request"
            )
        request.state = RequisitionState.DENIED
        request.denial_reason = reason
        request.decided_by = by
        return DelegationOutcome(
            approved=False, tier=request.tier or ApprovalTier.T2, reason=reason,
            state=RequisitionState.DENIED, needs_owner=False,
        )

    # ── spawning ────────────────────────────────────────────────────────────

    def _spawn(self, request: Requisition, *, active_chain: list[str], depth: int,
               parent_budget: Budget | None) -> DelegationOutcome:
        """Create the agent from an approved requisition, recording its lineage (S6)."""
        with self._lock:
            child_budget = Budget(
                allocated_usd=request.requested_usd,
                allocated_tokens=request.requested_tokens,
            )
            name = self._unique_name(request)
            spec = AgentSpec(
                id=new_agent_id(),
                name=name,
                title=f"{request.kind.title()} — {request.skill}",
                skills=[request.skill],
                provider=request.provider,
                model=request.model,
                context_window=request.context_window,
                max_output=None,
                level=AgentLevel.PRACTITIONER,
                team=self.org.get(request.requester_id).team if request.requester_id in self.org.agents else "",
                # S6: lineage.
                parent_id=request.requester_id,
                origin="requisition",
                capabilities=list(request.capabilities),
                budget=child_budget,
                tags=[f"tier:{request.tier.value}" if request.tier else "tier:unknown",
                      f"requisition:{request.id}"],
            )
            # S4: least privilege is enforced by carrying only the requested set, never the
            # parent's. An empty set means no extra authority, which is the safe default.
            try:
                self.org.hire(spec)
            except OrgError as exc:
                raise DelegationError(f"could not hire the requested agent: {exc}") from exc
            parent = self.org.agents.get(request.requester_id)
            if parent is not None:
                # Setting reports_to is what makes span_of_control count this child; a parallel
                # counter would drift out of step with the roster.
                spec.reports_to = parent.id
                # The chain is threaded onto the child's runtime so its own delegations can
                # detect a cycle.
                self.org.runtime(spec.id).chain = [*active_chain, request.requester_id]
            self._spawned[spec.id] = spec
            request.state = RequisitionState.ACTIVE
            return DelegationOutcome(
                approved=True,
                tier=request.tier or ApprovalTier.T0,
                reason=(
                    f"spawned {name} ({spec.id}) as a {request.kind} for skill "
                    f"{request.skill}; lineage parent={request.requester_id}"
                ),
                state=RequisitionState.ACTIVE,
                agent_id=spec.id,
            )

    def _unique_name(self, request: Requisition) -> str:
        """A display name that does not collide with an existing agent.

        Names must be unique because the roster and every log line would otherwise be ambiguous,
        so a suffix is added rather than refusing the hire.
        """
        base = request.skill.replace("-", " ").title().split()[0] or "Helper"
        existing = {a.name.lower() for a in self.org.agents.values()}
        if base.lower() not in existing:
            return base
        for index in range(2, 100):
            candidate = f"{base} {index}"
            if candidate.lower() not in existing:
                return candidate
        return f"{base} {os.urandom(2).hex()}"

    # ── lifecycle ───────────────────────────────────────────────────────────

    def destroy(self, agent_id: str, *, reason: str = "scope ended") -> AgentSpec | None:
        """Destroy a helper once its scope ends.

        Returns None when the agent is already gone, so a retried cleanup is harmless. A
        specialist is refused: it is durable by definition and must be retired deliberately.
        """
        with self._lock:
            spec = self.org.agents.get(agent_id)
            if spec is None:
                return None
            if spec.origin == "requisition" and "specialist" in (spec.title or "").lower():
                raise DelegationError(
                    f"{spec.name!r} is a permanent specialist; retire it deliberately rather "
                    "than destroying it with a task scope",
                )
            parent_id = spec.parent_id
            try:
                removed = self.org.terminate(agent_id, reason=reason)
            except OrgError as exc:
                raise DelegationError(f"could not destroy {entity_label(spec)}: {exc}") from exc
            self._spawned.pop(agent_id, None)
            return removed

    def retirement_review(self, *, idle_runs: int = 20) -> list[dict[str, Any]]:
        """Specialists that look unused and should be reviewed for retirement.

        Every extension needs a removal path; without this an experiment becomes permanent
        infrastructure simply because nobody remembered to look.
        """
        out: list[dict[str, Any]] = []
        for spec in self.org.agents.values():
            if not spec.is_ai or spec.role == "owner":
                continue
            runtime = self.org.runtime(spec.id)
            if runtime.tasks_completed == 0 and runtime.turns == 0 and spec.origin == "requisition":
                out.append({
                    "agent_id": spec.id, "name": spec.name, "skill": spec.skills[0] if spec.skills else "",
                    "reason": "spawned by requisition and has done no work",
                    "recommendation": "retire, or assign work",
                })
        return out

    def anti_sprawl(self, *, window_runs: int = 5, growth_threshold: float = 0.20) -> dict[str, Any]:
        """Report token-per-completed-task per agent — the library's anti-leak metric.

        Growth above the threshold without a topology change indicates a delegation leak or
        context bloat, which is the signal the design uses to suspect sprawl.
        """
        report: dict[str, Any] = {}
        for spec in self.org.agents.values():
            if not spec.is_ai:
                continue
            runtime = self.org.runtime(spec.id)
            completed = runtime.tasks_completed
            if completed == 0:
                continue
            per_task = spec.budget.spent_tokens / completed
            report[spec.id] = {
                "name": spec.name,
                "tokens_per_completed_task": round(per_task, 1),
                "tasks_completed": completed,
                "tokens_spent": spec.budget.spent_tokens,
                "suspect_sprawl": (
                    spec.budget.spent_tokens > 0
                    and per_task > (5_000 * (1 + growth_threshold))
                ),
            }
        suspects = [entry["name"] for entry in report.values() if entry["suspect_sprawl"]]
        return {"agents": report, "suspects": suspects,
                "window_runs": window_runs, "threshold": growth_threshold}

    def lineage(self, agent_id: str) -> dict[str, Any]:
        """The full delegation path for an agent: who spawned it and through whom."""
        spec = self.org.agents.get(agent_id) or self._spawned.get(agent_id)
        if spec is None:
            raise DelegationError(f"unknown agent {agent_id!r}")
        ancestors: list[dict[str, Any]] = []
        current = spec.parent_id
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            parent = self.org.agents.get(current)
            if parent is None:
                ancestors.append({"agent_id": current, "name": "(departed)"})
                break
            ancestors.append({"agent_id": parent.id, "name": parent.name,
                              "title": parent.title, "origin": parent.origin})
            current = parent.parent_id
        return {
            "agent": {"agent_id": spec.id, "name": spec.name, "origin": spec.origin,
                      "tags": list(spec.tags)},
            "depth": len(ancestors),
            "ancestors": ancestors,
            "chain": list(self.org.runtime(spec.id).chain),
        }

    def stats(self) -> dict[str, Any]:
        """Counts for the hiring desk view."""
        requisitions = {s: 0 for s in RequisitionState}
        return {
            "spawned_by_requisition": len(self._spawned),
            "max_depth": self.max_depth,
            "span_of_control": self.span_of_control,
            "allow_ephemeral": self.allow_ephemeral,
            "budget_share_max": self.budget_share_max,
            "invariants": dict(S_INVARIANTS),
        }


def entity_label(spec: AgentSpec) -> str:
    """A display label for an agent, used in error messages."""
    return f"{spec.name} ({spec.id})" if spec.name else spec.id


def _iso_now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
