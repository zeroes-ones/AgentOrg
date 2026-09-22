#!/usr/bin/env python3
"""Phase 11 tests — the serve loop the native console drives.

The macOS app has always launched `engine.cli serve`. The command did not exist, so the console
could never start the engine it exists to watch. These tests protect the contract the app depends on,
which is narrow and worth stating exactly:

- one JSON command per line on stdin, one JSON event per line on stdout;
- **stdout carries the protocol and nothing else** — a stray print corrupts the stream the app parses;
- every command is acknowledged, correlated by `cmd_id`, *including* commands that fail;
- **a run belongs to its own thread** — the command worker stays free, so `status` and `pause`/`abort`
  are answered while a graph is in flight rather than queued behind the run they act on;
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
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load
from engine.library import resolve
from engine.serve import Server
from engine.state import Workspace


class CapturedOut:
    """A stdout stand-in that records whole lines.

    Optionally records an interleaved *timeline* of acks and events, and calls back on every ack: the
    order of arrival is what distinguishes "answered while the graph was in flight" from "answered once
    it had ended", and a list of lines alone cannot say that.
    """

    def __init__(self, timeline: list[str] | None = None, on_ack=None) -> None:
        self.lines: list[str] = []
        self.timeline = timeline
        self.on_ack = on_ack

    def write(self, text: str) -> None:
        self.lines.append(text)
        if self.timeline is None and self.on_ack is None:
            return
        try:
            event = json.loads(text.strip() or "{}")
        except json.JSONDecodeError:
            return
        payload = event.get("payload") or {}
        if self.timeline is not None:
            self.timeline.append(f"ack:{payload.get('cmd_id')}" if event.get("type") == "command.ack"
                                 else f"event:{event.get('type')}")
        if self.on_ack is not None and event.get("type") == "command.ack":
            self.on_ack(payload)

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


class QueuedStdin:
    """A stdin the test feeds one line at a time.

    `drive` below hands the loop every command at once, which cannot express "a command sent *while* a
    run is in flight" — the whole point of these tests. Iteration blocks on the queue, so a command's
    place in time is the test's decision, and the loop is still the real one.
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue[str | None]" = queue.Queue()

    def send(self, command: dict) -> None:
        self._queue.put(json.dumps(command) + "\n")

    def close(self) -> None:
        self._queue.put(None)

    def __iter__(self):
        while True:
            line = self._queue.get()
            if line is None:
                return
            yield line


@pytest.fixture(scope="module")
def config():
    return load()


@pytest.fixture(scope="module")
def library():
    return resolve()


def drive(config, library, commands: list[dict | str], *, slug: str = "servetest", out=None):
    """Run the loop over in-memory streams and return the events it emitted."""
    stdin = io.StringIO("\n".join(
        c if isinstance(c, str) else json.dumps(c) for c in commands) + "\n")
    out = out if out is not None else CapturedOut()
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


# ── a run belongs to its own thread, not to the command worker ────────────────
#
# The defect these protect, in one line: a run executing on the command worker queued the app's
# `status` poll and its `pause`/`abort` behind the very run they describe, so the panels froze and Pause
# did nothing until the graph had already ended. The graph here is stubbed to stay in flight until the
# pause *acknowledgement* is on the wire, which makes the ordering a property of the code rather than of
# the machine's timing — and means the old topology fails this deterministically in ~5s, not flakily.


class _RecordingHost:
    """The slice of `RunnerHost` a stubbed run needs, with pause and abort observable."""

    def __init__(self) -> None:
        self.running = True
        self.aborted = False
        self.paused = False

    def pause(self) -> bool:
        self.paused = True
        return True

    def resume(self) -> bool:
        self.paused = False
        return True

    def abort(self) -> bool:
        self.aborted = True
        return True

    def wedged(self):
        return None

    def with_contract_rework(self, attempts):
        return None


class _Outcome:
    """Stands in for `RunOutcome`: the three attributes `_run_graph` reads off it."""

    def __init__(self, aborted: bool) -> None:
        self.summary = {"outcome": "aborted by the Owner" if aborted else "done"}
        self.state = type("_State", (), {"value": "aborted" if aborted else "done"})()
        self.gated = False


def _in_flight_execute(*, release: threading.Event, timeline: list[str],
                       seconds: float = 5.0, fail: Exception | None = None):
    """An `Orchestrator.execute` that stays in flight until `release` is set.

    Only the model calls are replaced — the run thread, the ack and the event stream are the real ones.
    `release` is set by the test when the pause ack is on the wire, so a graph can never settle before
    the ack it is compared against. The bounded wait means a broken implementation fails the assertion
    instead of hanging the suite.
    """

    def execute(self, run=None, **_kwargs):
        from engine.orchestrator import RunPhase

        run = self._resolve(run)
        if fail is not None:
            raise fail
        host = _RecordingHost()
        with self._lock:
            self._host = host
        run.phase = RunPhase.RUNNING
        self._persist(run)
        timeline.append("run-live")
        try:
            release.wait(seconds)
        finally:
            with self._lock:
                self._host = None
        run.phase = RunPhase.ABORTED if host.aborted else RunPhase.DONE
        run.touch()
        self._persist(run)
        timeline.append("run-finished")
        return _Outcome(host.aborted)

    return execute


def _live_server(config, library, *, slug: str, out):
    """A server whose read loop runs in a thread, so a test can send commands as time passes."""
    stdin = QueuedStdin()
    workspace = Workspace.for_project(slug, root=pathlib.Path(tempfile.mkdtemp()))
    workspace.ensure()
    server = Server(config=config, library=library, workspace=workspace, slug=slug,
                    stdin=stdin, stdout=out)
    thread = threading.Thread(target=server.serve_forever, name="test-serve", daemon=True)
    thread.start()
    return server, stdin, thread


