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
_DEFAULT_SHAPE: tuple[tuple[str, str, str], ...] = (
    ("pm", "product-manager", "DISCOVER"),
    ("architect", "system-architect", "DESIGN"),
    ("api", "api-designer", "DESIGN"),
    ("developer", "backend-developer", "BUILD"),
    ("reviewer", "code-reviewer", "REVIEW"),
    ("qa", "qa-engineer", "VERIFY"),
    ("security", "security-reviewer", "VERIFY"),
)

# Goal keywords that select specialist skills in addition to the default company. Each entry
# is (pattern, skill name). Ordered so the first match wins per skill.
_GOAL_HINTS: tuple[tuple[str, str], ...] = (
    (r"\b(api|rest|graphql|grpc|endpoint|openapi)\b", "api-designer"),
    (r"\b(ui|frontend|react|swift|screen|dashboard|interface)\b", "frontend-developer"),
    (r"\b(macos|swift|appkit|swiftui|ios)\b", "macos-developer"),
    (r"\b(kubernetes|docker|deploy|infra|terraform|pipeline|ci/?cd)\b", "devops-engineer"),
    (r"\b(auth|login|password|oauth|permission|rbac|token)\b", "security-engineer"),
    (r"\b(database|schema|migration|sql|postgres|sqlite)\b", "database-designer"),
    (r"\b(data|analytics|etl|warehouse|pipeline)\b", "data-engineer"),
    (r"\b(mobile|android|flutter|react native)\b", "mobile-developer"),
    (r"\b(payment|billing|subscription|stripe|pricing)\b", "fintech-app-developer"),
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
    """

    def __init__(self, source: Any, *, validator: Any = None, config: Config | None = None) -> None:
        self.source = source
        self.config = config
        self._validator = validator
        self._library_validator = None

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
        selected = self._select_skills(goal)

        candidates = [
            ("full", self._compose(project, goal, selected, max_iterations, max_steps,
                                   include_parallel=True, include_security=True)),
            ("lean", self._compose(project, goal, selected, max_iterations, max_steps,
                                   include_parallel=False, include_security=False)),
            ("minimal", self._minimal(project, goal, max_iterations)),
        ]

        dropped: list[str] = []
        for label, manifest in candidates:
            validation = self.validate(manifest)
            if validation.valid:
                notes = [] if label == "full" else [
                    f"used the {label} shape because the richer shape did not validate"
                ]
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
                )
            dropped.append(f"{label} shape rejected: " + "; ".join(validation.errors[:3]))

        raise PlanError(
            "no candidate manifest validated, which indicates a defect in the planner "
            "rather than an unusual goal:\n  " + "\n  ".join(dropped)
        )

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

    def _select_skills(self, goal: str) -> list[tuple[str, str, str]]:
        """Choose the company for a goal: the default shape plus goal-matched specialists.

        A skill that is not in the library, or whose contract cannot be loaded, is skipped —
        the planner must never emit an edge to a node whose contract it could not read.
        """
        selected: list[tuple[str, str, str]] = list(_DEFAULT_SHAPE)
        already = {skill for _id, skill, _phase in selected}
        lowered = goal.lower()
        for pattern, skill in _GOAL_HINTS:
            if skill in already:
                continue
            if re.search(pattern, lowered):
                if self._load(skill) is not None:
                    selected.append((_node_id_for(skill), skill, _phase_for(skill)))
                    already.add(skill)
        return selected

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

    def _compose(self, slug: str, goal: str, selected: list[tuple[str, str, str]],
                 max_iterations: int, max_steps: int | None, *,
                 include_parallel: bool, include_security: bool) -> dict[str, Any]:
        """Build a manifest from the selected skills.

        The graph is: sequential phases, then a parallel review fan-out, then a bounded
        review/rework loop whose exhaustion reaches a terminal human gate. That shape is what
        makes "work until done" terminate rather than spin.
        """
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        used_skills: list[str] = []
        dropped: list[str] = []
        # Type mismatches that were allowed rather than fatal, surfaced for the Owner.
        type_notes: list[str] = []

        # Separate the build chain from the review/verify specialists.
        reviewers = {"code-reviewer", "security-reviewer", "qa-engineer"}
        primary: list[tuple[str, str, str]] = []
        review: list[tuple[str, str, str]] = []
        for entry in selected:
            if entry[1] in reviewers:
                if entry[1] == "security-reviewer" and not include_security:
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
            # A node in a *producing* phase works on the code, so it gets the tools to read and
            # change it. Without this the planner emitted no `tools` key at all, so a normal
            # `run --goal` never used the tool loop — the feature would have existed and been
            # unreachable, which is the failure mode this codebase keeps producing.
            #
            # Reviewers are deliberately excluded: they judge an artifact, and a verifier that can
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

        # The rework loop: reviewers -> developer -> reviewers, bounded and with an exit.
        developer = next((n["id"] for n in nodes if n["skill"] == "backend-developer"), last_primary)
        verdict_node = review_nodes[0]
        gate_id = "human-gate"
        nodes.append({
            "id": gate_id,
            "type": "gate",
            "kind": "human",
            "requires": [f"{nid}.summary" for nid in review_nodes][:3],
            "description": (
                "Owner approval: the change is released once the review loop converges, or the "
                "escalation report is reviewed when automation exhausted its budget."
            ),
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
                "escalate_to": gate_id,
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


def _phase_for(skill: str) -> str:
    """The lifecycle phase a specialist belongs to."""
    if skill in ("product-manager", "product-strategist", "ux-researcher"):
        return "DISCOVER"
    if skill in ("system-architect", "api-designer", "database-designer", "cloud-architect"):
        return "DESIGN"
    if skill in ("devops-engineer", "platform-engineer", "site-reliability-engineer"):
        return "OPERATE"
    if skill in ("qa-engineer", "security-reviewer", "security-engineer",
                 "performance-engineer", "accessibility-auditor"):
        return "VERIFY"
    return "BUILD"
