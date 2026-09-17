#!/usr/bin/env python3
"""Phase 2 tests — providers, gateway, catalog, tokens, cost correctness.

The emphasis is on the translations that fail *quietly*: a mis-hoisted system prompt, a
usage field mapped to the wrong counter, an unmeasured cost rendered as zero, a
context-overflow retried identically. Those are the bugs that look like success, so they
get the most coverage.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine.bus import EventBus
from engine.catalog import ModelCatalog
from engine.config import ConfigError, load
from engine.gateway import BudgetExceeded, Cost, CostLedger, Gateway
from engine.providers.anthropic import AnthropicProvider
from engine.providers.base import (
    ChatRequest,
    ContentBlock,
    ErrorKind,
    FinishReason,
    GatewayError,
    Message,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
    estimate_message_tokens,
)
from engine.providers.fake import FakeProvider, json_reply
from engine.providers.http import HttpTransport, backoff_delay
from engine.providers.ollama import OllamaProvider
from engine.providers.openai import OpenAICompatibleProvider
from engine.providers.registry import build_provider, build_providers
from engine.tokens import TokenEstimator


# ── test doubles ─────────────────────────────────────────────────────────────


class StubResponse:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status = status
        self.body = json.dumps(payload).encode()
        self.headers: dict[str, str] = {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class StubTransport:
    """Replays recorded provider responses and records what the adapter sent."""

    def __init__(self, routes: dict[str, object] | None = None, *, error: Exception | None = None):
        self.routes = dict(routes or {})
        self.error = error
        self.calls: list[tuple[str, str, dict | None]] = []

    def _match(self, url: str, method: str, body: dict | None):
        self.calls.append((method, url, body))
        if self.error is not None:
            raise self.error
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return StubResponse(payload)
        raise GatewayError(ErrorKind.NOT_FOUND, f"stub has no route for {url}")

    def get_json(self, url, headers, **kwargs):
        return self._match(url, "GET", None)

    def post_json(self, url, headers, body, **kwargs):
        return self._match(url, "POST", body)

    def stream_lines(self, url, headers, body, **kwargs):
        self.calls.append(("POST", url, body))
        if self.error is not None:
            raise self.error
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                for line in payload:  # type: ignore[union-attr]
                    yield line
                return
        raise GatewayError(ErrorKind.NOT_FOUND, f"stub has no stream route for {url}")


def _ollama(transport) -> OllamaProvider:
    return OllamaProvider(provider_id="ollama", base_url="http://localhost:11434",
                          transport=transport)


def _openai(transport, *, api_key: str = "sk-test-0123456789abcdefghij") -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(provider_id="openai", base_url="https://api.openai.com/v1",
                                    api_key=api_key, transport=transport)


def _anthropic(transport) -> AnthropicProvider:
    return AnthropicProvider(provider_id="anthropic", base_url="https://api.anthropic.com",
                             api_key="sk-ant-test-0123456789abcdef", transport=transport)


# ── usage semantics ──────────────────────────────────────────────────────────


def test_usage_unknown_is_not_zero():
    """`None` must mean not-reported, and must not be read as 'spent nothing'."""
    usage = Usage()
    assert usage.total_tokens is None
    assert not usage.measured
    partial = Usage(prompt_tokens=10)
    assert partial.total_tokens is None, "a total inferred from one part is fabricated"
    assert not partial.measured


def test_usage_total_computed_only_when_both_parts_known():
    assert Usage(prompt_tokens=10, completion_tokens=5).total_tokens == 15


def test_usage_merge_preserves_partial_knowledge():
    full = Usage(prompt_tokens=10, completion_tokens=5)
    partial = Usage(prompt_tokens=7)
    merged = full.merge(partial)
    assert merged.total_tokens is None
    assert not merged.measured


# ── error taxonomy ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, expected",
    [(401, ErrorKind.AUTH), (403, ErrorKind.AUTH), (429, ErrorKind.RATE_LIMIT),
     (400, ErrorKind.BAD_REQUEST), (404, ErrorKind.NOT_FOUND), (408, ErrorKind.TIMEOUT),
     (503, ErrorKind.SERVER), (418, ErrorKind.UNKNOWN)],
)
def test_from_status_classification(status, expected):
    assert GatewayError.from_status(status, "x").kind is expected


@pytest.mark.parametrize(
    "kind, retryable",
    [(ErrorKind.RATE_LIMIT, True), (ErrorKind.TIMEOUT, True), (ErrorKind.CONNECTION, True),
     (ErrorKind.SERVER, True), (ErrorKind.AUTH, False), (ErrorKind.CONTEXT_LENGTH, False),
     (ErrorKind.BAD_REQUEST, False), (ErrorKind.NOT_FOUND, False)],
)
def test_retryability(kind, retryable):
    err = GatewayError(kind, "x")
    assert err.retryable is retryable
    assert kind.retryable is retryable


def test_context_length_is_not_retryable():
    """Retrying an identical oversized request can only fail again."""
    assert not ErrorKind.CONTEXT_LENGTH.retryable


# ── transport ────────────────────────────────────────────────────────────────


def test_backoff_is_bounded_and_jittered():
    plain = [backoff_delay(n, jitter=False) for n in range(1, 6)]
    assert plain == sorted(plain), "without jitter the delay must grow monotonically"
    assert plain[0] < plain[-1]
    for _ in range(20):
        d = backoff_delay(3)
        assert 0 < d <= plain[2], "jitter must stay within the nominal window"


def test_retry_honours_retry_after_and_stops_when_not_retryable():
    import urllib.error

    sleeps: list[float] = []
    transport = HttpTransport(sleep=sleeps.append, max_retries=2)
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None, context=None):
        calls["n"] += 1
        if calls["n"] == 1:
            headers = {"Retry-After": "3"}
            raise urllib.error.HTTPError(request.full_url, 429, "slow down", headers, None)
        return _FakeHttpResponse(b'{"ok":true}')

    transport_open = transport
    monkeypatch_target = "engine.providers.http.urllib.request.urlopen"
    import engine.providers.http as http_mod
    original = http_mod.urllib.request.urlopen
    http_mod.urllib.request.urlopen = fake_urlopen
    try:
        result = transport_open.post_json("http://x/y", {}, {})
        assert result.status == 200
    finally:
        http_mod.urllib.request.urlopen = original
    assert calls["n"] == 2
    assert sleeps == [3.0], "Retry-After must be honoured over the computed backoff"


def test_non_retryable_error_raises_immediately():
    import urllib.error
    import engine.providers.http as http_mod

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None, context=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(request.full_url, 401, "bad key", {}, None)

    transport = HttpTransport(sleep=lambda _s: None, max_retries=5)
    original = http_mod.urllib.request.urlopen
    http_mod.urllib.request.urlopen = fake_urlopen
    try:
        with pytest.raises(GatewayError) as excinfo:
            transport.post_json("http://x/y", {}, {})
    finally:
        http_mod.urllib.request.urlopen = original
    assert excinfo.value.kind is ErrorKind.AUTH
    assert calls["n"] == 1, "a 401 must not be retried"


def test_context_overflow_reported_as_400_is_classified():
    import urllib.error
    import engine.providers.http as http_mod

    body = b'{"error":{"message":"This model maximum context length is 8192 tokens"}}'

    def fake_urlopen(request, timeout=None, context=None):
        raise urllib.error.HTTPError(request.full_url, 400, "bad request", {}, _BytesReader(body))

    transport = HttpTransport(sleep=lambda _s: None, max_retries=3)
    original = http_mod.urllib.request.urlopen
    http_mod.urllib.request.urlopen = fake_urlopen
    try:
        with pytest.raises(GatewayError) as excinfo:
            transport.post_json("http://x/y", {}, {})
    finally:
        http_mod.urllib.request.urlopen = original
    assert excinfo.value.kind is ErrorKind.CONTEXT_LENGTH, (
        "a 400 mentioning context length must trigger compaction, not a retry"
    )


class _FakeHttpResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.status = 200
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._body.splitlines(keepends=True))

    def close(self):
        pass


class _BytesReader:
    """Minimal readable object, standing in for an HTTPError's body stream."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self, *_args) -> bytes:
        return self._body

    def close(self) -> None:
        """Present so a temporary-file cleanup in the interpreter cannot warn."""
        return None