def _wait_for(predicate, seconds: float, what: str) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_command_is_answered_while_a_graph_is_in_flight(config, library, monkeypatch):
    """`status` and `pause` must be answered *during* the run, not after it has ended."""
    from engine.orchestrator import Orchestrator

    timeline: list[str] = []
    pause_on_the_wire = threading.Event()
    out = CapturedOut(timeline=timeline,
                      on_ack=lambda payload: pause_on_the_wire.set()
                      if payload.get("cmd_id") == "p2" else None)
    monkeypatch.setattr(Orchestrator, "execute",
                        _in_flight_execute(release=pause_on_the_wire, timeline=timeline))

    server, stdin, thread = _live_server(config, library, slug="threadtest", out=out)
    try:
        _wait_for(lambda: "event:engine.ready" in timeline, 20, "the engine to be ready")
        stdin.send({"cmd_id": "s1", "type": "start", "payload": {"goal": "ship a landing page"}})
        _wait_for(lambda: "run-live" in timeline, 60, "the graph to be in flight")

        stdin.send({"cmd_id": "p1", "type": "status"})
        stdin.send({"cmd_id": "p2", "type": "pause"})
        stdin.send({"cmd_id": "p4", "type": "start",
                    "payload": {"goal": "a second goal while the first is running"}})
        _wait_for(pause_on_the_wire.is_set, 10, "the pause to be acknowledged")
        _wait_for(lambda: "run-finished" in timeline, 10, "the graph to settle")
    finally:
        stdin.close()
        thread.join(timeout=15)

    events = out.events()
    assert timeline.index("ack:p2") < timeline.index("run-finished"), (
        "the pause was answered after the graph had ended — it was queued behind the run it pauses")
    assert ack_for(events, "p2")["detail"]["paused"] is True

    assert timeline.index("ack:p1") < timeline.index("run-finished"), (
        "the status poll was answered after the graph had ended — no panel could have updated")
    status = ack_for(events, "p1")["detail"]
    assert status["running"] is True, "status must describe the run that is in flight, not a stale one"
    assert status["phase"] == "running"

    # One run at a time: the second `start` is refused, because the worker no longer serialises it by
    # accident now that the graph is off the worker.
    second = ack_for(events, "p4")
    assert second["ok"] is False and "already in flight" in second["error"]

    # The ack contract is unchanged: `start` is still answered, by the run thread, when it settles.
    started = ack_for(events, "s1")
    assert started is not None, "an unacknowledged start is indistinguishable from a lost one"
    assert started["ok"] is True and started["detail"]["phase"] == "done"
    assert started["detail"]["outcome"] == "done"
    assert server._in_flight == 0, "the run's in-flight token must be released when it settles"


def test_a_run_that_cannot_execute_acks_start_with_the_reason(config, library, monkeypatch):
    """A graph that fails is a failed `start`: `ok=false` **with the reason**, never silence."""
    from engine.orchestrator import Orchestrator

    timeline: list[str] = []
    refused = threading.Event()
    monkeypatch.setattr(Orchestrator, "execute", _in_flight_execute(
        release=refused, timeline=timeline, fail=RuntimeError("no executor available")))
    out = CapturedOut(timeline=timeline)

    server, stdin, thread = _live_server(config, library, slug="failrun", out=out)
    try:
        _wait_for(lambda: "event:engine.ready" in timeline, 20, "the engine to be ready")
        stdin.send({"cmd_id": "s1", "type": "start", "payload": {"goal": "ship a landing page"}})
        _wait_for(lambda: ack_for(out.events(), "s1") is not None, 60, "the start to be answered")
    finally:
        stdin.close()
        thread.join(timeout=15)

    payload = ack_for(out.events(), "s1")
    assert payload["ok"] is False
    assert "no executor available" in payload["error"]
    assert server._in_flight == 0, "a failed run must not leave the in-flight counter stuck"


# ── approving the plan, not a gate ───────────────────────────────────────────


def _recording_execute(calls: list[str]):
    """An `Orchestrator.execute` that settles at once, so a test can watch the route, not a run."""

    def execute(self, run=None, **_kwargs):
        from engine.orchestrator import RunPhase

        run = self._resolve(run)
        calls.append(run.run_id)
        run.phase = RunPhase.RUNNING
        self._persist(run)
        run.phase = RunPhase.DONE
        run.touch()
        self._persist(run)
        return _Outcome(False)

    return execute


def test_approve_plan_runs_the_graph_and_is_acked_once(config, library, monkeypatch):
    """The route the console lacked: approve the prepared graph and execute it.

    `approve` is `Orchestrator.decide`, which resolves a gate and raises for a plan — so a graph parked
    by "Plan only" had no command that could act on it. This is that command, and it is `start` minus the
    prepare step: the ack is the run's outcome, sent once, when the graph settles on the run thread.
    """
    from engine.orchestrator import Orchestrator

    calls: list[str] = []
    monkeypatch.setattr(Orchestrator, "execute", _recording_execute(calls))
    out = CapturedOut()

    server, stdin, thread = _live_server(config, library, slug="approveplan", out=out)
    try:
        _wait_for(lambda: any(e.get("type") == "engine.ready" for e in out.events()), 20, "ready")
        stdin.send({"cmd_id": "s1", "type": "start",
                    "payload": {"goal": "ship a landing page", "dry_run": True}})
        _wait_for(lambda: ack_for(out.events(), "s1") is not None, 60, "the plan to be parked")
        parked = ack_for(out.events(), "s1")
        assert parked["ok"] is True and parked["detail"]["phase"] == "awaiting_approval"

        stdin.send({"cmd_id": "a1", "type": "approve_plan"})
        _wait_for(lambda: ack_for(out.events(), "a1") is not None, 60, "the approval to settle")
    finally:
        stdin.close()
        thread.join(timeout=15)

    approved = ack_for(out.events(), "a1")
    assert approved["ok"] is True, approved
    assert approved["detail"]["phase"] == "done"
    assert approved["detail"]["outcome"] == "done"
    assert calls, "the graph the command approved must actually execute"
    assert len([e for e in acks(out.events()) if e["payload"].get("cmd_id") == "a1"]) == 1, \
        "a deferred ack must be sent exactly once — twice would corrupt the app's view"
    assert any(e.get("type") == "manifest.approved" for e in out.events())
    assert server._run_thread is None, "the liveness marker must be cleared once the run settles"


