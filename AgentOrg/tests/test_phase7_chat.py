#!/usr/bin/env python3
"""Phase 7 tests — the conversational front door.

The chat loop is the surface a person actually touches, so the parts worth testing are the ones that
would silently lie to them: a cost rendered as free when it was unmeasured, a command that looks
recognised but does nothing, a turn kept in the transcript that was never answered.

Everything here runs against the in-process fake provider, so the suite stays offline.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.chat import (COMMANDS, INTENT_CHAT, INTENT_UNSURE, INTENT_WORK, ChatSession,
                         RunProgress, _slug, classify_intent)
from engine.config import load
from engine.gateway import Gateway
from engine.org import default_company
from engine.providers.base import ChatResponse, Usage
from engine.providers.fake import FakeProvider
from engine.state import Workspace
from engine.tokens import TokenEstimator


class ScriptedProvider(FakeProvider):
    """A fake provider that returns a canned reply and reports usage.

    Usage is deliberately *reported*, so the cost footer exercises the measured path rather than the
    unknown one; a separate test forces the unknown path.
    """

    def __init__(self, reply: str = "a reply", *, report_usage: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.reply = reply
        self.report_usage = report_usage
        self.seen: list = []

    def complete(self, request):
        self.seen.append(request)
        usage = Usage(prompt_tokens=11, completion_tokens=22, reported_cost_usd=0.0) \
            if self.report_usage else Usage()
        return ChatResponse(text=self.reply, usage=usage, model=request.model,
                            provider_id=self.provider_id)

    def stream(self, request):
        from engine.providers.base import Chunk, FinishReason

        self.seen.append(request)
        yield Chunk(text=self.reply)
        usage = Usage(prompt_tokens=11, completion_tokens=22, reported_cost_usd=0.0) \
            if self.report_usage else Usage()
        yield Chunk(usage=usage, finish_reason=FinishReason.STOP)


@pytest.fixture(scope="module")
def config():
    return load()


@pytest.fixture(scope="module")
def library():
    """The pinned library handle — what `serve.Server` takes, so `/setup` needs it."""
    from engine.library import resolve

    return resolve()


#: The `run` fixture the front-door regression guard drives. A minimal graph whose only node parks the
#: human gate, so a `--dry-run` can be asserted without a provider and without executing anything.
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


@pytest.fixture
def run_project(tmp_path):
    root = tmp_path / "projects"
    project = root / "clirun"
    project.mkdir(parents=True)
    (project / "clirun.yaml").write_text(_RUN_MANIFEST)
    return root, project


def make_session(config, *, reply="a reply", report_usage=True, org=None, **kwargs):
    """A session wired to a scripted provider and captured output."""
    provider = ScriptedProvider(reply=reply, report_usage=report_usage)
    gateway = Gateway(config, {"fake": provider}, estimator=TokenEstimator())
    lines: list[str] = []
    session = ChatSession(config=config, gateway=gateway, org=org,
                          output_fn=lines.append, stream=False, **kwargs)
    session.provider = "fake"
    session.model = "fake-model"
    return session, lines, provider


# ── the command surface ──────────────────────────────────────────────────────


def test_every_command_has_a_handler_that_exists():
    """A command in the table with no method would look recognised and do nothing."""
    for command in COMMANDS:
        assert command.handler, f"{command.name} has no handler"
        assert hasattr(ChatSession, command.handler), \
            f"{command.name} names {command.handler}, which ChatSession does not define"


def test_every_command_is_documented():
    for command in COMMANDS:
        assert command.usage.startswith(command.name), f"{command.name} usage does not name it"
        assert command.help.strip(), f"{command.name} has no help text"


def test_help_lists_every_command(config):
    session, lines, _ = make_session(config)
    session._dispatch("/help")
    output = "\n".join(lines)
    for command in COMMANDS:
        assert command.name in output, f"/help omits {command.name}"


def test_an_unknown_command_says_so_rather_than_being_ignored(config):
    session, lines, _ = make_session(config)
    session._dispatch("/teleport")
    assert "unknown command" in "\n".join(lines)


def test_a_command_name_is_case_insensitive(config):
    session, lines, _ = make_session(config)
    session._dispatch("/HELP")
    assert "Commands:" in "\n".join(lines)


# ── direct chat ──────────────────────────────────────────────────────────────


def test_a_turn_prints_the_reply_and_a_usage_footer(config):
    session, lines, _ = make_session(config, reply="hello there")
    session._say("hi")
    output = "\n".join(lines)
    assert "hello there" in output
    assert "22" in output, "the completion token count must be shown"
    assert "fake/fake-model" in output


def test_the_transcript_keeps_both_sides_of_a_turn(config):
    session, _, _ = make_session(config, reply="answer")
    session._say("question")
    roles = [m.role.value for m in session.transcript]
    assert roles == ["user", "assistant"], roles


def test_an_unmeasured_turn_is_never_rendered_as_free(config):
    """The distinction the whole cost layer exists for, at the point a person reads it.

    Tested on the formatter directly: the point is that a `None` cost must render as `unknown`, and
    forcing a provider's locality to make that happen would be testing the locality table instead.
    """
    session, _, _ = make_session(config)
    line = session._usage_line({"tokens_in": None, "tokens_out": None,
                                "cost_usd": None, "source": "unknown"})
    assert "unknown" in line
    assert "$0.0000" not in line

    measured = session._usage_line({"tokens_in": 5, "tokens_out": 6,
                                    "cost_usd": 0.0, "source": "measured"})
    assert "$0.0000" in measured and "unknown" not in measured


def test_a_provider_error_does_not_leave_an_unanswered_turn(config):
    """A kept turn with no reply would be resent, and the model would see a question with no answer."""
    session, lines, provider = make_session(config)

    def boom(request):
        raise RuntimeError("provider exploded")

    provider.complete = boom
    session._say("hi")
    assert session.transcript == []
    assert "provider error" in "\n".join(lines)


def test_a_provider_error_does_not_end_the_session(config):
    session, lines, provider = make_session(config)
    provider.complete = lambda request: (_ for _ in ()).throw(RuntimeError("nope"))
    session._say("hi")
    # The loop is still usable: the next turn goes through.
    provider.complete = ScriptedProvider(reply="recovered").complete
    assert session._say("again") == "recovered"


def test_the_transcript_is_trimmed_so_it_cannot_grow_without_bound(config):
    from engine.providers.base import Message, Role

    session, _, _ = make_session(config)
    for i in range(80):
        session.transcript.append(Message.text_message(Role.USER, f"t{i}"))
    session._trim_transcript()
    assert len(session.transcript) <= 48, len(session.transcript)


# ── the roster and the model switch ──────────────────────────────────────────


def test_agents_command_lists_the_roster(config):
    org = default_company(provider="fake", model="fake-model", context_window=32768)
    session, lines, _ = make_session(config, org=org)
    session._cmd_agent("")
    output = "\n".join(lines)
    assert "Priya" in output and "Alice" in output


def test_switching_agent_switches_the_model_with_it(config):
    """Otherwise `/agent Sana` would change the label and not the model, which is a lie."""
    org = default_company(provider="fake", model="fake-model", context_window=32768,
                          reviewer_provider="other", reviewer_model="reviewer-model",
                          reviewer_context_window=32768)
    session, lines, _ = make_session(config, org=org)
    session._cmd_agent("Sana")
    assert session.provider == "other" and session.model == "reviewer-model"


def test_an_unknown_agent_is_refused(config):
    org = default_company(provider="fake", model="fake-model", context_window=32768)
    session, lines, _ = make_session(config, org=org)
    session._cmd_agent("Nobody")
    assert "no agent named" in "\n".join(lines)


def test_the_system_prompt_carries_the_agent_persona(config):
    org = default_company(provider="fake", model="fake-model", context_window=32768)
    session, _, _ = make_session(config, org=org)
    session._cmd_agent("Alice")
    prompt = session._system_prompt()
    assert "Alice" in prompt and "Backend Developer" in prompt


def test_org_commands_say_so_when_no_roster_is_loaded(config):
    """Without a roster the commands must explain, not silently no-op."""
    session, lines, _ = make_session(config)
    for name in ("/agents", "/run build a thing", "/status", "/approve", "/instruct hurry"):
        lines.clear()
        session._dispatch(name)
        assert lines, f"{name} produced no output at all"


# ── gates without leaving the chat ───────────────────────────────────────────


def test_decide_without_a_run_is_explained(config):
    session, lines, _ = make_session(config)
    session._cmd_approve("")
    assert "no run is loaded" in "\n".join(lines)


def test_slug_derivation_matches_the_planner_rule():
    assert _slug("Build a Booking API!") == "build-a-booking-api"
    assert _slug("") == "chat-run"


# ── the front door: a bare invocation opens the session ──────────────────────
#
# The regression these guard is the whole point of the workstream: `python3 -m engine.cli` used to be
# a hard usage error, and the fix must not have changed a single existing subcommand. Each test below
# runs the *real* entry point in a subprocess, because the failure mode being guarded is argparse's
# own behaviour — which an in-process `build_parser()` call would not reproduce.


def _cli(*args: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Run the CLI as a subprocess, so the real entry point and argv handling are exercised."""
    return subprocess.run([sys.executable, "-m", "engine.cli", *args],
                          capture_output=True, text=True, cwd=str(ROOT), input=stdin)


