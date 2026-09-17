#!/usr/bin/env python3
"""Phase 11 tests — the serve loop the native console drives.

The macOS app has always launched `engine.cli serve`. The command did not exist, so the console
could never start the engine it exists to watch. These tests protect the contract the app depends on,
which is narrow and worth stating exactly:

- one JSON command per line on stdin, one JSON event per line on stdout;
- **stdout carries the protocol and nothing else** — a stray print corrupts the stream the app parses;
- every command is acknowledged, correlated by `cmd_id`, *including* commands that fail;
- a malformed line is survived rather than fatal;
- closing stdin ends the server cleanly, after its queued commands have been answered.

Everything here drives the loop with in-memory streams, so it is fast, offline, and does not spawn a
process — except one test that deliberately does, because the app's path is a subprocess.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load
from engine.library import resolve
from engine.serve import Server
from engine.state import Workspace


class CapturedOut:
    """A stdout stand-in that records whole lines."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    def flush(self) -> None:  # noqa: D401 - the protocol requires a flush
        return None

    def events(self) -> list[dict]:
        out = []
        for line in self.lines:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
        return out


@pytest.fixture(scope="module")
def config():
    return load()


@pytest.fixture(scope="module")
def library():
    return resolve()


def drive(config, library, commands: list[dict | str], *, slug: str = "servetest"):
    """Run the loop over in-memory streams and return the events it emitted."""
    stdin = io.StringIO("\n".join(
        c if isinstance(c, str) else json.dumps(c) for c in commands) + "\n")
    out = CapturedOut()
    workspace = Workspace.for_project(slug, root=pathlib.Path(tempfile.mkdtemp()))
    workspace.ensure()
    server = Server(config=config, library=library, workspace=workspace, slug=slug,
                    stdin=stdin, stdout=out)
    code = server.serve_forever()
    return code, out.events()


def acks(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "command.ack"]


def ack_for(events: list[dict], cmd_id: str) -> dict | None:
    for event in acks(events):
        if event["payload"].get("cmd_id") == cmd_id:
            return event["payload"]
    return None


# ── the transport contract ───────────────────────────────────────────────────


def test_every_command_is_acknowledged(config, library):
    """An unacknowledged command is indistinguishable from a lost one, so the app would hang."""
    code, events = drive(config, library, [
        {"cmd_id": "c1", "type": "status"},
        {"cmd_id": "c2", "type": "org"},
        {"cmd_id": "c3", "type": "models"},
        {"cmd_id": "c4", "type": "pool"},
    ])
    assert code == 0
    for cmd_id in ("c1", "c2", "c3", "c4"):
        payload = ack_for(events, cmd_id)
        assert payload is not None, f"no ack for {cmd_id}"
        assert payload["ok"] is True


def test_a_failing_command_is_acknowledged_with_its_reason(config, library):
    """Silence would leave the app waiting on a reply that never comes."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "does-not-exist"}])
    payload = ack_for(events, "c1")
    assert payload is not None
    assert payload["ok"] is False
    assert "does-not-exist" in payload["error"]


def test_a_malformed_line_is_survived(config, library):
    """One bad frame must not cost the app its engine."""
    _, events = drive(config, library, ["{not json", {"cmd_id": "c2", "type": "status"}])
    assert ack_for(events, "c2") is not None, "the loop must carry on past a bad line"


def test_a_command_with_no_type_is_ignored_not_fatal(config, library):
    _, events = drive(config, library, [{"cmd_id": "c1"}, {"cmd_id": "c2", "type": "status"}])
    assert ack_for(events, "c2") is not None


def test_stdout_carries_only_parseable_json(config, library):
    """A stray print on stdout corrupts the protocol, so every line must parse."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "status"}])
    assert events, "at least one event"
    for event in events:
        assert isinstance(event, dict) and "type" in event


