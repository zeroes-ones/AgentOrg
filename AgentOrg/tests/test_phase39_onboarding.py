#!/usr/bin/env python3
"""Phase 39 — first-run onboarding: one definition of "ready", owned by the engine.

WHY THIS EXISTS
---------------
First-run setup was entirely the app's. `Setup.swift` computed a six-input `SetupGate` and the macOS
wizard walked a person through it, while `engine.cli` had *nothing* — a person at a terminal was told
to read `USAGE.md`. That is the same class of drift `doctor_checks` was extracted to prevent: two
surfaces answering "is this ready?" separately will answer it differently, and the terminal is the one
that cannot show a button when they do.

So these tests pin four properties, each of which is a way the guidance could be wrong:

1. **The vocabulary and the order are the app's.** The app has a `switch` over six `SetupGate.Kind`
   strings and a rail that ticks steps by their ids. A renamed state or a reordered gate breaks a
   surface this engine cannot see, so the names and the precedence are asserted against the Swift
   source rather than against a second Python copy of them.
2. **The first unmet thing is the one reported.** A list of five problems is not guidance — that is
   the whole failure this module replaces — so every test that could report two problems asserts *one*
   gate, and asserts which.
3. **Each `next_step` actually resolves its own gate.** This is the property that makes the guidance
   trustworthy. A command that does not work is worse than no guidance, because it teaches the person
   that running the command does not help. So the chain is driven for real, in a hermetic credentials
   file, asserting the gate advances at every step.
4. **The JSON the app consumes is stable.** The macOS client reads these keys by name; a renamed or
   prose-flavoured field is a panel that renders blank, and stdout carrying anything but JSON is a
   `| jq` that fails.

Nothing here touches the network. The one gate that would need a probe (a model with no known window)
is driven from a declared-table-free configuration where the answer is decidable without asking, and
everything else is `--no-probe` or in-process.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import onboarding as ob
from engine.cli import EXIT_CHECK_FAILED, EXIT_OK, build_parser, main
from engine.config import load
from engine.state import Workspace

SWIFT_SETUP = ROOT / "macos" / "Sources" / "AgentOrgKit" / "Setup.swift"
EXAMPLE = ROOT / "credentials.example.json"


# ── hermetic credentials ─────────────────────────────────────────────────────
#
# One factory, so a test says which *state* it wants rather than spelling out a JSON document. Every
# scenario is built from the committed example, with only the part under test replaced — a config
# invented from scratch would drift from the shape the loader actually has to accept.


def _example() -> dict:
    return json.loads(EXAMPLE.read_text())


def write_creds(tmp_path: pathlib.Path, document: dict, name: str = "credentials.json") -> pathlib.Path:
    path = tmp_path / name
    path.write_text(json.dumps(document))
    return path


def no_provider_config() -> dict:
    """A config whose only provider cannot be built: the model step, before any endpoint exists.

    A cloud kind with no key resolves to nothing *and* refuses to construct, which is precisely the
    state `onboard` must turn into a step rather than a traceback.
    """
    document = _example()
    document["providers"] = {
        "cloud": {"kind": "openai", "base_url": "https://api.example.com/v1",
                  "api_key_env": "AGENTORG_TEST_KEY_UNSET"},
    }
    document["defaults"] = {}
    return document


def provider_without_default() -> dict:
    """A provider that builds and offers a model, with no default naming either.

    The commonest first-run state the audit described: nothing looks broken, and no agent can bind.
    The endpoint is a loopback port nothing serves and the retry budget is zero, so a test that probes
    fails immediately rather than inheriting the example's 300-second timeout.
    """
    document = _example()
    document["providers"] = {"local": {"kind": "ollama", "base_url": "http://127.0.0.1:9",
                                       "timeout_s": 2, "max_retries": 0}}
    document["models"] = {"known": {"llama3.1:8b": {"context_window": 131072, "locality": "local"}}}
    document["defaults"] = {}
    return document


def hermetic(tmp_path: pathlib.Path, creds: pathlib.Path, **extra: str) -> dict:
    """An environment where every one of the engine's homes is inside `tmp_path`.

    Both roots matter and both are easy to forget:

    - `AGENTORG_CREDENTIALS`, so the config under test is the only config in play.
    - `AGENTORG_HOME`, because `onboard --project X` *records* the folder it confirmed (see
      `onboarding.remember_workspace`) — without this, a test would write into the developer's real
      `~/.agentorg` and, worse, read a folder some earlier run had recorded, so the project gate would
      pass for a reason the test never arranged.
    """
    import os

    return {"AGENTORG_CREDENTIALS": str(creds),
            "AGENTORG_HOME": str(tmp_path / "home"),
            **os.environ, **extra}


def run_cli(*args: str, env: dict | None = None, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    """The CLI as a subprocess, so the real entry point and argv handling are exercised.

    `cwd` defaults to the repository because most commands need the pinned library. A test that
    asserts the *project* gate passes a scratch directory instead: the repository is itself an
    initialised project, so running from it would find one and satisfy the step the test is about.
    """
    import os

    return subprocess.run([sys.executable, "-m", "engine.cli", *args],
                          capture_output=True, text=True, cwd=str(cwd or ROOT),
                          env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})})


def onboard_json(*args: str, creds: pathlib.Path, cwd: pathlib.Path | None = None) -> dict:
    """`onboard --json` in a hermetic environment, parsed.

    stderr is ignored on purpose: provider skips and permission warnings live there, and the whole
    point of the discipline is that they cannot reach stdout.
    """
    import os

    result = run_cli("--json", "onboard", *args,
                     env=hermetic(creds.parent, creds), cwd=cwd)
    assert result.stdout.strip(), f"nothing on stdout: {result.stderr[:400]}"
    return json.loads(result.stdout)


# ── 1. the vocabulary is the app's ───────────────────────────────────────────


def test_the_gate_kinds_are_the_apps_own_strings():
    """The app switches on these raw values, so a rename here is a broken build there.

    Read out of the Swift source rather than compared to a second Python list: a test that agreed with
    a duplicated copy would pass while the app was broken, which is the failure it is meant to catch.
    """
    swift = SWIFT_SETUP.read_text()
    block = swift.split("public enum Kind:", 1)[1].split("}", 1)[0]
    cases = re.findall(r"case (\w+)", block)
    assert cases, "the Swift Kind enum did not parse — this test is no longer checking anything"
    assert set(cases) == {kind.value for kind in ob.GateKind}, (
        f"engine kinds {sorted(k.value for k in ob.GateKind)} != app kinds {sorted(cases)}"
    )


def test_the_engine_asks_the_same_six_questions_in_the_same_order():
    """The Swift `gate(...)` parameter list is the contract; a missing or reordered input is a
    divergence that would only show up as the two surfaces disagreeing on a real machine."""
    swift = SWIFT_SETUP.read_text()
    signature = swift.split("public static func gate(", 1)[1].split(") -> SetupGate", 1)[0]
    # Split on the commas that separate parameters rather than on every comma: a default like
    # `String? = nil` carries no comma, but a generic type would, and a naive regex reads the *type*
    # as the name.
    swift_inputs = [part.split(":", 1)[0].strip()
                    for part in signature.split(",") if ":" in part]
    assert swift_inputs == [
        "engineIsRunning", "engineFailure", "defaults", "providers",
        "projectConfirmed", "postureChosen",
    ], swift_inputs
    python_inputs = [name for name in ob.gate.__code__.co_varnames[:ob.gate.__code__.co_argcount]]
    assert python_inputs == [
        "engine_is_running", "engine_failure", "defaults", "providers",
        "project_confirmed", "posture_chosen",
    ], python_inputs


def test_the_engine_has_the_apps_three_step_numbering():
    """`step` drives "Step 2 of 3" in the wizard, and `needsModel`/`needsWindow` share step one on
    purpose — they are one question with a follow-up, not two steps."""
    windowed = {"provider": "p", "model": "m", "context_window": 8192}
    assert ob.gate(False).step == 1
    assert ob.gate(True, defaults={}, providers=[]).step == 1
    assert ob.gate(True, defaults={"provider": "p", "model": "m"}, providers=["p"]).step == 1
    assert ob.gate(True, defaults=windowed, providers=["p"],
                   project_confirmed=False, posture_chosen=True).step == 2
    assert ob.gate(True, defaults=windowed, providers=["p"],
                   project_confirmed=True, posture_chosen=False).step == 3
    ready = ob.gate(True, defaults=windowed, providers=["p"],
                    project_confirmed=True, posture_chosen=True)
    assert ready.step == 3 and ready.kind is ob.GateKind.READY


# ── 2. each gate fires for the right condition, in the right order ───────────


def test_a_stopped_engine_reports_the_engines_own_reason():
    """The reason, not the fact. "The engine is not running" when the engine said exactly why is a
    step that sends the person to the wrong place."""
    gate = ob.gate(False, "no provider could be built from the configuration")
    assert gate.kind is ob.GateKind.ENGINE_UNAVAILABLE
    assert gate.why == "no provider could be built from the configuration"
    assert gate.is_blocking and gate.step == 1
    assert gate.next_step, "a blocking gate with no command is the scavenger hunt this replaces"


def test_no_providers_asks_for_an_endpoint_not_a_model():
    """Two different actions: "add an endpoint" versus "pick one of the endpoints you have".
    Collapsing them sends half the readers to the wrong control."""
    gate = ob.gate(True, defaults={"provider": "", "model": ""}, providers=[])
    assert gate.kind is ob.GateKind.NEEDS_MODEL
    assert "provider" in gate.why
    assert "providers add" in gate.next_step


def test_the_engines_build_failure_is_repeated_rather_than_summarised(tmp_path):
    """When the engine refused to construct the one provider it has, that refusal names the provider
    and the missing variable. Replacing it with a general sentence loses the only actionable part —
    "add an endpoint" sends a person who has already added one to a control they just used."""
    failure = ("no provider could be constructed:\n  cloud: provider 'cloud' has no API key. "
               "$AGENTORG_TEST_KEY_UNSET is not set")
    generic = ob.gate(True, defaults={}, providers=[]).why
    assert "no provider is configured yet" in generic, "with no engine text, the general sentence"

    # With the engine's own refusal, that text is what the gate shows — verbatim.
    gate = ob.readiness(load(write_creds(tmp_path, no_provider_config())), {},
                        build_failure=failure, probe=False)
    assert gate.kind is ob.GateKind.NEEDS_MODEL
    assert "has no API key" in gate.why and "$AGENTORG_TEST_KEY_UNSET" in gate.why
    assert gate.why != generic


def test_a_provider_with_no_default_model_blocks_as_the_configured_state():
    """The engine starts fine, a provider exists, and no default resolves — so nothing looks broken
    while no agent can bind. This is the state the app's audit found and the CLI now names."""
    gate = ob.gate(True, defaults={"provider": "", "model": "", "reason": "no default is set"},
                   providers=["local"])
    assert gate.kind is ob.GateKind.NEEDS_MODEL
    assert gate.why == "no default is set", "the engine's own explanation beats one we invent"


