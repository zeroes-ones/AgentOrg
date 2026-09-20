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

Routing plain text (see :func:`classify_intent`)
------------------------------------------------
A bare sentence is *not* assumed to be conversation. "add cursor pagination to /v1/items" is a job,
and silently answering it as chat — which is what this loop did — is the opposite of an org you talk
to. So a plain line is classified into one of three outcomes, and the classifier is stated rather
than felt:

- **work** — it opens with an imperative work verb *and* names something a repository holds (a path
  token, a file with an extension, or a code noun). The loop **offers to run it**; it never runs
  anything without an answer.
- **chat** — it is a question (`?`, or an interrogative/instructional opener) or a greeting. It goes
  to the model, which is what a question is for.
- **unsure** — anything else. The loop **asks** which was meant rather than guessing wrong: a wrong
  guess either spends money on a run nobody wanted or answers a job request as if it were a question.

`/chat <text>` bypasses the classifier entirely, and `/run <goal>` bypasses it the other way.

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
- **stdout is the answer, stderr is the noise.** A `/run` prints node transitions, handoff crossings
  and the gate to **stderr** (:class:`RunProgress`), because a chat whose stdout carried progress
  lines would not be pipeable. This is the same rule `cli._warn` follows.
- **A command with an implementation elsewhere calls it.** `/setup` and `/cache` drive the console's
  own handlers rather than growing a second definition of what a valid provider is; `/doctor` runs
  `cli.doctor_checks`. A second implementation is a second thing to keep correct.

Usage:
    from engine.chat import ChatSession
    ChatSession(config=cfg, gateway=gw, org=org).run()

    python3 -m engine.cli chat
    python3 -m engine.cli chat --agent Alice
    python3 -m engine.cli            # the same session, opened bare