# ── OpenAI-compatible adapter ────────────────────────────────────────────────


def test_openai_payload_hoists_system_into_messages():
    provider = _openai(StubTransport())
    request = ChatRequest(model="gpt-4o", system="be terse",
                          messages=[Message.text_message(Role.USER, "hi")])
    body = provider._payload(request)
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    assert body["messages"][1]["role"] == "user"
    assert "system" not in body, "OpenAI expects system inside messages, not top-level"


def test_openai_stream_requests_usage_inclusion():
    """Without stream_options a streamed call reports no tokens at all."""
    provider = _openai(StubTransport())
    body = provider._payload(ChatRequest(model="m", messages=[], stream=True))
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


def test_openai_parses_response_and_usage():
    transport = StubTransport({"chat/completions": {
        "model": "gpt-4o-2024-11-20",
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }})
    response = _openai(transport).complete(ChatRequest(model="gpt-4o", messages=[]))
    assert response.text == "hello"
    assert response.usage.prompt_tokens == 10 and response.usage.measured
    assert response.finish_reason is FinishReason.STOP


def test_openai_missing_usage_stays_unknown():
    transport = StubTransport({"chat/completions": {
        "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
    }})
    response = _openai(transport).complete(ChatRequest(model="m", messages=[]))
    assert response.usage.prompt_tokens is None
    assert not response.usage.measured


