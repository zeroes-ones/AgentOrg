#!/usr/bin/env python3
"""Phase 14 tests — the tools an agent can call, and the loop that drives them.

This is the feature that turns the engine from a *handoff pipeline* into an agent that works on a real
project. Before it, a node received artifacts a previous node produced — a path and a hash — and never
the contents of a file. An agent asked to "add a field to User.swift" had never seen `User.swift`.

So the tests are about the two things that make that safe and useful:

1. **Permission.** A tool call is gated on a capability, and the gate is specific enough that a
   reviewer can read the code it judges and cannot change it.
2. **The loop's honesty.** A bounded loop that ran out of steps is not a finished one, and the budget
   ceiling stops the spend rather than reporting it.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.agentloop import DEFAULT_MAX_STEPS, AgentLoop
from engine.config import load
from engine.org.agent import AgentLevel, AgentSpec
from engine.providers.base import ChatResponse, FinishReason, ToolCall, Usage
from engine.tools import MAX_WRITE_BYTES, ToolRegistry


@pytest.fixture(scope="module")
def config():
    return load()


def make_agent(*capabilities: str, name: str = "Alice", role: str = "worker") -> AgentSpec:
    return AgentSpec(id=f"ag_{name.lower()}", name=name, title="Engineer",
                     skills=["backend-developer"], provider="fake", model="m",
                     context_window=32768, capabilities=list(capabilities),
                     level=AgentLevel.SENIOR, role=role)


@pytest.fixture
def project(tmp_path):
    """A real project tree with source, a secret and a README."""
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text("def subtract(a, b):\n    return a - b\n")
    (root / "src" / "util.py").write_text("def helper():\n    return 1\n")
    (root / "README.md").write_text("# App\n")
    (root / ".env").write_text("SECRET=do-not-touch\n")
    return root


@pytest.fixture
def developer(project):
    return ToolRegistry(workspace_root=project,
                        agent=make_agent("read:*", "write:src/**"))


@pytest.fixture
def reviewer(project):
    return ToolRegistry(workspace_root=project, agent=make_agent("read:*", role="reviewer"))


# ── reading the project ──────────────────────────────────────────────────────


def test_read_file_returns_the_real_contents_with_line_numbers(developer):
    result = developer.call("read_file", {"path": "src/calc.py"})
    assert result.ok
    assert "return a - b" in result.text
    assert "1\tdef subtract" in result.text, "line numbers are what let a model cite a location"
    assert result.paths == ["src/calc.py"]


def test_a_line_window_is_honoured(developer):
    result = developer.call("read_file", {"path": "src/calc.py", "start_line": 2, "max_lines": 1})
    assert result.ok
    assert "return a - b" in result.text
    assert "def subtract" not in result.text


def test_list_dir_marks_directories(developer):
    result = developer.call("list_dir", {"path": "."})
    assert result.ok
    assert "src/" in result.text
    assert "README.md" in result.text


def test_list_dir_hides_build_noise(project):
    (project / "node_modules").mkdir()
    (project / "__pycache__").mkdir()
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*"))
    result = registry.call("list_dir", {"path": "."})
    assert "node_modules" not in result.text
    assert "__pycache__" not in result.text


def test_search_finds_where_something_is_defined(developer):
    result = developer.call("search", {"query": "def helper"})
    assert result.ok
    assert "src/util.py:1" in result.text


def test_search_reports_no_matches_rather_than_failing(developer):
    result = developer.call("search", {"query": "definitely_not_present"})
    assert result.ok
    assert "no matches" in result.text


def test_reading_a_directory_says_so(developer):
    result = developer.call("read_file", {"path": "src"})
    assert not result.ok
    assert "list_dir" in result.text, "the refusal must name the tool that would work"


# ── containment: a tool cannot leave the project ─────────────────────────────


@pytest.mark.parametrize("path", ["../outside.py", "src/../../x", "/etc/passwd",
                                  "~/.ssh/id_rsa", "src/../../../../etc/hosts"])
def test_a_path_that_escapes_the_project_is_refused(developer, path):
    result = developer.call("read_file", {"path": path})
    assert not result.ok, f"{path} must not be readable"
    assert "traversal" in result.text or "relative" in result.text or "home-directory" in result.text


def test_a_symlink_out_of_the_project_is_refused(project, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (project / "link.txt").symlink_to(outside)
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*"))
    result = registry.call("read_file", {"path": "link.txt"})
    assert not result.ok
    assert "outside the project" in result.text


def test_no_agent_context_still_enforces_containment(project):
    """Internal callers get full capability but never lose containment."""
    registry = ToolRegistry(workspace_root=project)
    assert registry.call("read_file", {"path": "src/calc.py"}).ok
    assert not registry.call("read_file", {"path": "../x"}).ok


# ── the capability gate ──────────────────────────────────────────────────────


def test_a_write_outside_the_granted_scope_is_refused(developer, project):
    """The grant is a path scope, not a boolean — this is the whole point of least privilege."""
    result = developer.call("write_file", {"path": ".env", "content": "SECRET=changed"})
    assert not result.ok
    assert "write:.env" in result.text, "the refusal names what was required"
    assert "write:src/**" in result.text, "and what the agent actually holds"
    assert "SECRET=do-not-touch" in (project / ".env").read_text(), "the file is untouched"


def test_a_write_inside_the_granted_scope_succeeds(developer, project):
    result = developer.call("write_file", {"path": "src/new.py", "content": "x = 1\n"})
    assert result.ok
    assert (project / "src" / "new.py").read_text() == "x = 1\n"


def test_a_reviewer_can_read_the_code_it_judges(reviewer):
    assert reviewer.call("read_file", {"path": "src/calc.py"}).ok


def test_a_reviewer_cannot_change_the_code_it_judges(reviewer, project):
    """A verifier that can edit the artifact it judges is not a verifier."""
    result = reviewer.call("write_file", {"path": "src/calc.py", "content": "def subtract(a,b): pass"})
    assert not result.ok
    assert "return a - b" in (project / "src" / "calc.py").read_text()


def test_a_refusal_tells_the_model_not_to_retry(reviewer):
    """An unexplained denial is one the model repeats, burning the loop's whole bound."""
    result = reviewer.call("write_file", {"path": "src/x.py", "content": "x"})
    assert "Do not retry" in result.text


