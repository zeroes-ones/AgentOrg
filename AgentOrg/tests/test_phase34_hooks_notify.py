#!/usr/bin/env python3
"""Phase 34 tests — lifecycle hooks, and the notification an unattended run owes you.

Two gaps, one theme: a run you leave alone. There was no way to say "when a run finishes, do X"
without the engine growing a feature per X, and there was no way for an unattended run to tell anyone
it had stopped. The reference agents ship twenty lifecycle events; this engine shipped none.

The tests are ordered by how much the property matters:

1. **A hook cannot change, break, or delay-fail the run.** Three failure shapes are exercised on
   purpose — a non-zero exit, a hang, and a binary that does not exist — because a hook is an
   *observation*. If any of them raised, or stopped the run, the feature would be a second control
   plane nobody can reason about. This is the assertion the module is built around.
2. **A hook is told what happened, and cannot be told anything else.** The event arrives as one JSON
   document on stdin *and* in the environment, and the payload is not interpolated into the command,
   so a payload cannot become the command.
3. **A notification fires for the list that was asked for and nothing else.** A channel that pings on
   every event is one you learn to ignore, which is worse than no channel — so the unlisted event is
   asserted *not* to notify, not merely ignored.
4. **Neither of them blocks the run for longer than the configured bound.**
"""

from __future__ import annotations

import http.server
import json
import pathlib
import sys
import tempfile
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.bus import EventBus
from engine.config import HooksConfig, NotifyConfig, load
from engine.hooks import (
    DISABLED_ENV,
    ENV_EVENT,
    ENV_PAYLOAD,
    ENV_RUN_ID,
    ENV_SLUG,
    HOOK_LOG_NAME,
    HookRunner,
    Lifecycle,
    Notifier,
    read_hook_log,
)
from engine.protocol import Event, EventType


class Section:
    """A stand-in for a config whose `hooks`/`notify` attributes are given, or `None` when absent.

    Two reasons it exists rather than a real `Config`. `_build_simple` ignores unknown keys, so a
    config cannot express "no hooks section at all" — the case `Lifecycle.attach` has to refuse, and
    the case a caller with a hand-built `Config` hits. And it keeps the tests honest about which two
    attributes these modules read, since adding a third read would fail here.
    """

    def __init__(self, hooks: HooksConfig | None = None, notify: NotifyConfig | None = None) -> None:
        self.hooks = hooks
        self.notify = notify


def hook_event(name: str = "run.end", *, slug: str = "demo", **payload) -> Event:
    """The event shape the module is handed in production: the bus redacts, then delivers this."""
    return Event(seq=1, type=name, run_id="run_1", payload={"slug": slug, **payload})