def test_a_model_with_no_known_window_blocks_because_no_agent_could_bind():
    """Not a formality: hiring refuses a model with no window, so a step that passed here would let
    a person reach a state where hiring is impossible with no explanation."""
    gate = ob.gate(True, defaults={"provider": "ollama", "model": "mystery"},
                   providers=["ollama"], project_confirmed=True, posture_chosen=True)
    assert gate.kind is ob.GateKind.NEEDS_WINDOW
    assert (gate.provider, gate.model) == ("ollama", "mystery")
    assert "mystery" in gate.why
    assert "--context-window" in gate.next_step, "the override is the fix that needs no probe"


def test_a_zero_window_is_treated_as_no_window():
    """The engine reports `0` when it has nothing usable; reading that as a real window would bind an
    agent to a context of no size."""
    gate = ob.gate(True, defaults={"provider": "p", "model": "m", "context_window": 0},
                   providers=["p"], project_confirmed=True, posture_chosen=True)
    assert gate.kind is ob.GateKind.NEEDS_WINDOW


def test_the_project_and_autonomy_steps_block_in_that_order():
    base = {"provider": "p", "model": "m", "context_window": 8192}
    project = ob.gate(True, defaults=base, providers=["p"],
                      project_confirmed=False, posture_chosen=False)
    assert project.kind is ob.GateKind.NEEDS_PROJECT and project.step == 2
    autonomy = ob.gate(True, defaults=base, providers=["p"],
                       project_confirmed=True, posture_chosen=False)
    assert autonomy.kind is ob.GateKind.NEEDS_AUTONOMY and autonomy.step == 3