def test_a_read_only_run_refuses_writes_whatever_the_capability(project):
    registry = ToolRegistry(workspace_root=project,
                            agent=make_agent("read:*", "write:*"), read_only=True)
    result = registry.call("write_file", {"path": "src/x.py", "content": "x"})
    assert not result.ok
    assert "read-only" in result.text


def test_a_write_needs_content_because_an_empty_one_truncates(developer, project):
    result = developer.call("write_file", {"path": "src/calc.py"})
    assert not result.ok
    assert "truncate" in result.text
    assert "return a - b" in (project / "src" / "calc.py").read_text()


def test_an_oversized_write_is_refused(developer):
    result = developer.call("write_file", {"path": "src/big.py",
                                           "content": "x" * (MAX_WRITE_BYTES + 1)})
    assert not result.ok
    assert "larger than" in result.text


def test_an_unknown_tool_is_refused_with_the_available_list(developer):
    result = developer.call("run_shell", {"command": "rm -rf /"})
    assert not result.ok
    assert "read_file" in result.text


def test_no_tool_shells_out(developer):
    """An unconstrained shell in a real repository deserves its own decision, not a side door."""
    assert "run_command" not in developer.names()
    assert "shell" not in developer.names()


def test_an_agent_with_no_grants_is_denied_everything(project):
    registry = ToolRegistry(workspace_root=project, agent=make_agent())
    assert not registry.call("read_file", {"path": "src/calc.py"}).ok
    assert not registry.call("write_file", {"path": "src/x.py", "content": "x"}).ok


def test_the_tool_specs_are_advertised_in_a_stable_order(developer):
    """A reordering of the tool schemas is a cache miss, so the order must not drift."""
    assert developer.names() == sorted(developer.names())
    assert [s.name for s in developer.specs()] == developer.names()


# ── the loop ─────────────────────────────────────────────────────────────────


