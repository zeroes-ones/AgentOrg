#!/usr/bin/env python3
"""rpc.py — the gateway as a service, so credentials live in exactly one process.

WHY THIS EXISTS
---------------
The design puts agent work in runner subprocesses while the host process is the sole
holder of API keys and the sole authority on rate limits and budget. That split is what
makes two guarantees true:

- **Secrets never reach a worker.** A runner that crashes, dumps memory, or writes a log
  cannot leak a key it was never given.
- **Rate limiting is global.** If each runner held its own keys and its own limiter, N
  runners would each believe they had the full quota and collectively blow through it.

So runners do not call providers directly; they call the host over a Unix domain socket,
and this module is that protocol. It is deliberately a thin, synchronous JSON-RPC: the
caller blocks on a model response anyway, so an async protocol would add complexity
without buying throughput.

DESIGN
------
- **Length-prefixed JSON frames over a Unix socket.** A local socket avoids exposing a
  TCP port, and length framing avoids ambiguity if content contains newlines.
- **One request per connection, closed after.** Simpler to reason about than a
  multiplexed channel, and connection setup on a Unix socket is cheap.
- **Socket path permission is checked.** A world-writable socket would let any local
  process spend the Owner's budget, so the directory is expected to be private.
- **Server is single-threaded per connection**, which is correct: concurrency belongs to
  the scheduler and the provider semaphores, not to the transport.
- **The client surfaces a typed error**, never a bare socket exception, so a runner can
  distinguish "host restarted" from "provider failed".

Usage:
    # host side
    server = GatewayServer(gateway, Path(socket_path))
    server.serve_forever()          # or serve_once() in a thread

    # runner side
    client = GatewayClient(Path(socket_path))
    response = client.complete({"provider_id": "ollama", "model": "m", "messages": [...]})
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
from pathlib import Path
from typing import Any

from .gateway import BudgetExceeded, Gateway
from .providers.base import (
    ChatRequest,
    ErrorKind,
    GatewayError,
    Message,
    Role,
    ToolSpec,
)

__all__ = ["GatewayServer", "GatewayClient", "RpcError", "frame", "read_frame", "MAX_FRAME_BYTES"]

# Bound a single message. A prompt is the largest legitimate payload; anything bigger is
# a bug (an artifact body inlined instead of referenced), and failing clearly beats
# buffering unbounded input on a socket.
MAX_FRAME_BYTES = 32 << 20
_HEADER = struct.Struct("!I")

# AF_UNIX paths are bounded by `sun_path` in the kernel structure — 104 bytes on macOS,
# 108 on Linux. A deep project path (a temp directory, a nested workspace) can exceed it,
# so binding must handle that rather than failing with an opaque "path too long".
_SUN_PATH_LIMIT = 100


def socket_path_within_limit(path: Path) -> Path:
    """Return a socket path that fits the platform's `AF_UNIX` limit.

    A path already short enough is returned unchanged. A longer one is relocated to a
    short private directory keyed by a hash of the original, because silently truncating
    a path would point the socket somewhere the caller did not intend.

    Candidate bases are tried shortest-first: `TMPDIR` on macOS can itself be long (a deep
    per-session temp directory), so a naive relocation to `gettempdir()` can still exceed
    the limit and produce the very failure it was meant to prevent.
    """
    raw = str(path)
    if len(raw.encode("utf-8")) <= _SUN_PATH_LIMIT:
        return path

    import hashlib
    import tempfile

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    candidates: list[Path] = [Path("/tmp"), Path("/private/tmp")]
    try:
        candidates.append(Path(tempfile.gettempdir()))
    except (OSError, RuntimeError):
        pass

    # Deduplicate while preserving the shortest-first order.
    seen: set[str] = set()
    ordered: list[Path] = []
    for base in candidates:
        key = str(base)
        if key not in seen:
            seen.add(key)
            ordered.append(base)
    ordered.sort(key=lambda p: len(str(p)))

    for base in ordered:
        candidate = base / f"agentorg-{digest}" / "gw.sock"
        if len(str(candidate).encode("utf-8")) <= _SUN_PATH_LIMIT:
            # Create the intermediate directory privately: under a shared base like /tmp
            # a 0700 subdirectory is what keeps the socket itself protected.
            candidate.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(candidate.parent, 0o700)
            except OSError:
                pass
            return candidate

    raise RpcError(
        f"cannot find a socket path within the {_SUN_PATH_LIMIT} byte AF_UNIX limit for "
        f"{raw}; set AGENTORG_SOCKET to a shorter path"
    )


class RpcError(RuntimeError):
    """Transport-level failure between a runner and the gateway host."""

    def __init__(self, message: str, *, kind: ErrorKind = ErrorKind.CONNECTION) -> None:
        super().__init__(message)
        self.kind = kind


def frame(payload: dict[str, Any]) -> bytes:
    """Serialise a payload into a length-prefixed frame."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise RpcError(
            f"frame is {len(body)} bytes, over the {MAX_FRAME_BYTES} byte limit; "
            "reference large content by path",
        )
    return _HEADER.pack(len(body)) + body