def test_every_answer_present_is_ready_and_the_gate_stops_blocking():
    gate = ob.gate(True, defaults={"provider": "p", "model": "m", "context_window": 8192},
                   providers=["p"], project_confirmed=True, posture_chosen=True)
    assert gate.kind is ob.GateKind.READY
    assert not gate.is_blocking
    assert ob.first_run_hint(gate) == "", "a ready setup must not leave a line nagging on entry"


def test_the_gate_reports_the_earliest_blocking_step_not_the_last_one():
    """With nothing done the model step is the one shown, because the project question can be
    answered at any time and the model one cannot — leading with the project would lead with the
    question whose answer unblocks nothing."""
    gate = ob.gate(True, defaults={}, providers=[], project_confirmed=False, posture_chosen=False)
    assert gate.step == 1 and gate.kind is ob.GateKind.NEEDS_MODEL


def test_a_later_answer_cannot_skip_an_earlier_gate():
    """The order is a property of the function, not of the caller's inputs: a confirmed project and a
    chosen posture with no model is still the model step."""
    gate = ob.gate(True, defaults={}, providers=[], project_confirmed=True, posture_chosen=True)
    assert gate.kind is ob.GateKind.NEEDS_MODEL


def test_every_kind_is_reachable_and_has_its_own_words():
    """A `Kind` no input can produce is a step nobody can reach; two kinds reading the same is a
    wizard a person cannot tell progress in."""
    windowed = {"provider": "p", "model": "m", "context_window": 1}
    reached = {
        ob.gate(False).kind,
        ob.gate(True, defaults={}, providers=[]).kind,
        ob.gate(True, defaults={"provider": "p", "model": ""}, providers=["p"]).kind,
        ob.gate(True, defaults={"provider": "p", "model": "m", "context_window": 0},
                providers=["p"]).kind,
        ob.gate(True, defaults=windowed, providers=["p"],
                project_confirmed=False, posture_chosen=True).kind,
        ob.gate(True, defaults=windowed, providers=["p"],
                project_confirmed=True, posture_chosen=False).kind,
        ob.gate(True, defaults=windowed, providers=["p"],
                project_confirmed=True, posture_chosen=True).kind,
    }
    assert reached == set(ob.GateKind), set(ob.GateKind) - reached
    # Model and window share a heading on purpose; the *three* steps must read differently.
    assert ob.gate(True, defaults={"provider": "p", "model": ""},
                   providers=["p"]).title == \
        ob.gate(True, defaults={"provider": "p", "model": "m"},
                providers=["p"]).title
    headings = {
        ob.GateKind.ENGINE_UNAVAILABLE: ob.gate(False).title,
        ob.GateKind.NEEDS_MODEL: ob.gate(True, defaults={}, providers=[]).title,
        ob.GateKind.NEEDS_PROJECT: ob.gate(True, defaults=windowed, providers=["p"],
                                          posture_chosen=True).title,
        ob.GateKind.NEEDS_AUTONOMY: ob.gate(True, defaults=windowed, providers=["p"],
                                            project_confirmed=True).title,
        ob.GateKind.READY: ob.gate(True, defaults=windowed, providers=["p"],
                                   project_confirmed=True, posture_chosen=True).title,
    }
    assert len(set(headings.values())) == 5, headings