def test_a_bare_invocation_enters_the_session_rather_than_erroring():
    """It used to print a usage error and exit 2; a person who typed the program's name wants the
    program, not a list of 25 commands. EOF is fed in so the loop leaves immediately."""
    result = _cli()
    assert result.returncode == 0, result.stderr[:400]
    assert "usage:" not in result.stderr, "a bare invocation must not be a usage error"
    assert "Chatting with" in result.stdout
    assert "/help" in result.stdout


def test_a_bare_invocation_with_a_global_flag_also_opens_the_session():
    """The flag comes before the subcommand, so argv has no bare word before it at all."""
    result = _cli("--no-org")
    assert result.returncode == 0, result.stderr[:400]
    assert "Chatting with" in result.stdout


def test_a_bare_invocation_does_not_swallow_a_mistyped_command():
    """The front door must not turn a typo into an interactive session: `teleport` has to fail the
    way it always did, or a script with a misspelled command would hang waiting for input."""
    result = _cli("teleport")
    assert result.returncode == 2
    assert "unrecognized arguments: teleport" in result.stderr


def test_an_unknown_config_path_still_fails_as_a_usage_error(tmp_path):
    """`--config` takes a *value*, and that value is a bare word. Treating it as the subcommand would
    open a session against the wrong configuration, so the scan must step over it."""
    missing = tmp_path / "absent.json"
    result = _cli("--config", str(missing), "doctor")
    assert result.returncode == 1
    assert "no configuration found" in result.stderr
    assert "Chatting with" not in result.stdout


