#!/usr/bin/env python3
"""router.py — decide who does the next thing, and know why.

WHY THIS EXISTS
---------------
The orchestrator reaches a decision point constantly: this node is done, who gets the work? A
naive answer routes to the only agent holding the skill. A better one scores the candidates and
— crucially — knows when it does not know, so it can propose instead of guessing.

The distinguishing feature is not the scoring; it is the **confidence gate**. Below the
threshold the router does not act, it presents ranked candidates and waits. The library's own
rule is "no match → escalate to human with suggested route", and this is where that lives.

DESIGN
------
- **Hard filters before scoring.** Contract compatibility, availability, budget and
  independence are pass/fail. Scoring a candidate that cannot legally take the work wastes the
  score and hides the real reason.
- **Every decision is explainable.** The result carries the candidates, their scores, their
  rejected reasons, the threshold, and which policy layer decided.
- **`auto` and `confirm` emit the same record.** Only the waiting differs, which is what makes
  "why did it route there?" answerable in both cases.
- **A proposal is a real result, not a failure.** `RouteDecision.proposed` is a normal outcome;
  only a genuinely empty candidate set is an error.

Usage:
    router = Router(org, policy=resolver, threshold=0.62, margin=0.15)
    decision = router.route(context)
    if decision.proposed:
        ...  # surface the ranked candidates and wait
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .agent import AgentSpec
from .binding import BindingError, BindingPolicy, Binder
from .policy import Autonomy, PolicyResolver, RouteClass
from .roster import Org

__all__ = ["Candidate", "RouteContext", "RouteDecision", "Router", "RouterError"]


class RouterError(RuntimeError):
    """Raised when no candidate can take the work at all — the `R-MATCH-FAIL` case."""


@dataclass
class RouteContext:
    """What the router knows when it decides.

    Carrying the produced artifact types is what lets the contract filter run: a candidate is
    only eligible if the work it would receive matches what it declares it consumes.
    """

    node_id: str
    skill: str
    # Artifact types available to hand over.
    artifacts: list[str] = field(default_factory=list)
    # The agent that produced the artifact, excluded when the target is a reviewer.
    producer_id: str | None = None
    # Whether the target is a verification role, which forces independence.
    is_reviewer: bool = False
    # Remaining budget for this run, in USD. A candidate with no budget cannot be chosen.
    budget_remaining_usd: float | None = None
    # The route class, which selects the policy that governs whether to act or propose.
    route_class: RouteClass = RouteClass.CONTRACT
    # Preferences.
    prefer_provider: str | None = None
    prefer_model: str | None = None
    require_level: int | None = None
    # Scopes for policy resolution.
    team: str = ""
    run_id: str = ""
    task_id: str = ""
    # How deep the delegation chain already is, for the depth cap.
    depth: int = 0


@dataclass
class Candidate:
    """One possible recipient, with its score and the reason it was kept or dropped."""

    agent_id: str
    name: str
    skill: str
    score: float = 0.0
    eligible: bool = True
    rejected_reason: str = ""
    components: dict[str, float] = field(default_factory=dict)
    model: str = ""
    provider: str = ""
    level: int = 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "skill": self.skill,
            "score": round(self.score, 4),
            "eligible": self.eligible,
            "rejected_reason": self.rejected_reason,
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "model": self.model,
            "provider": self.provider,
            "level": self.level,
        }


@dataclass
class RouteDecision:
    """The router's verdict: either a chosen agent, a proposal, or a hard failure.

    `proposed=True` means the confidence gate was not cleared. That is a *normal* outcome — the
    design prefers a proposal to a confident guess — so it is a field rather than an exception.
    """

    context: RouteContext
    chosen: str | None = None
    policy: BindingPolicy = BindingPolicy.LOAD_BALANCED
    autonomy: Autonomy = Autonomy.CONFIRM
    route_class: RouteClass = RouteClass.CONTRACT
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    threshold: float = 0.0
    margin: float = 0.0
    proposed: bool = False
    # Which policy layer decided the autonomy level.
    decided_by: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        """True when an agent was chosen."""
        return self.chosen is not None and not self.proposed

    def ranked(self, limit: int = 5) -> list[Candidate]:
        """Eligible candidates, best first — what a proposal shows the Owner."""
        return [c for c in self.candidates if c.eligible][:limit]

    def as_dict(self) -> dict[str, Any]:
        """The record for `route.decided`/`route.proposed`. Full detail, so it is auditable."""
        return {
            "node_id": self.context.node_id,
            "skill": self.context.skill,
            "route_class": self.route_class.value,
            "autonomy": self.autonomy.value,
            "decided_by": self.decided_by,
            "chosen": self.chosen,
            "policy": self.policy.value,
            "proposed": self.proposed,
            "reason": self.reason,
            "threshold": self.threshold,
            "margin": self.margin,
            "candidates": [c.as_dict() for c in self.ranked(10)],
            "rejected": [c.as_dict() for c in self.rejected[:10]],
        }

    def summary(self) -> str:
        """A readable one-liner for the terminal and the route trace."""
        if self.proposed:
            names = ", ".join(f"{c.name} ({c.score:.2f})" for c in self.ranked(3))
            return (
                f"{self.context.node_id}: proposed because confidence did not clear "
                f"{self.threshold:.2f} — waiting on the Owner. Candidates: {names or '(none)'}"
            )
        if self.chosen is None:
            return f"{self.context.node_id}: no candidate could take the work"
        return (
            f"{self.context.node_id}: routed to {self.chosen_id_name()} "
            f"(autonomy {self.autonomy.value} at layer {self.decided_by!r})"
        )

    def chosen_id_name(self) -> str:
        """The chosen agent's name, for display, falling back to its id."""
        for candidate in self.candidates:
            if candidate.agent_id == self.chosen:
                return f"{candidate.name} ({candidate.agent_id})"
        return self.chosen or "?"