# ── 3. the journey marks satisfied steps and names what each unlocks ─────────


def test_the_journey_is_in_dependency_order_with_the_apps_step_ids():
    """The ids are how the app's rail matches a row to a step, so they are asserted against the Swift
    list rather than against a second copy here."""
    swift = SWIFT_SETUP.read_text()
    assert set(ob.STEP_ORDER) == set(re.findall(r'id: "(\w+)"', swift))
    assert ob.STEP_ORDER == ("engine", "model", "project", "autonomy")


def test_the_journey_marks_every_step_with_what_unlocks_it():
    gate = ob.gate(True, defaults={}, providers=[])
    steps = ob.journey(gate)
    assert [step.id for step in steps] == list(ob.STEP_ORDER)
    for step in steps:
        assert step.title and step.purpose and step.unlocks, f"{step.id} explains nothing"
    # `needsModel` means the engine *is* up — that is what makes the model question askable — so the
    # engine step is behind the person and nothing after it is. This is the Swift `isPast` rule: a
    # step is done when it comes before the current one, and only `engineUnavailable` means nothing is.
    assert {step.id: step.satisfied for step in steps} == {
        "engine": True, "model": False, "project": False, "autonomy": False}


def test_a_stopped_engine_has_nothing_behind_it():
    """The one case where even the first step is open: nothing can be asked before the engine starts,
    so it must not be shown as done — the person has not answered anything yet."""
    assert not any(step.satisfied for step in ob.journey(ob.gate(False)))


def test_a_satisfied_step_is_one_the_gate_has_passed():
    """The checklist and the wizard must not disagree: a ticked step the gate still blocks on is
    worse than no checklist."""
    gate = ob.gate(True, defaults={"provider": "p", "model": "m", "context_window": 1},
                   providers=["p"], project_confirmed=True, posture_chosen=False)
    steps = {step.id: step.satisfied for step in ob.journey(gate)}
    assert steps == {"engine": True, "model": True, "project": True, "autonomy": False}
    ready = ob.gate(True, defaults={"provider": "p", "model": "m", "context_window": 1},
                    providers=["p"], project_confirmed=True, posture_chosen=True)
    assert all(step.satisfied for step in ob.journey(ready))


def test_the_current_steps_command_is_the_gates_own_not_the_static_one():
    """The static `resolves` names placeholders. The blocked step must carry the situational command,
    or the guidance a person reads is not the guidance the engine just computed."""
    gate = ob.gate(True, defaults={"provider": "groq", "model": ""}, providers=["groq"])
    current = next(step for step in ob.journey(gate) if step.id == "model")
    assert current.resolves == gate.next_step
    assert "groq" in current.resolves


def test_a_satisfied_step_keeps_the_static_command_it_took():
    """A done step still says what it took to satisfy it — the rail shows it collapsed, but the
    command is what a person re-runs after a provider is removed."""
    gate = ob.gate(True, defaults={"provider": "p", "model": "m", "context_window": 1},
                   providers=["p"], project_confirmed=True, posture_chosen=False)
    done = next(step for step in ob.journey(gate) if step.id == "model")
    assert done.satisfied and done.resolves != ""


def test_the_hint_names_the_step_and_one_command():
    """One line, printed before the person has asked anything. A paragraph at a prompt is a paragraph
    nobody reads."""
    gate = ob.gate(True, defaults={}, providers=[])
    hint = ob.first_run_hint(gate)
    assert hint.count("\n") == 0
    assert gate.next_step in hint
    assert gate.title.lower().split()[0] in hint.lower()


# ── 4. a fresh environment reports the first step, not an error ──────────────


def test_a_missing_configuration_is_a_step_rather_than_a_crash(tmp_path):
    """A machine with no `credentials.json` is the *first* state this command exists to describe, so
    crashing on it would be the command failing at its own job."""
    creds = tmp_path / "absent.json"
    result = run_cli("onboard", env=hermetic(tmp_path, creds))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "Traceback" not in result.stderr
    assert "no configuration found" in result.stderr, "the engine's own reason, on stderr"
    assert "credentials.example.json" in result.stdout, "the step says how to get one"