def test_approve_plan_is_refused_while_a_run_is_in_flight(config, library, monkeypatch):
    """Two graphs on one workspace is the failure `start`'s liveness guard exists to prevent."""
    from engine.orchestrator import Orchestrator

    timeline: list[str] = []
    release = threading.Event()
    monkeypatch.setattr(Orchestrator, "execute",
                        _in_flight_execute(release=release, timeline=timeline))
    out = CapturedOut(timeline=timeline)

    server, stdin, thread = _live_server(config, library, slug="busyapprove", out=out)
    try:
        _wait_for(lambda: "event:engine.ready" in timeline, 20, "the engine to be ready")
        stdin.send({"cmd_id": "s1", "type": "start",
                    "payload": {"goal": "ship a landing page", "dry_run": True}})
        _wait_for(lambda: ack_for(out.events(), "s1") is not None, 60, "the plan to be parked")
        stdin.send({"cmd_id": "a1", "type": "approve_plan"})
        _wait_for(lambda: "run-live" in timeline, 60, "the graph to be in flight")
        stdin.send({"cmd_id": "a2", "type": "approve_plan"})
        _wait_for(lambda: ack_for(out.events(), "a2") is not None, 20, "the refusal to be answered")
        release.set()
        _wait_for(lambda: ack_for(out.events(), "a1") is not None, 20, "the run to settle")
    finally:
        stdin.close()
        thread.join(timeout=15)

    second = ack_for(out.events(), "a2")
    assert second["ok"] is False and "already in flight" in second["error"]


def test_approve_plan_with_nothing_prepared_says_so(config, library):
    """A refusal a person can act on names what is missing, not just that it is missing."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "approve_plan"}])
    payload = ack_for(events, "c1")
    assert payload["ok"] is False
    assert "no prepared plan" in payload["error"]


def test_status_carries_the_pending_plan_after_a_relaunch(config, library):
    """The plan is event-sourced, so a console relaunched after "Plan only" must still have it.

    A *new* server over the same workspace is the relaunch: nothing is loaded in memory, so the plan can
    only come from the checkpoint the engine already keeps for the prepared run.
    """
    root = pathlib.Path(tempfile.mkdtemp())
    workspace = Workspace.for_project("relaunch", root=root)
    workspace.ensure()
    out = CapturedOut()
    stdin = io.StringIO(json.dumps({"cmd_id": "c1", "type": "start",
                                    "payload": {"goal": "ship a landing page",
                                                "dry_run": True}}) + "\n")
    Server(config=config, library=library, workspace=workspace, slug="relaunch",
           stdin=stdin, stdout=out).serve_forever()
    parked = ack_for(out.events(), "c1")
    assert parked["ok"] is True and parked["detail"]["phase"] == "awaiting_approval"

    fresh = Server(config=config, library=library, workspace=workspace, slug="relaunch",
                   stdin=io.StringIO(""), stdout=CapturedOut())
    status = fresh._cmd_status({})
    proposal = status.get("proposal")
    assert proposal, "the parked plan must survive the restart"
    assert proposal["nodes"], "with the step list the card draws"
    assert proposal["slug"] == "relaunch" and proposal["run_id"] == parked["detail"]["run_id"]
    assert proposal["approvable"] is True and proposal["reason"] == ""
    # And the app's other clearing rule cannot apply: the run is not running and nothing executes.
    assert status["phase"] == "idle" and status["running"] is False


def test_the_proposal_says_why_a_plan_is_not_approvable(config, library):
    """The engine's verdict, so the card renders no control the engine would refuse."""
    root = pathlib.Path(tempfile.mkdtemp())
    workspace = Workspace.for_project("unvalid", root=root)
    workspace.ensure()
    server = Server(config=config, library=library, workspace=workspace, slug="unvalid",
                    stdin=io.StringIO(""), stdout=CapturedOut())
    # The state `prepare` leaves, with a graph the validator refused — the one case where the engine's
    # `approve` raises rather than executing.
    server.workspace.save_checkpoint({
        "run_id": "run_unvalid", "slug": "unvalid", "goal": "g", "phase": "awaiting_approval",
        "manifest_path": str(workspace.path / "unvalid.yaml"),
        "plan": {"manifest": {"name": "unvalid", "nodes": [{"id": "a"}]},
                 "validation": {"valid": False, "errors": ["no end node"]}},
        "staffing_gaps": [],
    })

    proposal = server._cmd_status({})["proposal"]
    assert proposal["approvable"] is False
    assert "did not validate" in proposal["reason"]
    assert "no end node" in proposal["reason"], "the engine's own error text must reach the card"


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


def test_the_provider_kinds_are_the_configs_own_tuple(tmp_path):
    """The console's kind picker is built from this reply, so a dialect the engine accepts cannot be
    missing from it and one it refuses cannot appear. `SetupPane` used to spell the three by hand,
    which is a second copy of `config.SUPPORTED_KINDS` — the copy the reply replaces."""
    from engine.config import SUPPORTED_KINDS

    server, _ = _server_with_creds(tmp_path)
    assert server._cmd_providers({})["kinds"] == list(SUPPORTED_KINDS)


def test_the_agents_reply_carries_the_levels_and_roles_a_hire_may_name(tmp_path):
    """The hire form's pickers read these. The form listed five of `people.LEVELS`' six names, so
    `mid` could not be chosen from the app; the reply now carries the engine's own tables instead."""
    from engine.people import HIRE_ROLES, LEVELS

    server, _ = _server_with_creds(tmp_path)
    reply = server._cmd_agents({})
    assert reply["levels"] == list(LEVELS)
    assert "mid" in reply["levels"]
    assert reply["roles"] == list(HIRE_ROLES)


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


def test_the_local_ollama_kind_refuses_ollamas_cloud_endpoint(tmp_path):
    """The two are different protocols, and picking the wrong one looked like a bad key.

    `ollama` targets a *local server's* `/api/chat`; Ollama's cloud speaks the OpenAI dialect at
    `/v1`. Choosing the "Ollama" kind for `ollama.com` produced `…/chat/completions/api/chat`, whose
    404 is indistinguishable from an authentication failure — so the natural response was to re-paste
    a key and edit the URL, neither of which was wrong.

    Refused with the correction rather than silently rewritten: quietly swapping one protocol for the
    other would store a config describing something the person did not choose.
    """
    server, creds = _server_with_creds(tmp_path)
    with pytest.raises(Exception, match="OpenAI-compatible"):
        server._cmd_provider_add({"provider_id": "cloud", "kind": "ollama",
                                  "base_url": "https://ollama.com/v1/chat/completions",
                                  "api_key": "k" * 12})
    # The right pair is accepted, and the pasted endpoint is reduced to the base.
    server._cmd_provider_add({"provider_id": "cloud", "kind": "openai",
                              "base_url": "https://ollama.com/v1/chat/completions",
                              "api_key": "k" * 12})
    assert json.loads(creds.read_text())["providers"]["cloud"]["base_url"] == "https://ollama.com/v1"


