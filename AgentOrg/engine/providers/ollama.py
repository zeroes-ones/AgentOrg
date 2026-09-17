#!/usr/bin/env python3
"""ollama.py — the Ollama adapter, speaking its native `/api/chat` NDJSON protocol.

WHY A NATIVE ADAPTER
--------------------
Ollama is the primary local provider and the design leans on it for local agents, so it
gets first-class treatment rather than a fallback path:

- `/api/show` reports the **real** context length and parameter size, which is what makes
  a local model *probed* rather than *assumed* in the catalog — and the session
  projection depends on that number being real.
- `keep_alive` controls how long weights stay resident. On a unified-memory Mac, leaving
  a model loaded after a run finishes is the difference between a responsive machine and
  one that swaps.
- Streaming is newline-delimited JSON, not SSE, so the framing logic differs.

The OpenAI-compatible `/v1/chat/completions` endpoint is used as a fallback when the
native one is unavailable, so a proxy or an older build still works.

DESIGN
------
- **`eval_count`/`prompt_eval_count` are real usage**, so local runs are *measured* for
  tokens even though they cost nothing — which keeps the token budget honest.
- **Local cost is zero, not unknown.** A local model genuinely costs no API money, so
  `reported_cost_usd=0.0` is the truth here, unlike a cloud provider that failed to
  report.
- **`keep_alive` is set on every request** and is configurable, because the default
  Ollama behaviour (5 minutes) keeps unified memory occupied after a run.
- **A model-not-found error is classified as NOT_FOUND**, so the model picker can offer
  to pull it rather than reporting a generic failure.

Usage:
    provider = OllamaProvider(provider_id="ollama", base_url="http://localhost:11434")
    response = provider.complete(ChatRequest(model="qwen2.5-coder:7b", messages=[...]))
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
)
from .http import HttpTransport

__all__ = ["OllamaProvider"]

# How long to keep model weights resident after a request. Overridable per request via
# `extra={"keep_alive": ...}`; "0" unloads immediately, which the idle-unload policy uses.
_DEFAULT_KEEP_ALIVE = "5m"


class OllamaProvider(Provider):
    """Adapter for a local Ollama server."""

    kind = "ollama"

    def __init__(self, *, provider_id: str, base_url: str, api_key: str | None = None,
                 timeout_s: float = 300.0, max_retries: int = 2,
                 keep_alive: str = _DEFAULT_KEEP_ALIVE,
                 # A reverse-proxied Ollama often sits behind a gateway that wants a header of its
                 # own; without this the only route was a proxy in front of the proxy.
                 extra_headers: dict[str, str] | None = None,
                 transport: HttpTransport | None = None) -> None:
        self.provider_id = provider_id
        self.base_url = base_url.rstrip("/")
        # Ollama needs no key, but a reverse-proxied instance might.
        self.api_key = api_key
        self.keep_alive = keep_alive
        self.extra_headers = dict(extra_headers or {})
        # Long default timeout: loading weights for a large model on a cold start is
        # legitimately slow, and timing out there would look like a failure.
        self.transport = transport or HttpTransport(timeout_s=timeout_s, max_retries=max_retries)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for name, value in self.extra_headers.items():
            headers.setdefault(name, value)
        return headers

    def _endpoint(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    # ── payload ─────────────────────────────────────────────────────────────

    def _payload(self, request: ChatRequest, *, stream: bool = False) -> dict[str, Any]:
        """Build the `/api/chat` body.

        Option names differ from OpenAI: `num_predict` is the output cap, `options`
        nests sampling parameters, and `keep_alive` is top-level.
        """
        messages: list[dict[str, Any]] = []
        if request.system:
            # Ollama accepts a system role in the messages array, like OpenAI.
            messages.append({"role": Role.SYSTEM.value, "content": request.system})
        for message in request.messages:
            messages.append(self._ollama_message(message))

        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        if request.stop:
            options["stop"] = request.stop
        if request.json_mode:
            options["format"] = "json"

        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "stream": stream,
            "keep_alive": request.extra.get("keep_alive", self.keep_alive),
        }
        if options:
            body["options"] = options
        if request.tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in request.tools
            ]
        body.update({k: v for k, v in request.extra.items() if k != "keep_alive"})
        return body

    # ── parsing ─────────────────────────────────────────────────────────────

    @staticmethod
    def _ollama_message(message: Any) -> dict[str, Any]:
        """Render one message in the shape Ollama's template actually accepts.

        **A tool call must travel as text, not as a `tool_calls` field.** Ollama's chat template
        renders an assistant turn by emitting `<tool_call>{"name": …, "arguments": …}</tool_call>`
        into the content and then *parsing it back* server-side. Handing it the OpenAI shape instead —
        `content: null` with a structured `tool_calls` array — is rejected outright:

            HTTP 400: Value looks like object, but can't find closing '}' symbol

        which is the template trying and failing to parse `None` as JSON. So a tool call is rendered
        into the content exactly as the template expects, and the tool result goes back as a plain
        `tool` message.

        This was measured, not assumed: the structured shape returns 400 on every multi-turn tool
        conversation, which made the agent loop unusable on the *documented default* provider.
        """
        data = message.as_openai()
        calls = getattr(message, "tool_calls", None) or []
        if calls and message.role.value == "assistant":
            rendered = "\n".join(
                f'<tool_call>\n{{"name": "{call.name}", "arguments": {call.arguments_json}}}\n'
                f"</tool_call>" for call in calls)
            # The narration (if any) precedes the calls, which is how the template emits it back.
            narrative = message.text or ""
            data["content"] = f"{narrative}\n{rendered}".strip()
            # The structured field is dropped: the template cannot read it, and leaving it in makes
            # Ollama try to parse both forms.
            data.pop("tool_calls", None)
        return data

    @staticmethod
    def _parse_usage(data: dict[str, Any]) -> Usage:
        """Map Ollama's counters to canonical usage.

        `prompt_eval_count` and `eval_count` are genuine measurements, and local cost is
        genuinely zero — reported as 0.0 rather than left unknown, because the two mean
        different things to the economics view.

        Ollama *does* reuse a resident prefix via `keep_alive`, but its API reports no cache
        read/hit counters, so the cache fields stay `None`. That is deliberate rather than an
        omission: inferring a hit rate from a faster-than-usual prompt would be a guess, and an
        unreported cache must never be rendered as a 0% hit rate — the same rule that keeps an
        unreported cost from rendering as free.
        """
        prompt = data.get("prompt_eval_count")
        completion = data.get("eval_count")
        if prompt is None and completion is None:
            return Usage()
        return Usage(
            prompt_tokens=int(prompt) if isinstance(prompt, int) else None,
            completion_tokens=int(completion) if isinstance(completion, int) else None,
            reported_cost_usd=0.0,
        )

    @staticmethod
    def _parse_tool_calls(raw: list[dict[str, Any]] | None) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for entry in raw or []:
            if not isinstance(entry, dict):
                continue
            function = entry.get("function") or {}
            raw_args = function.get("arguments")
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {"_raw": raw_args}
            calls.append(ToolCall(
                id=str(entry.get("id") or f"call_{len(calls)}"),
                name=str(function.get("name") or ""),
                arguments=raw_args if isinstance(raw_args, dict) else {},
            ))
        return calls

    @staticmethod
    def recover_tool_calls(text: str, tool_names: set[str]) -> list[ToolCall]:
        """Recover a tool call the model wrote as plain text instead of in `tool_calls`.

        Ollama's template *instructs* a model to wrap each call in `<tool_call>…</tool_call>` and
        parses that server-side. Models frequently ignore the tags and emit the bare JSON, or drop it
        inside a ```json fence — the server then returns `tool_calls: []` with the call sitting in the
        content, and an agent loop sees "no tool call" and stops after one step having done nothing.

        Measured on `qwen2.5-coder:14b`: asked to read a file, it replied with
        `{"name": "read_file", "arguments": {...}}` and no `tool_calls` at all. Without recovery the
        tool loop is unusable on that model, which is the *documented* default here.

        This is repair, not interpretation: a recovery is accepted only when the object names a tool
        that was actually advertised and carries an `arguments` object. That guard is what stops a
        model's illustrative example from being executed as a real call.
        """
        import re

        candidates: list[str] = []
        for match in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL):
            candidates.append(match.group(1))
        for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
            candidates.append(match.group(1))
        # A bare object last: it is the shape most likely to appear inside prose, so it is only tried
        # when nothing was tagged or fenced.
        stripped = text.strip()
        if stripped.startswith("{"):
            candidates.append(stripped)

        calls: list[ToolCall] = []
        for candidate in candidates:
            payload = _first_json_object(candidate)
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("name") or payload.get("tool") or "").strip()
            arguments = payload.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    continue
            if name in tool_names and isinstance(arguments, dict) and arguments:
                calls.append(ToolCall(id=f"recovered_{len(calls)}", name=name,
                                      arguments=arguments))
        return calls

    def _parse_message(self, data: dict[str, Any], model: str,
                       tool_names: set[str] | None = None) -> ChatResponse:
        message = data.get("message") or {}
        done = bool(data.get("done"))
        text = str(message.get("content") or "")
        calls = self._parse_tool_calls(message.get("tool_calls"))
        if not calls and tool_names:
            # The server returned no structured call, but the model may have written one as text —
            # the common failure on models that ignore the `<tool_call>` wrapper. Recovery runs only
            # when there is nothing to lose, so a properly-parsed call is never second-guessed.
            recovered = self.recover_tool_calls(text, tool_names)
            if recovered:
                calls = recovered
                text = ""
        if not done:
            finish = FinishReason.UNKNOWN
        elif calls:
            # A reply carrying tool calls is not a finished reply, whatever the server says: the
            # loop must continue rather than treat it as the model's final answer.
            finish = FinishReason.TOOL_CALLS
        else:
            finish = FinishReason.STOP
        return ChatResponse(
            text=text,
            tool_calls=calls,
            usage=self._parse_usage(data),
            finish_reason=finish,
            model=str(data.get("model") or model),
            provider_id=self.provider_id,
            raw=data,
        )

    def _classify(self, exc: GatewayError) -> GatewayError:
        """Refine a generic 404 into a model-not-found with an actionable message.

        Ollama returns 404 both for a missing model and for a missing route, and the fix
        differs sharply — so the message names the likely remedy.
        """
        if exc.kind is ErrorKind.NOT_FOUND and "model" in str(exc).lower():
            exc.detail["hint"] = "model not present locally; run: ollama pull <model>"
        return exc

    # ── Provider interface ──────────────────────────────────────────────────

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Perform a non-streaming completion via `/api/chat` with `stream=false`."""
        try:
            response = self.transport.post_json(
                self._endpoint("api/chat"), self._headers(), self._payload(request, stream=False),
                provider_id=self.provider_id, model=request.model,
            )
        except GatewayError as exc:
            raise self._classify(exc) from None
        return self._parse_message(response.json(), request.model,
                                   {t.name for t in request.tools})

    def stream(self, request: ChatRequest) -> Iterator[Chunk]:
        """Stream a completion, parsing Ollama's newline-delimited JSON.

        Each line is a complete JSON object ending with `done: true`, which carries the
        final counters. There is no `[DONE]` sentinel as in SSE.
        """
        usage: Usage = Usage()
        finish: FinishReason | None = None
        index = 0
        try:
            for line in self.transport.stream_lines(
                self._endpoint("api/chat"), self._headers(), self._payload(request, stream=True),
                provider_id=self.provider_id, model=request.model,
            ):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("error"):
                    raise GatewayError(
                        ErrorKind.BAD_REQUEST, f"ollama error: {data['error']}",
                        provider_id=self.provider_id, model=request.model,
                    )
                message = data.get("message") or {}
                text = message.get("content")
                if text:
                    index += 1
                    yield Chunk(text=str(text), index=index)
                for call in self._parse_tool_calls(message.get("tool_calls")):
                    index += 1
                    yield Chunk(tool_call=call, index=index)
                if data.get("done"):
                    usage = self._parse_usage(data)
                    finish = FinishReason.STOP
                    break
        except GatewayError as exc:
            raise self._classify(exc) from None

        yield Chunk(finish_reason=finish or FinishReason.STOP, usage=usage, index=index + 1)

    def health(self) -> dict[str, Any]:
        """Check reachability via `/api/tags`. Never raises."""
        result: dict[str, Any] = {
            "provider_id": self.provider_id,
            "kind": "ollama",
            "base_url": self.base_url,
            "locality": "local",
            "has_key": bool(self.api_key),
        }
        try:
            response = self.transport.get_json(self._endpoint("api/tags"), self._headers(),
                                               provider_id=self.provider_id)
            data = response.json()
            models = data.get("models")
            result["status"] = "ok"
            result["model_count"] = len(models) if isinstance(models, list) else None
            result["loaded"] = [
                {"name": m.get("name"), "size_bytes": m.get("size")}
                for m in (models or []) if isinstance(m, dict)
            ][:50]
        except GatewayError as exc:
            result["status"] = "down"
            result["error"] = str(exc)
            result["error_kind"] = exc.kind.value
            result["hint"] = "start Ollama, or set base_url to the right port"
        return result

    def capabilities(self) -> ProviderCapabilities:
        """Ollama reports usage and streams; tool support depends on the model.

        `supports_tools=None` because it is a property of the *model*, not the server —
        claiming True here would make the planner send tools to a model that ignores them.
        """
        return ProviderCapabilities(
            supports_tools=None,
            supports_streaming=True,
            supports_json_mode=True,   # the `format: json` option is server-wide
            reports_usage=True,
            locality="local",
            source="declared",
        )


def _first_json_object(text: str) -> dict[str, Any] | None:
    """The first balanced JSON object in `text`, or None.

    Balanced rather than greedy: a reply may contain several objects, and `json.loads` on the whole
    string would either fail or take the wrong one. Braces inside strings are skipped, so a path like
    `{"path": "a{b}.py"}` does not end the scan early.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None
