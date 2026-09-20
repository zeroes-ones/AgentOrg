#!/usr/bin/env python3
"""anthropic.py — the native Anthropic Messages adapter (`POST /v1/messages`).

WHY THIS EXISTS
---------------
Anthropic's API is **not** OpenAI-shaped, and pretending otherwise with a proxy would
hide the token accounting the cost ceiling depends on. The concrete differences this
adapter handles:

- the system prompt is a **top-level `system` field**, not a message in the array
- messages carry **content blocks**, and an assistant turn may mix text and `tool_use`
- auth is `x-api-key`, plus a mandatory `anthropic-version` header
- usage is `input_tokens`/`output_tokens`, and the stop reason is `end_turn`/`tool_use`
- tools are declared as a flat array with `input_schema`, not nested under `function`

An adapter that gets these wrong fails *quietly* — a mis-hoisted system prompt yields
plausible but unguided output — so the translations are covered by golden-payload tests.

DESIGN
------
- **Max tokens is mandatory.** The Messages API requires it, so a request that omits it
  gets a safe default rather than an API error.
- **Blocks are preserved both ways.** An assistant turn containing `tool_use` survives a
  round trip, which matters because the agent loop replays its own history.
- **`tool_result` blocks are converted to the correct shape**, which Anthropic expects as
  a user-role block referencing the tool call id.
- **A user-role turn must precede the request.** Anthropic rejects a conversation that
  starts with an assistant message; that is surfaced as a clear bad-request error rather
  than an opaque 400 from the API.

Usage:
    provider = AnthropicProvider(provider_id="anthropic", base_url="https://api.anthropic.com",
                                 api_key="sk-ant-...", api_version="2023-06-01")
    response = provider.complete(ChatRequest(model="claude-sonnet-4-20250514", messages=[...]))
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from .base import (
    ChatRequest,
    ChatResponse,
    Chunk,
    ContentBlock,
    ErrorKind,
    FinishReason,
    GatewayError,
    Message,
    Provider,
    ProviderCapabilities,
    Role,
    ToolCall,
    Usage,
    cache_tokens_from,
)
from .http import HttpTransport

__all__ = ["AnthropicProvider"]

# Anthropic reports a stop reason, not a finish reason; map it to the canonical set.
_STOP_MAP = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "tool_use": FinishReason.TOOL_CALLS,
    "refusal": FinishReason.CONTENT_FILTER,
}
# The Messages API requires max_tokens. This default matches a common ceiling and is
# overridden by any explicit request value.
_DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider(Provider):
    """Adapter for Anthropic's native Messages API."""

    kind = "anthropic"

    def __init__(self, *, provider_id: str, base_url: str, api_key: str | None = None,
                 api_version: str = "2023-06-01", timeout_s: float = 120.0,
                 max_retries: int = 3,
                 # A proxied or gateway-fronted Anthropic endpoint may need its own header (an
                 # organisation id, a routing key). Sent as-is, after the protocol headers, so a
                 # caller cannot accidentally break `anthropic-version` by supplying it here.
                 extra_headers: dict[str, str] | None = None,
                 transport: HttpTransport | None = None) -> None:
        self.provider_id = provider_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_version = api_version
        self.extra_headers = dict(extra_headers or {})
        self.transport = transport or HttpTransport(timeout_s=timeout_s, max_retries=max_retries)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "anthropic-version": self.api_version,
            "Content-Type": "application/json",
        }
        if self.api_key:
            # Anthropic uses x-api-key, not a Bearer token.
            headers["x-api-key"] = self.api_key
        for name, value in self.extra_headers.items():
            headers.setdefault(name, value)
        return headers

    def _endpoint(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    # ── payload translation ─────────────────────────────────────────────────

    @staticmethod
    def _content_blocks(message: Message) -> list[dict[str, Any]]:
        """Convert a canonical message into Anthropic content blocks.

        Preserves mixed text+tool_use assistant turns and tool_result user turns, which
        is what lets the agent loop replay its own history without losing the tool
        activity the next turn depends on.
        """
        blocks: list[dict[str, Any]] = []
        for block in message.content:
            if block.type == "tool_use" and block.tool_call is not None:
                blocks.append({
                    "type": "tool_use",
                    "id": block.tool_call.id,
                    "name": block.tool_call.name,
                    "input": block.tool_call.arguments,
                })
            elif block.type == "tool_result":
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.tool_call_id or "",
                    "content": block.text or "",
                })
            elif block.text:
                blocks.append({"type": "text", "text": block.text})

        # Canonical tool_calls on the message become tool_use blocks too, so a caller
        # that set `tool_calls` rather than content blocks is still translated correctly.
        if not any(b["type"] == "tool_use" for b in blocks):
            for call in message.tool_calls:
                blocks.append({
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                })

        # A canonical `tool` turn may carry its result as plain text with only `tool_call_id` set,
        # rather than as a pre-built `tool_result` block — the executor builds both shapes. Without
        # this the result went to the API as an ordinary `text` block in a user turn, which the model
        # reads as an unrelated remark rather than as the answer to the call it just made.
        if message.role is Role.TOOL and not any(b["type"] == "tool_result" for b in blocks):
            blocks.insert(0, {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": message.text or "",
            })
            if len(blocks) > 1 and blocks[1]["type"] == "text":
                blocks.pop(1)

        if not blocks:
            # Never send an empty content array: the API rejects it, and an empty turn is
            # more honestly represented as an empty string.
            blocks.append({"type": "text", "text": message.text or ""})
        return blocks

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        """Build the Messages API body.

        Note the two shape differences from OpenAI: `system` is top-level, and `tools`
        uses `input_schema` rather than a nested `function`.
        """
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            role = message.role
            if role is Role.SYSTEM:
                # System turns are hoisted; leaving one in the array is an API error.
                continue
            # **The Messages API has no `tool` role.** Its only roles are `user` and `assistant`,
            # and a tool's result is a `tool_result` content block *inside a user turn*. Sending the
            # canonical `tool` role through verbatim produced
            # `HTTP 422 ... messages[2].role: unknown variant 'tool'`, which killed every tool-using
            # node against any Anthropic-dialect endpoint — the run never got past its first tool
            # call. The block builder already emits the right `tool_result` block; only the role was
            # wrong.
            wire_role = "user" if role is Role.TOOL else role.value
            blocks = self._content_blocks(message)
            # **Every `tool_result` for one assistant turn must arrive in the user turn immediately
            # after it.** The agent loop emits one tool message per call, so when a turn made two calls
            # the request carried assistant(tool_use a, b) → user(result a) → user(result b), and the
            # API refused it: "`tool_use` ids were found without `tool_result` blocks immediately
            # after: call_…". Merging consecutive tool turns into one user turn is what satisfies the
            # rule; it only merges result-carrying turns, so an ordinary user remark still starts its
            # own turn.
            if (wire_role == "user" and messages and messages[-1]["role"] == "user"
                    and any(b.get("type") == "tool_result" for b in blocks)
                    and any(b.get("type") == "tool_result" for b in messages[-1]["content"])):
                messages[-1]["content"].extend(blocks)
                continue
            messages.append({"role": wire_role, "content": blocks})

        if not messages:
            raise GatewayError(
                ErrorKind.BAD_REQUEST,
                "Anthropic requires at least one non-system message",
                provider_id=self.provider_id,
            )
        if messages[0]["role"] != Role.USER.value:
            raise GatewayError(
                ErrorKind.BAD_REQUEST,
                "Anthropic requires the conversation to begin with a user message; "
                f"got {messages[0]['role']!r}. Move any leading assistant turn or drop it.",
                provider_id=self.provider_id,
            )

        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens or _DEFAULT_MAX_TOKENS,
        }
        if request.system:
            # A cache breakpoint on the system block. Anthropic's caching is **explicit**: unlike
            # DeepSeek and OpenAI, which reuse a matching prefix automatically, Claude caches only
            # what a `cache_control` marker covers — so without this the engine read Anthropic's
            # cache counters and reported "not reported" forever, on a provider that has caching.
            #
            # The system prompt is the right place because it is the stable head of every request:
            # it is byte-identical across every agent working a skill, which is exactly the invariant
            # the prefix work above exists to keep.
            body["system"] = [
                {"type": "text", "text": request.system,
                 "cache_control": {"type": "ephemeral"}}
            ]
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.stop:
            body["stop_sequences"] = request.stop
        if request.tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
            # Mark the tool block too. A breakpoint *after* the tools means the system prompt and the
            # whole tool list are cached together — they change for the same reason (a new session on
            # a different skill), so one marker covers both and a second would be wasted.
            body["tools"][-1]["cache_control"] = {"type": "ephemeral"}
        if request.stream:
            body["stream"] = True
        body.update(request.extra)
        return body

    # ── response translation ────────────────────────────────────────────────

    @staticmethod
    def _parse_usage(data: dict[str, Any] | None) -> Usage:
        """Map Anthropic's token fields to canonical usage, preserving unknown as None.

        Anthropic reports prompt caching as `cache_read_input_tokens` (served from cache) and
        `cache_creation_input_tokens` (written into it). Both are captured: the read is what a hit
        rate needs, and the write is billed at a *premium*, so dropping it would understate cost.
        """
        if not data:
            return Usage()
        prompt = data.get("input_tokens")
        completion = data.get("output_tokens")
        hit, _ = cache_tokens_from(data, kind="anthropic")

        def _int(value: Any) -> int | None:
            return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

        return Usage(
            prompt_tokens=int(prompt) if isinstance(prompt, int) else None,
            completion_tokens=int(completion) if isinstance(completion, int) else None,
            cache_hit_tokens=hit,
            cache_write_tokens=_int(data.get("cache_creation_input_tokens")),
        )

    @classmethod
    def _parse_response(cls, data: dict[str, Any], provider_id: str) -> ChatResponse:
        """Turn a Messages body into a canonical response."""
        content = data.get("content")
        if not isinstance(content, list):
            raise GatewayError(
                ErrorKind.BAD_RESPONSE,
                "response contained no content blocks",
                provider_id=provider_id,
                detail={"keys": sorted(data.keys())},
            )
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                raw_input = block.get("input")
                calls.append(ToolCall(
                    id=str(block.get("id") or f"call_{len(calls)}"),
                    name=str(block.get("name") or ""),
                    arguments=raw_input if isinstance(raw_input, dict) else {"_raw": raw_input},
                ))
        return ChatResponse(
            text="".join(texts),
            tool_calls=calls,
            usage=cls._parse_usage(data.get("usage")),
            finish_reason=_STOP_MAP.get(str(data.get("stop_reason") or "").lower(), FinishReason.UNKNOWN),
            model=str(data.get("model") or ""),
            provider_id=provider_id,
            raw=data,
        )

    # ── Provider interface ──────────────────────────────────────────────────

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Perform a non-streaming completion via `/v1/messages`."""
        if request.stream:
            raise GatewayError(
                ErrorKind.BAD_REQUEST,
                "complete() called with stream=True; use stream() instead",
                provider_id=self.provider_id,
            )
        response = self.transport.post_json(
            self._endpoint("v1/messages"), self._headers(), self._payload(request),
            provider_id=self.provider_id, model=request.model,
        )
        return self._parse_response(response.json(), self.provider_id)

    def stream(self, request: ChatRequest) -> Iterator[Chunk]:
        """Stream a completion, parsing Anthropic's named SSE events.

        Anthropic emits typed events (`message_start`, `content_block_delta`,
        `message_delta`, `message_stop`) rather than OpenAI's uniform delta. Text arrives
        in `content_block_delta`; usage arrives split across `message_start` (input) and
        `message_delta` (output), so both are accumulated and emitted together.
        """
        body = self._payload(_with_stream(request))
        usage = Usage()
        finish: FinishReason | None = None
        pending_calls: dict[int, dict[str, Any]] = {}
        index = 0

        for line in self.transport.stream_lines(
            self._endpoint("v1/messages"), self._headers(), body,
            provider_id=self.provider_id, model=request.model,
        ):
            if not line.startswith("data:"):
                # `event:` lines carry the type, but the type is also inside the JSON, so
                # only `data:` is needed.
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue

            event_type = str(event.get("type") or "")

            if event_type == "message_start":
                usage = self._parse_usage((event.get("message") or {}).get("usage"))
                continue

            if event_type == "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") == "tool_use":
                    slot = int(event.get("index", 0))
                    pending_calls[slot] = {
                        "id": block.get("id"), "name": block.get("name") or "",
                        "json": json.dumps(block.get("input") or {}),
                    }
                continue

            if event_type == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    index += 1
                    yield Chunk(text=str(delta["text"]), index=index)
                elif delta.get("type") == "input_json_delta":
                    slot = int(event.get("index", 0))
                    entry = pending_calls.setdefault(slot, {"id": None, "name": "", "json": ""})
                    # The first delta replaces the seed; later ones append.
                    if entry["json"] == "{}":
                        entry["json"] = ""
                    entry["json"] += str(delta.get("partial_json") or "")
                continue

            if event_type == "message_delta":
                delta = event.get("delta") or {}
                if delta.get("stop_reason"):
                    finish = _STOP_MAP.get(str(delta["stop_reason"]).lower(), FinishReason.UNKNOWN)
                # Output tokens arrive here, not in message_start.
                part = self._parse_usage(event.get("usage"))
                if part.completion_tokens is not None:
                    usage.completion_tokens = part.completion_tokens
                    if usage.prompt_tokens is not None:
                        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
                continue

            if event_type == "message_stop":
                break

        for slot, entry in sorted(pending_calls.items()):
            try:
                arguments = json.loads(entry["json"] or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw": entry["json"]}
            index += 1
            yield Chunk(
                tool_call=ToolCall(id=str(entry["id"] or f"call_{slot}"),
                                   name=entry["name"], arguments=arguments),
                index=index,
            )

        yield Chunk(finish_reason=finish or FinishReason.STOP, usage=usage, index=index + 1)

    def health(self) -> dict[str, Any]:
        """Check configuration and reachability. Never raises.

        There is no lightweight ping on the Messages API, so this reports configuration
        state by default and only records a definitive failure when a call was attempted.
        Reporting "unknown" is more honest than probing with a paid request.
        """
        result: dict[str, Any] = {
            "provider_id": self.provider_id,
            "kind": "anthropic",
            "base_url": self.base_url,
            "api_version": self.api_version,
            "locality": "cloud",
            "has_key": bool(self.api_key),
            "status": "configured" if self.api_key else "misconfigured",
        }
        if not self.api_key:
            result["error"] = "no API key resolved; set the configured api_key_env variable"
            result["error_kind"] = ErrorKind.AUTH.value
        return result

    def capabilities(self) -> ProviderCapabilities:
        """Anthropic supports tools, streaming and real usage reporting."""
        return ProviderCapabilities(
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=None,  # no dedicated json mode; relies on prompt + tools
            supports_vision=True,
            reports_usage=True,
            locality="cloud",
            source="declared",
        )


def _with_stream(request: ChatRequest) -> ChatRequest:
    """Return a copy with `stream=True`, never mutating the caller's request."""
    return ChatRequest(
        model=request.model,
        messages=request.messages,
        system=request.system,
        tools=request.tools,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        stream=True,
        json_mode=request.json_mode,
        stop=request.stop,
        extra=request.extra,
    )
