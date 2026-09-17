#!/usr/bin/env python3
"""openai.py — the OpenAI-compatible adapter (OpenAI, DeepSeek, LM Studio, vLLM, OpenRouter).

WHY THIS EXISTS
---------------
`/chat/completions` with a Bearer token is the de-facto industry dialect. Supporting it
once covers OpenAI, DeepSeek, LM Studio, vLLM, OpenRouter, Together, Groq and most
self-hosted gateways — which is most of the "narrow waist" the design relies on.

DESIGN
------
- **System prompt goes in the messages array** as a leading `system` turn, which is how
  this dialect expects it (Anthropic hoists it instead — see `anthropic.py`).
- **SSE framing is parsed defensively.** The stream is `data: {json}` lines terminated by
  `data: [DONE]`. Comment lines and unknown fields are skipped rather than raising,
  because providers add fields over time and a strict parser would break on a provider
  upgrade.
- **Usage is requested explicitly** via `stream_options.include_usage`, because a
  streamed call otherwise reports no tokens at all — and an unmeasured run that looks
  free is exactly the cost illusion the design forbids.
- **Missing usage stays `None`, never zero.** Some compatible servers omit the usage
  block entirely; inventing a number there would corrupt the budget.
- **`json_mode` uses `response_format`, and only when asked.** Not every compatible
  server supports it, so an unsupported endpoint returning 400 is surfaced as a clear
  error rather than silently degrading.

Usage:
    provider = OpenAICompatibleProvider(provider_id="deepseek", base_url="...", api_key="...")
    response = provider.complete(ChatRequest(model="deepseek-chat", messages=[...]))
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from .base import (
    ChatRequest,
    ChatResponse,
    Chunk,
    ErrorKind,
    FinishReason,
    GatewayError,
    Provider,
    ProviderCapabilities,
    Role,
    ToolCall,
    Usage,
    cache_tokens_from,
)
from .http import HttpTransport

__all__ = ["OpenAICompatibleProvider"]

_FINISH_MAP = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}


class OpenAICompatibleProvider(Provider):
    """Adapter for any server speaking the OpenAI `/chat/completions` dialect.

    Parameters
    ----------
    provider_id:
        Key from `credentials.json`, used in events and error reporting.
    base_url:
        Endpoint root, e.g. `https://api.openai.com/v1`. A trailing `/chat/completions`
        is appended (or `/models` for discovery).
    api_key:
        Resolved secret, or None for local servers that need no auth.
    locality:
        "local" for Ollama/LM Studio-style endpoints, "cloud" otherwise. Drives the
        scheduler's memory accounting.
    """

    kind = "openai"

    def __init__(self, *, provider_id: str, base_url: str, api_key: str | None = None,
                 timeout_s: float = 120.0, max_retries: int = 3, locality: str = "cloud",
                 extra_headers: dict[str, str] | None = None,
                 transport: HttpTransport | None = None) -> None:
        self.provider_id = provider_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.locality = locality
        self.extra_headers = dict(extra_headers or {})
        self.transport = transport or HttpTransport(timeout_s=timeout_s, max_retries=max_retries)

    # ── headers ─────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _endpoint(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    # ── payload ─────────────────────────────────────────────────────────────

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        """Build the wire body.

        The system prompt becomes a leading `system` message; the rest map one-to-one.
        `stream` and `stream_options` are only set when streaming, so a non-streaming
        request matches what a caller would send by hand.
        """
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": Role.SYSTEM.value, "content": request.system})
        for message in request.messages:
            messages.append(message.as_openai())

        body: dict[str, Any] = {"model": request.model, "messages": messages}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.stop:
            body["stop"] = request.stop
        if request.tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in request.tools
            ]
        if request.json_mode:
            body["response_format"] = {"type": "json_object"}
        if request.stream:
            body["stream"] = True
            # Without this, a streamed response reports no usage at all.
            body["stream_options"] = {"include_usage": True}
        body.update(request.extra)
        return body

    # ── parsing ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_usage(data: dict[str, Any] | None) -> Usage:
        """Extract usage, preserving `None` for anything the provider omitted.

        DeepSeek — and any OpenAI-compatible endpoint that implements it — reports prompt caching as
        `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`. OpenAI itself nests the hit count at
        `prompt_tokens_details.cached_tokens`. Both are read, because the same adapter serves both
        and a dialect that reports cache but is not parsed produces a hit rate of zero, which reads
        as "caching is broken" rather than "we did not look".
        """
        if not data:
            return Usage()
        prompt = data.get("prompt_tokens")
        completion = data.get("completion_tokens")
        total = data.get("total_tokens")

        hit, miss = cache_tokens_from(data, kind="openai")
        return Usage(
            prompt_tokens=int(prompt) if isinstance(prompt, int) else None,
            completion_tokens=int(completion) if isinstance(completion, int) else None,
            total_tokens=int(total) if isinstance(total, int) else None,
            cache_hit_tokens=hit,
            cache_miss_tokens=miss,
        )

    @staticmethod
    def _parse_tool_calls(raw_calls: list[dict[str, Any]] | None) -> list[ToolCall]:
        """Parse tool calls, tolerating a malformed `arguments` string.

        A model that emits invalid JSON arguments is a real failure mode; recording the
        raw string as `{"_raw": ...}` keeps the information rather than dropping the call.
        """
        calls: list[ToolCall] = []
        for entry in raw_calls or []:
            if not isinstance(entry, dict):
                continue
            function = entry.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError, ValueError):
                arguments = {"_raw": raw_args}
            calls.append(ToolCall(
                id=str(entry.get("id") or f"call_{len(calls)}"),
                name=str(function.get("name") or ""),
                arguments=arguments,
            ))
        return calls

    def _parse_choice(self, data: dict[str, Any]) -> ChatResponse:
        """Turn a chat-completions body into a canonical response."""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise GatewayError(
                ErrorKind.BAD_RESPONSE,
                "response contained no choices",
                provider_id=self.provider_id,
                detail={"keys": sorted(data.keys())},
            )
        choice = choices[0] or {}
        message = choice.get("message") or {}
        text = message.get("content")
        # Some servers return content as a list of parts; flatten it rather than
        # stringifying a Python list into the prompt.
        if isinstance(text, list):
            text = "".join(
                part.get("text", "") for part in text if isinstance(part, dict)
            )
        finish = _FINISH_MAP.get(str(choice.get("finish_reason") or "").lower(), FinishReason.UNKNOWN)
        return ChatResponse(
            text=text or "",
            tool_calls=self._parse_tool_calls(message.get("tool_calls")),
            usage=self._parse_usage(data.get("usage")),
            finish_reason=finish,
            model=str(data.get("model") or ""),
            provider_id=self.provider_id,
            raw=data,
        )

    # ── Provider interface ──────────────────────────────────────────────────

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Perform a non-streaming completion."""
        if request.stream:
            raise GatewayError(
                ErrorKind.BAD_REQUEST,
                "complete() called with stream=True; use stream() instead",
                provider_id=self.provider_id,
            )
        response = self.transport.post_json(
            self._endpoint("chat/completions"), self._headers(), self._payload(request),
            provider_id=self.provider_id, model=request.model,
        )
        return self._parse_choice(response.json())

    def stream(self, request: ChatRequest) -> Iterator[Chunk]:
        """Stream a completion, reassembling SSE deltas.

        Tool-call fragments arrive split across chunks (OpenAI sends the name in one and
        the arguments in later ones), so fragments are accumulated and emitted as one
        `ToolCall` on the terminal chunk. Emitting them piecemeal would hand the caller
        unusable partial calls.
        """
        body = self._payload(_with_stream(request))
        pending: dict[int, dict[str, Any]] = {}
        finish: FinishReason | None = None
        usage: Usage | None = None
        index = 0

        for line in self.transport.stream_lines(
            self._endpoint("chat/completions"), self._headers(), body,
            provider_id=self.provider_id, model=request.model,
        ):
            if not line.startswith("data:"):
                # SSE comments (`: ping`) and unrelated fields are ignored on purpose.
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                # A provider keep-alive or a non-JSON data line; skipping is correct.
                continue

            if isinstance(data.get("usage"), dict):
                usage = self._parse_usage(data["usage"])

            choices = data.get("choices") or []
            if not choices:
                continue
            choice = choices[0] or {}
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, list):
                text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
            reason = choice.get("finish_reason")
            if reason:
                finish = _FINISH_MAP.get(str(reason).lower(), FinishReason.UNKNOWN)

            for fragment in delta.get("tool_calls") or []:
                if not isinstance(fragment, dict):
                    continue
                slot = int(fragment.get("index", 0))
                slot_data = pending.setdefault(slot, {"id": None, "name": "", "arguments": ""})
                if fragment.get("id"):
                    slot_data["id"] = fragment["id"]
                function = fragment.get("function") or {}
                if function.get("name"):
                    slot_data["name"] += function["name"]
                if function.get("arguments"):
                    slot_data["arguments"] += function["arguments"]

            if text:
                index += 1
                yield Chunk(text=text, index=index)

        for slot, slot_data in sorted(pending.items()):
            try:
                arguments = json.loads(slot_data["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw": slot_data["arguments"]}
            index += 1
            yield Chunk(
                tool_call=ToolCall(id=str(slot_data["id"] or f"call_{slot}"),
                                   name=slot_data["name"], arguments=arguments),
                index=index,
            )

        yield Chunk(finish_reason=finish or FinishReason.STOP, usage=usage, index=index + 1)

    def health(self) -> dict[str, Any]:
        """Check reachability via `/models`. Never raises.

        A 401 is reported as `misconfigured` rather than `down`, because the two need
        different fixes from the Owner and a single "unhealthy" would hide which.
        """
        result: dict[str, Any] = {
            "provider_id": self.provider_id,
            "kind": "openai",
            "base_url": self.base_url,
            "locality": self.locality,
            "has_key": bool(self.api_key),
        }
        try:
            response = self.transport.get_json(self._endpoint("models"), self._headers(),
                                               provider_id=self.provider_id)
            data = response.json()
            models = data.get("data")
            result["status"] = "ok"
            result["model_count"] = len(models) if isinstance(models, list) else None
        except GatewayError as exc:
            result["status"] = {
                ErrorKind.AUTH: "misconfigured",
                ErrorKind.NOT_FOUND: "unsupported_endpoint",
            }.get(exc.kind, "down")
            result["error"] = str(exc)
            result["error_kind"] = exc.kind.value
        return result

    def capabilities(self) -> ProviderCapabilities:
        """Declared capabilities. Local servers are marked unprobed for tools.

        LM Studio and friends often accept a `tools` array and ignore it, which would
        silently lose the structured trailer — so an unprobed local endpoint reports
        `None` rather than a hopeful `True`.
        """
        return ProviderCapabilities(
            supports_tools=True if self.locality == "cloud" else None,
            supports_streaming=True,
            supports_json_mode=True if self.locality == "cloud" else None,
            reports_usage=True,
            locality=self.locality,
            source="declared",
        )


def _with_stream(request: ChatRequest) -> ChatRequest:
    """Return a copy of a request with `stream=True`.

    `stream()` must never mutate the caller's request: the orchestrator reuses a request
    object for a retry, and a mutated `stream` flag would turn a replayed call into a
    streamed one.
    """
    clone = ChatRequest(
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
    return clone
