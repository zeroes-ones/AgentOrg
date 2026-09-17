#!/usr/bin/env python3
"""Phase 4 CLI tests — the commands the documentation tells you to run.

Documentation that describes commands nobody can run is worse than none, so every command named in
README.md, USAGE.md and TROUBLESHOOTING.md is executed here and its output shape asserted. If a
command's behaviour changes, these tests fail before the docs go stale.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.cli import EXIT_CHECK_FAILED, EXIT_OK, EXIT_USAGE, build_parser, main


def run(*args: str) -> subprocess.CompletedProcess:
    """Run the CLI as a subprocess, so the real entry point is exercised."""
    return subprocess.run(
        [sys.executable, "-m", "engine.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )


def run_json(*args: str) -> dict:
    """Run a read command with --json and parse it.

    stderr is ignored on purpose: the CLI writes warnings there and keeps stdout for the answer,
    which is the discipline the docs rely on.
    """
    result = run("--json", *args)
    assert result.returncode == EXIT_OK, f"exit {result.returncode}: {result.stderr[:400]}"
    return json.loads(result.stdout)


# ── the parser surface ───────────────────────────────────────────────────────


def test_parser_accepts_every_documented_command():
    parser = build_parser()
    for argv in (["doctor"], ["skills", "list"], ["skills", "show", "code-reviewer"],
                 ["models"], ["models", "--refresh"],
                 ["plan", "--goal", "x"], ["org"], ["org", "--goal", "x"], ["delegation"]):
        assert parser.parse_args(argv).func is not None, f"{argv} does not resolve to a command"


def test_parser_requires_a_command():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_rejects_an_unknown_command():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["teleport"])


def test_main_returns_an_exit_code():
    """Every command returns a code, so a script can branch on it."""
    assert main(["doctor", "--json"]) in (EXIT_OK, EXIT_CHECK_FAILED)


def test_help_lists_the_commands():
    parser = build_parser()
    text = parser.format_help()
    for command in ("doctor", "skills", "models", "plan", "org", "delegation"):
        assert command in text


def test_help_documents_the_exit_codes():
    """The exit codes are part of the contract, so they are in the help."""
    text = build_parser().format_help()
    assert "exit codes" in text.lower()
    assert "0  success" in text


# ── doctor ───────────────────────────────────────────────────────────────────


def test_doctor_passes_in_this_environment():
    result = run("doctor")
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "doctor: all checks passed" in result.stdout


def test_doctor_reports_seven_checks():
    """The count is documented, so it is asserted."""
    result = run("doctor")
    lines = [line for line in result.stdout.splitlines()
             if line.startswith(("OK ", "FAIL"))]
    assert len(lines) == 7


def test_doctor_checks_the_thing_that_matters():
    payload = run_json("doctor")
    names = {check["check"] for check in payload["checks"]}
    for expected in ("configuration", "skills library", "skill bundles", "providers",
                     "machine", "model catalog", "secret hygiene"):
        assert expected in names


def test_doctor_json_reports_failures():
    payload = run_json("doctor")
    assert "failures" in payload
    assert payload["failures"] == sum(1 for c in payload["checks"] if not c["ok"])


def test_doctor_names_skipped_providers_with_the_reason():
    """A provider that could not be built must be reported, not hidden."""
    payload = run_json("doctor")
    providers = next(c for c in payload["checks"] if c["check"] == "providers")
    assert "skipped" in providers
    for entry in providers["skipped"]:
        assert ":" in entry, "a skip must name the provider and the reason"


def test_doctor_reports_the_ceiling_with_a_reason():
    payload = run_json("doctor")
    machine = next(c for c in payload["checks"] if c["check"] == "machine")
    assert "ceiling" in machine["detail"]


# ── skills ───────────────────────────────────────────────────────────────────


def test_skills_list_returns_the_whole_library():
    payload = run_json("skills", "list")
    assert payload["count"] > 300
    assert all("name" in s for s in payload["skills"])


def test_skills_list_reports_contract_counts():
    payload = run_json("skills", "list")
    entry = next(s for s in payload["skills"] if s["name"] == "code-reviewer")
    assert entry["criteria"] == 3
    assert entry["checklist"] == 14
    assert entry["outputs"] == ["review-report"]


def test_skills_show_extracts_the_cr_checklist():
    """The acceptance bar for the whole skill-ingestion layer."""
    payload = run_json("skills", "show", "code-reviewer")
    ids = [item["id"] for item in payload["checklist"]]
    assert ids == [f"CR{i}" for i in range(1, 15)]
    assert payload["contract"]["evidence_required"] is True
    assert payload["contract"]["escalate_to"] == ["human-gate"]


def test_skills_show_includes_the_research_gate():
    payload = run_json("skills", "show", "code-reviewer")
    assert len(payload["research_steps"]) == 8
    assert payload["research_steps"][0].startswith("RP1")


def test_skills_show_includes_anti_rationalization_rules():
    payload = run_json("skills", "show", "code-reviewer")
    assert payload["anti_rationalization"]


def test_skills_show_reports_section_tiers():
    payload = run_json("skills", "show", "code-reviewer")
    assert set(payload["section_tiers"]) == {"ROUTE", "CORE", "DETAIL"}
    assert all(v > 0 for v in payload["section_tiers"].values())


def test_skills_show_fails_clearly_for_an_unknown_skill():
    result = run("skills", "show", "not-a-real-skill")
    assert result.returncode == EXIT_CHECK_FAILED
    assert "not found" in result.stderr


def test_every_library_skill_bundles_via_the_cli():
    payload = run_json("skills", "list")
    errors = [s for s in payload["skills"] if "error" in s]
    assert not errors, f"skills that failed to bundle: {[e['name'] for e in errors][:5]}"


# ── models ───────────────────────────────────────────────────────────────────


def test_models_reports_provenance():
    """The `source` column is the honesty mechanism: an assumed window cannot be bound."""
    payload = run_json("models")
    assert payload["models"]
    for entry in payload["models"]:
        assert entry["source"] in ("probed", "declared", "assumed")


def test_models_reports_bindability():
    payload = run_json("models")
    assert payload["bindable"] == sum(1 for m in payload["models"] if m["window_known"])


def test_models_never_reports_an_invented_window():
    """An unknown window stays None rather than becoming a default."""
    payload = run_json("models")
    for entry in payload["models"]:
        if entry["context_window"] is None:
            assert entry["window_known"] is False


def test_models_scopes_each_model_to_a_provider_that_can_serve_it():
    """Offering `gpt-4o` on Anthropic would waste the Owner's time and fail at call time.

    The check is that a cloud model is offered only by a provider whose *family* it belongs to, using
    the alias tables the config declares rather than a guess about names.
    """
    payload = run_json("models")
    import json as _json

    config = _json.loads((ROOT / "credentials.example.json").read_text())
    by_provider: dict[str, set[str]] = {}
    for provider_id, spec in config["providers"].items():
        aliases = set((spec.get("model_aliases") or {}).values())
        by_provider[provider_id] = aliases

    for entry in payload["models"]:
        if entry["locality"] == "local":
            continue
        provider = entry["provider_id"]
        model = entry["model_id"]
        # A cloud model must be one the provider's own aliases declare, or name the provider, or have
        # been **probed from that provider** — the last case matters because a live endpoint's own model
        # list is the strongest possible evidence that it serves them, and it is not something the
        # example config can know in advance (a provider the Owner added is not in the example at all).
        declared = model in by_provider.get(provider, set())
        names_provider = provider.split("-")[0] in model.lower()
        probed_from_provider = entry.get("source") == "probed"
        assert declared or names_provider or probed_from_provider, (
            f"{model} is offered under {provider}, which does not declare it and did not report it"
        )


def test_models_reports_provider_discovery_status():
    payload = run_json("models")
    assert payload["provider_status"]
    for status in payload["provider_status"].values():
        assert "status" in status and "count" in status


def test_models_reports_a_down_provider_without_failing():
    """The app must open on a machine where a local provider is not running."""
    result = run("--json", "models")
    assert result.returncode == EXIT_OK


# ── plan ─────────────────────────────────────────────────────────────────────


def test_plan_produces_a_validated_manifest():
    payload = run_json("plan", "--goal", "Build a booking API with auth", "--slug", "cli-plan")
    assert payload["validation"]["valid"] is True
    assert payload["manifest"]["name"] == "cli-plan"


def test_plan_always_contains_a_bounded_loop_and_a_gate():
    payload = run_json("plan", "--goal", "Build a booking API", "--slug", "cli-loop")
    loops = payload["manifest"]["loops"]
    assert loops and loops[0]["exit_when"] and loops[0]["max_iterations"] >= 1
    assert any(g.get("kind") == "human" for g in payload["manifest"]["gates"])


def test_plan_reports_skill_names():
    payload = run_json("plan", "--goal", "Build a booking API", "--slug", "cli-skills")
    assert "backend-developer" in payload["skills_used"]
    assert "code-reviewer" in payload["skills_used"]


def test_plan_slug_is_derived_when_omitted():
    payload = run_json("plan", "--goal", "Build a Booking SaaS!")
    assert payload["slug"] == "build-a-booking-saas"


def test_plan_human_output_shows_the_sequence_and_loops():
    result = run("plan", "--goal", "Build a booking API", "--slug", "cli-human")
    assert result.returncode == EXIT_OK
    assert "Sequence:" in result.stdout
    assert "Loops" in result.stdout
    assert "Gates:" in result.stdout
    assert "validated: yes" in result.stdout


def test_plan_writes_validatable_safe_yaml(tmp_path):
    """A plan that validates in memory must be readable from disk by the library's runner."""
    target = tmp_path / "cli-out.yaml"
    result = run("plan", "--goal", "Build a booking API", "--slug", "cli-out",
                 "--out", str(target))
    assert result.returncode == EXIT_OK
    assert target.is_file()
    text = target.read_text()
    assert "{" not in text and "}" not in text, "flow maps are outside the Safe YAML Subset"
    assert "name: cli-out" in text