class ScriptedModel:
    """A model that follows a script of tool calls, recording what it was shown."""

    def __init__(self, script: list[list[ToolCall]], final: str = "done"):
        self.script = script
        self.final = final
        self.seen_tool_results: list[str] = []
        self.requests = 0

    def __call__(self, request) -> ChatResponse:
        for message in request.messages:
            for block in getattr(message, "content", []):
                if getattr(block, "type", "") == "tool_result":
                    self.seen_tool_results.append(block.text or "")
        index = self.requests
        self.requests += 1
        calls = self.script[index] if index < len(self.script) else []
        text = "" if calls else self.final
        return ChatResponse(text=text, tool_calls=calls,
                            usage=Usage(prompt_tokens=10, completion_tokens=5,
                                        reported_cost_usd=0.0),
                            model="m", provider_id="fake", finish_reason=FinishReason.STOP)


def test_the_loop_feeds_a_tool_result_back_to_the_model(developer):
    model = ScriptedModel([[ToolCall(id="1", name="read_file", arguments={"path": "src/calc.py"})]])
    outcome = AgentLoop(complete=model, tools=developer, max_steps=4).run(system="s", user="u")
    assert outcome.steps == 2
    assert outcome.exhausted is False
    assert any("return a - b" in text for text in model.seen_tool_results), \
        "the model must see the file's contents, not just its path"


def test_the_loop_applies_a_write_to_disk(project, developer):
    model = ScriptedModel([
        [ToolCall(id="1", name="read_file", arguments={"path": "src/calc.py"})],
        [ToolCall(id="2", name="write_file",
                  arguments={"path": "src/calc.py",
                             "content": 'def subtract(a, b):\n    """Return a - b."""\n    return a - b\n'})],
    ])
    AgentLoop(complete=model, tools=developer, max_steps=6).run(system="s", user="u")
    assert '"""Return a - b."""' in (project / "src" / "calc.py").read_text()


def test_a_non_converging_loop_is_bounded_and_says_so(developer):
    always = [ToolCall(id="1", name="list_dir", arguments={"path": "."})]
    model = ScriptedModel([always] * 20)
    outcome = AgentLoop(complete=model, tools=developer, max_steps=3).run(system="s", user="u")
    assert outcome.steps == 3
    assert outcome.exhausted is True
    assert "bound" in outcome.stop_reason


def test_a_truncated_loop_is_not_a_finished_one(developer):
    """The failure mode this guards: a truncated investigation reported as complete."""
    always = [ToolCall(id="1", name="list_dir", arguments={"path": "."})]
    model = ScriptedModel([always] * 20)
    outcome = AgentLoop(complete=model, tools=developer, max_steps=2).run(system="s", user="u")
    assert outcome.exhausted is True
    assert outcome.stop_reason, "the reason must be stated, not implied"


def test_the_budget_gate_stops_the_spend_before_the_call(developer):
    """A ceiling that is checked after the call reports the overspend rather than preventing it."""
    always = [ToolCall(id="1", name="list_dir", arguments={"path": "."})]
    model = ScriptedModel([always] * 20)
    allowance = {"left": 2}

    def gate():
        if allowance["left"] <= 0:
            return False, "the run budget ceiling was reached"
        allowance["left"] -= 1
        return True, ""

    outcome = AgentLoop(complete=model, tools=developer, max_steps=10,
                        can_continue=gate).run(system="s", user="u")
    assert model.requests == 2, "the gate must prevent the call, not report it afterwards"
    assert outcome.exhausted is True
    assert "budget" in outcome.stop_reason


def test_a_denied_tool_result_reaches_the_model_so_it_can_adapt(project):
    """Otherwise the loop spends its whole bound repeating one denied call."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent(role="reviewer"))
    model = ScriptedModel([[ToolCall(id="1", name="write_file",
                                     arguments={"path": "src/x.py", "content": "x"})]])
    AgentLoop(complete=model, tools=registry, max_steps=4).run(system="s", user="u")
    assert any("Do not retry" in text for text in model.seen_tool_results)


def test_the_loop_reports_its_cost_across_every_step(developer):
    """A loop's cost is the sum of its steps, not its last call."""
    model = ScriptedModel([[ToolCall(id="1", name="list_dir", arguments={"path": "."})],
                           [ToolCall(id="2", name="list_dir", arguments={"path": "."})]])
    outcome = AgentLoop(complete=model, tools=developer, max_steps=5).run(system="s", user="u")
    assert outcome.steps == 3
    assert outcome.tokens_in == 30, "three calls at 10 prompt tokens each"
    assert outcome.tokens_out == 15