def test_openai_parses_tool_calls_and_tolerates_bad_arguments():
    transport = StubTransport({"chat/completions": {
        "choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "check", "arguments": '{"a": 1}'}},
            {"id": "c2", "type": "function",
             "function": {"name": "broken", "arguments": 'not json'}},
        ]}, "finish_reason": "tool_calls"}],
    }})
    response = _openai(transport).complete(ChatRequest(model="m", messages=[]))
    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.tool_calls[0].arguments == {"a": 1}
    assert response.tool_calls[1].arguments == {"_raw": "not json"}, "bad JSON must be kept, not dropped"


def test_openai_empty_choices_is_a_bad_response():
    transport = StubTransport({"chat/completions": {"choices": []}})
    with pytest.raises(GatewayError) as excinfo:
        _openai(transport).complete(ChatRequest(model="m", messages=[]))
    assert excinfo.value.kind is ErrorKind.BAD_RESPONSE


def test_openai_stream_reassembles_split_tool_calls():
    """OpenAI splits a tool call across chunks; reassembly must emit one complete call."""
    lines = [
        'data: {"choices":[{"delta":{"content":"He"}}]}',
        'data: {"choices":[{"delta":{"content":"llo"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"che","arguments":"{\\"a\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"name":"ck","arguments":"1}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        'data: {"usage":{"prompt_tokens":5,"completion_tokens":2}}',
        "data: [DONE]",
    ]
    provider = _openai(StubTransport({"chat/completions": lines}))
    chunks = list(provider.stream(ChatRequest(model="m", messages=[])))
    text = "".join(c.text for c in chunks)
    calls = [c.tool_call for c in chunks if c.tool_call]
    assert text == "Hello"
    assert len(calls) == 1, "fragments must be accumulated into one call"
    assert calls[0].name == "check"
    assert calls[0].arguments == {"a": 1}
    assert chunks[-1].finish_reason is FinishReason.TOOL_CALLS
    assert chunks[-1].usage is not None and chunks[-1].usage.prompt_tokens == 5


def test_openai_stream_ignores_comments_and_unknown_fields():
    lines = [
        ": keep-alive",
        'data: {"choices":[{"delta":{"content":"ok"}}], "unexpected": {"nested": true}}',
        "data: [DONE]",
    ]
    provider = _openai(StubTransport({"chat/completions": lines}))
    assert "".join(c.text for c in provider.stream(ChatRequest(model="m", messages=[]))) == "ok"


def test_stream_does_not_mutate_the_caller_request():
    provider = _openai(StubTransport({"chat/completions": ["data: [DONE]"]}))
    request = ChatRequest(model="m", messages=[], stream=False)
    list(provider.stream(request))
    assert request.stream is False, "streaming must not flip the caller's request"


def test_openai_health_distinguishes_misconfigured_from_down():
    provider = _openai(StubTransport(error=GatewayError(ErrorKind.AUTH, "no key")))
    health = provider.health()
    assert health["status"] == "misconfigured"


def test_openai_health_never_raises():
    provider = _openai(StubTransport(error=GatewayError(ErrorKind.CONNECTION, "refused")))
    assert provider.health()["status"] == "down"


# ── Anthropic adapter ────────────────────────────────────────────────────────


def test_anthropic_hoists_system_to_top_level_with_a_cache_breakpoint():
    """Anthropic needs an explicit `cache_control` marker or nothing caches at all.

    Unlike DeepSeek and OpenAI, which reuse a matching prefix automatically, Claude only caches what a
    marker covers. Without this the engine read Anthropic's cache counters and reported "not
    reported" forever, on a provider that has caching.
    """
    provider = _anthropic(StubTransport())
    body = provider._payload(ChatRequest(model="claude", system="be terse",
                                         messages=[Message.text_message(Role.USER, "hi")]))
    assert body["system"] == [{"type": "text", "text": "be terse",
                               "cache_control": {"type": "ephemeral"}}]
    assert all(m["role"] != "system" for m in body["messages"]), "system must not stay in messages"


def test_anthropic_marks_the_tool_block_as_cacheable():
    """One breakpoint after the tools covers the system prompt and the whole tool list together."""
    provider = _anthropic(StubTransport())
    body = provider._payload(ChatRequest(
        model="claude", system="be terse",
        messages=[Message.text_message(Role.USER, "hi")],
        tools=[ToolSpec(name="read_file", description="r", parameters={"type": "object"})]))
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_requires_max_tokens():
    provider = _anthropic(StubTransport())
    body = provider._payload(ChatRequest(model="claude", messages=[
        Message.text_message(Role.USER, "hi")]))
    assert isinstance(body["max_tokens"], int) and body["max_tokens"] > 0


