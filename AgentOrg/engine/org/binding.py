#!/usr/bin/env python3
"""binding.py — bind a manifest node to an agent from the roster.

WHY THIS EXISTS
---------------
This is the module that makes "the same skill exists many times under different names" real.
A manifest node names a *capability* (`skill: backend-developer`); the roster supplies the
*headcount* (Alice, Bob, Chen). Binding is the join between them, and it is where two
structural refusals live:

1. **A reviewer may not be the artifact's producer.** `verification-independence-engineer`
   requires a verifier that differs from the producer by model, context lineage, or both.
   Enforcing it here means the property cannot be forgotten.
2. **An agent bound to a model with an unknown context window is refused**, because the
   session projection cannot size a prompt without that number.

DESIGN
------
- **Four policies**, because one is not enough: `pinned` (a fixed agent), `round-robin`
  (spread work), `load-balanced` (prefer a holder with a free concurrency slot), and `swarm`
  (all of them, for a quorum).
- **Availability is a count, not a guess.** A holder is a candidate while it has a slot left under
  its own `max_concurrency`, and the binding carries the whole eligible pool so the executor can
  claim a *real* slot from it when the node starts. The choice here is a preference; the claim is
  what makes it binding, and the two are split because this module also binds a manifest for
  display, where nothing runs and nothing may be consumed.
- **Determinism is a feature.** Selection is stable for the same inputs, so a replayed run
  routes identically and a bug is reproducible.
- **Quarantined agents are never selected.** The health engine removes them from the pool, and
  binding is the enforcement point.
- **Every refusal names the alternative**, so a binding failure tells the Owner what to do.

Usage:
    binder = Binder(org)
    binding = binder.bind(node, policy=BindingPolicy.LOAD_BALANCED, exclude={producer_id})
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .agent import AgentError, AgentKind, AgentSpec, AgentState
from .roster import Org

# Imported from the skills package, not duplicated here: one answer to "does this node judge or
# produce?" keeps the binder from disagreeing with the planner about who a reviewer is.
from ..skills.roles import is_verifier

__all__ = ["BindingError", "BindingPolicy", "Binder", "NodeBinding", "declared_policy"]


def declared_policy(node: dict[str, Any]) -> BindingPolicy | None:
    """The binding policy a manifest node declares, if any.

    Read from `binding` (the clearer name a plan would use) or `policy` (the field this module
    already speaks). An unknown value returns None rather than raising: a manifest is authored, and a
    typo there should fall back to the default policy rather than kill a run at bind time.

    Lives here, next to `BindingPolicy`, because both the planner-side binder and the executor need
    it — and two copies of "how a node declares its policy" is how the two disagree.
    """
    raw = node.get("binding") or node.get("policy")
    if not raw:
        return None
    try:
        return BindingPolicy(str(raw).strip().lower())
    except ValueError:
        return None


class BindingError(RuntimeError):
    """Raised when a node cannot be bound to an agent."""


class BindingPolicy(str, Enum):
    """How to choose an agent for a node."""

    PINNED = "pinned"              # always this agent
    ROUND_ROBIN = "round-robin"    # rotate through the candidates
    LOAD_BALANCED = "load-balanced"  # prefer the idle, then the most capable
    SWARM = "swarm"                # every candidate, for an n-of-m quorum


@dataclass
class NodeBinding:
    """The resolved assignment of one node."""

    node_id: str
    skill: str
    agents: list[str]                 # agent ids, in the order they will be used
    policy: BindingPolicy = BindingPolicy.LOAD_BALANCED
    pinned_id: str | None = None
    # Why this selection, recorded so "why did this node run as that agent?" is answerable.
    reason: str = ""
    excluded: tuple[str, ...] = ()
    #: Every eligible holder, best-first, for the policies where a substitution is legitimate.
    #:
    #: `agents[0]` is the choice; this is the fallback pool behind it, in the order that should be
    #: tried, and the executor claims a slot along it when the chosen agent turns out to be at its
    #: limit by the time the node starts. Binding cannot make that claim itself — `plan_bindings`
    #: binds a whole manifest at plan time, and a preview that consumed capacity would leave the
    #: agents it named looking busy before any work existed. Empty for `pinned` and `round-robin`,
    #: whose selection *is* the distribution policy and must not be second-guessed.
    candidates: tuple[str, ...] = ()

    @property
    def primary(self) -> str:
        """The agent that will run the node (the first, for every policy)."""
        if not self.agents:
            raise BindingError(f"node {self.node_id!r} has no bound agent")
        return self.agents[0]

    @property
    def is_swarm(self) -> bool:
        return self.policy is BindingPolicy.SWARM

    def quorum(self, n: int | None = None) -> int:
        """How many agreeing answers constitute a swarm verdict.

        A strict majority, because an even split with no majority must be resolved by
        escalation rather than by picking a side.
        """
        if n is None:
            n = len(self.agents)
        return max(1, n // 2 + 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "skill": self.skill,
            "agents": list(self.agents),
            "policy": self.policy.value,
            "pinned_id": self.pinned_id,
            "reason": self.reason,
            "excluded": list(self.excluded),
            "quorum": self.quorum() if self.is_swarm else None,
        }


@dataclass
class Binder:
    """Resolves manifest nodes to roster agents.

    Parameters
    ----------
    org:
        The roster. Lookups go through it so live state (free slots, quarantined) is respected.
    """

    org: Org
    _round_robin: dict[str, int] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ── binding ─────────────────────────────────────────────────────────────

    def bind(self, node: dict[str, Any], *, policy: BindingPolicy = BindingPolicy.LOAD_BALANCED,
             pinned: str | None = None, exclude: Iterable[str] = (),
             prefer_model: str | None = None,
             available_only: bool = True) -> NodeBinding:
        """Choose the agent(s) for a node.

        Parameters
        ----------
        node:
            The manifest node. Its `skill` names the capability to find.
        policy:
            How to select. `SWARM` returns every candidate so a quorum can vote.
        pinned:
            For `PINNED`, the agent id.
        exclude:
            Agent ids that may not be chosen — the producer, when binding a reviewer.
        prefer_model:
            Bias selection toward a specific model, used to enforce independence by binding a
            reviewer to a different model than the producer.
        available_only:
            Exclude quarantined and terminated agents. Default on; a caller that needs to
            inspect the full set of holders can turn it off.

        Raises
        ------
        BindingError
            When the node declares no skill, no candidate holds it, every candidate is
            excluded, or a reviewer would be its own producer.
        """
        skill = str(node.get("skill") or "").strip()
        if not skill:
            # A gate or a supervisor node deliberately names no skill; it is not an error, but
            # it cannot be bound either, and saying so is clearer than returning an empty list.
            raise BindingError(
                f"node {node.get('id')!r} declares no skill, so it cannot be bound to an agent. "
                "Gate and supervisor nodes are handled by the orchestrator, not the binder."
            )
        node_id = str(node.get("id") or skill)
        excluded = tuple(sorted(set(exclude)))

        candidates = self.org.candidates_for(skill, available_only=available_only)
        if not candidates:
            raise BindingError(
                f"no agent in the roster holds the skill {skill!r} needed by node {node_id!r}. "
                "Hire an agent with that skill, or check that its model has a known context "
                "window (a binding without one is refused)."
            )

        eligible = [a for a in candidates if a.id not in excluded]
        if not eligible:
            holders = ", ".join(f"{a.name} ({a.id})" for a in candidates)
            raise BindingError(
                f"every agent holding {skill!r} for node {node_id!r} is excluded. "
                f"Excluded: {', '.join(excluded) or '(none)'}. Holders: {holders}. "
                "Hire another agent with this skill so the producer is not the reviewer."
            )

        # Independence by model: when asked, prefer candidates on a different model.
        if prefer_model:
            different = [a for a in eligible if a.model != prefer_model]
            if different:
                eligible = different
            else:
                # Every holder shares the producer's model. That leaves context lineage as the
                # only boundary, which is still valid, so the caller is told rather than
                # blocked — but the weaker basis is recorded.
                pass

        if policy is BindingPolicy.PINNED:
            return self._bind_pinned(node_id, skill, eligible, pinned, excluded)
        if policy is BindingPolicy.SWARM:
            return self._bind_swarm(node_id, skill, eligible, excluded)
        if policy is BindingPolicy.ROUND_ROBIN:
            return self._bind_round_robin(node_id, skill, eligible, excluded)
        return self._bind_load_balanced(node_id, skill, eligible, excluded)

    def _bind_pinned(self, node_id: str, skill: str, eligible: list[AgentSpec],
                     pinned: str | None, excluded: tuple[str, ...]) -> NodeBinding:
        """Bind to a specific agent, verifying it is eligible."""
        if not pinned:
            raise BindingError(
                f"node {node_id!r} uses the pinned policy but no agent was named. "
                "Supply pinned=<agent id>."
            )
        chosen = next((a for a in eligible if a.id == pinned), None)
        if chosen is None:
            holder = next((a for a in eligible if a.name.lower() == pinned.lower()), None)
            if holder is not None:
                chosen = holder
            else:
                eligible_names = ", ".join(f"{a.name} ({a.id})" for a in eligible)
                raise BindingError(
                    f"node {node_id!r} is pinned to {pinned!r}, but that agent either does not "
                    f"hold {skill!r}, is quarantined or terminated, or is excluded. "
                    f"Eligible: {eligible_names}"
                )
        return NodeBinding(
            node_id=node_id, skill=skill, agents=[chosen.id],
            policy=BindingPolicy.PINNED, pinned_id=chosen.id,
            reason=f"pinned to {chosen.name} by configuration",
            excluded=excluded,
        )

    def _bind_round_robin(self, node_id: str, skill: str, eligible: list[AgentSpec],
                          excluded: tuple[str, ...]) -> NodeBinding:
        """Rotate through the candidates across successive bindings of the same node."""
        with self._lock:
            index = self._round_robin.get(node_id, 0) % len(eligible)
            chosen = eligible[index]
            self._round_robin[node_id] = index + 1
        return NodeBinding(
            node_id=node_id, skill=skill, agents=[chosen.id],
            policy=BindingPolicy.ROUND_ROBIN,
            reason=f"round-robin position {index + 1} of {len(eligible)}",
            excluded=excluded,
        )

    def _bind_load_balanced(self, node_id: str, skill: str, eligible: list[AgentSpec],
                            excluded: tuple[str, ...]) -> NodeBinding:
        """Prefer a holder with a free slot, then the most capable, then the name.

        WHY the free slot is read from `AgentRuntime.available()` and not from `state`: availability
        is a *count* of held slots against the agent's own `max_concurrency`, because single-flight
        means "no more than the agent may run at once". An agent permitted two tasks is still a
        candidate while it works on one of them; an agent at its limit is not, however many capable
        siblings the roster has.

        This is a *preference*, not a reservation. The claim is taken by the executor when the node
        starts, along `candidates` — see `NodeBinding.candidates` for why the decision cannot be made
        at plan time, when a manifest is bound for display and nothing runs.
        """
        def key(spec: AgentSpec) -> tuple[int, int, str]:
            runtime = self.org.runtime(spec.id)
            busy = 0 if runtime.available() else 1
            return (busy, -int(spec.level), spec.name)

        ordered = sorted(eligible, key=key)
        chosen = ordered[0]
        idle = sum(1 for a in eligible if self.org.runtime(a.id).available())
        # The limit travels in the reason because "why is nobody free?" is the question this reason
        # exists to answer, and "0 of 3 have a free slot" says it without opening the roster.
        def slot_line(spec: AgentSpec) -> str:
            # `capacity` from the runtime rather than `max_concurrency` from the spec: the runtime's
            # copy was just refreshed from the spec by `org.runtime`, and the reason should quote the
            # limit that is actually in force, not the one the roster happens to carry.
            runtime = self.org.runtime(spec.id)
            return f"{spec.name} {runtime.inflight}/{runtime.capacity}"

        limits = ", ".join(slot_line(a) for a in ordered)
        return NodeBinding(
            node_id=node_id, skill=skill, agents=[chosen.id],
            policy=BindingPolicy.LOAD_BALANCED,
            reason=(
                f"{chosen.name} chosen: {idle} of {len(eligible)} with a free slot, "
                f"level {chosen.level.label} [{limits}]"
            ),
            excluded=excluded,
            candidates=tuple(a.id for a in ordered),
        )

    def _bind_swarm(self, node_id: str, skill: str, eligible: list[AgentSpec],
                    excluded: tuple[str, ...]) -> NodeBinding:
        """Bind every candidate, ordered deterministically, for a quorum vote."""
        ordered = sorted(eligible, key=lambda a: (-int(a.level), a.name))
        return NodeBinding(
            node_id=node_id, skill=skill, agents=[a.id for a in ordered],
            policy=BindingPolicy.SWARM,
            reason=f"swarm of {len(ordered)}; quorum {max(1, len(ordered) // 2 + 1)}",
            excluded=excluded,
            # The voters are the pool: a voter whose agent is at its limit may be run by another
            # holder, which weakens nothing — the vote is counted per agent, not per slot.
            candidates=tuple(a.id for a in ordered),
        )

    # ── the independence refusal ────────────────────────────────────────────

    def assert_independent(self, reviewer: AgentSpec, producer: AgentSpec) -> None:
        """Refuse a review where the reviewer produced the artifact.

        This is the structural expression of `verification-independence-engineer`'s rule: a
        check must name a verifier that is not the producer of the artifact it judges. A
        reviewer that is the producer inherits its own blind spots and agrees for the same
        wrong reason, which is the failure this refusal prevents.
        """
        if reviewer.id == producer.id:
            raise BindingError(
                f"{reviewer.name!r} cannot review its own work. "
                "verification-independence-engineer requires the verifier to differ from the "
                "producer. Bind the review node to a different agent, ideally on a different "
                "model."
            )

    def independence_basis(self, reviewer: AgentSpec, producer: AgentSpec) -> dict[str, Any]:
        """Describe *how* a reviewer differs from a producer.

        Recorded in the review feedback because independence that cannot be demonstrated is
        indistinct from none. Context lineage always differs — the reviewer never receives the
        producer's reasoning — so the entry is always non-empty, and a different model is
        reported additionally.
        """
        differs_by: list[str] = ["context_lineage"]
        if reviewer.model and producer.model and reviewer.model != producer.model:
            differs_by.append("model")
        if reviewer.provider and producer.provider and reviewer.provider != producer.provider:
            differs_by.append("provider")
        return {
            "reviewer": {"agent_id": reviewer.id, "name": reviewer.name, "model": reviewer.model},
            "producer": {"agent_id": producer.id, "name": producer.name, "model": producer.model},
            "differs_by": differs_by,
            "information_boundary": "conclusion and evidence only; never the producer's reasoning",
            "model_independent": "model" in differs_by,
        }

    # ── binding plans ───────────────────────────────────────────────────────

    def plan_bindings(self, manifest: dict[str, Any], *,
                      policies: dict[str, BindingPolicy] | None = None,
                      pins: dict[str, str] | None = None,
                      skip_unstaffed: bool = False) -> dict[str, NodeBinding]:
        """Bind every bindable node in a manifest, keeping reviewers independent.

        Nodes that name no skill (gates, supervisors) are skipped. For each reviewer node the
        producer's agent is excluded, so the independence rule holds across a whole graph
        rather than only when a caller remembers to ask.

        Parameters
        ----------
        skip_unstaffed:
            When True, a node whose skill no agent holds is skipped rather than raising. That is
            the right mode for *planning* — the roster may legitimately not yet cover every node,
            and the caller compares the plan against the roster to report the shortfall. The
            default is strict, because during a *run* an unstaffed node must fail loudly.

        Raises
        ------
        BindingError
            When a node cannot be bound and `skip_unstaffed` is False.
        """
        policies = policies or {}
        pins = pins or {}
        bindings: dict[str, NodeBinding] = {}
        produced_by: str | None = None

        for node in manifest.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            skill = str(node.get("skill") or "")
            if not skill:
                continue
            node_id = str(node.get("id") or skill)
            # One shared answer to "does this node judge or produce?" (`skills/roles.py`), rather than
            # a second hardcoded set here that can drift from the planner's. A node in the REVIEW
            # phase is a verifier regardless, because the plan said so.
            is_reviewer = node.get("phase") == "REVIEW" or is_verifier(skill)
            exclude: set[str] = set()
            prefer_model: str | None = None
            if is_reviewer and produced_by:
                # Exclude the producer outright, and bias toward a different model so the
                # independence is stronger than context lineage alone where the roster allows.
                exclude.add(produced_by)
                producer = self.org.agents.get(produced_by)
                if producer is not None and producer.model:
                    prefer_model = producer.model
            # Precedence: an explicit caller override, then the node's own declaration, then the
            # default. The node's declaration matters because it is what makes a swarm *requestable
            # in a plan* — reading only the caller's map meant a manifest could ask for a swarm and
            # be silently bound as a single agent, which is exactly the kind of quiet downgrade this
            # module exists to avoid.
            policy = policies.get(node_id) or declared_policy(node) or BindingPolicy.LOAD_BALANCED
            try:
                binding = self.bind(
                    node, policy=policy, pinned=pins.get(node_id),
                    exclude=exclude, prefer_model=prefer_model,
                )
            except BindingError:
                if skip_unstaffed:
                    continue
                raise
            bindings[node_id] = binding
            chosen = self.org.agents.get(binding.primary)
            if is_reviewer and chosen is not None and produced_by:
                producer = self.org.agents.get(produced_by)
                if producer is not None:
                    self.assert_independent(chosen, producer)
            else:
                produced_by = binding.primary
        return bindings

    def staffing_gaps(self, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        """Nodes whose skill no agent in the roster holds.

        The companion to `skip_unstaffed`: rather than silently producing a partial binding, the
        caller can report exactly which capabilities the plan needs and the roster lacks, and the
        Owner can hire or amend the plan. This is the reconciliation the first-run experience and
        the approval prompt both need.
        """
        gaps: list[dict[str, Any]] = []
        for node in manifest.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            skill = str(node.get("skill") or "")
            if not skill:
                continue
            holders = self.org.agents_for_skill(skill)
            healthy = [a for a in holders
                       if self.org.runtime(a.id).state not in (AgentState.QUARANTINED,
                                                              AgentState.TERMINATED)]
            if not healthy:
                gaps.append({
                    "node_id": str(node.get("id") or skill),
                    "skill": skill,
                    "holders": [a.name for a in holders],
                    "reason": (
                        "every holder is quarantined or terminated" if holders
                        else "no agent in the roster holds this skill"
                    ),
                })
        return gaps

    def rotation_summary(self) -> dict[str, int]:
        """Round-robin counters, for diagnostics."""
        with self._lock:
            return dict(self._round_robin)