# ── the regression guard: every existing subcommand is unchanged ─────────────
#
# Named explicitly because these are the four the brief calls out: a front door that quietly changed
# one of them would break scripts, the suite and the macOS app without any of them failing loudly.


def test_doctor_is_unchanged_by_the_front_door():
    result = _cli("doctor")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "doctor: all checks passed" in result.stdout
    lines = [line for line in result.stdout.splitlines() if line.startswith(("OK ", "FAIL"))]
    # Eight since the machine posture became a check of its own; this test is about the front door
    # leaving `doctor` alone, so it counts the checks rather than naming them.
    assert len(lines) == 8, "doctor must still report every check it has"


def test_status_is_unchanged_by_the_front_door(tmp_path):
    """It still refuses an unknown run with a diagnostic on stderr — not by opening a session."""
    result = _cli("status", "--slug", "nothing-here", "--root", str(tmp_path))
    assert result.returncode == 1
    assert "no run found" in result.stderr
    assert "Chatting with" not in result.stdout


def test_decide_is_unchanged_by_the_front_door(tmp_path):
    result = _cli("decide", "--slug", "nothing-here", "--root", str(tmp_path), "--approve")
    assert result.returncode == 1
    assert "Chatting with" not in result.stdout


def test_run_dry_run_is_unchanged_by_the_front_door(run_project):
    """`run` is the command the whole engine is driven by, so it is exercised end to end: a dry run
    plans, prints the phase, and executes nothing."""
    root, project = run_project
    result = _cli("run", "--manifest", str(project / "clirun.yaml"), "--slug", "clirun",
                  "--root", str(root), "--dry-run")
    assert result.returncode == 0, result.stderr[:400]
    assert "dry run" in result.stdout
    assert "awaiting_approval" in result.stdout
    assert "Chatting with" not in result.stdout


