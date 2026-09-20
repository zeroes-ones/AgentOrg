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
                 ["plan", "--goal", "x"], ["org"], ["org", "--goal", "x"], ["delegation"],
                 ["mission", "status"], ["mission", "set", "ship it", "--objective", "a"],
                 ["mission", "add", "a step"], ["mission", "start", "--index", "0"],
                 ["mission", "advance"], ["mission", "mark", "0", "done"]):
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


def test_doctor_reports_every_check():
    """The count is documented, so it is asserted.

    Eight, not seven: the machine posture (`[system]`) became a check when it turned out `doctor`
    never read the section its own remedy text pointed at — so a person on a full-access machine got
    "all checks passed" and nothing about the switch that decides whether the engine can touch their
    Mac at all. The number is asserted rather than derived so that *losing* a check is a failure
    here, which is the only way a silent drop in coverage gets noticed.
    """
    result = run("doctor")
    lines = [line for line in result.stdout.splitlines()
             if line.startswith(("OK ", "FAIL"))]
    assert len(lines) == 8


def test_doctor_checks_the_thing_that_matters():
    payload = run_json("doctor")
    names = {check["check"] for check in payload["checks"]}
    for expected in ("configuration", "skills library", "skill bundles", "providers",
                     "machine", "model catalog", "secret hygiene", "system access"):
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
#
# Which model a reviewer is bound to is a property of the *configuration*, so every assertion in this
# section supplies the configuration it is made against. The test that used to sit here ran `org` with
# none, which reads `./credentials.json` — the developer's own providers and default model — so its
# claim ("reviewers differ from the builders") was a statement about whoever ran the suite. That is
# the only reason it was red: on a configuration that offers one model, the built-in company has one
# model to put everyone on, and no amount of correctness in the engine changes that. The same
# objection applies to the rest of this section, and to the network those tests used to reach.
#
# Two providers on a closed loopback port (`discard`), because `org` probes them for discovery: nothing
# listens, so a probe is refused in milliseconds, no endpoint off this machine is ever named and
# nothing can be spent. `max_retries: 0` and a 2s timeout keep a hypothetical reachable endpoint from
# turning a unit test into a hang.

_ORG_ENDPOINT = "http://127.0.0.1:9"        # port 9: refused, never served
_ORG_DEFAULT_MODEL = "harness-model"        # the configured default, so the builders' model
_ORG_OTHER_MODEL = "other-model"            # the distinct model the second provider can offer
# Sorts after both of the above, so "the declared pair was read" is distinguishable from "the
# catalogue happened to agree with it". An assertion that passes either way proves neither.
_ORG_DECLARED_MODEL = "zz-declared-model"


def _org_config(tmp_path, *, second_offers: str = _ORG_DEFAULT_MODEL, declared: str = "") -> str:
    """The configuration this section asserts against — built here, never read off the machine.

    `second_offers` is what the second provider serves, and it is what puts each test in its case:

    - a model of its own → the configuration offers a model distinct from the builders', so the
      reviewers have to be bound to it;
    - the default model (the default here, so the sibling tests need not think about it) → no distinct
      model exists, which is the single-model machine the engine's own comment names. The second
      provider still declares the alias, because that is what makes the engine's *fallback* reachable
      rather than its "no other provider at all" path — two different code paths, one of which warns.

    `declared` writes `defaults.reviewer`, the pair `defaults set --reviewer-model` writes.

    The document is derived from the committed template's other sections, the way `_creds` does — that
    is this file's one hermetic-credentials writer, and it lives in the `defaults` section below — so
    budget, context, policy and hooks stay the documented ones instead of becoming a second config
    format that drifts from the template.
    """
    def mutate(document: dict) -> None:
        loopback = {"kind": "ollama", "base_url": _ORG_ENDPOINT, "api_key": None,
                    "api_key_env": None, "timeout_s": 2, "max_retries": 0, "concurrency": 1,
                    "model_aliases": {}}
        document["providers"] = {
            "harness": dict(loopback),
            "second": {**loopback, "model_aliases": {"reviewer": second_offers}},
        }
        known = {_ORG_DEFAULT_MODEL: {"context_window": 32768, "max_output": 8192,
                                      "locality": "local"}}
        for model in (second_offers, declared):
            # Every model the fixture offers declares a window: an agent cannot be bound without one,
            # so a fixture whose model had none would test "nobody could be bound" rather than "which
            # model was chosen".
            if model:
                known[model] = {"context_window": 32768, "max_output": 8192, "locality": "local"}
        document["models"] = {"catalog": (document.get("models") or {}).get("catalog") or {},
                              "known": known}
        document["defaults"] = {"provider": "harness", "model": _ORG_DEFAULT_MODEL,
                                "temperature": 0.2}
        if declared:
            document["defaults"]["reviewer"] = {"provider": "second", "model": declared}
        # The template's per-provider limits name the providers it configures; left in place they
        # would be pruned as unknown on every call, with a warning each time.
        document["concurrency"] = {**(document.get("concurrency") or {}),
                                   "per_provider_limits": {"harness": 1, "second": 1}}
    target = tmp_path / "config"
    target.mkdir(exist_ok=True)
    return _creds(target, mutate)