def test_anthropic_rejects_a_leading_assistant_turn_with_a_clear_error():
    provider = _anthropic(StubTransport())
    with pytest.raises(GatewayError) as excinfo:
        provider._payload(ChatRequest(model="claude", messages=[
            Message.text_message(Role.ASSISTANT, "hello")]))
    assert excinfo.value.kind is ErrorKind.BAD_REQUEST
    assert "begin with a user message" in str(excinfo.value)


def test_anthropic_tools_use_input_schema_not_function():
    provider = _anthropic(StubTransport())
    body = provider._payload(ChatRequest(
        model="claude", messages=[Message.text_message(Role.USER, "hi")],
        tools=[ToolSpec(name="check", description="d", parameters={"type": "object"})],
    ))
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    assert "function" not in body["tools"][0]


def test_anthropic_round_trips_mixed_text_and_tool_use():
    message = Message(role=Role.ASSISTANT, content=[
        ContentBlock(type="text", text="thinking"),
        ContentBlock(type="tool_use", tool_call=ToolCall(id="t1", name="check", arguments={"a": 1})),
    ])
    blocks = AnthropicProvider._content_blocks(message)
    assert [b["type"] for b in blocks] == ["text", "tool_use"]
    assert blocks[1]["input"] == {"a": 1}


def test_anthropic_parses_response_usage_and_stop_reason():
    transport = StubTransport({"v1/messages": {
        "model": "claude-sonnet-4-20250514",
        "content": [{"type": "text", "text": "answer"},
                    {"type": "tool_use", "id": "t1", "name": "check", "input": {"a": 1}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 20, "output_tokens": 8},
    }})
    response = _anthropic(transport).complete(ChatRequest(model="claude", messages=[
        Message.text_message(Role.USER, "q")]))
    assert response.text == "answer"
    assert response.usage.prompt_tokens == 20 and response.usage.completion_tokens == 8
    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.tool_calls[0].arguments == {"a": 1}


def test_anthropic_stream_parses_named_events():
    events = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":30}}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}',
        'data: {"type":"message_stop"}',
    ]
    provider = _anthropic(StubTransport({"v1/messages": events}))
    chunks = list(provider.stream(ChatRequest(model="claude", messages=[
        Message.text_message(Role.USER, "q")])))
    assert "".join(c.text for c in chunks) == "Hello"
    terminal = chunks[-1]
    assert terminal.finish_reason is FinishReason.STOP
    assert terminal.usage is not None
    assert terminal.usage.prompt_tokens == 30, "input tokens arrive in message_start"
    assert terminal.usage.completion_tokens == 4, "output tokens arrive in message_delta"


def test_anthropic_health_without_key_is_misconfigured():
    provider = AnthropicProvider(provider_id="anthropic", base_url="https://api.anthropic.com",
                                 api_key=None, transport=StubTransport())
    assert provider.health()["status"] == "misconfigured"


# ── Ollama adapter ───────────────────────────────────────────────────────────


def test_ollama_maps_options_not_openai_names():
    provider = _ollama(StubTransport())
    body = provider._payload(ChatRequest(model="m", messages=[], temperature=0.1, max_tokens=64,
                                         json_mode=True))
    assert body["options"]["num_predict"] == 64
    assert body["options"]["format"] == "json"
    assert "max_tokens" not in body and "response_format" not in body
    assert "keep_alive" in body, "weights must be releasable to free unified memory"


def test_ollama_usage_is_measured_and_free():
    transport = StubTransport({"api/chat": {
        "model": "qwen2.5-coder:7b",
        "message": {"content": "hi"},
        "done": True,
        "prompt_eval_count": 42,
        "eval_count": 7,
    }})
    response = _ollama(transport).complete(ChatRequest(model="m", messages=[]))
    assert response.usage.prompt_tokens == 42 and response.usage.completion_tokens == 7
    assert response.usage.reported_cost_usd == 0.0, "local cost is a known zero"


def test_ollama_stream_parses_ndjson():
    lines = [
        '{"message":{"content":"a"},"done":false}',
        '{"message":{"content":"b"},"done":false}',
        '{"message":{"content":""},"done":true,"prompt_eval_count":9,"eval_count":2}',
    ]
    provider = _ollama(StubTransport({"api/chat": lines}))
    chunks = list(provider.stream(ChatRequest(model="m", messages=[])))
    assert "".join(c.text for c in chunks) == "ab"
    assert chunks[-1].usage is not None and chunks[-1].usage.prompt_tokens == 9


def test_ollama_surfaces_an_inline_error():
    provider = _ollama(StubTransport({"api/chat": ['{"error":"model not found"}']}))
    with pytest.raises(GatewayError) as excinfo:
        list(provider.stream(ChatRequest(model="m", messages=[])))
    assert excinfo.value.kind is ErrorKind.BAD_REQUEST