def test_plan_respects_max_iterations():
    payload = run_json("plan", "--goal", "Build a booking API", "--slug", "cli-iter",
                       "--max-iterations", "5")
    assert payload["manifest"]["loops"][0]["max_iterations"] == 5


# ── org ──────────────────────────────────────────────────────────────────────


def test_org_shows_the_default_company():
    payload = run_json("org")
    roster = payload["roster"]
    owner = next(a for a in roster if a["role"] == "owner")
    assert owner["kind"] == "human"
    assert len([a for a in roster if a["kind"] == "ai"]) == 7


def test_org_binds_reviewers_to_a_different_model():
    """Independence holds structurally, not by instruction."""
    payload = run_json("org")
    reviewers = [a for a in payload["roster"] if a["role"] == "reviewer"]
    builders = [a for a in payload["roster"] if a["role"] == "worker" and a["kind"] == "ai"]
    assert reviewers and builders
    assert all(r["model"] != builders[0]["model"] for r in reviewers)


def test_org_shows_the_policy_matrix():
    payload = run_json("org")
    assert set(payload["policy"]) == {"R-CONTRACT", "R-REWORK", "R-DELEGATE",
                                      "R-ESCALATE", "R-CONFLICT", "R-MATCH-FAIL"}
    assert payload["policy"]["R-ESCALATE"]["level"] == "confirm"
    assert payload["policy"]["R-CONTRACT"]["level"] == "auto"