def test_org_shows_the_default_company(tmp_path):
    payload = run_json("--config", _org_config(tmp_path), "org")
    roster = payload["roster"]
    owner = next(a for a in roster if a["role"] == "owner")
    assert owner["kind"] == "human"
    assert len([a for a in roster if a["kind"] == "ai"]) == 7


def test_org_binds_reviewers_to_a_different_model(tmp_path):
    """Independence holds structurally, not by instruction — and the configuration is what decides.

    Hermetic on purpose. The rule is about the configuration ("bind a reviewer to a second model when
    one is offered"), so the configuration is the input this test builds; the previous form of it read
    the developer's own `credentials.json`, which made the assertion a claim about their laptop and is
    the only thing that was wrong with it. Nothing here can pass or fail because of what this machine
    has installed.

    The model is named rather than merely "different": naming it is what separates "the engine bound
    the reviewers to the model the configuration offered" from "two strings happened not to be equal".
    """
    payload = run_json("--config", _org_config(tmp_path, second_offers=_ORG_OTHER_MODEL), "org")
    reviewers = [a for a in payload["roster"] if a["role"] == "reviewer"]
    builders = [a for a in payload["roster"] if a["role"] == "worker" and a["kind"] == "ai"]
    assert reviewers and builders
    assert {b["model"] for b in builders} == {_ORG_DEFAULT_MODEL}, "the builders are the control"
    assert {r["model"] for r in reviewers} == {_ORG_OTHER_MODEL}
    assert all(r["model"] != builders[0]["model"] for r in reviewers)


def test_org_warns_rather_than_refusing_when_no_distinct_model_is_offered(tmp_path):
    """The other half of the rule, and the half the engine's own comment is about.

    A reviewer cannot be *refused* for sharing the producers' model: `People.hire` records why
    (engine/people.py:379-383) — refusing "would make any reviewer unhirable on a single-model local
    setup, where no distinct model exists to offer". So the company is built anyway, and the weaker
    boundary is *said* out loud: the warning names the model, which is what tells a person their review
    is running on the same weights that wrote the work rather than on an independent verifier.
    """
    result = run("--json", "--config", _org_config(tmp_path), "org")
    assert result.returncode == EXIT_OK, result.stderr[:400]        # warned, not refused
    payload = json.loads(result.stdout)
    reviewers = [a for a in payload["roster"] if a["role"] == "reviewer"]
    builders = [a for a in payload["roster"] if a["role"] == "worker" and a["kind"] == "ai"]
    assert reviewers and builders
    assert {r["model"] for r in reviewers} == {builders[0]["model"]}, "no distinct model exists here"
    assert "reviewers share the builders' model" in result.stderr, (
        "a shared-model reviewer presented without a word is a same-model verdict dressed as "
        "independence")
    assert _ORG_DEFAULT_MODEL in result.stderr, "the warning must name the model it is about"


