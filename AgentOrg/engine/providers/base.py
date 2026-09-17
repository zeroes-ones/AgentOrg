#!/usr/bin/env python3
"""base.py — the canonical request/response shape every provider adapter speaks.

WHY THIS EXISTS
---------------
Four provider APIs, one caller. OpenAI-compatible endpoints take `messages` with a
leading system role and return `choices[0].message`; Anthropic takes a top-level
`system` field, content blocks, and returns `content[]` plus `input_tokens`/
`output_tokens`; Ollama streams NDJSON and reports `eval_count`/`prompt_eval_count`.
If that variance leaked upward, every caller would need to know which provider it was
talking to — which is exactly what "model-agnostic" is supposed to prevent.

So the canonical types live here, the adapters translate at the edge, and nothing above
this package can tell the difference.

DESIGN
------
- **Usage is explicit about what is unknown.** `prompt_tokens=None` means *not
  reported*, which is different from zero. The cost layer refuses to treat the two
  alike, because a dashboard that reads "unmeasured" as "free" lies.
- **`locality` is a property of the provider, not the model**, so the scheduler can
  count local models from the provider config alone.
- **Errors carry a machine-readable kind.** Retry logic needs to distinguish a 429
  (back off, then retry) from a 401 (never retry) from a context-length overflow
  (shrink, don't repeat). A bare exception string cannot support that.
- **`ProviderCapabilities` is declared, and `None` means unprobed.** A provider that
  has not advertised tool support must not be assumed to have it.
- **The ABC is deliberately small**: `complete()`, `stream()`, `health()`,
  `capabilities`. A provider that needs more is doing something wrong.

Usage:
    req = ChatRequest(model="gpt-4o", messages=[Message(Role.USER, "hi")], system="be terse")
    resp = provider.complete(req)
    resp.text; resp.usage.total_tokens; resp.finish_reason
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator

__all__ = [
    "Role",
    "FinishReason",
    "ErrorKind",
    "GatewayError",
    "ToolSpec",
    "ToolCall",
    "ContentBlock",
    "Message",
    "ChatRequest",
    "Usage",
    "cache_tokens_from",
    "ChatResponse",
    "Chunk",
    "ProviderCapabilities",
    "Provider",
    "estimate_message_tokens",
]


class Role(str, Enum):
    """Canonical message role. `SYSTEM` is hoisted per provider by each adapter."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(str, Enum):
    """Why generation stopped, normalised across providers.

    Anthropic says `end_turn`/`max_tokens`/`tool_use` and OpenAI says
    `stop`/`length`/`tool_calls`; both map here so a caller can branch on one value.
    """

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    UNKNOWN = "unknown"


class ErrorKind(str, Enum):
    """Machine-readable failure classes, because retry policy depends on them."""

    AUTH = "auth"                     # 401/403 — never retry, the key is wrong
    RATE_LIMIT = "rate_limit"         # 429 — back off, honour Retry-After
    TIMEOUT = "timeout"               # network deadline — retry with backoff
    CONNECTION = "connection"         # DNS/refused/reset — retry with backoff
    CONTEXT_LENGTH = "context_length"  # too many tokens — shrink, do not repeat
    BAD_REQUEST = "bad_request"       # 400 — the caller built a bad request
    SERVER = "server"                 # 5xx — retry with backoff
    NOT_FOUND = "not_found"           # 404 — model or endpoint wrong, do not retry
    UNSUPPORTED = "unsupported"       # capability missing on this provider
    BAD_RESPONSE = "bad_response"     # 200 but unparseable — retry once, then fail
    CANCELLED = "cancelled"           # caller aborted
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        """Whether a failed call of this kind is worth retrying.

        `CONTEXT_LENGTH` is deliberately False: retrying an identical oversized request
        can only fail again. The caller must compact or rotate instead.
        """
        return self in (
            ErrorKind.RATE_LIMIT,
            ErrorKind.TIMEOUT,
            ErrorKind.CONNECTION,
            ErrorKind.SERVER,
            ErrorKind.BAD_RESPONSE,
        )


