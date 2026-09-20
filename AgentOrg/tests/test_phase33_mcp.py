#!/usr/bin/env python3
"""Phase 33 tests — the MCP client, against real servers and real processes.

The engine had no Model Context Protocol support, which meant every new capability — an issue
tracker, a database, a browser — had to become a bespoke tool in `tools.py`. MCP is the standard
answer to that, and the thing that makes it worth having is not the protocol but a promise about
failure: a server the engine cannot reach is a *missing capability*, never a dead run.

So the tests are written against the properties rather than the code:

1. **The handshake is the published one, and it is proved over a real pipe.** A Python script
   speaking JSON-RPC 2.0 on stdin/stdout is spawned as a genuine subprocess and driven through
   `initialize` → `notifications/initialized` → `tools/list` → `tools/call`. Mocking the transport
   here would test the mock: the failure this guards against is a client whose wire format is
   plausible and wrong, which only a real server can catch. The fixture also *asserts* the order and
   the shapes it receives, so a client that got the handshake wrong fails rather than passing against
   a server written to match it.
2. **Two servers may offer the same tool name.** Both are advertised, and each namespaced name routes
   back to the exact server and the exact native name — asserted for a tool whose own name contains
   the separator, which is where re-parsing the namespaced string would silently mis-route.
3. **A server that will not start is reported, and everything else still works.** The failure is a
   recorded reason, not an exception, and the other servers keep answering.
4. **A server that dies mid-call yields a failed result, not a raised one.** The child is killed
   between the handshake and the call, which is the shape of a crash in an unattended run.
5. **A timeout is honoured, and the child is not left running.** The fixture hangs on request; the
   call returns a refusal inside the bound, and the process is gone afterwards.
6. **The bridged tools are the engine's own.** Same dataclass, same `spec()` output as the native
   tools, and the registry's existing gate — including read-only — applies to them unchanged.
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import McpConfig
from engine.mcp import (
    MCP_PROTOCOL_VERSION,
    McpClient,
    McpError,
    McpServerSpec,
    McpToolBridge,
    namespaced_tool_name,
)
from engine.org.agent import AgentSpec
from engine.tools import Tool, ToolRegistry


# ── the fixture server ─────────────────────────────────────────────────────
#
# A real MCP server in ~100 lines, because the honest test of a protocol client is a second
# implementation of the protocol. It records what it received into a log file so a test can assert
# the *client's* messages rather than only the server's replies — a client that sent the right
# requests in the wrong order would otherwise pass.

SERVER_SOURCE = r'''
import json, os, select, sys, time

LOG = os.environ.get("MCP_TEST_LOG", "")
NAME = os.environ.get("MCP_TEST_NAME", "fixture")
TOOLS = json.loads(os.environ.get("MCP_TEST_TOOLS") or "[]")
DIE_AFTER_HANDSHAKE = os.environ.get("MCP_TEST_DIE_AFTER_HANDSHAKE") == "1"
HANG_ON = os.environ.get("MCP_TEST_HANG_ON", "")
BAD_VERSION = os.environ.get("MCP_TEST_BAD_VERSION", "")
VERSION = os.environ.get("MCP_TEST_VERSION", "2025-06-18")


def log(entry):
    if not LOG:
        return
    with open(LOG, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def hang(name):
    """Never answer, but keep reading — the shape of a server that is slow rather than dead.

    It polls rather than sleeps so that a cancellation arriving *during* the hang is actually
    observed and logged. A sleeping server would look identical from the client's side and would make
    the cancellation untestable, which is exactly the kind of thing this suite exists to catch.
    """
    log({"hanging": name})
    deadline = time.time() + 600
    while time.time() < deadline:
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not ready:
            continue
        line = sys.stdin.readline()
        if not line:
            return
        try:
            msg = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        log({"received": msg})
        if msg.get("method") == "notifications/cancelled":
            log({"cancelled": msg.get("params")})


while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    log({"received": msg})
    method = msg.get("method")
    if method == "tools/call" and (msg.get("params") or {}).get("name") == HANG_ON:
        hang(HANG_ON)
    elif method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": BAD_VERSION or VERSION,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": NAME, "version": "9.9.9"},
        }})
    elif method == "notifications/initialized":
        if DIE_AFTER_HANDSHAKE:
            log({"dying": True})
            os._exit(3)
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        if name == "boom":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "content": [{"type": "text", "text": "the tool refused"}], "isError": True}})
        elif name == "missing":
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                "code": -32602, "message": "Tool missing not found"}})
        else:
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [
                {"type": "text", "text": "called %s on %s with %s" % (
                    name, NAME, json.dumps(params.get("arguments") or {}, sort_keys=True))},
            ]}})
    elif method == "notifications/cancelled":
        log({"cancelled": msg.get("params")})
    else:
        send({"jsonrpc": "2.0", "id": msg.get("id"), "error": {
            "code": -32601, "message": "unknown method %s" % method}})
'''


@pytest.fixture(scope="module")
def server_script():
    """The fixture server's script, written once for the whole module.

    Built with `tempfile` rather than `tmp_path_factory` on purpose: the suite runs under two
    runners — real pytest and `run_tests.py` — and only one of them provides that factory. A test
    that silently *skips* under one runner is a test that proves nothing there, so the fixture owns
    its own directory and works identically under both.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="agentorg-mcp-fixture-") as directory:
        path = pathlib.Path(directory) / "fixture_server.py"
        path.write_text(SERVER_SOURCE)
        yield path