def wait_for(predicate, *, timeout_s: float = 5.0) -> bool:
    """Poll until a condition holds, or give up after the grace period.

    Used by the assertions about a timed-out hook's *descendants*, where the honest form of the check
    is "nothing has happened yet". A fixed `sleep` would be a guess about this machine's scheduler.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ── a hook is told what happened ─────────────────────────────────────────────


def test_a_hook_receives_the_event_on_stdin_and_in_the_environment(tmp_path):
    """The event arrives as one JSON document on stdin, and as the four documented variables.

    Both, not one: a shell one-liner wants `$AGENTORG_EVENT` without parsing, and a script that wants
    the payload wants the whole document. Delivering only one of them makes the feature useful to only
    one kind of hook.
    """
    out = tmp_path / "seen.json"
    command = (
        f'{sys.executable} -c '
        f"'import json,os,sys; "
        f"json.dump({{\"stdin\": json.load(sys.stdin), \"env\": "
        f"{{k: os.environ.get(k) for k in (\"AGENTORG_EVENT\",\"AGENTORG_RUN_ID\",\"AGENTORG_SLUG\","
        f"\"AGENTORG_PAYLOAD\")}}}}, open(r\"{out}\", \"w\"))'"
    )
    runner = HookRunner(Section(HooksConfig(events={"run.end": command})),
                        run_id="run_1", slug="fallback", state_dir=tmp_path)
    outcomes = runner.dispatch(hook_event("run.end", slug="demo", outcome="done"))

    assert [o.status for o in outcomes] == ["ok"]
    seen = json.loads(out.read_text())
    assert seen["stdin"]["type"] == "run.end"
    assert seen["stdin"]["payload"] == {"slug": "demo", "outcome": "done"}
    assert seen["env"][ENV_EVENT] == "run.end"
    assert seen["env"][ENV_RUN_ID] == "run_1"
    # The slug comes from the event, not from the constructor: a bus knows a run id, never a project.
    assert seen["env"][ENV_SLUG] == "demo"
    assert json.loads(seen["env"][ENV_PAYLOAD]) == {"slug": "demo", "outcome": "done"}


def test_a_wildcard_hook_fires_for_every_event(tmp_path):
    """`*` is the escape hatch for "I want to see everything", and it must really be everything.

    Without it a person who wants one tee-command per event has to enumerate the vocabulary, which
    goes stale the first time the engine adds an event.
    """
    log = tmp_path / "events.txt"
    runner = HookRunner(Section(HooksConfig(events={"*": f"echo $AGENTORG_EVENT >> {log}"})),
                        run_id="run_1", state_dir=tmp_path)
    for name in ("run.start", "node.enter", "llm.response", "run.end"):
        runner.dispatch(hook_event(name))

    assert log.read_text().split() == ["run.start", "node.enter", "llm.response", "run.end"]


def test_a_prefix_pattern_covers_its_family_and_the_payload_is_not_the_command(tmp_path):
    """`goal.*` matches the goal family, and a payload is never interpolated into the command.

    The second half is the security property: the command is exactly the string the person wrote, so
    a slug or a model output containing `$(…)` or `;` cannot become a command. The first half is the
    ergonomic one — a config entry that looks like it should work and silently fires nothing is the
    failure this codebase refuses elsewhere.
    """
    log = tmp_path / "family.txt"
    runner = HookRunner(Section(HooksConfig(events={"goal.*": f"echo $AGENTORG_EVENT >> {log}"})),
                        run_id="run_1", state_dir=tmp_path)
    runner.dispatch(hook_event("goal.completed"))
    runner.dispatch(hook_event("goal.blocked"))
    runner.dispatch(hook_event("run.end"))
    assert log.read_text().split() == ["goal.completed", "goal.blocked"]

    hostile = tmp_path / "owned"
    payload = {"text": f"'; touch {hostile}; echo '"}
    ran = runner.dispatch(hook_event("goal.paused", **payload))
    assert ran[0].status == "ok"
    assert not hostile.exists(), "a payload must never reach the shell as code"


# ── a hook cannot break the run ──────────────────────────────────────────────


def test_a_hook_that_fails_is_recorded_and_the_rest_still_run(tmp_path):
    """A non-zero exit is a *recorded* failure, and the next hook for the same event still fires.

    If the first command's failure aborted the list, one broken hook would silently disable every
    other hook — a config that half-works and reports nothing.
    """
    log = tmp_path / "second.txt"
    runner = HookRunner(
        Section(HooksConfig(events={"run.end": ["false", f"echo ran >> {log}"]})),
        run_id="run_1", state_dir=tmp_path)

    outcomes = runner.dispatch(hook_event())

    assert [o.status for o in outcomes] == ["failed", "ok"]
    assert outcomes[0].exit_code != 0
    assert outcomes[0].ok is False
    assert log.read_text().strip() == "ran"
    record = read_hook_log(tmp_path / HOOK_LOG_NAME)[-2]
    assert record["status"] == "failed" and record["event"] == "run.end"


def test_a_hanging_hook_hits_the_timeout_and_is_killed(tmp_path):
    """A command that never returns is killed at `timeout_s`, recorded, and the run continues.

    This is the failure a naive implementation gets wrong by waiting forever: an unattended run with
    one bad hook would hang for the life of the process, which is worse than the hook never having
    been configured. The bound is what makes an inline dispatcher acceptable at all.
    """
    began = time.monotonic()
    runner = HookRunner(Section(HooksConfig(events={"run.end": "sleep 120"}, timeout_s=1)),
                        run_id="run_1", state_dir=tmp_path)

    outcomes = runner.dispatch(hook_event())

    elapsed = time.monotonic() - began
    assert [o.status for o in outcomes] == ["timeout"]
    assert elapsed < 15, f"a 1s timeout took {elapsed:.1f}s"
    assert outcomes[0].exit_code is None
    assert "timeout_s=1" in outcomes[0].reason


def test_a_hanging_hook_leaves_no_daemon_behind(tmp_path):
    """The kill takes the whole process group, so a timed-out hook is not still running afterwards.

    A shell command starts a shell; killing only the shell leaves the real command working against
    the repository it was pointed at, invisibly, for the rest of the machine's uptime. The sleep here
    is deliberately *shorter than the hook's own work*: if the group were not killed, the marker would
    appear within the grace period.
    """
    marker = tmp_path / "still-alive"
    runner = HookRunner(
        Section(HooksConfig(events={"run.end": f"sleep 1; touch {marker}"}, timeout_s=1)),
        run_id="run_1", state_dir=tmp_path)
    outcomes = runner.dispatch(hook_event())
    assert outcomes[0].status == "timeout"

    # Wait past the point where the *surviving* command would have written its marker.
    assert not wait_for(marker.exists, timeout_s=4.0), \
        "the descendant of a killed hook must not survive the timeout"


def test_a_missing_binary_is_recorded_and_does_not_break_the_run(tmp_path):
    """A command whose binary does not exist is a recorded failure, with the reason named.

    `exit 127` is technically accurate and practically useless. The reason says the binary does not
    exist, so the one-line fix is obvious from the log rather than from a debugging session.
    """
    runner = HookRunner(
        Section(HooksConfig(events={"run.end": "definitely-not-an-agentorg-binary --go"})),
        run_id="run_1", state_dir=tmp_path)

    outcomes = runner.dispatch(hook_event())

    assert [o.status for o in outcomes] == ["missing"]
    assert "does not exist" in outcomes[0].reason or "not on PATH" in outcomes[0].reason
    # And the process is still perfectly usable: the next event fires normally.
    assert runner.dispatch(hook_event("run.start")) == []


def test_a_hook_never_raises_out_of_dispatch(tmp_path):
    """Even a command the shell itself cannot start is an outcome, never an exception.

    The bus already disables a raising subscriber — but a disabled subscriber is a *silent* one, and
    a run with no hooks and no notification is exactly the failure this module exists to remove. So
    the failure has to come back as data instead.
    """
    runner = HookRunner(Section(HooksConfig(events={"run.end": "   "})), state_dir=tmp_path)
    assert runner.dispatch(hook_event()) == [], "a blank command is not a command"

    runner = HookRunner(Section(HooksConfig(events={"run.end": ["", "echo ok"]})), state_dir=tmp_path)
    outcomes = runner.dispatch(hook_event())
    assert [o.ok for o in outcomes] == [True], "the blank entry is refused, not run as an empty shell"
    assert any("not a shell command" in p for p in runner.problems())


def test_output_is_captured_capped_and_redacted(tmp_path):
    """A hook's output is kept, capped, and scrubbed of key-shaped text before it is logged.

    Capped because a hook that dumps a build log must not become the largest file in the workspace,
    and because a cap is what lets the reader bound memory. Redacted on the way into the log because
    stdout is arbitrary text — a log that once held a key is a leak regardless of later readers.
    """
    secret = "sk-abcdefghijklmnopqrstuvwx"
    runner = HookRunner(
        Section(HooksConfig(events={"run.end": f"printf '%s' '{secret}'; yes x | head -c 20000"})),
        run_id="run_1", state_dir=tmp_path)

    outcomes = runner.dispatch(hook_event())

    assert outcomes[0].ok
    assert "more characters not shown" in outcomes[0].stdout, "the cap must be visible, not silent"
    logged = json.dumps(read_hook_log(tmp_path / HOOK_LOG_NAME))
    assert secret not in logged, "a hook's stdout is redacted like every other string that leaves"
    assert "[REDACTED]" in logged


# ── enabled gates everything ─────────────────────────────────────────────────


def test_hooks_disabled_means_nothing_runs(tmp_path):
    """`hooks.enabled = false` runs no command at all — not even the wildcard."""
    log = tmp_path / "never.txt"
    runner = HookRunner(Section(HooksConfig(enabled=False, events={"*": f"echo x >> {log}"})),
                        state_dir=tmp_path)

    assert runner.dispatch(hook_event()) == []
    assert runner.commands_for("run.end") == []
    assert not log.exists()


def test_the_environment_kill_switch_wins_over_the_config(tmp_path, monkeypatch):
    """`$AGENTORG_NO_HOOKS` disables a run whose config says the hooks are on.

    A configured hook is an arbitrary shell command in a file, and the person running the engine is
    not always the person who owns the repository's `credentials.json`. Without a per-process switch
    the only way to run the suite, or a diagnostic, in someone else's checkout would be to edit their
    config.
    """
    log = tmp_path / "never.txt"
    monkeypatch.setenv(DISABLED_ENV, "1")
    runner = HookRunner(Section(HooksConfig(events={"*": f"echo x >> {log}"})), state_dir=tmp_path)

    assert runner.enabled is False
    assert runner.dispatch(hook_event()) == []
    assert not log.exists()


# ── the notifier ─────────────────────────────────────────────────────────────


def test_the_notifier_fires_only_for_the_configured_list(tmp_path):
    """The `on` list is the whole policy, and an unlisted event notifies for nothing.

    Asserted as a *negative* because that is the property that matters: the default list is five
    events precisely so the channel stays worth reading, and a notifier that leaked every event into
    it would train the person to ignore it — worse than having no channel.
    """
    log = tmp_path / "notified.txt"
    notifier = Notifier(
        Section(notify=NotifyConfig(on=["run.end", "goal.blocked"],
                                    command=f"echo $AGENTORG_EVENT >> {log}")),
        run_id="run_1", state_dir=tmp_path)

    assert notifier.dispatch(hook_event("node.enter")) == []
    assert notifier.dispatch(hook_event("llm.response")) == []
    assert notifier.wanted("goal.blocked") is True
    assert notifier.wanted("goal.progress") is False

    outcomes = notifier.dispatch(hook_event("goal.blocked"))
    assert [o.ok for o in outcomes] == [True]
    assert log.read_text().split() == ["goal.blocked"]


def test_the_command_channel_and_the_url_channel_each_deliver(tmp_path):
    """Both channels work: a shell command, and a POST of the event JSON to a URL.

    The URL channel is exercised against a server started inside the test, so the assertion is about
    our own serialisation rather than about the internet being reachable — a test that depends on a
    remote endpoint is a test that fails on a plane.
    """
    received: list[dict] = []
    server = _Recorder(received)

    log = tmp_path / "cmd.txt"
    notifier = Notifier(
        Section(notify=NotifyConfig(on=["run.end"],
                                    command=f"echo $AGENTORG_EVENT >> {log}",
                                    url=server.url)),
        run_id="run_1", slug="demo", state_dir=tmp_path)
    try:
        outcomes = notifier.dispatch(hook_event("run.end", outcome="done"))
    finally:
        server.stop()

    assert [o.channel for o in outcomes] == ["command", "url"]
    assert all(o.ok for o in outcomes), [o.detail for o in outcomes]
    assert log.read_text().strip() == "run.end"
    assert received and received[0]["type"] == "run.end"
    assert received[0]["payload"]["outcome"] == "done"
    assert received[0]["run_id"] == "run_1"


def test_a_failed_notification_is_swallowed_and_recorded(tmp_path):
    """A dead endpoint and a dying command are both recorded, and neither raises.

    The notifier's whole job is to reach a person in a case where nobody is watching, so it can
    hardly be allowed to be the thing that stops the run it was reporting on.
    """
    log = tmp_path / "cmd.txt"
    notifier = Notifier(
        Section(notify=NotifyConfig(on=["run.end"], command=f"echo $AGENTORG_EVENT >> {log}",
                                    url="http://127.0.0.1:9/nothing-here", timeout_s=2)),
        state_dir=tmp_path)

    outcomes = notifier.dispatch(hook_event())

    assert [o.channel for o in outcomes] == ["command", "url"]
    assert outcomes[0].ok is True, "one channel failing must not skip the other"
    assert outcomes[1].ok is False
    assert outcomes[1].detail
    assert [r["channel"] for r in read_hook_log(tmp_path / HOOK_LOG_NAME)] == ["command", "url"]


def test_the_notifier_refuses_a_command_that_cannot_be_started(tmp_path):
    """A notification command that does not exist is an outcome, not an exception."""
    notifier = Notifier(
        Section(notify=NotifyConfig(on=["run.end"], command="definitely-not-a-binary")),
        state_dir=tmp_path)

    outcomes = notifier.dispatch(hook_event())

    assert [o.ok for o in outcomes] == [False]
    assert outcomes[0].status == "missing"
    assert "PATH" in outcomes[0].detail


def test_notify_disabled_means_no_channel_fires(tmp_path):
    """`notify.enabled = false` turns off both channels, and `channels()` says so plainly."""
    log = tmp_path / "never.txt"
    notifier = Notifier(
        Section(notify=NotifyConfig(enabled=False, on=["run.end"],
                                    command=f"echo x >> {log}",
                                    url="http://127.0.0.1:9/nothing-here")),
        state_dir=tmp_path)

    assert notifier.enabled is False
    assert notifier.channels() == []
    assert notifier.wanted("run.end") is False
    assert notifier.dispatch(hook_event()) == []
    assert not log.exists()


# ── the bus integration ──────────────────────────────────────────────────────


def test_the_bus_fires_hooks_and_notifications_for_every_event(tmp_path):
    """One subscriber on the bus covers every event type, and the trace is unaffected.

    This is the integration the whole feature rests on: a hook wired at the bus cannot be forgotten by
    a new call site, because there are no call sites. `lifecycle=` is opt-in, so a bus built without
    it — every existing caller — pays nothing.
    """
    hook_log = tmp_path / "hooks.txt"
    notify_log = tmp_path / "notify.txt"
    section = Section(hooks=HooksConfig(events={"*": f"echo $AGENTORG_EVENT >> {hook_log}"}),
                      notify=NotifyConfig(on=["run.end"],
                                          command=f"echo $AGENTORG_EVENT >> {notify_log}"))
    bus = EventBus(run_id="run_1", trace_path=tmp_path / "trace.jsonl",
                   lifecycle=section, lifecycle_slug="demo")
    try:
        assert bus.lifecycle is not None
        assert bus.lifecycle.configured() == ["hooks", "notify"]
        assert bus.emit(EventType.NODE_ENTER, payload={"node_id": "fixer"}).seq == 1
        assert bus.emit(EventType.RUN_END, payload={"slug": "demo", "outcome": "done"}).seq == 2
        assert bus.lifecycle.runner.outcomes()[0].event == "node.enter"
    finally:
        bus.lifecycle.close()
        bus.close()

    assert hook_log.read_text().split() == ["node.enter", "run.end"]
    assert notify_log.read_text().split() == ["run.end"], "only the configured event notifies"


def test_the_bus_log_path_follows_a_hand_carried_workspace(tmp_path):
    """The hook log lands in the state directory the bus already resolved from its trace.

    The bus is the only object in that chain that knows where `.agent_state/` is, so deriving the log
    from the trace path is what keeps hooks and events recordable in the same directory. A log written
    anywhere else is one nobody looks for.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    state_dir = tmp_path / "project" / ".agent_state"
    bus = EventBus(run_id="run_1", trace_path=state_dir / "trace.jsonl",
                   lifecycle=Section(hooks=HooksConfig(events={"*": "true"})))
    try:
        assert bus.lifecycle is not None
        assert bus.lifecycle.log_path() == state_dir / HOOK_LOG_NAME
    finally:
        if bus.lifecycle is not None:
            bus.lifecycle.close()
        bus.close()
    assert not (elsewhere / HOOK_LOG_NAME).exists()


