#!/usr/bin/env python3
"""Phase 45 tests — the console's *action* surface for machine access.

The app could already read what each system capability means (`_cmd_system`, phase 40). What it could
not do was anything about it: no way to grant a capability, answer a consent request, or run a
capability to see what it does. A panel that describes twelve capabilities and offers no way to use
one is a manual, not a control surface.

Three commands close that, and the tests here hold the properties that make them safe rather than
merely present:

  - `system_set` writes the switches (phase 40 covers the read; this covers the write round trip).
  - `system_consent` records an approval in the run's ledger, and refuses one an agent could give
    itself.
  - `system_invoke` runs a capability through `ToolRegistry.call` — **the same gate an agent's tool
    call passes** — so the panel cannot become a second, unrecorded route to the machine. That shared
    path is the property worth testing, and the first test drives on the seam that proves it.

Every workspace here is a temp directory and every config is a copy, because these commands write to
disk and to a ledger; a test that wrote to the real `credentials.json` would be editing the reader's
own machine access.
"""

from __future__ import annotations

import io
import json
import pathlib
import shutil
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load
from engine.library import resolve
from engine.serve import Server
from engine.state import Workspace


class Captured:
    """A stdout stand-in that records whole lines, as `serve` writes them."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    def flush(self) -> None:
        pass

    def acks(self) -> list[dict]:
        return [json.loads(line) for line in self.lines
                if line.strip() and json.loads(line).get("type") == "command.ack"]


@pytest.fixture
def workspace():
    path = pathlib.Path(tempfile.mkdtemp())
    ws = Workspace.for_project("sysact", root=path)
    ws.ensure()
    return ws


@pytest.fixture
def config():
    """A copy of the real credentials file, so a write cannot touch the reader's own access."""
    source = ROOT / "credentials.json"
    if not source.is_file():
        pytest.skip("no credentials.json to copy; this suite writes a config")
    target = pathlib.Path(tempfile.mkdtemp()) / "credentials.json"
    shutil.copy(source, target)
    loaded = load(target, warn=False)
    loaded.path = target
    return loaded


def drive(config, workspace, commands: list[dict], *, slug: str = "sysact") -> list[dict]:
    """Run the serve loop over in-memory streams and return the command acknowledgements."""
    stdin = io.StringIO("\n".join(json.dumps(c) for c in commands) + "\n")
    out = Captured()
    Server(config=config, library=resolve(), workspace=workspace, slug=slug,
           stdin=stdin, stdout=out).serve_forever()
    return out.acks()


def detail_of(acks: list[dict], cmd_id: str) -> dict:
    for ack in acks:
        payload = ack.get("payload") or {}
        if payload.get("cmd_id") == cmd_id:
            return payload.get("detail") or {}
    raise AssertionError(f"no ack for {cmd_id!r} in {[a.get('payload', {}).get('cmd_id') for a in acks]}")


def outcome_of(acks: list[dict], cmd_id: str) -> dict:
    for ack in acks:
        payload = ack.get("payload") or {}
        if payload.get("cmd_id") == cmd_id:
            return payload
    raise AssertionError(f"no ack for {cmd_id!r}")


# ── the switches ─────────────────────────────────────────────────────────────


def test_a_switch_change_is_written_and_reported_back(config, workspace):
    """The write half of phase 40: the panel sends a switch, the engine persists it and says so."""
    acks = drive(config, workspace, [
        {"cmd_id": "off", "type": "system_set", "payload": {"enabled": False}},
        {"cmd_id": "read", "type": "system", "payload": {}},
    ])
    written = detail_of(acks, "off")
    assert written["enabled"] is False
    # Read back through the *other* command, so this cannot pass by echoing the request.
    assert detail_of(acks, "read")["enabled"] is False
    on_disk = json.loads((config.path).read_text())[ "system"]
    assert on_disk["enabled"] is False


