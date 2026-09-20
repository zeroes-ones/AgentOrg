#!/usr/bin/env python3
"""mcp.py — the Model Context Protocol client: capabilities the engine does not have to invent.

WHY THIS EXISTS
---------------
Everything else in this engine is closed. A node has a workspace, a handful of file tools, and an
optional confined shell; if an agent needs to file an issue, query a database, drive a browser or
reach an internal API, the answer so far has been "add a tool to `tools.py`". That does not scale
and it is the wrong place for the decision: an integration is configuration, not engine code.

MCP is the industry answer. A server speaks one of two transports, advertises its tools with a JSON
Schema, and is called by name. The engine needs exactly three things to participate — a client that
performs the handshake, a registry entry per remote tool, and a hard rule that a broken server is a
*missing capability* rather than a dead run. This module is those three things.

DESIGN
------
- **The wire format is not invented here.** It is the published MCP spec, and it was confirmed
  against real servers before this file was written (a stdio server on the official TypeScript SDK
  and a streamable-HTTP server on the same SDK): `initialize` → `notifications/initialized` →
  `tools/list` → `tools/call`, JSON-RPC 2.0, newline-delimited on stdio. Guessing this would have
  produced a client that cannot talk to anything, so every field named below came from an observed
  message rather than from memory.
- **A failure is a refusal, never an exception.** This is the property that matters most, and it is
  the one an unattended run cannot do without. A server that will not start, dies mid-call or hangs
  must leave the other nine servers working and the run alive. Every failure path in this module ends
  in `ToolResult(ok=False, ...)` naming what happened, because an agent that is told *why* a
  capability is missing can route around it, and an exception would end the node.
- **Nothing blocks forever.** Every wait — handshake, listing, calling, shutdown — is bounded by
  `timeout_s`, because the failure mode this module exists to prevent is a run that stops at 3am
  with no event and no explanation. A timeout also sends `notifications/cancelled`, so the server
  is told to stop working rather than left to finish work nobody will read.
- **Names are namespaced, and the way back is a table.** Two servers may both offer `search`, and a
  model must be able to call either. Tools are advertised as `mcp__<server>__<tool>`; the reverse
  mapping is a *dict built from the advertised list*, never a parse of the name. A server is free to
  name a tool `github__create_issue`, and re-splitting the string would silently mis-route it to a
  server that does not have it.
- **A stdio child is always reaped.** It is started in its own process group and the whole group is
  killed, because the interesting servers are launcher scripts (`npx -y …`) whose real work happens
  in a grandchild that a naive `terminate()` leaves running forever.
- **A server that does not declare itself safe is treated as unsafe.** `readOnlyHint` defaults to
  false in the spec, so an unannotated tool is advertised as mutating and is therefore refused in a
  read-only run. The alternative — assuming safe — would let a run that promised not to change the
  workspace change it.

Usage:
    bridge = McpToolBridge.from_config(config.mcp, on_event=bus.emit)
    bridge.install(registry)          # registry.specs() now advertises the remote tools
    try:
        ...
    finally:
        bridge.close()                # reaps every stdio child
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import redact
from .tools import Tool, ToolResult

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "MCP_TOOL_PREFIX",
    "MCP_TOOL_SEPARATOR",
    "McpError",
    "McpServerSpec",
    "McpClient",
    "McpCallResult",
    "McpToolBridge",
    "namespaced_tool_name",
    "iter_server_specs",
    "attach",
]

#: What this client asks for. The spec requires the client to offer a version it actually implements
#: and to disconnect if the server answers with one it does not. `2025-06-18` is the revision the
#: handshake and tool shapes below were verified against — both against a server on the official
#: SDK and against the published JSON schema — and it is not the newest revision the SDK knows, which
#: is deliberate: every server that accepts `2025-06-18` also accepts it as *the* answer, whereas a
#: newer request is refused outright by any server that has not been updated.
MCP_PROTOCOL_VERSION = "2025-06-18"

#: Revisions this client can speak. A server is allowed to negotiate *down* — it answers with the
#: newest revision it supports, which may be older than what was asked for — so the reply is checked
#: against this set rather than against the requested string. Refusing a whole server because it
#: answered `2025-03-26` would make the client useless against last year's servers for no gain: the
#: four messages this module sends are unchanged across all of them.
SUPPORTED_PROTOCOL_VERSIONS = frozenset({
    "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07",
})

#: How a remote tool is spelled in the registry. Two separators rather than one because tool names
#: routinely contain a single underscore (`read_file`, `create_issue`), and a one-separator scheme
#: would make `mcp__github_read_file` ambiguous to read even though the routing table is exact.
MCP_TOOL_PREFIX = "mcp__"
MCP_TOOL_SEPARATOR = "__"

#: The largest content a single call may return into a prompt. A remote server is not written by this
#: project and can return a megabyte of JSON; unbounded, one call would consume a node's whole
#: context. Cutting it and *saying* it was cut is far more useful than either refusing or flooding.
MAX_RESULT_BYTES = 64 * 1024

#: How many `nextCursor` pages a listing will follow, so a server that always reports a cursor cannot
#: spin forever during discovery.
MAX_LIST_PAGES = 20

#: Grace given to a stdio server to exit on its own after its stdin is closed, before it is stopped.
#: The spec's shutdown sequence is exactly this: close the input stream, wait briefly, escalate.
SHUTDOWN_GRACE_S = 2.0

#: JSON-RPC error codes this module produces or reads back. Named because a refusal that quotes the
#: numeric code is less useful to a reader than one that says which kind of failure it was.
_ERR_METHOD_NOT_FOUND = -32601

#: The client declares **no** optional capabilities. Roots, sampling and elicitation are features the
#: engine would have to implement to answer for, and a client that advertises a capability it cannot
#: honour turns a well-behaved server's question into a hang. An empty object is the honest statement.
_CLIENT_CAPABILITIES: dict[str, Any] = {}


class McpError(RuntimeError):
    """A protocol-level failure against one server.

    Raised inside the client and the bridge, never out of them: the bridge turns it into the refusal
    shape the engine already speaks. It exists as a type so the *reason* a server is unusable can be
    carried, reported once at discovery, and quoted in every later call for that server.
    """


# ── the server spec ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class McpServerSpec:
    """One configured server, in one of the two shapes MCP defines.

    A `command` means stdio: the engine spawns the process and speaks JSON-RPC over its pipes, which
    is what a local integration (a filesystem, a git helper, a database CLI) almost always wants. A
    `url` means HTTP: the server is already running somewhere, which is what a shared team service
    wants. The two are not interchangeable and the spec keeps them apart rather than papering over
    the difference with a mode flag.
    """

    name: str
    transport: str  # "stdio" | "http"
    #: stdio only: the program, its arguments, and any environment merged over the engine's own.
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    #: http only: where to POST, and any headers (auth, tenancy) the service needs.
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    #: Per-server override of `mcp.timeout_s`; 0 means "use the section's value".
    timeout_s: int = 0

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise McpError("an MCP server needs a name; without one its tools cannot be namespaced")
        if self.transport not in ("stdio", "http"):
            raise McpError(
                f"mcp.servers[{self.name!r}] has transport {self.transport!r}; the only transports "
                "this client speaks are 'stdio' (a `command`) and 'http' (a `url`)")
        if self.transport == "stdio" and not str(self.command or "").strip():
            raise McpError(
                f"mcp.servers[{self.name!r}] is a stdio server but names no command; there is "
                "nothing to spawn, so this server cannot be started")
        if self.transport == "http" and not str(self.url or "").strip():
            raise McpError(
                f"mcp.servers[{self.name!r}] is an http server but names no url; there is nothing "
                "to POST to, so this server cannot be reached")

    @property
    def namespaced_prefix(self) -> str:
        """The prefix every tool from this server is advertised under.

        Public because it is the one place the scheme is spelled for a *server* rather than for a
        single tool; `namespaced_tool_name` builds on the same two constants, so the prefix a caller
        searches for and the name a model is given cannot drift apart.
        """
        return f"{MCP_TOOL_PREFIX}{self.name}{MCP_TOOL_SEPARATOR}"

    def describe(self) -> str:
        """A one-line description safe to log.

        Header *values* are redacted and env values are never shown: the interesting headers are
        `Authorization` and friends, and an integration's credentials must not reach `trace.jsonl`
        just because a run reported which servers it could reach.
        """
        if self.transport == "stdio":
            shown = " ".join([self.command, *[redact(str(a)) for a in self.args]]).strip()
            env_names = ",".join(sorted(self.env)) if self.env else "-"
            return f"stdio: {shown} (env: {env_names})"
        return f"http: {self.url} (headers: {','.join(sorted(self.headers)) or '-'})"

def _spec_from_entry(name: str, raw: Any) -> McpServerSpec | None:
    """Build a spec from one `mcp.servers` entry, or None when the entry is switched off.

    Refuses rather than guessing on a malformed entry: one with neither `command` nor `url` is a
    typo, and a client that silently skipped it would leave a person believing a capability was
    available when it was not. The refusal names the server, which is the one thing that makes it
    actionable. `enabled: false` is a different statement — the operator said no on purpose — so it
    is dropped quietly rather than added to a failure list a reader is supposed to take seriously.
    """
    if not isinstance(raw, dict):
        raise McpError(
            f"mcp.servers[{name!r}] must be an object with either a `command` (stdio) or a `url` "
            f"(http), got {type(raw).__name__}")
    if raw.get("enabled") is False:
        # Not a failure and deliberately not recorded as one. An operator who turned a server off
        # does not want it reported as broken on every run — that trains a reader to ignore the
        # failure list, which is where the genuinely-broken servers have to stand out.
        return None
    command = raw.get("command")
    url = raw.get("url")
    args = raw.get("args") or ()
    env = raw.get("env") or {}
    headers = raw.get("headers") or {}
    timeout_s = _positive_int(raw.get("timeout_s") or 0, default=0)
    if command:
        if not isinstance(args, (list, tuple)):
            raise McpError(f"mcp.servers[{name!r}].args must be a list of strings")
        if not isinstance(env, dict):
            raise McpError(f"mcp.servers[{name!r}].env must be an object of names to values")
        return McpServerSpec(name=name, transport="stdio", command=str(command),
                             args=tuple(str(a) for a in args),
                             env={str(k): str(v) for k, v in env.items()}, timeout_s=timeout_s)
    if url:
        if not isinstance(headers, dict):
            raise McpError(f"mcp.servers[{name!r}].headers must be an object of names to values")
        return McpServerSpec(name=name, transport="http", url=str(url),
                             headers={str(k): str(v) for k, v in headers.items()},
                             timeout_s=timeout_s)
    raise McpError(
        f"mcp.servers[{name!r}] names neither `command` (a stdio server to spawn) nor `url` (an "
        "http server to POST to), so there is no way to reach it")


def iter_server_specs(config: Any) -> tuple[list[McpServerSpec], dict[str, str]]:
    """Every configured server, plus the reason each unusable one was dropped.

    Returns rather than raises, and returns the failures *alongside* the successes: a single
    malformed entry must not cost the run every other server it was configured with, and a silent
    drop would be worse still — the run would report a capability as merely absent when the truth is
    that it was misspelled.
    """
    specs: list[McpServerSpec] = []
    failures: dict[str, str] = {}
    if config is None or not getattr(config, "enabled", False):
        return specs, failures
    servers = getattr(config, "servers", None) or {}
    if not isinstance(servers, dict):
        return specs, {"<mcp.servers>": "mcp.servers must be an object keyed by server name"}
    for name, raw in sorted(servers.items()):
        try:
            spec = _spec_from_entry(str(name), raw)
        except McpError as exc:
            failures[str(name)] = str(exc)
        except Exception as exc:  # noqa: BLE001 - a malformed entry is data, not a crash
            failures[str(name)] = f"{type(exc).__name__}: {exc}"
        else:
            if spec is not None:
                specs.append(spec)
    return specs, failures


def _positive_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


# ── transport ──────────────────────────────────────────────────────────────


class _StdioTransport:
    """A spawned MCP server, spoken to over its stdin/stdout.

    Messages are newline-delimited JSON-RPC, which is what the spec's stdio binding defines and what
    the reference SDK's `ReadBuffer` implements. Two details here exist because the naive version
    fails in ways that are hard to see:

    - **A reader thread owns stdout.** `readline()` on a pipe has no timeout, so a synchronous read
      is a hang waiting for a server that never answers. The thread reads lines, parses them and
      pushes them onto a queue; a wait is then a `queue.get(timeout=…)`, and the same queue reports
      end-of-stream so a dead child is *noticed* rather than waited on.
    - **The child gets its own process group.** The useful servers are launcher scripts — `npx -y
      some-mcp-server` — where the process the engine holds is a wrapper and the real server is a
      grandchild. `terminate()` on the wrapper alone leaves the server running and its port held, so
      the whole group is signalled instead.
    """

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self._proc: subprocess.Popen[str] | None = None
        self._inbox: "queue.Queue[Any]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr_tail: list[str] = []
        self._lock = threading.Lock()
        #: The child's process group, captured while it is alive. See `_signal_group`.
        self._pgid: int | None = None

    def start(self) -> None:
        command = self.spec.command
        if shutil.which(command) is None and not os.path.isabs(command):
            # Checked before spawning so the refusal names the fix. `Popen` would raise an
            # `FileNotFoundError` that says only "no such file", which reads as a project defect
            # rather than "this server is not installed on this machine".
            raise McpError(
                f"cannot start MCP server {self.spec.name!r}: no executable {command!r} on PATH. "
                "Install it, or point `command` at an absolute path.")
        env = {**os.environ, **self.spec.env}
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - the command is operator configuration
                [command, *self.spec.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise McpError(
                f"cannot start MCP server {self.spec.name!r} ({command}): "
                f"{type(exc).__name__}: {exc}") from None
        # `start_new_session=True` makes the child a group leader, so its pid *is* the group id and
        # can be recorded now. It has to be recorded now: `os.getpgid` on a pid that has been waited
        # on fails, and by teardown time the child may well have exited cleanly — which is exactly
        # the case where the grandchild is still alive and still needs signalling.
        getpgid = getattr(os, "getpgid", None)
        if getpgid is not None:
            try:
                self._pgid = getpgid(self._proc.pid)
            except Exception:  # noqa: BLE001 - no group id just means the fallback path is used
                self._pgid = None
        self._reader = threading.Thread(target=self._pump, name=f"mcp-{self.spec.name}",
                                        daemon=True)
        self._reader.start()
        threading.Thread(target=self._drain_stderr, name=f"mcp-{self.spec.name}-err",
                         daemon=True).start()

    def _pump(self) -> None:
        """Read stdout until the server goes away, handing each parsed message to the inbox.

        A line that is not JSON is *not* fatal: a server that prints a banner or a stray debug line
        before its first real message is common, and killing the connection over it would be a
        client that cannot talk to an otherwise working server. It is dropped, and the end sentinel
        still arrives if the process is gone.
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._inbox.put(_EOF)
            return
        try:
            for line in proc.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self._inbox.put(message)
        except Exception:  # noqa: BLE001 - a broken pipe on shutdown is expected, not exceptional
            pass
        finally:
            self._inbox.put(_EOF)

    def _drain_stderr(self) -> None:
        """Keep the last few stderr lines, so a server that dies can say why.

        Drained rather than left alone: an unread stderr pipe fills, and a server that blocks writing
        its own log has *become* a hung server. Only a tail is kept — the useful part of a crash is
        the last line, and the whole log of a chatty server is not worth holding in memory.
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self._stderr_tail.append(line.rstrip())
                if len(self._stderr_tail) > 20:
                    del self._stderr_tail[0]
        except Exception:  # noqa: BLE001 - see above; the pipe closing is the normal end
            pass

    def send(self, message: dict[str, Any]) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise McpError(f"server {self.spec.name!r} is not running: {self.cause()}")
            try:
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise McpError(
                    f"server {self.spec.name!r} closed its input while a message was being sent "
                    f"({type(exc).__name__}): {self.cause()}") from None

    def receive(self, timeout_s: float) -> Any:
        try:
            return self._inbox.get(timeout=max(0.01, timeout_s))
        except queue.Empty:
            raise McpError(
                f"server {self.spec.name!r} did not answer within {timeout_s:g}s") from None

    def cause(self) -> str:
        """Why a server is gone: its exit status, and the last thing it said on stderr."""
        proc = self._proc
        code = proc.poll() if proc is not None else None
        reason = "exited" if code is not None else "is unresponsive"
        detail = ""
        if code is not None:
            detail = f" (exit status {code})"
        tail = [line for line in self._stderr_tail if line.strip()][-3:]
        if tail:
            detail += "; last stderr: " + " | ".join(redact(line)[:200] for line in tail)
        return f"{reason}{detail}"

    def close(self) -> None:
        """Reap the child *and its process group*. Idempotent, bounded, and never raises.

        The spec's order is followed for the child itself: close stdin first, because a well-behaved
        server exits on end-of-input and needs no signal at all; then a short grace; then SIGTERM,
        then SIGKILL. Escalating only when the polite step failed is what keeps a server's own
        cleanup — flushing a database, removing a socket — from being cut off unnecessarily.

        **The group is swept even when the polite step worked**, and that is not redundant. The
        integrations that matter are launchers: `npx -y some-server` runs a wrapper that `exec`s or
        spawns the real server as a grandchild. The wrapper exits cleanly the moment its stdin closes
        — so a teardown that stopped there would report success while leaving the actual server, its
        port and its child processes running for the life of the machine. Signalling the group after
        a clean exit is a no-op when there is no grandchild left, which is why it costs nothing to do
        unconditionally.
        """
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001 - teardown must not raise over bookkeeping
            pass
        try:
            proc.wait(timeout=SHUTDOWN_GRACE_S)
        except Exception:  # noqa: BLE001 - including TimeoutExpired; the signal below is the answer
            self._signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=SHUTDOWN_GRACE_S)
            except Exception:  # noqa: BLE001 - the same, one step further up
                pass
        # Whether the child left of its own accord or had to be stopped, anything it started is
        # still in the group and is this client's to clean up.
        self._signal_group(proc, signal.SIGKILL)
        self._pgid = None
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:  # noqa: BLE001 - see above
                pass

    def _signal_group(self, proc: subprocess.Popen, number: int) -> None:
        """Signal the child's whole process group, falling back to the child alone.

        The group is the point: a launcher's grandchild is what actually holds the resources, and it
        is not the process handle this client holds. The group id was captured at spawn (`_pgid`)
        because the child is normally *already reaped* by the time this runs — a wrapper exits the
        moment its stdin closes — and `os.getpgid` on a waited-on pid fails, which would silently
        downgrade this to signalling a process that no longer exists.

        The signal is passed as a number rather than by name on purpose. Looking it up as
        `getattr(signal, "kill".upper())` resolves to *nothing* — the constants are `SIGKILL` and
        `SIGTERM` — which yields `None` and fails here with a confusing `TypeError`, leaving the
        grandchild alive while every step of the teardown appears to have succeeded. That failure was
        observed while writing this and is the reason the caller hands over the constant directly.
        """
        killpg = getattr(os, "killpg", None)
        pgid = self._pgid
        if killpg is not None and pgid:
            try:
                killpg(pgid, number)
                return
            except Exception:  # noqa: BLE001 - the group may already be gone; try the process
                pass
        signaller = proc.kill if number == getattr(signal, "SIGKILL", None) else proc.terminate
        try:
            signaller()
        except Exception:  # noqa: BLE001 - an already-dead child is the expected case
            pass


class _HttpTransport:
    """A remote MCP server, spoken to over streamable HTTP.

    Verified against a real server on the official SDK, and the three things that had to be right are
    the reason this is not four lines of `urlopen`:

    - **The `Accept` header must offer both.** A streamable-HTTP server may answer a POST with plain
      JSON *or* with a `text/event-stream`; a client that accepts only JSON is refused by servers that
      stream, which is most of them.
    - **A streamed answer is SSE-framed.** The JSON-RPC object arrives inside a `data:` line, so it is
      parsed out of the event stream rather than `json.loads`-ed from the body.
    - **The session is stated in headers.** A server may hand back `mcp-session-id` on the handshake
      response; every later request must quote it back, and once the version is negotiated it is
      quoted in `MCP-Protocol-Version` too. Omitting either is what produces a 400 that looks like a
      server bug.
    """

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.session_id = ""
        self.protocol_version = ""

    def start(self) -> None:
        # There is no process to spawn and nothing to check: an HTTP server is either already
        # running or the first request says so, which is exactly the message a person needs.
        return

    def send(self, message: dict[str, Any]) -> None:
        # A notification expects no response. It is still sent as a POST because that is the only
        # verb the transport defines, and a server answers it with 202 and no body. The reply is
        # dropped rather than checked: `notifications/initialized` is fire-and-forget by definition,
        # so a server that answers it with an error has nothing this client could usefully do.
        self._post(message)

    def receive(self, timeout_s: float) -> Any:
        raise McpError(
            f"server {self.spec.name!r} is an http server; its answer arrives with the request "
            "rather than from a separate read")

    def cause(self) -> str:
        return f"unreachable at {self.spec.url}"

    def request(self, message: dict[str, Any], timeout_s: float) -> Any:
        """POST one request and return its JSON-RPC answer.

        The timeout is split between connect and read because `urlopen`'s `timeout` covers each
        socket operation rather than the whole exchange — a server that accepts the connection and
        then dribbles bytes could otherwise hold a node indefinitely. The read deadline is enforced
        by the socket timeout itself, so an unresponsive-but-connected server still fails in bounded
        time.
        """
        body = self._post(message, timeout_s=timeout_s)
        if body is None:
            raise McpError(
                f"server {self.spec.name!r} accepted the request but returned no answer (HTTP 202); "
                "an http MCP server must answer a request with the JSON-RPC result")
        for message_out in body:
            if message_out.get("id") == message.get("id"):
                return message_out
        raise McpError(
            f"server {self.spec.name!r} answered without a result for request id "
            f"{message.get('id')!r}")

    def _post(self, message: dict[str, Any],
              timeout_s: float | None = None) -> list[dict[str, Any]] | None:
        headers = {
            "content-type": "application/json",
            # Both are advertised, which is what the transport requires: the server chooses.
            "accept": "application/json, text/event-stream",
            **self.spec.headers,
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        if self.protocol_version:
            headers["mcp-protocol-version"] = self.protocol_version
        request = urllib.request.Request(  # noqa: S310 - the scheme comes from operator config
            self.spec.url, data=json.dumps(message).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_s or 30.0) as response:
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self.session_id = session_id
                content_type = (response.headers.get("content-type") or "").lower()
                raw = response.read()
                if response.status == 202 or not raw:
                    return None
                text = raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise McpError(self._explain_http_error(exc)) from None
        except urllib.error.URLError as exc:
            raise McpError(
                f"cannot reach MCP server {self.spec.name!r} at {self.spec.url}: {exc.reason}") from None
        except (TimeoutError, OSError) as exc:
            raise McpError(
                f"MCP server {self.spec.name!r} at {self.spec.url} timed out after "
                f"{timeout_s or 30.0:g}s: {type(exc).__name__}") from None
        if "text/event-stream" in content_type:
            return _parse_sse(text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            raise McpError(
                f"MCP server {self.spec.name!r} returned a body that is neither JSON nor an event "
                f"stream (content-type {content_type or 'absent'}): {redact(text[:200])}") from None
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        return [parsed] if isinstance(parsed, dict) else []

    def _explain_http_error(self, exc: urllib.error.HTTPError) -> str:
        """Turn an HTTP failure into the sentence a person needs.

        The body is read because the useful cases put the reason there: a streamable-HTTP server
        rejects an unsupported protocol version with a JSON-RPC error naming the versions it *does*
        support, and a 401 with a `WWW-Authenticate` header is an auth problem rather than a bug.
        """
        try:
            detail = redact(exc.read().decode("utf-8", errors="replace")[:400])
        except Exception:  # noqa: BLE001 - an unreadable error body must not hide the status
            detail = ""
        hint = ""
        if exc.code in (401, 403):
            hint = (" — the server rejected these credentials; check the entry's `headers`")
        elif exc.code == 400 and self.protocol_version:
            hint = (f" — sent MCP-Protocol-Version {self.protocol_version}; the server may want a "
                    "different revision")
        return (f"MCP server {self.spec.name!r} at {self.spec.url} answered HTTP {exc.code}"
                f"{hint}: {detail or '(no body)'}")

    def close(self) -> None:
        """Nothing is held open between requests, so there is no connection to close.

        A session id could be released with an HTTP DELETE, but that is optional in the transport and
        a server that outlives a run is the *expected* case for HTTP — it is shared with other
        clients, so tearing it down when one run ends would be wrong.
        """
        return


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """Pull the JSON-RPC messages out of a `text/event-stream` body.

    Implements only the part of SSE this transport uses: `data:` lines accumulate until a blank line
    ends the event, and the joined payload is the JSON-RPC message. Comments (`:`) and other fields
    are ignored rather than treated as errors — a server is free to send keep-alives, and a client
    that choked on one would drop a working connection.
    """
    messages: list[dict[str, Any]] = []
    buffer: list[str] = []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
            continue
        if line.strip() and buffer:
            continue
        if not buffer:
            continue
        payload = "\n".join(buffer)
        buffer = []
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            messages.append(parsed)
        elif isinstance(parsed, list):
            messages.extend(item for item in parsed if isinstance(item, dict))
    return messages


class _Eof:
    """Sentinel pushed onto a stdio inbox when the server's output ends."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<mcp-eof>"