def test_org_reports_staffing_gaps_for_a_goal():
    """The pre-run check that prevents a graph stopping mid-way."""
    payload = run_json("org", "--goal", "Build a booking API with auth", "--slug", "cli-gap")
    assert "staffing_gaps" in payload
    assert all("node_id" in g and "skill" in g and "reason" in g for g in payload["staffing_gaps"])


def test_org_binds_every_staffed_node():
    payload = run_json("org", "--goal", "Build a booking API with auth", "--slug", "cli-bind")
    gaps = {g["node_id"] for g in payload["staffing_gaps"]}
    for node_id, binding in payload["bindings"].items():
        assert node_id not in gaps
        assert binding["agents"], "a binding must name at least one agent"


def test_org_human_output_names_the_gaps():
    result = run("org", "--goal", "Build a booking API with auth", "--slug", "cli-gap-human")
    assert result.returncode == EXIT_OK
    assert "AgentOrg:" in result.stdout
    assert "Policy" in result.stdout


# ── delegation ───────────────────────────────────────────────────────────────


def test_delegation_lists_the_six_invariants():
    payload = run_json("delegation")
    assert set(payload["invariants"]) == {"S1", "S2", "S3", "S4", "S5", "S6"}
    assert payload["max_depth"] == 3


def test_delegation_reports_the_budget_share():
    payload = run_json("delegation")
    assert 0 < payload["budget_share_max"] <= 1


