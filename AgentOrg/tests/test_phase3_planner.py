#!/usr/bin/env python3
"""Phase 3 planner tests — composition, validation, termination and fallback.

A planner's failure mode is not a crash: it is emitting a graph that cannot terminate or
cannot run. So these tests assert the termination invariants directly, and assert that every
returned plan has passed the library's own validator.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine.library import resolve
from engine.planner import Plan, PlanError, Planner, PlanValidation, emit_safe_yaml
from engine.skills import FilesystemSkillSource


@pytest.fixture(scope="module")
def source():
    return FilesystemSkillSource(resolve())


@pytest.fixture(scope="module")
def planner(source):
    return Planner(source)


@pytest.fixture(scope="module")
def plan(planner):
    return planner.plan("Build me a booking SaaS MVP with auth and payments", slug="booking-mvp")


# ── the library validator is really used ─────────────────────────────────────


def test_planner_loads_the_library_validator(planner):
    """Building on the library only pays off if its validator is authoritative."""
    assert planner._get_validator() is not None


def test_plan_is_validated_by_the_library(plan):
    assert plan.validation.valid, f"errors: {plan.validation.errors}"


def test_a_handcrafted_invalid_manifest_is_rejected(planner):
    """A manifest with an unreachable end must not be reported valid."""
    broken = {
        "name": "broken", "start": "a", "end": ["ghost"],
        "nodes": [{"id": "a", "skill": "backend-developer", "outputs": ["change"]}],
        "edges": [], "loops": [], "gates": [],
    }
    assert not planner.validate(broken).valid


def test_structural_check_catches_a_missing_end_without_a_library_validator(source):
    """Without the library validator we cannot claim validity, but we still refuse rubbish."""
    class NoLibrarySource:
        library_root = None

    local = Planner(source)
    local._validator = None
    local._library_validator = None
    local.source = NoLibrarySource()

    verdict = local.validate({"name": "x"})
    assert not verdict.valid
    assert any("no nodes" in e for e in verdict.errors)
    assert any("end node" in e for e in verdict.errors)


def test_structural_check_accepts_a_well_formed_manifest(source):
    class NoLibrarySource:
        library_root = None

    local = Planner(source)
    local._validator = None
    local._library_validator = None
    local.source = NoLibrarySource()

    manifest = {
        "name": "ok", "start": "a", "end": ["g"],
        "nodes": [{"id": "a", "skill": "x"}, {"id": "g", "type": "gate", "kind": "human"}],
        "edges": [{"from": "a", "to": "g", "when": "a.status == done"}],
        "loops": [{"id": "l", "nodes": ["a"], "exit_when": "a.verdict == pass",
                   "max_iterations": 2, "escalate_to": "g"}],
    }
    assert local.validate(manifest).valid


def test_structural_check_rejects_an_unbounded_loop(source):
    class NoLibrarySource:
        library_root = None

    local = Planner(source)
    local._validator = None
    local._library_validator = None
    local.source = NoLibrarySource()

    manifest = {
        "name": "loop", "start": "a", "end": ["g"],
        "nodes": [{"id": "a", "skill": "x"}, {"id": "g", "type": "gate", "kind": "human"}],
        "edges": [],
        "loops": [{"id": "l", "nodes": ["a"], "escalate_to": "g"}],
    }
    verdict = local.validate(manifest)
    assert not verdict.valid
    assert any("exit_when" in e for e in verdict.errors)
    assert any("max_iterations" in e for e in verdict.errors)


# ── termination invariants ───────────────────────────────────────────────────


def test_plan_declares_a_start_and_end(plan):
    assert plan.manifest["start"]
    assert plan.manifest["end"]


def test_plan_has_a_reachable_terminal_human_gate(plan):
    gates = {g["id"] for g in plan.gates if g.get("kind") == "human"}
    assert gates, "a plan must end at a human gate so only the Owner holds terminal authority"
    assert set(plan.manifest["end"]) <= gates
    assert any(g.get("requires") for g in plan.gates), "the gate should require artifacts"


def test_plan_contains_a_bounded_loop(plan):
    assert plan.loops, "a plan without a loop cannot iterate toward correctness"
    loop = plan.loops[0]
    assert loop["exit_when"], "an unbounded loop would spin"
    assert loop["max_iterations"] >= 1
    assert loop["convergence"]["require_delta"] is True, (
        "without a delta requirement a loop burns budget on identical passes"
    )
    assert loop["convergence"]["window"] >= 1


def test_loop_escalates_to_the_human_gate(plan):
    loop = plan.loops[0]
    gate_ids = {g["id"] for g in plan.gates}
    assert loop["escalate_to"] in gate_ids


def test_every_edge_endpoint_resolves(plan):
    """Edges may target gates, so resolution is checked against nodes plus gates."""
    resolvable = {n["id"] for n in plan.nodes} | {g["id"] for g in plan.gates}
    for edge in plan.manifest["edges"]:
        assert edge["from"] in resolvable, f"unknown edge source {edge['from']}"
        assert edge["to"] in resolvable, f"unknown edge target {edge['to']}"


def test_every_edge_carries_a_declared_payload(plan):
    registry = plan.manifest["payloads"]
    for edge in plan.manifest["edges"]:
        assert edge["payload"] in registry, "an unregistered payload would fail validation"
    assert "status" in registry["handoff-v1"]
    assert "verification_evidence" in registry["handoff-v1"]


def test_every_loop_node_exists(plan):
    node_ids = {n["id"] for n in plan.nodes}
    for loop in plan.loops:
        for node_id in loop["nodes"]:
            assert node_id in node_ids, f"loop references unknown node {node_id}"


def test_every_edge_condition_uses_the_documented_vocabulary(plan):
    """The runner interprets a fixed condition grammar; anything else is unexecutable."""
    import re

    pattern = re.compile(
        r"^(always|[a-z0-9-]+\.(status\s*(==|!=)\s*\w+"
        r"|status\s+in\s*\(.*\)|verdict\s*==\s*\w+)|loop\.iterations\s*<\s*\d+)$"
    )
    for edge in plan.manifest["edges"]:
        assert pattern.match(edge["when"]), f"unintelligible condition: {edge['when']!r}"


# ── composition ──────────────────────────────────────────────────────────────


def test_plan_includes_the_core_company(plan):
    skills = set(plan.skills_used)
    for expected in ("product-manager", "system-architect", "backend-developer", "code-reviewer"):
        assert expected in skills, f"{expected} missing from the plan"


def test_plan_has_a_parallel_review_fan_out(plan):
    """Parallel reviewers are what make review independent rather than sequential."""
    parallel = plan.manifest.get("parallel")
    assert parallel, "the full shape should fan reviews out in parallel"
    assert parallel[0]["join"] == "all"
    assert len(parallel[0]["nodes"]) >= 2


def test_plan_nodes_carry_their_skill_contracts(plan):
    developer = next(n for n in plan.nodes if n["skill"] == "backend-developer")
    reviewer = next(n for n in plan.nodes if n["skill"] == "code-reviewer")
    assert developer["outputs"] == ["change"]
    assert reviewer["inputs"] == ["change"]


def test_goal_keywords_add_specialists(planner):
    infra = planner.plan("Set up a Kubernetes deployment pipeline", slug="infra")
    assert "devops-engineer" in infra.skills_used

    macos = planner.plan("Create a Swift macOS app with charts", slug="macapp")
    assert "macos-developer" in macos.skills_used

    api = planner.plan("Design a REST API with a Postgres schema", slug="api")
    assert "api-designer" in api.skills_used
    assert "database-designer" in api.skills_used


def test_a_plain_goal_does_not_add_specialists(planner):
    plain = planner.plan("Refactor the authentication module", slug="refactor")
    assert "macos-developer" not in plain.skills_used
    assert "frontend-developer" not in plain.skills_used


def test_phase_labels_are_assigned(planner):
    plan = planner.plan("build something", slug="phases")
    phases = {n.get("phase") for n in plan.nodes}
    assert phases <= {"DISCOVER", "DESIGN", "BUILD", "REVIEW", "VERIFY", "OPERATE", None}
    assert "BUILD" in phases


# ── naming ───────────────────────────────────────────────────────────────────


def test_slug_is_derived_from_the_goal(planner):
    plan = planner.plan("Build a Booking SaaS!")
    assert plan.slug == "build-a-booking-saas"
    assert plan.manifest["name"] == plan.slug


def test_slug_never_starts_with_a_hyphen(planner):
    """The library requires `[a-z0-9][a-z0-9-]*`."""
    plan = planner.plan("!!! urgent work !!!")
    assert plan.manifest["name"][0].isalnum()


def test_explicit_slug_is_honoured(planner):
    assert planner.plan("anything", slug="my-project").slug == "my-project"


def test_description_is_a_single_line(planner):
    plan = planner.plan("multi\nline   goal\nwith   whitespace", slug="desc")
    description = plan.manifest["description"]
    assert "\n" not in description
    assert "  " not in description


def test_very_long_goal_is_truncated_in_the_description(planner):
    from engine.planner import _one_line

    assert len(_one_line("word " * 200)) <= 300


# ── refusal and fallback ─────────────────────────────────────────────────────


def test_empty_goal_is_refused(planner):
    with pytest.raises(PlanError, match="goal is required"):
        planner.plan("   ")


def test_a_failing_validator_falls_back_before_raising(source):
    """The planner tries leaner shapes, so a goal that cannot support the full company still
    yields a runnable graph."""
    calls: list[str] = []
    shapes_seen: list[int] = []

    class RejectFullOnly:
        def __call__(self, manifest):
            shapes_seen.append(len(manifest.get("nodes") or []))
            # Reject anything with more than two nodes, accept the minimal skeleton.
            if len(manifest.get("nodes") or []) > 2:
                calls.append("reject")
                return PlanValidation(valid=False, errors=("too complex for this test",))
            return PlanValidation(valid=True)

    planner = Planner(source, validator=RejectFullOnly())
    plan = planner.plan("Build a booking service", slug="fallback")
    assert plan.validation.valid
    assert plan.notes, "the plan should say it used a leaner shape"
    assert len(shapes_seen) >= 2, "the planner must have tried more than one shape"


def test_all_shapes_failing_is_reported_as_a_planner_defect(source):
    """If even the minimal skeleton fails, the bug is here, not in the goal."""
    class AlwaysReject:
        def __call__(self, manifest):
            return PlanValidation(valid=False, errors=("forced failure",))

    with pytest.raises(PlanError, match="defect in the planner"):
        Planner(source, validator=AlwaysReject()).plan("anything")


def test_dropped_reasons_are_surfaced_to_the_owner(source):
    """A phase quietly dropped is a promise quietly broken; it must be visible."""
    class RejectFullOnly:
        def __call__(self, manifest):
            if len(manifest.get("nodes") or []) > 2:
                return PlanValidation(valid=False, errors=("too complex",))
            return PlanValidation(valid=True)

    plan = Planner(source, validator=RejectFullOnly()).plan("Build a service", slug="dropped")
    assert plan.notes or plan.dropped
    summary = plan.summary()
    assert "rejected" in summary or "used the" in summary


# ── plan presentation ────────────────────────────────────────────────────────


def test_summary_names_the_sequence_loops_and_gates(plan):
    summary = plan.summary()
    assert "Sequence:" in summary
    assert "Loops" in summary
    assert "Gates:" in summary
    assert "human-gate" in summary
    assert "validated: yes" in summary


def test_summary_lists_every_node(plan):
    summary = plan.summary()
    for node in plan.nodes:
        assert node["id"] in summary


def test_plan_serialises_for_the_event_stream(plan):
    payload = plan.as_dict()
    assert payload["slug"] == "booking-mvp"
    assert payload["validation"]["valid"] is True
    assert payload["manifest"]["name"] == "booking-mvp"
    assert isinstance(payload["skills_used"], list)


def test_plan_exposes_its_collections(plan):
    assert plan.node_ids()
    assert isinstance(plan.loops, list)
    assert isinstance(plan.gates, list)


# ── cross-goal robustness ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "goal, slug",
    [
        ("Build a mobile fitness tracker", "fitness"),
        ("Set up CI/CD with Docker and Kubernetes", "cicd"),
        ("Create a data warehouse ETL pipeline", "warehouse"),
        ("Design a payment and billing system", "payments"),
        ("Write a small CLI utility", "cli"),
        ("Improve accessibility of the checkout flow", "a11y"),
        ("Build an analytics dashboard with charts", "analytics"),
    ],
)
def test_every_goal_produces_a_validated_terminating_plan(planner, goal, slug):
    """The planner must be robust across the goals an Owner will actually type."""
    plan = planner.plan(goal, slug=slug)
    assert plan.validation.valid, f"{goal}: {plan.validation.errors}"

    resolvable = {n["id"] for n in plan.nodes} | {g["id"] for g in plan.gates}
    assert plan.manifest["start"] in resolvable
    assert all(e in resolvable for e in plan.manifest["end"])
    assert all(e["from"] in resolvable and e["to"] in resolvable for e in plan.manifest["edges"])
    assert plan.loops and plan.loops[0]["exit_when"] and plan.loops[0]["max_iterations"] >= 1
    assert any(g.get("kind") == "human" for g in plan.gates)
    assert plan.skills_used, "a plan with no skills cannot run"


# ── Safe-YAML emission ───────────────────────────────────────────────────────


def test_emitted_yaml_round_trips_through_the_library_parser(plan):
    """A plan that validates in memory must be readable from disk — that is the only way
    the runner ever sees it."""
    import importlib.util

    lib = resolve()
    spec = importlib.util.spec_from_file_location("sy_probe", lib.files.root / "scripts" / "lib" / "safe_yaml.py")
    safe_yaml = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(safe_yaml)

    parsed = safe_yaml.parse(emit_safe_yaml(plan.manifest))
    assert parsed["name"] == plan.manifest["name"]
    assert parsed["start"] == plan.manifest["start"]
    assert len(parsed["nodes"]) == len(plan.manifest["nodes"])
    assert parsed["loops"][0]["max_iterations"] == plan.loops[0]["max_iterations"]
    assert parsed["loops"][0]["exit_when"] == plan.loops[0]["exit_when"]
    assert parsed["payloads"]["handoff-v1"] == plan.manifest["payloads"]["handoff-v1"]
    assert parsed["parallel"][0]["nodes"] == plan.manifest["parallel"][0]["nodes"]


def test_emitted_yaml_has_no_flow_maps(plan):
    """Flow maps are outside the subset, so emitting one makes the file unreadable."""
    text = emit_safe_yaml(plan.manifest)
    assert "{" not in text, "the subset parser rejects flow maps"
    assert "}" not in text


def test_emitted_yaml_passes_the_library_validator_on_disk(plan, tmp_path):
    import subprocess

    lib = resolve()
    path = tmp_path / f"{plan.slug}.yaml"
    path.write_text(emit_safe_yaml(plan.manifest))
    result = subprocess.run(
        [sys.executable, str(lib.files.validator), "--manifest", str(path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"validator rejected the emitted file: {result.stdout}{result.stderr}"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("plain", "plain"),
        ("has: colon", '"has: colon"'),
        ("true", '"true"'),
        ("42", '"42"'),
        ("", '""'),
        (True, "true"),
        (None, "null"),
        (7, "7"),
        ("- leading dash", '"- leading dash"'),
    ],
)
def test_scalar_emission_quotes_only_what_needs_it(value, expected):
    from engine.planner import _emit_scalar

    assert _emit_scalar(value) == expected


def test_emitted_edge_conditions_survive_a_round_trip(plan):
    import importlib.util

    lib = resolve()
    spec = importlib.util.spec_from_file_location("sy_probe2", lib.files.root / "scripts" / "lib" / "safe_yaml.py")
    safe_yaml = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(safe_yaml)

    parsed = safe_yaml.parse(emit_safe_yaml(plan.manifest))
    original = {(e["from"], e["to"]): e["when"] for e in plan.manifest["edges"]}
    for edge in parsed["edges"]:
        assert edge["when"] == original[(edge["from"], edge["to"])], (
            "an edge condition that changes on round-trip would change control flow"
        )
