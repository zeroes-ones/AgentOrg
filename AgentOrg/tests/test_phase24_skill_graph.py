#!/usr/bin/env python3
"""Phase 24 tests — the library's chain: dependency graph, made usable.

Every skill declares `consumes_from` / `feeds_into`. The engine parsed those fields and never read
them, so planning threw away the library's own knowledge of which procedure depends on which. These
tests pin the reader: the edges, the traversals, and the *calibrated* plan review that survives a
deliberately dense graph.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.library import resolve
from engine.skills import FilesystemSkillSource
from engine.skills.graph import FRAMEWORK_SKILLS, GraphError, SkillGraph


@pytest.fixture(scope="module")
def source():
    return FilesystemSkillSource(resolve())


@pytest.fixture(scope="module")
def graph(source):
    return SkillGraph(source)


# ── a stub source, so the graph logic is tested without the corpus ───────────


class _StubBundle:
    def __init__(self, consumes=(), feeds=()):
        self.consumes_from = tuple(consumes)
        self.feeds_into = tuple(feeds)


class _StubSource:
    """A tiny graph: a -> b -> c, plus a mutual pair d <-> e and an orphan f."""

    def __init__(self):
        self._skills = {
            # Both a and b consume c, so omitting c from a plan that runs a and b is a consensus gap.
            "a": _StubBundle(consumes=["b", "c"], feeds=["b"]),
            "b": _StubBundle(consumes=["c"], feeds=["a", "c"]),
            "c": _StubBundle(consumes=[], feeds=["b"]),
            "d": _StubBundle(consumes=["e"], feeds=["e"]),
            "e": _StubBundle(consumes=["d"], feeds=["d"]),
            "f": _StubBundle(),
        }

    def names(self):
        return sorted(self._skills)

    def load(self, name):
        if name not in self._skills:
            raise KeyError(name)
        return self._skills[name]


# ── edges ────────────────────────────────────────────────────────────────────


def test_upstream_and_downstream_read_the_chain(graph):
    # The corpus is real, so assert shape rather than an exact list that would rot.
    assert graph.upstream("code-reviewer")
    assert graph.downstream("backend-developer")
    assert "backend-developer" in graph.upstream("code-reviewer")


def test_an_unknown_skill_has_no_edges_and_does_not_raise(graph):
    """`consumes_from` routinely names meta-procedures; an unreadable skill must contribute none."""
    assert graph.upstream("no-such-skill-xyz") == []
    assert graph.downstream("no-such-skill-xyz") == []


def test_framework_skills_are_filtered_by_default(graph):
    raw = graph.upstream("code-reviewer", include_framework=True)
    filtered = graph.upstream("code-reviewer")
    assert any(s in FRAMEWORK_SKILLS for s in raw), "the corpus should reference a framework skill"
    assert not any(s in FRAMEWORK_SKILLS for s in filtered)
    assert len(filtered) <= len(raw)


def test_edges_are_sorted_and_deduplicated(graph):
    ups = graph.upstream("backend-developer")
    assert ups == sorted(ups)
    assert len(ups) == len(set(ups))


# ── traversal, on the stub so the answer is exact ────────────────────────────


def test_closure_follows_a_chain():
    g = SkillGraph(_StubSource())
    assert g.closure(["a"], direction="upstream") == ["b", "c"]
    assert g.closure(["c"], direction="downstream") == ["a", "b"]


def test_closure_can_be_bounded_by_depth():
    g = SkillGraph(_StubSource())
    # depth=1 from `a` yields its direct dependencies only: b and c.
    assert g.closure(["a"], direction="upstream", depth=1) == ["b", "c"]


def test_closure_refuses_an_unknown_direction():
    with pytest.raises(GraphError, match="direction"):
        SkillGraph(_StubSource()).closure(["a"], direction="sideways")


def test_topological_order_puts_dependencies_first():
    g = SkillGraph(_StubSource())
    ordered, cyclic = g.topological_order(["a", "b", "c"])
    assert cyclic == []
    assert ordered.index("c") < ordered.index("b") < ordered.index("a")


def test_a_cycle_is_reported_not_dropped():
    """A mutual pair is a real property of the corpus; a plan must not lie about its order."""
    g = SkillGraph(_StubSource())
    ordered, cyclic = g.topological_order(["d", "e"])
    assert cyclic == ["d", "e"]
    assert ordered == []


def test_topological_order_is_deterministic():
    g = SkillGraph(_StubSource())
    assert g.topological_order(["a", "b", "c"]) == g.topological_order(["a", "b", "c"])


# ── the calibrated plan review ───────────────────────────────────────────────


def test_coherence_counts_in_plan_neighbours():
    g = SkillGraph(_StubSource())
    review = g.plan_review(["a", "b", "c"])
    # a-b, b-c, a-c via feeds: every node is related to at least one other.
    assert review["coherence"]["b"] >= 2
    assert review["isolated"] == []


def test_an_isolated_skill_is_flagged():
    g = SkillGraph(_StubSource())
    review = g.plan_review(["a", "b", "f"])
    assert "f" in review["isolated"]


def test_consensus_missing_finds_the_shared_prerequisite():
    g = SkillGraph(_StubSource())
    # Both a and b consume c; if the plan omits c, c is a consensus prerequisite.
    review = g.plan_review(["a", "b"], min_consensus=2)
    skills = [entry["skill"] for entry in review["consensus_missing"]]
    assert "c" in skills


def test_plan_review_on_the_real_corpus_is_not_noise(graph):
    """The calibrated view must be a short list, not the hundreds a raw gap check would give."""
    plan = ["product-manager", "system-architect", "api-designer", "backend-developer",
            "code-reviewer", "qa-engineer"]
    review = graph.plan_review(plan, min_consensus=3)
    assert review["isolated"] == [], "a real software plan's nodes are all related to each other"
    assert 0 < len(review["consensus_missing"]) <= 20
    assert all(entry["demanded_by"] >= 3 for entry in review["consensus_missing"])


def test_stats_count_the_whole_graph(graph):
    stats = graph.stats()
    assert stats.skills > 300
    assert stats.edges > 1000
    assert stats.orphans == 0


def test_edge_map_is_deterministic(graph):
    first = graph.edge_map()
    second = graph.edge_map()
    assert list(first) == sorted(first)
    assert first == second


def test_explain_reports_both_directions(graph):
    explained = graph.explain("code-reviewer")
    assert explained["skill"] == "code-reviewer"
    assert explained["upstream_count"] > 0
    assert explained["downstream_count"] > 0