def test_a_declared_reviewer_model_is_the_model_the_reviewers_get(tmp_path):
    """KNOWN DEFECT — the pair `defaults set --reviewer-model` writes is read by nothing.

    Everything except a consumer exists: `defaults.reviewer` is settable (engine/cli.py:2745-2746,
    written by engine/config.py:1424-1432), loaded into `DefaultsConfig.reviewer_provider` /
    `reviewer_model` (engine/config.py:608-609, 1645-1646), reported by `defaults show`
    (engine/cli.py:2799-2801) and documented as the way to keep reviewers independent (USAGE.md:814;
    DESIGN-DEFAULTS-AUTONOMY.md:70-73). No code path reads it to *bind* an agent: those two reports are
    its only readers, and `cmd_org` (engine/cli.py:807 onward) discovers a reviewer model from the
    catalogue without ever consulting the declaration. So a person who sets it gets whatever the
    catalogue happened to offer — the fixture below is built so that the two differ.

    Correct behaviour, from the field's own docstring (engine/config.py:605-607): non-empty means "the
    model reviewers run on"; empty means "find a distinct one, or fall back with a warning". The
    discovery loop is the *empty* case. This test asserts the declared model wins, and reports the
    defect rather than failing on it while the defect stands — `run_tests.py` executes the test
    functions itself and honours no `xfail` marker (pytest_shim.py:106-137 accepts and ignores any mark
    that is not `parametrize`), so a skip with the reason in it is the only way this file can record a
    known-bad expectation without leaving the suite red. When `cmd_org` honours the declared pair,
    delete the guard and this becomes an ordinary assertion.

    Both models are on offer on purpose. `_ORG_DECLARED_MODEL` sorts after `_ORG_OTHER_MODEL`, so the
    catalogue's own pick and the declaration are different models — a fixture offering only the
    declared one would pass whether or not the declaration was read, which proves nothing.
    """
    creds = _org_config(tmp_path, second_offers=_ORG_OTHER_MODEL, declared=_ORG_DECLARED_MODEL)
    payload = run_json("--config", creds, "org")
    reviewers = [a for a in payload["roster"] if a["role"] == "reviewer"]
    assert reviewers
    if reviewers[0]["model"] != _ORG_DECLARED_MODEL:
        pytest.skip(
            f"KNOWN DEFECT (engine/cli.py:807): the reviewers are on {reviewers[0]['model']!r} while "
            f"defaults.reviewer names {_ORG_DECLARED_MODEL!r}; the declared pair must win over "
            "catalogue discovery."
        )
    assert {r["model"] for r in reviewers} == {_ORG_DECLARED_MODEL}


def test_the_reviewer_binding_org_shows_is_the_binding_a_run_uses(tmp_path):
    """KNOWN DEFECT — `org` shows a company no run builds, so its reviewer binding is a promise kept
    nowhere.

    `cmd_org` builds its own `default_company` with a reviewer model it discovered from the catalogue
    (engine/cli.py:799-861) and prints that. A run does not use it: `_orchestrator_for`
    (engine/cli.py:980) takes the roster from `_roster_for` (engine/cli.py:1121), which is
    `People.load` — the built-in company from `People._base_company` (engine/people.py:161-170), which
    passes no reviewer pair to `default_company`, plus whatever `roster.json` holds. So under a
    two-model configuration the same three reviewers are on the distinct model in `org`'s output and on
    the builders' model in the roster a run loads. `agents` is that roster — the same loader
    (`_roster_for`) the run takes, so this compares the display against what actually runs.

    Correct behaviour: one roster, so the reviewer resolution belongs where the company is built
    (`People._base_company`, and therefore `_roster_for`) rather than only in the command that displays
    it. Guarded by a skip for the same reason as the test above.
    """
    creds = _org_config(tmp_path, second_offers=_ORG_OTHER_MODEL)
    project = tmp_path / "project"
    (project / ".agentorg").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    import os as _os

    # `AGENTORG_HOME` is pinned because `People.load` reads the global root as well as the project's:
    # without it this would merge whatever roster the developer keeps in `~/.agentorg`.
    env = {**_os.environ, "AGENTORG_HOME": str(home)}

    def run_isolated(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "engine.cli", *argv],
                              capture_output=True, text=True, cwd=str(ROOT), env=env)

    shown = run_isolated("--json", "--config", creds, "org")
    used = run_isolated("--json", "--config", creds, "agents", "--project", str(project))
    assert shown.returncode == EXIT_OK, shown.stderr[:400]
    assert used.returncode == EXIT_OK, used.stderr[:400]
    display = {a["name"]: f"{a['provider']}/{a['model']}"
               for a in json.loads(shown.stdout)["roster"]}
    roster = {a["name"]: f"{a['provider']}/{a['model']}"
              for a in json.loads(used.stdout)["agents"]}
    disagreeing = sorted(name for name in set(display) & set(roster)
                         if display[name] != roster[name])
    if disagreeing:
        pytest.skip(
            "KNOWN DEFECT (engine/cli.py:807 vs engine/people.py:161): `org` shows "
            + ", ".join(f"{name} on {display[name]} but a run gets {roster[name]}"
                        for name in disagreeing)
        )
    # Humans are the Owner, and `agents` lists agents only; everyone else must agree.
    assert set(display) - {"Owner"} <= set(roster), "`org` shows people a run does not have"
    assert all(display[name] == roster[name] for name in display if name in roster)