def test_the_parser_requires_a_command_in_process():
    """The front door is in `main`, not in the parser. A caller using `build_parser()` directly —
    which the whole suite does — must still be told that a command is required."""
    from engine.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# ── plain text routes to work, not only to chat ──────────────────────────────


@pytest.mark.parametrize("text,expected", [
    # A job about something openable is work.
    ("add cursor pagination to /v1/items", INTENT_WORK),
    ("rename User.swift", INTENT_WORK),
    ("fix the crashing bug in the parser", INTENT_WORK),
    ("please add a test for the cli parser", INTENT_WORK),
    ("implement /v2/orders", INTENT_WORK),
    # A question is a question, even when it opens with a work verb.
    ("how do I add pagination?", INTENT_CHAT),
    ("can you add cursor pagination to /v1/items?", INTENT_CHAT),
    ("what does the gate do", INTENT_CHAT),
    ("hello", INTENT_CHAT),
    ("explain the review loop", INTENT_CHAT),
    # Neither: an imperative about nothing openable, or not an imperative at all.
    ("refactor the report", INTENT_UNSURE),
    ("that looks wrong", INTENT_UNSURE),
    ("deploy", INTENT_UNSURE),
    ("", INTENT_UNSURE),
])
def test_the_routing_rule_is_stated_and_checkable(text, expected):
    assert classify_intent(text) == expected, f"{text!r} classified wrongly"


def test_work_text_offers_the_run_rather_than_running_it(config):
    """The offer must not spend: the planner is only reached after a "yes", so declining is free."""
    session, lines, _ = make_session(config)
    calls: list = []
    session.orchestrator = _RecordingOrchestrator(calls)
    session._input = lambda prompt: "n"
    session._plain("add cursor pagination to /v1/items")
    assert calls == [], "declining must reach neither the planner nor an execution"
    assert "reads like a job" in "\n".join(lines)
    assert "dropped" in "\n".join(lines)


def test_work_text_runs_it_when_answered_yes(config):
    """Routed through `/run`'s own implementation, so the offer and the command cannot drift."""
    session, lines, _ = make_session(config)
    calls: list = []
    session.orchestrator = _RecordingOrchestrator(calls)
    session._input = lambda prompt: "y"
    session._plain("add cursor pagination to /v1/items")
    assert calls == ["prepare"], calls


def test_work_text_can_be_talked_through_instead(config):
    """`c` answers it as chat — the escape hatch for a sentence that reads like a job."""
    session, lines, provider = make_session(config, reply="pagination answer")
    session.orchestrator = _RecordingOrchestrator([])
    session._input = lambda prompt: "c"
    session._plain("add cursor pagination to /v1/items")
    assert "pagination answer" in "\n".join(lines)


def test_an_ambiguous_line_asks_rather_than_guessing(config):
    """Guessing costs either real money on an unwanted run or a job answered as a question."""
    session, lines, _ = make_session(config)
    session._plain("refactor the report")
    output = "\n".join(lines)
    assert "Not sure whether" in output
    assert "/run <goal>" in output and "/chat <text>" in output


def test_a_question_still_goes_to_the_model(config):
    session, lines, _ = make_session(config, reply="here is how")
    session._plain("how do I add pagination?")
    assert "here is how" in "\n".join(lines)


def test_slash_chat_forces_a_reply_for_work_shaped_text(config):
    """The escape hatch stated as a command, so a job-shaped question needs no rewording."""
    session, lines, _ = make_session(config, reply="forced reply")
    session._cmd_chat("add cursor pagination to /v1/items")
    assert "forced reply" in "\n".join(lines)