def test_provider_add_requires_a_base_url(tmp_path):
    server, creds = _server_with_creds(tmp_path)
    with pytest.raises(Exception):
        server._cmd_provider_add({"provider_id": "x", "kind": "openai"})


def test_provider_add_keeps_a_pasted_key_even_when_a_variable_is_also_named(tmp_path):
    """Filling in both key fields must not discard the key.

    A person adding a cloud endpoint pastes the key they were given and may also name the variable
    they intend to use later. The save used to write *one* of the two (env `elif` literal), so the
    pasted key vanished; the next call then reported "has no API key" to someone who had just supplied
    one, and the variable was unset on the machine. Both are now kept, and `resolve_key` reads the
    variable first — the safer source still wins, and the key is there when it is not.
    """
    server, creds = _server_with_creds(tmp_path)
    server._cmd_provider_add({"provider_id": "ollamacloud", "kind": "openai",
                              "base_url": "https://ollama.com/v1/chat/completions",
                              "api_key": "pasted-" + "x" * 20,
                              "api_key_env": "OLLAMA_API_KEY"})
    entry = json.loads(creds.read_text())["providers"]["ollamacloud"]
    assert entry["api_key_env"] == "OLLAMA_API_KEY"
    assert entry.get("api_key"), "the pasted key must survive a save that also names a variable"
    # And the pasted endpoint is reduced to the base a provider can append an operation to.
    assert entry["base_url"] == "https://ollama.com/v1"


def test_a_full_endpoint_is_accepted_as_a_base_url(tmp_path):
    """People are handed `…/v1/chat/completions` and type it into the base field.

    Appending `/models` to it probes a path that does not exist, so a correct key and a correct URL
    were reported as an unreachable provider. The operation is stripped on the way in.
    """
    from engine.config import normalize_base_url

    base, note = normalize_base_url("https://ollama.com/v1/chat/completions", "openai")
    assert base == "https://ollama.com/v1"
    assert "chat/completions" in note, "the correction must be reported, not silent"

    server, creds = _server_with_creds(tmp_path)
    server._cmd_provider_add({"provider_id": "shortened", "kind": "openai",
                              "base_url": "https://ollama.com/v1/chat/completions",
                              "api_key": "k" * 12})
    assert json.loads(creds.read_text())["providers"]["shortened"]["base_url"] == "https://ollama.com/v1"


def test_provider_remove_deletes_only_the_named_one(tmp_path):
    server, creds = _server_with_creds(tmp_path)
    server._cmd_provider_add({"provider_id": "groq", "kind": "openai",
                              "base_url": "https://api.groq.com/openai/v1"})
    server._cmd_provider_remove({"provider_id": "groq"})
    document = json.loads(creds.read_text())
    assert "groq" not in document["providers"]
    assert "ollama" in document["providers"]


def test_provider_remove_refuses_the_last_provider(tmp_path):
    """Removing the only provider would write a config no launch can read.

    `load` raises "config defines no providers", and the running server *hides* the breakage — it
    swallows the reload failure and keeps serving the stale object — so the damage would surface only
    on the next start. Refusing names the way forward instead of quietly keeping or inventing a
    replacement, which would be a routing decision the person did not make.
    """
    server, creds = _server_with_creds(tmp_path)
    before = creds.read_text()
    with pytest.raises(Exception, match="only provider"):
        server._cmd_provider_remove({"provider_id": "ollama"})
    assert creds.read_text() == before, "a refused removal must not write anything"
    assert "ollama" in load(str(creds), warn=False).providers


def test_provider_remove_reports_the_agents_left_bound_to_it(tmp_path):
    """A removal re-points nobody: the roster keeps the binding, so those agents stop being callable.

    `people.save` writes an agent's `provider` back verbatim, so an agent hired onto an endpoint keeps
    that provider after the endpoint is gone. The engine names those agents in its reply, which is what
    lets the console report the blast radius with names instead of asserting it in prose of its own.
    """
    server, creds = _server_with_creds(tmp_path)
    server._cmd_provider_add({"provider_id": "groq", "kind": "openai",
                              "base_url": "https://api.groq.com/openai/v1"})
    hired = server._cmd_hire({"name": "Bound", "skill": "code-reviewer", "provider": "groq",
                              "model": "qwen2.5-coder:7b", "context_window": 32768})["agent"]
    reply = server._cmd_provider_remove({"provider_id": "groq"})
    assert reply["agent_count"] == 1
    assert reply["agents"] == [{"id": hired["id"], "name": "Bound"}]


def test_a_removal_does_not_blame_the_built_in_company(tmp_path):
    """The built-ins are re-derived from the resolved default, so a removed default re-points them.

    Reporting them would be a false "these agents broke": the next load binds them to whatever the
    engine now resolves. Only a *persisted* binding cannot move, and only those are reported.
    """
    from engine.config import set_defaults

    server, creds = _server_with_creds(tmp_path)
    server._cmd_provider_add({"provider_id": "groq", "kind": "openai",
                              "base_url": "https://api.groq.com/openai/v1"})
    # Make groq the declared default, so the built-in company is bound to it right now.
    set_defaults(creds, provider="groq", model="qwen2.5-coder:7b")
    server._reload_config()
    reply = server._cmd_provider_remove({"provider_id": "groq"})
    assert reply["agents"] == []
    assert reply["agent_count"] == 0


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