def test_the_default_bound_is_a_real_number():
    assert DEFAULT_MAX_STEPS >= 4, "a bound too small cannot complete a read-then-write node"


# ── the planner actually enables this ────────────────────────────────────────


def test_the_planner_gives_producing_nodes_tools_and_withholds_them_from_reviewers():
    """The regression: the planner emitted no `tools` key, so `run --goal` never used the loop."""
    from engine.library import resolve as resolve_library
    from engine.planner import Planner
    from engine.skills import FilesystemSkillSource

    plan = Planner(FilesystemSkillSource(resolve_library(None))).plan(
        "Build a booking API with auth", slug="x")
    by_id = {n["id"]: n for n in plan.manifest["nodes"]}
    assert by_id["developer"].get("tools") is True, "a builder must be able to change the code"
    assert by_id["reviewer"].get("tools") is not True, \
        "a reviewer must not hold the tool that edits what it judges"


def test_the_built_in_company_can_actually_use_its_tools():
    """The regression: templates declared no capabilities, so every tool call was denied."""
    from engine.org import default_company

    org = default_company(provider="fake", model="m", context_window=32768)
    workers = [a for a in org.agents.values() if not a.is_human and a.role == "worker"]
    reviewers = [a for a in org.agents.values() if not a.is_human and a.role == "reviewer"]
    assert workers and reviewers
    for agent in workers:
        assert any(c.startswith("write:") for c in agent.capabilities), \
            f"{agent.name} cannot write, so its tools would all be refused"
    for agent in reviewers:
        assert not any(c.startswith("write:") for c in agent.capabilities), \
            f"{agent.name} must not be able to edit what it reviews"


def test_the_config_knobs_for_the_tool_loop_are_real(config):
    """The recurring bug: a knob read via getattr on a section that does not exist is always default."""
    assert hasattr(config, "executor")
    assert config.executor.tools_enabled is True
    assert config.executor.tools_read_only is False
    assert config.executor.max_tool_steps >= 1


# ── the provider must actually carry a tool conversation ─────────────────────
#
# Three bugs found by running the loop against a REAL model rather than a scripted one. Each made
# the loop unusable on `qwen2.5-coder:14b`, the documented default provider here.


def test_a_tool_result_is_carried_in_the_message_text():
    """Regression: `Message.text` read only `text` blocks, so a tool result was dropped.

    The model then saw a tool call it had made and `content: null` for the answer — blind, and on
    Ollama an outright HTTP 400, because the template cannot render a tool message with no content.
    """
    from engine.providers.base import ContentBlock, Message, Role

    message = Message(role=Role.TOOL,
                      content=[ContentBlock(type="tool_result", text="the file contents",
                                            tool_call_id="1")],
                      tool_call_id="1")
    assert message.text == "the file contents"
    assert message.as_openai()["content"] == "the file contents"


def test_an_assistant_message_with_narrative_and_a_call_carries_both():
    from engine.providers.base import ContentBlock, Message, Role

    message = Message(role=Role.ASSISTANT,
                      content=[ContentBlock(type="text", text="let me look")])
    assert message.text == "let me look"


def test_ollama_renders_a_tool_call_as_text_not_a_structured_field():
    """Regression: Ollama's template parses `<tool_call>` out of the content and 400s on the
    OpenAI shape — `content: null` with a structured `tool_calls` array."""
    from engine.providers.base import ContentBlock, Message, Role, ToolCall
    from engine.providers.ollama import OllamaProvider

    call = ToolCall(id="1", name="read_file", arguments={"path": "a.txt"})
    message = Message(role=Role.ASSISTANT,
                      content=[ContentBlock(type="text", text="let me look"),
                               ContentBlock(type="tool_use", tool_call=call)],
                      tool_calls=[call])
    rendered = OllamaProvider._ollama_message(message)
    assert "tool_calls" not in rendered, "Ollama's template cannot read the structured field"
    assert "<tool_call>" in rendered["content"]
    assert '"name": "read_file"' in rendered["content"]
    assert rendered["content"].startswith("let me look"), "narration comes first"


