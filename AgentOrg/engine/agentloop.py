#!/usr/bin/env python3
"""agentloop.py — the loop that makes an agent work *on a project*, not just answer a prompt.

WHY THIS EXISTS
---------------
Every node used to be one model call: a prompt in, a trailer out. That is a *handoff*, and it works
when each node inherits exactly what it needs from the node before it. It falls apart the moment the
work requires looking at the code — "add a field to User.swift" cannot be done by a model that has
never seen `User.swift`, and no amount of prompt engineering fixes that.

This is the loop the reference harnesses use, and it is deliberately small: the model is given tools,
it calls one, the result goes back, and it repeats until it stops calling tools or hits a bound. What
matters is not the loop's shape but the three things wrapped around it:

- **A bound.** `max_steps` ends a loop that is not converging, and the ending is reported. An
  ungoverned loop is an unbounded bill, which is the one failure this project treats as unacceptable
  everywhere else.
- **A budget check before each step.** Not after: the point of a ceiling is that it stops the spend
  rather than reporting it.
- **An honest stop.** A loop that ran out of steps is `needs_review` with the reason, never `done`. A
  truncated investigation reported as complete is exactly the confident-wrong-output failure the eval
  suite exists to catch.

DESIGN
------
- **The registry is injected, not built here.** This module owns *when* to call and *when to stop*;
  `engine/tools.py` owns *what* may be called and under whose permission. Keeping them apart is what
  lets the permission model be tested without a model.
- **Tool results are fed back verbatim.** A refusal reaches the model as text, because a refusal it
  cannot read is one it will retry — the loop would then burn its whole bound on the same denied call.
- **Parallel-safe calls are not batched here.** Sequential dispatch is the honest default: a read
  followed by a write to the same file must not race, and nothing in the tool set declares itself
  independent yet.
- **Text between tool calls is kept.** A model's narration before a call is context for the next turn,
  and discarding it makes the loop amnesiac within a single node.

Usage:
    loop = AgentLoop(complete=gateway_complete, tools=registry, max_steps=12)
    outcome = loop.run(system=prompt.system, user=prompt.text)
    outcome.text         # the final assistant message
    outcome.steps        # how many model calls it took
    outcome.exhausted    # True when the bound ended it
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

__all__ = ["AgentLoop", "LoopOutcome", "LoopError", "DEFAULT_MAX_STEPS"]

#: How many model calls one node may make when the caller does not say. Generous enough for a real
#: investigation (read, search, read, write, verify) and small enough that a non-converging loop is
#: caught inside a useful budget.
DEFAULT_MAX_STEPS = 12


class LoopError(RuntimeError):
    """A loop that could not run, named so the caller can act on it."""


@dataclass
class LoopOutcome:
    """What a tool-using node produced."""

    #: The final assistant text — the trailer is parsed out of this by the caller, unchanged.
    text: str = ""
    #: Every model call made, counted so a caller can see what the loop cost.
    steps: int = 0
    #: True when the step bound ended the loop rather than the model finishing. A run that is
    #: truncated must not be reported as complete.
    exhausted: bool = False
    #: Every tool call made, in order, for the trace and the artifact record.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: Files read or written, so the node's artifacts can include what it touched.
    paths: list[str] = field(default_factory=list)
    #: Tokens summed across every step — the honest cost of a loop, not just its last call.
    tokens_in: int = 0
    tokens_out: int = 0
    #: Why the loop stopped, in words, for the gate or the log.
    stop_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps, "exhausted": self.exhausted, "stop_reason": self.stop_reason,
            "tool_calls": list(self.tool_calls), "paths": sorted(set(self.paths)),
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
        }


class AgentLoop:
    """Drive a model through tool calls until it stops or the bound is reached.

    Parameters
    ----------
    complete:
        `complete(request) -> response`, where the response carries `.text` and `.tool_calls`. The
        gateway's own `complete` matches this, so the loop needs no knowledge of providers.
    tools:
        A `ToolRegistry`. Only its `specs()` and `call()` are used, so a test can pass a stub.
    max_steps:
        The hard bound on model calls. Reaching it is reported, never hidden.
    can_continue:
        Optional check before each step — the budget gate. Returning False ends the loop with a
        reason, which is how a ceiling stops the spend rather than reporting it.
    on_step:
        Optional observer, called with each step's summary, for the trace.
    """

    def __init__(self, *, complete: Callable[[Any], Any], tools: Any,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 can_continue: Callable[[], tuple[bool, str]] | None = None,
                 on_step: Callable[[dict[str, Any]], None] | None = None,
                 max_output_tokens: int = 4096) -> None:
        self.complete = complete
        self.tools = tools
        self.max_steps = max(1, int(max_steps))
        self.can_continue = can_continue
        self.on_step = on_step
        self.max_output_tokens = max_output_tokens

    def run(self, *, system: str, user: str,
            history: Iterable[Any] = ()) -> LoopOutcome:
        """Run the loop. Returns the final text and what it cost.

        The message list is built once and appended to, so each turn carries the full conversation —
        which is both what a tool-using model needs and what keeps the provider's prefix cacheable,
        since the earlier messages are never rewritten.
        """
        from .providers.base import ChatRequest, ContentBlock, Message, Role

        outcome = LoopOutcome()
        messages: list[Message] = list(history)
        messages.append(Message.text_message(Role.USER, user))
        specs = self.tools.specs() if hasattr(self.tools, "specs") else []

        for step in range(1, self.max_steps + 1):
            if self.can_continue is not None:
                allowed, reason = self.can_continue()
                if not allowed:
                    outcome.stop_reason = reason or "the budget ceiling was reached"
                    outcome.exhausted = True
                    outcome.text = outcome.text or (
                        f"[the run stopped before this node finished: {outcome.stop_reason}]")
                    return outcome

            request = ChatRequest(model="", messages=list(messages), system=system,
                                  tools=list(specs), max_tokens=self.max_output_tokens)
            response = self.complete(request)
            outcome.steps = step
            usage = getattr(response, "usage", None)
            outcome.tokens_in += int(getattr(usage, "prompt_tokens", 0) or 0)
            outcome.tokens_out += int(getattr(usage, "completion_tokens", 0) or 0)

            text = getattr(response, "text", "") or ""
            calls = list(getattr(response, "tool_calls", None) or [])
            outcome.text = text or outcome.text

            if self.on_step is not None:
                try:
                    self.on_step({"step": step, "text_chars": len(text),
                                  "tool_calls": [c.name for c in calls]})
                except Exception:  # noqa: BLE001 - an observer must not break the loop
                    pass

            if not calls:
                # No tool call: the model is done. The bound was not the reason, and saying so is
                # what lets the caller treat this as a real completion.
                outcome.stop_reason = "the model finished"
                return outcome

            # Record the assistant turn *with* its tool calls, then each result, so the next request
            # carries the exchange rather than only the last message.
            messages.append(self._assistant_turn(text, calls))
            for call in calls:
                result = self.tools.call(getattr(call, "name", ""),
                                         getattr(call, "arguments", None) or {})
                outcome.tool_calls.append({
                    "step": step, "tool": getattr(call, "name", ""),
                    "arguments": getattr(call, "arguments", {}) or {},
                    "ok": bool(getattr(result, "ok", False)),
                    "result_chars": len(getattr(result, "text", "") or ""),
                })
                outcome.paths.extend(getattr(result, "paths", []) or [])
                messages.append(self._tool_result(call, result))

        # The bound ended it. Reported rather than hidden: a node that ran out of steps has not
        # finished, and calling that done is the failure the evidence contract exists to prevent.
        outcome.exhausted = True
        outcome.stop_reason = f"reached the {self.max_steps}-step bound without the model finishing"
        return outcome

    # ── message construction ────────────────────────────────────────────────

    def _assistant_turn(self, text: str, calls: list[Any]) -> Any:
        """The assistant's turn, carrying both its narration and its tool calls."""
        from .providers.base import ContentBlock, Message, Role, ToolCall

        blocks: list[ContentBlock] = []
        if text:
            blocks.append(ContentBlock(type="text", text=text))
        tool_calls: list[Any] = []
        for raw in calls:
            name = getattr(raw, "name", "")
            arguments = getattr(raw, "arguments", {}) or {}
            call_id = getattr(raw, "id", "") or f"call_{name}_{len(tool_calls)}"
            blocks.append(ContentBlock(
                type="tool_use",
                tool_call=ToolCall(id=call_id, name=name, arguments=arguments)))
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
        return Message(role=Role.ASSISTANT, content=blocks, tool_calls=tool_calls)

    def _tool_result(self, call: Any, result: Any) -> Any:
        """One tool result, as a message the model will read next turn.

        The text is passed through unchanged — including a refusal. A refusal the model cannot read is
        one it will repeat, and the loop would then spend its whole bound on the same denied call.
        """
        from .providers.base import ContentBlock, Message, Role

        call_id = getattr(call, "id", "") or f"call_{getattr(call, 'name', '')}"
        payload = getattr(result, "text", "") or ""
        if getattr(result, "truncated", False):
            payload += "\n[the content above was truncated]"
        return Message(
            role=Role.TOOL,
            content=[ContentBlock(type="tool_result", text=payload, tool_call_id=call_id)],
            tool_call_id=call_id)
