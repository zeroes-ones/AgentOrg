#!/usr/bin/env python3
"""fake.py — a deterministic provider for tests, so the suite needs no network.

WHY THIS EXISTS
---------------
Every meaningful behaviour in this engine — the review-rework loop, session rotation,
delegation, health recovery — needs to be driven many times with controlled outputs.
Calling a real provider would make the suite slow, flaky, non-deterministic and
dependent on credentials, which means it would stop being run.

So the test double is a first-class adapter, not a mock scattered through tests:

- **Scripted responses** are queued per call, so a test can say "reject, then reject, then
  approve" and assert the loop's behaviour exactly.
- **Requests are recorded** so a test can assert *what the engine actually sent* — the
  checklist IDs, the primed guardrails, the failure log — which is the part that matters.
- **Failures are first-class**, because the interesting paths are the ones where a
  provider rate-limits, times out, or returns malformed JSON.
- **Determinism is explicit**: identical inputs produce identical outputs, so a failing
  test is reproducible rather than a coin flip.

Usage:
    fake = FakeProvider(provider_id="fake", script=[{"text": '{"status":"changes_requested"}'}])
    response = fake.complete(ChatRequest(model="fake-model", messages=[...]))
    assert "CR1" in fake.requests[0].messages[-1].text
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .base import (
    ChatRequest,
    ChatResponse,
    Chunk,
    ErrorKind,
    FinishReason,
    GatewayError,
    Provider,
    ProviderCapabilities,
    Usage,
)

__all__ = ["FakeProvider", "RecordedRequest", "ScriptedReply"]


@dataclass
class ScriptedReply:
    """One queued reply.

    Exactly one shape is used per reply: `text`, `error`, or `chunks`. `usage_tokens`
    drives the reported accounting, and leaving it None reproduces a provider that does
    not report usage at all — which is a case the cost layer must handle.
    """

    text: str = ""
    error: GatewayError | None = None
    chunks: list[str] = field(default_factory=list)
    finish_reason: FinishReason = FinishReason.STOP
    prompt_tokens: int | None = 120
    completion_tokens: int | None = 30
    model: str = "fake-model"
    latency_ms: float = 1.0
    # A malformed body, to exercise the BAD_RESPONSE path.
    raw_body: dict[str, Any] | None = None


@dataclass
class RecordedRequest:
    """A request the engine actually sent, for assertions about its content."""

    request: ChatRequest
    streamed: bool = False

    @property
    def system(self) -> str:
        return self.request.system or ""

    @property
    def last_user_text(self) -> str:
        for message in reversed(self.request.messages):
            if message.text:
                return message.text
        return ""

    @property
    def all_text(self) -> str:
        """Every message concatenated — convenient for `assert "CR1" in ...` checks."""
        parts = [self.system] if self.system else []
        parts.extend(m.text for m in self.request.messages)
        return "\n".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.request.model,
            "streamed": self.streamed,
            "system_chars": len(self.system),
            "message_count": len(self.request.messages),
            "temperature": self.request.temperature,
            "max_tokens": self.request.max_tokens,
            "tool_count": len(self.request.tools),
        }


class FakeProvider(Provider):
    """A deterministic in-process provider.

    Parameters
    ----------
    script:
        Replies or dicts consumed in order. When exhausted, `default_reply` is used, so a
        test does not have to enumerate every call.
    default_reply:
        The reply once the script runs out.
    on_request:
        Optional hook called with each :class:`RecordedRequest`, for assertions or to
        compute a reply from the request.
    """

    kind = "fake"

    def __init__(self, *, provider_id: str = "fake",
                 script: list[ScriptedReply | dict[str, Any]] | None = None,
                 default_reply: ScriptedReply | dict[str, Any] | None = None,
                 locality: str = "local",
                 on_request: Callable[[RecordedRequest], None] | None = None) -> None:
        self.provider_id = provider_id
        self.locality = locality
        self._script: list[ScriptedReply] = [_coerce(r) for r in (script or [])]
        self._default = _coerce(default_reply) if default_reply is not None else None
        self.requests: list[RecordedRequest] = []
        self._on_request = on_request
        self._cursor = 0

    # ── scripting ───────────────────────────────────────────────────────────

    def push(self, reply: ScriptedReply | dict[str, Any]) -> None:
        """Append a reply to the script. Lets a test react mid-run."""
        self._script.append(_coerce(reply))

    def set_default(self, reply: ScriptedReply | dict[str, Any]) -> None:
        """Set the reply used once the script is exhausted."""
        self._default = _coerce(reply)

    def reset(self) -> None:
        """Clear recorded requests and restart the script from the beginning."""
        self.requests.clear()
        self._cursor = 0

    def _next(self) -> ScriptedReply:
        if self._cursor < len(self._script):
            reply = self._script[self._cursor]
            self._cursor += 1
            return reply
        if self._default is not None:
            return self._default
        # An unscripted call is an unscripted call *anywhere*; saying so beats silently
        # returning empty text and letting a downstream assertion fail obscurely.
        return ScriptedReply(text=f"[unscripted reply #{self._cursor}]")

    def _record(self, request: ChatRequest, *, streamed: bool) -> RecordedRequest:
        recorded = RecordedRequest(request=request, streamed=streamed)
        self.requests.append(recorded)
        if self._on_request is not None:
            self._on_request(recorded)
        return recorded

    # ── Provider interface ──────────────────────────────────────────────────

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Return the next scripted reply, or raise its scripted error."""
        self._record(request, streamed=False)
        reply = self._next()
        if reply.error is not None:
            raise reply.error
        if reply.raw_body is not None:
            # Simulates a provider that returns 200 with an unparseable body.
            raise GatewayError(
                ErrorKind.BAD_RESPONSE,
                "fake provider returned a malformed body",
                provider_id=self.provider_id,
                detail={"keys": sorted(reply.raw_body.keys())},
            )
        return ChatResponse(
            text=reply.text,
            usage=Usage(prompt_tokens=reply.prompt_tokens, completion_tokens=reply.completion_tokens),
            finish_reason=reply.finish_reason,
            model=reply.model,
            provider_id=self.provider_id,
            raw={"fake": True, "text_chars": len(reply.text)},
            latency_ms=reply.latency_ms,
        )

    def stream(self, request: ChatRequest) -> Iterator[Chunk]:
        """Stream the next scripted reply as chunks.

        When the reply has no explicit `chunks`, the text is split deterministically into
        fixed-size pieces so reassembly can be tested without hand-authoring fragments.
        """
        self._record(request, streamed=True)
        reply = self._next()
        if reply.error is not None:
            raise reply.error

        pieces = reply.chunks
        if not pieces and reply.text:
            pieces = [reply.text[i:i + 16] for i in range(0, len(reply.text), 16)]
        for index, piece in enumerate(pieces):
            yield Chunk(text=piece, index=index)
        yield Chunk(
            finish_reason=reply.finish_reason,
            usage=Usage(prompt_tokens=reply.prompt_tokens, completion_tokens=reply.completion_tokens),
            index=len(pieces),
        )

    def health(self) -> dict[str, Any]:
        """Always reachable — the point of a fake is to remove that variable."""
        return {
            "provider_id": self.provider_id,
            "kind": "fake",
            "base_url": "in-process",
            "locality": self.locality,
            "has_key": True,
            "status": "ok",
            "model_count": 1,
        }

    def capabilities(self) -> ProviderCapabilities:
        """Fully capable, so tests are not accidentally limited by the double."""
        return ProviderCapabilities(
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            reports_usage=True,
            locality=self.locality,
            source="probed",
        )


def _coerce(reply: ScriptedReply | dict[str, Any]) -> ScriptedReply:
    """Build a :class:`ScriptedReply` from a dict, ignoring unknown keys.

    Convenience for the common case: `script=[{"text": "..."}]`.
    """
    if isinstance(reply, ScriptedReply):
        return reply
    known = {f for f in ScriptedReply.__dataclass_fields__}  # type: ignore[attr-defined]
    return ScriptedReply(**{k: v for k, v in reply.items() if k in known})


def json_reply(payload: dict[str, Any], **kwargs: Any) -> ScriptedReply:
    """Build a reply whose text is the JSON serialisation of `payload`.

    Agent replies are structured trailers, so tests overwhelmingly want this shape.
    """
    return ScriptedReply(text=json.dumps(payload), **kwargs)