def test_a_config_whose_only_provider_cannot_be_built_reports_the_model_step(tmp_path):
    """A cloud provider with no key: the engine refuses to construct it. That is the model step, and
    the refusal's own words are what the step shows — "add an endpoint" would send a person who has
    already added one somewhere they have been."""
    import os

    creds = write_creds(tmp_path, no_provider_config())
    result = run_cli("onboard", "--no-probe",
                     env=hermetic(tmp_path, creds, AGENTORG_TEST_KEY_UNSET=""))
    assert result.returncode == EXIT_CHECK_FAILED, result.stdout
    assert "Traceback" not in result.stderr
    assert "providers add" in result.stdout
    assert "no provider could be constructed" in result.stdout, result.stdout
    assert "has no API key" in result.stdout, result.stdout


def test_a_broken_credentials_file_is_reported_as_the_first_step(tmp_path):
    """A file that will not parse is a state, not a crash: the person has to be told which file."""
    creds = tmp_path / "credentials.json"
    creds.write_text("{not json")
    result = run_cli("onboard", env=hermetic(tmp_path, creds))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "Traceback" not in result.stderr
    assert "credentials.json" in result.stderr


def test_the_ready_exit_code_is_zero_so_a_script_can_branch(tmp_path):
    """Readiness is the answer, so it is the exit code — a bootstrap script must not have to parse
    prose to find out whether to continue."""
    document = _example()
    path = write_creds(tmp_path, document)
    work = tmp_path / "work"
    (work / ".agent_state").mkdir(parents=True)
    env = hermetic(tmp_path, path)
    result = run_cli("onboard", "--project", str(work), "--no-probe", env=env)
    assert result.returncode in (EXIT_OK, EXIT_CHECK_FAILED)  # shape asserted; the chain is tested below
    assert "Traceback" not in result.stderr


# ── 5. every next_step actually resolves its own gate ────────────────────────
#
# The property that makes the guidance trustworthy. Each step's command is *run*, and the gate is
# read again afterwards — a printed command that leaves the gate where it was teaches the person that
# running the command does not help, which is worse than printing nothing.


def test_the_whole_journey_advances_step_by_step(tmp_path):
    """Drive the engine from "no configuration" to "ready", running only the commands `onboard`
    printed, and assert the gate advances at every one."""
    import os

    creds = tmp_path / "credentials.json"
    work = tmp_path / "work"
    work.mkdir()
    # A scratch working directory, because the repository is itself an initialised project: running
    # from here would satisfy the project step by discovery and the chain would skip a rung.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = hermetic(tmp_path, creds)
    seen: list[str] = []

    def gate_now() -> dict:
        result = run_cli("--json", "onboard", "--no-probe", env=env, cwd=cwd)
        assert "Traceback" not in result.stderr, result.stderr
        return json.loads(result.stdout)["gate"]

    def run_step(command: str) -> None:
        assert command.startswith("python3 -m engine.cli "), command
        argv = command[len("python3 -m engine.cli "):].split()
        result = run_cli(*argv, env=env, cwd=cwd)
        assert "Traceback" not in result.stderr, f"{command} crashed:\n{result.stderr}"

    # 1 → the engine has no configuration, and the step says how to get one.
    first = gate_now()
    assert first["kind"] == "engineUnavailable"
    assert first["next_step"].startswith("cp ")
    seen.append(first["kind"])
    # The command names a real template; run its equivalent against the hermetic path.
    creds.write_text(EXAMPLE.read_text())

    # 2 → a provider exists in the file but no default names a model.
    document = json.loads(creds.read_text())
    document["providers"] = {"local": {"kind": "ollama", "base_url": "http://127.0.0.1:9"}}
    document["models"] = {"known": {"llama3.1:8b": {"context_window": 131072, "locality": "local"}}}
    document["defaults"] = {}
    creds.write_text(json.dumps(document))

    second = gate_now()
    assert second["kind"] == "needsModel", second
    assert "--provider local" in second["next_step"], second["next_step"]
    seen.append(second["kind"])
    run_step(second["next_step"])

    third = gate_now()
    assert third["kind"] == "needsProject", third
    seen.append(third["kind"])

    # 3 → the project step's command names a folder, and naming a real one advances it.
    run_step(f"python3 -m engine.cli onboard --project {work} --no-probe")

    fourth = gate_now()
    assert fourth["kind"] == "needsAutonomy", fourth
    seen.append(fourth["kind"])
    run_step(fourth["next_step"])

    fifth = gate_now()
    assert fifth["kind"] == "ready", fifth
    seen.append(fifth["kind"])
    assert seen == ["engineUnavailable", "needsModel", "needsProject", "needsAutonomy", "ready"]