class GatewayError(RuntimeError):
    """A provider call failed, with a class that drives retry and escalation.

    Attributes
    ----------
    kind:
        The failure class.
    status:
        HTTP status when there was one.
    retry_after_s:
        Server-requested delay (from `Retry-After`), when supplied.
    provider_id / model:
        Which endpoint and model failed, for the error event and the health signals.
    """

    def __init__(self, kind: ErrorKind, message: str, *,
                 status: int | None = None,
                 retry_after_s: float | None = None,
                 provider_id: str | None = None,
                 model: str | None = None,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after_s = retry_after_s
        self.provider_id = provider_id
        self.model = model
        self.detail = detail or {}

    @property
    def retryable(self) -> bool:
        """Whether this specific failure is worth retrying."""
        return self.kind.retryable

    def to_dict(self) -> dict[str, Any]:
        """Wire form for the `error` event."""
        return {
            "kind": self.kind.value,
            "message": str(self),
            "status": self.status,
            "retry_after_s": self.retry_after_s,
            "provider_id": self.provider_id,
            "model": self.model,
            "retryable": self.retryable,
            "detail": self.detail,
        }

    @staticmethod
    def from_status(status: int, message: str, **kwargs: Any) -> "GatewayError":
        """Classify an HTTP status into the right :class:`ErrorKind`.

        Centralised here rather than in each adapter so all four providers agree on what
        a 429 means, which is what lets one retry policy serve them all.
        """
        if status in (401, 403):
            kind = ErrorKind.AUTH
        elif status == 429:
            kind = ErrorKind.RATE_LIMIT
        elif status == 400:
            kind = ErrorKind.BAD_REQUEST
        elif status == 404:
            kind = ErrorKind.NOT_FOUND
        elif status == 408:
            kind = ErrorKind.TIMEOUT
        elif 500 <= status < 600:
            kind = ErrorKind.SERVER
        else:
            kind = ErrorKind.UNKNOWN
        return GatewayError(kind, message, status=status, **kwargs)


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call, in a provider-neutral form."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass
class ToolCall:
    """A model's request to invoke a tool."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def arguments_json(self) -> str:
        """Arguments serialised as JSON, for replaying into a provider payload."""
        return json.dumps(self.arguments, separators=(",", ":"), sort_keys=True)


@dataclass
class ContentBlock:
    """One content block, covering both text and tool use.

    Anthropic's API is block-based natively; OpenAI's is string-based. Keeping blocks as
    the canonical form means neither adapter has to lose information, and a mixed
    text+tool_use assistant turn survives a round trip.
    """

    type: str                       # "text" | "tool_use" | "tool_result"
    text: str | None = None
    tool_call: ToolCall | None = None
    tool_call_id: str | None = None


@dataclass
class Message:
    """One conversational turn.

    `content` is canonical as a list of blocks; :meth:`text` flattens it. Adapters that
    want a plain string call `.text`; adapters that want blocks use `.content` directly.
    """

    role: Role
    content: list[ContentBlock] = field(default_factory=list)
    name: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None

    @classmethod
    def text_message(cls, role: Role, text: str) -> "Message":
        """Convenience constructor for a plain text turn."""
        return cls(role=role, content=[ContentBlock(type="text", text=text)])

    @property
    def text(self) -> str:
        """The message's text, including any tool result.

        `tool_result` blocks are included deliberately. They carry the *output* of a tool call, and
        omitting them made the whole tool loop blind: the assistant asked to read a file, the result
        was attached as a `tool_result` block, this property returned `""`, and the OpenAI-shaped
        payload sent `content: null`. The model then saw a tool call it had made and no answer to it —
        which is both useless and, on Ollama, an outright HTTP 400 because the template cannot render
        a tool message with no content.
        """
        return "".join(block.text or "" for block in self.content
                       if block.type in ("text", "tool_result"))

    def is_empty(self) -> bool:
        """True when the message carries neither text nor tool activity."""
        return not self.text and not self.tool_calls and not self.content

    def as_openai(self) -> dict[str, Any]:
        """Render for an OpenAI-compatible request payload."""
        data: dict[str, Any] = {"role": self.role.value, "content": self.text or None}
        if self.name:
            data["name"] = self.name
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments_json},
                }
                for call in self.tool_calls
            ]
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        return data


def estimate_message_tokens(messages: Iterable[Message], *, chars_per_token: float = 4.0) -> int:
    """Cheap, provider-independent token estimate for a message list.

    Deliberately a heuristic: it exists so the pre-flight projection has *a* number
    before the first call returns real usage, and it is corrected against that usage
    afterwards. `chars_per_token=4` is the usual English approximation.
    """
    total_chars = 0
    count = 0
    for message in messages:
        count += 1
        total_chars += len(message.text)
        for call in message.tool_calls:
            total_chars += len(call.arguments_json) + len(call.name)
    # Rough per-message overhead for role/turn framing.
    return int(total_chars / max(1.0, chars_per_token)) + count * 4


def cache_tokens_from(usage: dict[str, Any] | None, *, kind: str = "openai") -> tuple[int | None, int | None]:
    """Read `(cache_hit_tokens, cache_miss_tokens)` from a provider's usage body.

    One implementation for every dialect, because the three spell the same fact differently and a
    per-adapter copy is how one of them silently stops being parsed:

    - **DeepSeek** (and OpenAI-compatible servers that follow it):
      ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``.
    - **OpenAI**: ``prompt_tokens_details.cached_tokens`` — a hit count with no explicit miss, so the
      miss is derived from ``prompt_tokens - cached``. Deriving it is honest here because OpenAI does
      report ``prompt_tokens``; where it did not, miss stays ``None``.
    - **Anthropic**: ``cache_read_input_tokens`` (a hit) and ``cache_creation_input_tokens`` (a
      write). A read with no miss reported leaves miss ``None`` rather than inventing one.

    Returns ``(None, None)`` when the body says nothing about caching, which is the signal the cost
    and display layers use to stay silent instead of claiming a 0% hit rate.
    """
    if not isinstance(usage, dict):
        return None, None

    def _int(value: Any) -> int | None:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    if kind == "anthropic":
        hit = _int(usage.get("cache_read_input_tokens"))
        # A cache *write* is not a miss; it is the cost of making the prefix cacheable. Anthropic
        # reports it separately and the cost layer bills it separately, so it is not folded in here.
        return hit, None

    hit = _int(usage.get("prompt_cache_hit_tokens"))
    miss = _int(usage.get("prompt_cache_miss_tokens"))
    if hit is not None or miss is not None:
        return hit, miss

    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = _int(details.get("cached_tokens"))
        if cached is not None:
            prompt = _int(usage.get("prompt_tokens"))
            return cached, (max(0, prompt - cached) if prompt is not None else None)

    cached = _int(usage.get("cached_tokens"))
    if cached is not None:
        prompt = _int(usage.get("prompt_tokens"))
        return cached, (max(0, prompt - cached) if prompt is not None else None)
    return None, None


@dataclass
class ChatRequest:
    """A canonical completion request."""

    model: str
    messages: list[Message] = field(default_factory=list)
    system: str | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    temperature: float | None = 0.2
    max_tokens: int | None = None
    stream: bool = False
    # Ask the provider for a JSON object when it supports it. Used for the agent's
    # machine-readable trailer; a provider without the capability silently ignores it
    # and the orchestrator falls back to extracting the fenced block.
    json_mode: bool = False
    stop: list[str] = field(default_factory=list)
    # Free-form passthrough for provider-specific knobs (e.g. Ollama `keep_alive`).
    extra: dict[str, Any] = field(default_factory=dict)

    def estimated_prompt_tokens(self) -> int:
        """Estimate prompt size, including the system prompt."""
        messages = list(self.messages)
        if self.system:
            messages.insert(0, Message.text_message(Role.SYSTEM, self.system))
        tokens = estimate_message_tokens(messages)
        for tool in self.tools:
            tokens += len(json.dumps(tool.as_dict())) // 4
        return tokens


@dataclass
class Usage:
    """Token accounting for one call.

    ``None`` means *not reported by the provider*, which is not the same as zero. The
    cost layer surfaces that distinction all the way to the UI.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    # Provider-reported cost, when a local provider computes one. Almost always None.
    reported_cost_usd: float | None = None
    # ── prompt caching ──
    # How many of `prompt_tokens` were served from the provider's prefix cache, and how many were
    # not. These are the *only* honest source for a cache-hit rate: the difference between them is
    # what a cache-aware cost calculation bills at a lower rate, and it is what a UI must show
    # rather than an inferred figure.
    #
    # `None` means the provider did not report it — which is not the same as zero. A provider that
    # reports nothing must never be rendered as a total miss, for the same reason an unreported cost
    # is never rendered as free.
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    # Tokens written *into* the cache this call. Anthropic charges a premium for these; DeepSeek and
    # OpenAI do not bill them separately. Kept distinct because the cost models differ.
    cache_write_tokens: int | None = None

    def __post_init__(self) -> None:
        # Fill the total only when both parts are known — inferring a total from one
        # known part would fabricate a number.
        if self.total_tokens is None and self.prompt_tokens is not None and self.completion_tokens is not None:
            self.total_tokens = self.prompt_tokens + self.completion_tokens

    @property
    def measured(self) -> bool:
        """True when the provider reported real token counts."""
        return self.prompt_tokens is not None and self.completion_tokens is not None

    @property
    def cache_reported(self) -> bool:
        """Whether the provider told us anything about caching for this call.

        The distinction matters at the display layer: a hit rate computed over *unreported* calls
        would be a number invented from silence.
        """
        return self.cache_hit_tokens is not None or self.cache_miss_tokens is not None

    @property
    def cache_hit_rate(self) -> float | None:
        """Hit tokens as a fraction of cached-eligible input, or None when unreported."""
        if not self.cache_reported:
            return None
        hit = self.cache_hit_tokens or 0
        miss = self.cache_miss_tokens or 0
        total = hit + miss
        if total <= 0:
            # Everything reported but nothing counted: a miss of an empty prompt is a rate of 0,
            # not an undefined one, but only when a count was actually reported.
            return 0.0
        return hit / total

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "measured": self.measured,
            "reported_cost_usd": self.reported_cost_usd,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_reported": self.cache_reported,
            "cache_hit_rate": (round(self.cache_hit_rate, 4)
                               if self.cache_hit_rate is not None else None),
        }

    def merge(self, other: "Usage") -> "Usage":
        """Add another usage into this one, treating unknown as zero *for the sum only*.

        The `measured` property remains False if either side was unmeasured, so an
        aggregate containing an unmeasured call still reports itself as partly unknown.
        """
        def add(a: int | None, b: int | None) -> int | None:
            if a is None and b is None:
                return None
            return (a or 0) + (b or 0)

        cost = None
        if self.reported_cost_usd is not None or other.reported_cost_usd is not None:
            cost = (self.reported_cost_usd or 0.0) + (other.reported_cost_usd or 0.0)
        merged = Usage(
            prompt_tokens=add(self.prompt_tokens, other.prompt_tokens),
            completion_tokens=add(self.completion_tokens, other.completion_tokens),
            reported_cost_usd=cost,
        )
        # Preserve partial-knowledge: if either side lacked a part, the sum lacks it too.
        if not (self.measured and other.measured):
            if self.prompt_tokens is None or other.prompt_tokens is None:
                merged.prompt_tokens = None
            if self.completion_tokens is None or other.completion_tokens is None:
                merged.completion_tokens = None
            merged.total_tokens = None
        return merged