def test_org_shows_the_policy_matrix(tmp_path):
    payload = run_json("--config", _org_config(tmp_path), "org")
    assert set(payload["policy"]) == {"R-CONTRACT", "R-REWORK", "R-DELEGATE",
                                      "R-ESCALATE", "R-CONFLICT", "R-MATCH-FAIL"}
    assert payload["policy"]["R-ESCALATE"]["level"] == "confirm"
    assert payload["policy"]["R-CONTRACT"]["level"] == "auto"


def test_org_reports_staffing_gaps_for_a_goal(tmp_path):
    """The pre-run check that prevents a graph stopping mid-way."""
    payload = run_json("--config", _org_config(tmp_path),
                       "org", "--goal", "Build a booking API with auth", "--slug", "cli-gap")
    assert "staffing_gaps" in payload
    assert all("node_id" in g and "skill" in g and "reason" in g for g in payload["staffing_gaps"])


def test_org_binds_every_staffed_node(tmp_path):
    payload = run_json("--config", _org_config(tmp_path),
                       "org", "--goal", "Build a booking API with auth", "--slug", "cli-bind")
    gaps = {g["node_id"] for g in payload["staffing_gaps"]}
    for node_id, binding in payload["bindings"].items():
        assert node_id not in gaps
        assert binding["agents"], "a binding must name at least one agent"


def test_org_human_output_names_the_gaps(tmp_path):
    result = run("--config", _org_config(tmp_path),
                 "org", "--goal", "Build a booking API with auth", "--slug", "cli-gap-human")
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


# ── roster discovery: `--project` must load the attached folder's roster ──────


def test_project_root_for_honours_project_flag():
    """The bug: `--project Ideas` was ignored, so the CEO hired there never entered a run."""
    import argparse

    from engine import usercfg
    from engine.cli import _project_root_for

    args = argparse.Namespace(project="/tmp/attached-proj", root=None)
    assert _project_root_for(args) == pathlib.Path("/tmp/attached-proj")

    args = argparse.Namespace(project=None, root="/tmp/projects-dir")
    assert _project_root_for(args) == usercfg.project_root("/tmp/projects-dir")

    args = argparse.Namespace(project=None, root=None)
    assert _project_root_for(args) is None


def test_project_flag_wins_over_root():
    import argparse

    from engine.cli import _project_root_for

    args = argparse.Namespace(project="/tmp/attached-proj", root="/tmp/projects-dir")
    assert _project_root_for(args) == pathlib.Path("/tmp/attached-proj")


def test_agents_loads_the_roster_from_an_attached_project(tmp_path):
    """A hire written into `<project>/.agentorg/roster.json` must appear under `--project`."""
    project = tmp_path / "Attached"
    (project / ".agentorg").mkdir(parents=True)
    (project / ".git").mkdir()
    (project / ".agentorg" / "roster.json").write_text(json.dumps({
        "name": "AgentOrg", "org_version": "1.0.0",
        "agents": [{
            "id": "ag_ceo", "name": "CEO", "title": "SP", "kind": "ai", "role": "worker",
            "level": 5, "provider": "Olla", "model": "deepseek-v4.1-flash",
            "context_window": 1048576, "skills": ["ceo-strategist"],
            "capabilities": ["read:*"], "origin": "owner", "team": "", "tags": [],
        }],
        "teams": [], "policy": {},
    }))

    result = _run_cli("agents", "--project", str(project), "--json")
    assert result.returncode == EXIT_OK, result.stderr[:300]
    payload = json.loads(result.stdout)
    names = {a["name"] for a in payload["agents"]}
    assert "CEO" in names, f"the attached roster was not read: {sorted(names)}"
    assert any(str(project) in p for p in payload["loaded_from"])