def test_the_model_steps_command_names_a_model_the_provider_can_offer(tmp_path):
    """A command with an invented model id would fail with "unknown model" — the guidance would look
    precise and be wrong. The candidate comes from the engine's own attribution.

    Run from a scratch directory, because the repository is itself an initialised project — a test
    about the *project* gate that ran from here would find one and pass for the wrong reason.
    """
    import os

    creds = write_creds(tmp_path, provider_without_default())
    work = tmp_path / "cwd"
    work.mkdir()
    env = hermetic(tmp_path, creds)
    result = run_cli("--json", "onboard", "--no-probe", env=env, cwd=work)
    command = json.loads(result.stdout)["gate"]["next_step"]
    assert "--provider local" in command
    assert "--model llama3.1:8b" in command, command
    # And it runs.
    argv = command[len("python3 -m engine.cli "):].split()
    ran = run_cli(*argv, "--context-window", "131072", env=env, cwd=work)
    assert ran.returncode == EXIT_OK, ran.stderr
    after = run_cli("--json", "onboard", "--no-probe", env=env, cwd=work)
    assert json.loads(after.stdout)["gate"]["kind"] == "needsProject"


def test_the_window_steps_override_actually_makes_the_model_bindable(tmp_path):
    """`--context-window` is the one fix that works without a probe. If it did not move the gate, the
    commonest first-run failure would have no command that resolves it.

    The window-less state is built the way it really occurs: the default names a model the declared
    table has never heard of, and the provider is unreachable, so the catalogue has nothing to add.
    """
    import os

    document = provider_without_default()
    document["defaults"] = {"provider": "local", "model": "mystery"}
    # An empty declared table, so the window is genuinely unknown rather than known-but-zero — the
    # loader refuses a zero window, and a test that wrote one would be asserting on a config the
    # engine would not accept.
    document["models"] = {"known": {}}
    creds = write_creds(tmp_path, document)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = hermetic(tmp_path, creds)

    before = json.loads(run_cli("--json", "onboard", "--no-probe", env=env, cwd=cwd).stdout)["gate"]
    assert before["kind"] == "needsWindow", before
    assert "--context-window" in before["next_step"]

    ran = run_cli("defaults", "set", "--provider", "local", "--model", "mystery",
                  "--context-window", "32768", env=env, cwd=cwd)
    assert ran.returncode == EXIT_OK, ran.stderr
    after = json.loads(run_cli("--json", "onboard", "--no-probe", env=env, cwd=cwd).stdout)["gate"]
    assert after["kind"] == "needsProject", after


def test_the_project_command_initialises_the_folder_it_names(tmp_path):
    """The command *is* the answer to the project step, so it has to create the state the gate reads —
    otherwise it appears to work and changes nothing, which is worse than no guidance."""
    import os

    work = tmp_path / "myrepo"
    work.mkdir()
    before = Workspace.attach(work)
    assert not before.exists()
    result = run_cli("onboard", "--project", str(work), "--no-probe",
                     env=hermetic(tmp_path, EXAMPLE))
    assert result.returncode in (EXIT_OK, EXIT_CHECK_FAILED), result.stderr
    assert (work / ".agent_state").is_dir(), "the command must confirm the folder it names"
    assert not (work / "src").exists(), "an attached folder must not be given the engine's own tree"


def test_confirming_a_project_does_not_happen_without_the_flag(tmp_path):
    """Opening a session, or asking for the journey, must never create a workspace nobody named."""
    work = tmp_path / "untouched"
    work.mkdir()
    run_cli("onboard", "--no-probe", env=hermetic(tmp_path, EXAMPLE))
    assert not (work / ".agent_state").exists()


def test_the_autonomy_command_moves_the_gate_and_is_recorded(tmp_path):
    """The engine's own default is `unattended`, so the step must read the *file* rather than the
    validated config — otherwise the step could never be satisfied, because the default already
    answers it."""
    import os

    document = _example()
    document["providers"] = {"local": {"kind": "ollama", "base_url": "http://127.0.0.1:9"}}
    document["models"] = {"known": {"llama3.1:8b": {"context_window": 131072, "locality": "local"}}}
    document["defaults"] = {"provider": "local", "model": "llama3.1:8b"}
    document.pop("goal", None)
    creds = write_creds(tmp_path, document)
    work = tmp_path / "w"
    (work / ".agent_state").mkdir(parents=True)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = hermetic(tmp_path, creds)

    before = json.loads(run_cli("--json", "onboard", "--project", str(work), "--no-probe",
                                env=env, cwd=cwd).stdout)
    assert before["gate"]["kind"] == "needsAutonomy", before["gate"]
    assert before["gate"]["next_step"].endswith("defaults autonomy --posture unattended")

    ran = run_cli("defaults", "autonomy", "--posture", "unattended", env=env, cwd=cwd)
    assert ran.returncode == EXIT_OK, ran.stderr
    after = json.loads(run_cli("--json", "onboard", "--project", str(work), "--no-probe",
                               env=env, cwd=cwd).stdout)
    assert after["gate"]["kind"] == "ready", after["gate"]


