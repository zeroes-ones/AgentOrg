#!/usr/bin/env python3
"""graph.py — the library's own skill dependency graph, made usable.

WHY THIS EXISTS
---------------
Every skill in the library declares, in its `chain:` frontmatter, what it `consumes_from` (upstream
procedures whose output it needs) and what it `feeds_into` (downstream procedures that consume its
output). Across the corpus that is a real dependency graph — thousands of symmetric edges — and the
library's own `workflow-graph-authoring` skill says plainly that node selection should use it:

    "node selection from the chain: graph"

The engine parsed those fields into `SkillBundle.consumes_from` / `feeds_into` and then never read
them. Planning composed a graph from hand-written tables, so the library's knowledge about *which
procedure depends on which* — the thing that makes a plan defensible rather than ad hoc — was thrown
away one parse after it was read.

This module is the missing reader. It answers the questions a planner, a reviewer or a person
actually asks of the graph:

- **What does this skill depend on?** — `upstream(name)`
- **What depends on it?** — `downstream(name)`
- **What is the right order to run a set of skills in?** — `topological_order(names)`
- **What does this skill need that the plan left out?** — `missing_upstream(names)`
- **What is the transitive closure (all prerequisites) of this work?** — `closure(names)`

DESIGN
------
- **Lazy and cached per skill.** Reading one skill's edges loads one skill. Only the full-graph
  operations (`edge_map`, `stats`) load the corpus, and they are the ones a person invokes
  deliberately. This matters because a plan touches a handful of skills, not 327.
- **Cycles are reported, not smoothed over.** The corpus is largely a DAG but a dependency graph of
  real procedures has mutual references (`code-reviewer` ↔ `backend-developer`). `topological_order`
  returns the cyclic remainder explicitly rather than silently dropping it — a plan that hides a cycle
  is a plan that lies about its order.
- **An unknown skill is a value, not an error.** `consumes_from` routinely names skills that are
  meta-procedures (the library's own `using-agent-skills`, `skill-levels`) or that live in the
  framework domain. `upstream` returns whatever the skill declares; a consumer decides what to do
  about an edge to something outside its set.
- **Deterministic.** Every set-returning method sorts, and `topological_order` breaks ties by name,
  so two runs over the same corpus produce byte-identical order. A planner that reordered between
  runs would make every plan unreviewable.

Usage:
    graph = SkillGraph(source)
    graph.upstream("code-reviewer")            # ['api-designer', 'backend-developer', ...]
    graph.missing_upstream(["backend-developer", "code-reviewer"])
    graph.topological_order(["backend-developer", "code-reviewer", "qa-engineer"])
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = ["SkillGraph", "GraphError", "GraphStats"]


class GraphError(RuntimeError):
    """Raised when the graph cannot be read — never for an edge to an unknown skill."""


#: Skills that are meta-procedures rather than executable work: how to use the library, how skills
#: are levelled, the portability contract. They appear as upstream of nearly everything and are not
#: work a plan should schedule, so a caller that wants "real" prerequisites can filter them out.
FRAMEWORK_SKILLS: frozenset[str] = frozenset({
    "using-agent-skills", "skill-levels", "agent-support-matrix", "context-engineering",
    "workflow-graph-authoring", "iterative-task-execution", "using-all-skills",
})


@dataclass
class GraphStats:
    """A summary of the corpus graph, for `skills graph`."""

    skills: int = 0
    edges: int = 0
    with_edges: int = 0
    orphans: int = 0
    cycles: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "skills": self.skills, "edges": self.edges, "with_edges": self.with_edges,
            "orphans": self.orphans, "cycles": self.cycles,
        }


@dataclass
class SkillGraph:
    """The library's `chain:` graph, read lazily from a skill source.

    Parameters
    ----------
    source:
        A `SkillSource`. Only `load` and `names` are used, so any source works — the filesystem one,
        the overlay, or a stub in a test.
    """

    source: Any
    #: Per-skill edge cache: name -> (consumes_from, feeds_into). `None` once a skill is known to be
    #: unloadable, so a missing skill is not re-attempted on every call.
    _edges: dict[str, tuple[tuple[str, ...], tuple[str, ...]] | None] = field(
        default_factory=dict, repr=False)

    # ── edges ───────────────────────────────────────────────────────────────

    def _edges_of(self, name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(consumes_from, feeds_into) for one skill, cached. Empty for an unknown skill.

        An unloadable skill yields empty edges rather than raising: `consumes_from` often names
        meta-procedures, and a plan that refused because it referenced one would be unable to use
        most of the library.
        """
        if name in self._edges:
            cached = self._edges[name]
            return cached if cached is not None else ((), ())
        try:
            bundle = self.source.load(name)
            edges = (tuple(str(s) for s in bundle.consumes_from),
                     tuple(str(s) for s in bundle.feeds_into))
        except Exception:  # noqa: BLE001 - an unreadable skill contributes no edges
            edges = ((), ())
        self._edges[name] = edges if edges != ((), ()) else None
        return edges

    def upstream(self, name: str, *, include_framework: bool = False) -> list[str]:
        """What this skill consumes from — its declared dependencies. Sorted, de-duplicated."""
        consumes, _ = self._edges_of(name)
        return _filter(sorted(set(consumes)), include_framework)

    def downstream(self, name: str, *, include_framework: bool = False) -> list[str]:
        """What consumes this skill's output. Sorted, de-duplicated."""
        _, feeds = self._edges_of(name)
        return _filter(sorted(set(feeds)), include_framework)

    def neighbours(self, name: str) -> list[str]:
        """Both directions, sorted — the skill's immediate neighbourhood."""
        consumes, feeds = self._edges_of(name)
        return sorted(set(consumes) | set(feeds))

    # ── traversal ───────────────────────────────────────────────────────────

    def closure(self, names: Iterable[str], *, direction: str = "upstream",
                depth: int | None = None, include_framework: bool = False) -> list[str]:
        """Every skill transitively reachable from `names` in one direction.

        Upstream is the interesting direction for planning: it is "everything this work depends on".
        The result excludes the seed names themselves, so it reads as *prerequisites*, and it is
        sorted for determinism. `depth` bounds the walk so a caller can ask for direct deps only
        (`depth=1`) without a separate method.
        """
        if direction not in ("upstream", "downstream"):
            raise GraphError(f"direction must be 'upstream' or 'downstream', not {direction!r}")
        seeds = [str(n) for n in names]
        seen: set[str] = set(seeds)
        frontier = deque((seed, 0) for seed in seeds)
        found: set[str] = set()
        while frontier:
            current, level = frontier.popleft()
            if depth is not None and level >= depth:
                continue
            children = (self.upstream(current) if direction == "upstream"
                        else self.downstream(current))
            for child in children:
                found.add(child)
                if child not in seen:
                    seen.add(child)
                    frontier.append((child, level + 1))
        found -= set(seeds)
        return _filter(sorted(found), include_framework)

    def topological_order(self, names: Iterable[str], *,
                          include_framework: bool = False) -> tuple[list[str], list[str]]:
        """Order a set of skills so every dependency precedes its dependents.

        Returns ``(ordered, cyclic)``. `ordered` is a deterministic Kahn ordering over the *induced*
        subgraph — edges to skills outside `names` are ignored, because a plan can only order the
        nodes it has. `cyclic` is the set of skills left over because they are in a cycle, returned
        rather than dropped: a mutual pair (`code-reviewer` ↔ `backend-developer`) is a real property
        of the corpus, and a plan that silently discarded one would misreport its own order.
        """
        wanted = [str(n) for n in names]
        wanted_set = set(wanted)
        # Induced adjacency: dependents -> the dependencies that are also in the set.
        deps: dict[str, set[str]] = {name: set() for name in wanted}
        for name in wanted:
            for up in self.upstream(name):
                if up in wanted_set and up != name:
                    deps[name].add(up)
        ordered: list[str] = []
        remaining = dict(deps)
        while remaining:
            ready = sorted(n for n, d in remaining.items() if not d)
            if not ready:
                break
            for node in ready:
                ordered.append(node)
                remaining.pop(node, None)
            for d in remaining.values():
                d -= set(ready)
        cyclic = sorted(remaining)
        return _filter(ordered, include_framework), cyclic

    def missing_upstream(self, names: Iterable[str], *,
                         include_framework: bool = False) -> dict[str, list[str]]:
        """For each skill, the declared dependencies that are not in `names` and exist in the library.

        This is the diagnostic a plan wants: *you scheduled the developer and the reviewer, but the
        API designer they both consume from is not in the graph.* Only skills the library actually
        has are reported, so a reference to a genuinely absent name is not a false alarm; and the
        framework meta-skills are excluded by default because depending on `using-agent-skills` is
        not a planning gap.
        """
        wanted = [str(n) for n in names]
        wanted_set = set(wanted)
        available = self._available()
        gaps: dict[str, list[str]] = {}
        for name in wanted:
            missing = []
            for up in self.upstream(name):
                if up in wanted_set or up == name:
                    continue
                if not include_framework and up in FRAMEWORK_SKILLS:
                    continue
                # An edge to a skill the library does not have is a library concern, not a plan gap.
                if available is not None and up not in available:
                    continue
                missing.append(up)
            if missing:
                gaps[name] = sorted(set(missing))
        return gaps

    # ── the whole graph ─────────────────────────────────────────────────────

    def _available(self) -> set[str] | None:
        """The set of skills the source can provide, or None when it cannot enumerate."""
        try:
            return set(self.source.names())
        except Exception:  # noqa: BLE001
            return None

    def edge_map(self) -> dict[str, list[str]]:
        """`feeds_into` for every skill — the whole graph as an adjacency map.

        Loads the corpus, so it is the deliberate, whole-graph call (`skills graph`, an export), not
        something a planner does. Deterministic: sorted keys and sorted values.
        """
        names = self._available()
        if names is None:
            raise GraphError("this source cannot enumerate its skills, so the whole graph is unknown")
        graph: dict[str, list[str]] = {}
        for name in sorted(names):
            graph[name] = self.downstream(name)
        return graph

    def stats(self) -> GraphStats:
        """Corpus-level counts: skills, edges, orphans, and how many cycles exist."""
        graph = self.edge_map()
        skills = len(graph)
        edges = sum(len(v) for v in graph.values())
        with_edges = sum(1 for v in graph.values() if v)
        orphans = sum(1 for name, v in graph.items()
                      if not v and not self.upstream(name))
        # Count a cycle as a mutual (or longer) dependency among a *sample* of the graph. A full
        # strongly-connected-component pass over 327 nodes is cheap, but doing it here keeps stats a
        # summary rather than a graph algorithm the caller did not ask for.
        _, cyclic = self.topological_order(sorted(graph))
        return GraphStats(skills=skills, edges=edges, with_edges=with_edges,
                          orphans=orphans, cycles=len(cyclic))

    def explain(self, name: str, *, limit: int = 12) -> dict[str, Any]:
        """A readable account of one skill's place in the graph."""
        return {
            "skill": name,
            "upstream": self.upstream(name)[:limit],
            "downstream": self.downstream(name)[:limit],
            "upstream_count": len(self.upstream(name)),
            "downstream_count": len(self.downstream(name)),
            "prerequisites": self.closure([name], direction="upstream", depth=1)[:limit],
        }

    # ── the calibrated, plan-oriented view ──────────────────────────────────
    #
    # A raw `missing_upstream` over this corpus is not useful: the graph is deliberately dense (a
    # producer and its reviewer reference each other; nearly every skill lists ~40 upstream), so the
    # raw gap set is hundreds of names and says nothing about *this* plan. Two signals survive the
    # density and are what a planner can actually act on:
    #
    #   1. **Coherence** — how many of a plan's own nodes the library relates to each other. A node
    #      with *zero* in-plan neighbours is a skill the corpus says is unrelated to everything else
    #      being run; that is a real "these belong in different runs" warning.
    #   2. **Consensus prerequisites** — a skill that many of the plan's nodes declare they consume,
    #      which the plan does not include. That is the "everyone here needs X and X is missing"
    #      signal, which is exactly the kind of gap a person misses.

    def plan_review(self, names: Iterable[str], *, min_consensus: int = 2,
                    include_framework: bool = False) -> dict[str, Any]:
        """How well a set of skills hangs together, per the library's own graph.

        Returns a dict with ``coherence`` (per-node count of in-plan neighbours, and the unconnected
        ones), ``consensus_missing`` (upstream skills several plan nodes declare but the plan omits,
        most-demanded first) and ``isolated`` (nodes with no in-plan neighbour at all).
        """
        plan = [str(n) for n in names]
        plan_set = set(plan)
        available = self._available()
        coherence: dict[str, int] = {}
        for name in plan:
            coherence[name] = len(set(self.neighbours(name)) & plan_set)
        isolated = sorted(n for n, c in coherence.items() if c == 0 and len(plan) > 1)

        # Consensus prerequisites: count how many plan nodes declare each external skill upstream.
        demand: dict[str, int] = {}
        for name in plan:
            for up in set(self.upstream(name, include_framework=include_framework)):
                if up in plan_set or up == name:
                    continue
                if not include_framework and up in FRAMEWORK_SKILLS:
                    continue
                if available is not None and up not in available:
                    continue
                demand[up] = demand.get(up, 0) + 1
        consensus = sorted(
            ((skill, count) for skill, count in demand.items() if count >= min_consensus),
            key=lambda pair: (-pair[1], pair[0]),
        )

        return {
            "skills": plan,
            "coherence": coherence,
            "isolated": isolated,
            "consensus_missing": [{"skill": s, "demanded_by": c} for s, c in consensus[:20]],
            "min_consensus": min_consensus,
        }


def _filter(names: list[str], include_framework: bool) -> list[str]:
    """Drop the framework meta-skills unless asked for them."""
    if include_framework:
        return names
    return [n for n in names if n not in FRAMEWORK_SKILLS]