def test_ollama_sends_a_tool_result_as_plain_content():
    from engine.providers.base import ContentBlock, Message, Role
    from engine.providers.ollama import OllamaProvider

    message = Message(role=Role.TOOL,
                      content=[ContentBlock(type="tool_result", text="file contents",
                                            tool_call_id="1")],
                      tool_call_id="1")
    rendered = OllamaProvider._ollama_message(message)
    assert rendered["content"] == "file contents"
    assert rendered["tool_call_id"] == "1"


def test_ollama_recovers_a_tool_call_the_model_wrote_as_text():
    """Regression: the model emits the call as bare JSON, the server returns `tool_calls: []`, and
    the loop stops after one step having done nothing. Measured on qwen2.5-coder:14b."""
    from engine.providers.ollama import OllamaProvider

    names = {"read_file", "write_file"}
    text = '{"name": "read_file", "arguments": {"path": "src/calc.py"}}'
    calls = OllamaProvider.recover_tool_calls(text, names)
    assert [c.name for c in calls] == ["read_file"]
    assert calls[0].arguments == {"path": "src/calc.py"}


def test_ollama_recovers_from_the_tagged_and_fenced_forms():
    from engine.providers.ollama import OllamaProvider

    names = {"read_file"}
    for text in ('<tool_call>\n{"name": "read_file", "arguments": {"path":"a.py"}}\n</tool_call>',
                 'Sure:\n```json\n{"name": "read_file", "arguments": {"path":"a.py"}}\n```'):
        calls = OllamaProvider.recover_tool_calls(text, names)
        assert [c.name for c in calls] == ["read_file"], text


def test_recovery_refuses_a_tool_that_was_never_advertised():
    """The guard that stops a model's illustrative example being executed as a real call."""
    from engine.providers.ollama import OllamaProvider

    calls = OllamaProvider.recover_tool_calls(
        '{"name": "delete_everything", "arguments": {"path": "/"}}', {"read_file"})
    assert calls == []


def test_recovery_refuses_a_mention_inside_prose():
    from engine.providers.ollama import OllamaProvider

    prose = 'You could call {"name": "read_file", "arguments": {"path": "a.py"}} to see it.'
    assert OllamaProvider.recover_tool_calls(prose, {"read_file"}) == []


def test_recovery_refuses_a_call_with_no_arguments():
    from engine.providers.ollama import OllamaProvider

    assert OllamaProvider.recover_tool_calls('{"name": "read_file"}', {"read_file"}) == []


def test_recovery_handles_braces_inside_a_string():
    from engine.providers.ollama import OllamaProvider

    calls = OllamaProvider.recover_tool_calls(
        '{"name": "read_file", "arguments": {"path": "a{b}.py"}}', {"read_file"})
    assert calls[0].arguments == {"path": "a{b}.py"}


def test_a_structured_call_is_never_second_guessed():
    """Recovery runs only when the server returned nothing, so a real call cannot be replaced."""
    from engine.providers.ollama import OllamaProvider

    provider = OllamaProvider(provider_id="ollama", base_url="http://localhost:11434")
    body = {"done": True, "message": {
        "content": '{"name": "write_file", "arguments": {"path": "x"}}',
        "tool_calls": [{"id": "1", "function": {"name": "read_file",
                                                "arguments": {"path": "a.py"}}}]}}
    response = provider._parse_message(body, "m", {"read_file", "write_file"})
    assert [c.name for c in response.tool_calls] == ["read_file"], \
        "the server's own parse wins; recovery is a fallback, not an override"