def test_ollama_health_never_raises():
    provider = _ollama(StubTransport(error=GatewayError(ErrorKind.CONNECTION, "refused")))
    health = provider.health()
    assert health["status"] == "down"
    assert "hint" in health


# ── registry ─────────────────────────────────────────────────────────────────


def test_registry_reports_skipped_providers_with_the_reason(monkeypatch):
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    providers, skipped = build_providers(load())
    assert "ollama" in providers
    assert any(s.startswith("openai:") and "OPENAI_API_KEY" in s for s in skipped)


def test_registry_strict_mode_raises(monkeypatch):
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ConfigError, match="strict mode"):
        build_providers(load(), strict=True)


def test_registry_rejects_unknown_kind():
    cfg = load()
    spec = cfg.provider("ollama")
    object.__setattr__(spec, "kind", "nonsense")
    with pytest.raises(ConfigError, match="unsupported kind"):
        build_provider(spec)


# ── token estimator ──────────────────────────────────────────────────────────


def test_estimator_starts_heuristic_and_calibrates():
    estimator = TokenEstimator()
    request = ChatRequest(model="m", messages=[Message.text_message(Role.USER, "x" * 400)])
    first = estimator.estimate(request, provider_id="p")
    assert first.source == "heuristic"

    chars = TokenEstimator.count_chars(request)
    estimator.observe("p", "m", chars=chars, usage=Usage(prompt_tokens=200, completion_tokens=1))
    after = estimator.estimate(request, provider_id="p")
    assert after.source == "calibrated"
    assert after.ratio != first.ratio


def test_calibration_converges_and_shrinks_error():
    """Error must fall as observations accumulate — the update must not be circular."""
    estimator = TokenEstimator(default_ratio=2.0)
    request = ChatRequest(model="m", messages=[Message.text_message(Role.USER, "x" * 4000)])
    chars = TokenEstimator.count_chars(request)
    errors: list[float] = []
    for _ in range(6):
        estimate = estimator.estimate(request, provider_id="p")
        errors.append(abs(estimate.tokens - 1000) / 1000.0)
        estimator.observe("p", "m", chars=chars,
                          usage=Usage(prompt_tokens=1000, completion_tokens=1),
                          estimated_tokens=estimate.tokens)
    assert errors[-1] < errors[0], "calibration must reduce the error"
    assert errors[-1] < 0.05, f"calibration should converge; final error {errors[-1]:.3f}"


def test_calibration_ignores_unmeasured_usage():
    estimator = TokenEstimator()
    estimator.observe("p", "m", chars=1000, usage=Usage())
    assert estimator.ratio_for("p", "m") == estimator.default_ratio, (
        "silence must not be treated as ground truth"
    )


def test_calibration_is_clamped():
    estimator = TokenEstimator()
    estimator.observe("p", "m", chars=100_000, usage=Usage(prompt_tokens=1, completion_tokens=1))
    assert 2.0 <= estimator.ratio_for("p", "m") <= 5.0


def test_calibration_is_per_model():
    estimator = TokenEstimator()
    # 2000 chars / 1000 tokens = 2.0, which differs from the 4.0 baseline so the
    # assertion is meaningful rather than accidentally comparing two defaults.
    estimator.observe("p", "a", chars=2000, usage=Usage(prompt_tokens=1000, completion_tokens=1))
    assert estimator.ratio_for("p", "a") != estimator.ratio_for("p", "b")
    assert estimator.ratio_for("p", "b") == estimator.default_ratio


def test_estimate_counts_tool_schemas():
    estimator = TokenEstimator()
    without = ChatRequest(model="m", messages=[Message.text_message(Role.USER, "hi")])
    with_tools = ChatRequest(model="m", messages=[Message.text_message(Role.USER, "hi")],
                             tools=[ToolSpec(name="n", description="d" * 500,
                                             parameters={"type": "object"})])
    assert estimator.estimate(with_tools).tokens > estimator.estimate(without).tokens


def test_message_token_estimate_is_positive():
    assert estimate_message_tokens([Message.text_message(Role.USER, "hello")]) > 0


# ── cost correctness ─────────────────────────────────────────────────────────


def test_local_model_cost_is_a_known_free_zero():
    gateway = Gateway(load(), {}, estimator=TokenEstimator())
    cost = gateway.compute_cost("ollama", "qwen2.5-coder:7b", Usage(prompt_tokens=100, completion_tokens=50))
    assert cost.source == "free"
    assert cost.known and cost.usd == 0.0


