#!/usr/bin/env python3
"""Phase 47 — the autonomy step a person answers must actually move the engine's journey.

WHY THIS EXISTS
---------------
The first-run wizard asks four questions and the last one is "how much does it decide alone?". The app
answered it locally: `OrgController.goalPosturePreference` wrote the choice to `UserDefaults`, the
window's own gate went satisfied, and `showsFirstRunWizard` went false. Nothing ever told the engine.

So the app and the engine disagreed about the *same question*, permanently, and the disagreement was
invisible from inside the window:

- `onboard` kept reporting `3 of 4 step(s) done — next: Choose how much it decides alone.`
- Re-running setup asked the question again, with the answer already stored and nothing on screen
  saying why it came back.
- `onboarding.posture_recorded` — the single rule the journey step is decided by — reads the
  *document*, and `goal.default_posture` was never in it.

That is the "still confusing about onboarding" report in its purest form: a person answers a question,
and the software asks it again. The window cannot see this, because the window is not the surface that
remembers; the engine is.

These tests pin the fix at the layer that owns the fact:

1. `autonomy_set` accepts a `posture` key and writes `goal.default_posture`, so the command the app
   already sends can carry the answer.
2. An invalid posture is *refused*, not written — a bad value in the file makes the whole
   configuration unloadable on the next read, which would turn a wizard answer into a dead engine.
3. The write is what `onboard` reads: after it, the journey's autonomy step is satisfied and the
   summary changes. This is asserted by *re-running the command*, not by reading the field back —
   the point is the loop, not the write.
4. `status` carries the posture and whether the file records one, because those are two different
   facts and the app draws a step on the first and a question on the second.
"""

from __future__ import annotations

import io
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import onboarding as ob
from engine.config import GoalConfig, load
from engine.library import resolve
from engine.state import Workspace

EXAMPLE = ROOT / "credentials.example.json"

# ── the state every test starts from ─────────────────────────────────────────
#
# A config whose model and project questions are already answered and whose autonomy one is not —
# which is exactly where the app's wizard hands this step over. The posture is deliberately *absent*
# from the document rather than set to the engine's default, because "the engine has a default" and
# "the person answered" are the two facts the whole step turns on.


def _example() -> dict:
    return json.loads(EXAMPLE.read_text())


def unanswered_config() -> dict:
    """Model and project resolved, posture not recorded — the app's autonomy step, as the engine sees it."""
    document = _example()
    document["providers"] = {"local": {"kind": "ollama", "base_url": "http://127.0.0.1:9",
                                       "timeout_s": 2, "max_retries": 0}}
    document["models"] = {"known": {"llama3.1:8b": {"context_window": 131072, "locality": "local"}}}
    document["defaults"] = {"provider": "local", "model": "llama3.1:8b"}
    document["goal"] = {}
    return document


def write_creds(tmp_path: pathlib.Path, document: dict) -> pathlib.Path:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(document))
    return path


def make_server(creds: pathlib.Path, slug: str = "autonomy") -> object:
    """A real `Server` over a real workspace, so the command under test is the one the app calls."""
    from engine.serve import Server

    workspace = Workspace.for_project(slug)
    workspace.ensure()
    return Server(config=load(str(creds), warn=False), library=resolve(), workspace=workspace,
                  slug=slug, stdin=io.StringIO(""), stdout=io.StringIO())


# ── 1. the command carries the answer ────────────────────────────────────────


def test_autonomy_set_accepts_a_posture_and_writes_it_to_the_document(tmp_path):
    """The key the app depends on. Without it the wizard's last answer has nowhere to go, which is the
    bug: a step a person has answered that the engine still calls unanswered."""
    creds = write_creds(tmp_path, unanswered_config())
    server = make_server(creds)
    server._cmd_autonomy_set({"posture": "supervised"})
    assert json.loads(creds.read_text())["goal"]["default_posture"] == "supervised"


def test_the_posture_write_keeps_the_other_autonomy_switches(tmp_path):
    """A merge, not a replace. `set_autonomy` is documented as merging the named keys, and a posture
    write that reset the gate flags would change an authority the person set deliberately."""
    document = unanswered_config()
    document["goal"] = {"auto_pass_auto_gates": False, "auto_hire_missing": False}
    creds = write_creds(tmp_path, document)
    make_server(creds)._cmd_autonomy_set({"posture": "unattended"})
    goal = json.loads(creds.read_text())["goal"]
    assert goal["default_posture"] == "unattended"
    assert goal["auto_pass_auto_gates"] is False, "the posture write clobbered a gate setting"
    assert goal["auto_hire_missing"] is False


def test_a_posture_the_engine_does_not_know_is_refused_not_written(tmp_path):
    """A bad value must be refused at the write. `GoalConfig` validates `default_posture` on load, so a
    junk posture written here would make the whole configuration unreadable on the next command — a
    wizard answer that bricks the engine is a far worse outcome than a refused switch."""
    from engine.serve import ServerError

    creds = write_creds(tmp_path, unanswered_config())
    server = make_server(creds)
    try:
        server._cmd_autonomy_set({"posture": "cowboy"})
    except ServerError as exc:
        assert "cowboy" in str(exc)
    else:
        raise AssertionError("an unknown posture must be refused")
    # And the file is untouched, so the refusal cannot half-apply.
    assert "default_posture" not in json.loads(creds.read_text())["goal"]