def test_work_text_with_no_orchestrator_says_so_rather_than_guessing(config):
    session, lines, _ = make_session(config)
    session._plain("add cursor pagination to /v1/items")
    output = "\n".join(lines)
    assert "No orchestrator is loaded" in output
    assert "/chat" in output


class _RecordingOrchestrator:
    """Records which orchestrator calls were reached, and refuses planning so nothing executes."""

    bus = None

    def __init__(self, calls: list) -> None:
        self.calls = calls

    def prepare(self, goal: str, slug: str | None = None) -> object:
        self.calls.append("prepare")
        raise RuntimeError("planning is refused in this test")


# ── /run streams progress to stderr, and stdout stays the answer ─────────────


class _Bus:
    """A minimal bus the narration can subscribe to, for asserting the live half of the display."""

    def __init__(self, run_id: str = "bus_1") -> None:
        self.run_id = run_id
        self.subscribers: list = []

    def subscribe(self, callback):
        self.subscribers.append(callback)
        return callback

    def unsubscribe(self, callback) -> None:
        self.subscribers = [s for s in self.subscribers if s is not callback]

    def emit(self, kind: str, *, payload: dict | None = None, **kwargs) -> None:
        from engine.protocol import Event

        event = Event(seq=1, type=kind, payload=payload or {}, run_id=self.run_id,
                      node_id=kwargs.get("node_id"))
        for callback in list(self.subscribers):
            callback(event)


class _OrchWithBus:
    """An orchestrator that owns a bus and a workspace, which is all the narration touches."""

    def __init__(self, bus, workspace) -> None:
        self.bus = bus
        self.workspace = workspace


def _workspace(tmp_path, slug="progressprobe"):
    ws = Workspace.for_project(slug, root=tmp_path / "projects")
    ws.ensure()
    return ws


def test_a_streamed_run_reports_node_events_to_stderr(tmp_path):
    """The gap being closed: `/run` blocked with zero output, and waiting silently reads as a hang.

    The bus is driven directly rather than by executing a graph, because what is under test is the
    *narration* — that a node entry, a handoff crossing and a gate each produce a line. Executing a
    graph here would test the orchestrator, which its own phase already does.

    The process's own stderr is redirected and read, so "printed to stderr" is checked rather than
    assumed from the object's bookkeeping.
    """
    import contextlib
    import io

    ws = _workspace(tmp_path)
    bus = _Bus()
    progress = RunProgress(_OrchWithBus(bus, ws))
    captured = io.StringIO()
    with contextlib.redirect_stderr(captured), progress:
        bus.emit("node.enter", node_id="dev", payload={"phase": "BUILD"})
        bus.emit("handoff.accepted", node_id="dev",
                 payload={"from_node": "dev", "to_node": "review", "summary": "crossed"})
        bus.emit("human.gate", node_id="release", payload={"gate_id": "release", "kind": "human",
                                                           "reason": "Owner approval"})
    written = captured.getvalue()
    assert "→ node" in written and "dev" in written
    assert "handoff accepted" in written and "crossed" in written
    assert "GATE" in written and "Owner approval" in written