def test_a_config_with_no_lifecycle_installs_no_subscriber():
    """An empty config costs an event nothing: no subscriber is attached at all.

    A subscriber that does nothing is one more thing between an event and its handler, and this is
    the case for every existing run — the default `hooks.events` is empty and `notify` has no channel.
    """
    bus = EventBus(run_id="run_1", lifecycle=Section(hooks=HooksConfig(), notify=NotifyConfig()))
    try:
        assert bus.lifecycle is None
        assert bus.emit(EventType.RUN_END, payload={}).seq == 1
    finally:
        bus.close()


def test_a_broken_hooks_block_does_not_take_the_notifier_with_it(tmp_path):
    """The two sections are attached independently, because one failing must not silence the other.

    The notification is the more valuable half for a run nobody is watching. Refusing to notify
    because a *hook* was malformed is exactly how an unattended run stops without telling anyone —
    which is the failure this whole module exists to remove.
    """
    notify_log = tmp_path / "notify.txt"
    lifecycle = Lifecycle.attach(
        EventBus(run_id="run_1", trace_path=tmp_path / "trace.jsonl"),
        Section(hooks=None, notify=NotifyConfig(on=["run.end"],
                                                command=f"echo $AGENTORG_EVENT >> {notify_log}")),
        run_id="run_1", state_dir=tmp_path)
    try:
        assert lifecycle is not None
        assert lifecycle.configured() == ["notify"]
        lifecycle.dispatch(hook_event())
        assert notify_log.read_text().split() == ["run.end"]
    finally:
        if lifecycle is not None:
            lifecycle.close()