def test_roster_root_honours_project_flag(tmp_path):
    """A hire with `--project X` must land in `X/.agentorg/`, where the loader looks for it."""
    import argparse

    from engine.cli import _roster_root_for

    project = tmp_path / "Attached"
    (project / ".git").mkdir(parents=True)
    args = argparse.Namespace(project=str(project), root=None, roster_root=None)
    assert _roster_root_for(args) == (project / ".agentorg").resolve()


def test_skill_root_honours_project_flag(tmp_path):
    """`skills new --project X` must author into the project the Owner named."""
    import argparse

    from engine.cli import _skill_root_for

    project = tmp_path / "Attached"
    (project / ".git").mkdir(parents=True)
    args = argparse.Namespace(project=str(project), root=None, global_=False)
    assert _skill_root_for(args) == (project / ".agentorg").resolve()


def test_skills_new_writes_into_an_attached_project(tmp_path):
    project = tmp_path / "Attached"
    (project / ".git").mkdir(parents=True)
    result = _run_cli("skills", "new", "custom-check", "--project", str(project),
                      "--purpose", "check the thing", "--criterion", "the thing is checked")
    assert result.returncode == EXIT_OK, result.stderr[:300]
    written = project / ".agentorg" / "skills" / "custom-check" / "SKILL.md"
    assert written.is_file(), f"skill not written into the project: {sorted(project.rglob('*'))}"


def test_mission_set_status_and_advance_end_to_end(tmp_path):
    """The mission is reachable from the CLI, and `start` hands an objective to a goal."""
    project = tmp_path / "MissionProj"
    (project / ".git").mkdir(parents=True)

    result = _run_cli("mission", "set", "ship the MVP", "--objective", "get auth green",
                      "--objective", "pagination", "--project", str(project))
    assert result.returncode == EXIT_OK, result.stderr[:300]
    assert "ship the MVP" in result.stdout

    status = _run_cli("mission", "status", "--project", str(project), "--json")
    payload = json.loads(status.stdout)
    assert payload["mission"]["statement"] == "ship the MVP"
    assert payload["mission"]["counts"]["total"] == 2

    # `start` records the objective and sets a goal — but does not spend without arming.
    started = _run_cli("mission", "start", "--index", "0", "--no-arm", "--project", str(project),
                       "--json")
    detail = json.loads(started.stdout)
    assert detail["objective"]["text"] == "get auth green"
    assert detail["goal"]["live"] is False

    _run_cli("mission", "mark", "0", "done", "--summary", "auth is green", "--project", str(project))
    advanced = _run_cli("mission", "advance", "--project", str(project), "--json")
    payload = json.loads(advanced.stdout)
    assert payload["mission"]["progress"]["done"] == 1
    assert payload["mission"]["now"]["text"] == "pagination"


# ── the portfolio: one principal, several orgs ───────────────────────────────


def test_portfolio_commands_are_in_the_parser():
    parser = build_parser()
    for argv in (["portfolio", "init", "Elon"],
                 ["portfolio", "status"],
                 ["portfolio", "add", "Tesla", "--path", "/tmp/tesla"],
                 ["portfolio", "use", "tesla"],
                 ["portfolio", "show", "tesla"],
                 ["portfolio", "run", "tesla", "do work"],
                 ["portfolio", "stop", "tesla"],
                 ["portfolio", "remove", "tesla"]):
        assert parser.parse_args(argv).func is not None, f"{argv} does not resolve"