def test_priced_cloud_model_is_estimated():
    gateway = Gateway(load(), {}, estimator=TokenEstimator())
    cost = gateway.compute_cost("openai", "gpt-4o-2024-11-20",
                                Usage(prompt_tokens=1000, completion_tokens=500))
    assert cost.source == "estimated"
    assert cost.known and cost.usd > 0


def test_unmeasured_priced_model_is_unknown_not_zero():
    gateway = Gateway(load(), {}, estimator=TokenEstimator())
    cost = gateway.compute_cost("openai", "gpt-4o-2024-11-20", Usage())
    assert cost.source == "unknown"
    assert not cost.known, "an unmeasured cost must never read as free"


def test_unpriced_cloud_model_is_unknown():
    gateway = Gateway(load(), {}, estimator=TokenEstimator())
    cost = gateway.compute_cost("openai", "brand-new-model",
                                Usage(prompt_tokens=100, completion_tokens=50))
    assert cost.source == "unknown"


def test_provider_reported_cost_wins():
    gateway = Gateway(load(), {}, estimator=TokenEstimator())
    cost = gateway.compute_cost("x", "y", Usage(prompt_tokens=1, completion_tokens=1,
                                                reported_cost_usd=0.5))
    assert cost.source == "measured" and cost.usd == 0.5


def test_ledger_separates_unknown_from_zero():
    ledger = CostLedger()
    ledger.charge(Cost(usd=0.01, source="estimated", prompt_tokens=10, completion_tokens=5,
                       model="m", provider_id="p"))
    ledger.charge(Cost(usd=0.0, source="unknown", model="m", provider_id="p"))
    snapshot = ledger.snapshot()
    assert snapshot["total_usd"] == 0.01
    assert not snapshot["cost_complete"], "an unmeasured call must mark the total incomplete"
    assert snapshot["unknown_cost_calls"] == 1


def test_ledger_attributes_by_agent_and_model():
    ledger = CostLedger()
    ledger.charge(Cost(usd=0.02, source="estimated", prompt_tokens=1, completion_tokens=1,
                       model="m1", provider_id="p"), agent_id="ag_1")
    ledger.charge(Cost(usd=0.03, source="estimated", prompt_tokens=1, completion_tokens=1,
                       model="m2", provider_id="p"), agent_id="ag_1")
    snapshot = ledger.snapshot()
    assert snapshot["by_agent_usd"]["ag_1"] == pytest.approx(0.05)
    assert set(snapshot["by_model_usd"]) == {"m1", "m2"}


# ── gateway ──────────────────────────────────────────────────────────────────


def test_gateway_budget_stops_before_spending():
    gateway = Gateway(load(), {}, estimator=TokenEstimator())
    gateway.run_max_usd = 0.001
    gateway.ledger.charge(Cost(usd=0.5, source="estimated", model="m", provider_id="p"))
    with pytest.raises(BudgetExceeded):
        gateway.check_budget()


def test_gateway_emits_cost_ceiling_event():
    bus = EventBus(run_id="r")
    gateway = Gateway(load(), {}, bus=bus, estimator=TokenEstimator())
    gateway.run_max_usd = 0.001
    gateway.ledger.charge(Cost(usd=1.0, source="estimated", model="m", provider_id="p"))
    with pytest.raises(BudgetExceeded):
        gateway.check_budget()
    assert any(e.type_value == "cost.ceiling" for e in bus.history())


def test_gateway_complete_end_to_end_with_fake_provider():
    bus = EventBus(run_id="r")
    fake = FakeProvider(script=[{"text": "done", "prompt_tokens": 150, "completion_tokens": 40}])
    gateway = Gateway(load(), {"fake": fake}, bus=bus, estimator=TokenEstimator())
    response = gateway.complete(
        ChatRequest(model="fake-model", system="sys", messages=[Message.text_message(Role.USER, "go")]),
        provider_id="fake", agent_id="ag_1", node_id="fixer",
    )
    assert response.text == "done"
    assert gateway.ledger.calls == 1
    assert gateway.ledger.total_tokens == 190
    types = [e.type_value for e in bus.history()]
    assert "llm.request" in types and "llm.response" in types


def test_gateway_unknown_provider_names_the_available_ones():
    gateway = Gateway(load(), {"ollama": _ollama(StubTransport())}, estimator=TokenEstimator())
    with pytest.raises(GatewayError) as excinfo:
        gateway.provider_for("ghost")
    assert "ollama" in str(excinfo.value)


def test_gateway_explains_a_skipped_provider():
    gateway = Gateway(load(), {}, estimator=TokenEstimator())
    gateway.note_skipped(["openai: no API key. Set the $OPENAI_API_KEY environment variable."])
    with pytest.raises(GatewayError) as excinfo:
        gateway.provider_for("openai")
    assert excinfo.value.kind is ErrorKind.AUTH
    assert "OPENAI_API_KEY" in str(excinfo.value)