def test_progress_goes_to_stderr_and_leaves_stdout_clean(tmp_path):
    """stdout is the answer — the rule `cli._warn` states and `--json` depends on."""
    import contextlib
    import io

    ws = _workspace(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        with RunProgress(_OrchWithBus(_Bus(), ws)) as progress:
            progress.orchestrator.bus.emit("node.enter", node_id="dev", payload={})
    assert out.getvalue() == "", "progress must never appear on stdout"
    assert "node" in err.getvalue()


def test_the_live_half_is_not_narrated_twice_from_the_trace(tmp_path):
    """Both processes append to one trace. Without filtering, every lifecycle event would be shown
    twice — once live from the subscription and once read back from the file."""
    ws = _workspace(tmp_path)
    bus = _Bus(run_id="own_bus")
    ws.trace_path.write_text(json.dumps(
        {"seq": 1, "type": "run.start", "run_id": "own_bus", "payload": {}}) + "\n")
    progress = RunProgress(_OrchWithBus(bus, ws), quiet=True)
    with progress:
        progress._drain_trace()
    assert progress.lines == [], "an event this process emitted must not be re-narrated"


def test_the_trace_half_narrates_what_the_subprocess_emitted(tmp_path):
    """Node transitions happen in the generated executor subprocess, whose bus we do not hold — the
    trace is the only way they can reach the display at all.

    The line is appended *after* the context is entered, which is the real sequence: the follower
    starts at the current end of the trace, so an earlier run's history is not replayed as this run's
    progress.
    """
    ws = _workspace(tmp_path)
    progress = RunProgress(_OrchWithBus(_Bus(run_id="own_bus"), ws), quiet=True)
    with progress:
        ws.trace_path.write_text(json.dumps(
            {"seq": 1, "type": "node.enter", "run_id": "run_child", "node_id": "dev",
             "payload": {"phase": "BUILD"}}) + "\n")
        progress._drain_trace()
    assert any("BUILD" in line and "dev" in line for line in progress.lines), progress.lines


def test_an_earlier_runs_trace_is_not_replayed_as_this_runs_progress(tmp_path):
    """The follower starts at the current end: yesterday's nodes are history, not progress."""
    ws = _workspace(tmp_path)
    ws.trace_path.write_text(json.dumps(
        {"seq": 1, "type": "node.enter", "run_id": "run_old", "node_id": "ancient",
         "payload": {}}) + "\n")
    progress = RunProgress(_OrchWithBus(_Bus(run_id="own_bus"), ws), quiet=True)
    with progress:
        progress._drain_trace()
    assert progress.lines == [], progress.lines


def test_a_torn_trace_line_is_retried_rather_than_parsed(tmp_path):
    """A half-written line at the moment of a poll is the normal case for an append-only file."""
    ws = _workspace(tmp_path)
    progress = RunProgress(_OrchWithBus(_Bus(run_id="own_bus"), ws), quiet=True)
    complete = json.dumps({"type": "node.enter", "run_id": "run_child", "node_id": "dev",
                           "payload": {}})
    with progress:
        ws.trace_path.write_text(complete + "\n" + '{"type": "node.ex')  # torn write
        progress._drain_trace()
        assert len(progress.lines) == 1
        # The torn line is completed and picked up on the next poll rather than lost.
        ws.trace_path.write_text(complete + "\n" + '{"type": "node.exit", "run_id": "run_child",'
                                                   ' "node_id": "dev", "payload": {}}\n')
        progress._drain_trace()
    assert len(progress.lines) == 2, progress.lines


def test_a_run_with_no_bus_says_so_instead_of_hanging_silently(tmp_path):
    """A silently empty display is the failure this whole class exists to end."""
    class NoBus:
        workspace = None

    progress = RunProgress(NoBus(), quiet=True)
    with progress:
        pass
    assert any("no event bus" in line for line in progress.lines), progress.lines


def test_progress_is_bounded_so_a_long_run_cannot_flood_the_terminal(tmp_path):
    """A terminal that scrolls at thousands of lines a minute is indistinguishable from a hung one,
    so the narration is capped and the cap is *reported* rather than silently applied."""
    ws = _workspace(tmp_path)
    bus = _Bus()
    progress = RunProgress(_OrchWithBus(bus, ws), limit=3, quiet=True)
    with progress:
        for index in range(50):
            bus.emit("node.enter", node_id=f"n{index}", payload={})
    events = [line for line in progress.lines if "node" in line]
    assert len(events) == 3, progress.lines
    assert progress.dropped == 47
    assert any("47 further event(s) not shown" in line for line in progress.lines), progress.lines
    # The note reports the display itself, so it does not consume a slot in the event budget.
    assert len(progress.lines) == 4
    assert progress.dropped == 47


def test_a_broken_subscriber_does_not_disable_the_narration(tmp_path):
    """A subscriber that raises is disabled permanently by `EventBus._deliver`, so the narration must
    survive an event whose payload cannot be formatted — otherwise one bad shape kills the display
    for the rest of the run."""
    ws = _workspace(tmp_path)
    bus = _Bus()
    progress = RunProgress(_OrchWithBus(bus, ws), quiet=True)
    with progress:
        bus.emit("node.enter", node_id="dev", payload={"summary": object()})
        bus.emit("node.exit", node_id="dev", payload={"status": "done"})
    assert any("← node" in line for line in progress.lines), progress.lines


# ── the run produces the node transitions the display reads ──────────────────


def test_the_executor_emits_a_node_entry_and_exit_around_each_node(tmp_path, config, library):
    """`node.enter`/`node.exit` were in the protocol and read by the activity timeline, but **nothing
    emitted them** — so a run's node transitions existed only in the runner's checkpoint.

    That is the same defect as the silent `/run`, one level down: anything watching the event stream
    saw a run that started, went quiet and ended. This asserts the producer exists and that the exit
    precedes the handoff the node produced, which is the order a reader expects.
    """
    from engine.artifacts import ArtifactStore
    from engine.bus import EventBus
    from engine.executor import ExecutorContext, NodeExecutor
    from engine.idempotency import EffectJournal
    from engine.skills import FilesystemSkillSource

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    bus = EventBus(run_id="run_t", trace_path=project / ".agent_state" / "trace.jsonl")
    org = default_company(provider="fake", model="fake-model", context_window=32768)
    for spec in org.agents.values():
        if spec.is_ai:
            spec.provider, spec.model = "fake", "fake-model"
    provider = ScriptedProvider(reply=json.dumps({
        "status": "done", "verdict": "ok", "summary": "implemented",
        "criteria_satisfied": [{"criterion": "Evidence recorded", "satisfied": True,
                                "evidence": "src/app.py#1"}],
        "artifacts": [{"type": "change", "path": "src/app.py", "content": "x = 1\n"}],
    }))
    manifest = {"nodes": [{"id": "dev", "skill": "backend-developer", "outputs": ["change"]}]}
    ctx = ExecutorContext(
        org=org, gateway=Gateway(config, {"fake": provider}, estimator=TokenEstimator()),
        skills=FilesystemSkillSource(library), workspace=project,
        store=ArtifactStore(workspace_root=project),
        journal=EffectJournal(path=project / ".agent_state" / "effects.jsonl"),
        bus=bus, run_id="run_t", workflow="t", config=config, manifest=manifest)
    NodeExecutor(ctx).execute_node(
        "dev", {"nodes": {}, "artifacts": {}, "budget": {"steps_used": 0}, "decisions": [],
                "open_questions": []}, {"pass": 1})

    kinds = [event.type_value for event in bus.history()]
    assert "node.enter" in kinds, kinds
    assert "node.exit" in kinds, kinds
    # The entry opens the node and the exit closes it, with the handoff the node produced after it.
    assert kinds.index("node.enter") < kinds.index("node.exit")
    for handoff in ("handoff.proposed", "handoff.accepted"):
        if handoff in kinds:
            assert kinds.index("node.exit") < kinds.index(handoff), kinds


# ── /cache and /doctor: the two read surfaces that were serve-only ───────────


def test_cache_reports_the_store_for_this_workspace(config, tmp_path):
    """The store is what survives a restart, so its directory and counts must be named."""
    ws = _workspace(tmp_path)
    session, lines, _ = make_session(config)
    session.workspace = ws
    session._cmd_cache("")
    output = "\n".join(lines)
    assert str(ws.cache_dir) in output
    assert "prefixes" in output and "savings" in output
    # An unreported cache must read as unreported, never as a 0% hit rate.
    assert "unreported" in output
    assert "unknown" in output


def test_cache_says_so_when_no_workspace_is_attached(config):
    session, lines, _ = make_session(config)
    session._cmd_cache("")
    assert "no workspace" in "\n".join(lines)


def test_doctor_in_the_session_runs_the_same_checks_as_the_command(config, library):
    """Two copies of "what a healthy engine is" is a second thing to keep correct, so this asserts
    the session reports every check the command does."""
    from engine.cli import doctor_checks

    session, lines, _ = make_session(config)
    session.library = library
    session._cmd_doctor("")
    output = "\n".join(lines)
    for check in doctor_checks(config.path, library.files.root):
        assert check["check"] in output, f"/doctor omits {check['check']}"
    assert "doctor:" in output


# ── /setup: the provider wizard, through the console's own path ──────────────


@pytest.fixture
def creds(tmp_path):
    """A writable copy of the real credentials, so `/setup` never touches the checkout's own file."""
    target = tmp_path / "credentials.json"
    shutil.copy(ROOT / "credentials.json", target)
    return target


def test_setup_writes_a_provider_through_the_console_handler(creds, library):
    """Before this, there was no CLI way to add a provider at all: users hand-edited
    `credentials.json`, the one file where a mistake costs a key. It must go through the same
    handler the app uses, so the two cannot disagree about what a valid entry is."""
    config = load(creds)
    before = json.loads(creds.read_text())
    lines: list[str] = []
    answers = iter(["groq", "openai", "http://127.0.0.1:9/v1", "GROQ_API_KEY"])
    session = ChatSession(config=config, gateway=Gateway(config, {}, estimator=TokenEstimator()),
                          workspace=_workspace(pathlib.Path(tempfile.mkdtemp())), library=library,
                          output_fn=lines.append, input_fn=lambda prompt: next(answers, ""),
                          stream=False)
    session._cmd_setup("")
    after = json.loads(creds.read_text())
    assert "groq" in after["providers"]
    assert after["providers"]["groq"]["api_key_env"] == "GROQ_API_KEY"
    assert after["providers"]["groq"]["base_url"] == "http://127.0.0.1:9/v1"
    # Merge, never replace: every other provider and the model windows must survive.
    assert sorted(after["providers"]) == sorted([*before["providers"], "groq"])
    assert after["models"] == before["models"]
    assert "saved groq" in "\n".join(lines)


def test_setup_reports_an_unreachable_endpoint_without_refusing_to_save(creds, library):
    """A failed test is an *answer* to "test this", not a refusal: an endpoint can be down while the
    values are right, and the user is the one who knows which."""
    config = load(creds)
    lines: list[str] = []
    session = ChatSession(config=config, gateway=Gateway(config, {}, estimator=TokenEstimator()),
                          workspace=_workspace(pathlib.Path(tempfile.mkdtemp())), library=library,
                          output_fn=lines.append, input_fn=lambda prompt: "", stream=False)
    session._cmd_setup("deadprov openai http://127.0.0.1:9/v1")
    output = "\n".join(lines)
    assert "not reachable" in output
    assert "deadprov" in json.loads(creds.read_text())["providers"]


def test_setup_refuses_an_unknown_kind_without_writing(creds, library):
    config = load(creds)
    before = creds.read_text()
    lines: list[str] = []
    session = ChatSession(config=config, gateway=Gateway(config, {}, estimator=TokenEstimator()),
                          workspace=_workspace(pathlib.Path(tempfile.mkdtemp())), library=library,
                          output_fn=lines.append, input_fn=lambda prompt: "", stream=False)
    session._cmd_setup("bad banana http://127.0.0.1:9/v1")
    assert "could not save" in "\n".join(lines)
    assert creds.read_text() == before


def test_setup_abandoned_at_the_first_prompt_writes_nothing(creds, library):
    config = load(creds)
    before = creds.read_text()
    lines: list[str] = []
    session = ChatSession(config=config, gateway=Gateway(config, {}, estimator=TokenEstimator()),
                          workspace=_workspace(pathlib.Path(tempfile.mkdtemp())), library=library,
                          output_fn=lines.append, input_fn=lambda prompt: "", stream=False)
    session._cmd_setup("")
    assert "nothing added" in "\n".join(lines)
    assert creds.read_text() == before


def test_setup_says_so_when_there_is_no_credentials_file(library):
    """Refusing to invent a path: `write_provider` requires an existing file, so a session without one
    must explain rather than create a second config the engine will never read."""
    from engine.config import Config

    lines: list[str] = []
    session = ChatSession(config=Config(), gateway=Gateway(Config(), {}, estimator=TokenEstimator()),
                          workspace=_workspace(pathlib.Path(tempfile.mkdtemp())), library=library,
                          output_fn=lines.append, input_fn=lambda prompt: "", stream=False)
    session._cmd_setup("x openai http://127.0.0.1:9/v1")
    assert "could not save" in "\n".join(lines)
