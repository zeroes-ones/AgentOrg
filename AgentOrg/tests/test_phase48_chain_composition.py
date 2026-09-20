#!/usr/bin/env python3
"""Phase 48 — the library's `chain:` graph drives composition, with a recorded fallback.

WHY THIS EXISTS
---------------
`engine/skills/graph.py` made the library's `chain:` edges readable, and `_graph_review` used them to
*describe* a plan — but composition itself never asked. The build chain came from hand-written
tables in `planner.py` and the edges were wired **by list index**:

    for index in range(len(nodes) - 1):
        producer, consumer = nodes[index], nodes[index + 1]

So the corpus's own answer was available and ignored. For `gtm` the graph says
`growth-engineer` precedes `product-manager`; the planner emitted the reverse. Worse, the corpus
declares `api-designer` and `backend-developer` mutually dependent, so for the *default* software
shape there is no graph order at all — a planner that imposed one anyway would report an order the
library contradicts.

Separately, the artifact-type mismatch check ran and then its results were discarded.
`_handoff_compatible` appended to a local `type_notes`, `_compose` copied that into
`self._last_dropped`, and `Plan.dropped` was built from a *different* local list. `_last_dropped` has
no reader anywhere. On an ordinary booking-SaaS plan it held seven real mismatches while
`plan.dropped == ()`.

These tests pin the three properties that fix it:

1. **A type mismatch is visible on the plan** — `Plan.type_notes` is populated, and `summary()` and
   `as_dict()` both report it. A plan whose handoffs are untyped must say so.
2. **A DAG domain is ordered by the graph** — the emitted chain matches
   `SkillGraph.topological_order`, and the plan records that it did.
3. **A cyclic domain falls back and records why** — the declared order is kept, the cycle is named in
   `Plan.order_notes`, and nothing raises.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.library import resolve
from engine.planner import _DOMAIN_SHAPES, Planner
from engine.skills import FilesystemSkillSource
from engine.skills.graph import SkillGraph


@pytest.fixture(scope="module")
def source():
    return FilesystemSkillSource(resolve())


@pytest.fixture(scope="module")
def planner(source):
    return Planner(source)


@pytest.fixture(scope="module")
def graph(source):
    return SkillGraph(source)


def _chain_skills(plan) -> list[str]:
    """The skills of the sequential build chain, in manifest order.

    The verifiers are appended after the chain and carry their own phase, so the chain is the nodes
    that are not the parallel review fan-out's members.
    """
    review = set()
    for group in plan.manifest.get("parallel") or []:
        review.update(group.get("nodes") or [])
    return [n["skill"] for n in plan.nodes if n.get("skill") and n["id"] not in review]


# ── 1. type notes reach the Owner ────────────────────────────────────────────


def test_a_type_mismatch_is_surfaced_on_the_plan(planner):
    """The regression: mismatches were computed, stashed on an unread attribute, and lost."""
    plan = planner.plan("Build a booking SaaS MVP with auth and payments", slug="typed")
    assert plan.type_notes, "an ordinary software plan has real artifact mismatches to report"
    assert any("declares outputs" in note or "expects" in note for note in plan.type_notes)


def test_no_attribute_carries_them_instead(planner):
    """The old `_last_dropped` carrier must be gone, or the two lists can drift again."""
    plan = planner.plan("Build a booking SaaS MVP with auth and payments", slug="typed-attr")
    assert not hasattr(planner, "_last_dropped"), (
        "_last_dropped had no reader; the notes travel on the Plan instead"
    )
    assert plan.type_notes


def test_the_type_notes_are_in_the_owner_facing_summary(planner):
    """A plan's summary is the approval prompt, so an unmentioned mismatch is an unseen one."""
    plan = planner.plan("Build a booking SaaS MVP with auth and payments", slug="typed-summary")
    summary = plan.summary()
    assert "type mismatch" in summary
    for note in plan.type_notes[:3]:
        assert note in summary


def test_the_type_notes_travel_in_the_serialised_plan(planner):
    plan = planner.plan("Design a REST API with a Postgres schema", slug="typed-json")
    payload = plan.as_dict()
    assert payload["type_notes"], "the event stream must carry them too"
    assert payload["type_notes"] == list(plan.type_notes)
    assert isinstance(payload["order_notes"], list)


def test_a_fully_typed_handoff_is_not_reported_as_a_mismatch(planner):
    """The notes must mean something: a matching pair is not a mismatch.

    `_handoff_compatible` is the predicate, so assert it directly on a producer/consumer whose
    declarations do intersect and one whose do not.
    """
    matching = {"id": "p", "outputs": ["change"]}, {"id": "c", "inputs": ["change"]}
    disjoint = {"id": "p", "outputs": ["spec"]}, {"id": "c", "inputs": ["findings"]}
    assert planner._handoff_compatible(*matching)
    assert not planner._handoff_compatible(*disjoint)


# ── 2. a DAG domain is ordered by the library's graph ────────────────────────


def test_a_dag_domain_is_ordered_topologically(planner, graph):
    """`gtm`'s skills form a DAG, so the corpus's order must be the emitted order."""
    plan = planner.plan("Plan a go-to-market launch and capture demand for the app", slug="dag-gtm")
    assert plan.shape == "gtm"
    chain = _chain_skills(plan)
    ordered, cyclic = graph.topological_order(chain, include_framework=True)
    assert cyclic == [], "the premise of this test is that gtm has no cycle"
    assert chain == ordered, f"the chain should follow the graph: {chain} != {ordered}"