def test_only_the_keys_sent_are_changed(config, workspace):
    """An absent key means "leave this alone", which is what stops one panel reverting another.

    If `system_set` rewrote the whole section from the payload, a panel that only knew about
    `enabled` would silently clear the allowlists every time someone flipped that switch — and the
    symptom would be an `open` that stopped working for no visible reason.
    """
    acks = drive(config, workspace, [
        {"cmd_id": "apps", "type": "system_set",
         "payload": {"allow_apps": ["Safari", "Notes"]}},
        {"cmd_id": "switch", "type": "system_set", "payload": {"enabled": True}},
    ])
    assert detail_of(acks, "apps")["allow_apps"] == ["Safari", "Notes"]
    # Flipping `enabled` afterwards must not have disturbed the list.
    after = detail_of(acks, "switch")
    assert after["allow_apps"] == ["Safari", "Notes"], (
        "a later switch change rewrote the allowlist, which means an absent key was not left alone")


def test_an_empty_change_is_refused_rather_than_writing_nothing(config, workspace):
    acks = drive(config, workspace, [{"cmd_id": "noop", "type": "system_set", "payload": {}}])
    payload = outcome_of(acks, "noop")
    assert payload.get("ok") is False
    assert "at least one setting" in str(payload.get("error") or "")


def test_an_allowlist_that_is_not_a_list_is_refused(config, workspace):
    """A string where a list belongs is the shape a form sends by mistake, and it must not be stored."""
    acks = drive(config, workspace, [
        {"cmd_id": "bad", "type": "system_set", "payload": {"allow_apps": "Safari"}}])
    payload = outcome_of(acks, "bad")
    assert payload.get("ok") is False
    assert "list" in str(payload.get("error") or "")


# ── consent ──────────────────────────────────────────────────────────────────


def test_a_consent_grant_reaches_the_ledger(config, workspace):
    """The approval is a ledger entry, not a flag in memory — that is what makes it reviewable."""
    from engine.sysctl_tools import consent_gate

    acks = drive(config, workspace, [
        {"cmd_id": "grant", "type": "system_consent",
         "payload": {"tool": "set_volume", "agent_id": "ag_test", "approved": True,
                     "note": "panel test"}},
    ])
    assert detail_of(acks, "grant")["approved"] is True
    ledger = workspace.state_dir / "ledger.jsonl"
    assert ledger.is_file(), "no ledger was written, so the approval exists nowhere but this reply"
    text = ledger.read_text()
    assert consent_gate("set_volume", "ag_test") in text
    assert "panel test" in text, "the note was dropped, so the approval has no reason attached"


def test_an_agent_cannot_approve_its_own_destructive_call(config, workspace):
    """The module's own guard, reached through the console.

    `grant_consent` refuses a `by` naming an agent. This asserts the console did **not** route around
    it by substituting a person: the refusal must come back to the caller.
    """
    server = Server(config=config, library=resolve(), workspace=workspace, slug="x",
                    stdin=io.StringIO(""), stdout=Captured())
    server._principal_id = lambda: "ag_agent"  # type: ignore[method-assign]
    with pytest.raises(Exception) as excinfo:
        server._cmd_system_consent({"tool": "set_volume", "agent_id": "ag_other",
                                    "approved": True})
    assert "agent" in str(excinfo.value).lower()


def test_consent_needs_a_holder(config, workspace):
    """A grant with no holder would read as a grant to everyone, so it is refused."""
    acks = drive(config, workspace, [
        {"cmd_id": "noholder", "type": "system_consent", "payload": {"tool": "set_volume"}}])
    payload = outcome_of(acks, "noholder")
    assert payload.get("ok") is False
    assert "agent_id" in str(payload.get("error") or "")


def test_a_tool_that_needs_no_approval_is_refused(config, workspace):
    """Only the ten state-changing tools are in `CONSENT_REQUIRED`; a read is not a decision."""
    acks = drive(config, workspace, [
        {"cmd_id": "read", "type": "system_consent",
         "payload": {"tool": "system_state", "agent_id": "ag_test", "approved": True}}])
    payload = outcome_of(acks, "read")
    assert payload.get("ok") is False
    assert "system_state" in str(payload.get("error") or "")


# ── invocation ───────────────────────────────────────────────────────────────