def test_delegation_human_output_is_readable():
    result = run("delegation")
    assert result.returncode == EXIT_OK
    assert "invariants" in result.stdout.lower()
    assert "S1" in result.stdout


# ── output discipline ────────────────────────────────────────────────────────


def test_stdout_carries_only_the_answer():
    """Warnings go to stderr, so --json on stdout stays parseable."""
    result = run("--json", "doctor")
    assert json.loads(result.stdout)  # parses despite warnings on stderr


def test_json_output_is_sorted_and_stable():
    first = run("--json", "delegation").stdout
    second = run("--json", "delegation").stdout
    assert first == second, "the same command must produce the same output"


def test_unknown_config_path_fails_clearly(tmp_path):
    """An explicit pointer is honoured exclusively, so a bad one fails loudly."""
    target = tmp_path / "absent.json"
    result = run("--config", str(target), "doctor")
    assert result.returncode == EXIT_CHECK_FAILED
    # A failure is a diagnostic, so it goes to stderr; with --json the structured result is on stdout.
    assert "no configuration found" in result.stderr

    structured = run("--json", "--config", str(target), "doctor")
    assert structured.returncode == EXIT_CHECK_FAILED
    payload = json.loads(structured.stdout)
    assert payload["failures"] == 1
    assert "no configuration found" in payload["checks"][0]["detail"]


# ── the run lifecycle commands ───────────────────────────────────────────────


_RUN_MANIFEST = """name: clirun
version: "1.0.0"
description: CLI run probe
payloads:
  handoff-v1:
    - status
    - summary
start: dev
nodes:
  - id: dev
    skill: backend-developer
    outputs: [change]
gates:
  - id: release
    type: gate
    kind: human
    requires: [change]
    description: Owner release approval
edges:
  - from: dev
    to: release
    when: dev.status == done
    payload: handoff-v1
end: [release]
"""

_RUN_STUB = '''CRITERIA = [
    "Every accepted finding addressed with a concrete diff",
    "No finding silently dropped",
    "Open items declared in open questions rather than hidden",
]


def execute_node(node_id, state, ctx):
    if node_id == "release":
        return {"status": "needs_review", "verdict": "awaiting_owner",
                "summary": "gate reached", "evidence": ["g"]}
    return {"status": "done", "verdict": "ok", "summary": "implemented",
            "evidence": ["src/app.py#abc"], "criteria_met": list(CRITERIA),
            "artifacts": [{"name": "change", "path": "src/app.py", "sha": "abc",
                           "type": "change"}]}
'''


@pytest.fixture
def run_project(tmp_path):
    root = tmp_path / "projects"
    project = root / "clirun"
    project.mkdir(parents=True)
    (project / "clirun.yaml").write_text(_RUN_MANIFEST)
    (project / "stub.py").write_text(_RUN_STUB)
    return root, project


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "engine.cli", *args],
                          capture_output=True, text=True, cwd=str(ROOT))


def test_fanout_expands_a_template_without_spending_anything():
    """`--dry-run` is the first thing anyone wants when a template behaves unexpectedly."""
    result = run("fanout", "Review {{item}} for regressions.",
                 "--item", "src/a.ts", "--item", "src/b.ts", "--dry-run")
    assert result.returncode == EXIT_OK, result.stderr[:300]
    assert "Review src/a.ts for regressions." in result.stdout
    assert "Review src/b.ts for regressions." in result.stdout


def test_fanout_refuses_a_template_without_the_placeholder():
    """The same refusal the executor and serve use, because all three go through plan_fanout."""
    result = run("fanout", "Review the file", "--item", "a", "--item", "b", "--dry-run")
    assert result.returncode == EXIT_CHECK_FAILED
    assert "{{item}}" in result.stderr


def test_the_parser_accepts_fanout():
    parser = build_parser()
    args = parser.parse_args(["fanout", "Do {{item}}", "--item", "a", "--item", "b"])
    assert args.func is not None
    assert args.item == ["a", "b"]


def test_run_accepts_existing_manifest_commands():
    assert "run" in build_parser().format_help()
    for command in ("status", "decide", "instruct"):
        assert command in build_parser().format_help()