def test_the_liveness_check_answers_gone_rather_than_unknown_for_a_dead_pid():
    """The branch the orphan came from — pinned, because "unknown" read as "alive" is unfalsifiable.

    `_pid_alive` must be able to say *gone*, and say it the same way for both shapes a dead parent
    takes: a pid that is free (the app reaped by launchd — the normal case) and a pid that is still
    held by a zombie (the app not yet reaped — the case the docstring names). A check that answered
    "alive" for either would poll for ever, which is what left the engine holding the project.
    """
    import engine.serve as serve

    # Beyond any `pid_max` (Linux caps at 2**22, macOS's is smaller still), so it cannot name a process.
    assert serve._pid_alive(2 ** 30) is False
    # We are running, so we are alive — the check must not answer False to everything.
    assert serve._pid_alive(os.getpid()) is True

    # A zombie: exited, not yet reaped. Its pid still exists and `kill -0` still succeeds, which is
    # exactly why the status has to be read rather than asked about.
    zombie = os.fork()
    if zombie == 0:  # pragma: no cover - the child's whole job is to exit
        os._exit(0)
    try:
        time.sleep(0.5)
        assert serve._pid_alive(zombie) is False, (
            "a zombie is a dead process: reporting it alive is a check that cannot fail")
    finally:
        os.waitpid(zombie, 0)


def test_an_unanswerable_liveness_check_ends_in_gone(monkeypatch):
    """What the check does when the platform will not describe the process — the case that used to leak.

    `_pid_alive` answering `None` must not be read as alive. The verdict comes from one fact the
    platform always supplies: a process is our parent until it exits, so if the named pid is no longer
    our parent it is gone; and if it *is* still our parent it exists, and a failed read is not a reason
    to stop a working engine.
    """
    import engine.serve as serve

    monkeypatch.setattr(serve, "_pid_alive", lambda pid: None)
    monkeypatch.setattr(serve.os, "getppid", lambda: 1)
    assert serve._parent_is_gone(4242, started_as_child=True) is True

    monkeypatch.setattr(serve.os, "getppid", lambda: 4242)
    assert serve._parent_is_gone(4242, started_as_child=True) is False


def test_a_live_pid_is_only_proof_the_app_lives_while_it_is_our_parent(monkeypatch):
    """A live pid is not proof the *app* is alive: pids are reused.

    When we were spawned by the named pid, being reparented away from it is the kernel saying it
    exited — whatever the pid now refers to. This is the one check that cannot be fooled by a pid
    reused by an unrelated process, each of which the other checks would report as alive for ever.
    """
    import engine.serve as serve

    # Our own pid is certainly alive; with `getppid` still naming us, it is the parent and alive.
    monkeypatch.setattr(serve.os, "getppid", lambda: os.getpid())
    assert serve._parent_is_gone(os.getpid(), started_as_child=True) is False

    # The same live pid, no longer our parent: the app that spawned us is gone and something else now
    # holds the number.
    monkeypatch.setattr(serve.os, "getppid", lambda: 1)
    assert serve._parent_is_gone(os.getpid(), started_as_child=True) is True


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


def test_status_carries_the_activity_timeline(config, library):
    """The "what is happening" panel is populated from the poll every panel already makes."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "status"}])
    detail = ack_for(events, "c1")["detail"]
    assert "activity" in detail, "the status snapshot must carry the activity timeline"
    activity = detail["activity"]
    for key in ("headline", "timeline", "counts", "next_action", "going"):
        assert key in activity, f"activity is missing {key!r}"


def test_activity_is_pollable_on_its_own(config, library):
    """A snapshot command, so the panel can refresh without a start/stop."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "activity"}])
    detail = ack_for(events, "c1")["detail"]
    assert "headline" in detail and isinstance(detail.get("timeline"), list)


def test_status_carries_the_mission(config, library):
    """The console shows the standing purpose from the poll it already makes."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "status"}])
    detail = ack_for(events, "c1")["detail"]
    assert "mission" in detail, "the status snapshot must carry the mission"
    assert "state" in detail["mission"] and "objectives" in detail["mission"]


def test_mission_is_pollable_on_its_own(config, library):
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "mission"}])
    detail = ack_for(events, "c1")["detail"]
    assert "mission" in detail
    assert detail["mission"]["statement"] == ""


def test_status_carries_the_portfolio(config, library):
    """The console shows every org the principal runs from the poll it already makes."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "status"}])
    detail = ack_for(events, "c1")["detail"]
    assert "portfolio" in detail, "the status snapshot must carry the portfolio"
    assert "orgs" in detail["portfolio"]


def test_portfolio_is_pollable_on_its_own(config, library):
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "portfolio"}])
    detail = ack_for(events, "c1")["detail"]
    assert "orgs" in detail and "active_org_id" in detail


def test_portfolio_live_is_a_separate_command(config, library):
    """The live cross-org view is expensive, so it is its own command, not the status poll."""
    _, events = drive(config, library, [{"cmd_id": "c1", "type": "portfolio_live"}])
    detail = ack_for(events, "c1")["detail"]
    assert "rollup" in detail or "fleet" in detail


def test_the_status_snapshot_carries_the_new_panels(tmp_path):
    """The app polls `status`; every new panel's data must travel with it.

    The console reads these keys by name, so a missing one is a blank panel. `flow` (who is working on
    what), `defaults` (the model everyone runs on) and `goal` all have to be present in **both** the
    idle and the running branch, which is why the idle branch is tested here.
    """
    server, _ = _server_with_creds(tmp_path, slug="panels")
    payload = server._cmd_status({})
    for key in ("flow", "defaults", "goal", "activity", "portfolio", "subagents"):
        assert key in payload, f"{key} must travel with status or its panel cannot render"
    defaults = payload["defaults"]
    assert defaults["provider"] == "ollama"
    assert defaults["model"] == "qwen2.5-coder:7b"
    # The window the system will actually bind with — not the declared table alone.
    assert defaults["context_window"] == 32768
    assert defaults["autonomy"]["auto_pass_auto_gates"] is True


# ── stopping, refusing and the writes that used to kill the worker ───────────
#
# Found by an audit of this file, each with a reproduction against the real server. They share one
# shape, which is the shape that matters here: a command the app had already sent going unanswered.
# An unacknowledged command is indistinguishable from a lost one, so the app waits its full 600s budget
# per command on a reply that was never going to come — against an engine that looked healthy.