#: Verification roles, which force the independence filter.
_REVIEWER_SKILLS: frozenset[str] = frozenset({
    "code-reviewer", "security-reviewer", "qa-engineer", "accessibility-auditor",
    "performance-engineer", "contract-completeness-review", "security-reviewer",
})

#: Scoring weights. They sum to 1.0 so a score is directly comparable to the threshold, which
#: is what makes "0.62" a meaningful number rather than an arbitrary cut.
_WEIGHTS: dict[str, float] = {
    "contract": 0.30,   # do the declarations line up
    "capability": 0.25, # level and skills
    "availability": 0.20,  # is it free
    "economics": 0.15,  # relative cost
    "affinity": 0.10,   # provider/model preference and team match
}


@dataclass
class Router:
    """Scores candidates and decides whether to act or propose.

    Parameters
    ----------
    org:
        The roster, for candidates and their live availability.
    policy:
        Resolves the route class to an autonomy level, with the safety floor applied.
    threshold:
        Minimum top score to act. Below it, the router proposes.
    margin:
        How far the top candidate must lead the runner-up. A near-tie is exactly when a
        confident automatic choice is least justified, so a narrow margin also proposes.
    """

    org: Org
    policy: PolicyResolver = field(default_factory=PolicyResolver)
    threshold: float = 0.62
    margin: float = 0.15
    binder: Binder | None = None

    def __post_init__(self) -> None:
        if not (0.0 <= self.threshold <= 1.0):
            raise RouterError(f"router threshold must be in [0, 1]; got {self.threshold}")
        if not (0.0 <= self.margin <= 1.0):
            raise RouterError(f"router margin must be in [0, 1]; got {self.margin}")
        if self.binder is None:
            self.binder = Binder(self.org)

    # ── the decision ────────────────────────────────────────────────────────

    def route(self, context: RouteContext, *,
              max_depth: int = 3) -> RouteDecision:
        """Decide who takes the work.

        Raises
        ------
        RouterError
            Only when there is no candidate at all. A low-confidence situation yields a
            proposal instead — the design prefers asking to guessing.
        """
        resolution = self.policy.resolve(
            context.route_class, agent_id="", team=context.team,
            run_id=context.run_id, task_id=context.task_id,
        )
        decision = RouteDecision(
            context=context,
            route_class=context.route_class,
            autonomy=resolution.level,
            decided_by=resolution.layer,
            threshold=self.threshold,
            margin=self.margin,
        )

        holders = self.org.candidates_for(context.skill, available_only=True)
        if not holders:
            raise RouterError(
                f"no agent holds {context.skill!r} for node {context.node_id!r}. "
                f"The roster has: {', '.join(self.org.skills_present()) or '(no skills)'}"
            )

        # Hard filters first: a candidate that cannot legally take the work is not scored.
        candidates: list[Candidate] = []
        for spec in holders:
            candidate = self._score(spec, context, max_depth=max_depth)
            if candidate.eligible:
                candidates.append(candidate)
            else:
                decision.rejected.append(candidate)

        if not candidates:
            reasons = "; ".join(f"{c.name}: {c.rejected_reason}" for c in decision.rejected[:4])
            raise RouterError(
                f"every agent holding {context.skill!r} was filtered out for node "
                f"{context.node_id!r}: {reasons}"
            )

        candidates.sort(key=lambda c: (-c.score, c.name))
        decision.candidates = candidates

        top = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None
        gap = top.score - (runner_up.score if runner_up else 0.0)
        confident = top.score >= self.threshold and (runner_up is None or gap >= self.margin)

        # `manual` never acts; `confirm` acts only with a confident match; `auto`/`notify` act
        # when confident and otherwise propose.
        if resolution.level is Autonomy.MANUAL:
            decision.proposed = True
            decision.reason = (
                f"policy at layer {resolution.layer!r} is 'manual', so the Owner must initiate"
            )
            return decision

        if resolution.level is Autonomy.CONFIRM or not confident:
            decision.proposed = True
            if resolution.level is Autonomy.CONFIRM:
                decision.reason = (
                    f"policy at layer {resolution.layer!r} is 'confirm'; proposing with "
                    f"{len(candidates)} ranked candidate(s)"
                )
            else:
                decision.reason = (
                    f"confidence {top.score:.2f} did not clear threshold {self.threshold:.2f}"
                    + (f" with margin {gap:.2f} < {self.margin:.2f}" if runner_up else "")
                )
            return decision

        decision.chosen = top.agent_id
        decision.policy = BindingPolicy.PINNED
        decision.reason = (
            f"{top.name} scored {top.score:.2f} (margin {gap:.2f}); autonomy "
            f"{resolution.level.value} at layer {resolution.layer!r}"
        )
        return decision

    # ── scoring ─────────────────────────────────────────────────────────────

    def _score(self, spec: AgentSpec, context: RouteContext, *, max_depth: int) -> Candidate:
        """Score one candidate, or mark it ineligible with the reason.

        Filters run first and are absolute. A candidate excluded for independence is not given a
        low score — it is given a *reason*, because "excluded because it produced the artifact"
        is actionable information and a low score is not.
        """
        candidate = Candidate(
            agent_id=spec.id, name=spec.name, skill=context.skill,
            model=spec.model, provider=spec.provider, level=int(spec.level),
        )

        # ── hard filters ──
        if context.is_reviewer or context.skill in _REVIEWER_SKILLS:
            if context.producer_id and spec.id == context.producer_id:
                candidate.eligible = False
                candidate.rejected_reason = (
                    "it produced the artifact under review; "
                    "verification-independence-engineer forbids self-review"
                )
                return candidate

        if context.require_level is not None and int(spec.level) < context.require_level:
            candidate.eligible = False
            candidate.rejected_reason = (
                f"level {spec.level.label} is below the required L{context.require_level}"
            )
            return candidate

        if context.budget_remaining_usd is not None and spec.budget.exhausted:
            candidate.eligible = False
            candidate.rejected_reason = "its allocated budget is exhausted"
            return candidate

        runtime = self.org.runtime(spec.id)
        if runtime.state.value in ("quarantined", "terminated"):
            candidate.eligible = False
            candidate.rejected_reason = f"it is {runtime.state.value}"
            return candidate

        if max_depth and context.depth >= max_depth and spec.parent_id:
            candidate.eligible = False
            candidate.rejected_reason = (
                f"delegation depth {context.depth} has reached the cap {max_depth}"
            )
            return candidate

        # ── contract compatibility ──
        # A candidate that declares inputs gets credit when the available artifacts satisfy
        # them. Declarations describe typical use, so a mismatch reduces the score rather than
        # disqualifying — matching the planner's reading of the same field.
        declared_inputs = set(self._declared_inputs(spec, context.skill))
        available = set(context.artifacts)
        if not declared_inputs:
            contract_score = 0.7  # a generic consumer accepts anything
        elif declared_inputs & available:
            contract_score = 1.0
        else:
            contract_score = 0.25

        # ── capability ──
        level_score = min(1.0, int(spec.level) / 5.0)
        skill_score = 1.0 if spec.has_skill(context.skill) else 0.0
        capability_score = 0.6 * level_score + 0.4 * skill_score

        # ── availability ──
        availability_score = 1.0 if runtime.available() else 0.35
        # Penalise an agent already carrying reports, so a supervisor is not also the worker.
        reports = self.org.span_of_control(spec.id)
        if reports:
            availability_score = max(0.1, availability_score - 0.1 * min(reports, 4))

        # ── economics ──
        # Local models cost nothing, so they score highest on cost without being penalised for
        # it; a cloud model with no remaining budget scores lowest.
        if spec.provider and spec.provider.lower() in ("ollama", "lmstudio"):
            economics_score = 1.0
        elif context.budget_remaining_usd is None:
            economics_score = 0.6
        else:
            spent = spec.budget.spent_usd
            economics_score = max(0.1, 1.0 - min(1.0, spent / max(0.01, spent + spec.budget.remaining_usd)))

        # ── affinity ──
        affinity_score = 0.5
        if context.prefer_provider and spec.provider == context.prefer_provider:
            affinity_score += 0.25
        if context.prefer_model and spec.model == context.prefer_model:
            affinity_score += 0.25
        if context.team and spec.team == context.team:
            affinity_score += 0.15
        affinity_score = min(1.0, affinity_score)

        candidate.components = {
            "contract": contract_score,
            "capability": capability_score,
            "availability": availability_score,
            "economics": economics_score,
            "affinity": affinity_score,
        }
        candidate.score = sum(_WEIGHTS[name] * value for name, value in candidate.components.items())
        return candidate

    def _declared_inputs(self, spec: AgentSpec, skill: str) -> list[str]:
        """The inputs this agent's *skill* declares, read from the bundle when available.

        Read from the skill rather than the agent because the contract belongs to the capability,
        not the employee. A missing bundle yields an empty list, which the scorer treats as a
        generic consumer rather than as a failure.
        """
        loader = getattr(self, "_bundle_loader", None)
        if loader is None:
            return []
        try:
            bundle = loader(skill)
        except Exception:  # noqa: BLE001 - a missing contract degrades the score, not the route
            return []
        if bundle is None:
            return []
        return list(getattr(bundle.contract, "inputs", ()) or [])

    def attach_skills(self, source: Any) -> "Router":
        """Give the router a skill source so contract compatibility can be scored.

        Optional: without it the contract component falls back to "generic consumer", which
        keeps the router usable in tests and in a context where skills are not loaded.
        """
        self._bundle_loader = source.load  # type: ignore[attr-defined]
        return self

    # ── reporting ───────────────────────────────────────────────────────────

    def policy_matrix(self, *, team: str = "", run_id: str = "") -> dict[str, Any]:
        """Every route class's effective autonomy, for the routing view."""
        return self.policy.effective(team=team, run_id=run_id)