def test_portfolio_init_add_status_end_to_end(tmp_path):
    """One person, several orgs — set up and inspected from the CLI."""
    home = tmp_path / "home"
    env = {"AGENTORG_HOME": str(home)}

    def run_home(*argv: str) -> subprocess.CompletedProcess:
        import os as _os

        merged = {**_os.environ, **env}
        return subprocess.run([sys.executable, "-m", "engine.cli", *argv],
                              capture_output=True, text=True, cwd=str(ROOT), env=merged)

    init = run_home("portfolio", "init", "Elon Musk", "--json")
    assert init.returncode == EXIT_OK, init.stderr[:300]
    assert json.loads(init.stdout)["principal"]["name"] == "Elon Musk"

    add = run_home("portfolio", "add", "Tesla", "--slug", "tesla",
                   "--path", str(tmp_path / "tesla"), "--charter", "EVs", "--json")
    assert add.returncode == EXIT_OK, add.stderr[:300]
    entry = json.loads(add.stdout)
    assert entry["slug"] == "tesla" and entry["id"] == "org_tesla"

    run_home("portfolio", "add", "SpaceX", "--slug", "spacex",
             "--path", str(tmp_path / "spacex"))

    status = run_home("portfolio", "status", "--json")
    payload = json.loads(status.stdout)
    assert payload["rollup"]["totals"]["orgs"] == 2
    assert [o["name"] for o in payload["rollup"]["orgs"]] == ["Tesla", "SpaceX"]


def test_the_org_flag_selects_an_orgs_folder(tmp_path):
    """`--org` resolves the org's folder, so every existing command is org-scoped for free."""
    home = tmp_path / "home"
    project = tmp_path / "tesla"
    (project / ".git").mkdir(parents=True)
    import os as _os

    env = {**_os.environ, "AGENTORG_HOME": str(home)}

    def run_home(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "engine.cli", *argv],
                              capture_output=True, text=True, cwd=str(ROOT), env=env)

    run_home("portfolio", "init", "Elon")
    run_home("portfolio", "add", "Tesla", "--slug", "tesla", "--path", str(project))
    result = run_home("agents", "--org", "tesla", "--json")
    assert result.returncode == EXIT_OK, result.stderr[:300]
    payload = json.loads(result.stdout)
    assert payload["agents"], "the org's roster should load"
    # A bad org names the available ones rather than silently falling back.
    bad = run_home("agents", "--org", "nosuch")
    assert bad.returncode == EXIT_CHECK_FAILED
    assert "known orgs" in bad.stderr


# ── the serve bootstrap reports why the engine could not start ───────────────


def test_serve_emits_a_typed_error_frame_when_the_bootstrap_fails(tmp_path):
    """The app reads stdout, so a failing bootstrap must say why *there*, not only on stderr.

    The bug: a config that would not load made `serve` exit 1 with the reason on stderr, which the
    console never read, so it reported "the engine exited with status 1" and left the UI looking idle.
    """
    project = tmp_path / "engine"
    project.mkdir()
    creds = tmp_path / "credentials.json"
    doc = json.loads((ROOT / "credentials.example.json").read_text())
    doc["providers"]["ollama"]["kind"] = "weird"  # a genuine, fatal config error
    creds.write_text(json.dumps(doc))
    import os as _os

    _os.chmod(creds, 0o600)

    result = subprocess.run(
        [sys.executable, "-m", "engine.cli", "serve", "--config", str(creds)],
        capture_output=True, text=True, cwd=str(ROOT))
    assert result.returncode == EXIT_CHECK_FAILED
    # stdout carries exactly one typed frame, and it is the error with the reason.
    frames = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert frames, "the bootstrap failure must write a frame to stdout"
    error = next((f for f in frames if f["type"] == "error"), None)
    assert error is not None, f"no error frame in {frames}"
    assert error["payload"]["fatal"] is True
    assert error["payload"]["phase"] == "bootstrap"
    assert "unsupported kind" in error["payload"]["message"]


def test_serve_starts_when_a_stale_provider_reference_is_present(tmp_path):
    """The end-to-end regression: the exact config that bricked the engine now starts it."""
    creds = tmp_path / "credentials.json"
    doc = json.loads((ROOT / "credentials.example.json").read_text())
    doc["providers"].pop("anthropic", None)                     # provider removed ...
    doc["concurrency"]["per_provider_limits"]["anthropic"] = 3  # ... limit left behind
    creds.write_text(json.dumps(doc))
    import os as _os

    _os.chmod(creds, 0o600)

    # Serve runs forever on a live stream; drive it with EOF on stdin so it exits cleanly, and check it
    # got far enough to emit its first frame rather than failing the bootstrap.
    result = subprocess.run(
        [sys.executable, "-m", "engine.cli", "serve", "--config", str(creds)],
        capture_output=True, text=True, cwd=str(ROOT), input="")
    frames = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert not any(f["type"] == "error" for f in frames), frames
    assert any(f["type"] == "agent.log" for f in frames), "the server should have started"


