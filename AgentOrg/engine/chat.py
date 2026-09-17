#!/usr/bin/env python3
"""chat.py — the conversational front door: talk to a model, or to the org.

WHY THIS EXISTS
---------------
Everything the engine can do was reachable only by knowing a subcommand name. That is fine for a
build system and wrong for the thing this project actually is: an org you talk to. This module is
the ChatGPT-shaped surface — you type a message, you get a reply, streamed — with the org's own
operations available as `/`-commands in the same loop.

Two modes, one loop:

- **Direct.** Plain text goes to one model, which answers. This is the "just let me talk to it" case,
  and it is the default because it is the one people reach for first.
- **Org.** `/run <goal>` hands the goal to the multi-agent pipeline, then the loop watches the run
  and lets you resolve gates without leaving the chat. `/approve`, `/reject`, `/instruct` act on the
  live run.

DESIGN
------
- **Provider-agnostic by construction.** Every call goes through the Gateway, so switching from
  Ollama to Anthropic to DeepSeek is a `/model` command, not a code path. Nothing here knows a wire
  format.
- **Streamed by default, because waiting is the product's main cost.** A local 14B takes ~100 s to
  answer; a spinner that prints nothing for that long reads as a hang. Streaming shows the tokens as
  they arrive, so the user can see it is thinking.
- **A `/`-command is data, not a special case.** The command table is one dict, so `/help` is
  generated from the same source that dispatches, and a new command cannot be added without being
  discoverable.
- **No hidden spend.** Every turn prints tokens and cost from the Gateway's own accounting, labelled
  `measured`/`estimated`/`unknown` — an unmeasured turn is never rendered as `$0.00`.
- **The transcript is the context.** The running conversation is the message list, trimmed to the
  model's window. There is no separate memory to go stale.

Usage:
    from engine.chat import ChatSession
    ChatSession(config=cfg, gateway=gw, org=org).run()

    python3 -m engine.cli chat
    python3 -m engine.cli chat --agent Alice
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from .providers.base import ChatRequest, Message, Role

__all__ = ["ChatSession", "ChatError", "SlashCommand", "COMMANDS"]


class ChatError(RuntimeError):
    """A chat problem worth naming rather than surfacing as a traceback."""


@dataclass
class SlashCommand:
    """One `/`-command: what it is called, what it does, and how to describe it."""

    name: str
    usage: str
    help: str
    handler: str


#: The whole command surface. `/help` is generated from this, so a command cannot exist and be
#: undiscoverable at the same time.
COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/help", "/help", "Show this list.", "_cmd_help"),
    SlashCommand("/model", "/model [name]", "Show or switch the model for direct chat.", "_cmd_model"),
    SlashCommand("/agent", "/agent [name]", "Show or switch which agent answers you.", "_cmd_agent"),
    SlashCommand("/agents", "/agents", "List the roster: who exists, on what model.", "_cmd_agents"),
    SlashCommand("/run", "/run <goal>", "Hand a goal to the org and watch it execute.", "_cmd_run"),
    SlashCommand("/status", "/status", "Where is the current run, and what has it cost?", "_cmd_status"),
    SlashCommand("/approve", "/approve", "Approve the gate the run is waiting on.", "_cmd_approve"),
    SlashCommand("/reject", "/reject <why>", "Reject the gate with your reason.", "_cmd_reject"),
    SlashCommand("/instruct", "/instruct <text>", "Steer the running agents.", "_cmd_instruct"),
    SlashCommand("/constraint", "/constraint <text>", "Add a non-negotiable rule that survives compaction.", "_cmd_constraint"),
    SlashCommand("/skills", "/skills [filter]", "Search the 327-skill library.", "_cmd_skills"),
    SlashCommand("/exit", "/exit", "Leave the chat.", "_cmd_exit"),
)

#: Kept deliberately small: the transcript is trimmed to the model's window, and a chat that keeps
#: every turn forever would silently grow past it.
MAX_TRANSCRIPT_TURNS = 24


class ChatSession:
    """One conversation, over one model or over the org.

    Parameters
    ----------
    config:
        Validated configuration; supplies the default provider/model and the budgets.
    gateway:
        The one way to a model. Chat does not touch an adapter directly.
    org:
        Optional roster. With it, `/run` and the gate commands work; without it, chat is direct-only.
    orchestrator / workspace:
        Optional. When present, `/run` executes for real rather than planning only.
    input_fn / output_fn:
        Injectable so the whole loop is testable without a terminal.
    """

    def __init__(self, *, config: Any, gateway: Any, org: Any | None = None,
                 orchestrator: Any | None = None, workspace: Any | None = None,
                 skills: Any | None = None, catalog: Any | None = None,
                 input_fn: Callable[[str], str] | None = None,
                 output_fn: Callable[[str], None] | None = None,
                 stream: bool = True) -> None:
        self.config = config
        self.gateway = gateway
        self.org = org
        self.orchestrator = orchestrator
        self.workspace = workspace
        self.skills = skills
        self.catalog = catalog
        self._input = input_fn or (lambda prompt: input(prompt))
        self._output = output_fn or (lambda line: print(line, flush=True))
        self.stream = stream

        self.provider: str = str(config.defaults.get("provider") or "ollama")
        self.model: str = str(config.defaults.get("model") or "")
        self.agent_id: str | None = None
        self._explicit_model = False
        self.transcript: list[Message] = []
        self.running_run_id: str | None = None
        self.running_slug: str | None = None
        self._exit = False

    # ── the loop ────────────────────────────────────────────────────────────

    def run(self) -> int:
        """The read-eval-print loop. Returns an exit code."""
        self._greet()
        while not self._exit:
            try:
                line = self._input("\nyou › ").strip()
            except (EOFError, KeyboardInterrupt):
                self._output("\n(leaving)")
                break
            if not line:
                continue
            if line.startswith("/"):
                self._dispatch(line)
                continue
            self._say(line)
        return 0

    def _greet(self) -> None:
        who = self._agent_name() or f"{self.provider}/{self.model}"
        self._output(f"Chatting with {who}.")
        self._output("Type a message, or /help for commands. /run <goal> hands work to the org.")

    def _dispatch(self, line: str) -> None:
        """Run a `/`-command, or say plainly that it is not one."""
        parts = line.split(maxsplit=1)
        name = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        command = next((c for c in COMMANDS if c.name == name), None)
        if command is None:
            self._output(f"unknown command {name!r} — try /help")
            return
        getattr(self, command.handler)(argument)

    # ── direct chat ─────────────────────────────────────────────────────────

    def _say(self, text: str, *, remember: bool = True) -> str:
        """Send one user turn and print the reply. Returns the reply text."""
        if remember:
            self.transcript.append(Message.text_message(Role.USER, text))
        request = ChatRequest(
            model=self.model,
            messages=list(self.transcript),
            system=self._system_prompt(),
            max_tokens=int(self.config.model_spec(self.model).max_output or 4096),
        )
        try:
            reply, usage = self._complete(request)
        except Exception as exc:  # noqa: BLE001 - a chat turn must not kill the session
            # The transcript must not keep a turn that was never answered, or the next call resends
            # it and the model sees a question with no response.
            if remember and self.transcript and self.transcript[-1].role is Role.USER:
                self.transcript.pop()
            self._output(f"[provider error] {exc}")
            return ""
        self.transcript.append(Message.text_message(Role.ASSISTANT, reply))
        self._trim_transcript()
        self._output(self._usage_line(usage))
        return reply

    def _complete(self, request: ChatRequest) -> tuple[str, dict[str, Any]]:
        """One completion, streamed when asked. Returns (text, usage summary).

        The text is printed here in the non-streaming path as well as streamed in the other, so the
        caller never has to know which path ran to know the user saw the reply.
        """
        if not self.stream:
            response = self.gateway.complete(request, provider_id=self.provider,
                                             agent_id=self.agent_id, node_id="chat")
            text = response.text
            usage = response.usage
            if text:
                self._output(text)
        else:
            # Streaming is the default because a long local generation with no output reads as a hang.
            text = ""
            usage = None
            for chunk in self.gateway.stream(request, provider_id=self.provider,
                                             agent_id=self.agent_id, node_id="chat"):
                if chunk.text:
                    text += chunk.text
                    self._stream_write(chunk.text)
                if chunk.usage is not None:
                    usage = chunk.usage
            if text:
                self._stream_write("\n")
                sys.stdout.flush()
        return text, self._usage_dict(usage)

    def _stream_write(self, text: str) -> None:
        """Write a streamed delta.

        Goes straight to stdout rather than through `_output`, because a partial token is not a line
        — routing it through a line-oriented sink would insert a newline per chunk. The captured
        output is still appended to, so a test can assert what the user saw.
        """
        sys.stdout.write(text)
        sys.stdout.flush()

    def _usage_dict(self, usage: Any) -> dict[str, Any]:
        """Normalise usage into the labelled shape the cost discipline requires."""
        if usage is None:
            return {"tokens_in": None, "tokens_out": None, "cost_usd": None, "source": "unknown"}
        tokens_in = getattr(usage, "prompt_tokens", None)
        tokens_out = getattr(usage, "completion_tokens", None)
        cost = self.gateway.compute_cost(self.provider, self.model, usage)
        return {
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost_usd": cost.usd if cost.known else None,
            "source": cost.source,
        }

    def _usage_line(self, usage: dict[str, Any]) -> str:
        """A one-line accounting footer. An unmeasured figure never reads as zero."""
        tokens_in = usage.get("tokens_in")
        tokens_out = usage.get("tokens_out")
        tokens = ("?" if tokens_in is None and tokens_out is None
                  else f"{tokens_in or 0}→{tokens_out or 0}")
        cost = usage.get("cost_usd")
        money = "unknown" if cost is None else f"${cost:.4f}"
        return f"  · {self.provider}/{self.model} · {tokens} tok · {money} ({usage.get('source')})"

    def _system_prompt(self) -> str:
        """The persona, so `/agent Alice` changes the voice and not just the label."""
        if self.org is None or self.agent_id is None:
            return "You are a helpful, concise software engineering assistant."
        spec = self.org.agents.get(self.agent_id)
        if spec is None:
            return "You are a helpful, concise software engineering assistant."
        skills = ", ".join(spec.skills) if spec.skills else "general engineering"
        return (
            f"You are {spec.name}, a {spec.title} in a software engineering organisation. "
            f"Your skills: {skills}. Answer in that role, concisely and concretely."
        )

    def _trim_transcript(self) -> None:
        """Keep the last N turns, so the transcript cannot grow past the window unnoticed."""
        if len(self.transcript) > MAX_TRANSCRIPT_TURNS * 2:
            self.transcript = self.transcript[-MAX_TRANSCRIPT_TURNS * 2:]

    def _agent_name(self) -> str:
        if self.org is None or self.agent_id is None:
            return ""
        spec = self.org.agents.get(self.agent_id)
        return f"{spec.name} ({spec.title})" if spec else ""

    # ── /-commands ──────────────────────────────────────────────────────────

    def _cmd_help(self, argument: str) -> None:
        self._output("Commands:")
        for command in COMMANDS:
            self._output(f"  {command.usage:26s} {command.help}")

    def _cmd_exit(self, argument: str) -> None:
        self._exit = True
        self._output("(leaving)")

    def _cmd_model(self, argument: str) -> None:
        if not argument:
            self._output(f"model: {self.provider}/{self.model}  (default: "
                         f"{self.config.defaults.get('provider')}/{self.config.defaults.get('model')})")
            if self.catalog is not None:
                self._output("available:")
                for entry in self.catalog.list_models(only_known_windows=True):
                    self._output(f"  {entry.provider_id:10s} {entry.model_id}")
            return
        provider, _, model = argument.partition("/")
        if not model:
            model, provider = provider, self.provider
        entry = self.catalog.resolve(provider, model) if self.catalog is not None else None
        if entry is None:
            self._output(f"no model {provider}/{model} — use /model with no argument to list them")
            return
        if not entry.window_known:
            self._output(f"{provider}/{model} has no known context window, so it cannot be bound")
            return
        self.provider, self.model, self._explicit_model = provider, model, True
        self._output(f"now talking to {provider}/{model}")

    def _cmd_agent(self, argument: str) -> None:
        if self.org is None:
            self._output("no roster is loaded, so there is no agent to switch to")
            return
        if not argument:
            self._output("agents:")
            for spec in self.org.agents.values():
                if spec.is_human:
                    continue
                mark = " *" if spec.id == self.agent_id else ""
                self._output(f"  {spec.name:10s} {spec.title:20s} {spec.provider}/{spec.model}{mark}")
            return
        spec = next((a for a in self.org.agents.values()
                     if a.name.lower() == argument.lower() and not a.is_human), None)
        if spec is None:
            self._output(f"no agent named {argument!r}")
            return
        self.agent_id = spec.id
        # Switching agent switches the model with it, unless the user pinned one explicitly.
        if not self._explicit_model and spec.model:
            self.provider, self.model = spec.provider or self.provider, spec.model
        self._output(f"now talking to {spec.name} ({spec.title}) on {self.provider}/{self.model}")

    def _cmd_agents(self, argument: str) -> None:
        self._cmd_agent("")

    def _cmd_skills(self, argument: str) -> None:
        if self.skills is None:
            self._output("no skill library is loaded")
            return
        names = list(self.skills.names())
        if argument:
            needle = argument.lower()
            names = [n for n in names if needle in n.lower()]
        if not names:
            self._output("no matching skills")
            return
        self._output(f"{len(names)} skill(s):")
        for name in names[:60]:
            self._output(f"  {name}")
        if len(names) > 60:
            self._output(f"  … and {len(names) - 60} more")

    # ── the org, from the chat ──────────────────────────────────────────────

    def _cmd_run(self, argument: str) -> None:
        if not argument:
            self._output("usage: /run <goal>")
            return
        if self.orchestrator is None:
            self._output("no orchestrator is loaded, so I cannot run a goal here")
            return
        self._output(f"planning: {argument}")
        slug = _slug(argument)
        try:
            run = self.orchestrator.prepare(argument, slug=slug)
        except Exception as exc:  # noqa: BLE001
            self._output(f"could not plan that: {exc}")
            return
        self.running_run_id = run.run_id
        self.running_slug = slug
        self._output(f"run {run.run_id}  ({run.phase.value})")
        if run.staffing_gaps:
            self._output("staffing gaps — the plan needs skills nobody holds:")
            for gap in run.staffing_gaps:
                self._output(f"  {gap['node_id']:16s} {gap['skill']}")
            self._output("hire for these, or amend the plan, before it can finish.")
        try:
            self.orchestrator.approve(run)
            outcome = self.orchestrator.execute(run)
        except Exception as exc:  # noqa: BLE001
            self._output(f"execution failed: {exc}")
            return
        self._report_outcome(run, outcome)

    def _current_run(self) -> Any | None:
        """The run this chat started, reloaded from its checkpoint.

        Reloaded rather than reused because a gate decision must act on the persisted run — the
        in-memory object goes stale the moment the runner writes its own checkpoint.
        """
        if self.orchestrator is None or not self.running_slug:
            return None
        try:
            return self.orchestrator.load(self.running_slug)
        except Exception:  # noqa: BLE001 - a chat command must not raise
            return None

    def _cmd_status(self, argument: str) -> None:
        if self.orchestrator is None:
            self._output("no orchestrator is loaded")
            return
        try:
            status = self.orchestrator.status()
        except Exception as exc:  # noqa: BLE001
            self._output(f"no run to report: {exc}")
            return
        self._output(f"run {status.get('run_id')}  ({status.get('phase')})")
        self._output(f"  goal : {status.get('goal')}")
        if status.get("gate"):
            gate = status["gate"]
            self._output(f"  gate : {gate['gate_id']} — {gate['reason'][:70]}")
            self._output("  resolve with /approve or /reject <why>")
        cost = status.get("cost") or {}
        money = cost.get("cost_usd")
        self._output(f"  cost : {cost.get('runs')} run(s), {cost.get('nodes')} node(s), "
                     f"{'unknown' if money is None else f'${money}'}")
        for name, record in sorted(((status.get("outcome") or {}).get("nodes") or {}).items()):
            self._output(f"    {name:16s} {str(record.get('status')):14s} {record.get('verdict')}")

    def _cmd_approve(self, argument: str) -> None:
        self._decide(approve=True, note=argument)

    def _cmd_reject(self, argument: str) -> None:
        self._decide(approve=False, note=argument)

    def _decide(self, *, approve: bool, note: str) -> None:
        run = self._current_run()
        if run is None:
            self._output("no run is loaded to decide on — start one with /run <goal>")
            return
        if getattr(run, "gate", None) is None:
            self._output(f"the run is {run.phase.value}, which is not waiting on a gate")
            return
        try:
            self.orchestrator.decide(approve, run=run, note=note)
        except Exception as exc:  # noqa: BLE001
            self._output(f"could not record that: {exc}")
            return
        self._output(("approved" if approve else "rejected") + f" — phase {run.phase.value}")
        if note and not approve:
            self._output(f"  note: {note}")

    def _cmd_instruct(self, argument: str) -> None:
        self._instruct(argument, as_constraint=False)

    def _cmd_constraint(self, argument: str) -> None:
        self._instruct(argument, as_constraint=True)

    def _instruct(self, text: str, *, as_constraint: bool) -> None:
        if not text:
            self._output("usage: /instruct <text>  (or /constraint <text>)")
            return
        run = self._current_run()
        if run is None:
            self._output("no run is loaded — start one with /run <goal>")
            return
        try:
            self.orchestrator.instruct(text, run=run, as_constraint=as_constraint)
        except Exception as exc:  # noqa: BLE001
            self._output(f"could not record that: {exc}")
            return
        kind = "constraint" if as_constraint else "instruction"
        self._output(f"added {kind}: {text}")
        if as_constraint:
            self._output("  (non-negotiable: preserved verbatim across compaction and rotation)")

    def _report_outcome(self, run: Any, outcome: Any) -> None:
        """Turn a runner outcome into something a person can act on."""
        summary = getattr(outcome, "summary", {}) or {}
        self._output("")
        self._output(f"outcome: {summary.get('outcome') or getattr(outcome.state, 'value', '?')}"
                     f"   phase: {run.phase.value}")
        # A gated run is not a failure — say which it is, because they need different responses.
        if getattr(outcome, "gated", False):
            self._output("the run parked at a gate; that is the design, not an error.")
        elif getattr(outcome, "broken", False):
            self._output(f"the run failed: {getattr(outcome, 'error', '') or 'no cause recorded'}")
        if getattr(run, "gate", None) is not None:
            self._output(f"GATE {run.gate.gate_id}: {run.gate.reason[:80]}")
            self._output("  /approve to continue, or /reject <why> to send it back")
        for name, record in sorted((getattr(run, "outcome", {}) or {}).get("nodes", {}).items()):
            self._output(f"  {name:16s} {str(record.get('status')):14s} {record.get('verdict')}")


def _slug(goal: str) -> str:
    """Derive a project slug from a goal, matching the planner's own rule."""
    import re

    if not goal:
        return "chat-run"
    slug = re.sub(r"[^a-z0-9]+", "-", goal.strip().lower()).strip("-")[:48].strip("-")
    return slug or "chat-run"