def _tool_json(name: str, *, description: str = "", schema: dict | None = None,
               read_only: bool = False) -> dict:
    entry = {
        "name": name,
        "description": description or f"{name} tool",
        "inputSchema": schema or {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    }
    if read_only:
        entry["annotations"] = {"readOnlyHint": True}
    return entry


def stdio_spec(server_script: pathlib.Path, name: str, *, tools: list[dict] | None = None,
               log: pathlib.Path | None = None, timeout_s: int = 10,
               env: dict | None = None) -> McpServerSpec:
    """A spec that spawns a real MCP server with the fixture's knobs set through its environment."""
    environment = {
        "MCP_TEST_NAME": name,
        "MCP_TEST_TOOLS": json.dumps(tools if tools is not None else [_tool_json("search")]),
        # The tool the fixture makes never answer. Set from the advertised list rather than from the
        # knob, so a test cannot advertise a tool it forgot to make hang — which is exactly how this
        # suite's first version passed a call it expected to time out.
        "MCP_TEST_HANG_ON": "hang",
    }
    if log is not None:
        environment["MCP_TEST_LOG"] = str(log)
    environment.update(env or {})
    return McpServerSpec(
        name=name, transport="stdio", command=sys.executable,
        args=[str(server_script)], env=environment, timeout_s=timeout_s)


def read_log(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def received(path: pathlib.Path, kind: str) -> list[dict]:
    entries = []
    for record in read_log(path):
        if "received" in record and record["received"].get("method") == kind:
            entries.append(record["received"])
    return entries


def wait_for_method(path: pathlib.Path, method: str, timeout: float = 5.0) -> bool:
    """Wait until the server has logged a given method, or give up.

    A notification is fire-and-forget by design — the client sends it and does not wait for a reply
    because the protocol defines no reply — so the server may not have *read* it yet at the instant a
    test looks. Asserting immediately would make this suite flaky in exactly the way that teaches a
    reader to re-run failures instead of trusting them.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if received(path, method):
            return True
        time.sleep(0.02)
    return False


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def make_agent(*capabilities: str) -> AgentSpec:
    return AgentSpec(id="ag_alice", name="Alice", title="Engineer",
                     skills=["backend-developer"], provider="fake", model="m",
                     context_window=32768, capabilities=list(capabilities))


# ── 1. the handshake, over a real subprocess ───────────────────────────────


def test_a_real_stdio_server_completes_the_handshake(server_script, tmp_path):
    """The published sequence, driven over a genuine pipe against a second implementation.

    This is the test that would have caught a guessed wire format. It asserts both directions: the
    client understood the server's replies, and the server saw the client's messages in the order the
    spec requires.
    """
    log = tmp_path / "server.log"
    spec = stdio_spec(server_script, "fixture", log=log,
                      tools=[_tool_json("search", description="Search things")])
    client = McpClient(spec, timeout_s=10)
    try:
        client.connect()
        assert client.connected
        assert client.protocol_version == MCP_PROTOCOL_VERSION
        assert client.server_info["name"] == "fixture"

        seen = [m.get("method") for m in received(log, "initialize")]
        assert seen == ["initialize"]
        assert wait_for_method(log, "notifications/initialized")
        # The order is the requirement: a server is told to ignore requests until it has answered
        # `initialize`, so a client that lists tools first is talking to a closed door.
        all_methods = [record["received"].get("method") for record in read_log(log)
                       if "received" in record]
        assert all_methods[0] == "initialize"
        assert all_methods.index("initialize") < all_methods.index("notifications/initialized")

        init = received(log, "initialize")[0]
        assert init["jsonrpc"] == "2.0"
        assert init["params"]["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert init["params"]["clientInfo"]["name"] == "agentorg"
    finally:
        client.close()


def test_the_handshake_is_followed_by_a_real_tools_list_and_call(server_script, tmp_path):
    """`tools/list` then `tools/call`, with the arguments arriving intact and the result parsed."""
    log = tmp_path / "server.log"
    spec = stdio_spec(server_script, "fixture", log=log, tools=[
        _tool_json("search", description="Search the world"),
        _tool_json("add", read_only=True, schema={
            "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"]}),
    ])
    client = McpClient(spec, timeout_s=10)
    try:
        advertised = client.list_tools()
        assert [t["name"] for t in advertised] == ["search", "add"]
        assert advertised[0]["description"] == "Search the world"

        result = client.call_tool("search", {"q": "hello world"})
        assert not result.is_error
        assert "called search on fixture" in result.text
        assert json.dumps({"q": "hello world"}, sort_keys=True) in result.text
    finally:
        client.close()


def test_a_server_that_refuses_a_tool_reports_it_rather_than_raising(server_script, tmp_path):
    """`isError: true` is the *tool* failing, which is the model's to read and act on."""
    spec = stdio_spec(server_script, "fixture",
                      tools=[_tool_json("boom"), _tool_json("missing")])
    client = McpClient(spec, timeout_s=10)
    try:
        refused = client.call_tool("boom", {})
        assert refused.is_error
        assert "the tool refused" in refused.text
        # A protocol-level error is a different thing and must not be flattened into the same shape:
        # "the tool said no" and "there is no such tool" call for different next steps.
        with pytest.raises(McpError) as raised:
            client.call_tool("missing", {})
        assert "Tool missing not found" in str(raised.value)
    finally:
        client.close()


def test_a_version_this_client_cannot_speak_is_refused_at_the_handshake(server_script):
    """The check the spec requires, at the point where the cause is still visible.

    A client that carried on would mis-read a later message and fail somewhere unrelated. The
    refusal names both versions so a person can act on it.
    """
    spec = stdio_spec(server_script, "fixture",
                      env={"MCP_TEST_BAD_VERSION": "1995-01-01"})
    client = McpClient(spec, timeout_s=10)
    try:
        with pytest.raises(McpError) as raised:
            client.connect()
        message = str(raised.value)
        assert "1995-01-01" in message and "does not implement" in message
        assert not client.connected
        # Remembered, so a caller that asks twice gets the same answer instead of a second spawn.
        with pytest.raises(McpError):
            client.connect()
    finally:
        client.close()


def test_an_older_supported_version_is_accepted(server_script, tmp_path):
    """A server may negotiate *down*; refusing that would make the client useless for no gain."""
    spec = stdio_spec(server_script, "fixture",
                      env={"MCP_TEST_VERSION": "2024-11-05"},
                      tools=[_tool_json("search")])
    client = McpClient(spec, timeout_s=10)
    try:
        client.connect()
        assert client.protocol_version == "2024-11-05"
    finally:
        client.close()


def test_the_stdio_child_is_terminated_by_close(server_script):
    """Orphan processes are how a crashed overnight run fills a machine."""
    spec = stdio_spec(server_script, "fixture")
    client = McpClient(spec, timeout_s=10)
    client.connect()
    pid = client._transport._proc.pid
    assert alive(pid)
    client.close()
    assert not alive(pid)
    client.close()  # idempotent: teardown runs on paths that may already have run


def test_close_reaps_a_launchers_grandchild(tmp_path):
    """The real shape of a launcher: a wrapper whose *work* happens in a grandchild.

    This is a regression test for a bug found while writing this module. The first version swept the
    process group only on the *escalation* path, and looked the group id up at teardown time from a
    pid that had already been reaped. A wrapper that exits politely on end-of-input — which is the
    advertised shutdown — therefore left its grandchild, the actual server, running for the life of
    the machine while every line of the teardown appeared to succeed.

    The wrapper here answers the handshake and spawns a child, so the assertion is made while the
    connection is healthy and the grandchild is provably alive. The grandchild's pid is found by
    process group rather than by parent, because the parent is what gets reaped first.
    """
    script = tmp_path / "launcher.py"
    script.write_text(
        "import json, subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],\n"
        "                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "                 stderr=subprocess.DEVNULL)\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if m.get('method') == 'initialize':\n"
        "        send = {'jsonrpc': '2.0', 'id': m['id'], 'result': {\n"
        "            'protocolVersion': '2025-06-18', 'capabilities': {},\n"
        "            'serverInfo': {'name': 'launcher', 'version': '1'}}}\n"
        "        sys.stdout.write(json.dumps(send) + '\\n'); sys.stdout.flush()\n"
    )
    spec = McpServerSpec(name="launcher", transport="stdio", command=sys.executable,
                         args=[str(script)], timeout_s=10)
    client = McpClient(spec, timeout_s=10)
    client.connect()
    transport = client._transport
    assert transport is not None and transport._pgid is not None
    grandchild = None
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and grandchild is None:
        grandchild = _other_process_in_group(transport._pgid, transport._proc.pid)
        time.sleep(0.05)
    assert grandchild is not None, "the fixture launcher never started a grandchild"
    assert alive(grandchild)
    client.close()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and alive(grandchild):
        time.sleep(0.05)
    assert not alive(grandchild), (
        "a launcher's grandchild survived close(), so a real `npx`-style server would be left "
        "running as an orphan for the life of the machine")


def _other_process_in_group(pgid: int, exclude_pid: int) -> int | None:
    """Any surviving process in a group other than the one that leads it.

    Found through `ps` rather than `pgrep -P`: by the time the grandchild matters, its parent has
    already exited, so the parent-child link is exactly what is no longer available.
    """
    try:
        out = subprocess.run(["ps", "-o", "pid=,pgid=", "-A"], capture_output=True,
                             text=True).stdout
    except OSError:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, group = int(parts[0]), int(parts[1])
        if group == pgid and pid != exclude_pid:
            return pid
    return None


# ── 2. namespacing two servers that offer the same tool ────────────────────


def test_two_servers_offering_the_same_tool_are_both_reachable(server_script, tmp_path):
    """The naming problem solved deliberately, and the mapping back proved exact.

    `alpha` also offers a tool whose own name contains the separator (`github__create_issue`). That
    is the case where splitting the advertised name back apart mis-routes: a client that parsed
    `mcp__alpha__github__create_issue` would send `create_issue` to a server called `alpha__github`.
    The routing table is built from what each server advertised, so the assertion below is exact.
    """
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "alpha", tools=[
        _tool_json("search", description="search on alpha"),
        _tool_json("github__create_issue", description="tricky name"),
    ]))
    bridge.add_server(stdio_spec(server_script, "beta", tools=[
        _tool_json("search", description="search on beta"),
    ]))
    try:
        tools = bridge.discover()
        names = sorted(t.name for t in tools)
        assert names == [
            "mcp__alpha__github__create_issue",
            "mcp__alpha__search",
            "mcp__beta__search",
        ]
        assert bridge.route_for("mcp__alpha__search") == ("alpha", "search")
        assert bridge.route_for("mcp__beta__search") == ("beta", "search")
        assert bridge.route_for("mcp__alpha__github__create_issue") == (
            "alpha", "github__create_issue")

        alpha = bridge.call("mcp__alpha__search", {"q": "x"})
        beta = bridge.call("mcp__beta__search", {"q": "x"})
        assert alpha.ok and "on alpha" in alpha.text
        assert beta.ok and "on beta" in beta.text
        tricky = bridge.call("mcp__alpha__github__create_issue", {})
        assert tricky.ok and "github__create_issue on alpha" in tricky.text
    finally:
        bridge.close()