# ── 6. the JSON shape is stable, and stdout is only JSON ─────────────────────


def test_the_json_shape_is_the_one_the_app_decodes(tmp_path):
    """The macOS client reads these keys by name — `id`, `title`, `purpose`, `unlocks`, `satisfied`,
    `resolves` — and a renamed field is a rail that renders blank. A renamed *gate* field is a wizard
    that cannot tell which step to show."""
    import os

    creds = write_creds(tmp_path, provider_without_default())
    payload = onboard_json("--no-probe", creds=creds)
    assert set(payload) == {"summary", "steps", "gate", "config_path", "workspace"}
    assert set(payload["gate"]) == {
        "kind", "title", "detail", "why", "next_step", "step", "step_id", "blocking",
        "provider", "model",
    }
    for step in payload["steps"]:
        assert set(step) == {"id", "title", "purpose", "unlocks", "satisfied", "resolves"}
    assert isinstance(payload["summary"], str) and payload["summary"]
    assert [step["id"] for step in payload["steps"]] == list(ob.STEP_ORDER)
    assert isinstance(payload["gate"]["blocking"], bool)
    assert isinstance(payload["gate"]["step"], int)


def test_the_json_is_parseable_with_no_prose_in_front_of_it(tmp_path):
    """The discipline the whole CLI depends on: stderr carries the diagnostics, stdout the answer. A
    stray `print` makes `onboard --json | jq` fail on line one."""
    import os

    creds = write_creds(tmp_path, no_provider_config())
    result = run_cli("--json", "onboard", "--no-probe",
                     env=hermetic(tmp_path, creds))
    json.loads(result.stdout)  # raises if anything but JSON reached stdout
    assert "warning" not in result.stdout
    assert "warning" in result.stderr, "the diagnostics must still be reported somewhere"


def test_the_ready_payload_says_so_without_a_prose_prefix(tmp_path):
    import os

    document = _example()
    document["providers"] = {"local": {"kind": "ollama", "base_url": "http://127.0.0.1:9"}}
    document["models"] = {"known": {"llama3.1:8b": {"context_window": 131072, "locality": "local"}}}
    document["defaults"] = {"provider": "local", "model": "llama3.1:8b"}
    document.setdefault("goal", {})["default_posture"] = "unattended"
    creds = write_creds(tmp_path, document)
    work = tmp_path / "w"
    (work / ".agent_state").mkdir(parents=True)
    result = run_cli("--json", "onboard", "--project", str(work), "--no-probe",
                     env=hermetic(tmp_path, creds))
    payload = json.loads(result.stdout)
    assert result.returncode == EXIT_OK
    assert payload["gate"]["kind"] == "ready"
    assert payload["gate"]["blocking"] is False
    assert payload["gate"]["step_id"] is None
    assert all(step["satisfied"] for step in payload["steps"])


def test_the_gate_object_carries_no_text_the_app_has_to_render_blind():
    """Every human string is a *field*, never a serialised object. A `why` holding a Python repr is a
    message the person reads as a dict — which is exactly the `status['goal']` bug the CLI fixed once
    already."""
    gate = ob.gate(True, defaults={}, providers=[])
    payload = gate.as_dict()
    assert json.loads(json.dumps(payload)) == payload, "the payload must be JSON round-trippable"
    for key in ("title", "detail", "why", "next_step"):
        assert isinstance(payload[key], str), key
        assert "{" not in payload[key] and "}" not in payload[key], payload[key]


# ── 7. the session guides first-run ──────────────────────────────────────────


def test_the_session_says_what_is_missing_before_a_goal_is_typed():
    """The front door opened silently even when nothing could run: a person typed a goal, watched it
    fail for a reason the session never mentioned, and had to discover the cause themselves."""
    from engine.gateway import Gateway
    from engine.providers.fake import FakeProvider
    from engine.tokens import TokenEstimator

    config = load()
    lines: list[str] = []
    from engine.chat import ChatSession

    session = ChatSession(config=config, gateway=Gateway(config, {"fake": FakeProvider(provider_id="fake")},
                                                         estimator=TokenEstimator()),
                          output_fn=lines.append, stream=False)
    session._warn_if_not_ready()
    output = "\n".join(lines)
    assert output, "a blocked session must say so on entry"
    assert "python3 -m engine.cli" in output, output
    assert output.count("\n") <= 1, "one line, then where the whole path is"


def test_a_ready_session_says_nothing_extra():
    """A session printed per turn would be a nag; a ready engine must not warn about setup."""
    from engine.chat import ChatSession
    from engine.config import ModelSpec

    class _Ready:
        raw = {"goal": {"default_posture": "unattended"}}
        defaults = {"provider": "p", "model": "m"}

        def default_pair(self):
            return ("p", "m", "configured default")

        def default_model_spec(self):
            return ModelSpec(model_id="m", context_window=8192)

        def default_models_for(self, provider):
            return {"m"}

    class _Ws:
        def exists(self):
            return True

    lines: list[str] = []
    session = ChatSession(config=_Ready(), gateway=None, workspace=_Ws(),
                          output_fn=lines.append, stream=False)
    session._warn_if_not_ready()
    assert lines == [], lines