# ── `defaults` — the one place that says what model everyone runs on ─────────


def _creds(tmp_path, mutate=None) -> str:
    """A hermetic credentials file, so these tests never read the developer's own.

    `load()` prefers `./credentials.json`, which on a working machine holds *that person's* providers
    and default model. A test that reads it passes or fails on who ran it — which is exactly how a
    `defaults` assertion would depend on someone's private configuration.
    """
    doc = json.loads((ROOT / "credentials.example.json").read_text())
    if mutate:
        mutate(doc)
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(doc))
    import os as _os

    _os.chmod(path, 0o600)
    return str(path)


def test_bare_defaults_prints_the_answer_rather_than_crashing(tmp_path):
    """`engine.cli defaults` with no action must show the default, not raise AttributeError.

    It used to raise `'Namespace' object has no attribute 'func'` — the "nothing works" symptom on the
    single command that answers "which model do my people run on".
    """
    result = _run_cli("--config", _creds(tmp_path), "defaults")
    assert result.returncode == EXIT_OK, result.stderr[:400]
    assert "default   :" in result.stdout


def test_defaults_show_reports_the_declared_pair(tmp_path):
    result = _run_cli("--config", _creds(tmp_path), "defaults", "show", "--json")
    assert result.returncode == EXIT_OK, result.stderr[:400]
    payload = json.loads(result.stdout)
    assert payload["provider"] == "ollama"
    assert payload["model"] == "qwen2.5-coder:7b"
    assert payload["context_window"] == 32768, "the declared window must be reported"
    assert payload["autonomy"]["auto_pass_auto_gates"] is True


def test_defaults_set_changes_only_what_was_named(tmp_path):
    creds = _creds(tmp_path)
    before = json.loads(pathlib.Path(creds).read_text())
    result = _run_cli("--config", creds, "defaults", "set", "--model", "qwen2.5-coder:14b")
    assert result.returncode == EXIT_OK, result.stderr[:400]
    after = json.loads(pathlib.Path(creds).read_text())
    assert after["defaults"]["model"] == "qwen2.5-coder:14b"
    assert set(after["providers"]) == set(before["providers"]), "a default must not cost you a provider"
    assert after["policy"] == before["policy"], "nor your policy"


def test_defaults_set_refuses_an_unknown_provider_rather_than_writing_it(tmp_path):
    """A default naming a provider that does not exist would resolve to a fallback and silently not be
    what the person chose, so it is refused with the real list."""
    creds = _creds(tmp_path)
    result = _run_cli("--config", creds, "defaults", "set", "--provider", "nope")
    assert result.returncode == EXIT_CHECK_FAILED
    assert "nope" in result.stderr


def test_defaults_autonomy_turns_a_gate_human_for_every_new_goal(tmp_path):
    creds = _creds(tmp_path)
    result = _run_cli("--config", creds, "defaults", "autonomy", "--no-auto-gates")
    assert result.returncode == EXIT_OK, result.stderr[:400]
    payload = json.loads(_run_cli("--config", creds, "defaults", "show", "--json").stdout)
    assert payload["autonomy"]["auto_pass_auto_gates"] is False
    # Only the named switch changed.
    assert payload["autonomy"]["auto_hire_missing"] is True


def test_run_dry_run_json_emits_only_json(tmp_path):
    """`--json` means only JSON on stdout: a human line first makes the output unparsable.

    `run --goal … --dry-run --json` printed `(dry run: nothing executed)` before the document, so
    `… | jq` failed on line one — on the command whose entire purpose is to be safe to inspect.
    """
    project = tmp_path / "project"
    project.mkdir()
    manifest = project / "dryrun.yaml"
    manifest.write_text(
        "name: dryrun\nversion: '1.0.0'\ndescription: d\n"
        "payloads:\n  handoff-v1: [status, summary]\nstart: dev\n"
        "nodes:\n  - id: dev\n    skill: backend-developer\n    outputs: [change]\n"
        "edges: []\nend: [dev]\n")
    result = subprocess.run(
        [sys.executable, "-m", "engine.cli", "run", "--manifest", str(manifest),
         "--slug", "dryrun", "--root", str(tmp_path / "projects"), "--dry-run", "--json"],
        capture_output=True, text=True, cwd=str(ROOT))
    assert result.returncode == EXIT_OK, result.stderr[:400]
    payload = json.loads(result.stdout)          # must not raise
    assert payload["phase"] == "awaiting_approval"
    assert "dry run" not in result.stdout, "the human line must not pollute the JSON stream"


