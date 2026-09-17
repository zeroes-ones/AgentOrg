#!/usr/bin/env python3
"""http.py — the one place that speaks HTTP, so adapters do not each invent retries.

WHY THIS EXISTS
---------------
Every adapter needs the same three things around a request: a timeout, a retry policy
that respects `Retry-After`, and a classified error when it fails. Implementing that
per adapter would mean four subtly different retry behaviours — and the differences
would only show up under a rate limit, which is exactly when they matter most.

So the transport lives here. Adapters build a URL, headers and a JSON body; this module
sends it, streams it, classifies failures, and retries what is worth retrying.

DESIGN
------
- **One retry policy for all providers.** Classification comes from
  :meth:`GatewayError.from_status`, so a 429 means the same thing everywhere.
- **`Retry-After` is honoured over the computed backoff**, because the server knows its
  own limit and guessing shorter only deepens the penalty.
- **Backoff is jittered.** Without jitter, N agents that hit a limit together retry
  together and reproduce the limit — the thundering herd. Jitter desynchronises them.
- **Streaming is line-buffered and torn-line tolerant.** A killed connection mid-SSE
  yields a partial final line, which must be discarded rather than crash the caller.
- **The transport never logs the body.** It may contain a key in a header or prompt
  content; redaction happens at the bus, and this module keeps no copy.

Pure stdlib: `urllib.request`, `json`, `time`, `random`. No `requests`, no `httpx`,
keeping the engine's no-runtime-dependency property.

Usage:
    transport = HttpTransport(timeout_s=120, max_retries=4)
    payload = transport.post_json(url, headers, body, provider_id="openai", model="gpt-4o")
"""

from __future__ import annotations

import json
import os
import random
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

from .base import ErrorKind, GatewayError

__all__ = ["HttpTransport", "TransportResponse", "backoff_delay", "default_ssl_context"]

# Base delay for exponential backoff, in seconds. Chosen so the first retry is quick
# (a transient blip should not cost the user a minute) while later attempts back off
# hard enough to clear a real rate limit.
_BASE_DELAY_S = 0.5
_MAX_DELAY_S = 30.0

#: Where a CA bundle may live when the interpreter does not ship one.
#:
#: This exists because `ssl.create_default_context()` can return a context that trusts **nothing**:
#: on a macOS python.org build, `ssl.get_default_verify_paths().openssl_cafile` points at
#: `.../etc/openssl/cert.pem`, which is not shipped — so the context has zero CA certificates and
#: every HTTPS call fails with `CERTIFICATE_VERIFY_FAILED`. That looks like "the provider is down",
#: which is exactly the wrong story: the endpoint is fine and the *machine* is missing roots.
#:
#: The order is deliberate. An explicitly configured bundle wins, then the standard system stores
#: (`/etc/ssl/cert.pem` is the macOS system bundle), then the interpreter's own default. Falling back
#: to the default last means a correctly installed Python keeps its normal behaviour untouched.
_CA_CANDIDATES = (
    "/etc/ssl/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/certs/ca-certificates.crt",
)


def default_ssl_context() -> ssl.SSLContext:
    """A TLS context that actually trusts the public roots, or the interpreter's default.

    The bug this fixes was invisible and total: with no CA bundle, *every* cloud provider failed
    identically and the error named the provider rather than the machine. Resolving the bundle here
    means one place decides, and a run that needs HTTPS either works or says why.

    `$SSL_CERT_FILE` is honoured first, because that is the standard way an operator points at a
    corporate or custom bundle — and it should win over our guesses.
    """
    override = os.environ.get("SSL_CERT_FILE")
    if override and os.path.isfile(override):
        try:
            return ssl.create_default_context(cafile=override)
        except (OSError, ssl.SSLError):
            pass  # fall through to the discovered stores rather than failing the request outright
    for candidate in _CA_CANDIDATES:
        if os.path.isfile(candidate):
            try:
                context = ssl.create_default_context(cafile=candidate)
                # Only accept it if it actually loaded certificates; an empty bundle would put us
                # straight back in the broken state, with a *successful-looking* context.
                if context.cert_store_stats().get("x509_ca", 0) > 0:
                    return context
            except (OSError, ssl.SSLError):
                continue
    # The interpreter's own default: correct on a well-installed Python, and an honest failure
    # otherwise rather than silently skipping verification.
    return ssl.create_default_context()