def test_the_namespaced_name_is_the_only_thing_the_model_sees(server_script):
    """A model must not be offered two identical names, and must be told where each one lives."""
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "alpha", tools=[_tool_json("search")]))
    try:
        tools = bridge.discover()
        assert len({tool.name for tool in tools}) == len(tools)
        assert tools[0].name == namespaced_tool_name("alpha", "search")
        assert "[mcp:alpha]" in tools[0].description
    finally:
        bridge.close()


def test_every_advertised_tool_routes_back_exactly(server_script):
    """The round trip, over every advertised name: no name is left half-mapped."""
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "alpha", tools=[
        _tool_json("one"), _tool_json("two"), _tool_json("__"), _tool_json("a__b__c")]))
    try:
        for tool in bridge.discover():
            route = bridge.route_for(tool.name)
            assert route is not None, f"{tool.name} has no route back"
            server, native = route
            assert tool.name == namespaced_tool_name(server, native)
    finally:
        bridge.close()


# ── 3. a server that fails to start ────────────────────────────────────────


def test_a_server_with_no_executable_is_reported_and_the_run_continues(server_script, tmp_path):
    """The property the whole module exists for: a broken server is a missing capability."""
    diag = _Recorder()
    bridge = McpToolBridge(timeout_s=10, diagnostics=diag)
    bridge.add_server(McpServerSpec(name="ghost", transport="stdio",
                                    command="definitely-not-installed-anywhere-12345"))
    bridge.add_server(stdio_spec(server_script, "good", tools=[_tool_json("search")]))
    try:
        tools = bridge.discover()
        assert [t.name for t in tools] == ["mcp__good__search"]
        assert "ghost" in bridge.failures
        assert "no executable" in bridge.failures["ghost"]
        # Reported on the diagnostics channel, so an unattended run can say why a capability is gone.
        assert any("ghost" in record for record in diag.warnings)
        assert bridge.call("mcp__good__search", {"q": "still working"}).ok
    finally:
        bridge.close()