class _AckBreakingOut(CapturedOut):
    """stdout whose acknowledgements cannot be written, recording every line it was asked for.

    The reproduction of the worst of them: `emit` guarded `json.dumps` and not the write. A
    `BrokenPipeError` from an ack was caught by `_work`'s `except`, whose *failure* ack raised in turn —
    and that one escaped, so the worker thread died inside an acknowledgement while the queue still held
    every command behind it. Recording what was *attempted* is what distinguishes "answered" from
    "never reached": the old worker died on the first command and never attempted the other two.
    """

    def __init__(self) -> None:
        super().__init__()
        self.attempted: list[str] = []

    def write(self, text: str) -> None:
        self.attempted.append(text)
        if '"command.ack"' in text:
            raise BrokenPipeError(32, "Broken pipe")
        super().write(text)

    def attempted_acks(self) -> list[str]:
        ids = []
        for line in self.attempted:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "command.ack":
                ids.append(event["payload"]["cmd_id"])
        return ids


def _plain_server(config, library, slug, *, out, stdin=None):
    """A server over in-memory streams, returned so a test can inspect its own state."""
    workspace = Workspace.for_project(slug, root=pathlib.Path(tempfile.mkdtemp()))
    workspace.ensure()
    return Server(config=config, library=library, workspace=workspace, slug=slug,
                  stdin=stdin if stdin is not None else io.StringIO(""), stdout=out)


def test_a_failed_write_during_an_ack_cannot_kill_the_worker(config, library):
    """A write failure must not propagate, and must not cost the queue its answers.

    Measured before the fix, with a stdout that raised on every ack: `serve_forever` returned 0 after
    10.01s, the worker thread was dead from an unhandled `BrokenPipeError`, zero acks were written and
    the queue still held all three commands. The app then waits 600s per command against an engine that
    looks healthy. `_log`'s own doctrine says a diagnostic that can break the code path it describes is
    worse than none — `emit` is on the *ack* path, so the same rule has to hold there.
    """
    raised: list[BaseException] = []
    previous = threading.excepthook
    threading.excepthook = lambda args: raised.append(args.exc_value)
    out = _AckBreakingOut()
    stdin = io.StringIO("\n".join(json.dumps(c) for c in [
        {"cmd_id": "c1", "type": "status"},
        {"cmd_id": "c2", "type": "org"},
        {"cmd_id": "c3", "type": "pool"}]) + "\n")
    server = _plain_server(config, library, "writefail", out=out, stdin=stdin)
    try:
        started = time.time()
        code = server.serve_forever()
        elapsed = time.time() - started
    finally:
        threading.excepthook = previous

    assert code == 0
    assert not raised, f"the worker died instead of stopping: {raised}"
    assert server._output_dead.is_set(), "a failed write must be recorded as dead output"
    # Every command that was sent got an answer *attempted* — c1 ran, c2 and c3 were refused.
    assert set(out.attempted_acks()) == {"c1", "c2", "c3"}, (
        f"a command was left unanswered: attempted {out.attempted_acks()}")
    assert server._commands.qsize() == 0, "a queued command was dropped without an answer"
    # Not the old 10s drain that waited for a queue nobody would empty any more.
    assert elapsed < 5.0, f"the dead output should stop the engine at once, took {elapsed:.1f}s"


def test_a_command_past_the_old_twelve_second_bound_is_still_acknowledged(config, library):
    """A command that ran is answered, even when it outruns the old drain-and-join budget.

    `_drain` waited 10s and `_shutdown` joined the worker for 2s, so a handler longer than ~12s had its
    ack written after `serve_forever` had already returned. Measured against the real server: 11.5s kept
    its ack, 13.0s lost it. The bound is now `engine.serve._DRAIN_TIMEOUT_S` (60s), and this test is the
    slow end of that: it fails if the bound is ever quietly lowered again.
    """
    from engine import serve as serve_module

    slept = 13.0
    assert serve_module._DRAIN_TIMEOUT_S > slept, (
        "the drain bound must exceed the window that was measured losing acks")

    class Slow(Server):
        def _cmd_status(self, payload):
            time.sleep(slept)
            return {"slow": True}

    out = CapturedOut()
    stdin = io.StringIO(json.dumps({"cmd_id": "slow1", "type": "status"}) + "\n")
    workspace = Workspace.for_project("boundtest", root=pathlib.Path(tempfile.mkdtemp()))
    workspace.ensure()
    server = Slow(config=config, library=library, workspace=workspace, slug="boundtest",
                  stdin=stdin, stdout=out)
    started = time.time()
    code = server.serve_forever()
    elapsed = time.time() - started

    assert code == 0
    payload = ack_for(out.events(), "slow1")
    assert payload is not None, f"the {slept}s command ran but was never acknowledged"
    assert payload["ok"] is True
    assert elapsed >= slept, "the ack cannot have preceded the command"


def test_commands_left_in_the_queue_when_the_engine_stops_are_refused(config, library):
    """A `shutdown` ends the worker, so everything behind it is answered as a refusal.

    Measured before the fix: three commands, one ack, `serve_forever` returned 0 after 10.03s with
    `qsize == 3`. The stopped server had silently dropped two commands the app was waiting on. A command
    that will not run is still an answerable command — "never ran, and why" is the answer.
    """
    class Slow(Server):
        def _cmd_status(self, payload):
            time.sleep(1.0)      # long enough for the read loop to queue every line behind it
            return {"slow": True}

    out = CapturedOut()
    stdin = io.StringIO("\n".join(json.dumps(c) for c in [
        {"cmd_id": "c1", "type": "status"},
        {"cmd_id": "c2", "type": "shutdown"},
        {"cmd_id": "c3", "type": "status"},
        {"cmd_id": "c4", "type": "org"}]) + "\n")
    workspace = Workspace.for_project("shutqueue", root=pathlib.Path(tempfile.mkdtemp()))
    workspace.ensure()
    server = Slow(config=config, library=library, workspace=workspace, slug="shutqueue",
                  stdin=stdin, stdout=out)
    started = time.time()
    code = server.serve_forever()
    elapsed = time.time() - started
    events = out.events()

    assert code == 0
    assert ack_for(events, "c1")["ok"] is True
    assert ack_for(events, "c2")["ok"] is True, "the shutdown itself must be acknowledged"
    for cmd_id in ("c3", "c4"):
        payload = ack_for(events, cmd_id)
        assert payload is not None, f"{cmd_id} was left unanswered by the stop"
        assert payload["ok"] is False
        assert "never ran" in payload["error"], payload["error"]
    assert server._commands.qsize() == 0, "the queue must be emptied by answering it"
    assert elapsed < 5.0, f"a stopped engine should not wait out the drain, took {elapsed:.1f}s"