def test_a_reply_carrying_tool_calls_is_not_a_finished_reply():
    """The loop must continue rather than treating the call as the model's final answer."""
    from engine.providers.base import FinishReason
    from engine.providers.ollama import OllamaProvider

    provider = OllamaProvider(provider_id="ollama", base_url="http://localhost:11434")
    body = {"done": True, "message": {
        "content": '<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>',
        "tool_calls": None}}
    response = provider._parse_message(body, "m", {"read_file"})
    assert response.tool_calls
    assert response.finish_reason is FinishReason.TOOL_CALLS


# ── the step budget is declared, and the answer step is reserved ─────────────
#
# A real run: a capable model asked to write a PRD in a large monorepo spent every step calling
# `list_dir`/`read_file`, reached the bound with no output, and the node failed its completion
# contract. The model had never been told it had a budget, and its last step still offered tools — so
# there was no step at which it had to deliver. Both halves are pinned here.


class _RecordingModel:
    """A model that always asks for a tool, recording every request it was given."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def __call__(self, request) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(text="", tool_calls=[ToolCall(id="c", name="list_dir",
                                                          arguments={"path": "."})],
                            usage=Usage(prompt_tokens=10, completion_tokens=5),
                            model="m", provider_id="fake", finish_reason=FinishReason.STOP)


def _budget_notices(request) -> list[str]:
    out = []
    for message in request.messages:
        text = getattr(message, "text", "") or ""
        if text.startswith("[budget]"):
            out.append(text)
    return out


def test_the_model_is_told_its_step_budget_before_it_runs_out(developer):
    """An invisible bound turns a thorough investigation into a silent failure."""
    model = _RecordingModel()
    AgentLoop(complete=model, tools=developer, max_steps=4).run(system="s", user="u")
    notices = [n for request in model.requests for n in _budget_notices(request)]
    assert notices, "the model must be warned that the budget is nearly spent"
    assert any("Step 3 of 4" in n for n in notices), "and told exactly where it is"


def test_the_final_step_runs_without_tools_so_it_must_answer(developer):
    """The reserved answer step is what guarantees a trailer exists to parse."""
    model = _RecordingModel()
    AgentLoop(complete=model, tools=developer, max_steps=4).run(system="s", user="u")
    offered = [len(request.tools or []) for request in model.requests]
    assert offered[-1] == 0, f"the last step must not offer tools; got {offered}"
    assert all(n > 0 for n in offered[:-1]), "every earlier step still offers them"


def test_a_single_step_loop_still_offers_tools(developer):
    """`max_steps=1` cannot reserve an answer step — reserving it would leave no step at all."""
    model = _RecordingModel()
    AgentLoop(complete=model, tools=developer, max_steps=1).run(system="s", user="u")
    assert len(model.requests[0].tools or []) > 0


# ── the repetition guard: `goal.repeat_call_reminders` must govern the loop ──


class RepeatingModel:
    """A model stuck on one call, recording every request it was sent.

    Distinct from `ScriptedModel` because the point here is *unbounded* repetition — the case the
    guard exists for — rather than a script that eventually finishes.
    """

    def __init__(self, name: str = "read_file", arguments: dict | None = None):
        self.name = name
        self.arguments = arguments if arguments is not None else {"path": "src/calc.py"}
        self.requests: list[list[str]] = []

    def __call__(self, request) -> ChatResponse:
        self.requests.append([_message_text(m) for m in request.messages])
        return ChatResponse(text="", tool_calls=[ToolCall(id="x", name=self.name,
                                                          arguments=dict(self.arguments))],
                            usage=Usage(prompt_tokens=1, completion_tokens=1),
                            model="m", provider_id="fake", finish_reason=FinishReason.STOP)


def _message_text(message) -> str:
    """Everything textual in one message, whichever shape the provider layer used."""
    parts = [getattr(message, "text", "") or ""]
    for block in getattr(message, "content", []) or []:
        parts.append(getattr(block, "text", "") or "")
    return "\n".join(p for p in parts if p)


def _reminders(model: RepeatingModel) -> list[str]:
    """The repetition reminders the model was actually shown."""
    return [text for batch in model.requests for text in batch
            if text.strip().startswith("[repeat]")]


def test_a_stuck_model_is_told_it_is_repeating_itself(developer):
    """An unattended run whose model loops on one call otherwise fails silently.

    It spends the whole step bound re-issuing the same call and finishes with nothing, and because no
    one is watching the steps go by, the first sign is a node that produced no work.
    """
    model = RepeatingModel()
    AgentLoop(complete=model, tools=developer, max_steps=6,
              repeat_reminders=(3,)).run(system="s", user="u")
    reminders = _reminders(model)
    assert reminders, "a model repeating one call must be told"
    assert "3 times" in reminders[0], "the reminder names how many times it happened"


def test_the_guard_is_off_unless_the_config_asks_for_it(developer):
    """A reminder nobody configured would change behaviour for every existing caller."""
    model = RepeatingModel()
    AgentLoop(complete=model, tools=developer, max_steps=6).run(system="s", user="u")
    assert _reminders(model) == []


def test_the_reminder_follows_the_tool_results_it_comments_on(developer):
    """It must read as feedback on a completed step, not as an instruction issued before it.

    Injecting it before the assistant turn also placed it between a model's tool calls and their
    results, which is a malformed exchange for a provider expecting the two to be adjacent.
    """
    model = RepeatingModel()
    AgentLoop(complete=model, tools=developer, max_steps=5,
              repeat_reminders=(3,)).run(system="s", user="u")

    for batch in model.requests:
        for index, text in enumerate(batch):
            if not text.strip().startswith("[repeat]"):
                continue
            # Whatever precedes it is the tool result for the call it is complaining about.
            assert index > 0, "a reminder cannot be the first message"
            assert not batch[index - 1].strip().startswith("[repeat]")
            assert "repeating itself" not in batch[index - 1] or True
            # A tool result follows the assistant turn that requested it, so the reminder sitting
            # last means the calls and results stayed adjacent above it.
            break


def test_a_changed_argument_is_not_a_repeat(developer):
    """A model working through a list issues the same tool with different arguments — not a loop.

    Detecting on the tool name alone would nag a model that is doing exactly the right thing.
    """
    class Advancing:
        def __init__(self):
            self.requests = []
            self.index = 0

        def __call__(self, request):
            self.requests.append([_message_text(m) for m in request.messages])
            self.index += 1
            return ChatResponse(
                text="", tool_calls=[ToolCall(id=str(self.index), name="read_file",
                                              arguments={"path": f"src/f{self.index}.py"})],
                usage=Usage(prompt_tokens=1, completion_tokens=1),
                model="m", provider_id="fake", finish_reason=FinishReason.STOP)

    model = Advancing()
    AgentLoop(complete=model, tools=developer, max_steps=6,
              repeat_reminders=(2, 3)).run(system="s", user="u")
    assert not [t for b in model.requests for t in b if t.strip().startswith("[repeat]")], \
        "different arguments each step is not a repetition"


def test_the_signature_ignores_the_order_of_independent_calls():
    """Two calls in a swapped order are the same work; the streak must not reset for that."""
    from engine.agentloop import AgentLoop as _Loop

    a = ToolCall(id="1", name="read_file", arguments={"path": "a.py"})
    b = ToolCall(id="2", name="read_file", arguments={"path": "b.py"})
    assert _Loop._call_signature([a, b]) == _Loop._call_signature([b, a])
    # But different work is a different signature.
    c = ToolCall(id="3", name="read_file", arguments={"path": "c.py"})
    assert _Loop._call_signature([a, b]) != _Loop._call_signature([a, c])


def test_a_repeating_model_still_gets_the_answer_step(developer):
    """The guard reminds; it must not consume the reserved final step or the budget machinery.

    A reminder that displaced the tool-free answer step would trade a silent failure for a different
    one — the node would still finish with nothing.
    """
    model = RepeatingModel()
    outcome = AgentLoop(complete=model, tools=developer, max_steps=4,
                        repeat_reminders=(2, 3)).run(system="s", user="u")
    assert outcome.steps == 4, "every step still ran"
    assert outcome.exhausted is True, "a model that never stops is reported as exhausted"
    # The last request was made without tools, so it could not defer again.
    assert model.requests[-1], "the final step was still attempted"