def test_a_server_that_exits_during_the_handshake_is_reported(server_script, tmp_path):
    """A crash at startup is the most common way a bad integration shows up."""
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "flaky",
                                 env={"MCP_TEST_DIE_AFTER_HANDSHAKE": "1"}))
    bridge.add_server(stdio_spec(server_script, "good", tools=[_tool_json("search")]))
    try:
        tools = bridge.discover()
        assert [t.name for t in tools] == ["mcp__good__search"]
        assert "flaky" in bridge.failures
        assert "closed its output" in bridge.failures["flaky"] or "exit" in bridge.failures["flaky"]
    finally:
        bridge.close()


def test_a_malformed_config_entry_does_not_cost_the_other_servers(server_script, tmp_path):
    """One misspelled line must not take nine working servers down with it.

    `McpConfig` itself already refuses a non-object entry, so the entries that can actually reach
    this client are the ones that pass the dataclass and fail here: no command, no url, or both.
    Those are the shapes a person writes by hand, and each must cost only its own server.
    """
    config = McpConfig(servers={
        "nocommand": {"command": ""},            # a command that is present but empty
        "nourl": {"args": ["-y", "x"]},          # stdio arguments with nothing to run
        "disabled": {"command": "x", "enabled": False},
        "good": {"command": sys.executable, "args": [str(server_script)],
                 "env": {"MCP_TEST_NAME": "good",
                         "MCP_TEST_TOOLS": json.dumps([_tool_json("search")]),
                         "MCP_TEST_HANG_ON": "hang"}},
    })
    bridge = McpToolBridge.from_config(config)
    try:
        assert set(bridge.clients) == {"good"}
        assert set(bridge.failures) == {"nocommand", "nourl"}
        assert "neither `command`" in bridge.failures["nocommand"]
        assert "neither `command`" in bridge.failures["nourl"]
        # A server the operator switched off is not a failure, so it is not reported as one: a
        # failure list that includes deliberate choices is a list a reader learns to ignore.
        assert "disabled" not in bridge.failures
        # The one good server still works, which is the whole promise.
        assert [t.name for t in bridge.discover()] == ["mcp__good__search"]
    finally:
        bridge.close()