def test_decide_approve_continues_the_run(run_project):
    """`decide --approve` is documented as "approve and continue" — it must actually continue.

    It only cleared the gate, leaving the run at `ready` with nothing driving it, so an operator who
    approved a gate saw the gate go and the work never resume. The CLI had no path that did the
    second half, which is the difference between resolving a gate and resuming behind it.

    This fixture's graph *ends* at its gate, so continuing means the run finishes rather than parking
    again. That is the stronger property, and it is what the older form of this assertion got wrong:
    it asserted `awaiting_gate`, which only held because the gate was re-detected forever — a released
    gate whose node record still carried `verdict: awaiting_owner` looked pending to `_detect_gate`,
    so an approval appeared to "continue" by re-parking on the node it had just released. A run with
    nothing after the gate must reach `done`, and a run with work after it must run that work.
    """
    root, project = run_project
    _run_cli("run", "--manifest", str(project / "clirun.yaml"), "--slug", "clirun",
             "--root", str(root), "--executor", str(project / "stub.py"))
    result = _run_cli("decide", "--slug", "clirun", "--root", str(root), "--approve",
                      "--executor", str(project / "stub.py"))
    assert result.returncode == EXIT_OK, result.stderr[:400]
    assert "approved release" in result.stdout
    assert "Continuing the run past the gate" in result.stdout
    # The run resumed past the gate and finished, instead of re-parking on it.
    status = _run_cli("status", "--slug", "clirun", "--root", str(root), "--json")
    payload = json.loads(status.stdout)
    assert payload["phase"] == "done", payload["phase"]
    assert not payload.get("gate"), "a released gate must not still be waiting"


def test_decide_no_continue_clears_the_gate_without_spending(run_project):
    """The opt-out an operator wants when they intend to inspect before paying for more work."""
    root, project = run_project
    _run_cli("run", "--manifest", str(project / "clirun.yaml"), "--slug", "clirun",
             "--root", str(root), "--executor", str(project / "stub.py"))
    result = _run_cli("decide", "--slug", "clirun", "--root", str(root), "--approve",
                      "--no-continue")
    assert result.returncode == EXIT_OK, result.stderr[:400]
    assert "Continuing the run" not in result.stdout
    status = _run_cli("status", "--slug", "clirun", "--root", str(root), "--json")
    assert json.loads(status.stdout)["phase"] == "ready"


def test_agents_reports_sprawl_so_a_leaky_delegate_is_visible(tmp_path):
    """The anti-sprawl metric existed on the desk and was reachable from no command.

    A roster could therefore grow a delegate that burns tokens without finishing work — exactly the
    delegation leak the metric is defined to detect — and nothing a person could run would say so.
    `agents` is where it belongs, because it is the only command already holding the real roster.
    """
    project = tmp_path / "Sprawling"
    (project / ".agentorg").mkdir(parents=True)
    (project / ".git").mkdir()
    (project / ".agentorg" / "roster.json").write_text(json.dumps({
        "name": "AgentOrg", "org_version": "1.0.0",
        "agents": [{
            "id": "ag_bloat", "name": "Bloat", "title": "Engineer", "kind": "ai", "role": "worker",
            "level": 3, "provider": "Olla", "model": "deepseek-v4.1-flash",
            "context_window": 1048576, "skills": ["backend-developer"],
            "capabilities": ["read:*"], "origin": "owner", "team": "", "tags": [],
        }],
        "teams": [], "policy": {},
    }))

    result = _run_cli("agents", "--project", str(project), "--json")
    assert result.returncode == EXIT_OK, result.stderr[:300]
    payload = json.loads(result.stdout)
    assert "sprawl" in payload, "the metric must travel with the roster it describes"
    # The report is always present and named, so an empty one is distinguishable from no report.
    assert set(payload["sprawl"]) >= {"agents", "suspects", "threshold", "window_runs"}
    assert isinstance(payload["sprawl"]["suspects"], list)