def test_a_command_arriving_after_the_stop_is_refused_not_ignored(config, library):
    """A line read *after* the stop is refused, because `break`ing on it was silence.

    The read loop checked the stop flag before parsing the line it had just read, so a command sent
    while the engine was stopping was dropped without a word — the app cannot tell that from a lost
    command, and it has a 600s budget to sit through.
    """
    out = CapturedOut()
    server, stdin, thread = _live_server(config, library, slug="afterstop", out=out)
    try:
        _wait_for(lambda: any(e.get("type") == "engine.ready" for e in out.events()), 20,
                  "the engine to be ready")
        stdin.send({"cmd_id": "c1", "type": "shutdown"})
        _wait_for(lambda: ack_for(out.events(), "c1") is not None, 10, "the shutdown to be answered")
        stdin.send({"cmd_id": "c2", "type": "status"})
        _wait_for(lambda: ack_for(out.events(), "c2") is not None, 10,
                  "the late command to be answered")
    finally:
        stdin.close()
        thread.join(timeout=15)

    payload = ack_for(out.events(), "c2")
    assert payload["ok"] is False
    assert "stop" in payload["error"], payload["error"]


def test_a_burst_beyond_the_queue_bound_is_refused_not_grown(config, library, monkeypatch):
    """The queue is bounded, and the commands that do not fit are answered, not dropped.

    It was unbounded: an app sending faster than the worker drains grew it without limit, which is an
    engine that answers later and later while looking healthy. The bound is enforced by `put_nowait` on
    a sized queue; the burst here is sized against a small bound so the test stays fast, but the real
    bound is asserted to be finite too — a bound of none is the defect.
    """
    from engine import serve as serve_module

    assert 0 < serve_module._COMMAND_QUEUE_MAX <= 10_000, "the command queue must stay bounded"
    monkeypatch.setattr(serve_module, "_COMMAND_QUEUE_MAX", 8)

    class Slow(Server):
        def _cmd_status(self, payload):
            time.sleep(0.05)
            return {"slow": True}

    total = 20
    out = CapturedOut()
    stdin = io.StringIO("\n".join(
        json.dumps({"cmd_id": f"c{i}", "type": "status"}) for i in range(total)) + "\n")
    workspace = Workspace.for_project("boundq", root=pathlib.Path(tempfile.mkdtemp()))
    workspace.ensure()
    server = Slow(config=config, library=library, workspace=workspace, slug="boundq",
                  stdin=stdin, stdout=out)
    assert server.serve_forever() == 0
    events = out.events()

    answered = {e["payload"]["cmd_id"] for e in acks(events)}
    assert answered == {f"c{i}" for i in range(total)}, (
        f"every command must be answered, missing: "
        f"{ {f'c{i}' for i in range(total)} - answered }")
    refused = [e["payload"] for e in acks(events) if not e["payload"]["ok"]]
    assert refused, "a burst larger than the bound must produce at least one refusal"
    assert "behind" in refused[0]["error"], refused[0]["error"]
    assert server._commands.qsize() == 0


def test_two_untagged_commands_get_distinct_ids(config, library, tmp_path):
    """The fallback `cmd_id` is unique per command, not per millisecond.

    The app correlates its answers on `cmd_id`. A millisecond timestamp gave two untagged commands in
    the same millisecond one name, so the reply to one was a reply to the other — the app would resolve
    the wrong command, or resolve one that was still running.
    """
    server, _ = _server_with_creds(tmp_path, slug="cmdid")
    ids = [server._parse(json.dumps({"type": "status"})).cmd_id for _ in range(3)]
    assert len(set(ids)) == 3, f"commands shared an id: {ids}"
    assert all(cmd_id.startswith("cmd_") for cmd_id in ids)


def test_resume_is_refused_while_a_run_is_in_flight(config, library, monkeypatch):
    """`resume` under a live run would rewrite the run the run thread is executing.

    `Orchestrator.resume` sets the phase to READY, clears the gate and persists — while the run thread
    writes its own phases to the same run and the same checkpoint. The check is `_cmd_start`'s check, for
    the same reason: the run thread is the only thing that can answer "is a graph executing right now".
    """
    from engine.orchestrator import Orchestrator

    timeline: list[str] = []
    release = threading.Event()
    monkeypatch.setattr(Orchestrator, "execute",
                        _in_flight_execute(release=release, timeline=timeline))
    out = CapturedOut(timeline=timeline)
    server, stdin, thread = _live_server(config, library, slug="resumeguard", out=out)
    try:
        _wait_for(lambda: "event:engine.ready" in timeline, 20, "the engine to be ready")
        stdin.send({"cmd_id": "s1", "type": "start", "payload": {"goal": "ship a landing page"}})
        _wait_for(lambda: "run-live" in timeline, 60, "the graph to be in flight")
        stdin.send({"cmd_id": "r1", "type": "resume"})
        _wait_for(lambda: ack_for(out.events(), "r1") is not None, 10,
                  "the resume to be answered")

        payload = ack_for(out.events(), "r1")
        assert payload["ok"] is False, "a resume against a live graph must be refused"
        assert "already in flight" in payload["error"], payload["error"]
        live = getattr(server.orchestrator, "_run", None)
        assert live is not None and live.phase.value == "running", (
            "the refusal must leave the executing run's phase alone")
    finally:
        release.set()
        stdin.close()
        thread.join(timeout=15)