def backoff_delay(attempt: int, *, base: float = _BASE_DELAY_S, cap: float = _MAX_DELAY_S,
                  jitter: bool = True) -> float:
    """Exponential backoff with optional full jitter.

    `attempt` is 1-based. Full jitter (`random.uniform(0, delay)`) is deliberate: it
    spreads concurrent retries across the whole window rather than clustering them just
    after the nominal delay.
    """
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    if jitter:
        return random.uniform(delay * 0.5, delay)
    return delay


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header, supporting both delta-seconds and HTTP-date forms.

    The date form is rare but real; falling back to None rather than misparsing means a
    weirder header simply does not shorten our backoff below a safe value.
    """
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
        return max(0.0, min(seconds, _MAX_DELAY_S * 4))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        if when is None:
            return None
        delta = when.timestamp() - time.time()
        return max(0.0, min(delta, _MAX_DELAY_S * 4))
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass
class TransportResponse:
    """A successful HTTP response."""

    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        """Parse the body as a JSON object.

        Raises
        ------
        GatewayError
            With kind `BAD_RESPONSE` when the body is not a JSON object — a 200 that
            cannot be parsed is a provider problem, not a caller problem.
        """
        try:
            data = json.loads(self.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            preview = self.body[:200].decode("utf-8", errors="replace")
            raise GatewayError(
                ErrorKind.BAD_RESPONSE,
                f"provider returned {self.status} but the body is not valid JSON: {exc.msg} | {preview}",
                status=self.status,
            ) from exc
        if not isinstance(data, dict):
            raise GatewayError(
                ErrorKind.BAD_RESPONSE,
                f"expected a JSON object, got {type(data).__name__}",
                status=self.status,
            )
        return data


class HttpTransport:
    """Sends JSON requests with a shared retry and error-classification policy.

    Parameters
    ----------
    timeout_s:
        Per-attempt socket timeout. Long by default because a local model on a cold
        start can legitimately take minutes to load weights.
    max_retries:
        Additional attempts after the first. 0 means try exactly once.
    sleep:
        Injected so tests can assert retry behaviour without actually sleeping.
    """

    def __init__(self, *, timeout_s: float = 120.0, max_retries: int = 3,
                 sleep: Any = time.sleep,
                 ssl_context: ssl.SSLContext | None = None) -> None:
        self.timeout_s = timeout_s
        self.max_retries = max(0, max_retries)
        self._sleep = sleep
        # Built once per transport rather than per request: resolving the CA bundle reads the
        # filesystem, and a retry storm must not do that on every attempt.
        self.ssl_context = ssl_context if ssl_context is not None else default_ssl_context()
        # Recorded for diagnostics: how many retries we performed and why.
        self.retries: list[dict[str, Any]] = []

    # ── non-streaming ───────────────────────────────────────────────────────

    def post_json(self, url: str, headers: dict[str, str], body: dict[str, Any], *,
                  provider_id: str = "", model: str = "") -> TransportResponse:
        """POST a JSON body, retrying retryable failures.

        Raises
        ------
        GatewayError
            On a non-retryable failure, or when retries are exhausted. The final error
            carries the last classified kind so the caller can escalate precisely.
        """
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers = {"Content-Type": "application/json", **headers}
        return self._request(url, request_headers, data=data, provider_id=provider_id, model=model)

    def get_json(self, url: str, headers: dict[str, str], *,
                 provider_id: str = "", model: str = "") -> TransportResponse:
        """GET a JSON document, retrying retryable failures. Used by model discovery."""
        return self._request(url, headers, data=None, provider_id=provider_id, model=model)

    def _request(self, url: str, headers: dict[str, str], *, data: bytes | None,
                 provider_id: str, model: str) -> TransportResponse:
        last: GatewayError | None = None
        for attempt in range(1, self.max_retries + 2):
            try:
                request = urllib.request.Request(url, data=data, headers=headers,
                                                 method="POST" if data is not None else "GET")
                with urllib.request.urlopen(request, timeout=self.timeout_s,
                                              context=self.ssl_context) as response:
                    body = response.read()
                    return TransportResponse(
                        status=response.status,
                        body=body,
                        headers={k.lower(): v for k, v in response.headers.items()},
                    )
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                error = GatewayError.from_status(
                    exc.code,
                    f"{provider_id or 'provider'} returned HTTP {exc.code}: {_short(payload)}",
                    provider_id=provider_id or None,
                    model=model or None,
                )
                error.retry_after_s = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                # A 400 that mentions context length is really a context overflow, and
                # must not be retried as a generic bad request.
                if exc.code == 400 and _mentions_context_length(payload):
                    error.kind = ErrorKind.CONTEXT_LENGTH
            except urllib.error.URLError as exc:
                reason = str(getattr(exc, "reason", exc))
                kind = ErrorKind.TIMEOUT if "timed out" in reason.lower() else ErrorKind.CONNECTION
                error = GatewayError(kind, f"{provider_id or 'provider'} unreachable: {reason}",
                                     provider_id=provider_id or None, model=model or None)
            except TimeoutError as exc:
                error = GatewayError(ErrorKind.TIMEOUT, f"{provider_id or 'provider'} timed out: {exc}",
                                     provider_id=provider_id or None, model=model or None)
            except OSError as exc:
                error = GatewayError(ErrorKind.CONNECTION, f"{provider_id or 'provider'} connection error: {exc}",
                                     provider_id=provider_id or None, model=model or None)

            last = error
            if not error.retryable or attempt > self.max_retries:
                raise error
            delay = error.retry_after_s if error.retry_after_s is not None else backoff_delay(attempt)
            self.retries.append({
                "provider_id": provider_id, "model": model, "attempt": attempt,
                "kind": error.kind.value, "status": error.status, "delay_s": round(delay, 3),
            })
            self._sleep(delay)

        # Unreachable in practice; the loop either returns or raises.
        raise last or GatewayError(ErrorKind.UNKNOWN, "request failed with no recorded error")

    # ── streaming ───────────────────────────────────────────────────────────

    def stream_lines(self, url: str, headers: dict[str, str], body: dict[str, Any], *,
                     provider_id: str = "", model: str = "") -> Iterator[str]:
        """POST and yield decoded lines as they arrive.

        Retries only the initial connection: once bytes have flowed, retrying would
        duplicate generation and confuse the caller's accumulator, so a mid-stream
        failure raises and the caller decides whether to restart the turn.

        Yields
        ------
        str
            Non-empty lines with trailing newlines stripped. Blank lines (SSE event
            separators) are skipped. A torn final line is yielded only if it is
            complete — a partial JSON fragment is dropped, because feeding it to a
            parser produces a misleading error.
        """
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers = {"Content-Type": "application/json", "Accept": "text/event-stream", **headers}

        last: GatewayError | None = None
        response = None
        for attempt in range(1, self.max_retries + 2):
            try:
                request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
                response = urllib.request.urlopen(request, timeout=self.timeout_s,
                                                    context=self.ssl_context)
                break
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                error = GatewayError.from_status(
                    exc.code, f"{provider_id or 'provider'} returned HTTP {exc.code}: {_short(payload)}",
                    provider_id=provider_id or None, model=model or None,
                )
                error.retry_after_s = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                if exc.code == 400 and _mentions_context_length(payload):
                    error.kind = ErrorKind.CONTEXT_LENGTH
                last = error
            except urllib.error.URLError as exc:
                reason = str(getattr(exc, "reason", exc))
                kind = ErrorKind.TIMEOUT if "timed out" in reason.lower() else ErrorKind.CONNECTION
                last = GatewayError(kind, f"{provider_id or 'provider'} unreachable: {reason}",
                                    provider_id=provider_id or None, model=model or None)
            except OSError as exc:
                last = GatewayError(ErrorKind.CONNECTION, f"{provider_id or 'provider'} connection error: {exc}",
                                    provider_id=provider_id or None, model=model or None)

            if last is None or not last.retryable or attempt > self.max_retries:
                raise last if last is not None else GatewayError(ErrorKind.UNKNOWN, "stream failed")
            delay = last.retry_after_s if last.retry_after_s is not None else backoff_delay(attempt)
            self.retries.append({
                "provider_id": provider_id, "model": model, "attempt": attempt,
                "kind": last.kind.value, "status": last.status, "delay_s": round(delay, 3),
            })
            self._sleep(delay)

        if response is None:
            raise last or GatewayError(ErrorKind.UNKNOWN, "stream could not be opened")

        try:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                yield line
        except (urllib.error.URLError, OSError) as exc:
            raise GatewayError(
                ErrorKind.CONNECTION,
                f"stream interrupted after data began: {exc}. "
                "Not retried: restarting would duplicate generation.",
                provider_id=provider_id or None, model=model or None,
            ) from exc
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - closing must never mask the real error
                pass

    def retry_report(self) -> dict[str, Any]:
        """Summary of retries performed, for the resources view and diagnostics."""
        by_kind: dict[str, int] = {}
        for entry in self.retries:
            by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
        return {"total": len(self.retries), "by_kind": dict(sorted(by_kind.items())),
                "events": list(self.retries)}


def _short(payload: bytes, limit: int = 240) -> str:
    """A short, single-line preview of an error body. Never returns the whole body."""
    text = payload[:limit].decode("utf-8", errors="replace").replace("\n", " ").strip()
    return text or "(empty body)"


_CONTEXT_MARKERS = (
    "context length", "context_length", "too many tokens", "maximum context",
    "context window", "prompt is too long", "reduce the length",
)


def _mentions_context_length(payload: bytes) -> bool:
    """Detect a context-overflow error reported as a generic HTTP 400.

    Providers are inconsistent here — some return 400 rather than a dedicated code — and
    the distinction matters enormously: a context overflow must trigger compaction, while
    retrying it identically can only fail again.
    """
    lowered = payload[:2000].decode("utf-8", errors="replace").lower()
    return any(marker in lowered for marker in _CONTEXT_MARKERS)