def read_frame(sock: socket.socket) -> dict[str, Any]:
    """Read one length-prefixed frame, or raise :class:`RpcError`."""
    header = _recv_exact(sock, _HEADER.size)
    if header is None:
        raise RpcError("connection closed before a frame header arrived")
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise RpcError(f"peer announced a {length} byte frame, over the limit")
    body = _recv_exact(sock, length)
    if body is None:
        raise RpcError("connection closed mid-frame")
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RpcError(f"frame body is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise RpcError("frame body must be a JSON object")
    return data


def _recv_exact(sock: socket.socket, count: int) -> bytes | None:
    """Read exactly `count` bytes, or None if the peer closed first."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        try:
            chunk = sock.recv(remaining)
        except OSError as exc:
            raise RpcError(f"socket read failed: {exc}") from exc
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# ── wire conversions ─────────────────────────────────────────────────────────


def request_to_wire(request: ChatRequest, *, provider_id: str | None, agent_id: str | None,
                     node_id: str | None, session_id: str | None) -> dict[str, Any]:
    """Render a :class:`ChatRequest` for the wire.

    Messages are flattened to text plus tool activity. The block structure is preserved
    only where it carries meaning (tool use), keeping the payload small enough that a long
    conversation does not approach the frame limit.
    """
    return {
        "provider_id": provider_id,
        "agent_id": agent_id,
        "node_id": node_id,
        "session_id": session_id,
        "model": request.model,
        "system": request.system,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "json_mode": request.json_mode,
        "stop": request.stop,
        "extra": request.extra,
        "messages": [
            {
                "role": m.role.value,
                "text": m.text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments} for c in m.tool_calls
                ],
            }
            for m in request.messages
        ],
        "tools": [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in request.tools
        ],
    }


def wire_to_request(payload: dict[str, Any]) -> ChatRequest:
    """Rebuild a :class:`ChatRequest` from the wire."""
    messages: list[Message] = []
    for raw in payload.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        try:
            role = Role(str(raw.get("role", "user")))
        except ValueError:
            role = Role.USER
        from .providers.base import ToolCall

        message = Message.text_message(role, str(raw.get("text") or ""))
        for call in raw.get("tool_calls") or []:
            if isinstance(call, dict):
                message.tool_calls.append(ToolCall(
                    id=str(call.get("id") or "call_0"),
                    name=str(call.get("name") or ""),
                    arguments=call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                ))
        messages.append(message)

    tools: list[ToolSpec] = []
    for raw in payload.get("tools") or []:
        if isinstance(raw, dict):
            tools.append(ToolSpec(
                name=str(raw.get("name") or ""),
                description=str(raw.get("description") or ""),
                parameters=raw.get("parameters") if isinstance(raw.get("parameters"), dict) else {},
            ))

    return ChatRequest(
        model=str(payload.get("model") or ""),
        messages=messages,
        system=payload.get("system"),
        tools=tools,
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
        stream=False,
        json_mode=bool(payload.get("json_mode")),
        stop=list(payload.get("stop") or []),
        extra=payload.get("extra") if isinstance(payload.get("extra"), dict) else {},
    )


# ── server ───────────────────────────────────────────────────────────────────


class GatewayServer:
    """Serves the host gateway to runner subprocesses over a Unix socket.

    Parameters
    ----------
    gateway:
        The host's gateway. This object holds the credentials; nothing else should.
    socket_path:
        Unix socket path. Its parent directory must not be world-writable, or any local
        process could spend the Owner's budget.
    """

    def __init__(self, gateway: Gateway, socket_path: os.PathLike | str) -> None:
        self.gateway = gateway
        self.socket_path = Path(socket_path)
        # The requested path is remembered separately: the caller needs to know where to
        # connect, and that is the path *it* asked for, not our relocated fallback.
        self.requested_path = Path(socket_path)
        self._server: socket.socket | None = None
        self._stop = threading.Event()

    def bind(self) -> None:
        """Create and bind the listening socket, with private permissions.

        The socket file is chmod 0600 and a pre-existing file is removed first: a stale
        socket from a previous crash would otherwise make `bind` fail confusingly.
        """
        # Relocate before anything else so an over-long path is handled, not fatal.
        relocated = socket_path_within_limit(self.requested_path)
        if relocated != self.requested_path:
            self.socket_path = relocated
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Refuse a shared directory: a world-writable parent defeats the 0600 socket.
        try:
            mode = self.socket_path.parent.stat().st_mode
            if mode & 0o002 and self.socket_path == self.requested_path:
                raise RpcError(
                    f"socket directory {self.socket_path.parent} is world-writable; "
                    "any local process could spend the run budget. Choose a private directory."
                )
        except OSError:
            pass

        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError as exc:
                raise RpcError(f"cannot remove stale socket {self.socket_path}: {exc}") from exc

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            server.listen(16)
        except OSError as exc:
            server.close()
            raise RpcError(f"cannot bind {self.socket_path}: {exc}") from exc
        self._server = server

    def serve_once(self) -> bool:
        """Handle a single connection. Returns False if the server was stopped.

        Separated from :meth:`serve_forever` so a caller can drive it from a loop, or from
        a thread that also needs to observe shutdown.
        """
        if self._server is None:
            self.bind()
        assert self._server is not None
        try:
            connection, _ = self._server.accept()
        except OSError:
            return False
        if self._stop.is_set():
            connection.close()
            return False
        with connection:
            self._handle(connection)
        return True

    def _handle(self, connection: socket.socket) -> None:
        """Read one request, dispatch it, write one response."""
        try:
            request = read_frame(connection)
        except RpcError as exc:
            _send(connection, {"ok": False, "error": str(exc), "kind": "protocol"})
            return

        method = str(request.get("method") or "complete")
        payload = request.get("params") if isinstance(request.get("params"), dict) else {}
        try:
            if method == "complete":
                result = self._do_complete(payload)
            elif method == "health":
                result = self.gateway.health()
            elif method == "cost":
                result = self.gateway.cost_snapshot()
            else:
                _send(connection, {"ok": False, "error": f"unknown method {method!r}",
                                   "kind": "protocol"})
                return
            _send(connection, {"ok": True, "result": result})
        except BudgetExceeded as exc:
            # A budget stop is a policy decision, not a provider failure: the runner must
            # be able to distinguish it and park the run rather than retry.
            _send(connection, {"ok": False, "error": str(exc), "kind": "budget",
                               "retryable": False, "detail": exc.to_dict()})
        except GatewayError as exc:
            _send(connection, {"ok": False, "error": str(exc), "kind": exc.kind.value,
                               "retryable": exc.retryable, "detail": exc.to_dict()})
        except Exception as exc:  # noqa: BLE001 - never kill the host on a bad request
            _send(connection, {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                               "kind": "internal"})

    def _do_complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = wire_to_request(payload)
        response = self.gateway.complete(
            request,
            provider_id=payload.get("provider_id"),
            agent_id=payload.get("agent_id"),
            node_id=payload.get("node_id"),
            session_id=payload.get("session_id"),
        )
        result = response.as_dict()
        result["text"] = response.text
        result["tool_calls_full"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls
        ]
        result["cost"] = self.gateway.compute_cost(
            response.provider_id or str(payload.get("provider_id") or ""),
            response.model, response.usage,
        ).as_dict()
        return result

    def serve_forever(self) -> None:
        """Accept connections until :meth:`stop` is called."""
        if self._server is None:
            self.bind()
        while not self._stop.is_set():
            self.serve_once()

    def stop(self) -> None:
        """Stop accepting and clean up the socket file."""
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass

    def start_thread(self) -> threading.Thread:
        """Run the server on a daemon thread and return it."""
        thread = threading.Thread(target=self.serve_forever, name="gateway-rpc", daemon=True)
        thread.start()
        return thread


def _send(connection: socket.socket, payload: dict[str, Any]) -> None:
    """Write one response frame, ignoring a peer that has already gone away."""
    try:
        connection.sendall(frame(payload))
    except (OSError, RpcError):
        pass


# ── client ───────────────────────────────────────────────────────────────────


class GatewayClient:
    """Calls the host gateway over the Unix socket.

    A fresh connection per call. That is a deliberate trade: it costs a socket round trip,
    which is negligible beside a model call, and it keeps a runner that has been killed
    from leaving a half-open channel that the host must reason about.
    """

    def __init__(self, socket_path: os.PathLike | str, *, timeout_s: float = 600.0) -> None:
        self.requested_path = Path(socket_path)
        # Resolve the same way the server does, so a path too long for AF_UNIX locates the
        # relocated socket instead of reporting the host as absent.
        self.socket_path = socket_path_within_limit(self.requested_path)
        self.timeout_s = timeout_s

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and return its result.

        Raises
        ------
        GatewayError
            When the host reported a provider failure, preserving its kind and retryable
            flag so the caller's policy logic is unchanged across the socket.
        RpcError
            On a transport failure.
        BudgetExceeded
            When the host refused for budget reasons.
        """
        if not self.socket_path.exists():
            raise RpcError(
                f"gateway socket {self.socket_path} does not exist; the host is not running "
                "or has not bound yet"
            )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout_s)
        try:
            sock.connect(str(self.socket_path))
            sock.sendall(frame({"method": method, "params": params or {}}))
            response = read_frame(sock)
        except OSError as exc:
            raise RpcError(f"gateway call failed: {exc}") from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass

        if response.get("ok"):
            result = response.get("result")
            return result if isinstance(result, dict) else {}
        error = str(response.get("error") or "unknown gateway error")
        kind = str(response.get("kind") or "unknown")
        if kind == "budget":
            detail = response.get("detail") or {}
            raise BudgetExceeded(float(detail.get("spent_usd") or 0.0),
                                 float(detail.get("limit_usd") or 0.0),
                                 scope=str(detail.get("scope") or "run"))
        try:
            error_kind = ErrorKind(kind)
        except ValueError:
            error_kind = ErrorKind.UNKNOWN
        raise GatewayError(error_kind, error, detail={"rpc": True})

    def complete(self, **kwargs: Any) -> dict[str, Any]:
        """Convenience wrapper for the `complete` method."""
        return self.call("complete", kwargs)

    def health(self) -> dict[str, Any]:
        """Fetch the host's health, or a down status if it cannot be reached."""
        try:
            return self.call("health")
        except (RpcError, GatewayError, BudgetExceeded) as exc:
            return {"status": "down", "error": str(exc)}

    def cost(self) -> dict[str, Any]:
        """Fetch the host's cost snapshot."""
        return self.call("cost")
