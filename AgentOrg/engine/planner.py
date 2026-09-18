#!/usr/bin/env python3
"""planner.py — turn a goal into a validated workflow manifest for the Owner to approve.

WHY THIS EXISTS
---------------
The Owner states an outcome ("a booking SaaS MVP with auth and payments"); the org needs a
graph. Authoring that graph by hand is exactly the work a planner agent should do, but an
agent-authored graph is also the most dangerous artifact in the system: an unreachable end
node means a run that never finishes, an unbounded loop means one that never stops, and a
missing gate means work that ships without review.

So the planner does not merely generate YAML. It **generates, validates, and refuses**. Every
candidate manifest is checked against the library's own `workflow-runner` semantics before
the Owner ever sees it, and the planner falls back to a known-good skeleton rather than
emitting a graph that cannot run.

DESIGN
------
- **Deterministic composition, not free generation.** The phases come from the skills' own
  `workflow.artifacts` contracts, so the graph is derived from what the library declares
  rather than invented. An LLM may later refine the plan; the *structure* is generated here
  where it can be validated.
- **Validation is a hard gate.** A candidate that fails `validate_manifest` is never
  returned. The planner tries a richer shape, then a leaner one, then a minimal skeleton —
  and a skeleton that still fails is a bug in this module, reported as such.
- **The graph always terminates.** Every generated manifest contains at least one bounded
  loop with an `exit_when` and `max_iterations`, and a reachable terminal human gate. Those
  are the properties the design's termination section promises, so they are asserted rather
  than hoped for.
- **Handoffs are typed.** Each edge connects a producer whose declared outputs satisfy the
  consumer's declared inputs, or the planner says so and drops the edge.

Usage:
    planner = Planner(source)
    plan = planner.plan(goal="build a booking API", slug="booking")
    plan.manifest           # the YAML-equivalent dict
    plan.summary()          # human-readable, for the approval prompt
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .skills.bundle import SkillBundle, SkillError

__all__ = ["Plan", "Planner", "PlanError", "PlanValidation"]


class PlanError(RuntimeError):
    """Raised when no valid plan can be produced for a goal."""


# The default company, in pipeline order. Each entry is (node id, skill name, phase label).
#
# This is a *composition template*, not a hardcoded pipeline: the planner verifies each
# skill's declared contract before including it, and a skill whose contract is missing is
# dropped with a reason rather than producing an edge that cannot type-check.
#
# It is the **software** shape. A goal about strategy, market research or go-to-market gets a
# different build chain (see `_DOMAIN_SHAPES`) because the software pipeline is simply the wrong
# org for that work — a CEO-and-market-researcher goal run through product-manager/architect/
# backend-developer does not "improve the project", it produces a spurious API spec.
_DEFAULT_SHAPE: tuple[tuple[str, str, str], ...] = (
    ("pm", "product-manager", "DISCOVER"),
    ("architect", "system-architect", "DESIGN"),
    ("api", "api-designer", "DESIGN"),
    ("developer", "backend-developer", "BUILD"),
)

# The software domain's verifiers, kept separate from the build chain for the same reason every other
# domain keeps them separate: the rework loop hands findings back to the chain's *producer*, and a
# build chain that also contained the reviewers made that resolve to a reviewer (the last entry),
# which is not a node that can fix anything.
_SOFTWARE_VERIFIERS: tuple[tuple[str, str, str], ...] = (
    ("reviewer", "code-reviewer", "REVIEW"),
    ("qa", "qa-engineer", "VERIFY"),
    ("security", "security-reviewer", "VERIFY"),
)

# Goals that are *not* primarily a software build. Each domain selects a build chain and a
# verification set appropriate to the work, because the same seven software roles are the wrong
# answer for a business or research goal.
#
# Intent domains, tested before `software`. A *stated intent* ("strategy", "go-to-market",
# "research") is a stronger signal than a technical noun, so these run first; `software` is only
# reached when no intent matched. Order matters: research before gtm, because "market research"
# names a research activity while "market" alone could suggest either.
_DOMAIN_INTENT_ORDER: tuple[str, ...] = ("strategy", "research", "gtm", "data")

# Each domain: a build chain (node id, skill, phase) and a verifier set (node id, skill, phase).
# The verifiers are the ones whose declared contract can consume what the chain produces, so a
# research deliverable is checked by a research verifier rather than by `code-reviewer`.
_DOMAIN_SHAPES: dict[str, dict[str, tuple[tuple[str, str, str], ...]]] = {
    "software": {
        "build": _DEFAULT_SHAPE,
        "verify": _SOFTWARE_VERIFIERS,
    },
    "strategy": {
        # Company/business strategy: frame the decision, ground it in market evidence, model the
        # numbers, then have it independently challenged. `ceo-strategist` frames; `business-strategist`
        # produces the go-to-market and unit economics; `fp-and-a-analyst` models the financing.
        "build": (
            ("ceo", "ceo-strategist", "DISCOVER"),
            ("bizstrat", "business-strategist", "DESIGN"),
            ("fpa", "fp-and-a-analyst", "DESIGN"),
        ),
        "verify": (
            ("bizdev", "bizdev-manager", "REVIEW"),
            ("critic", "critical-thinker", "REVIEW"),
        ),
    },
    "gtm": {
        # Go-to-market: position the product, engineer growth, and produce the collateral.
        "build": (
            ("pm", "product-manager", "DISCOVER"),
            ("pmm", "marketing-manager", "DESIGN"),
            ("growth", "growth-engineer", "BUILD"),
            ("content", "content-strategist", "BUILD"),
        ),
        "verify": (
            ("analyst", "product-analyst", "REVIEW"),
            ("critic", "critical-thinker", "VERIFY"),
        ),
    },
    "research": {
        # Discovery research: scope the questions, research users, synthesise, and write it up.
        "build": (
            ("pm", "product-manager", "DISCOVER"),
            ("uxr", "ux-researcher", "DISCOVER"),
            ("bi", "business-intelligence-engineer", "DESIGN"),
        ),
        "verify": (
            ("analyst", "product-analyst", "REVIEW"),
            ("critic", "critical-thinker", "VERIFY"),
        ),
    },
    "data": {
        # Data/analytics work: an engineering chain, but verified by analytics rather than QA.
        "build": (
            ("pm", "product-manager", "DISCOVER"),
            ("architect", "system-architect", "DESIGN"),
            ("engineer", "data-engineer", "BUILD"),
        ),
        "verify": (
            ("analyst", "product-analyst", "REVIEW"),
            ("critic", "critical-thinker", "VERIFY"),
        ),
    },
}

# How a goal is classified into a *non-software* domain. Each domain lists patterns that state
# intent — a domain word like "strategy", "go-to-market" or "research". There is deliberately no
# `software` entry: `software` is the fallback when no intent matches, so a pattern for it would be
# unreachable. That is also the correct behaviour — a technical noun alone ("api", "database") should
# not decide the org, and a goal with no stated intent is most often an engineering build.
_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "strategy": (
        r"\b(ceo|founder|board|investor|fundrais\w*|cap table|dilution)\b",
        r"\b(strategy|strategic|business model|business plan|unit economics|"
        r"pricing strategy|valuation|market entry|expansion)\b",
        r"\b(raise|raising)\b.*\b(capital|funding|round|seed|series [a-c])\b",
        r"\b(competitiv\w+ (analysis|positioning)|porters?\b)",
    ),
    "gtm": (
        r"\b(go[- ]to[- ]market|gtm|launch plan|positioning|messaging|"
        r"battle card|sales enablement|demand gen\w*|lead gen\w*)\b",
        r"\b(marketing|promotion|brand|campaign|content plan|seo|funnel|"
        r"conversion|acquisition|retention)\b",
        r"\b(capture|grow|expand|acquire)\b.*\b(market|users?|customers?|demand)\b",
    ),
    "research": (
        r"\b(research|user research|market research|survey|interview|persona|"
        r"journey map|usability|discovery|competitive analysis|landscape|"
        r"feasibility|due diligence)\b",
        r"\b(who are|understand)\b.*\b(users?|customers?|market)\b",
    ),
    "data": (
        r"\b(analytics|dashboard|metric|kpi|reporting|data warehouse|etl|elt|"
        r"data pipeline|data model|bi\b|business intelligence|telemetry|instrumentation)\b",
    ),
}

# A domain that the goal names outright ("use the CEO skill", "do market research") wins over the
# keyword vote, because naming a capability is an instruction rather than a hint.
_DOMAIN_NAMES: dict[str, str] = {
    "ceo": "strategy",
    "ceo-strategist": "strategy",
    "business-strategist": "strategy",
    "business strategy": "strategy",
    "market researcher": "research",
    "market research": "research",
    "ux researcher": "research",
    "user research": "research",
    "go-to-market": "gtm",
    "gtm": "gtm",
    "marketing": "gtm",
    "growth": "gtm",
    "data engineering": "data",
    "analytics": "data",
}

# Goal keywords that select specialist skills in addition to a chosen shape. Each entry
# is (pattern, skill name). Ordered so the first match wins per skill.
#
# These are technical specialists, so they only extend a chain — they never replace the domain's
# own roles. A strategy goal that also says "dashboard" still gets its strategists.
_GOAL_HINTS: tuple[tuple[str, str], ...] = (
    (r"\b(api|rest|graphql|grpc|endpoint|openapi)\b", "api-designer"),
    (r"\b(ui|frontend|react|swift|screen|dashboard|interface)\b", "frontend-developer"),
    (r"\b(macos|swift|appkit|swiftui|ios)\b", "macos-developer"),
    (r"\b(kubernetes|docker|deploy|infra|terraform|pipeline|ci/?cd)\b", "devops-engineer"),
    (r"\b(auth|login|password|oauth|permission|rbac|token)\b", "security-engineer"),
    (r"\b(database|schema|migration|sql|postgres|sqlite)\b", "database-designer"),
    (r"\b(etl|elt|warehouse|lakehouse|medallion)\b", "data-engineer"),
    (r"\b(mobile|android|flutter|react native)\b", "mobile-developer"),
    (r"\b(payment|billing|subscription|stripe|pricing)\b", "fintech-app-developer"),
    (r"\b(docs|documentation|readme|runbook|api reference|adr)\b", "technical-writer"),
    (r"\b(budget|financial model|projection|forecast|arr|nrr|ltv|cac)\b", "fp-and-a-analyst"),
    (r"\b(partner|partnership|channel|reseller|alliance)\b", "bizdev-manager"),
    (r"\b(fundrais\w*|raise capital|pitch deck|data room|cap table|dilution)\b",
     "investor-relations"),
)

# Roles a goal may *name outright* ("bring a market researcher"). A named person is an instruction,
# so it is added as a specialist even when the domain's own chain would not have included them —
# which is what makes "use the CEO skill and bring a market researcher" produce both, rather than
# silently picking one. Ordered so the first phrase match wins per skill.
_ROLE_HINTS: tuple[tuple[str, str], ...] = (
    (r"\b(market researcher|market research|user research|ux researcher|personas?|"
     r"journey maps?|user interviews?|usability)\b", "ux-researcher"),
    (r"\b(ceo|chief executive)\b", "ceo-strategist"),
    (r"\b(business strategist|go[- ]to[- ]market|business model|unit economics)\b",
     "business-strategist"),
    (r"\b(product strategist|product-market fit|roadmap)\b", "product-strategist"),
    (r"\b(product marketing|positioning|battle cards?|sales enablement)\b",
     "product-marketing-manager"),
    (r"\b(marketing manager|campaigns?|brand)\b", "marketing-manager"),
    (r"\b(growth engineer|funnel|a/b test\w*|conversion rate|referral|activation)\b",
     "growth-engineer"),
    (r"\b(content strategist|editorial|content plan|seo)\b", "content-strategist"),
    (r"\b(financial analyst|financial model|p&l|board financials|saas metrics)\b",
     "fp-and-a-analyst"),
    (r"\b(business intelligence|bi engineer|semantic layer|dashboards?)\b",
     "business-intelligence-engineer"),
    (r"\b(product analyst|product metrics|kpis?|cohort|retention analysis)\b",
     "product-analyst"),
    (r"\b(project manager|project plan|raid log|wbs|gantt|milestones)\b", "project-manager"),
    (r"\b(bizdev|business development|strategic partners?)\b", "bizdev-manager"),
    (r"\b(technical writer|api reference|runbook|adrs?)\b", "technical-writer"),
)

# The handoff payload every edge carries. Matches the library's registry name.
_PAYLOAD = "handoff-v1"
_PAYLOAD_FIELDS = (
    "status", "summary", "artifacts", "decisions", "open_questions",
    "verification_evidence", "context", "budget", "next",
)


@dataclass
class PlanValidation:
    """The validator's verdict on a candidate manifest."""

    valid: bool
    errors: tuple[str, ...] = ()
    name: str = ""
    manifest_sha: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "errors": list(self.errors),
                "name": self.name, "manifest_sha": self.manifest_sha}