def test_run_dry_run_plans_without_executing(run_project):
    """A dry run must not execute, so an Owner can inspect the graph first."""
    root, project = run_project
    result = _run_cli("run", "--manifest", str(project / "clirun.yaml"),
                      "--slug", "clirun", "--root", str(root), "--dry-run")
    assert result.returncode == EXIT_OK
    assert "dry run" in result.stdout
    assert "awaiting_approval" in result.stdout


def test_run_reaches_the_gate_and_reports_it(run_project):
    """The CLI must show the gate and how to decide it, not just stop."""
    root, project = run_project
    result = _run_cli("run", "--manifest", str(project / "clirun.yaml"),
                      "--slug", "clirun", "--root", str(root),
                      "--executor", str(project / "stub.py"))
    assert result.returncode == EXIT_OK
    assert "awaiting_gate" in result.stdout
    assert "GATE: release" in result.stdout
    assert "engine.cli decide" in result.stdout


def test_status_reports_the_phase_and_the_gate(run_project):
    root, project = run_project
    _run_cli("run", "--manifest", str(project / "clirun.yaml"), "--slug", "clirun",
             "--root", str(root), "--executor", str(project / "stub.py"))
    result = _run_cli("status", "--slug", "clirun", "--root", str(root))
    assert result.returncode == EXIT_OK
    assert "awaiting_gate" in result.stdout
    assert "release (human)" in result.stdout


def test_decide_rejects_with_a_note_that_is_recorded(run_project):
    """A rejection the agents cannot read is one they will re-attempt identically."""
    root, project = run_project
    _run_cli("run", "--manifest", str(project / "clirun.yaml"), "--slug", "clirun",
             "--root", str(root), "--executor", str(project / "stub.py"))
    result = _run_cli("decide", "--slug", "clirun", "--root", str(root),
                      "--reject", "--note", "needs a rollback plan")
    assert result.returncode == EXIT_OK
    assert "paused" in result.stdout

    status = _run_cli("status", "--slug", "clirun", "--root", str(root), "--json")
    payload = json.loads(status.stdout)
    assert payload["phase"] == "paused"
    assert any("rollback plan" in i for i in payload["instructions"])


def test_decide_requires_a_choice(run_project):
    root, _ = run_project
    result = _run_cli("decide", "--slug", "clirun", "--root", str(root))
    assert result.returncode == EXIT_USAGE
    assert "choose --approve or --reject" in result.stderr


def test_decide_on_a_run_not_at_a_gate_is_refused(run_project):
    root, project = run_project
    _run_cli("run", "--manifest", str(project / "clirun.yaml"), "--slug", "clirun",
             "--root", str(root), "--dry-run")
    result = _run_cli("decide", "--slug", "clirun", "--root", str(root), "--approve")
    assert result.returncode == EXIT_CHECK_FAILED
    assert "not waiting on a gate" in result.stderr


def test_instruct_records_a_constraint_separately(run_project):
    """Only a constraint is preserved verbatim; the CLI must make the difference visible."""
    root, project = run_project
    _run_cli("run", "--manifest", str(project / "clirun.yaml"), "--slug", "clirun",
             "--root", str(root), "--dry-run")
    result = _run_cli("instruct", "NEVER log the raw auth token", "--slug", "clirun",
                      "--root", str(root), "--constraint")
    assert result.returncode == EXIT_OK
    assert "non-negotiable" in result.stdout

    status = _run_cli("status", "--slug", "clirun", "--root", str(root), "--json")
    payload = json.loads(status.stdout)
    assert payload["constraints"] == ["NEVER log the raw auth token"]
    assert payload["instructions"] == []


def test_instruct_records_a_plain_instruction_as_guidance(run_project):
    root, project = run_project
    _run_cli("run", "--manifest", str(project / "clirun.yaml"), "--slug", "clirun",
             "--root", str(root), "--dry-run")
    _run_cli("instruct", "prefer no new dependencies", "--slug", "clirun", "--root", str(root))
    status = _run_cli("status", "--slug", "clirun", "--root", str(root), "--json")
    payload = json.loads(status.stdout)
    assert payload["instructions"] == ["prefer no new dependencies"]
    assert payload["constraints"] == []


def test_status_for_an_unknown_run_fails_clearly(run_project):
    root, _ = run_project
    result = _run_cli("status", "--slug", "never-ran", "--root", str(root))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "no run found" in result.stderr