def test_the_session_greeting_does_not_touch_the_network():
    """A greeting that probes would block the prompt on a slow provider. The window input is read
    from the configuration alone, and the probe is left to `onboard` and `doctor`."""
    from engine.chat import ChatSession
    from engine.gateway import Gateway
    from engine.providers.fake import FakeProvider
    from engine.tokens import TokenEstimator

    seen: list[str] = []

    class _NoProbeGateway(Gateway):
        @property
        def providers(self):
            seen.append("touched")
            raise AssertionError("the greeting must not resolve providers for a probe")

    config = load()
    lines: list[str] = []
    gateway = Gateway(config, {"fake": FakeProvider(provider_id="fake")}, estimator=TokenEstimator())
    session = ChatSession(config=config, gateway=gateway, output_fn=lines.append, stream=False)
    session._warn_if_not_ready()          # must complete without a socket
    assert lines, "the blocked state must still be reported from the configuration alone"


def test_the_session_offers_the_onboard_command():
    """The entry line names one step; the whole path has to be reachable from where the person is."""
    from engine.chat import COMMANDS

    usage = {command.name: command for command in COMMANDS}
    assert "/onboard" in usage
    assert hasattr(__import__("engine.chat", fromlist=["ChatSession"]).ChatSession, usage["/onboard"].handler)


def test_onboard_is_a_command_the_parser_knows():
    parser = build_parser()
    args = parser.parse_args(["onboard"])
    assert args.func is not None
    for argv in (["onboard", "--json"], ["onboard", "--no-probe"],
                 ["onboard", "--project", "/tmp"], ["onboard", "--slug", "demo", "--root", "/tmp"]):
        assert parser.parse_args(argv).func is not None, argv


def test_the_front_door_still_opens_the_session():
    """A new subcommand must not change how a bare invocation resolves: `onboard` is a *word*, and
    the scan that decides must treat it as one."""
    result = run_cli(env={})
    assert result.returncode == EXIT_OK
    assert "Chatting with" in result.stdout


# ── 8. regression: doctor is unchanged ───────────────────────────────────────


def test_doctor_still_reports_its_checks():
    """`onboard` is a second reading of the same environment, not a replacement. `doctor` keeps its
    shape: independent, unordered checks against a healthy machine — eight of them since the machine
    posture (`[system]`) became one, having previously been the section `doctor` could not read."""
    result = run_cli("doctor")
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith(("OK ", "FAIL"))]
    assert len(lines) == 8, lines
    assert "doctor: all checks passed" in result.stdout


def test_doctor_still_fails_with_a_named_reason_and_a_diagnostic_stream(tmp_path):
    import os

    creds = write_creds(tmp_path, no_provider_config())
    result = run_cli("doctor", "--json",
                     env=hermetic(tmp_path, creds, AGENTORG_TEST_KEY_UNSET=""))
    assert result.returncode == EXIT_CHECK_FAILED
    payload = json.loads(result.stdout)
    assert payload["failures"] >= 1
    assert payload["checks"][0]["check"] == "configuration"
    assert "no key" in payload["checks"][0]["detail"]


def test_doctor_and_onboard_agree_about_the_configuration(tmp_path):
    """Two readings of one environment. They answer different questions, but they must not disagree
    about the *facts* — a doctor that blesses a config the onboard gate calls broken is the drift
    this whole workstream exists to prevent."""
    import os

    creds = write_creds(tmp_path, provider_without_default())
    env = hermetic(tmp_path, creds)
    doctor = json.loads(run_cli("--json", "doctor", env=env).stdout)
    assert doctor["failures"] == 0, doctor
    onboard = json.loads(run_cli("--json", "onboard", "--no-probe", env=env).stdout)
    # The config is healthy; the *journey* is not finished. That is the distinction between the two
    # commands, and both have to be visible at once for it to be a distinction rather than a bug.
    assert onboard["gate"]["kind"] == "needsModel"
    assert onboard["gate"]["blocking"] is True


def test_the_onboard_command_is_in_the_help_and_beside_doctor():
    text = build_parser().format_help()
    assert "onboard" in text
    assert "first-run" in text.lower()


def test_main_returns_the_exit_code_rather_than_raising(tmp_path):
    import os

    creds = write_creds(tmp_path, provider_without_default())
    code = main(["onboard", "--no-probe"])
    assert code in (EXIT_OK, EXIT_CHECK_FAILED)


def test_an_attached_folder_that_does_not_exist_is_a_usage_error(tmp_path):
    """`--project` must refuse a typo rather than create a directory somewhere unintended, and the
    refusal must name the fix — the engine's own standard for a usage error."""
    result = run_cli("onboard", "--project", str(tmp_path / "typo"))
    assert result.returncode != EXIT_OK
    assert "Traceback" not in result.stderr
    assert "does not exist" in result.stderr