def test_a_non_object_entry_is_refused_and_isolated(server_script, tmp_path, monkeypatch):
    """The same isolation when the malformed entry never even gets past the config dataclass.

    `McpConfig` rejects `mcp.servers['x'] = "string"` at construction, which is the right place for
    it and means the client normally never sees one. This drives the parser directly with that shape
    so the client's own isolation is proved rather than inherited — the config layer is owned by
    another part of the engine and this module must not depend on its strictness.
    """
    from engine.mcp import iter_server_specs

    class Loose:
        enabled = True
        timeout_s = 30
        servers = {"strings": "not-an-object", "good": {"command": sys.executable}}

    specs, failures = iter_server_specs(Loose())
    assert [s.name for s in specs] == ["good"]
    assert "must be an object" in failures["strings"]


def test_the_whole_mcp_section_can_be_turned_off():
    bridge = McpToolBridge.from_config(McpConfig(enabled=False, servers={
        "x": {"command": "whatever"}}))
    assert bridge.clients == {}
    assert bridge.discover() == []
    bridge.close()


# ── 4. a server that dies mid-call ─────────────────────────────────────────


def test_a_server_that_dies_mid_call_yields_a_failed_result(server_script, tmp_path):
    """The exact shape of an unattended failure: connected, listed, then gone.

    An exception here would end the node and lose the work it had already done, so the assertion is
    that a `ToolResult` comes back at all — and that it explains itself.
    """
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "dying", tools=[_tool_json("search")]))
    bridge.add_server(stdio_spec(server_script, "good", tools=[_tool_json("search")]))
    try:
        assert len(bridge.discover()) == 2
        client = bridge.clients["dying"]
        pid = client._transport._proc.pid
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.3)

        result = bridge.call("mcp__dying__search", {"q": "x"})
        assert not result.ok
        assert "mcp__dying__search" in result.text
        assert "closed its output" in result.text or "is not running" in result.text
        # And the other server is untouched — the isolation is the point.
        assert bridge.call("mcp__good__search", {"q": "x"}).ok
    finally:
        bridge.close()