def test_every_event_carries_a_seq_and_timestamp(config, library):
    """The app keys its event list on `seq`, so a missing one collapses its identity."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "status"}])
    for event in events:
        assert isinstance(event.get("seq"), int)
        assert event.get("ts")


def test_closing_stdin_exits_cleanly(config, library):
    code, events = drive(config, library, [{"cmd_id": "c1", "type": "status"}])
    assert code == 0
    assert acks(events), "the queued command must be answered before the loop stops"


def test_a_slow_command_does_not_block_the_read_loop(config, library):
    """A run takes minutes; the read loop must stay free or /pause could not be honoured."""
    calls: list[str] = []

    class Probe(Server):
        def _cmd_status(self, payload):
            calls.append(payload.get("probe", ""))
            return {}

    stdin = io.StringIO(json.dumps({"cmd_id": "c1", "type": "status"}) + "\n")
    out = CapturedOut()
    workspace = Workspace.for_project("slowtest", root=pathlib.Path(tempfile.mkdtemp()))
    workspace.ensure()
    server = Probe(config=config, library=library, workspace=workspace, slug="slowtest",
                   stdin=stdin, stdout=out)
    assert server.serve_forever() == 0
    assert acks(out.events()), "the read loop must not starve the worker of acks"


# ── the shapes the panels read ───────────────────────────────────────────────


def test_status_carries_the_keys_the_ui_reads(config, library):
    """`refresh()` reads org, gate and outcome by name; a rename would blank the panels."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "status"}])
    detail = ack_for(events, "c1")["detail"]
    for key in ("org", "gate", "outcome", "running", "phase"):
        assert key in detail, f"status is missing {key!r}"


def test_models_lists_bindable_models_with_provenance(config, library):
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "models"}])
    detail = ack_for(events, "c1")["detail"]
    assert "models" in detail and "providers" in detail
    if detail["models"]:
        entry = detail["models"][0]
        assert "source" in entry, "a model must say how its window was determined"


def test_pool_reports_its_summary(config, library):
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "pool"}])
    detail = ack_for(events, "c1")["detail"]
    assert "summary" in detail and "tasks" in detail


def test_start_requires_a_goal(config, library):
    """A start with no goal must be refused with a reason, not silently plan nothing."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "start"}])
    payload = ack_for(events, "c1")
    assert payload["ok"] is False
    assert "goal" in payload["error"]


def test_a_run_command_with_no_run_loaded_says_so(config, library):
    """`approve` before any run must explain, not crash."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "approve"}])
    payload = ack_for(events, "c1")
    assert payload["ok"] is False
    assert "no run" in payload["error"]


def test_pause_and_abort_are_harmless_with_nothing_running(config, library):
    """The app posts these without awaiting, so they must never raise."""
    _, events = drive(config, library, [
        {"cmd_id": "c1", "type": "pause"},
        {"cmd_id": "c2", "type": "abort"},
    ])
    for cmd_id in ("c1", "c2"):
        payload = ack_for(events, cmd_id)
        assert payload is not None and payload["ok"] is True


# ── the app's real path: a subprocess ────────────────────────────────────────