def test_a_data_goal_is_ordered_topologically(planner, graph):
    plan = planner.plan("Build a data warehouse ETL pipeline and a dashboard", slug="dag-data")
    chain = _chain_skills(plan)
    ordered, cyclic = graph.topological_order(chain, include_framework=True)
    assert cyclic == []
    assert chain == ordered


def test_the_graph_order_is_recorded_on_the_plan(planner):
    plan = planner.plan("Plan a go-to-market launch and capture demand for the app", slug="dag-note")
    assert plan.order_notes, "the plan should say where its order came from"
    assert any("chain:` graph" in note for note in plan.order_notes)
    assert any("ordered" in note and "kept" not in note for note in plan.order_notes)


def test_the_emitted_order_never_puts_a_dependent_before_its_dependency(planner, graph):
    """The property that matters, stated without naming the algorithm.

    For every in-set edge the graph declares, the producer must come first in the manifest — on a
    domain where that is achievable at all.
    """
    plan = planner.plan("Plan a go-to-market launch and capture demand for the app", slug="dag-order")
    chain = _chain_skills(plan)
    position = {skill: index for index, skill in enumerate(chain)}
    in_set = set(chain)
    for skill in chain:
        for dependency in graph.upstream(skill, include_framework=True):
            if dependency in in_set and dependency != skill:
                assert position[dependency] < position[skill], (
                    f"{dependency} must precede {skill}, which it declares it consumes from"
                )


# ── 3. a cyclic domain falls back, and says why ──────────────────────────────


def test_a_cyclic_domain_falls_back_to_the_declared_order(planner, graph):
    """The software shape contains `api-designer` <-> `backend-developer`, so there is no DAG."""
    goal = "Build a booking SaaS MVP with auth and payments"
    plan = planner.plan(goal, slug="cycle-software")
    chain = _chain_skills(plan)
    _, cyclic = graph.topological_order(chain, include_framework=True)
    assert cyclic, "the premise of this test is that the software chain has a cycle"
    # The declared order is what `_select_skills` produced, with the verifiers removed by `_compose`.
    verifiers = {skill for _id, skill, _phase in _DOMAIN_SHAPES["software"]["verify"]}
    declared = [skill for _id, skill, _phase in planner._select_skills(goal, "software")
                if skill not in verifiers]
    assert chain == declared, (
        "a cyclic domain must keep the declared order rather than have one imposed"
    )


def test_the_cycle_is_named_in_the_order_notes(planner):
    plan = planner.plan("Build a booking SaaS MVP with auth and payments", slug="cycle-note")
    assert plan.order_notes, "the fallback must be recorded, not silent"
    reason = " ".join(plan.order_notes)
    assert "cycle" in reason
    assert "api-designer" in reason or "backend-developer" in reason


def test_a_cyclic_domain_still_produces_a_valid_terminating_plan(planner):
    """Falling back must not break the invariants: the same graph, only ordered as declared."""
    plan = planner.plan("Build a booking SaaS MVP with auth and payments", slug="cycle-valid")
    assert plan.validation.valid, plan.validation.errors
    assert plan.loops and plan.loops[0]["exit_when"]
    assert plan.loops[0]["max_iterations"] >= 1
    resolvable = {n["id"] for n in plan.nodes} | {g["id"] for g in plan.gates}
    assert all(e["from"] in resolvable and e["to"] in resolvable for e in plan.manifest["edges"])


def test_the_fallback_reason_appears_in_the_summary(planner):
    plan = planner.plan("Build a booking SaaS MVP with auth and payments", slug="cycle-summary")
    assert "Chain order:" in plan.summary()


# ── the fallback is a real dependency on the graph, not a coincidence ────────


def test_an_unreadable_graph_keeps_the_declared_order(source):
    """A source with no readable edges must NOT be alphabetised into a false dependency order.

    This is why `_order_chain` requires at least one in-set edge: with no edges at all, Kahn emits
    the nodes sorted by name, which for a real chain is a silent reordering for no reason.
    """

    class BlindSource:
        """Resolves names but declares no chain edges, and cannot load a skill."""

        library_root = None

        def names(self):
            return ["product-manager", "system-architect", "api-designer", "backend-developer"]

        def load(self, name):
            raise KeyError(name)

        def has(self, name):
            return name in self.names()

        def text_of(self, name):
            return ""

    ordered, notes = Planner(BlindSource())._order_chain([
        ("pm", "product-manager", "DISCOVER"),
        ("architect", "system-architect", "DESIGN"),
        ("api", "api-designer", "DESIGN"),
        ("developer", "backend-developer", "BUILD"),
    ])
    assert [entry[1] for entry in ordered] == [
        "product-manager", "system-architect", "api-designer", "backend-developer"]
    assert notes and "no dependency" in notes[0]

def test_a_single_node_chain_needs_no_order(planner):
    ordered, notes = planner._order_chain([("pm", "product-manager", "DISCOVER")])
    assert [entry[1] for entry in ordered] == ["product-manager"]
    assert notes == ()


def test_the_ordering_is_deterministic(planner):
    """Two runs over the same corpus must produce the same order, or no plan is reviewable."""
    first = planner.plan("Plan a go-to-market launch and capture demand for the app", slug="det-1")
    second = planner.plan("Plan a go-to-market launch and capture demand for the app", slug="det-2")
    assert _chain_skills(first) == _chain_skills(second)
    assert first.order_notes == second.order_notes