def test_attach_refuses_when_the_config_is_absent():
    """A config-less caller gets a refusal naming what to pass, not an object that does nothing.

    This is the module's own rule applied to itself: a runner with nothing to run reads as working.
    """
    from engine.hooks import HooksError

    with pytest.raises(HooksError, match="hooks"):
        HookRunner(None)
    with pytest.raises(HooksError, match="notify"):
        Notifier(None)


def test_the_shipped_config_loads_the_default_notify_list():
    """The real config path reaches the two sections, with the documented default `on` list.

    The module reads `config.hooks` and `config.notify`, and those exist only because the loader built
    them. Asserting the default list here is what catches a change to it made in `config.py` that the
    engine's own documentation does not reflect.
    """
    config = load()
    assert config.hooks.enabled is True
    assert config.hooks.timeout_s >= 1
    assert config.notify.on == ["run.end", "goal.completed", "goal.blocked", "goal.paused",
                                "human.gate"]
    assert config.notify.command == "" and config.notify.url == ""
    # A config-driven runner is the real construction path, not only the injected section.
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="agentorg-hooks-"))
    runner = HookRunner(config, run_id="run_1", state_dir=scratch)
    assert runner.enabled and runner.commands_for("run.end") == []
    runner.close()


class _Recorder:
    """A one-request JSON endpoint on a loopback port the OS picks.

    Bound to port 0 rather than a fixed port, so two test runs at once cannot collide, and no test
    can depend on the internet being reachable.
    """

    def __init__(self, received: list[dict]) -> None:
        self.received = received

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8")
                try:
                    received.append(json.loads(body))
                except json.JSONDecodeError:
                    # Recorded as an unparsable body rather than crashing inside the handler, where
                    # the failure would surface as a connection error and hide the real reason.
                    received.append({"unparsable": body[:200]})
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                """Silence the handler's stderr chatter; the assertions are the record."""

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/event"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Shut the server down. The daemon thread exits with the process either way."""
        self._server.shutdown()
        self._server.server_close()