_EOF = _Eof()


# ── the client ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class McpCallResult:
    """What a remote tool returned, already flattened for a model to read."""

    text: str
    is_error: bool = False
    structured: dict[str, Any] | None = None
    truncated: bool = False


class McpClient:
    """One server, connected lazily and spoken to synchronously.

    Lazy is deliberate. Building the client is configuration work that cannot fail; *connecting* is
    the step that spawns a process or opens a socket, and deferring it to first use means a run with
    ten configured servers pays for the one or two a node actually needs, and a server that is broken
    costs nothing until something asks for it.

    Every method is safe to call from one thread; the lock is there because discovery and a tool call
    can race when tools are listed while a node is already running.
    """

    def __init__(self, spec: McpServerSpec, *, timeout_s: int = 30,
                 on_event: Callable[..., Any] | None = None) -> None:
        self.spec = spec
        self.timeout_s = float(spec.timeout_s or timeout_s or 30)
        self.on_event = on_event
        self.server_info: dict[str, Any] = {}
        self.protocol_version = ""
        self.capabilities: dict[str, Any] = {}
        self._transport: Any = None
        self._next_id = 0
        self._lock = threading.RLock()
        self._connected = False
        self._failure: str = ""

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def failure(self) -> str:
        """Why this server could not be used, empty when it worked. Quoted by every later refusal."""
        return self._failure

    def _build_transport(self) -> Any:
        if self.spec.transport == "stdio":
            return _StdioTransport(self.spec)
        return _HttpTransport(self.spec)

    def connect(self) -> None:
        """Perform the handshake, once. A second call is a no-op, and a failure is remembered.

        Remembering is the point: a server that will not start should be reported *once*, at
        discovery, and every later call against it should return the same explanation rather than
        respawning the process per call — which is how a broken server becomes a run that spawns
        hundreds of processes and never says why.
        """
        with self._lock:
            if self._connected:
                return
            if self._failure:
                raise McpError(self._failure)
            transport = self._build_transport()
            self._transport = transport
            try:
                transport.start()
                result = self._handshake(transport)
            except McpError as exc:
                self._failure = str(exc)
                self._teardown()
                raise
            except Exception as exc:  # noqa: BLE001 - no connection failure may escape
                self._failure = (
                    f"MCP server {self.spec.name!r} could not be connected: "
                    f"{type(exc).__name__}: {exc}")
                self._teardown()
                raise McpError(self._failure) from None
            self.capabilities = dict(result.get("capabilities") or {})
            self.server_info = dict(result.get("serverInfo") or {})
            self.protocol_version = str(result.get("protocolVersion") or "")
            self._connected = True
            self._log("mcp.connected", {
                "server": self.spec.name,
                "transport": self.spec.transport,
                "protocol_version": self.protocol_version,
                "server_info": self.server_info.get("name") or "",
            })

    def _handshake(self, transport: Any) -> dict[str, Any]:
        """`initialize` then `notifications/initialized`, with a protocol-version check.

        The check is the part that is not optional. The server answers with the revision it will
        speak, which may be older than the one asked for, and the spec requires the client to
        disconnect if it cannot support the answer. Proceeding anyway is how a client ends up
        interpreting a newer or older message shape and failing obscurely later, at a point where the
        real cause is no longer visible — so the refusal happens here, while the cause is still known.
        """
        result = self._exchange(transport, {
            "jsonrpc": "2.0", "id": self._mint_id(), "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": dict(_CLIENT_CAPABILITIES),
                "clientInfo": {"name": "agentorg", "version": _engine_version()},
            },
        })
        if "protocolVersion" not in result:
            raise McpError(
                f"MCP server {self.spec.name!r} answered the handshake without the required "
                f"protocolVersion and serverInfo: {redact(json.dumps(result)[:300])}")
        version = str(result.get("protocolVersion") or "")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise McpError(
                f"MCP server {self.spec.name!r} wants protocol version {version!r}, which this "
                f"client does not implement (it speaks: "
                f"{', '.join(sorted(SUPPORTED_PROTOCOL_VERSIONS))}). Refusing to continue, because "
                "a version this client cannot read would fail later and more confusingly.")
        self.protocol_version = version
        if isinstance(transport, _HttpTransport):
            # Set before the notification so every later request quotes the negotiated version and
            # any session the server handed back — a streamable-HTTP server rejects the next request
            # outright when either is missing.
            transport.protocol_version = version
        # A notification: no id, and no answer is expected or waited for.
        transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def close(self) -> None:
        """Release the server. Idempotent and silent, because it runs in teardown paths."""
        with self._lock:
            self._teardown()

    def _teardown(self) -> None:
        transport, self._transport = self._transport, None
        was_connected = self._connected
        self._connected = False
        if transport is None:
            return
        try:
            transport.close()
        except Exception:  # noqa: BLE001 - a teardown must never raise over its own bookkeeping
            pass
        if was_connected:
            self._log("mcp.closed", {"server": self.spec.name, "transport": self.spec.transport})

    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── protocol ────────────────────────────────────────────────────────────

    def _mint_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _exchange(self, transport: Any, message: dict[str, Any],
                  *, timeout_s: float | None = None) -> dict[str, Any]:
        """Send one request and return the matching JSON-RPC *result*.

        Returns the unwrapped result rather than the envelope, because the envelope is pure protocol
        and no caller has a use for it: the four messages this client sends all have an object result
        and nothing downstream should have to remember to peel one layer.

        Traffic that is not the answer is handled rather than dropped, and each kind differently:
        a *notification* from the server is a log line worth keeping; a *request* from the server is
        something this client has no way to serve — it declared no capabilities — so it is answered
        with `method not found` rather than left hanging, because a server blocked on a request it
        will never get an answer to is indistinguishable from a server that has crashed.
        """
        deadline = self.timeout_s if timeout_s is None else timeout_s
        request_id = message.get("id")
        with self._lock:
            if isinstance(transport, _HttpTransport):
                return self._require_result(transport.request(message, deadline), message)
            transport.send(message)
            while True:
                incoming = transport.receive(deadline)
                if isinstance(incoming, _Eof):
                    raise McpError(
                        f"MCP server {self.spec.name!r} closed its output while "
                        f"{message.get('method')!r} was in flight: {transport.cause()}")
                if incoming.get("id") == request_id:
                    return self._require_result(incoming, message)
                if "id" in incoming and incoming.get("method"):
                    self._answer_unsupported(transport, incoming)
                elif "id" not in incoming:
                    self._log("mcp.notification", {
                        "server": self.spec.name,
                        "method": str(incoming.get("method") or ""),
                    })

    def _require_result(self, response: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
        """Unwrap a JSON-RPC response's `result`, turning a protocol error into a sentence.

        The error's own `message` is kept verbatim because that is what the server author wrote, and
        it is far more useful than anything this module could construct — "tool not found", "no API
        key configured", "rate limited until 12:04". The method and the code are added because a bare
        message is unattributable when several servers are in play.

        A `result` that is not an object is wrapped rather than refused: the spec says it is an
        object, but the refusal that matters is the one the *server* sends, and fabricating a
        protocol error over a shape this client can still read would lose a working answer.
        """
        error = response.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            raise McpError(
                f"MCP server {self.spec.name!r} refused {message.get('method')!r} "
                f"(JSON-RPC error {code}): {redact(str(error.get('message') or '(no message)'))}")
        result = response.get("result")
        return result if isinstance(result, dict) else {"result": result}

    def _answer_unsupported(self, transport: Any, incoming: dict[str, Any]) -> None:
        try:
            transport.send({"jsonrpc": "2.0", "id": incoming.get("id"),
                            "error": {"code": _ERR_METHOD_NOT_FOUND,
                                      "message": f"{incoming.get('method')!r} is not supported by "
                                                 "this client"}})
        except Exception:  # noqa: BLE001 - a failed courtesy reply must not break the exchange
            pass

    def notify_cancelled(self, request_id: Any, reason: str) -> None:
        """Tell a server to stop work on a request whose answer is no longer wanted.

        The spec asks for this on timeout, and it is not bookkeeping: the timed-out call is often the
        expensive one, and a server left to finish it holds a connection, a rate-limit slot or a
        database cursor that the next call wants back.
        """
        transport = self._transport
        if transport is None:
            return
        try:
            transport.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                            "params": {"requestId": request_id, "reason": reason}})
        except Exception:  # noqa: BLE001 - cancelling is best effort by definition
            pass

    def list_tools(self) -> list[dict[str, Any]]:
        """Every tool the server advertises, following `nextCursor` to the last page.

        Pagination is followed rather than ignored because a server with more tools than fit one page
        would otherwise appear to offer a truncated set — and a model that cannot see a tool cannot
        ask for it, so the missing ones would be invisible rather than broken.
        """
        self.connect()
        transport = self._transport
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = self._exchange(transport, {
                "jsonrpc": "2.0", "id": self._mint_id(), "method": "tools/list",
                "params": params,
            })
            tools = result.get("tools")
            if isinstance(tools, list):
                collected.extend(item for item in tools if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return collected

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> McpCallResult:
        """Invoke one remote tool by its *server-native* name.

        The namespaced name the model sees is resolved to this by the bridge's routing table; this
        method deliberately knows nothing about namespacing, so the mapping has exactly one owner and
        cannot be half-applied in two places.
        """
        self.connect()
        transport = self._transport
        request: dict[str, Any] = {
            "jsonrpc": "2.0", "id": self._mint_id(), "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments or {})},
        }
        try:
            result = self._exchange(transport, request)
        except McpError as exc:
            if "did not answer within" in str(exc) or "timed out" in str(exc):
                self.notify_cancelled(request["id"], "client timed out")
            raise
        if not result:
            raise McpError(
                f"MCP server {self.spec.name!r} answered {name!r} with no result object at all")
        text, truncated = _flatten_content(result, name=name, server=self.spec.name)
        structured = result.get("structuredContent")
        return McpCallResult(
            text=text,
            # `isError: true` means the *tool* failed, which is a fact for the model to read, not a
            # transport fault. Keeping the two apart is what lets a node try the next approach
            # instead of treating the server as unavailable.
            is_error=bool(result.get("isError")),
            structured=structured if isinstance(structured, dict) else None,
            truncated=truncated,
        )

    # ── reporting ───────────────────────────────────────────────────────────

    def _log(self, kind: str, payload: dict[str, Any]) -> None:
        """Emit one observation, if anyone is listening. Never let reporting break the run."""
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:  # noqa: BLE001 - an observer must not break what it observes
            pass


def _engine_version() -> str:
    try:
        from . import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001 - a version string is never worth failing a handshake over
        return "0"


def _flatten_content(result: dict[str, Any], *, name: str, server: str) -> tuple[str, bool]:
    """Render a `CallToolResult` for a model, honouring the size cap.

    Every content block type the spec defines is handled, and the two that are *not* turned into raw
    text are the point of this function:

    - **Images and audio are described, never inlined.** Their payload is base64, which would burn
      tens of thousands of tokens to say nothing a text model can use. The note names the mime type
      and the size, so a reader knows what was returned and that it was not lost.
    - **Structured content is appended only when nothing textual came back.** Servers are told to
      serialise their structured result into a text block as well, so appending it unconditionally
      would pay for the same bytes twice.
    """
    blocks = result.get("content")
    parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text") or ""))
            elif kind == "image":
                parts.append(_binary_note("image", block))
            elif kind == "audio":
                parts.append(_binary_note("audio", block))
            elif kind == "resource":
                parts.append(_resource_text(block))
            elif kind == "resource_link":
                parts.append(f"[resource link: {block.get('uri')}]")
            else:
                parts.append(redact(json.dumps(block, default=str)))
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and not any(part.strip() for part in parts):
        # Servers are told to serialise a structured result into a text block *as well*, so this
        # normally has nothing to add. It earns its place for a server that sends only structured
        # content: without it, a working call would come back looking empty.
        parts.append(json.dumps(structured, indent=2, default=str, sort_keys=True))
    if not parts:
        # An empty content list is legal and means "it worked, there is nothing to say". Saying so
        # explicitly beats an empty string, which a model reads as a failed call.
        parts.append("(the tool returned no content)")
    body = "\n".join(part for part in parts if part != "")
    if len(body.encode("utf-8", errors="replace")) <= MAX_RESULT_BYTES:
        return body, False
    clipped = body.encode("utf-8", errors="replace")[:MAX_RESULT_BYTES].decode("utf-8", "replace")
    return (clipped + f"\n\n[truncated at {MAX_RESULT_BYTES} bytes; the full result from "
                      f"{server}.{name} is larger]"), True


def _binary_note(kind: str, block: dict[str, Any]) -> str:
    mime = block.get("mimeType") or "unknown type"
    data = block.get("data")
    size = len(data) if isinstance(data, str) else 0
    return (f"[{kind}: {mime}, {size} bytes base64 — not inlined, because a model cannot read "
            f"the bytes and they would cost more context than the call]")


def _resource_text(block: dict[str, Any]) -> str:
    resource = block.get("resource")
    if not isinstance(resource, dict):
        return "[embedded resource]"
    if isinstance(resource.get("text"), str):
        return str(resource["text"])
    if resource.get("blob"):
        return f"[embedded binary resource: {resource.get('uri') or 'unknown'}]"
    return f"[embedded resource: {resource.get('uri') or 'unknown'}]"


# ── the bridge ─────────────────────────────────────────────────────────────


def namespaced_tool_name(server: str, tool: str) -> str:
    """The name a remote tool is advertised under, in the registry and to the model."""
    return f"{MCP_TOOL_PREFIX}{server}{MCP_TOOL_SEPARATOR}{tool}"


class McpToolBridge:
    """Turns one or more servers' tools into the engine's own `Tool` objects.

    The bridge is the whole integration. Once `install(registry)` has run, `registry.specs()` offers
    the remote tools exactly as it offers `read_file`, a model calls them by name, and nothing
    downstream — the loop, the prompt builder, the provider adapters — can tell the difference. That
    indistinguishability is the requirement, and it is why nothing here is a subclass: the engine's
    `Tool` is a dataclass whose `spec()` produces the provider-neutral shape, so producing a `Tool`
    *is* the integration rather than a parallel implementation of it.

    Routing is a table, not a parse. `_routes[namespaced] == (server, native_tool)` is built from the
    list each server actually advertised, so a tool whose own name contains the separator still
    routes to the right server and the right tool. Parsing the advertised name back apart would work
    for every name except the ones that matter.
    """

    def __init__(self, *, timeout_s: int = 30, on_event: Callable[..., Any] | None = None,
                 diagnostics: Any = None,
                 client_factory: Callable[..., McpClient] | None = None) -> None:
        self.timeout_s = int(timeout_s or 30)
        self.on_event = on_event
        self.diagnostics = diagnostics
        #: Injected by tests to observe connection attempts; the default builds the real transport.
        self._client_factory = client_factory or (
            lambda spec: McpClient(spec, timeout_s=self.timeout_s, on_event=on_event))
        self.clients: dict[str, McpClient] = {}
        #: server name -> the reason it is unusable. Reported once, quoted on every later call.
        self.failures: dict[str, str] = {}
        #: namespaced name -> (server, the server's own tool name). Built from the advertised list.
        self._routes: dict[str, tuple[str, str]] = {}
        self._installed: list[Tool] = []

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: Any, *, on_event: Callable[..., Any] | None = None,
                    diagnostics: Any = None,
                    client_factory: Callable[..., McpClient] | None = None) -> "McpToolBridge":
        """Build a bridge for every usable entry in `mcp`. Never raises.

        A malformed entry is recorded in `failures` rather than propagated, for the same reason a
        server that will not start is: the run has nine other capabilities configured and losing them
        because one line of JSON was misspelled would be a poor trade. `enabled: false` on the whole
        section yields an empty bridge, which is the shape "MCP is off" takes everywhere else.
        """
        section = config
        timeout_s = _positive_int(getattr(section, "timeout_s", 30), default=30) or 30
        bridge = cls(timeout_s=timeout_s, on_event=on_event, diagnostics=diagnostics,
                     client_factory=client_factory)
        specs, failures = iter_server_specs(section)
        bridge.failures.update(failures)
        for spec in specs:
            bridge.clients[spec.name] = bridge._client_factory(spec)
        for name, reason in failures.items():
            bridge._report(name, reason)
        return bridge

    def add_server(self, spec: McpServerSpec) -> None:
        """Add one server explicitly, for a caller that built a spec rather than reading config."""
        self.clients[spec.name] = self._client_factory(spec)

    # ── discovery ───────────────────────────────────────────────────────────

    def discover(self) -> list[Tool]:
        """Connect to every server, list its tools, and return the `Tool` objects.

        A server that fails at any step is skipped and reported; the loop moves on to the next one.
        That *is* the failure-isolation property, and it is implemented here rather than in the tool
        call because a server that cannot be listed is a server whose tools should never be
        advertised — offering them and refusing every call would teach a model to distrust the list.

        A server is reported the first time it fails and not again. A second discovery pass — which
        happens whenever a node is built — would otherwise re-announce a known-broken server on every
        step of a long run, and a warning nobody can act on because they already read it is a warning
        that trains a reader to stop reading warnings.
        """
        tools: list[Tool] = []
        for name in sorted(self.clients):
            client = self.clients[name]
            first_failure = name not in self.failures
            try:
                advertised = client.list_tools()
            except McpError as exc:
                self.failures.setdefault(name, str(exc))
            except Exception as exc:  # noqa: BLE001 - a server must never break discovery
                self.failures.setdefault(name, f"{type(exc).__name__}: {exc}")
            else:
                for entry in advertised:
                    tool = self._build_tool(name, entry)
                    if tool is not None:
                        tools.append(tool)
                continue
            if first_failure:
                self._report(name, self.failures[name])
        return tools

    def _build_tool(self, server: str, entry: dict[str, Any]) -> Tool | None:
        """One advertised tool as an engine `Tool`, or None when it cannot be represented.

        The JSON Schema is copied rather than converted. MCP defines `inputSchema` as the same JSON
        Schema the engine already advertises to providers, so mapping it "straight onto
        `Tool.parameters`" is genuinely a copy — and any translation would be a place for the two
        schemas to disagree about a server's parameters.
        """
        native = str(entry.get("name") or "").strip()
        if not native:
            self._report(server, "a tool with no name was advertised and cannot be called")
            return None
        namespaced = namespaced_tool_name(server, native)
        if namespaced in self._routes and self._routes[namespaced] != (server, native):
            # Two servers cannot collide here because the server name is part of the key; a collision
            # means one server advertised the same name twice, and the first wins so that a call is
            # never routed to an entry the model was not shown.
            return None
        self._routes[namespaced] = (server, native)
        description = str(entry.get("description") or entry.get("title") or "").strip()
        annotations = entry.get("annotations")
        read_only = bool(isinstance(annotations, dict) and annotations.get("readOnlyHint"))
        return Tool(
            name=namespaced,
            # The origin is stated to the model rather than hidden: when a capability lives in
            # someone else's service, "which service" is part of understanding what it does and what
            # it might cost.
            description=(f"[mcp:{server}] {description}" if description
                         else f"[mcp:{server}] (the server gave no description)"),
            parameters=_normalise_schema(entry.get("inputSchema")),
            handler=self._make_handler(namespaced),
            # An unannotated tool is treated as mutating, which is the spec's own default and the
            # safe reading: a read-only run must not change the workspace through a server that
            # simply did not say whether its tool writes.
            mutates=not read_only,
        )

    def _make_handler(self, namespaced: str) -> Callable[[dict[str, Any]], ToolResult]:
        def handler(arguments: dict[str, Any]) -> ToolResult:
            return self.call(namespaced, arguments)

        return handler

    # ── the call path ───────────────────────────────────────────────────────

    def call(self, namespaced: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Invoke a namespaced remote tool, turning every failure into a refusal.

        This is the method the failure-isolation promise rests on, and it catches `Exception` rather
        than `McpError` on purpose: the transport runs a subprocess, threads and sockets, and the
        range of things that can go wrong there is not enumerable from here. A node that dies because
        a third-party server was badly written is the one outcome this module exists to prevent.
        """
        route = self._routes.get(namespaced)
        if route is None:
            # Reached when a model calls a tool that was advertised by a server which has since been
            # dropped, or invents a name. The refusal lists what is actually callable.
            known = ", ".join(sorted(self._routes)) or "(no MCP tools are available)"
            return ToolResult(
                False, f"no MCP tool named {namespaced!r}; available MCP tools: {known}")
        server, native = route
        client = self.clients.get(server)
        if client is None:
            return ToolResult(
                False, f"{namespaced} is unavailable: server {server!r} is no longer configured")
        try:
            result = client.call_tool(native, dict(arguments or {}))
        except McpError as exc:
            return ToolResult(False, f"MCP call {namespaced} failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - see the docstring: a remote server cannot kill a run
            self.failures[server] = f"{type(exc).__name__}: {exc}"
            return ToolResult(
                False,
                f"MCP call {namespaced} failed inside the client: {type(exc).__name__}: {exc}. "
                f"The server {server!r} may be unhealthy; other servers are unaffected.")
        if result.is_error:
            # The tool ran and refused. This is the model's to read and act on, so it is phrased as
            # the server phrased it rather than as an engine failure.
            return ToolResult(False, f"{namespaced} reported an error: {result.text}")
        return ToolResult(True, result.text, result.truncated, paths=[])

    # ── registry integration ────────────────────────────────────────────────

    def install(self, registry: Any) -> int:
        """Advertise every remote tool on a `ToolRegistry`. Returns how many were registered.

        Registration is deliberately unconditional with respect to capability: the registry's own
        gate still applies, because these are ordinary `Tool`s. A remote tool marked `mutates` is
        refused in a read-only run by the same line of code that refuses `write_file`, which is what
        "indistinguishable from a native tool" has to mean for the property to be worth anything.
        """
        tools = self.discover()
        for tool in tools:
            registry.register(tool)
        self._installed = tools
        if tools:
            self._log("mcp.tools.installed", {
                "count": len(tools),
                "servers": sorted({server for server, _ in self._routes.values()}),
            })
        return len(tools)

    def installed(self) -> list[str]:
        return [tool.name for tool in self._installed]

    def route_for(self, namespaced: str) -> tuple[str, str] | None:
        """The exact `(server, native tool)` a namespaced name maps to, or None."""
        return self._routes.get(namespaced)

    def close(self) -> None:
        """Close every client, reaping every stdio child. Idempotent and never raises."""
        for client in self.clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001 - teardown is where exceptions go to be swallowed
                pass

    def __enter__(self) -> "McpToolBridge":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── reporting ───────────────────────────────────────────────────────────

    def _report(self, server: str, reason: str) -> None:
        """Record and announce one unusable server, on every channel that might be watching."""
        if self.diagnostics is not None:
            try:
                self.diagnostics.warning("mcp.server.unavailable",
                                         message=f"{server}: {reason}")
            except Exception:  # noqa: BLE001 - diagnostics is best-effort like every observer
                pass
        self._log("mcp.server.unavailable", {"server": server, "reason": redact(str(reason))})

    def _log(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:  # noqa: BLE001 - an observer must not break what it observes
            pass


def _normalise_schema(schema: Any) -> dict[str, Any]:
    """Coerce an advertised `inputSchema` into the object schema the engine advertises.

    MCP requires `type: "object"`, so this is normally a copy. The coercion exists for the servers
    that get it slightly wrong — a missing `properties`, a `required` that is not a list of strings —
    because the alternative is a provider adapter failing on a schema the engine passed through
    unexamined, which surfaces as a broken model call with no trace back to the server that caused
    it. A missing schema becomes the permissive empty object rather than a refusal: a tool with no
    parameters is callable, and refusing it would lose a working capability over a cosmetic omission.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    out["type"] = "object"
    properties = out.get("properties")
    out["properties"] = properties if isinstance(properties, dict) else {}
    required = out.get("required")
    if isinstance(required, (list, tuple)):
        cleaned = [str(item) for item in required if isinstance(item, str)]
        if cleaned:
            out["required"] = cleaned
        else:
            out.pop("required", None)
    else:
        out.pop("required", None)
    return out


def attach(config: Any, registry: Any, *, on_event: Callable[..., Any] | None = None,
           diagnostics: Any = None) -> McpToolBridge | None:
    """Install MCP tools on a registry, or return None when there is nothing to install.

    The one entry point a caller needs, and it cannot fail: a misconfigured section, an unreachable
    server and a server that dies during `tools/list` all end in a bridge with a `failures` map
    rather than an exception. A run must not fail to *start* because an optional capability was
    unavailable — the same rule `hooks.Lifecycle.attach` follows, for the same reason.
    """
    try:
        bridge = McpToolBridge.from_config(config, on_event=on_event, diagnostics=diagnostics)
        if not bridge.clients:
            return None
        bridge.install(registry)
        return bridge
    except Exception as exc:  # noqa: BLE001 - an optional capability must never block a run
        if diagnostics is not None:
            try:
                diagnostics.warning("mcp.attach.failed", message=f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 - diagnostics is best-effort too
                pass
        return None