def test_the_registry_call_path_returns_a_refusal_not_an_exception(server_script, tmp_path):
    """The same failure, reached the way a node reaches it: through `ToolRegistry.call`."""
    registry = ToolRegistry(workspace_root=tmp_path, agent=make_agent("read:*", "write:*"))
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "dying", tools=[_tool_json("search")]))
    try:
        assert bridge.install(registry) == 1
        os.kill(bridge.clients["dying"]._transport._proc.pid, signal.SIGKILL)
        time.sleep(0.3)
        result = registry.call("mcp__dying__search", {"q": "x"})
        assert not result.ok
        assert "failed" in result.text
    finally:
        bridge.close()


def test_a_tool_the_server_never_advertised_is_refused_with_the_list(server_script, tmp_path):
    """A model can invent a name; the refusal has to tell it what it *can* call."""
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "alpha", tools=[_tool_json("search")]))
    try:
        bridge.discover()
        result = bridge.call("mcp__alpha__invented", {})
        assert not result.ok
        assert "no MCP tool named" in result.text
        assert "mcp__alpha__search" in result.text
    finally:
        bridge.close()


# ── 5. the timeout ─────────────────────────────────────────────────────────


def test_a_timeout_is_honoured_and_the_child_is_not_left_running(server_script, tmp_path):
    """A run that stops at 3am with no event is the failure this bound exists to prevent."""
    log = tmp_path / "server.log"
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "slow", timeout_s=1, log=log,
                                 tools=[_tool_json("hang"), _tool_json("search")]))
    try:
        bridge.discover()
        pid = bridge.clients["slow"]._transport._proc.pid
        started = time.monotonic()
        result = bridge.call("mcp__slow__hang", {})
        elapsed = time.monotonic() - started
        assert not result.ok
        assert "did not answer" in result.text
        # Bounded, with generous room for the subprocess and the queue handoff on a loaded machine.
        assert elapsed < 8.0, f"the call took {elapsed:.1f}s against a 1s timeout"
        # The server was *told* to stop, rather than left working on an answer nobody will read. The
        # fixture polls during its hang precisely so this is observable.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not any("cancelled" in r for r in read_log(log)):
            time.sleep(0.05)
        assert any(record.get("cancelled") for record in read_log(log)), \
            "the timed-out request was not cancelled"
        # And it is reaped on close, so the timeout did not orphan anything.
        bridge.close()
        assert not alive(pid)
    finally:
        bridge.close()


def test_a_hanging_server_does_not_stop_a_second_one_answering(server_script):
    """Isolation under the failure that is hardest to notice: a server that never replies."""
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "slow", timeout_s=1,
                                 tools=[_tool_json("hang"), _tool_json("search")]))
    bridge.add_server(stdio_spec(server_script, "good", tools=[_tool_json("search")]))
    try:
        bridge.discover()
        assert not bridge.call("mcp__slow__hang", {}).ok
        assert bridge.call("mcp__good__search", {"q": "fine"}).ok
    finally:
        bridge.close()


# ── 6. the tools are the engine's own ──────────────────────────────────────