def test_gateway_alias_expansion_reaches_the_provider():
    transport = StubTransport({"chat/completions": {
        "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
    }})
    gateway = Gateway(load(), {"openai": _openai(transport)}, estimator=TokenEstimator())
    gateway.complete(ChatRequest(model="gpt-4o", messages=[]), provider_id="openai")
    sent_model = transport.calls[-1][2]["model"]
    assert sent_model == "gpt-4o-2024-11-20", "the alias must be resolved before sending"


def test_gateway_emits_a_classified_error_with_retryability():
    bus = EventBus(run_id="r")
    fake = FakeProvider(script=[{"error": GatewayError(ErrorKind.RATE_LIMIT, "slow down")}])
    gateway = Gateway(load(), {"fake": fake}, bus=bus, estimator=TokenEstimator())
    with pytest.raises(GatewayError):
        gateway.complete(ChatRequest(model="m", messages=[]), provider_id="fake")
    errors = [e for e in bus.history() if e.type_value == "error"]
    assert errors and errors[-1].payload["kind"] == "rate_limit"
    assert errors[-1].payload["retryable"] is True


def test_gateway_marks_a_context_overflow_as_needing_compaction():
    bus = EventBus(run_id="r")
    fake = FakeProvider(script=[{"error": GatewayError(ErrorKind.CONTEXT_LENGTH, "too long")}])
    gateway = Gateway(load(), {"fake": fake}, bus=bus, estimator=TokenEstimator())
    with pytest.raises(GatewayError):
        gateway.complete(ChatRequest(model="m", messages=[]), provider_id="fake")
    errors = [e for e in bus.history() if e.type_value == "error"]
    assert errors[-1].payload["needs_compaction"] is True
    assert errors[-1].payload["retryable"] is False


def test_gateway_stream_accounts_cost_on_the_terminal_chunk():
    bus = EventBus(run_id="r")
    fake = FakeProvider(script=[{"text": "abc", "prompt_tokens": 11, "completion_tokens": 3}])
    gateway = Gateway(load(), {"fake": fake}, bus=bus, estimator=TokenEstimator())
    chunks = list(gateway.stream(ChatRequest(model="m", messages=[Message.text_message(Role.USER, "go")]),
                                 provider_id="fake", agent_id="ag_1"))
    assert "".join(c.text for c in chunks) == "abc"
    assert gateway.ledger.calls == 1
    assert gateway.ledger.total_tokens == 14


def test_gateway_does_not_mutate_the_request_model():
    fake = FakeProvider(script=[{"text": "x"}])
    gateway = Gateway(load(), {"fake": fake}, estimator=TokenEstimator())
    request = ChatRequest(model="alias-name", messages=[])
    gateway.complete(request, provider_id="fake")
    assert request.model == "alias-name"


# ── catalog ──────────────────────────────────────────────────────────────────


def test_catalog_unknown_window_is_never_invented():
    transport = StubTransport({"/models": {"data": [{"id": "mystery-model"}]}})
    catalog = ModelCatalog(load(), {"openai": _openai(transport)})
    entries = catalog.list_models(provider_id="openai")
    mystery = next(e for e in entries if e.model_id == "mystery-model")
    assert mystery.context_window is None
    assert not mystery.window_known
    assert mystery.source == "assumed"


def test_catalog_declared_window_is_marked_declared():
    transport = StubTransport({"/models": {"data": [{"id": "gpt-4o-2024-11-20"}]}})
    catalog = ModelCatalog(load(), {"openai": _openai(transport)})
    entry = catalog.resolve("openai", "gpt-4o-2024-11-20")
    assert entry.context_window == 128000
    assert entry.source == "declared"


def test_catalog_probes_ollama_for_the_real_window():
    transport = StubTransport({
        "/api/tags": {"models": [{"name": "qwen2.5-coder:7b", "size": 4_000_000_000,
                                  "details": {"quantization_level": "Q4_K_M",
                                              "parameter_size": "7.6B"}}]},
        "/api/show": {"model_info": {"qwen2.context_length": 32768}},
    })
    catalog = ModelCatalog(load(), {"ollama": _ollama(transport)})
    entry = catalog.resolve("ollama", "qwen2.5-coder:7b")
    assert entry.context_window == 32768
    assert entry.source == "probed", "a probed window must be marked as probed"
    assert entry.quantization == "Q4_K_M"
    assert entry.parameter_size == "7.6B"
    assert entry.locality == "local"


def test_catalog_dispatches_on_declared_kind_not_class_name():
    """A provider must be probed by its protocol, not by its Python class name."""
    transport = StubTransport({
        "/api/tags": {"models": [{"name": "m", "details": {}}]},
        "/api/show": {"model_info": {"x.context_length": 4096}},
    })
    provider = _ollama(transport)
    assert provider.kind == "ollama"
    catalog = ModelCatalog(load(), {"ollama": provider})
    assert catalog.resolve("ollama", "m").source == "probed"