def test_the_engine_still_refuses_a_payload_that_changes_nothing(tmp_path):
    """The pre-existing contract: an empty autonomy_set is an error rather than a no-op, so a caller
    that sent nothing learns it rather than believing it changed something."""
    from engine.serve import ServerError

    creds = write_creds(tmp_path, unanswered_config())
    try:
        make_server(creds)._cmd_autonomy_set({})
    except ServerError as exc:
        assert "at least one setting" in str(exc)
    else:
        raise AssertionError("an empty autonomy_set must be refused")


# ── 2. the engine's own journey moves ────────────────────────────────────────


def ready_workspace() -> object:
    """A project the gate will accept, so the autonomy step is the one under test.

    Without this the gate stops at the *project* step and every assertion below would be about the
    wrong question — the ordering is by dependency, so a step is only reachable when the ones before it
    are answered.
    """
    workspace = Workspace.for_project("autonomy-journey")
    workspace.ensure()
    return workspace


def journey_for(creds: pathlib.Path) -> dict:
    """The journey as `onboard --json` and the app's `status` both obtain it — through `inspect`."""
    from engine.onboarding import inspect, journey_payload

    report = inspect(config_path=str(creds), workspace=ready_workspace(), probe=False)
    return journey_payload(report.gate)


def test_the_autonomy_step_is_open_before_the_answer_and_shut_after(tmp_path):
    """**The test this file exists for.** Driven by running the journey twice, not by reading the field
    back: what was wrong was the loop, and a test that asserted the write would have passed while the
    window still showed the step as outstanding."""
    from engine.config import set_autonomy

    creds = write_creds(tmp_path, unanswered_config())
    before = journey_for(creds)
    assert next(s for s in before["steps"] if s["id"] == "autonomy")["satisfied"] is False
    assert "Choose how much it decides alone" in before["summary"], before["summary"]

    set_autonomy(creds, goal={"default_posture": "unattended"})

    after = journey_for(creds)
    assert next(s for s in after["steps"] if s["id"] == "autonomy")["satisfied"] is True
    assert "All 4 steps are done" in after["summary"], after["summary"]


def test_the_servers_own_command_advances_the_journey_too(tmp_path):
    """The path the app actually takes, end to end: the command, then the journey the next `status`
    poll would carry. The command and the journey must agree without a restart, or the window shows a
    step as done while the engine it is polling says otherwise."""
    creds = write_creds(tmp_path, unanswered_config())
    server = make_server(creds)
    assert next(s for s in server._journey()["steps"]
                if s["id"] == "autonomy")["satisfied"] is False

    server._cmd_autonomy_set({"posture": "unattended"})

    # `_cmd_autonomy_set` reloads the live config, so the very next journey is computed from the
    # written file rather than from the copy the process started with.
    journey = server._journey()
    assert next(s for s in journey["steps"] if s["id"] == "autonomy")["satisfied"] is True
    assert journey["summary"] == "All 4 steps are done — a run is possible."


def test_the_journey_rule_reads_the_document_not_the_default(tmp_path):
    """Why the write is the only thing that can satisfy the step. `GoalConfig.default_posture` is
    `"unattended"` whether or not anyone chose it, so a rule that read the *validated config* would
    report the step done on a fresh machine and the wizard would show three steps while showing four.
    `posture_recorded` is that rule, and this asserts it directly."""
    creds = write_creds(tmp_path, unanswered_config())
    config = load(str(creds), warn=False)
    assert config.goal.default_posture == GoalConfig().default_posture, "the engine's own default"
    assert ob.posture_recorded(config) is False, "a default is not an answer"


# ── 3. the status payload tells the app both facts ───────────────────────────


def test_status_carries_the_posture_and_whether_it_was_chosen(tmp_path):
    """Two fields, because they are two facts. The app needs `posture_recorded` to know whether the
    step is answerable-done and `posture` to show what the org currently does; collapsing them would
    make a fresh machine look answered."""
    creds = write_creds(tmp_path, unanswered_config())
    server = make_server(creds)
    autonomy = server._cmd_status({})["defaults"]["autonomy"]
    assert autonomy["posture"] == "unattended", "the effective default, which is real"
    assert autonomy["posture_recorded"] is False, "but nobody has answered it"

    server._cmd_autonomy_set({"posture": "supervised"})
    autonomy = server._cmd_status({})["defaults"]["autonomy"]
    assert autonomy["posture"] == "supervised"
    assert autonomy["posture_recorded"] is True


def test_the_status_autonomy_keys_are_unchanged_plus_the_two_new_ones(tmp_path):
    """Additive only. The console reads `auto_pass_auto_gates` and the rest by name, and this change
    must not be a reason for any of them to go missing."""
    creds = write_creds(tmp_path, unanswered_config())
    autonomy = make_server(creds)._cmd_status({})["defaults"]["autonomy"]
    for existing in ("auto_pass_auto_gates", "auto_hire_missing", "persist_auto_hires",
                     "auto_hire_max_tier", "token_budget"):
        assert existing in autonomy, f"{existing} disappeared from the defaults payload"


def test_an_unanswered_posture_still_resolves_to_the_engines_default(tmp_path):
    """The behaviour half: not answering must not leave the engine without a posture. A new goal on a
    fresh machine gets `unattended` — the documented polarity — while the journey still shows the step,
    which is the distinction the two fields exist to keep."""
    creds = write_creds(tmp_path, unanswered_config())
    config = load(str(creds), warn=False)
    assert config.goal.default_posture == GoalConfig().default_posture
    # The provider list is what the gate reads for "is any endpoint configured"; an empty one stops the
    # gate at the *model* step and the assertion below would be about the wrong question.
    from engine.providers.registry import build_providers

    built, _ = build_providers(config)
    assert ob.readiness(config, built, ready_workspace(),
                        probe=False).kind is ob.GateKind.NEEDS_AUTONOMY