def test_the_console_can_start_the_engine_as_a_subprocess(tmp_path):
    """The app's own path: spawn `engine.cli serve` and speak NDJSON to it.

    This is the test that would have caught the missing command — the app launches a subprocess, so a
    unit test of the loop alone would not prove the app can reach it.
    """
    import os

    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1")
    process = subprocess.Popen(
        [sys.executable, "-m", "engine.cli", "serve", "--slug", "subprocesstest",
         "--root", str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env, cwd=str(ROOT))
    try:
        process.stdin.write(json.dumps({"cmd_id": "c1", "type": "status"}) + "\n")
        process.stdin.flush()
        # Read the ack line directly, so the test fails fast rather than blocking on wait().
        import time
        deadline = time.time() + 60
        ack = None
        while time.time() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            event = json.loads(line)
            if event.get("type") == "command.ack":
                ack = event["payload"]
                break
        assert ack is not None, "the subprocess never acknowledged the command"
        assert ack["ok"] is True
        assert "phase" in ack["detail"]
    finally:
        try:
            process.stdin.close()
        except Exception:
            pass
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
    assert process.returncode == 0, process.stderr.read()[:400]


# ── the cache and swarm surfaces the console reads ───────────────────────────


def test_the_cache_command_reports_unreported_rather_than_zero(config, library):
    """A hit rate invented from silence would claim caching works when nobody has looked."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "cache"}])
    detail = ack_for(events, "c1")["detail"]
    assert detail["cache_reported"] is False
    assert detail["cache_hit_rate"] is None
    assert detail["cache_saving_usd"] is None


def test_status_carries_the_cache_so_the_cost_panel_gets_it_from_its_own_poll(config, library):
    """The panel already polls status; a second round trip is one the UI might never make."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "status"}])
    detail = ack_for(events, "c1")["detail"]
    assert "cache" in detail
    assert detail["cache"]["cache_reported"] is False


def test_cache_figures_accumulate_across_reported_calls():
    """The accumulation is what turns per-call cost facts into a run-level hit rate."""
    from engine.protocol import Event, EventType
    from engine.serve import Server

    server = Server(config=None, library=None, workspace=None)
    for hit, miss, saving in ((900, 100, 0.01), (800, 200, 0.008)):
        server._accumulate_cache(Event(
            seq=1, type=EventType.LLM_RESPONSE,
            payload={"cost": {"cache_hit_tokens": hit, "cache_miss_tokens": miss,
                              "cache_saving_usd": saving}}))
    summary = server._cmd_cache({})
    assert summary["cache_reported"] is True
    assert summary["cache_hit_tokens"] == 1700
    assert summary["cache_miss_tokens"] == 300
    assert summary["cache_hit_rate"] == pytest.approx(0.85)
    assert summary["cache_saving_usd"] == pytest.approx(0.018)


def test_an_unreported_call_does_not_turn_the_run_into_a_hit_rate():
    """A call that reported nothing must not drag the aggregate to a confident figure."""
    from engine.protocol import Event, EventType
    from engine.serve import Server

    server = Server(config=None, library=None, workspace=None)
    server._accumulate_cache(Event(seq=1, type=EventType.LLM_RESPONSE,
                                  payload={"cost": {"cache_hit_tokens": 10,
                                                    "cache_miss_tokens": 90}}))
    server._accumulate_cache(Event(seq=2, type=EventType.LLM_RESPONSE, payload={"cost": {}}))
    summary = server._cmd_cache({})
    assert summary["cache_hit_tokens"] == 10
    assert summary["cache_hit_rate"] == pytest.approx(0.1)


# ── the swarm surface ────────────────────────────────────────────────────────


def test_the_swarm_snapshot_is_pollable(config, library):
    """A UI that only listens can miss a transition and show a stale swarm forever."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "swarm"}])
    detail = ack_for(events, "c1")["detail"]
    assert detail["fanout"] is None
    assert detail["running"] is False


def test_status_carries_the_swarm_in_both_branches(config, library):
    """A field present only after a run starts renders as missing until then."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "status"}])
    detail = ack_for(events, "c1")["detail"]
    assert "swarm" in detail
    assert detail["swarm"]["running"] is False


def test_a_fanout_with_a_bad_template_is_refused_before_any_call(config, library):
    """Refused with the *same* words the executor uses, because both go through plan_fanout."""
    _, events = drive(config, library, [{
        "cmd_id": "c1", "type": "fanout",
        "payload": {"template": "Review the file", "items": ["a", "b"]}}])
    payload = ack_for(events, "c1")
    assert payload["ok"] is False
    assert "{{item}}" in payload["error"]


def test_a_fanout_with_no_goal_or_run_is_refused_clearly(config, library):
    _, events = drive(config, library, [{
        "cmd_id": "c1", "type": "fanout",
        "payload": {"template": "Review {{item}}", "items": ["a", "b"]}}])
    payload = ack_for(events, "c1")
    assert payload["ok"] is False
    assert "items" in payload["error"] or "run" in payload["error"]


# ── the goal, over the wire ──────────────────────────────────────────────────


def test_status_carries_the_goal_in_both_branches(config, library):
    """A field present only after a run starts renders as missing until then."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "status"}])
    detail = ack_for(events, "c1")["detail"]
    assert "goal" in detail
    assert detail["goal"]["state"] == "cleared"
    assert detail["goal"]["live"] is False


def test_status_carries_the_workspace_and_subagent_tree(config, library):
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "status"}])
    detail = ack_for(events, "c1")["detail"]
    assert "workspace" in detail and "attached" in detail["workspace"]
    assert "subagents" in detail and detail["subagents"]["count"] == 0


def test_goal_set_arms_and_reports_state(config, library):
    _, events = drive(config, library, [
        {"cmd_id": "c1", "type": "goal_set", "payload": {"objective": "Add pagination"}}])
    payload = ack_for(events, "c1")
    assert payload["ok"] is True
    assert payload["detail"]["goal"]["state"] == "armed"
    assert payload["detail"]["goal"]["live"] is True


def test_goal_set_needs_an_objective(config, library):
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "goal_set", "payload": {}}])
    assert ack_for(events, "c1")["ok"] is False


def test_goal_events_reach_the_console(config, library):
    """The console learns a goal exists from events, not only from the ack.

    A goal command creates its orchestrator lazily, so forwarding only on `start` meant `goal_set`
    produced events nobody was subscribed to — the panel would show a goal that never announced itself.
    """
    _, events = drive(config, library, [
        {"cmd_id": "c1", "type": "goal_set", "payload": {"objective": "Add pagination"}},
        {"cmd_id": "c2", "type": "goal_pause"},
        {"cmd_id": "c3", "type": "goal_resume"},
        {"cmd_id": "c4", "type": "goal_clear"}])
    types = {e.get("type") for e in events}
    assert "goal.armed" in types
    assert "goal.paused" in types
    assert "goal.resumed" in types
    assert "goal.cleared" in types


def test_a_goal_command_without_a_goal_is_refused_clearly(config, library):
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "goal_pause"}])
    payload = ack_for(events, "c1")
    assert payload["ok"] is False
    assert "no goal" in payload["error"]


def test_subagents_command_answers_with_an_empty_tree(config, library):
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "subagents"}])
    payload = ack_for(events, "c1")
    assert payload["ok"] is True
    assert payload["detail"]["count"] == 0


def test_subagent_result_needs_a_child_id(config, library):
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "subagent_result", "payload": {}}])
    payload = ack_for(events, "c1")
    assert payload["ok"] is False
    assert "child_id" in payload["error"]


# ── providers, over the wire ─────────────────────────────────────────────────


def _server_with_creds(tmp_path, slug="prov"):
    """A server whose config file is writable, so provider commands can be exercised."""
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({
        "version": "1.0.0",
        "providers": {"ollama": {"kind": "ollama", "base_url": "http://localhost:11434"}},
        "models": {"known": {"qwen2.5-coder:7b": {"context_window": 32768}}},
        "defaults": {"provider": "ollama", "model": "qwen2.5-coder:7b"},
    }))
    os.chmod(creds, 0o600)
    workspace = Workspace.for_project(slug, root=tmp_path)
    workspace.ensure()
    return Server(config=load(str(creds), warn=False), library=resolve(), workspace=workspace,
                  slug=slug, stdin=io.StringIO(""), stdout=io.StringIO()), creds


def test_the_provider_list_never_returns_a_key(tmp_path):
    """A secret that crosses the socket is a secret in a log, a screenshot and a crash report."""
    server, creds = _server_with_creds(tmp_path)
    payload = server._cmd_providers({})
    blob = json.dumps(payload)
    assert "api_key_value_should_not_appear" not in blob
    entry = payload["providers"][0]
    assert "api_key" not in entry
    assert "has_key" in entry
    # Header *names* are reported; values are not.
    assert isinstance(entry["headers"], list)


def test_the_provider_list_names_the_config_file(tmp_path):
    server, creds = _server_with_creds(tmp_path)
    assert server._cmd_providers({})["config_path"] == str(creds)


def test_provider_add_saves_and_reloads_in_place(tmp_path):
    server, creds = _server_with_creds(tmp_path)
    server._cmd_provider_add({"provider_id": "groq", "kind": "openai",
                              "base_url": "https://api.groq.com/openai/v1",
                              "api_key": "gsk_" + "x" * 20})
    document = json.loads(creds.read_text())
    assert "groq" in document["providers"]
    assert "ollama" in document["providers"], "the existing provider must survive"
    assert document["models"]["known"]["qwen2.5-coder:7b"]["context_window"] == 32768
    # The live config must see the change, or the next `models`/`start` uses the old provider set.
    assert "groq" in server.config.providers


def test_provider_add_rejects_an_unknown_kind(tmp_path):
    server, creds = _server_with_creds(tmp_path)
    with pytest.raises(Exception):
        server._cmd_provider_add({"provider_id": "x", "kind": "banana", "base_url": "https://x/v1"})


def test_provider_add_requires_a_base_url(tmp_path):
    server, creds = _server_with_creds(tmp_path)
    with pytest.raises(Exception):
        server._cmd_provider_add({"provider_id": "x", "kind": "openai"})


def test_provider_remove_deletes_only_the_named_one(tmp_path):
    server, creds = _server_with_creds(tmp_path)
    server._cmd_provider_add({"provider_id": "groq", "kind": "openai",
                              "base_url": "https://api.groq.com/openai/v1"})
    server._cmd_provider_remove({"provider_id": "groq"})
    document = json.loads(creds.read_text())
    assert "groq" not in document["providers"]
    assert "ollama" in document["providers"]


def test_provider_test_reports_an_unreachable_endpoint_without_raising(tmp_path):
    """A failed test is the *answer* to "test this", not a server error."""
    server, creds = _server_with_creds(tmp_path)
    result = server._cmd_provider_test({"provider_id": "nope", "kind": "openai",
                                        "base_url": "http://127.0.0.1:9/v1", "api_key": "x" * 20})
    assert result["ok"] is False
    assert result["reachable"] is False


def test_provider_test_refuses_a_bad_configuration(tmp_path):
    server, creds = _server_with_creds(tmp_path)
    # A cloud endpoint with no key cannot even be built, and that is reported rather than raised.
    result = server._cmd_provider_test({"provider_id": "c", "kind": "openai",
                                        "base_url": "https://api.openai.com/v1"})
    assert result["ok"] is False
    assert "key" in result["reason"].lower()


def test_provider_test_does_not_write_anything(tmp_path):
    """Testing the values in the form must not half-write a configuration."""
    server, creds = _server_with_creds(tmp_path)
    before = creds.read_text()
    server._cmd_provider_test({"provider_id": "groq", "kind": "openai",
                               "base_url": "http://127.0.0.1:9/v1"})
    assert creds.read_text() == before


# ── the shared discovery catalog ─────────────────────────────────────────────
#
# Found by timing the suite: `_cmd_models`/`_cmd_providers` built a NEW `ModelCatalog` on every call,
# so its 900s TTL cache was thrown away each time. The console polls on a timer, so every poll re-probed
# every provider — and a provider that is down spends its full retry budget with backoff, which made
# the provider test file take 632s and would have made the panel appear to hang.


def test_the_discovery_catalog_is_reused_across_calls(tmp_path):
    """The TTL cache only works if the object survives between polls."""
    server, creds = _server_with_creds(tmp_path)
    assert server._catalog() is server._catalog()


def test_a_config_reload_rebuilds_the_discovery_catalog(tmp_path):
    """A provider edit must not be hidden behind a 15-minute TTL: the cache is dropped deliberately."""
    server, creds = _server_with_creds(tmp_path)
    before = server._catalog()
    server._reload_config()
    assert server._catalog() is not before


def test_the_provider_list_does_not_report_unknown_after_a_probe(tmp_path):
    """`status()` reads the cache; it was being read before the cache was populated, so every
    provider showed as 'unknown' even right after a successful probe."""
    server, creds = _server_with_creds(tmp_path)
    entries = server._cmd_providers({})["providers"]
    assert entries, "no providers reported"
    assert all(e["status"] != "unknown" for e in entries), \
        [e["status"] for e in entries]


def test_a_failed_probe_is_labelled_once(tmp_path):
    """`configured+configured` was the old label: a doubled suffix nobody reads as anything."""
    server, creds = _server_with_creds(tmp_path)
    for entry in server._cmd_providers({})["providers"]:
        assert "+configured+configured" not in entry["status"]
        assert entry["status"] != "configured+configured"


# ── the parent watchdog ──────────────────────────────────────────────────────
#
# Found by launching the real .app and killing it: SIGTERM left the engine running indefinitely, holding
# the project while the next app instance started a second engine on it. The EOF path covers the normal
# case, but a parent that dies while *any* inherited descriptor still holds the stdin write end leaves
# the pipe open, so EOF never arrives and the engine outlives its app.


def test_the_engine_stops_when_its_parent_dies(config, library):
    """The orphan case, reproduced: the pipe is held open by a grandchild, so only the watchdog can fire."""
    import subprocess, sys as _sys, time

    repo = pathlib.Path(__file__).resolve().parent.parent
    engine = "/opt/homebrew/bin/python3"
    if not pathlib.Path(engine).exists():
        pytest.skip("no interpreter to run a subprocess with")

    project = pathlib.Path(tempfile.mkdtemp()) / "orphan"
    project.mkdir(parents=True)
    pid_file = pathlib.Path(tempfile.mkdtemp()) / "child.pid"

    # An "app" that spawns the engine and passes the stdin write end to a detached grandchild, then
    # dies. The grandchild is what keeps the pipe open after the parent is gone.
    # The app passes its own pid, exactly as AgentProcessService does. The engine watches *that*
    # rather than `getppid()`, because the latter races: an app that dies during the engine's import
    # leaves `getppid()` already 1, so there is nothing to detect.
    app_source = f'''
import os, subprocess, sys, time
env = dict(os.environ)
env["AGENTORG_PARENT_PID"] = str(os.getpid())
p = subprocess.Popen(
    ["{engine}", "-m", "engine.cli", "serve", "--project", sys.argv[1]],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
open(sys.argv[2], "w").write(str(p.pid))
# A detached grandchild inherits the stdin write end, so EOF never arrives when we die.
subprocess.Popen(["/bin/sleep", "60"], pass_fds=(p.stdin.fileno(),))
time.sleep(60)
'''
    app_file = project / "fake_app.py"
    app_file.write_text(app_source)

    env = dict(os.environ)
    env["AGENTORG_CREDENTIALS"] = str(pathlib.Path(repo / "credentials.json"))
    env["AGENTORG_HOME"] = tempfile.mkdtemp()
    app = subprocess.Popen([engine, str(app_file), str(project), str(pid_file)],
                           cwd=str(repo), env=env)
    try:
        # Wait for the app to record the engine's pid.
        deadline = time.time() + 20
        while not pid_file.is_file() and time.time() < deadline:
            time.sleep(0.2)
        if not pid_file.is_file():
            pytest.skip("the engine did not start in time")
        child = int(pid_file.read_text())

        def alive() -> bool:
            return subprocess.run(["kill", "-0", str(child)],
                                  capture_output=True).returncode == 0

        assert alive(), "the engine should be running while its app is"

        # Kill the app hard: no quit handler runs, and the pipe stays open.
        app.kill()

        # The watchdog polls every 2s, so allow generous slack for a loaded machine.
        stopped = False
        for _ in range(20):
            time.sleep(0.5)
            if not alive():
                stopped = True
                break
        assert stopped, ("the engine outlived its app — an orphan holding the project, which is the "
                         "failure the checkpoint exists to prevent")
    finally:
        app.kill()
        if pid_file.is_file():
            subprocess.run(["kill", "-9", pid_file.read_text().strip()], capture_output=True)


# ── a slow command's acknowledgement must not be lost on shutdown ────────────


def test_a_slow_command_is_acknowledged_before_the_server_exits(config, library):
    """Found by running the suite after the app added a live provider.

    The worker takes a command **off** the queue and then runs it, so `_drain` — which waited for the
    queue to be empty — returned immediately while the command was still in flight. `_shutdown` then
    killed the worker mid-command, and the ack never went out: the command had *succeeded* and the app
    was left waiting for a reply that never came. The worst shape of bug in this file, because the work
    is done and only the answer is lost.
    """
    import time as _time

    calls: list[str] = []

    class Slow(models_handlers := object):
        pass

    def _slow_handle(command):
        calls.append(command.type_value)
        # Longer than `_shutdown`'s 2s join, which is the whole point: with a wait that only looks at
        # the queue, this command is still running when the join gives up and the ack is lost. A 1s
        # command would have slipped through and hidden the bug — this test failed to catch it until
        # the sleep was long enough, which is worth remembering.
        _time.sleep(2.5)
        return {"slow": True}

    from engine.serve import Server

    server = Server.__new__(Server)
    from engine.bus import EventBus  # noqa: F401
    import queue as _queue
    import threading as _threading
    import io as _io

    stdin = _io.StringIO(json.dumps({"cmd_id": "slow1", "type": "slow"}) + "\n")
    out = CapturedOut()
    server.__init__(config=config, library=library,
                    workspace=Workspace.for_project("slowack", root=pathlib.Path(tempfile.mkdtemp())),
                    slug="slowack", stdin=stdin, stdout=out)
    server.workspace.ensure()
    server.handle = _slow_handle          # type: ignore[method-assign]

    code = server.serve_forever()
    assert code == 0
    acks = [e for e in out.events() if e.get("type") == "command.ack"]
    assert acks, "the slow command was executed but its acknowledgement never reached the client"
    assert acks[0]["payload"].get("ok") is True
    assert calls == ["slow"], "the command should have run exactly once"


# ── the picker data a hire form needs ────────────────────────────────────────
#
# Found by using the app: the People panel loaded only the roster, so its provider and model pickers
# were empty and hiring looked impossible — while the engine could serve it perfectly. The panel was
# fixed; these tests pin the *engine* half, which is what the panel depends on.


def test_providers_lists_a_provider_with_its_own_models(tmp_path):
    """A provider that reports models must report them here, or the picker has nothing to offer."""
    server, creds = _server_with_creds(tmp_path)
    payload = server._cmd_providers({})
    entry = next(p for p in payload["providers"] if p["id"] == "ollama")
    # Ollama is local and may be absent in CI, so assert the *shape* the picker reads rather than a
    # count: the panel filters `models` by `provider_id` and needs both keys present.
    assert "models" in entry and isinstance(entry["models"], list)
    assert "has_key" in entry and "status" in entry


def test_models_are_scoped_by_provider_id(tmp_path):
    """The panel's model picker filters on `provider_id`, so that key must be the id it selects with."""
    server, creds = _server_with_creds(tmp_path)
    detail = server._cmd_models({})
    for entry in detail["models"]:
        assert entry["provider_id"], "a model without a provider_id cannot be matched to a provider"
        assert entry["model_id"], "a model without an id cannot be chosen"


def test_a_hire_with_an_explicit_provider_and_model_is_accepted(tmp_path):
    """The path the picker drives — and the one that was unreachable when the pickers were empty."""
    server, creds = _server_with_creds(tmp_path)
    # A model the curated table declares a window for, since a probed one needs a live endpoint.
    result = server._cmd_hire({"name": "Picked", "skill": "code-reviewer",
                               "provider": "ollama", "model": "qwen2.5-coder:7b"})
    assert result["agent"]["provider"] == "ollama"
    assert result["agent"]["model"] == "qwen2.5-coder:7b"
    assert result["agent"]["context_window"] == 32768


def test_a_provider_add_is_visible_to_the_next_providers_and_models_read(tmp_path):
    """The sequence the panel performs: save, then reload both lists.

    The bug this guards is subtle and was real: adding a provider emitted an event that suppressed the
    model refresh, so the panel never populated after a change it had just made. The engine half is
    that the reload must *report* the new provider immediately.
    """
    server, creds = _server_with_creds(tmp_path)
    server._cmd_provider_add({"provider_id": "fresh", "kind": "openai",
                              "base_url": "https://example.invalid/v1", "api_key": "k" * 20})
    providers = server._cmd_providers({})
    assert "fresh" in [p["id"] for p in providers["providers"]]
    models = server._cmd_models({})
    assert "fresh" in (models.get("providers") or {}), \
        "the reloaded catalog must know about the provider it was just told about"


def test_the_status_snapshot_carries_what_every_panel_needs(tmp_path):
    """One poll feeds every panel, so a missing key is a panel that renders blank forever."""
    server, creds = _server_with_creds(tmp_path)
    detail = server._cmd_status({})
    for key in ("org", "goal", "subagents", "proposals", "workspace", "cache", "swarm"):
        assert key in detail, f"the snapshot omits {key}, so that panel can never populate"