def test_the_bridged_tools_are_indistinguishable_from_native_ones(server_script, tmp_path):
    """Same dataclass, same advertised spec shape, same registry — nothing downstream can tell."""
    registry = ToolRegistry(workspace_root=tmp_path, agent=make_agent("*:*"))
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "alpha", tools=[
        _tool_json("search", description="Search things", schema={
            "type": "object",
            "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["q"],
        }),
    ]))
    try:
        assert bridge.install(registry) == 1
        bridged = registry._tools["mcp__alpha__search"]
        native = registry._tools["read_file"]
        assert isinstance(bridged, Tool) and type(bridged) is type(native)
        assert set(vars(bridged)) == set(vars(native))
        spec = bridged.spec()
        assert spec.as_dict().keys() == native.spec().as_dict().keys()
        # The JSON Schema maps straight through: MCP defines `inputSchema` as the same JSON Schema
        # the engine already advertises, so any translation would be a place for the two to disagree.
        assert spec.parameters["properties"]["q"]["type"] == "string"
        assert spec.parameters["required"] == ["q"]
        assert "mcp__alpha__search" in registry.names()
    finally:
        bridge.close()


def test_a_read_only_run_refuses_a_remote_tool_that_writes(server_script, tmp_path):
    """The existing gate applies unchanged, which is what "same Tool shape" has to mean.

    An unannotated tool is treated as mutating — the spec's own default — so a server that simply
    did not say whether its tool writes cannot be used to change a workspace a run promised not to
    touch.
    """
    registry = ToolRegistry(workspace_root=tmp_path, agent=make_agent("*:*"), read_only=True)
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "alpha", tools=[
        _tool_json("writer"),                  # no annotations -> treated as mutating
        _tool_json("reader", read_only=True),  # explicitly safe
    ]))
    try:
        bridge.install(registry)
        assert not registry.call("mcp__alpha__writer", {"q": "x"}).ok
        assert "read-only" in registry.call("mcp__alpha__writer", {"q": "x"}).text
        assert registry.call("mcp__alpha__reader", {"q": "x"}).ok
    finally:
        bridge.close()


def test_a_capability_grant_still_applies_to_a_remote_tool(server_script, tmp_path):
    """A remote tool is an ordinary `Tool`, so the registry's own gate is the only gate."""
    registry = ToolRegistry(workspace_root=tmp_path, agent=make_agent("read:*"))
    bridge = McpToolBridge(timeout_s=10)
    bridge.add_server(stdio_spec(server_script, "alpha", tools=[_tool_json("search")]))
    try:
        bridge.install(registry)
        # An unknown name is refused by the registry itself, exactly as for a native tool.
        assert not registry.call("mcp__alpha__absent", {}).ok
        assert registry.call("mcp__alpha__search", {"q": "granted"}).ok
    finally:
        bridge.close()


def test_install_is_a_noop_when_mcp_is_disabled(tmp_path):
    registry = ToolRegistry(workspace_root=tmp_path, agent=make_agent("*:*"))
    before = registry.names()
    bridge = McpToolBridge.from_config(McpConfig(enabled=False))
    assert bridge.install(registry) == 0
    assert registry.names() == before
    bridge.close()


# ── config parsing ─────────────────────────────────────────────────────────


def test_a_stdio_entry_parses_into_a_spec():
    bridge = McpToolBridge.from_config(McpConfig(servers={"gh": {
        "command": "npx", "args": ["-y", "server-github"],
        "env": {"TOKEN": "t"}, "timeout_s": 12,
    }}), client_factory=lambda spec: McpClient(spec))
    spec = bridge.clients["gh"].spec
    assert spec.transport == "stdio" and spec.command == "npx"
    assert spec.args == ("-y", "server-github")
    assert spec.env == {"TOKEN": "t"} and spec.timeout_s == 12
    assert bridge.clients["gh"].timeout_s == 12


def test_an_http_entry_parses_into_a_spec():
    bridge = McpToolBridge.from_config(McpConfig(servers={"api": {
        "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer t"},
    }}), client_factory=lambda spec: McpClient(spec))
    spec = bridge.clients["api"].spec
    assert spec.transport == "http" and spec.url == "https://mcp.example.com/mcp"
    assert spec.headers == {"Authorization": "Bearer t"}


def test_a_description_never_leaks_a_configured_secret():
    """The run reports which servers it can reach; that report is not a place for a credential."""
    bridge = McpToolBridge.from_config(McpConfig(servers={"api": {
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer sk-abcdefghijklmnopqrstuvwxyz012345"},
    }}), client_factory=lambda spec: McpClient(spec))
    described = bridge.clients["api"].spec.describe()
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in described