def test_a_failed_portfolio_load_does_not_overwrite_the_register(tmp_path, monkeypatch):
    """One bad read must not license `portfolio_add` to replace the register.

    Measured before the fix: `_load_portfolio` marked itself loaded *before* the attempt and swallowed
    the failure into `self._portfolio = None`; `_cmd_portfolio_add` read that as "no portfolio", built
    `Portfolio.new()` and saved it — and `Portfolio.save` **replaces** the file. One transient read
    failure and the user's orgs are a one-org file. A register that could not be read is not a register
    that is absent.
    """
    from engine import serve as serve_module

    home = tmp_path / "home"
    home.mkdir()
    register = home / "portfolio.json"
    broken = "{ this is not a register"
    register.write_text(broken)
    monkeypatch.setenv("AGENTORG_HOME", str(home))

    server = _plain_server(None, None, "portfail", out=CapturedOut())
    with pytest.raises(serve_module.ServerError) as caught:
        server._cmd_portfolio_add({"name": "Tesla"})
    assert "could not be read" in str(caught.value), str(caught.value)
    assert register.read_text() == broken, "the failed read must not have been written over"

    # The snapshot says so rather than rendering as "no orgs", which is what invites the overwrite.
    snapshot = server._cmd_portfolio({})
    assert snapshot["orgs"] == [] and snapshot["error"], snapshot

    # And the failure is not cached: the next read succeeds once the register is readable again.
    from engine.portfolio import Portfolio

    fixed = Portfolio.new()
    fixed.add_org(name="SpaceX", slug="spacex")
    fixed.save()
    assert server._load_portfolio() is not None, "a read failure must be retried, not remembered"
    assert [org["name"] for org in server._cmd_portfolio({})["orgs"]] == ["SpaceX"]
    assert server._cmd_portfolio({})["error"] == ""

    # Which is what lets an add merge into the real register instead of replacing it.
    server._cmd_portfolio_add({"name": "Waymo", "slug": "waymo"})
    assert sorted(org["name"] for org in server._cmd_portfolio({})["orgs"]) == ["SpaceX", "Waymo"]


# ── the stop paths, against a real process ───────────────────────────────────

_STAND_IN = '''\
import json, sys

sys.path.insert(0, {repo!r})
from engine.serve import Server


class Cfg:
    providers = {{}}
    path = None


class WS:
    path = {ws!r}
    display_name = "stand-in"
    slug = "stand-in"
    root = "/tmp"


class Probe(Server):
    def _cmd_sleep(self, payload):
        import time
        time.sleep(float(payload.get("t") or 0))
        return {{"slept": payload.get("t")}}


server = Probe(config=Cfg(), library=None, workspace=WS(), slug="stand-in")
{extra}
server.serve_forever()
'''


def _spawn_stand_in(tmp_path, *, extra: str = ""):
    """A real server process over real pipes, for the behaviour that only exists between processes."""
    repo = pathlib.Path(__file__).resolve().parent.parent
    script = tmp_path / "stand_in_server.py"
    script.write_text(_STAND_IN.format(repo=str(repo), ws=str(tmp_path), extra=extra))
    proc = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    seen: list[str] = []
    ready = threading.Event()

    def _read() -> None:
        for line in proc.stdout:
            seen.append(line.strip())
            if "engine.ready" in line:
                ready.set()

    threading.Thread(target=_read, name="stand-in-reader", daemon=True).start()
    if not ready.wait(30):
        proc.kill()
        pytest.skip("the stand-in engine did not start in time")
    return proc, seen


def _send(proc, command: dict) -> None:
    proc.stdin.write(json.dumps(command) + "\n")
    proc.stdin.flush()


def _acks_from(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "command.ack":
            out.append(event["payload"])
    return out


def test_sigterm_unwinds_the_engine_through_its_own_shutdown(tmp_path):
    """The app's Stop path, against a real process: SIGTERM must not kill it where it stands.

    `terminate()` closes the command pipe and then signals, and the Swift comment on it claims the
    engine "finishes the command in flight, writes its checkpoint, and exits" — while `serve._drain`
    was the wait it described and no SIGTERM handler existed, so Python's default action killed the
    process in milliseconds and the wait never ran. This is the real thing: a real process, a real
    SIGTERM, no pipe close at all (which is the harder case — a *parked* read, where a flag alone
    cannot unwind the loop, only an exception can).
    """
    proc, seen = _spawn_stand_in(tmp_path)
    try:
        _send(proc, {"cmd_id": "c1", "type": "sleep", "payload": {"t": 30}})
        time.sleep(0.5)                     # let the worker be inside the command
        started = time.time()
        proc.send_signal(signal.SIGTERM)
        try:
            code = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("the engine ignored SIGTERM and had to be killed")
        elapsed = time.time() - started
    finally:
        if proc.poll() is None:
            proc.kill()

    assert code == 0, f"a signalled engine should exit cleanly, got {code}"
    # The command in flight cannot finish inside the app's 5s grace, and the engine says so rather than
    # exiting quietly — that is the "where it is genuinely impossible" half of the ack contract.
    payloads = _acks_from(seen)
    assert payloads, "the in-flight command was never answered"
    assert payloads[-1]["cmd_id"] == "c1" and payloads[-1]["ok"] is False
    assert "gave up waiting" in payloads[-1]["error"], payloads[-1]["error"]
    # Bounded by the signal's own cap, not by the 60s drain: the app SIGKILLs 5s after signalling.
    assert elapsed < 5.0, f"the exit must fit inside the app's grace, took {elapsed:.1f}s"


def test_stop_now_is_the_last_resort_it_claims_to_be(tmp_path):
    """The parent watchdog's force-exit waits for the process to stop itself first.

    `_stop_now` called `os._exit(0)` immediately from the watchdog thread: no drain, no shutdown, and it
    could take the process down mid-write. It is still the last resort — the read loop may never see EOF,
    which is why the watchdog exists — but "last" now means after the process had its chance.
    """
    extra = ("import threading, time\n"
             "threading.Thread(target=lambda: (time.sleep(0.5), server._stop_now()), daemon=True).start()\n")
    proc, seen = _spawn_stand_in(tmp_path, extra=extra)
    try:
        _send(proc, {"cmd_id": "c1", "type": "sleep", "payload": {"t": 2.0}})
        try:
            code = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("the force-exit never happened — the watchdog would be useless")
    finally:
        if proc.poll() is None:
            proc.kill()

    assert code == 0
    payloads = _acks_from(seen)
    assert payloads, "os._exit fired before the command in flight could be acknowledged"
    assert payloads[-1]["cmd_id"] == "c1" and payloads[-1]["ok"] is True
