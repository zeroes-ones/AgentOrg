#!/usr/bin/env python3
"""Phase 2 RPC tests — the gateway-as-a-service seam.

The socket layer carries two guarantees the design depends on: secrets stay in the host
process, and a runner can tell a budget stop from a provider failure from a transport
failure. Both are tested here over a real Unix socket.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine.bus import EventBus
from engine.config import load
from engine.gateway import BudgetExceeded, Cost, Gateway
from engine.providers.base import (
    ChatRequest,
    ErrorKind,
    GatewayError,
    Message,
    Role,
    ToolCall,
    ToolSpec,
)
from engine.providers.fake import FakeProvider
from engine.rpc import (
    GatewayClient,
    GatewayServer,
    RpcError,
    frame,
    read_frame,
    request_to_wire,
    wire_to_request,
)
from engine.tokens import TokenEstimator


def _gateway(script: list | None = None, *, bus: EventBus | None = None) -> Gateway:
    fake = FakeProvider(script=script or [{"text": "hi", "prompt_tokens": 10, "completion_tokens": 5}])
    return Gateway(load(), {"fake": fake}, bus=bus, estimator=TokenEstimator())


@pytest.fixture
def server(tmp_path):
    """A bound server on a private directory, started on a daemon thread."""
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir(mode=0o700)
    instance = GatewayServer(_gateway(), socket_dir / "gw.sock")
    instance.bind()
    thread = instance.start_thread()
    yield instance
    instance.stop()
    thread.join(timeout=2)


# ── wire conversions ─────────────────────────────────────────────────────────


def test_request_round_trips_through_the_wire():
    original = ChatRequest(
        model="m", system="be terse",
        messages=[Message.text_message(Role.USER, "hello")],
        tools=[ToolSpec(name="check", description="d", parameters={"type": "object"})],
        temperature=0.1, max_tokens=128, json_mode=True, stop=["END"],
        extra={"keep_alive": "1m"},
    )
    wire = request_to_wire(original, provider_id="p", agent_id="a", node_id="n", session_id="s")
    rebuilt = wire_to_request(wire)
    assert rebuilt.model == "m"
    assert rebuilt.system == "be terse"
    assert rebuilt.messages[0].text == "hello"
    assert rebuilt.tools[0].name == "check"
    assert rebuilt.temperature == 0.1 and rebuilt.max_tokens == 128
    assert rebuilt.json_mode is True and rebuilt.stop == ["END"]
    assert rebuilt.extra == {"keep_alive": "1m"}


def test_wire_preserves_tool_calls():
    message = Message.text_message(Role.ASSISTANT, "calling")
    message.tool_calls.append(ToolCall(id="t1", name="check", arguments={"a": 1}))
    wire = request_to_wire(ChatRequest(model="m", messages=[message]),
                           provider_id=None, agent_id=None, node_id=None, session_id=None)
    rebuilt = wire_to_request(wire)
    assert rebuilt.messages[0].tool_calls[0].name == "check"
    assert rebuilt.messages[0].tool_calls[0].arguments == {"a": 1}


def test_wire_tolerates_a_foreign_role():
    rebuilt = wire_to_request({"model": "m", "messages": [{"role": "wizard", "text": "x"}]})
    assert rebuilt.messages[0].role is Role.USER


# ── framing ──────────────────────────────────────────────────────────────────


def test_frame_round_trip_over_a_socket_pair():
    import socket

    left, right = socket.socketpair()
    try:
        left.sendall(frame({"method": "health"}))
        assert read_frame(right) == {"method": "health"}
    finally:
        left.close()
        right.close()


def test_frame_rejects_an_oversized_payload():
    with pytest.raises(RpcError, match="over the"):
        frame({"blob": "x" * (33 << 20)})


def test_read_frame_reports_a_closed_connection():
    import socket

    left, right = socket.socketpair()
    left.close()
    try:
        with pytest.raises(RpcError, match="closed"):
            read_frame(right)
    finally:
        right.close()


def test_read_frame_rejects_non_object_json():
    import socket

    left, right = socket.socketpair()
    try:
        body = b"[1,2,3]"
        import struct

        left.sendall(struct.pack("!I", len(body)) + body)
        with pytest.raises(RpcError, match="JSON object"):
            read_frame(right)
    finally:
        left.close()
        right.close()


# ── end-to-end over a real socket ────────────────────────────────────────────


def test_rpc_complete_end_to_end(server, tmp_path):
    client = GatewayClient(server.socket_path)
    result = client.complete(provider_id="fake", model="m", system="s",
                             messages=[{"role": "user", "text": "go"}])
    assert result["text"] == "hi"
    assert result["usage"]["prompt_tokens"] == 10
    assert result["cost"]["known"] is True


def test_rpc_cost_is_charged_once_on_the_host(server):
    client = GatewayClient(server.socket_path)
    client.complete(provider_id="fake", model="m", messages=[{"role": "user", "text": "go"}])
    client.complete(provider_id="fake", model="m", messages=[{"role": "user", "text": "go"}])
    cost = client.cost()
    assert cost["ledger"]["calls"] == 2, "the host owns the ledger, not the runner"


def test_rpc_health_over_the_socket(server):
    health = GatewayClient(server.socket_path).health()
    assert "providers" in health
    assert "budget" in health


def test_rpc_surfaces_a_budget_stop_as_its_own_error(tmp_path):
    """A budget stop must not look like a provider failure — the runner parks, not retries."""
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir(mode=0o700)
    gateway = _gateway()
    gateway.run_max_usd = 0.0001
    gateway.ledger.charge(Cost(usd=1.0, source="estimated", model="m", provider_id="fake"))
    instance = GatewayServer(gateway, socket_dir / "gw.sock")
    instance.bind()
    thread = instance.start_thread()
    try:
        with pytest.raises(BudgetExceeded) as excinfo:
            GatewayClient(instance.socket_path).complete(provider_id="fake", model="m", messages=[])
        assert excinfo.value.spent_usd == pytest.approx(1.0)
    finally:
        instance.stop()
        thread.join(timeout=2)


def test_rpc_preserves_the_provider_error_kind(tmp_path):
    """Retry policy must survive the socket, or a runner cannot classify a 429."""
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir(mode=0o700)
    gateway = _gateway([{"error": GatewayError(ErrorKind.RATE_LIMIT, "slow down")}])
    instance = GatewayServer(gateway, socket_dir / "gw.sock")
    instance.bind()
    thread = instance.start_thread()
    try:
        with pytest.raises(GatewayError) as excinfo:
            GatewayClient(instance.socket_path).complete(provider_id="fake", model="m", messages=[])
        assert excinfo.value.kind is ErrorKind.RATE_LIMIT
        assert excinfo.value.retryable is True
    finally:
        instance.stop()
        thread.join(timeout=2)


def test_rpc_rejects_an_unknown_method(server):
    with pytest.raises(GatewayError, match="unknown method"):
        GatewayClient(server.socket_path).call("teleport")


def test_rpc_client_reports_a_missing_socket(tmp_path):
    client = GatewayClient(tmp_path / "nope.sock")
    with pytest.raises(RpcError, match="does not exist"):
        client.call("health")


def test_rpc_health_is_down_not_raising_when_the_socket_is_absent(tmp_path):
    health = GatewayClient(tmp_path / "nope.sock").health()
    assert health["status"] == "down"


def test_rpc_socket_is_private_and_refuses_a_world_writable_dir():
    """A world-writable socket directory would let any local process spend the budget.

    Uses an explicitly short path under /tmp: a long path is relocated to a private
    directory (covered separately), which correctly makes this check inapplicable.
    """
    import os
    import shutil
    import tempfile

    world = pathlib.Path(tempfile.mkdtemp(prefix="ao-"))  # short, so no relocation
    os.chmod(world, 0o777)
    try:
        server = GatewayServer(_gateway(), world / "gw.sock")
        with pytest.raises(RpcError, match="world-writable"):
            server.bind()
    finally:
        shutil.rmtree(world, ignore_errors=True)


def test_rpc_relocates_an_over_long_socket_path(tmp_path):
    """A deep workspace path must not fail: AF_UNIX paths are length-bounded."""
    from engine.rpc import socket_path_within_limit

    deep = tmp_path / ("nested/" * 12) / "gw.sock"
    relocated = socket_path_within_limit(deep)
    assert len(str(relocated).encode()) <= 100
    assert relocated != deep
    # The relocated directory must be private, or relocation would weaken permissions.
    assert relocated.parent.stat().st_mode & 0o077 == 0


def test_rpc_keeps_a_short_socket_path_unchanged(tmp_path):
    from engine.rpc import socket_path_within_limit

    short = pathlib.Path("/tmp/ao-short.sock")
    assert socket_path_within_limit(short) == short


def test_rpc_socket_file_is_owner_only(server):
    import os
    import stat

    mode = server.socket_path.stat().st_mode
    assert not (mode & stat.S_IRGRP), "the socket must not be group readable"
    assert not (mode & stat.S_IROTH), "the socket must not be world readable"


def test_rpc_replaces_a_stale_socket_file(tmp_path):
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir(mode=0o700)
    path = socket_dir / "gw.sock"
    path.write_text("stale")
    server = GatewayServer(_gateway(), path)
    server.bind()  # must not raise
    try:
        assert path.exists()
    finally:
        server.stop()


def test_rpc_server_stop_removes_the_socket(tmp_path):
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir(mode=0o700)
    server = GatewayServer(_gateway(), socket_dir / "gw.sock")
    server.bind()
    assert server.socket_path.exists()
    server.stop()
    assert not server.socket_path.exists()


def test_rpc_never_leaks_a_provider_secret_in_a_response(tmp_path):
    """The host holds the key; nothing it returns may contain key material."""
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir(mode=0o700)
    instance = GatewayServer(_gateway(), socket_dir / "gw.sock")
    instance.bind()
    thread = instance.start_thread()
    try:
        client = GatewayClient(instance.socket_path)
        result = client.complete(provider_id="fake", model="m", messages=[{"role": "user", "text": "go"}])
        assert "sk-" not in str(result)
        assert "sk-" not in str(client.health())
    finally:
        instance.stop()
        thread.join(timeout=2)