# ── the http transport, framed by a local server ───────────────────────────
#
# No real network: a stdlib HTTP server on the loopback interface, speaking the same streamable-HTTP
# shape the official SDK produces — `text/event-stream` framing on a request, 202 with no body for a
# notification, and a session id handed back in a header. Its correctness was settled against a real
# SDK server before this module was written; what survives here is the regression check.


class _HttpFixture:
    """A minimal streamable-HTTP MCP endpoint, in process, on a loopback port."""

    def __init__(self, *, tools: list[dict], protocol_version: str = MCP_PROTOCOL_VERSION,
                 require_session: bool = False) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.tools = tools
        self.protocol_version = protocol_version
        self.require_session = require_session
        self.seen: list[dict] = []
        self.headers_seen: list[dict] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # silence the test output
                return

            def do_POST(self) -> None:  # noqa: N802 - the stdlib's spelling
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                fixture.headers_seen.append({k.lower(): v for k, v in self.headers.items()})
                message = json.loads(raw)
                fixture.seen.append(message)
                method = message.get("method")
                if "id" not in message:
                    # A notification: 202 with no body is what a real server answers.
                    self.send_response(202)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {"protocolVersion": fixture.protocol_version,
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "httpfixture", "version": "1.0"}}
                elif method == "tools/list":
                    result = {"tools": fixture.tools}
                elif method == "tools/call":
                    params = message.get("params") or {}
                    result = {"content": [{"type": "text",
                                           "text": f"http called {params.get('name')}"}]}
                else:
                    result = {}
                payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})
                body = f"event: message\ndata: {payload}\n\n".encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("mcp-session-id", "session-abc")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        import threading

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_the_http_transport_completes_the_handshake_and_a_call():
    """The streamable-HTTP shape: SSE framing parsed out, session id quoted back."""
    fixture = _HttpFixture(tools=[_tool_json("add")])
    try:
        client = McpClient(McpServerSpec(name="api", transport="http", url=fixture.url),
                           timeout_s=10)
        try:
            client.connect()
            assert client.protocol_version == MCP_PROTOCOL_VERSION
            assert client.list_tools()[0]["name"] == "add"
            assert client.call_tool("add", {"q": "x"}).text == "http called add"
            # The session id the server handed back must be quoted on every later request, or a
            # real server rejects them.
            assert fixture.headers_seen[-1].get("mcp-session-id") == "session-abc"
            assert fixture.headers_seen[-1].get("mcp-protocol-version") == MCP_PROTOCOL_VERSION
            # A notification must not be awaited: it is answered 202 with no body.
            assert any("id" not in m for m in fixture.seen)
        finally:
            client.close()
    finally:
        fixture.stop()


def test_an_http_server_refusing_credentials_explains_itself():
    """A remote service's 401 is an auth problem, and the refusal must say so rather than 'error'."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            body = b'{"error":"bad token"}'
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = McpClient(McpServerSpec(
            name="api", transport="http", url=f"http://127.0.0.1:{server.server_address[1]}/mcp",
            headers={"Authorization": "Bearer nope"}), timeout_s=5)
        with pytest.raises(McpError) as raised:
            client.connect()
        message = str(raised.value)
        assert "HTTP 401" in message and "credentials" in message
        assert client.failure and not client.connected
    finally:
        server.shutdown()
        server.server_close()


def test_an_unreachable_http_server_is_refused_quickly():
    """A port with nothing on it must not hold a node for the whole timeout."""
    client = McpClient(McpServerSpec(name="api", transport="http",
                                     url="http://127.0.0.1:1/mcp"), timeout_s=5)
    started = time.monotonic()
    with pytest.raises(McpError):
        client.connect()
    assert time.monotonic() - started < 5.0


def test_the_bridge_from_config_reaches_a_real_http_server():
    """End to end through the config shape a person actually writes."""
    fixture = _HttpFixture(tools=[_tool_json("add")])
    try:
        config = McpConfig(servers={"api": {"url": fixture.url}})
        bridge = McpToolBridge.from_config(config)
        try:
            assert bridge.discover()[0].name == "mcp__api__add"
            assert bridge.call("mcp__api__add", {"q": "y"}).text == "http called add"
        finally:
            bridge.close()
    finally:
        fixture.stop()


class _Recorder:
    """Stands in for `Diagnostics`, recording what a run would have logged."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, event: str, **kwargs: object) -> None:
        self.warnings.append(f"{event}: {kwargs.get('message', '')}")

    def info(self, event: str, **kwargs: object) -> None:
        return