"""

from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .providers.base import ChatRequest, Message, Role

__all__ = ["ChatSession", "ChatError", "SlashCommand", "COMMANDS",
           "RunProgress", "classify_intent",
           "INTENT_WORK", "INTENT_CHAT", "INTENT_UNSURE"]


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
    SlashCommand("/chat", "/chat <text>", "Talk to the model, even when the text reads as a job.", "_cmd_chat"),
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
    SlashCommand("/setup", "/setup [id] [kind] [url]", "Add or test a provider. Bare: a guided wizard.", "_cmd_setup"),
    SlashCommand("/onboard", "/onboard", "The first-run path: every step, and the one command for the next.", "_cmd_onboard"),
    SlashCommand("/cache", "/cache", "What the prefix cache did for this project.", "_cmd_cache"),
    SlashCommand("/doctor", "/doctor", "Check every precondition and say what failed.", "_cmd_doctor"),
    SlashCommand("/exit", "/exit", "Leave the chat.", "_cmd_exit"),
)

#: Kept deliberately small: the transcript is trimmed to the model's window, and a chat that keeps
#: every turn forever would silently grow past it.
MAX_TRANSCRIPT_TURNS = 24

#: How many events a single `/run` will narrate to stderr before it stops printing and just waits.
#: A long unattended run emits far more than a person can read, and a terminal that scrolls at
#: thousands of lines a minute is indistinguishable from one that has hung.
MAX_PROGRESS_EVENTS = 400

#: How many bytes of the trace one poll will read. A run that appends faster than this is not held up
#: — the remainder is picked up on the next tick — and one enormous line cannot be read unboundedly.
MAX_TRACE_READ_BYTES = 1 << 20

#: The three outcomes :func:`classify_intent` can return.
INTENT_WORK = "work"
INTENT_CHAT = "chat"
INTENT_UNSURE = "unsure"

#: Verbs that open a work request. Deliberately a closed list rather than "anything imperative":
#: a verb nobody listed routes to *unsure*, which asks, and asking is always safe.
_WORK_VERBS = frozenset("""
add build create implement write fix refactor rename move migrate remove delete drop
extract split merge upgrade downgrade bump pin update change replace introduce wire hook
document test cover profile optimise optimize speed cache paginate sort filter validate
sanitise sanitize harden rate-limit localize translate expose support make rework port
convert simplify dedupe extract instrument scaffold stub mock deploy package release
review audit investigate diagnose reproduce trace backfill
""".split())

#: Openers that are unambiguously conversation rather than instructions. Checked before the work
#: verbs, because "can you add …" is a question about feasibility even though it contains `add`.
_QUESTION_OPENERS = (
    "what", "why", "how", "when", "where", "which", "who", "whose", "whom",
    "is ", "are ", "was ", "were ", "do ", "does ", "did ", "can ", "could ", "should ",
    "would ", "will ", "may ", "might ", "explain", "tell me", "describe", "summarise",
    "summarize", "show me", "walk me", "help me understand", "compare", "clarify",
    "remind me", "hello", "hi ", "hey", "thanks", "thank you", "good morning", "good evening",
)

#: What a repository holds: a path segment, a filename, a code noun. One of these must be present
#: for a work-looking sentence to be routed to *work* rather than to *unsure* — "refactor the
#: report" is a job, but it names nothing this engine can open, so it is asked about rather than
#: turned into a run whose graph would be guessed.
_WORK_OBJECT_RE = re.compile(
    r"(?:^|\s)(?:/[\w./-]+|[\w.-]+/[\w./-]+|[\w-]+\.[a-z]{1,5}\b"
    r"|v\d+(?:/|$)|https?://)"
)
_CODE_NOUNS = frozenset("""
file files module modules function functions class classes method methods endpoint endpoints
api apis route routes schema schemas table tables migration migrations query queries index
indexes tests test suite config configs setting settings cli parser handler handlers
component components view views model models service services middleware decorator interface
library package packages dependency dependencies types type interface interface error errors
bug bugs crash crashes leak leaks timeout timeouts log logs build ci pipeline pipelines
""".split())


def classify_intent(text: str) -> str:
    """Decide whether a plain line is a job, a question, or neither — and say so rather than guess.

    The rule, in order, so each clause is checkable:

    1. **A question is a question.** A trailing `?`, or an interrogative/instructional opener, is
       `chat` — checked *first*, so "can you add cursor pagination to /v1/items?" stays a question
       even though it opens with a word containing "add".
    2. **A job is an imperative about something openable.** The first word must be one of
       :data:`_WORK_VERBS` (allowing a short lead-in phrase such as "please") **and** the text must
       name a path, a filename with an extension, or a code noun. That is `work`.
    3. **Everything else is `unsure`.** A greeting, a bare noun phrase, "that looks wrong" — the
       loop asks which was meant.

    The asymmetry is the point: `work` and `chat` are things the loop acts on, `unsure` is the only
    state on which it speaks to the user instead. Being wrong about the first two costs a wasted run
    or a wasted answer; being wrong about the third costs one question.
    """
    stripped = text.strip()
    if not stripped:
        return INTENT_UNSURE
    if stripped.endswith("?") or stripped.endswith("？"):
        return INTENT_CHAT
    lowered = stripped.lower()
    # Strip a politeness lead-in before looking for a verb, because "please add X" and "add X" are
    # the same instruction and only one of them starts with the verb.
    lead = re.sub(r"^(?:please|pls|can you|could you|i want you to|i need you to|go ahead and|now)\s+",
                  "", lowered)
    for opener in _QUESTION_OPENERS:
        if lowered.startswith(opener):
            return INTENT_CHAT
    words = lead.split()
    if not words:
        return INTENT_UNSURE
    if words[0] not in _WORK_VERBS:
        return INTENT_UNSURE
    if _WORK_OBJECT_RE.search(stripped):
        return INTENT_WORK
    if any(word.strip(".,;:!\"'()") in _CODE_NOUNS for word in words[1:]):
        return INTENT_WORK
    return INTENT_UNSURE


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
                 library: Any | None = None,
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
        #: The pinned library handle, when one is available. `serve.Server` takes the handle rather
        #: than the overlay source, so `/setup` and `/cache` need it to reuse the console's own
        #: implementations. Absent, those commands say so rather than inventing a second one.
        self.library = library
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
        self._warn_if_not_ready()
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
            self._plain(line)
        return 0

    def _greet(self) -> None:
        who = self._agent_name() or f"{self.provider}/{self.model}"
        self._output(f"Chatting with {who}.")
        self._output("Type a message, or /help for commands.")
        self._output("A sentence that reads like a job is offered as a run — /chat forces a reply, "
                     "/run forces a run.")

    def _warn_if_not_ready(self) -> None:
        """Say what is missing, on entry, before the person types a goal.

        The front door is bare `engine.cli`, and it opened silently even when nothing could run: a
        person typed a goal, watched it fail for a reason the session never mentioned, and had to
        discover the cause themselves. One line naming the single next step is the whole fix — the
        gate is a `SetupGate`, not a list, and the command in it is one to type rather than a
        diagnosis to act on.

        Only on entry, and only when blocked: a session printed per turn would be a nag, and a ready
        engine must not greet anyone with a warning about setup.

        The gate is evaluated **without probing**. A greeting must not open a socket — it would make
        `engine.cli` block on a slow provider before the prompt appeared — so the one input that costs
        a probe (the model's window) is read from the configuration alone here. That can miss a
        window the catalogue would resolve, so the line says the *step* rather than declaring the gate
        closed, and `onboard` and `doctor` remain the probing surfaces that answer it exactly.
        """
        try:
            from .onboarding import first_run_hint, readiness

            gate = readiness(self.config, getattr(self.gateway, "providers", None),
                             self.workspace, probe=False, check_window=False,
                             catalog=self.catalog)
        except Exception:  # noqa: BLE001 - a greeting must never be what stops a session opening
            return
        hint = first_run_hint(gate)
        if not hint:
            return
        self._output(hint)
        self._output("  the whole path: /onboard")

    def _plain(self, line: str) -> None:
        """Route one line of plain text through the classifier.

        The three outcomes are handled differently on purpose. `work` *offers* — it never spends
        without an answer, because a run is real money and real edits. `unsure` *asks* — guessing
        costs either a wasted run or a job answered as a question. Only `chat`, the one outcome the
        classifier is confident about, is acted on without confirmation.
        """
        intent = classify_intent(line)
        if intent == INTENT_CHAT:
            self._say(line)
            return
        if intent == INTENT_WORK:
            self._offer_run(line)
            return
        self._ask_intent(line)

    def _offer_run(self, line: str) -> None:
        """Confirm before turning a sentence into a run.

        Nothing has been planned or spent at this point, which is what makes declining free. The
        offer names the goal it would use so "yes" is informed rather than a leap of faith.
        """
        if self.orchestrator is None:
            self._output(f"That reads like a job: {line!r}")
            self._output("No orchestrator is loaded here, so I cannot run it — /chat <text> will "
                         "talk it through instead.")
            return
        self._output(f"That reads like a job: {line!r}")
        self._output("Run it?  [y]es to hand it to the org, [c]hat to just discuss it, "
                     "[n]o to drop it")
        try:
            answer = self._input("run? › ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            self._output("(no answer — nothing run)")
            return
        if answer in ("y", "yes"):
            # Routed through the one `/run` implementation so the offer and the command cannot
            # drift into two behaviours.
            self._cmd_run(line)
        elif answer in ("c", "chat", "d", "discuss"):
            self._say(line)
        else:
            self._output("(dropped — nothing run)")

    def _ask_intent(self, line: str) -> None:
        """Say that the line is ambiguous and let the user say which it was.

        Stated as a question with the two concrete commands rather than as a hint, because the
        resolution for each is a thing the user types next.
        """
        self._output(f"Not sure whether {line!r} is work or a question.")
        self._output("  /run <goal>  hand it to the org")
        self._output("  /chat <text> just answer it")

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

    def _cmd_chat(self, argument: str) -> None:
        """Force a conversational turn, whatever the classifier would have said.

        This is the escape hatch the routing owes the user: a sentence that *looks* like a job but was
        meant as a question must be answerable without rewording it.
        """
        if not argument:
            self._output("usage: /chat <text>")
            return
        self._say(argument)

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

    # ── setup, cache and doctor: the three gaps that were GUI-only ──────────

    def _console(self) -> Any:
        """A `serve.Server` over this session's workspace, or None when one cannot be built.

        Built rather than re-implemented: adding a provider is a validation, a normalisation, a
        merge-write and a reload, and `serve` already owns every one of those rules. A second copy
        here would be a second definition of what a valid provider is, and the first time the two
        disagreed the CLI would accept an entry the app refuses (or the reverse).

        The console's events are swallowed: `Server.emit` writes the protocol to stdout, and stdout
        here belongs to the chat's answers.
        """
        if self.workspace is None or self.library is None:
            return None
        try:
            from .serve import Server

            return Server(config=self.config, library=self.library, workspace=self.workspace,
                          stdout=_NullStream())
        except Exception as exc:  # noqa: BLE001 - no console is a degraded setup path, not a crash
            self._output(f"[setup] the console handler is unavailable here: {exc}")
            return None

    def _cmd_cache(self, argument: str) -> None:
        """Report the durable prefix-cache store for this workspace.

        Two sources, deliberately joined rather than picked between. The **store** is what survives a
        restart: which prefixes were pinned, how many shapes changed, what was saved. The **console's**
        live totals are what this process has seen cross the bus. Either alone answers half the
        question — a store with nothing live has no accumulation, and live totals with no store forget
        the moment the process exits.
        """
        if self.workspace is None:
            self._output("no workspace is attached, so there is no cache to report")
            return
        try:
            from .cachestore import CacheStore

            summary = CacheStore.for_workspace(self.workspace).summary()
        except Exception as exc:  # noqa: BLE001 - an unreadable store must not end the session
            self._output(f"could not read the cache store: {exc}")
            return
        self._output(f"cache store: {summary.get('directory')}")
        self._output(f"  prefixes : {summary.get('prefixes')}   "
                     f"shapes: {summary.get('shapes')}   "
                     f"({summary.get('prefix_changes')} prefix change(s))")
        self._output(f"  savings  : {summary.get('savings_records')} record(s)")
        hit, miss = summary.get("cache_hit_tokens"), summary.get("cache_miss_tokens")
        rate = summary.get("cache_hit_rate")
        self._output("  tokens   : "
                     + ("unreported by any provider" if not summary.get("cache_reported")
                        else f"{hit or 0} hit / {miss or 0} miss"
                             + (f"  ({rate:.1%} over {summary.get('rate_records')} record(s))"
                                if rate is not None else "  (rate not computable)")))
        saving = summary.get("cache_saving_usd")
        self._output(f"  saved    : {'unknown' if saving is None else f'${saving:.6f}'}")
        reasons = summary.get("change_reasons") or {}
        if reasons:
            self._output("  why prefixes changed:")
            for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:6]:
                self._output(f"    {count:4d}  {reason}")
        if summary.get("load_error"):
            self._output(f"  [warning] {summary['load_error']}")
        console = self._console()
        if console is not None:
            live = console._cmd_cache({})
            self._output(f"  this process: {live['turns']} model turn(s), "
                         + ("nothing reported about caching"
                            if not live["cache_reported"]
                            else f"{live['cache_hit_tokens'] or 0} hit token(s)"))

    def _cmd_doctor(self, argument: str) -> None:
        """Run the same checks `engine.cli doctor` runs, in the session.

        Imported from `cli` rather than re-run through a subprocess, so `/doctor` cannot disagree with
        the command scripts and the docs already depend on. The config *path* is passed, not the loaded
        object, because `doctor_checks` re-loads from disk — checking the file that is there now rather
        than the one this session started from. The first failing check is what a person came for, so
        the failures are listed before the pass count.
        """
        try:
            from .cli import doctor_checks

            checks = doctor_checks(getattr(self.config, "path", None),
                                   getattr(getattr(self.library, "files", None), "root", None))
        except Exception as exc:  # noqa: BLE001 - a broken check must not end the session
            self._output(f"doctor could not run: {exc}")
            return
        failures = [check for check in checks if not check["ok"]]
        for check in checks:
            mark = "OK  " if check["ok"] else "FAIL"
            self._output(f"{mark} {check['check']:<16s} {check['detail']}")
            for warning in check.get("warnings") or []:
                self._output(f"     warning: {warning}")
            for entry in check.get("skipped") or []:
                self._output(f"     skipped: {entry}")
        self._output("doctor: all checks passed" if not failures
                     else f"doctor: {len(failures)} check(s) failed — see FAIL above")
        if failures:
            self._output("next: /setup to add or fix a provider, then /doctor again")

    def _cmd_onboard(self, argument: str) -> None:
        """Show the first-run path in the session, from the engine's own answer.

        `/doctor` says *what is wrong*; `/onboard` says *what to do next*. The rendering is imported
        from the CLI's own module rather than reimplemented, so the command and the session cannot
        describe one path two ways — and the *gate* is `onboarding.readiness`, the same gatherer the
        command uses, so the two surfaces cannot report different steps either.

        Probing here, unlike the entry line: a person typed `/onboard`, so the network round trip is
        what they asked for and the step it may reveal (a model with no known window) is one the
        configuration alone cannot see.
        """
        try:
            from .onboarding import readiness, render_journey

            gate = readiness(self.config, getattr(self.gateway, "providers", None),
                             self.workspace, catalog=self.catalog)
            lines = render_journey(gate)
        except Exception as exc:  # noqa: BLE001 - a broken gate must not end the session
            self._output(f"onboard could not run: {exc}")
            return
        for line in lines:
            self._output(line)
        if gate.is_blocking:
            self._output("  (the same answer as `python3 -m engine.cli onboard`)")

    def _cmd_setup(self, argument: str) -> None:
        """Add or test a provider, or walk a guided wizard when called with no argument.

        The wizard exists because there was no CLI path to add a provider at all: a user had to
        hand-edit `credentials.json`, which is the one file where a mistake costs a key or a whole
        config. Both the argument form and the wizard end in the same console handler.
        """
        console = self._console()
        if console is None:
            self._output("no console handler is available, so there is nowhere to save a provider")
            return
        parts = argument.split()
        if parts:
            self._setup_from_arguments(console, parts)
            return
        self._setup_wizard(console)

    def _setup_from_arguments(self, console: Any, parts: list[str]) -> None:
        """`/setup <id> <kind> <base-url> [--key-env NAME | --key VALUE]`.

        Everything is tested before it is written, and a failed test is a *result* the user is shown
        rather than a refusal — an endpoint may be down without the values being wrong, and the user
        is the one who knows which.
        """
        if len(parts) < 3:
            self._output("usage: /setup <provider-id> <kind> <base-url> [--key-env NAME | --key VALUE]")
            self._output("  kinds: openai, anthropic, ollama   (bare /setup walks you through it)")
            return
        payload: dict[str, Any] = {"provider_id": parts[0], "kind": parts[1], "base_url": parts[2]}
        rest = parts[3:]
        index = 0
        while index < len(rest):
            flag = rest[index]
            value = rest[index + 1] if index + 1 < len(rest) else ""
            if flag == "--key-env":
                payload["api_key_env"] = value
            elif flag == "--key":
                payload["api_key"] = value
            elif flag == "--api-version":
                payload["api_version"] = value
            else:
                self._output(f"unknown option {flag!r} — expected --key-env, --key or --api-version")
                return
            index += 2
        self._setup_commit(console, payload)

    def _setup_wizard(self, console: Any) -> None:
        """Ask for one field at a time, defaulting to nothing rather than to a guess.

        No default provider id or URL is offered: a wrong default that is accepted by pressing return
        is exactly how a config ends up pointing at an endpoint nobody chose.
        """
        self._output("Adding a provider. Enter to abandon at any prompt.")
        self._output("  kinds: openai (OpenAI-compatible), anthropic, ollama")
        payload = self._setup_ask({"provider_id": "an id, e.g. groq",
                                   "kind": "openai, anthropic or ollama",
                                   "base_url": "the API base, e.g. https://api.groq.com/openai/v1"})
        if payload is None:
            self._output("(nothing added)")
            return
        key_env = self._prompt("environment variable holding the key (recommended, blank for none): ")
        if key_env:
            payload["api_key_env"] = key_env
        else:
            key = self._prompt("API key (blank for a local endpoint with no key): ")
            if key:
                payload["api_key"] = key
        self._setup_commit(console, payload)

    def _setup_ask(self, fields: dict[str, str]) -> dict[str, Any] | None:
        """Collect the required fields, abandoning the whole wizard on any empty answer."""
        payload: dict[str, Any] = {}
        for name, description in fields.items():
            answer = self._prompt(f"{description}: ")
            if not answer:
                return None
            payload[name] = answer
        return payload

    def _setup_commit(self, console: Any, payload: dict[str, Any]) -> None:
        """Test the entry, then save it through the console's own handler.

        The test is not a gate. It is reported, and the user chooses: an unreachable endpoint is a
        reason to check a URL, not a reason to refuse to keep correct values.
        """
        self._output(f"testing {payload['provider_id']} … (this contacts the endpoint)")
        try:
            probe = console._cmd_provider_test(dict(payload))
        except Exception as exc:  # noqa: BLE001 - a refused test is reported, not raised
            # Reported, then the save is still attempted below: the probe refuses what it cannot
            # *build*, and the save is where the authoritative refusal for a malformed entry lives.
            # Stopping here would leave the user with "the test could not run" and no reason why.
            self._output(f"  not testable: {exc}")
            probe = {}
        else:
            if probe.get("ok"):
                self._output(f"  reachable — {probe.get('model_count')} model(s)")
            else:
                self._output(f"  not reachable ({probe.get('reason') or 'no reason given'})")
            if probe.get("note"):
                self._output(f"  note: {probe['note']}")
        try:
            result = console._cmd_provider_add(dict(payload))
        except Exception as exc:  # noqa: BLE001 - a bad entry is the user's to fix, not a crash
            self._output(f"could not save that provider: {exc}")
            self._output("  nothing was written; fix the entry and run /setup again.")
            return
        self._output(f"saved {result['provider_id']} to {result['saved']}")
        if probe and not probe.get("ok"):
            # An unreachable endpoint is saved but not silent: the values may be right and the host
            # down, and saying so is the difference between a user re-checking a correct key and one
            # who knows what happened. This is the same distinction `provider_test` makes.
            self._output(f"  the endpoint did not answer ({probe.get('reason') or 'no reason'}), but "
                         "the entry is saved — fix the URL or bring the host up, then /doctor.")
        if result.get("note"):
            self._output(f"  note: {result['note']}")
        self._output("  /model with no argument lists every model that can now be bound.")

    def _prompt(self, label: str) -> str:
        """One wizard prompt. An interrupt abandons the wizard rather than ending the session."""
        try:
            return self._input(label).strip()
        except (EOFError, KeyboardInterrupt):
            self._output("\n(abandoned)")
            return ""

    # ── the org, from the chat ──────────────────────────────────────────────

    def _cmd_run(self, argument: str) -> None:
        """Plan, approve and execute a goal, narrating the run while it happens.

        `execute` blocks for as long as the graph takes, so the *only* thing standing between the user
        and an unbounded silent wait was this function saying nothing. :class:`RunProgress` taps the
        orchestrator's own bus — the same mechanism `serve._forward_bus` uses — so the narration is
        made of the engine's real events rather than a parallel story invented here.
        """
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
        # Said *before* the blocking call rather than by a spinner: an execution that takes minutes
        # must announce itself first, which is the whole complaint about this path.
        self._output("executing — node transitions below; ctrl-C returns to the prompt.")
        with RunProgress(self.orchestrator, run=run):
            try:
                self.orchestrator.approve(run)
                outcome = self.orchestrator.execute(run)
            except Exception as exc:  # noqa: BLE001
                self._output(f"execution failed: {exc}")
                return
        self._report_outcome(run, outcome)

    def _current_run(self) -> Any | None:
        """The run a gate decision should act on, reloaded from its checkpoint.

        Reloaded rather than reused because a gate decision must act on the persisted run — the
        in-memory object goes stale the moment the runner writes its own checkpoint.

        **Found by a real run.** This used to require `self.running_slug`, which only `/run` sets, so
        a gate could be resolved *only* in the process that had started it. A run parked at a gate and
        the session then restarted — the ordinary case, since every `engine.cli` invocation is a new
        process, and the documented workflow is "run, look, decide" — gave `no run is loaded to decide
        on` for a run sitting on disk with a gate wide open. The slug is therefore *discovered* from
        the workspace when the session did not start one: `status` already names the run, so a command
        whose whole job is to act on it must not be the one that cannot find it.
        """
        if self.orchestrator is None:
            return None
        slug = self.running_slug
        if not slug:
            slug = self._discovered_slug()
            if not slug:
                return None
        try:
            return self.orchestrator.load(slug)
        except Exception:  # noqa: BLE001 - a chat command must not raise
            return None

    def _discovered_slug(self) -> str | None:
        """The slug of the run this workspace holds, when the session did not start one.

        Reads the checkpoint's own record rather than guessing: `orchestrator.load(None)` resolves a
        single-run workspace by the folder, and a workspace with no run returns None — which is the
        honest answer for `/approve` to report.
        """
        try:
            run = self.orchestrator.load(None)
        except Exception:  # noqa: BLE001 - discovery must not raise
            return None
        return str(getattr(run, "slug", "") or "") or None

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
    if not goal:
        return "chat-run"
    slug = re.sub(r"[^a-z0-9]+", "-", goal.strip().lower()).strip("-")[:48].strip("-")
    return slug or "chat-run"


class _NullStream:
    """A write sink that discards everything, for a `Server` whose protocol stream nobody reads."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


#: Events a person watching a run wants, and the one-line shape each takes. Deliberately a whitelist
#: rather than "print every event": a run emits `llm.request`/`llm.response` per model call and an
#: `agent.log` per tool step, which at a terminal is a scroll that buries the transitions the user
#: came to see. A transition list that includes the *gate* is the whole point of the display.
_PROGRESS_EVENTS: dict[str, str] = {
    "run.start": "run started",
    "run.end": "run ended",
    "node.enter": "→ node",
    "node.exit": "← node",
    "phase.enter": "phase",
    "phase.exit": "phase out",
    "handoff.proposed": "handoff proposed",
    "handoff.accepted": "handoff accepted",
    "handoff.rejected": "handoff REJECTED",
    "handoff.fulfilled": "handoff fulfilled",
    "handoff.breached": "handoff BREACHED",
    "human.gate": "GATE",
    "human.decision": "decision",
    "review.rejected": "review rejected",
    "review.approved": "review approved",
    "cost.ceiling": "COST CEILING",
    "budget.burn": "budget",
    "error": "ERROR",
    "run.aborted": "run aborted",
    "watchdog.stall": "WATCHDOG: stalled",
}

#: A node's own words for why it stopped, which is the most useful thing in its exit payload.
_NODE_DETAIL_KEYS = ("summary", "verdict", "status", "reason", "why", "gate_id", "from_node",
                     "to_node", "skills", "kind")


class RunProgress:
    """Narrate a running graph to stderr, from the run's own events.

    WHY THIS EXISTS
    ---------------
    `/run` called `orchestrator.execute`, which blocks until the graph finishes and emitted nothing
    while it did. That is the one place in the whole CLI where a wait is unbounded, and a wait with no
    output is indistinguishable from a hang — so the user's only available move was to kill a run that
    may have been working perfectly.

    WHY IT NEEDS **BOTH** A BUS SUBSCRIPTION AND A TRACE FOLLOWER
    ------------------------------------------------------------
    Because the two halves of a run are produced in two different processes, and only one of them can
    reach us live:

    - The **orchestrator** runs in this process. Its bus carries the run's lifecycle — `run.start`,
      `human.gate`, `run.end`, the goal events — and a subscriber sees them the moment they happen.
      This is exactly the mechanism `serve._forward_bus` uses.
    - The **nodes** run in the generated executor subprocess (`RunnerHost` spawns it). Node
      transitions, handoff crossings and review verdicts are emitted on *that* process's bus, which we
      have no handle on — a subscription here would simply never see them. They do reach us, but only
      asynchronously, through the `trace.jsonl` both processes append to.

    So the live half is a subscriber and the durable half is a bounded tail of the shared trace,
    started when the run starts and drained when it ends. Claiming node events came "from the bus"
    would be false; :meth:`format_event` labels them `node` vs `run` so the display never pretends to
    a liveness it does not have.

    WHY STDERR
    ----------
    stdout is the answer. `cli._warn` states the rule and `--json` depends on it; a chat's stdout is
    what a caller pipes. Progress lines are diagnostics, so they go to stderr and stdout stays clean.

    The subscriber **never raises and never blocks**: `EventBus._deliver` catches a subscriber's
    exception and *disables it permanently*, so one bad format string would silently kill the
    narration for the rest of the run. Everything here is inside a `try`, the trace read is bounded
    per poll, and the write is guarded against a closed or full stderr — a diagnostic that can break
    the code path it is describing is worse than no diagnostic.
    """

    #: How often the trace is polled. Short enough that a node transition feels immediate to someone
    #: watching, long enough that a run with many model calls does not spend its time re-reading a
    #: file. The read itself is incremental, so this cost is a `stat` per tick when nothing happened.
    POLL_S = 0.25

    def __init__(self, orchestrator: Any, *, limit: int = MAX_PROGRESS_EVENTS,
                 quiet: bool = False, run: Any = None) -> None:
        self.orchestrator = orchestrator
        self.limit = int(limit)
        self.quiet = quiet
        #: The run being narrated, when the caller has it. Read only on exit, to report where the run
        #: ended — the gate it parked at is *derived* by the orchestrator after the child exits rather
        #: than emitted as an event, so it is not something a subscriber can ever see.
        self.run = run
        self.lines: list[str] = []
        self.dropped = 0
        self._subscription: Any = None
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: Byte offset already consumed from the trace, so each poll reads only new bytes.
        self._trace_offset = 0
        self._trace_path: Path | None = self._resolve_trace()
        #: The orchestrator's own bus id. Every event *this* process emits is stamped with it, and
        #: every event the executing subprocess emits is stamped with the run id instead — which is
        #: what makes "is this line something I already narrated live?" a one-field question rather
        #: than a guess. Both processes append to the same trace, so without this the lifecycle events
        #: would appear twice: once from the subscription and once from the file.
        bus = getattr(orchestrator, "bus", None)
        self._own_run_id: str = str(getattr(bus, "run_id", "") or "")

    def __enter__(self) -> "RunProgress":
        bus = getattr(self.orchestrator, "bus", None)
        subscribe = getattr(bus, "subscribe", None)
        if subscribe is None:
            # Stated rather than passed over: a run with no bus cannot be narrated live, and a user
            # who knows that is not left wondering why nothing appeared.
            self._write("(this run has no event bus; only the trace is followed)")
        else:
            try:
                self._subscription = subscribe(self._on_event)
            except Exception as exc:  # noqa: BLE001 - an untappable bus is a degraded display
                self._write(f"(cannot subscribe to the run's events: {exc})")
        self._start_follower()
        return self

    def __exit__(self, *exc: object) -> None:
        # Drained *before* unsubscribing, so events written between the run finishing and this line
        # are still shown. The follower is stopped first so it cannot interleave with the final read.
        self._stop_follower()
        self._drain_trace()
        bus = getattr(self.orchestrator, "bus", None)
        unsubscribe = getattr(bus, "unsubscribe", None)
        if unsubscribe is not None and self._subscription is not None:
            try:
                unsubscribe(self._subscription)
            except Exception:  # noqa: BLE001 - leaving a subscriber behind must not raise
                pass
        self._report_terminal()
        if self.dropped:
            self._write_note(f"… {self.dropped} further event(s) not shown")
        return None

    def _report_terminal(self) -> None:
        """State where the run ended and what it is waiting on, when a run was supplied.

        Necessary because the **gate is not an event on this path**. `orchestrator._settle` *derives*
        it after the child process exits (`_detect_gate` over the checkpoint) rather than emitting
        `human.gate`, so a subscriber never sees it and the trace never contains it. Reading it off the
        run is therefore the only way the display can end with the one line a person needs: what the
        run is blocked on and how to release it.
        """
        if self.run is None:
            return
        phase = getattr(getattr(self.run, "phase", None), "value", "") or ""
        if phase:
            self._write(f"[run] finished: phase {phase}")
        gate = getattr(self.run, "gate", None)
        if gate is not None:
            self._write(f"[run] GATE {gate.gate_id} ({gate.kind}): {str(gate.reason)[:100]}"
                        .rstrip())
            self._write("[run]   /approve to continue, or /reject <why> to send it back")
        reason = str(getattr(self.run, "stop_reason", "") or "")
        if reason:
            self._write(f"[run] stopped: {reason[:140]}")

    # ── the trace follower ──────────────────────────────────────────────────

    def _resolve_trace(self) -> Path | None:
        """The trace both processes append to, from whatever the orchestrator knows.

        Read off the workspace the orchestrator holds rather than taken as a parameter, because the
        path is already a property of the workspace and a second caller-supplied copy could disagree
        with the one the run actually writes.
        """
        workspace = getattr(self.orchestrator, "workspace", None)
        path = getattr(workspace, "trace_path", None)
        return Path(path) if path is not None else None

    def _start_follower(self) -> None:
        """Tail the trace on a daemon thread for the life of the run.

        A thread rather than a poll interleaved elsewhere: `execute` blocks, so there is no other
        place to run the read, and a daemon thread cannot outlive the process if the run is killed.
        A run with no trace path simply gets no follower — the live bus still narrates lifecycle.
        """
        if self._trace_path is None:
            return
        # Start at the current end, not at byte zero: an earlier run's trace is history, and replaying
        # it would attribute yesterday's nodes to this run's progress.
        try:
            self._trace_offset = self._trace_path.stat().st_size if self._trace_path.is_file() else 0
        except OSError:
            self._trace_offset = 0
        self._thread = threading.Thread(target=self._follow, daemon=True, name="chat-run-progress")
        self._thread.start()

    def _stop_follower(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def _follow(self) -> None:
        """Poll the trace until stopped. Every poll is cheap and cannot raise."""
        while not self._stop.wait(self.POLL_S):
            self._drain_trace()

    def _drain_trace(self) -> None:
        """Read whatever has been appended since the last read and narrate it.

        Incremental by byte offset rather than re-reading the file: an unattended run appends a line
        per model call, so a whole-file read per poll would be quadratic in the run's length. A
        partially-written last line is left for the next poll rather than parsed — the same tolerance
        `load_trace` keeps, for the same reason.
        """
        path = self._trace_path
        if path is None:
            return
        try:
            if not path.is_file():
                return
            size = path.stat().st_size
            if size <= self._trace_offset:
                return
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._trace_offset)
                blob = handle.read(MAX_TRACE_READ_BYTES)
                consumed = handle.tell()
            # Only advance past the last complete line, so a torn write is retried rather than lost
            # and never parsed as a truncated JSON document.
            cut = blob.rfind("\n")
            if cut < 0:
                return
            complete, rest = blob[:cut], blob[cut + 1:]
            self._trace_offset = consumed - len(rest.encode("utf-8"))
            for line in complete.splitlines():
                self._narrate_trace_line(line)
        except (OSError, ValueError):
            # A trace we cannot read is a missing display, not a failed run.
            return

    def _narrate_trace_line(self, line: str) -> None:
        """Decode and narrate one trace line, ignoring anything that is not a fresh wanted event.

        Two filters, both stated rather than implied:

        - **A wanted event type.** The whitelist, so a line per model call does not bury the
          transitions.
        - **An event this process did not itself emit.** The orchestrator's own bus stamps its events
          with its `run_id`, and the executing subprocess stamps its own with the run's id; both land
          in the same file. Without this filter every lifecycle event would be narrated twice — once
          live from the subscription, once from the file — which is exactly the double-report a
          second reading of a run is prone to.
        """
        if not line.strip():
            return
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(record, dict):
            return
        kind = str(record.get("type") or "")
        label = _PROGRESS_EVENTS.get(kind)
        if label is None:
            return
        if self._own_run_id and str(record.get("run_id") or "") == self._own_run_id:
            return
        node = str(record.get("node_id") or "")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        self._write(self._format(label, node, payload, source="node"))

    # ── the live half ───────────────────────────────────────────────────────

    def _on_event(self, event: Any) -> None:
        """Format and write one live event. Never raises — see the class docstring."""
        try:
            line = self.format_event(event)
        except Exception:  # noqa: BLE001 - a formatting bug must not disable the subscriber
            return
        if line:
            self._write(line)

    def format_event(self, event: Any) -> str:
        """The one-line rendering of a bus event, or `''` for an event nobody asked to see."""
        kind = getattr(event, "type_value", "") or str(getattr(event, "type", ""))
        label = _PROGRESS_EVENTS.get(kind)
        if label is None:
            return ""
        node = str(getattr(event, "node_id", "") or "")
        payload = getattr(event, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        return self._format(label, node, payload, source="run")

    def _format(self, label: str, node: str, payload: dict[str, Any], *, source: str) -> str:
        parts = [f"[{source}] {label}"]
        if node:
            parts.append(node)
        detail = self._detail(payload, node=node)
        if detail:
            parts.append(detail)
        return "  ".join(parts)

    def _detail(self, payload: dict[str, Any], *, node: str) -> str:
        """The payload's own words, in preference order, trimmed to one line."""
        for key in _NODE_DETAIL_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip() and value.strip() != node:
                return value.strip()[:110]
        for key in ("outcome", "state", "phase", "exit_code", "silence_s", "saturation"):
            value = payload.get(key)
            if value not in (None, "", []):
                return f"{key}={value}"
        return ""

    def _write(self, line: str) -> None:
        """Append and print to stderr, bounded and never raising."""
        with self._lock:
            if len(self.lines) >= self.limit:
                self.dropped += 1
                return
            self.lines.append(line)
        if self.quiet:
            return
        try:
            print(line, file=sys.stderr, flush=True)
        except (BrokenPipeError, ValueError, OSError):
            # A closed stderr is not a reason to abandon a run that is still working.
            pass

    def _write_note(self, line: str) -> None:
        """Write a line that reports on the display itself, outside the event budget.

        The budget bounds *events*, and the "N further events not shown" note is not one: charging it
        to the same count meant the note consumed the last slot and pushed the real drop total up by
        one, so a bounded run reported one more dropped event than it dropped.
        """
        with self._lock:
            self.lines.append(line)
        if self.quiet:
            return
        try:
            print(line, file=sys.stderr, flush=True)
        except (BrokenPipeError, ValueError, OSError):
            pass

    def stderr_text(self) -> str:
        """What was printed to stderr, for a caller (or a test) that needs the rendered lines.

        The lines are kept regardless, so a caller does not have to capture the process's own stderr
        to know what the user was shown — which is the same reason `_stream_write` keeps its output.
        """
        with self._lock:
            return "\n".join(self.lines)