def test_invocation_goes_through_the_registry_gate_not_the_tool_object(config, workspace, monkeypatch):
    """**The property that matters.** The panel must not become a second route to the machine.

    `ToolRegistry.call` is where the capability gate lives, so an invocation that reached
    `SystemTools` directly would bypass the one check deciding whether this holder may act — and it
    would do so silently, because the call would also succeed. This substitutes the registry class and
    proves the command used it. The companion assertion (that the probe is live) matters as much: an
    assertion that cannot fail would pass even if the substitution never took effect.
    """
    from engine import tools as tools_module

    calls: list[tuple[str, dict]] = []
    real = tools_module.ToolRegistry

    class Probe(real):
        def call(self, name, arguments=None):
            calls.append((name, dict(arguments or {})))
            return super().call(name, arguments)

    monkeypatch.setattr(tools_module, "ToolRegistry", Probe)
    acks = drive(config, workspace, [
        {"cmd_id": "state", "type": "system_invoke", "payload": {"tool": "system_state"}}])
    payload = outcome_of(acks, "state")

    if payload.get("ok") is False and "not offered" in str(payload.get("error") or ""):
        # `system.enabled` is off in this config, so nothing is advertised to any holder. That is the
        # gate doing its job, and it is still evidence the registry was consulted — but it does not
        # exercise `call`, so the seam assertion below would be vacuous. Say so rather than pass.
        pytest.skip("machine access is off in this config, so no tool is offered to invoke")
    assert calls, "the command did not go through ToolRegistry.call — the gate was bypassed"
    assert calls[0][0] == "system_state"


def test_an_unknown_tool_is_refused_by_name_rather_than_silently_absent(config, workspace):
    acks = drive(config, workspace, [
        {"cmd_id": "bogus", "type": "system_invoke", "payload": {"tool": "definitely_not_a_tool"}}])
    payload = outcome_of(acks, "bogus")
    assert payload.get("ok") is False
    message = str(payload.get("error") or "")
    assert "definitely_not_a_tool" in message, "the refusal must name the tool that was asked for"
    # The two causes need different fixes, so both are named.
    assert "system.enabled" in message


def test_invocation_needs_a_tool(config, workspace):
    acks = drive(config, workspace, [{"cmd_id": "none", "type": "system_invoke", "payload": {}}])
    payload = outcome_of(acks, "none")
    assert payload.get("ok") is False
    assert "tool" in str(payload.get("error") or "")


def test_a_named_holder_that_is_not_in_the_roster_is_refused(config, workspace):
    """Acting as somebody is only meaningful if they exist; a silent fallback would be worse.

    Falling back to the person's own `system:*` grants would make a typo act with *more* authority
    than the agent someone meant to test.
    """
    acks = drive(config, workspace, [
        {"cmd_id": "ghost", "type": "system_invoke",
         "payload": {"tool": "system_state", "agent_id": "no-such-agent"}}])
    payload = outcome_of(acks, "ghost")
    assert payload.get("ok") is False
    assert "no-such-agent" in str(payload.get("error") or "")


# ── the commands are named the way the dispatcher expects ────────────────────


def test_every_new_command_is_reachable_by_the_name_the_console_sends(config, workspace):
    """`serve.handle` resolves `_cmd_<type>` by name, so a protocol constant with no method is a
    command the app can send and the engine cannot answer — and the app would hang waiting."""
    from engine.protocol import CommandType

    server = Server(config=config, library=resolve(), workspace=workspace, slug="x",
                    stdin=io.StringIO(""), stdout=Captured())
    for member in (CommandType.SYSTEM, CommandType.SYSTEM_SET,
                   CommandType.SYSTEM_CONSENT, CommandType.SYSTEM_INVOKE):
        # The enum's *value* is what crosses the wire and what `serve.handle` interpolates into the
        # method name, so the value is what must resolve — asserting on `str(member)` would check
        # `CommandType.SYSTEM` while the dispatcher looks up `system`.
        assert hasattr(server, f"_cmd_{member.value}"), (
            f"{member.value} has no handler on the server")