@dataclass
class Plan:
    """A proposed manifest plus what the Owner needs to judge it."""

    goal: str
    slug: str
    manifest: dict[str, Any]
    validation: PlanValidation
    skills_used: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    #: Which composition shape the goal selected — `software`, `strategy`, `research`, `gtm`.
    #: Surfaced because "why this org?" is the first question an Owner asks of a plan, and the
    #: answer must not be something they have to infer from the node list.
    shape: str = "software"
    #: Skills the plan needs that the roster does not staff, when a roster was supplied.
    #: Each entry names the skill, why it is a gap, and the exact hire that closes it.
    staffing: tuple[dict[str, Any], ...] = ()
    #: The library's own view of this plan: how well its skills hang together and what several of
    #: them declare they need that the plan left out (`SkillGraph.plan_review`). Populated when a
    #: skill graph is available; empty when it is not, so a plan still renders without the library.
    graph_review: dict[str, Any] = field(default_factory=dict)

    @property
    def nodes(self) -> list[dict[str, Any]]:
        """The manifest's node list."""
        return list(self.manifest.get("nodes") or [])

    @property
    def loops(self) -> list[dict[str, Any]]:
        """The manifest's loop list."""
        return list(self.manifest.get("loops") or [])

    @property
    def gates(self) -> list[dict[str, Any]]:
        """The manifest's gate list."""
        return list(self.manifest.get("gates") or [])

    def node_ids(self) -> list[str]:
        """Every node id, in order."""
        return [str(node.get("id")) for node in self.nodes]

    def summary(self) -> str:
        """A human-readable description for the approval prompt.

        Written for the Owner, so it names the sequence, the loops, the gates and anything
        that was dropped — the four things that decide whether to approve.
        """
        lines = [
            f"Goal: {self.goal}",
            f"Workflow: {self.manifest.get('name')}  (validated: "
            f"{'yes' if self.validation.valid else 'NO'})",
            f"Shape: {self.shape}"
            + ("  (the goal selected a non-engineering org)"
               if self.shape != "software" else ""),
            "",
            "Sequence:",
        ]
        for node in self.nodes:
            kind = node.get("type", "skill")
            detail = node.get("skill") or node.get("kind") or kind
            outputs = ", ".join(node.get("outputs") or [])
            suffix = f" -> [{outputs}]" if outputs else ""
            lines.append(f"  {node.get('id')}  ({kind}: {detail}){suffix}")
        if self.loops:
            lines.append("")
            lines.append("Loops (bounded, with an exit condition and escalation):")
            for loop in self.loops:
                lines.append(
                    f"  {loop.get('id')}: {' -> '.join(loop.get('nodes') or [])}"
                    f"  exit when {loop.get('exit_when')}"
                    f"  max {loop.get('max_iterations')} iterations"
                    f"  escalate to {loop.get('escalate_to')}"
                )
        if self.gates:
            lines.append("")
            lines.append("Gates:")
            for gate in self.gates:
                lines.append(f"  {gate.get('id')}  kind={gate.get('kind')}  "
                             f"{gate.get('description', '')}")
        if self.dropped:
            lines.append("")
            lines.append("Omitted (with reason):")
            for entry in self.dropped:
                lines.append(f"  - {entry}")
        if self.staffing:
            lines.append("")
            lines.append("Staffing gaps — nobody in the roster holds these capabilities:")
            for gap in self.staffing:
                lines.append(f"  {gap['node_id']:16s} {gap['skill']:28s} {gap['reason']}")
                if gap.get("hire"):
                    lines.append(f"  {'':16s} close it: {gap['hire']}")
        review = self.graph_review or {}
        isolated = review.get("isolated") or []
        consensus = review.get("consensus_missing") or []
        if isolated:
            lines.append("")
            lines.append("Unrelated skills — the library's graph says these belong in another run:")
            for name in isolated:
                lines.append(f"  - {name}")
        if consensus:
            lines.append("")
            lines.append("Consensus prerequisites — several of these skills declare they need:")
            for entry in consensus[:8]:
                lines.append(f"  - {entry['skill']:32s} demanded by {entry['demanded_by']} skills")
        if self.notes:
            lines.append("")
            lines.append("Notes:")
            for note in self.notes:
                lines.append(f"  - {note}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        """Serialisation for the `manifest.proposed` event and for persistence."""
        return {
            "goal": self.goal,
            "slug": self.slug,
            "manifest": self.manifest,
            "validation": self.validation.as_dict(),
            "skills_used": list(self.skills_used),
            "notes": list(self.notes),
            "dropped": list(self.dropped),
            "shape": self.shape,
            "staffing": [dict(gap) for gap in self.staffing],
            "graph_review": dict(self.graph_review or {}),
        }


class Planner:
    """Composes a validated workflow manifest from a goal and the skill library.

    Parameters
    ----------
    source:
        Skill source, used to check each candidate skill's declared contract.
    validator:
        Optional callable `(manifest_dict) -> PlanValidation`. When omitted, the library's
        own `validate-workflows.py` is loaded from the pinned root. Injecting a stub is how
        the planner's fallback logic is tested without the library present.
    config:
        Supplies budget defaults and the manifest naming rules.
    org:
        The effective roster, when known. Optional: a planner with no roster still composes a
        runnable graph, but one *with* a roster can report which needed skills nobody holds and the
        exact hire that closes each gap.
    """

    def __init__(self, source: Any, *, validator: Any = None, config: Config | None = None,
                 org: Any = None) -> None:
        self.source = source
        self.config = config
        self._validator = validator
        self._library_validator = None
        #: The effective roster, when one is available. Optional on purpose: the planner must
        #: compose a runnable graph even with no roster (a fresh machine), but when a roster *is*
        #: known it can say which needed capabilities nobody holds — the gap the Owner most needs
        #: to see before approving a run.
        self.org = org

    # ── public API ──────────────────────────────────────────────────────────

    def plan(self, goal: str, *, slug: str | None = None, max_iterations: int = 3,
             max_steps: int | None = None) -> Plan:
        """Produce a validated manifest for a goal.

        Tries progressively simpler shapes so a goal that cannot support the full company
        still yields a runnable graph, and falls back to a minimal skeleton rather than
        failing outright. Every returned plan has passed validation.

        Raises
        ------
        PlanError
            When even the minimal skeleton fails validation, which means this module is
            broken rather than the goal being unusual.
        """
        if not goal or not goal.strip():
            raise PlanError("a goal is required; an empty goal cannot produce a plan")
        project = slug or _slugify(goal)
        # Classify first: the domain decides the *org*, not just which extra skills to bolt onto a
        # software pipeline. This is the fix for "a CEO-and-market goal ran as product-manager →
        # architect → backend-developer".
        domain = self._classify_domain(goal)
        selected = self._select_skills(goal, domain)

        candidates = [
            ("full", self._compose(project, goal, selected, domain, max_iterations, max_steps,
                                   include_parallel=True, include_all_verifiers=True)),
            ("lean", self._compose(project, goal, selected, domain, max_iterations, max_steps,
                                   include_parallel=False, include_all_verifiers=False)),
            ("minimal", self._minimal(project, goal, max_iterations)),
        ]

        dropped: list[str] = []
        for label, manifest in candidates:
            validation = self.validate(manifest)
            if validation.valid:
                notes = [] if label == "full" else [
                    f"used the {label} shape because the richer shape did not validate"
                ]
                if domain != "software":
                    notes = [*notes, f"classified the goal as a {domain} goal, not a software build"]
                return Plan(
                    goal=goal,
                    slug=project,
                    manifest=manifest,
                    validation=validation,
                    skills_used=tuple(
                        str(node.get("skill")) for node in manifest.get("nodes") or []
                        if node.get("skill")
                    ),
                    notes=tuple(notes),
                    dropped=tuple(dropped),
                    shape=domain,
                    staffing=self._staffing_gaps(manifest),
                    graph_review=self._graph_review(manifest),
                )
            dropped.append(f"{label} shape rejected: " + "; ".join(validation.errors[:3]))

        raise PlanError(
            "no candidate manifest validated, which indicates a defect in the planner "
            "rather than an unusual goal:\n  " + "\n  ".join(dropped)
        )

    # ── domain classification ───────────────────────────────────────────────

    def _classify_domain(self, goal: str) -> str:
        """Which composition shape a goal calls for.

        Two signals, in order of authority:

        1. **A named capability.** "use the CEO skill", "do market research" is an *instruction*,
           so a name match wins outright over any keyword.
        2. **Stated intent.** A domain word ("strategy", "go-to-market", "research") is a stronger
           signal than a mere technical noun, so intent domains are tested before `software`. A goal
           with a technical word but no domain intent ("build a booking SaaS") stays software.

        Falls back to `software`, which is the shape that always validates and the one an
        unqualified "build me X" most often means.
        """
        lowered = goal.lower()
        for name, domain in _DOMAIN_NAMES.items():
            if name in lowered:
                return domain
        for domain in _DOMAIN_INTENT_ORDER:
            for pattern in _DOMAIN_KEYWORDS.get(domain, ()):
                if re.search(pattern, lowered):
                    return domain
        return "software"

    def _select_skills(self, goal: str, domain: str) -> list[tuple[str, str, str]]:
        """Choose the company for a goal: the domain's chain, its verifiers, and goal-matched extras.

        A skill that is not in the library, or whose contract cannot be loaded, is skipped —
        the planner must never emit an edge to a node whose contract it could not read.
        """
        shape = _DOMAIN_SHAPES.get(domain) or _DOMAIN_SHAPES["software"]
        selected: list[tuple[str, str, str]] = list(shape["build"])
        already = {skill for _id, skill, _phase in selected}
        lowered = goal.lower()
        # Technical/functional specialists extend the build chain. They never *replace* the domain's
        # own roles: a strategy goal that also mentions "dashboard" still gets its strategists.
        for pattern, skill in _GOAL_HINTS:
            if skill in already:
                continue
            if re.search(pattern, lowered) and self._load(skill) is not None:
                selected.append((_node_id_for(skill), skill, _phase_for(skill)))
                already.add(skill)
        # A role the goal names outright is an instruction, so it joins the chain even when the
        # domain would not have chosen it — "use the CEO skill *and* bring a market researcher" must
        # produce both, not whichever the keyword vote happened to prefer.
        for pattern, skill in _ROLE_HINTS:
            if skill in already:
                continue
            if re.search(pattern, lowered) and self._load(skill) is not None:
                selected.append((_node_id_for(skill), skill, _phase_for(skill)))
                already.add(skill)
        # Verifiers go last so the chain's final node is the producing node, which is what the
        # rework loop hands back to.
        for node_id, skill, phase in shape["verify"]:
            if skill in already:
                continue
            if self._load(skill) is not None:
                selected.append((node_id, skill, phase))
                already.add(skill)
        return selected

    def _staffing_gaps(self, manifest: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        """Skills the plan needs that the roster does not staff, with the hire that closes each.

        The binder reports gaps too, but by the time a *run* exists the plan is already approved —
        and a gap is cheapest to fix before approval, when the Owner is looking at the graph. This is
        the planner's own copy of that check, phrased as "hire this to fix it".
        """
        if self.org is None:
            return ()
        gaps: list[dict[str, Any]] = []
        for node in manifest.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            skill = str(node.get("skill") or "")
            if not skill:
                continue
            # A human gate lists no skill; a skill with no holder is a gap unless the Owner's own
            # wildcard covers it (the Owner holds `*`, so nothing is a gap when they may act).
            holders = self._holders_of(skill)
            if holders:
                continue
            gaps.append({
                "node_id": str(node.get("id") or skill),
                "skill": skill,
                "reason": "no agent in the roster holds this skill",
                "hire": (f"engine.cli hire <name> --skill {skill} "
                         f"(or `org hire` in the app)"),
            })
        return tuple(gaps)

    def _holders_of(self, skill: str) -> list[Any]:
        """Roster agents that hold a skill, tolerating a roster that is not an `Org`."""
        try:
            holders = self.org.agents_for_skill(skill)
        except Exception:  # noqa: BLE001 - a roster we cannot read is not a plan failure
            return []
        return [a for a in holders if getattr(a, "kind", None) != "human"]

    def _graph_review(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Ask the library's own chain graph how this plan hangs together.

        A read-only diagnosis, not a composer: it reports which plan skills the corpus says are
        unrelated to the rest, and which prerequisites several of them declare that the plan omits.
        It never changes the manifest — the graph is dense and mutual, so it informs a person rather
        than dictating an order. Empty when no graph can be built, so the plan still renders.

        The consensus threshold scales with the plan: demanding a fixed 2 of 3 nodes would flood a
        small plan with noise from the graph's density, while a large plan needs a higher bar to be
        meaningful.
        """
        try:
            from .skills.graph import SkillGraph

            skills = [str(n.get("skill")) for n in manifest.get("nodes") or [] if n.get("skill")]
            if not skills:
                return {}
            threshold = max(2, len(skills) // 2)
            return SkillGraph(self.source).plan_review(skills, min_consensus=threshold)
        except Exception:  # noqa: BLE001 - the graph is a diagnosis, never a blocker
            return {}

    def validate(self, manifest: dict[str, Any]) -> PlanValidation:
        """Validate a manifest with the library's own validator.

        Uses the pinned `validate-workflows.py` so the verdict is the library's, not a
        reimplementation — the whole point of building on the library is that its
        validation is authoritative.
        """
        validator = self._get_validator()
        if validator is None:
            # Without a validator we cannot honestly claim validity, so structural checks
            # that this module can perform itself are applied and the result is still
            # labelled as locally checked rather than library-validated.
            return self._structural_check(manifest)
        result = validator(manifest)
        if isinstance(result, PlanValidation):
            return result
        if isinstance(result, dict):
            return PlanValidation(
                valid=bool(result.get("valid")),
                errors=tuple(
                    str(e.get("message") if isinstance(e, dict) else e)
                    for e in (result.get("errors") or [])
                ),
                name=str(result.get("name") or ""),
                manifest_sha=str(result.get("manifest_sha") or ""),
            )
        return PlanValidation(valid=False, errors=(f"validator returned {type(result).__name__}",))

    # ── validation plumbing ─────────────────────────────────────────────────

    def _get_validator(self):
        """Load the library's validator lazily, or return the injected stub."""
        if self._validator is not None:
            return self._validator
        if self._library_validator is not None:
            return self._library_validator
        try:
            import importlib.util
            import sys

            root = getattr(self.source, "library_root", None)
            if root is None:
                return None
            scripts = root / "scripts"
            validator_path = scripts / "validate-workflows.py"
            if not validator_path.is_file():
                return None
            # The validator imports its siblings by module name, so the scripts directory
            # must be importable before it is loaded.
            if str(scripts) not in sys.path:
                sys.path.insert(0, str(scripts))
            spec = importlib.util.spec_from_file_location("_agentorg_validate_workflows",
                                                          validator_path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules["_agentorg_validate_workflows"] = module
            spec.loader.exec_module(module)
            skills = module._find_skill_names() if hasattr(module, "_find_skill_names") else set()
            instance = module.WorkflowValidator(skills)

            def run(manifest: dict[str, Any]) -> dict[str, Any]:
                return instance.validate_data(manifest)

            self._library_validator = run
            return run
        except Exception:  # noqa: BLE001 - a missing validator degrades, never crashes
            return None

    def _structural_check(self, manifest: dict[str, Any]) -> PlanValidation:
        """Structural validation used when the library validator is unavailable.

        Checks the invariants the design promises — an end node, a bounded loop, a reachable
        terminal gate, and edges connecting nodes that exist — so a plan is never returned
        claiming validity it has not been checked for. The result is still labelled by
        which checks ran.
        """
        errors: list[str] = []
        nodes = {str(n.get("id")) for n in manifest.get("nodes") or [] if n.get("id")}
        if not nodes:
            errors.append("manifest has no nodes")
        if not manifest.get("start"):
            errors.append("manifest has no start node")
        elif str(manifest["start"]) not in nodes:
            errors.append(f"start node {manifest['start']!r} is not a declared node")

        for edge in manifest.get("edges") or []:
            if str(edge.get("from")) not in nodes:
                errors.append(f"edge from unknown node {edge.get('from')!r}")
            if str(edge.get("to")) not in nodes:
                errors.append(f"edge to unknown node {edge.get('to')!r}")

        ends = manifest.get("end") or []
        if not ends:
            errors.append("manifest declares no end node, so the run cannot terminate")
        for end in ends:
            if str(end) not in nodes:
                errors.append(f"end node {end!r} is not a declared node")

        for loop in manifest.get("loops") or []:
            if not loop.get("exit_when"):
                errors.append(f"loop {loop.get('id')!r} has no exit_when")
            if not loop.get("max_iterations"):
                errors.append(f"loop {loop.get('id')!r} has no max_iterations")
            target = loop.get("escalate_to")
            if target and str(target) not in nodes:
                errors.append(f"loop {loop.get('id')!r} escalates to unknown node {target!r}")

        return PlanValidation(valid=not errors, errors=tuple(errors),
                              name=str(manifest.get("name") or ""))

    # ── composition ─────────────────────────────────────────────────────────

    def _handoff_compatible(self, producer: dict[str, Any], consumer: dict[str, Any]) -> bool:
        """Whether a producer's declared outputs can satisfy a consumer's declared inputs.

        The library's validator checks graph shape but not artifact typing, so this is where
        the design's "a handoff the producer cannot satisfy is refused" promise is kept. A
        consumer with no declared inputs accepts anything (it is a generator or a gate); a
        producer that declares no outputs cannot satisfy a typed consumer.
        """
        if consumer.get("type") == "gate":
            return True
        required = set(consumer.get("inputs") or [])
        if not required:
            return True
        produced = set(producer.get("outputs") or [])
        return bool(produced & required)

    def _load(self, skill: str) -> SkillBundle | None:
        """Load a bundle, returning None when the skill is missing or unloadable."""
        try:
            return self.source.load(skill)
        except (SkillError, Exception):  # noqa: BLE001 - a bad skill is skipped, not fatal
            return None

    def _compose(self, slug: str, goal: str, selected: list[tuple[str, str, str]], domain: str,
                 max_iterations: int, max_steps: int | None, *,
                 include_parallel: bool, include_all_verifiers: bool) -> dict[str, Any]:
        """Build a manifest from the selected skills.

        The graph is: sequential phases, then a parallel verification fan-out, then a bounded
        rework loop whose exhaustion reaches a terminal human gate. That shape is what makes
        "work until done" terminate rather than spin, and it is the same shape for every domain —
        only the *people* differ, because the invariants (terminate, gate, bounded loop) are
        properties of the graph rather than of the work.

        `domain` selects the verifier set and the description; the software domain keeps the exact
        behavior it always had, so existing plans are unchanged.
        """
        shape = _DOMAIN_SHAPES.get(domain) or _DOMAIN_SHAPES["software"]
        verifier_skills = {skill for _id, skill, _phase in shape["verify"]}

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        used_skills: list[str] = []
        dropped: list[str] = []
        # Type mismatches that were allowed rather than fatal, surfaced for the Owner.
        type_notes: list[str] = []

        # Separate the build chain from the verification specialists.
        first_verifier = _first_verifier(selected, verifier_skills)
        primary: list[tuple[str, str, str]] = []
        review: list[tuple[str, str, str]] = []
        for entry in selected:
            if entry[1] in verifier_skills:
                # A lean shape keeps the first verifier only: enough to gate on, cheap to run.
                if not include_all_verifiers and entry != first_verifier:
                    dropped.append(f"{entry[1]} omitted from the lean shape")
                    continue
                review.append(entry)
            else:
                primary.append(entry)

        for node_id, skill, phase in primary:
            bundle = self._load(skill)
            if bundle is None:
                dropped.append(f"{skill} omitted: contract could not be loaded")
                continue
            node: dict[str, Any] = {
                "id": node_id,
                "skill": skill,
                "phase": phase,
                "max_iterations": 1,
            }
            # A node in a *producing* phase works on the artefact, so it gets the tools to read and
            # change it. Without this the planner emitted no `tools` key at all, so a normal
            # `run --goal` never used the tool loop — the feature would have existed and been
            # unreachable, which is the failure mode this codebase keeps producing.
            #
            # Verifiers are deliberately excluded: they judge an artifact, and a verifier that can
            # edit what it judges is not a verifier. Their capability set already denies writes, and
            # not advertising the tools keeps the prompt honest about what they may do.
            if phase in ("BUILD", "IMPLEMENT", "FIX", "DESIGN", "DISCOVER"):
                node["tools"] = True
            if bundle.contract.inputs:
                node["inputs"] = list(bundle.contract.inputs)
            if bundle.contract.outputs:
                node["outputs"] = list(bundle.contract.outputs)
            nodes.append(node)
            used_skills.append(skill)

        if not nodes:
            raise PlanError(f"no usable skills for goal {goal!r}")

        # Primary chain: each node hands off to the next when it is done.
        #
        # The skills' declared `workflow.artifacts` describe a skill's *typical* use, not an
        # exhaustive type system — `backend-developer` declares `inputs: [findings]` because
        # fixing review findings is its documented primary use, yet it plainly also builds
        # from a spec. Treating those declarations as hard pipeline types would break every
        # real pipeline, so a mismatch is recorded as a note the Owner can see rather than
        # used to silently drop a phase.
        for index in range(len(nodes) - 1):
            producer, consumer = nodes[index], nodes[index + 1]
            edges.append(self._edge(producer["id"], consumer["id"],
                                    f"{producer['id']}.status == done"))
            if not self._handoff_compatible(producer, consumer):
                type_notes.append(
                    f"{producer['id']} declares outputs {sorted(producer.get('outputs') or [])} "
                    f"while {consumer['id']} declares inputs {sorted(consumer.get('inputs') or [])}; "
                    "the handoff is allowed because artifact declarations describe typical use, "
                    "but the receiving node must state what it actually received"
                )
        last_primary = nodes[-1]["id"]

        # Review fan-out: the reviewers consume the produced change.
        review_nodes: list[str] = []
        for node_id, skill, phase in review:
            bundle = self._load(skill)
            if bundle is None:
                dropped.append(f"{skill} omitted: contract could not be loaded")
                continue
            node = {
                "id": node_id,
                "skill": skill,
                "phase": phase,
                "max_iterations": 1,
            }
            # Declared outputs *and* inputs, so the node carries the same contract detail as
            # the primary chain and the handoff type check has the information it needs.
            if bundle.contract.inputs:
                node["inputs"] = list(bundle.contract.inputs)
            if bundle.contract.outputs:
                node["outputs"] = list(bundle.contract.outputs)
            nodes.append(node)
            used_skills.append(skill)
            review_nodes.append(node_id)
            edges.append(self._edge(last_primary, node_id,
                                    f"{last_primary}.status == done"))
            producer = next(n for n in nodes if n["id"] == last_primary)
            if not self._handoff_compatible(producer, node):
                type_notes.append(
                    f"reviewer {node_id} expects {sorted(bundle.contract.inputs)} but "
                    f"{last_primary} declares {sorted(producer.get('outputs') or [])}; the "
                    "reviewer must read the artifact it was actually given"
                )

        if not review_nodes:
            raise PlanError(f"no reviewer skills available for goal {goal!r}")

        # The rework loop: verifiers -> the chain's producer -> verifiers, bounded and with an exit.
        #
        # The producer handed back the findings is the last node of the *domain's own chain*, which
        # for software is `backend-developer` and for a strategy goal is the business analyst. Using
        # the chain's producer rather than a hardcoded `backend-developer` is what lets a
        # non-software goal iterate at all — and preferring the chain over an incidental specialist
        # keeps the rework aimed at the work rather than at whoever happened to be added last.
        chain_skills = {skill for _id, skill, _phase in shape["build"]}
        developer = next((n["id"] for n in reversed(nodes) if n["skill"] in chain_skills),
                         last_primary)
        verdict_node = review_nodes[0]
        gate_id = "human-gate"
        agent_gate_id = "reroute-gate"
        # A bounded-reroute **agent gate** sits between the loop and the human. When automation
        # exhausts its iterations, the runner hands the gate the untried channels and asks which one
        # should lead a *fresh* pass — a decision the org can make itself, bounded by `max_reroutes`,
        # before bothering the Owner. The runner and the executor have always supported this
        # (`kind: agent`, `mode: identify`); nothing emitted one, so every exhaustion went straight to
        # a person. The gate's pool is the loop's own members, so a reroute stays inside the rework.
        nodes.append({
            "id": agent_gate_id,
            "type": "gate",
            "kind": "agent",
            "pool": [*review_nodes, developer],
            "max_reroutes": 2,
            "escalate_to": gate_id,
            "description": (
                "Bounded reroute: on exhaustion, identify the channel best placed to fix the "
                "shortfall and grant one fresh pass (at most twice) before escalating to the Owner."
            ),
        })
        nodes.append({
            "id": gate_id,
            "type": "gate",
            "kind": "human",
            "requires": [f"{nid}.summary" for nid in review_nodes][:3],
            "description": _gate_description(domain),
        })

        for node_id in review_nodes:
            edges.append(self._edge(node_id, gate_id, f"{node_id}.status == done"))

        manifest: dict[str, Any] = {
            "name": slug,
            "version": "1.0.0",
            "description": _one_line(goal),
            "payloads": {_PAYLOAD: list(_PAYLOAD_FIELDS)},
            "start": nodes[0]["id"],
            "nodes": nodes,
            "gates": [n for n in nodes if n.get("type") == "gate"],
            "edges": edges,
            "loops": [{
                "id": "review-fix-loop",
                "nodes": [*review_nodes, developer],
                "exit_when": f"{verdict_node}.verdict == pass",
                "max_iterations": max(1, int(max_iterations)),
                # Escalate to the *agent* gate, which decides a bounded reroute and only then
                # escalates onward to the human gate.
                "escalate_to": agent_gate_id,
                "convergence": {"window": 2, "require_delta": True},
            }],
            "end": [gate_id],
        }
        # Only the real nodes; the gate is listed once under `gates`.
        manifest["nodes"] = [n for n in nodes if n.get("type") != "gate"]

        if include_parallel and len(review_nodes) > 1:
            manifest["parallel"] = [{
                "id": "reviewers",
                "nodes": review_nodes,
                "join": "all",
                "outputs": ["review-findings"],
            }]
        if max_steps is not None:
            manifest["budget"] = {"max_steps": int(max_steps)}

        # Both dropped phases and allowed type mismatches are information the Owner needs,
        # so they travel with the plan rather than being logged and forgotten.
        self._last_dropped = [*dropped, *type_notes]
        return manifest
    def _minimal(self, slug: str, goal: str, max_iterations: int) -> dict[str, Any]:
        """The smallest runnable graph: one worker, one reviewer, one bounded loop, one gate.

        Used when the richer shapes fail validation. It still satisfies every termination
        invariant, so it is a genuine fallback rather than a degraded one.
        """
        return {
            "name": slug,
            "version": "1.0.0",
            "description": _one_line(goal),
            "payloads": {_PAYLOAD: list(_PAYLOAD_FIELDS)},
            "start": "developer",
            "nodes": [
                {"id": "developer", "skill": "backend-developer",
                 "inputs": ["findings"], "outputs": ["change"], "max_iterations": 1},
                {"id": "reviewer", "skill": "code-reviewer",
                 "inputs": ["change"], "outputs": ["review-report"], "max_iterations": 1},
            ],
            "gates": [{
                "id": "human-gate", "type": "gate", "kind": "human",
                "requires": ["review-report"],
                "description": "Owner approval after the review loop converges or escalates.",
            }],
            "edges": [
                {"from": "developer", "to": "reviewer",
                 "when": "developer.status == done", "payload": _PAYLOAD},
                {"from": "reviewer", "to": "human-gate",
                 "when": "reviewer.status == done", "payload": _PAYLOAD},
            ],
            "loops": [{
                "id": "review-fix-loop",
                "nodes": ["reviewer", "developer"],
                "exit_when": "reviewer.verdict == pass",
                "max_iterations": max(1, int(max_iterations)),
                "escalate_to": "human-gate",
                "convergence": {"window": 2, "require_delta": True},
            }],
            "end": ["human-gate"],
        }

    @staticmethod
    def _edge(source: str, target: str, when: str) -> dict[str, Any]:
        """A typed edge carrying the handoff payload."""
        return {"from": source, "to": target, "when": when, "payload": _PAYLOAD}


def emit_safe_yaml(data: dict[str, Any]) -> str:
    """Serialise a manifest into the library's Safe YAML Subset.

    The library parses manifests with a deliberately narrow subset parser that rejects flow
    maps (`{a: 1}`) and block scalars, but ships no emitter. So the planner must emit the
    subset itself — otherwise a plan that validates in memory would be unreadable from disk,
    which is the only way the runner ever sees it.

    Emits block mappings, block sequences at the parent indent, scalars quoted only when
    necessary, and flow lists of plain scalars (which the subset does allow).
    """
    lines: list[str] = []
    _emit_mapping(data, lines, 0)
    return "\n".join(lines) + "\n"


def _emit_mapping(mapping: dict[str, Any], lines: list[str], indent: int) -> None:
    pad = " " * indent
    for key, value in mapping.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            _emit_mapping(value, lines, indent + 2)
        elif isinstance(value, list):
            lines.append(f"{pad}{key}:")
            _emit_sequence(value, lines, indent + 2)
        else:
            lines.append(f"{pad}{key}: {_emit_scalar(value)}")


def _emit_sequence(sequence: list[Any], lines: list[str], indent: int) -> None:
    pad = " " * indent
    for item in sequence:
        if isinstance(item, dict):
            # A mapping item: the first key sits on the dash line, the rest align under it.
            entries = list(item.items())
            if not entries:
                lines.append(f"{pad}- {{}}")
                continue
            first_key, first_value = entries[0]
            if isinstance(first_value, (dict, list)):
                lines.append(f"{pad}- {first_key}:")
                if isinstance(first_value, dict):
                    _emit_mapping(first_value, lines, indent + 4)
                else:
                    _emit_sequence(first_value, lines, indent + 4)
            else:
                lines.append(f"{pad}- {first_key}: {_emit_scalar(first_value)}")
            for key, value in entries[1:]:
                if isinstance(value, dict):
                    lines.append(f"{pad}  {key}:")
                    _emit_mapping(value, lines, indent + 4)
                elif isinstance(value, list):
                    lines.append(f"{pad}  {key}:")
                    _emit_sequence(value, lines, indent + 4)
                else:
                    lines.append(f"{pad}  {key}: {_emit_scalar(value)}")
        elif isinstance(item, list):
            # A nested sequence: the subset allows one structural level, so this is emitted
            # as a flow list of scalars when possible.
            lines.append(f"{pad}- [{', '.join(_emit_scalar(v) for v in item)}]")
        else:
            lines.append(f"{pad}- {_emit_scalar(item)}")


def _emit_scalar(value: Any) -> str:
    """Render a scalar in a form the subset parser reads back as the same type.

    Quoting is applied only when needed: a bare word stays bare, but a value containing a
    colon, a leading special character, or a boolean/number lookalike is quoted so it is not
    misread. This is what keeps `when: reviewer.verdict == pass` working and
    `description: Build a booking SaaS` from becoming a parse error.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    needs_quotes = (
        ":" in text
        or text[:1] in ("[", "]", "{", "}", "&", "*", "!", "|", ">", "%", "@", "`", "#", ",")
        # A leading `- ` would be read as a nested sequence item by a YAML parser, which is
        # a real ambiguity rather than a stylistic one.
        or text.startswith("- ")
        or text.strip() != text
        or "\n" in text
        or text.lower() in ("true", "false", "yes", "no", "null", "~", "none")
        or text[:1].isdigit() and ("." in text or text.isdigit())
    )
    if not needs_quotes:
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


# ── helpers ──────────────────────────────────────────────────────────────────


def _slugify(text: str, *, limit: int = 48) -> str:
    """A manifest-safe slug: lowercase, alphanumeric and hyphens, length-bounded.

    The library requires `[a-z0-9][a-z0-9-]*`, so a goal like "Build a Booking SaaS!" must
    become `build-a-booking-saas` rather than failing validation on a stray character.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:limit].strip("-")
    if not slug:
        slug = "workflow"
    if not slug[0].isalnum():
        slug = "w" + slug
    return slug


def _one_line(text: str, *, limit: int = 300) -> str:
    """Collapse a goal to one line, because the manifest requires a single-line description."""
    collapsed = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def _node_id_for(skill: str) -> str:
    """A stable node id for a skill, matching the library's slug rules."""
    return _slugify(skill, limit=32)


def _first_verifier(selected: list[tuple[str, str, str]],
                    verifier_skills: set[str]) -> tuple[str, str, str] | None:
    """The verifier kept in the lean shape — the first one the domain declares.

    The lean shape drops *extra* verifiers to run cheaply, but it must keep at least one so the
    plan still has something to gate on. Naming the first (not "the security reviewer", which a
    strategy plan has never heard of) is what makes the same rule work in every domain.
    """
    for entry in selected:
        if entry[1] in verifier_skills:
            return entry
    return None


def _gate_description(domain: str) -> str:
    """The human gate's description, worded for the work the domain produces."""
    if domain == "software":
        return ("Owner approval: the change is released once the review loop converges, or the "
                "escalation report is reviewed when automation exhausted its budget.")
    return ("Owner decision: the deliverable is accepted once the verification loop converges, or "
            "the escalation report is reviewed when automation exhausted its budget. Nothing "
            "downstream (a build, a launch, a spend) proceeds without this.")


def _phase_for(skill: str) -> str:
    """The lifecycle phase a specialist belongs to.

    A verifier is detected by the *skill itself* (`skills/roles.py`), so a judging procedure the
    library adds is given `VERIFY` without editing this table — which is the whole point of deriving
    the role rather than listing it. The explicit tables below then place the producers.
    """
    from .skills.roles import is_verifier

    if is_verifier(skill):
        return "VERIFY"
    if skill in ("product-manager", "product-strategist", "ux-researcher", "ceo-strategist",
                 "business-strategist", "project-manager", "bizdev-manager",
                 "investor-relations"):
        return "DISCOVER"
    if skill in ("system-architect", "api-designer", "database-designer", "cloud-architect",
                 "business-intelligence-engineer", "fp-and-a-analyst", "security-engineer"):
        return "DESIGN"
    if skill in ("devops-engineer", "platform-engineer", "site-reliability-engineer"):
        return "OPERATE"
    return "BUILD"
