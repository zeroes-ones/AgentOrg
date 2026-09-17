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
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.chat import COMMANDS, ChatSession, _slug
from engine.config import load
from engine.gateway import Gateway
from engine.org import default_company
from engine.providers.base import ChatResponse, Usage
from engine.providers.fake import FakeProvider
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