def test_catalog_falls_back_offline_when_the_provider_is_down():
    transport = StubTransport(error=GatewayError(ErrorKind.CONNECTION, "refused"))
    catalog = ModelCatalog(load(), {"lmstudio": _openai(transport)})
    entries = catalog.list_models(provider_id="lmstudio")
    assert entries, "the app must still offer models when a provider is down"
    assert all(e.context_window is not None for e in entries)


def test_catalog_caches_and_can_be_refreshed():
    transport = StubTransport({"/models": {"data": [{"id": "m"}]}})
    catalog = ModelCatalog(load(), {"openai": _openai(transport)})
    catalog.list_models(provider_id="openai")
    calls_after_first = len(transport.calls)
    catalog.list_models(provider_id="openai")
    assert len(transport.calls) == calls_after_first, "a cached read must not re-probe"
    catalog.list_models(provider_id="openai", refresh=True)
    assert len(transport.calls) > calls_after_first


def test_catalog_only_known_windows_filter_hides_unbindable_models():
    transport = StubTransport({"/models": {"data": [
        {"id": "gpt-4o-2024-11-20"}, {"id": "mystery"}]}})
    catalog = ModelCatalog(load(), {"openai": _openai(transport)})
    all_entries = catalog.list_models(provider_id="openai")
    bindable = catalog.list_models(provider_id="openai", only_known_windows=True)
    assert len(bindable) < len(all_entries)
    assert all(e.window_known for e in bindable)


def test_catalog_never_raises_on_a_broken_provider():
    transport = StubTransport(error=RuntimeError("kaboom"))
    catalog = ModelCatalog(load(), {"openai": _openai(transport)})
    assert catalog.list_models(provider_id="openai") is not None
    assert catalog.status()["openai"]["status"].startswith("error")


def test_catalog_status_reports_offline_vs_probed():
    ok = ModelCatalog(load(), {"ollama": _ollama(StubTransport({
        "/api/tags": {"models": []},
    }))})
    ok.list_models(provider_id="ollama")
    assert ok.status()["ollama"]["status"] == "probed+configured"

    broken = ModelCatalog(load(), {"openai": _openai(
        StubTransport(error=GatewayError(ErrorKind.CONNECTION, "x")))})
    broken.list_models(provider_id="openai")
    assert broken.status()["openai"]["status"] == "down+configured"


# ── fake provider ────────────────────────────────────────────────────────────


def test_fake_provider_consumes_script_then_default():
    provider = FakeProvider(script=[{"text": "a"}, {"text": "b"}], default_reply={"text": "rest"})
    request = ChatRequest(model="m", messages=[])
    assert [provider.complete(request).text for _ in range(3)] == ["a", "b", "rest"]


def test_fake_provider_records_requests_for_content_assertions():
    provider = FakeProvider(script=[{"text": "x"}])
    provider.complete(ChatRequest(model="m", system="SYS-MARKER",
                                  messages=[Message.text_message(Role.USER, "USER-MARKER")]))
    recorded = provider.requests[0]
    assert "SYS-MARKER" in recorded.system
    assert recorded.last_user_text == "USER-MARKER"
    assert "SYS-MARKER" in recorded.all_text


def test_fake_provider_raises_scripted_errors():
    provider = FakeProvider(script=[{"error": GatewayError(ErrorKind.SERVER, "boom")}])
    with pytest.raises(GatewayError):
        provider.complete(ChatRequest(model="m", messages=[]))


def test_fake_provider_streams_deterministically():
    """A scripted reply streams the same bytes every time it is consumed."""
    text = "x" * 40
    provider = FakeProvider(script=[{"text": text, "prompt_tokens": 5, "completion_tokens": 9},
                                    {"text": text, "prompt_tokens": 5, "completion_tokens": 9}])
    first = [c.text for c in provider.stream(ChatRequest(model="m", messages=[]))]
    second = [c.text for c in provider.stream(ChatRequest(model="m", messages=[]))]
    assert first == second
    assert "".join(first) == text


def test_json_reply_helper_produces_a_trailer():
    provider = FakeProvider(script=[json_reply({"status": "changes_requested",
                                                "findings": [{"id": "F1"}]})])
    payload = json.loads(provider.complete(ChatRequest(model="m", messages=[])).text)
    assert payload["status"] == "changes_requested"
    assert payload["findings"][0]["id"] == "F1"


def test_fake_provider_reset_clears_state():
    provider = FakeProvider(script=[{"text": "a"}])
    provider.complete(ChatRequest(model="m", messages=[]))
    provider.reset()
    assert provider.requests == []
    assert provider.complete(ChatRequest(model="m", messages=[])).text == "a"