@dataclass
class ChatResponse:
    """A canonical completion response."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: FinishReason = FinishReason.STOP
    model: str = ""
    provider_id: str = ""
    # Verbatim provider payload, kept for debugging and for the trace. Redacted by the
    # bus before it is persisted, never logged raw.
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Wire form for the `llm.response` event."""
        return {
            "model": self.model,
            "provider_id": self.provider_id,
            "finish_reason": self.finish_reason.value,
            "usage": self.usage.as_dict(),
            "latency_ms": self.latency_ms,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls
            ],
            "text_chars": len(self.text),
        }


@dataclass
class Chunk:
    """One streamed delta."""

    text: str = ""
    tool_call: ToolCall | None = None
    finish_reason: FinishReason | None = None
    usage: Usage | None = None
    index: int = 0


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider can do, as declared or probed. ``None`` means unprobed.

    `None` is load-bearing: an adapter that has not verified tool support must not
    claim it, or the orchestrator will send tools to an endpoint that ignores them and
    silently lose the structured trailer.
    """

    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    supports_json_mode: bool | None = None
    supports_vision: bool | None = None
    reports_usage: bool | None = None
    locality: str = "cloud"          # "local" | "cloud"
    source: str = "declared"         # "declared" | "probed" | "assumed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "supports_json_mode": self.supports_json_mode,
            "supports_vision": self.supports_vision,
            "reports_usage": self.reports_usage,
            "locality": self.locality,
            "source": self.source,
        }


class Provider(ABC):
    """The interface every provider adapter implements.

    Deliberately narrow. `complete` and `stream` are the whole surface a caller needs;
    `health` exists so the first-run wizard and the model picker can tell "not running"
    from "misconfigured"; `capabilities` lets the planner avoid asking for features a
    provider cannot deliver.
    """

    #: Stable identifier matching the key in `credentials.json`.
    provider_id: str = ""
    #: Wire-format family: "openai" | "anthropic" | "ollama" | "fake". Declared as a
    #: class attribute so dispatch never depends on the concrete class name — a subclass
    #: or a test proxy must still be probed by its real protocol.
    kind: str = ""

    @abstractmethod
    def complete(self, request: ChatRequest) -> ChatResponse:
        """Perform a non-streaming completion. Raises :class:`GatewayError` on failure."""

    @abstractmethod
    def stream(self, request: ChatRequest) -> Iterator[Chunk]:
        """Stream a completion. Raises :class:`GatewayError` on failure.

        Implementations must yield a terminal chunk carrying `finish_reason` and, where
        the provider reports it, the final `usage`.
        """

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Report reachability and configuration state without raising.

        Must never raise: it is called by the UI on a timer, and a provider being down is
        information, not an error.
        """

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Declared capabilities. Cheap; must not perform network I/O."""

    # ── shared helpers ──────────────────────────────────────────────────────

    def stream_text(self, request: ChatRequest) -> Iterator[str]:
        """Convenience: stream only the text deltas."""
        for chunk in self.stream(request):
            if chunk.text:
                yield chunk.text

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"{type(self).__name__}(provider_id={self.provider_id!r})"
