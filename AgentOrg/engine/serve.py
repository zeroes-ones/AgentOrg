#!/usr/bin/env python3
"""serve.py — the long-running NDJSON server the native console drives.

WHY THIS EXISTS
---------------
The macOS app has always launched `engine.cli serve`. That command did not exist. So the console —
every tab, every panel — could never start the engine it was built to watch: the app would spawn a
process, the process would exit with a usage error, and the UI would sit at "idle" forever.

This module is the missing half. It is a **line-protocol server**: the app writes one JSON command per
line on stdin, the engine writes one JSON event per line on stdout, and the two never interleave
because diagnostics go to stderr.

DESIGN
------
- **stdout is the protocol, stderr is for humans.** A stray `print` on stdout corrupts the stream the
  app parses, so every diagnostic here goes to stderr. That rule is why the app can read stdout raw.
- **`stdin` owns the control channel, commands run on a worker, and a run gets a thread of its own.**
  A run takes minutes; if anything on the command path blocked while executing, the app's `/pause`
  would queue behind the very work it is trying to pause. Taking the run off the *read* loop was not
  enough: the worker is what answers `status` and `pause`/`abort`, so a run executing inline on it froze
  the panels and made Pause a no-op until the run had already ended. So the worker dispatches, and
  `start` executes its graph on a separate thread.
- **Every command is acknowledged, always.** The app's `send` awaits a `command.ack` correlated by
  `cmd_id` and times out otherwise. An unacknowledged command is indistinguishable from a lost one, so
  the ack is sent for failures too — with the reason. That holds when the engine is stopping, when it
  is behind, and when the output has died: a command that will not run is *refused*, by name, rather
  than left in a queue nobody will empty.
- **The event stream is the run's own bus.** Rather than inventing a second protocol, the run's
  `EventBus` is tapped and forwarded, so the UI sees exactly the events the engine already emits and
  the two cannot drift.
- **EOF is a clean shutdown, and so is a stop signal.** The app closes stdin when it stops, and signals
  SIGTERM moments later; both must end this process through the same path — drain the commands already
  accepted, refuse what will not run — rather than killing it where it stands. A drain is therefore
  *bounded* (see `_DRAIN_TIMEOUT_S`) and its expiry is spoken, not silent.
- **The transport never raises into the work.** `emit` is on the ack path, so a write that fails marks
  the output dead and stops the server deliberately: an engine that cannot write cannot answer, and one
  that keeps reading commands anyway is worse than one that stops and says why.

Usage:
    python3 -m engine.cli serve
    python3 -m engine.cli serve --slug demo --root projects
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .protocol import Ack, Command, Event, EventType

__all__ = ["Server", "ServerError", "serve"]


class ServerError(RuntimeError):
    """A server problem worth naming rather than surfacing as a traceback on stdout."""


#: Returned by a handler that has taken over its own acknowledgement for a command, so the command
#: worker must not send one. `start` does this, and so does `approve_plan`: both hand the graph to a run
#: thread (see `_start_run`) and the ack is that run's outcome, sent when the graph settles.
_ACK_DEFERRED = object()


class _StopRequested(Exception):
    """Raised by the SIGTERM handler to break a read that is parked on stdin.

    A stop *flag* cannot do it. PEP 475 retries an interrupted syscall once a signal handler returns
    without raising, so a loop blocked in `for line in self.stdin` would sit there with the flag set —
    an engine that ignores SIGTERM while believing it is stopping, which is worse than the default
    action it replaced. This is the same mechanism Ctrl-C has always used here; it is caught at the
    read loop so the ordinary drain and shutdown still run.
    """

#: How long a graceful stop waits for a command the worker has **already started**.
#:
#: This is the budget for a worker command to reach its ack, and it is chosen against a measured
#: failure rather than a feeling: with 10s here plus `_shutdown`'s 2s join, a handler that ran longer
#: than ~12s was killed mid-command and its ack never went out — the work done and the answer lost,
#: which is the worst shape of bug in this module because the app cannot tell it from a lost command.
#: The commands that run on the worker are bounded work (a status snapshot, a roster edit, a goal
#: arm), and the slowest of them measured cold is a few seconds; 60s is an order of magnitude of
#: headroom, and it stays bounded so a wedged handler cannot hold the engine open forever — at which
#: point `_shutdown` *says* which commands it is refusing instead of exiting on top of them.
_DRAIN_TIMEOUT_S = 60.0

#: The same wait once a stop **signal** has arrived. The app signals SIGTERM and then SIGKILLs this
#: process after its own 5s grace (`AgentProcessService.terminationGrace`), so a drain longer than
#: that is a drain that gets killed halfway — the exit must fit inside the grace to be orderly at all.
#: 4s leaves the kill a second of margin.
_SIGNAL_DRAIN_S = 4.0

#: How long `_stop_now` lets the main thread exit through its own orderly path before force-exiting.
#: Force-exiting is the last resort it always was, but "last" now means "after the process had its
#: chance", not "immediately, possibly mid-write".
_STOP_NOW_GRACE_S = 3.0

#: How long `_shutdown` waits for the worker to notice the stop sentinel. The drain has already
#: waited for the work, so this is a formality — the worker is idle-blocked and wakes in ≤0.2s.
_SHUTDOWN_JOIN_S = 2.0

#: How long `_shutdown` gives a still-live run's runner to exit after SIGTERM before it is SIGKILLed.
#:
#: Not `host.shutdown_runners`' 2s default: on a stop *signal* the drain has already been capped at
#: `_SIGNAL_DRAIN_S` (4s) of the app's 5s SIGKILL grace (`AgentProcessService.terminationGrace`), so
#: the runner's own grace has to fit in what is left — 0.5s leaves the host's 1s post-kill `wait()` a
#: margin inside that second, while still staging SIGTERM before SIGKILL rather than killing at once.
_RUNNER_SHUTDOWN_GRACE_S = 0.5

#: The most commands that may wait for the worker before the engine refuses new ones.
#:
#: The queue was unbounded, so an app sending faster than the worker drains grew it without limit —
#: an engine that looks healthy and answers later and later, holding memory for commands whose answers
#: nobody is waiting for any more. The bound is deliberately loose: the app awaits each command's ack
#: (`send` is an `async` round trip), so a well-behaved client has a handful outstanding at most, and
#: the queue only legitimately holds a burst the worker has not reached yet. Exceeding 512 therefore
#: means a client that is not waiting for its answers, and the honest response is a refusal naming the
#: backlog rather than a promise to catch up.
_COMMAND_QUEUE_MAX = 512


def _log(message: str) -> None:
    """Diagnostics go to stderr. stdout belongs to the protocol and a stray line corrupts it.

    Never raises. stderr is a pipe to the app, and once the app has exited that pipe is broken — so a
    logging call would raise `BrokenPipeError` at the worst possible moment, which is precisely when we
    are trying to report that the app is gone. A diagnostic that can break the code path it is
    describing is worse than no diagnostic.
    """
    try:
        print(message, file=sys.stderr, flush=True)
    except (BrokenPipeError, ValueError, OSError):
        pass


def _directory_bytes(path: Any) -> int:
    """Total bytes under a directory, for the "what would I lose" preview.

    Every `stat` is guarded: this walks a folder the person may be actively running in, so a file can
    vanish between the walk listing it and the size being read. An unreadable file contributes nothing
    rather than aborting the count, because a preview that raised would leave the confirmation with no
    number at all — and "I could not measure it" is a better answer than "remove anyway".
    """
    from pathlib import Path

    total = 0
    try:
        for entry in Path(path).rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


@dataclass
class Server:
    """The NDJSON command loop.

    Parameters
    ----------
    config:
        Validated configuration.
    library:
        The pinned library handle.
    workspace:
        The project the console is pointed at.
    stdin / stdout:
        Injectable streams, so the whole loop is testable without a terminal or a subprocess.
    """

    config: Any
    library: Any
    workspace: Any
    stdin: Any = None
    stdout: Any = None
    #: Set by `start`/`resume`: the orchestrator the run commands act on.
    orchestrator: Any = None
    slug: str = "console"
    #: The portfolio the console is showing, when there is one. Loaded lazily so a single-org setup
    #: pays nothing for the multi-org layer.
    _portfolio: Any = None
    _portfolio_loaded: bool = False
    #: The register file's `(size, mtime_ns)` when it was last read, so a rewrite by another writer —
    #: the CLI, another console — is noticed rather than served from a copy that has gone stale.
    #: See `_load_portfolio`.
    _portfolio_stamp: tuple[int, int] | None = None
    #: The fleet, built on first use, so several orgs can run at once from one server.
    _fleet: Any = None

    def __post_init__(self) -> None:
        self.stdin = self.stdin if self.stdin is not None else sys.stdin
        self.stdout = self.stdout if self.stdout is not None else sys.stdout
        self._seq = 0
        #: Guards the *transport*: `emit`'s seq/write pair. It is an RLock because `emit` can be
        #: re-entered (`ack` calls it) — and, critically, it is held across a **blocking** write, so
        #: nothing on the shutdown path may take it. The in-flight counter has its own lock for exactly
        #: that reason (see `_flight_lock`).
        self._lock = threading.RLock()
        self._commands: "queue.Queue[Command | None]" = queue.Queue(maxsize=_COMMAND_QUEUE_MAX)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        #: The worker's own lock: taken only to move `_in_flight`, never held across I/O. A counter
        #: shared with `emit` was the defect — `_drain` read it in another thread and could be blocked
        #: behind a write to a pipe the app had stopped reading, which delayed exactly the decision it
        #: was making.
        self._flight_lock = threading.Lock()
        #: Woken when a command is queued, so the worker picks it up without polling for it.
        self._work_ready = threading.Event()
        #: Set when a write to stdout fails. The app's read end is gone: answers now have nowhere to
        #: go, so the engine stops itself deliberately instead of raising through an ack.
        self._output_dead = threading.Event()
        #: Why the engine is stopping, in the client's words. Set by `shutdown`, a stop signal, a dead
        #: output, or a drain that gave up; used to word the refusals for commands that will not run.
        self._stop_reason = ""
        #: The command the worker is executing right now, so `_shutdown` can answer it if the engine
        #: exits on top of it rather than leaving the app waiting on a reply that will never come.
        self._current_cmd: Command | None = None
        #: A ceiling on the current drain, set by a stop signal that has a shorter grace than the drain
        #: (see `_SIGNAL_DRAIN_S`). Read on every pass, so a signal arriving mid-drain still counts.
        self._drain_cap: float | None = None
        #: The signal handlers this server displaced, so they can be put back when it stops. A caller
        #: that keeps running after `serve_forever` must not inherit our handlers.
        self._previous_signals: dict[int, Any] = {}
        #: True while `serve_forever` is in its read phase — from the readiness handshake to the end of
        #: the loop. A stop signal raises only while it is set, so the raise lands in the loop rather
        #: than in the shutdown that follows it (see `_on_stop_signal`). Only the main thread writes it.
        self._reading = False
        #: Fallback ids for commands the app sent without one. `itertools.count` rather than the clock:
        #: a millisecond timestamp collided for two untagged commands in the same millisecond, and two
        #: commands sharing a `cmd_id` are indistinguishable to the client that correlates on it.
        self._cmd_ids = itertools.count(1)
        #: Why the portfolio could not be read, when it could not. Empty means no failure — either the
        #: register was read or there genuinely is none. Kept because "I could not read your register"
        #: and "you have no register" must not lead to the same action (see `_cmd_portfolio_add`).
        self._portfolio_error = ""
        self._forwarded: set[int] = set()
        #: The thread executing a graph, when one is live. Read by `_cmd_start` to refuse a second run
        #: and by nothing else — the orchestrator's own `running` flag is set *after* `execute` starts,
        #: so it cannot answer "is a graph executing right now" in the window between the two.
        self._run_thread: threading.Thread | None = None
        #: Commands the worker has taken off the queue and not finished. `_drain` waits on this
        #: together with the queue, because an in-flight command has already left the queue and would
        #: otherwise be mistaken for nothing to wait for.
        #:
        #: It counts only what the *worker* is running — a graph handed to its own thread is not
        #: counted here. It used to be, so that a stop during a run waited for the run exactly as the
        #: worker's own commands do; but a run takes minutes and `_drain` is bounded, so that wait could
        #: only ever time out (delaying the exit by its full length while the run was killed anyway).
        #: The run's ack belongs to the run, and `_shutdown` says so plainly when one is still live.
        self._in_flight: int = 0
        #: The shared discovery catalog. Built lazily on first use and dropped by `_reload_config`,
        #: so its TTL cache survives across polls instead of being thrown away each time.
        self._discovery: Any = None

    # ── the transport ───────────────────────────────────────────────────────

    def emit(self, event: Event | dict[str, Any]) -> None:
        """Write one event as a single line. **Never raises.**

        Serialised under a lock because two threads emit: the worker handling a command and the run's
        own bus forwarding events. Interleaved writes would produce a line the app cannot parse.

        The write itself is guarded for the same reason `_log` guards stderr, and the reason matters
        more here: `emit` is on the **ack path**. When the app's read end closes — it is gone, or it
        stopped reading — `stdout.write` raises `BrokenPipeError`, and an `emit` that propagated it
        killed the worker from inside an acknowledgement: the first ack raised, the `except` in `_work`
        tried to ack the *failure* (raising again), and the thread died with the engine still reading
        commands and exit code 0. Measured before this guard: three queued commands, zero acks, worker
        dead, queue still holding all three, and the app waiting 600s per command against an engine
        that looked healthy. So a write failure is not raised to the caller: it marks the output dead
        and stops the server deliberately, which is a stop the engine can describe.
        """
        with self._lock:
            self._seq += 1
            if isinstance(event, Event):
                payload = event.to_dict()
                payload.setdefault("seq", self._seq)
                if not payload.get("seq"):
                    payload["seq"] = self._seq
            else:
                payload = dict(event)
                payload.setdefault("seq", self._seq)
            payload.setdefault("ts", _now())
            try:
                line = json.dumps(payload, default=str)
            except (TypeError, ValueError) as exc:
                _log(f"cannot serialise an event: {exc}")
                return
            try:
                self.stdout.write(line + "\n")
                self.stdout.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                # Nothing on this path may raise: the caller may be an ack, a run's event forward, or
                # a refusal being written during shutdown.
                self._output_failed(exc)

    def _output_failed(self, exc: BaseException) -> None:
        """The app's read end is gone: record it once, and stop the server on purpose.

        Stopping rather than continuing is the honest reading of a failed write. stdout *is* the
        protocol — every event, including every acknowledgement, goes through it — so an engine that
        cannot write cannot answer anything, and a command it still "accepts" is a command whose
        answer will never arrive. Continuing would leave the app waiting on replies from an engine
        that looks alive; this way the read loop unwinds, `_drain` refuses what will not run, and the
        process exits with a log line naming the reason.

        Idempotent: the writes that follow (the refusals themselves) fail too, and each must be a
        no-op rather than another stop.
        """
        if self._output_dead.is_set():
            return
        self._output_dead.set()
        self._stop.set()
        self._stop_reason = "the app stopped reading the engine's output"
        _log(f"serve: cannot write an event ({exc}); stopping after the current command")
        try:
            self._commands.put_nowait(None)
        except queue.Full:
            pass
        self._work_ready.set()
        if self.stdout is sys.stdout:
            # Point the interpreter's own stdout somewhere harmless. CPython flushes `sys.stdout` as it
            # exits, and *that* flush fails too against the same dead pipe — measured: a deliberate stop
            # left "Exception ignored while flushing sys.stdout: BrokenPipeError" on stderr and exit code
            # **120** instead of 0. Both are what a crash looks like: the app shows engine stderr in its
            # diagnostics and reports a non-zero exit as a failure, so a stop the engine chose would
            # read as a crash it did not. Only when this really is the process's own stdout — an injected
            # stream (a test, a caller) is not the interpreter's and is left exactly as it was.
            try:
                sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - the process is about to exit
            except OSError:  # pragma: no cover - devnull unavailable is not worth failing a stop over
                pass

    def ack(self, cmd_id: str, *, ok: bool, detail: dict[str, Any] | None = None,
            error: str | None = None) -> None:
        """Acknowledge a command. Sent for failures too, because a silent command looks lost."""
        event = Ack(cmd_id=cmd_id, ok=ok, error=error, detail=detail or {}).to_event(0)
        self.emit(event)

    # ── the loop ────────────────────────────────────────────────────────────

    def serve_forever(self) -> int:
        """Read commands until EOF, executing each off the read loop. Returns an exit code.

        Every way this ends — EOF, a stop signal, a `shutdown` command, an output that died — goes
        through the same `_drain` and `_shutdown`, so what the app has already sent is either run and
        acknowledged, or refused with a reason. None of them is a silent exit.
        """
        _log(f"agentorg serve: project={self.workspace.path} pid={_pid()}")
        # Signals first, so a stop that arrives during bootstrap is not the default action. See
        # `_install_signal_handlers`: without this the app's Stop killed the process in milliseconds and
        # the drain below never ran at all.
        self._install_signal_handlers()
        # Everything from here to the end of the read loop runs with `_reading` set, so a stop signal
        # that arrives anywhere in this window breaks the read (see `_on_stop_signal`). It has to cover
        # the handshake too, not just the loop: a signal during bootstrap would otherwise set a flag
        # nothing looks at, and this process would sit in a read the flag cannot interrupt.
        self._reading = True
        try:
            # The readiness handshake, first on the wire. The console cannot otherwise tell a live engine
            # from a spawned-but-doomed one: a bootstrap failure is a process that exists for a moment and
            # exits, and the app used to call that "running". This frame is emitted *after* the workspace,
            # config and providers are all resolved, so receiving it means the engine really is usable —
            # and anything that stops it before here is reported by the app as a failure, not a success.
            self.emit(Event(seq=0, type=EventType.ENGINE_READY,
                            payload={"pid": _pid(), "project": str(self.workspace.path),
                                     "slug": self.slug,
                                     "providers": sorted(getattr(self.config, "providers", {}) or {})}))
            self.emit(Event(seq=0, type=EventType.AGENT_LOG,
                            payload={"text": "engine ready", "stream": "stderr"}))
            self._worker = threading.Thread(target=self._work, name="agentorg-serve", daemon=True)
            self._worker.start()
            self._watch_parent()
            for line in self.stdin:
                line = line.strip()
                if not line:
                    continue
                command = self._parse(line)
                if command is None:
                    continue
                if self._stop.is_set():
                    # Read *after* the engine was told to stop: the worker will not take it — its loop
                    # has already ended — so it is refused here, with the same reason the queue gets.
                    # Breaking without this answered a command the app had already sent with silence.
                    self._refuse(command, self._stop_reason or "the engine is stopping")
                    break
                try:
                    self._commands.put_nowait(command)
                except queue.Full:
                    # Refused rather than queued: a queue that grows without limit is an engine that
                    # answers later and later while looking healthy, and the app would wait on an
                    # answer it cannot date. Saying "I am behind, and by this much" is an answer.
                    self._refuse(command,
                                 f"the engine is behind: {self._commands.maxsize} commands are "
                                 "already queued and have not run")
                    continue
                self._work_ready.set()
        except (KeyboardInterrupt, BrokenPipeError, _StopRequested):
            pass
        except OSError as exc:
            _log(f"serve: stdin failed: {exc}")
        finally:
            self._reading = False
            try:
                # Drain the queue before stopping. EOF arrives as soon as the writer closes stdin, which
                # can be well before the worker has executed what was already read — stopping first would
                # drop those commands' acks and leave the app waiting on a reply that never comes.
                self._drain()
                self._shutdown()
            except _StopRequested:
                # A further stop signal arrived *while* the engine was stopping. There is nothing left to
                # break: the shutdown in progress is already the orderly path, and its refusals are what
                # the app is owed. Swallowed so a second SIGTERM cannot turn a clean stop into a
                # traceback.
                _log("serve: a further stop signal arrived while the engine was already stopping")
            finally:
                self._restore_signal_handlers()
        return 0

    def _install_signal_handlers(self) -> None:
        """Handle SIGTERM and SIGINT, so both unwind through `_drain`/`_shutdown`.

        **SIGTERM was the app's Stop path, and it never reached the drain.** The app closes the command
        pipe and then signals (`AgentProcessService.terminate`, which says in its own comment that the
        engine "finishes the command in flight, writes its checkpoint, and exits"); Python's default
        SIGTERM action kills the process in milliseconds, so the drain that *would* have run on the EOF
        was cut off, and no acknowledgement ever went out. That comment described behaviour this file
        did not have. A handler that sets the stop flag — and, when the read is parked, breaks it — is
        what makes it true.

        **SIGINT keeps raising `KeyboardInterrupt`**, because a flag alone cannot unwind a read loop
        parked on a terminal: PEP 475 retries an interrupted syscall once the handler returns, so the
        loop would sit there with the stop flag set until the next line arrived. The exception is
        already the loop's own exit path (it is caught at the `try` above), so Ctrl-C behaves exactly
        as it did, with the stop flag now set as well.

        Installing is skipped outside the main thread — `signal.signal` refuses there, and more to the
        point, a test or a library driving this server in a thread must not have its own signals
        rewritten.
        """
        if threading.current_thread() is not threading.main_thread():
            return
        for signum, handler in ((signal.SIGTERM, self._on_stop_signal),
                                (signal.SIGINT, self._on_interrupt_signal)):
            try:
                self._previous_signals[signum] = signal.signal(signum, handler)
            except (ValueError, OSError, AttributeError) as exc:  # pragma: no cover - platform limit
                _log(f"serve: could not handle signal {signum}: {exc}")

    def _restore_signal_handlers(self) -> None:
        """Put back what we replaced, so a caller that keeps running after `serve_forever` — the CLI,
        or a test that drives the loop and then asserts on its own signals — is not left with ours."""
        if threading.current_thread() is not threading.main_thread():
            return
        for signum, previous in self._previous_signals.items():
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError, TypeError):  # pragma: no cover - platform limit
                pass
        self._previous_signals.clear()

    def _on_stop_signal(self, signum: int, frame: Any) -> None:
        """SIGTERM: stop deliberately, in time to exit before the app's SIGKILL.

        The signal names the reason and caps the drain, because this process is on a clock: the app
        sends SIGKILL 5s after signalling, so waiting the full drain here would only be cut off
        mid-ack — the exit has to fit inside the grace to be orderly at all.

        It raises, rather than only setting the flag, so a read parked on stdin actually ends. The
        app's own path closes the pipe *before* signalling, so normally EOF has already unwound the
        loop and the raise never happens; a `kill` on a terminal-launched engine has no EOF to rely on,
        and there the raise is the difference between stopping and appearing to ignore the signal. Only
        while `_reading` is set: an exception raised out of the shutdown that a *previous* signal
        started would abandon the refusals the app is owed, and `serve_forever` has already put the
        stop flag where that shutdown will see it.
        """
        self._stop_reason = self._stop_reason or f"the app asked the engine to stop (signal {signum})"
        self._stop.set()
        self._drain_cap = _SIGNAL_DRAIN_S
        self._work_ready.set()
        if self._reading:
            raise _StopRequested

    def _on_interrupt_signal(self, signum: int, frame: Any) -> None:
        """SIGINT: mark the stop, then raise, which is how the loop has always unwound on Ctrl-C."""
        self._stop_reason = self._stop_reason or "the engine was interrupted"
        self._stop.set()
        self._drain_cap = _SIGNAL_DRAIN_S
        self._work_ready.set()
        raise KeyboardInterrupt

    def _watch_parent(self) -> None:
        """Exit if the app that started us goes away, whatever the pipe says.

        Why the pipe is not enough. The app holds the *write* end of this process's stdin, so a normal
        quit closes it and the read loop ends. But if the app is killed outright — SIGKILL, a crash, a
        force-quit — the write end can still be open in some other process that inherited it (a helper,
        a backgrounded child), so EOF never arrives and this process would become an orphan holding the
        project. Observed in practice: SIGTERM to the app left the engine running indefinitely, and the
        next app instance then started a *second* engine on the same checkpoint.

        **The expected parent is named, not sampled.** An earlier version recorded `os.getppid()` at
        startup and watched for it to change — which has a race: if the app dies during this process's
        own import, the parent is already gone and `getppid()` reads 1, so there is nothing to detect
        and the watchdog gave up. Instead the app passes its own pid in `$AGENTORG_PARENT_PID`, and this
        polls whether *that* pid is still alive. Naming it removes the race entirely.

        Falls back to change-detection when the variable is absent, which is the case for a CLI launch —
        where `getppid()` becoming 1 is legitimate (detached with `nohup`/`setsid`) and must not be
        treated as a reason to exit.

        **The verdict is three-way, and that is the fix on this side.** `_pid_alive` used to answer
        "alive" whenever it could not answer anything else, so a platform that could not describe the
        parent produced a check that could never fail: the poll ran for ever against a pid that no
        longer existed, and the engine stayed up holding the project — the failure this whole method
        exists to prevent. It now answers True/False/None, and `_parent_is_gone` turns all three into a
        verdict, using one fact the platform always supplies: whether `expected` is still *our* parent.
        """
        expected_raw = os.environ.get("AGENTORG_PARENT_PID", "").strip()
        expected = int(expected_raw) if expected_raw.isdigit() else 0

        if expected > 1:
            # Recorded once, because it is a fact about how this process started and cannot change:
            # we were spawned by `expected` exactly when it is our parent at this moment.
            started_as_child = os.getppid() == expected

            def _poll_expected() -> None:
                while not self._stop.wait(2.0):
                    if _parent_is_gone(expected, started_as_child=started_as_child):
                        # Stop FIRST, then log. The log goes to stderr, which is the dead app's pipe —
                        # writing to it raises `BrokenPipeError`, and anything after that line would
                        # never run. Ordering this the other way round made the watchdog silently
                        # useless for exactly the case it exists for: the app being gone.
                        self._stop_now()
                        return

            threading.Thread(target=_poll_expected, name="agentorg-parent-watch", daemon=True).start()
            return

        # No named parent: the only signal available is a change in our own parent.
        parent = os.getppid()
        if parent <= 1:
            # Started detached (a shell that exited, `nohup`, `setsid`). There is nothing to watch, and
            # exiting would be wrong — a deliberately detached engine is meant to keep running.
            return

        def _poll_ppid() -> None:
            while not self._stop.wait(2.0):
                if os.getppid() != parent:
                    self._stop_now()   # stop before logging, for the same reason as above
                    return

        threading.Thread(target=_poll_ppid, name="agentorg-parent-watch", daemon=True).start()

    def _stop_now(self) -> None:
        """Stop now, but give the main thread its own orderly exit before forcing one.

        An engine whose app is gone has no client, no acknowledgements to send, and a project it should
        release, so it exits rather than lingering — and `os._exit` is still how, because the read loop
        is parked on stdin, which may never see EOF: the very condition that brought us here.

        What changed is that `os._exit` is the **last resort** rather than the first move. Called
        straight away from the watchdog thread it killed the process mid-write, with no drain and no
        `_shutdown`, for no stated gain — the app is gone either way, but a half-written frame and a
        half-finished command are artifacts a debugging session has to explain. So: set the stop, wake
        whatever is blocked on the queue, close the read end (which is what unblocks the loop where the
        platform honours it), and let the main thread run its own drain and shutdown. Only if it is
        still going after `_STOP_NOW_GRACE_S` is the process taken down beneath it.
        """
        self._stop.set()
        self._stop_reason = self._stop_reason or "the app that started the engine is gone"
        try:
            self._commands.put_nowait(None)
        except queue.Full:
            pass
        self._work_ready.set()
        try:
            os.close(0)
        except OSError:
            pass
        main = threading.main_thread()
        if main is not threading.current_thread():
            main.join(_STOP_NOW_GRACE_S)
        # `os._exit` skips `atexit`, so the runner reap the host registers there never runs on this
        # path — and a runner that outlives the engine keeps executing and keeps spending against the
        # same checkpoint while the app shows the run as stopped. Local import to match this module's
        # convention (the protocol is the only module-level import it carries). The default grace is
        # used here: the app that started us is gone, so there is no SIGKILL grace to fit inside.
        #
        # Swallowed without logging, deliberately: stderr is the dead app's pipe and a write to it
        # raises `BrokenPipeError`, so a diagnostic here would stop `os._exit` from ever running —
        # turning a failed reap into a process that lingers, which is the exact failure this whole
        # path exists to prevent.
        try:
            from .host import shutdown_runners

            shutdown_runners()
        except Exception:  # noqa: BLE001 - a failed reap must not hold the force-exit
            pass
        os._exit(0)

    def _drain(self, timeout_s: float = _DRAIN_TIMEOUT_S) -> None:
        """Wait until the worker has *finished* what it was given, bounded so a wedged command cannot
        hang exit.

        Why the queue being empty is not the same as done: the worker takes a command **off** the queue
        and then runs it, so the queue reads empty while the command is still in flight. Waiting on
        `empty()` therefore returned immediately and `_shutdown` killed the worker mid-command — the
        ack never went out, and the app sat waiting for a reply to a command that had actually
        succeeded. That is the worst shape of bug here: the work is done and the answer is lost.

        So the worker reports what it is doing, and this waits on that — under its own lock, and with
        the pop and the count taken together (`_take_command`), because a read that lands between the
        pop and the increment reads "queue empty, nothing in flight" while a command has already left
        the queue and has not started. That window is one GIL slice wide, which is why it passed for so
        long and then failed under load — the three flaky tests in this area were not a slow handler,
        they were this: `_drain` returned at once, `_shutdown`'s 2s join expired, and the ack landed
        after the process had already returned.

        Two bounds, deliberately. The wait is `timeout_s` (60s, see `_DRAIN_TIMEOUT_S`) for a worker
        command, capped by `_drain_cap` when a stop signal has arrived with a shorter grace than that.
        Once the stop flag is set — a `shutdown` command, a signal, a dead output — the *queue* is not
        waited for at all: the worker ends after the command it is on, so nothing else in the queue can
        run, and the honest thing is to refuse those commands now rather than hold the exit open for
        work that will never happen.
        """
        entered = time.time()
        while True:
            cap = self._drain_cap
            bound = timeout_s if cap is None else min(timeout_s, cap)
            with self._flight_lock:
                in_flight = self._in_flight
            if in_flight == 0 and (self._stop.is_set() or self._commands.empty()):
                return
            if time.time() >= entered + bound:
                self._stop_reason = (f"the engine gave up waiting after {bound:.0f}s with a command "
                                     "still running")
                _log(f"serve: {self._stop_reason}")
                return
            time.sleep(0.02)

    def _take_command(self) -> tuple[Any, bool]:
        """Pop one command and take its in-flight token **together**. Returns `(command, taken)`.

        One lock hold for both, because they are one fact: "this command has left the queue" and "this
        command is running" must never be observable apart. Split across two statements — as they were —
        `_drain` in another thread could see the queue empty and the counter zero in between, conclude
        there was nothing to wait for, and let `_shutdown` kill the worker mid-command.

        `False` means the queue was empty: not the `None` sentinel, which is a command-shaped value
        meaning "stop" and does take a token (released immediately by the caller).
        """
        with self._flight_lock:
            try:
                command: Any = self._commands.get_nowait()
            except queue.Empty:
                return None, False
            self._in_flight += 1
            return command, True

    def _release_flight(self) -> None:
        """Give back one in-flight token. In `finally` blocks, so a crashing command cannot leave the
        counter stuck and turn every later bounded wait into a full-length stall."""
        with self._flight_lock:
            self._in_flight -= 1

    def _refuse(self, command: Command, reason: str, *, ran: bool = False) -> None:
        """Answer a command that will **not** be run, with the reason. Never raises, never silent.

        Every command is acknowledged — that is the contract at the top of this file — and it does not
        get weaker when the engine is stopping or behind. An unacknowledged command is
        indistinguishable from a lost one, so the app waits its full 600s budget on a reply that is
        never coming; a refusal with a reason at least says which engine state it hit.
        """
        error = (f"{reason}; this command was still running, so its result will not arrive"
                 if ran else f"{reason}; this command was queued but never ran")
        self.ack(command.cmd_id, ok=False, error=error)

    def _refuse_pending(self, reason: str) -> list[str]:
        """Answer everything left over: the queue, and the command the worker is still inside.

        Called once at shutdown, after the drain has either finished the work or given up on it. The
        worker's own ack is the one that *should* answer the in-flight command, so this only speaks for
        it when the worker is still inside it — `_current_cmd` is cleared in the worker's `finally`, so
        a command that completed, even one that failed, has already been answered and is not touched
        here. (A command that completes in the microseconds between that check and this ack would be
        answered twice; the second ack names a command the app has already resolved, which it ignores,
        and the alternative — never answering — is the failure this whole method exists to prevent.)
        """
        refused: list[str] = []
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            if command is None:
                continue
            refused.append(command.cmd_id)
            self._refuse(command, reason)
        current = self._current_cmd
        if current is not None:
            refused.append(current.cmd_id)
            self._refuse(current, reason, ran=True)
        if refused:
            _log(f"serve: refused {len(refused)} command(s) that would not run: {', '.join(refused)}")
        return refused

    def _parse(self, line: str) -> Command | None:
        """Parse one command line, reporting a malformed one without dying.

        A single unparsable line must not take the server down: the app would then lose its engine
        over one bad frame. It is logged, and the loop continues.
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            _log(f"serve: ignoring a malformed command line: {exc}")
            return None
        if not isinstance(data, dict) or not data.get("type"):
            _log("serve: ignoring a command with no type")
            return None
        return Command(
            # The fallback id is per-process unique. A millisecond timestamp was not: two untagged
            # commands in the same millisecond produced one `cmd_id`, and the app correlates its
            # answers on that id — two commands answering to one name is a reply the app attributes to
            # the wrong command. (The app always sends its own id; this is the path a hand-written
            # line or a third-party client takes.)
            cmd_id=str(data.get("cmd_id") or f"cmd_{_pid()}_{next(self._cmd_ids)}"),
            type=str(data["type"]),
            payload=data.get("payload") if isinstance(data.get("payload"), dict) else {},
        )

    def _work(self) -> None:
        """Dispatch queued commands, one at a time, off the read loop.

        **Dispatch, not execute.** A command whose work outlives the call — `start` — hands its graph to
        a thread of its own, because this worker is what answers the app's 2s `status` poll and, far more
        importantly, its `pause`/`abort`. Executing a run inline here queued all of those behind the very
        run they were meant to act on: the panels froze and Pause did nothing until the run had ended.

        The loop takes each command and its in-flight token in one hold of `_flight_lock`, and waits for
        the next one on an event rather than a blocking `get` — a blocking `get` cannot also take the
        token, and a token taken *before* the pop would be held while idle, which would make `_drain`
        wait for a worker that is doing nothing.
        """
        while not self._stop.is_set():
            command, taken = self._take_command()
            if not taken:
                self._work_ready.wait(0.2)
                self._work_ready.clear()
                continue
            if command is None:
                self._release_flight()
                return
            self._current_cmd = command
            try:
                detail = self.handle(command)
                if detail is not _ACK_DEFERRED:
                    self.ack(command.cmd_id, ok=True, detail=detail or {})
            except Exception as exc:  # noqa: BLE001 - one bad command must not kill the server
                _log(f"serve: command {command.type!r} failed: {exc}")
                self.ack(command.cmd_id, ok=False, error=str(exc))
            finally:
                self._current_cmd = None
                # Released for every command, `_ACK_DEFERRED` included: the token means "the worker is
                # on this command", and after a handoff it is not — the run owns the rest of the work
                # and answers on its own thread. Holding it here would leave the counter stuck at one
                # for the whole run, which is exactly the wait `_drain` must not make.
                self._release_flight()

    def _shutdown(self) -> None:
        """Stop the worker, answer anything that will not run, and say what was left.

        The refusals are the part that did not exist. A `shutdown` command — or any stop — ends the
        worker after the command it is on, so everything else in the queue used to sit there: measured,
        one ack for three commands, and an engine that exited 0 with the other two never answered. The
        app has no deadline for that; it waits 600s per command. So the queue is drained here by
        *answering* it, as a refusal naming the reason, and the in-flight command is named too when the
        drain gave up on it.
        """
        self._stop.set()
        try:
            self._commands.put_nowait(None)
        except queue.Full:
            pass
        self._work_ready.set()
        with self._flight_lock:
            busy = self._in_flight
        # Joined only when the worker is *between* commands. The join is what lets it notice the stop
        # sentinel and return; a worker still inside a command cannot be helped by waiting longer — the
        # drain already gave that command its full bound — and two more seconds here is two seconds
        # taken off the app's 5s grace before it SIGKILLs us, for nothing.
        if not busy and self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=_SHUTDOWN_JOIN_S)
        self._refuse_pending(self._stop_reason or "the engine is stopping")
        # The run thread is *not* joined: it is a daemon and a graph takes minutes. A stop during a run
        # therefore ends the run where it is — said out loud rather than silently, because a run that is
        # running when the process ends leaves a checkpoint that still says "running" and no verdict on
        # the wire. (Aborting it properly would mean `Orchestrator.abort` → `Host._terminate`, which
        # signals the runner and then *waits out its grace period* — `grace_s` defaults to 5s — and that
        # wait is on the shutdown path, inside the very grace the app allows before SIGKILL.)
        #
        # What is done instead is the half that cannot wait: the run's *runner subprocess* is signalled
        # and reaped, because a runner that outlives the engine keeps executing and keeps spending
        # against the same checkpoint while the app shows the run as stopped. That trade used to be
        # deferred with "it is the host's trade to make, not here" — it now exists in the host as
        # `shutdown_runners`, and it is bounded (SIGTERM, a short grace, SIGKILL, then `wait`), so it
        # fits on this path rather than blocking it. The run thread itself is still left to die with
        # the process; its checkpoint continues to say "running", which is what actually happened.
        if self._run_is_live():
            from .host import shutdown_runners

            try:
                shutdown_runners(grace_s=_RUNNER_SHUTDOWN_GRACE_S)
            except Exception as exc:  # noqa: BLE001 - a failed reap must not break the shutdown
                _log(f"serve: reaping the run's runner failed: {exc}")
            _log("serve: a run was still in flight; its runner was signalled and the engine exited "
                 "before the run settled")
        if self._output_dead.is_set():
            _log("serve: the output stream was dead, so these refusals were logged, not delivered")
        _log("agentorg serve: stopped")

    # ── commands ────────────────────────────────────────────────────────────

    def handle(self, command: Command) -> Any:
        """Dispatch one command. Returns the ack's detail, or raises to report a refusal.

        `start` and `approve_plan` may instead return `_ACK_DEFERRED`, meaning "this command will
        acknowledge itself": their work outlives the call and their ack is the run's outcome, so the
        worker must not send one and the handler is handed the `cmd_id` to send it on.
        """
        kind = command.type_value
        handler: Callable[..., Any] | None = getattr(self, f"_cmd_{kind}", None)
        if handler is None:
            raise ServerError(f"unknown command {kind!r}")
        if kind in ("start", "approve_plan"):
            return handler(command.payload or {}, cmd_id=command.cmd_id)
        return handler(command.payload or {})

    # -- read-only ----------------------------------------------------------

    def _cmd_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The snapshot every panel polls. Shape matters: the UI reads these keys by name.

        Every key is present in **both** branches. A field that appears only once a run has started
        is a field the panel renders as missing until then — which for the cache would read as
        "caching is broken" rather than "nothing has run yet".
        """
        if self.orchestrator is None:
            # Every key the running branch carries is present here too — including the journey, which
            # is read *most* in this state: no run yet means the person is still setting up.
            idle: dict[str, Any] = {"phase": "idle", "running": False, "org": self._roster(),
                                    "gate": None, "outcome": {}, "cost": self._cost(),
                                    "cache": self._cmd_cache({}), "swarm": self._cmd_swarm({}),
                                    "workspace": self._workspace_info(),
                                    "goal": self._cmd_goal_status({})["goal"],
                                    "mission": self._cmd_mission({})["mission"],
                                    "subagents": self._cmd_subagents({}),
                                    "proposals": self._cmd_proposals({}),
                                    "activity": self._cmd_activity({}),
                                    "flow": self._cmd_flow({}),
                                    "defaults": self._cmd_defaults({}),
                                    "portfolio": self._cmd_portfolio({})}
            journey = self._journey()
            if journey is not None:
                idle["journey"] = journey
            # A plan parked before the last restart is still on disk, and the *event* that announced it
            # happened in the previous process — so the poll carries it from the checkpoint. This is the
            # state the console starts in after a relaunch, so the card must appear here or the person
            # would have to re-plan a graph the engine already composed.
            proposal = self._pending_proposal()
            if proposal is not None:
                idle["proposal"] = proposal
            return idle
        status = self.orchestrator.status()
        # The roster is what the Org panel renders, so it travels with every snapshot rather than
        # needing its own command the UI would have to remember to send.
        status["org"] = self._roster()
        status.setdefault("running", False)
        # The cache summary travels with status too, so the Cost panel gets it from the poll it
        # already makes rather than needing a second round trip that might never be added.
        status["cache"] = self._cmd_cache({})
        # The swarm snapshot travels the same way, so a panel showing per-item fan-out progress does
        # not need a second command the UI has to remember to send.
        status["swarm"] = self._cmd_swarm({})
        # Whether the agents are pointed at the user's own folder or a managed one is the first thing
        # the Org panel has to say, so it travels here rather than waiting for a dedicated command.
        status["workspace"] = self._workspace_info()
        # The goal travels with status so the Work panel can show whether the loop will continue
        # without a second round trip that might never be added.
        status.setdefault("goal", self._cmd_goal_status({})["goal"])
        # The mission travels the same way, so the console shows the standing purpose and the active
        # step from the poll it already makes.
        status.setdefault("mission", self._cmd_mission({})["mission"])
        # The subagent tree travels the same way, so the panel shows isolated children from the poll
        # it already makes.
        status.setdefault("subagents", self._cmd_subagents({}))
        # Proposals travel with status too, so a promoted fix is visible without the console having to
        # remember a second command — and so the count can badge the sidebar.
        status.setdefault("proposals", self._cmd_proposals({}))
        # The activity timeline travels the same way, so the console's "what is happening" panel is
        # populated from the poll it already makes rather than a second round trip.
        status.setdefault("activity", self._cmd_activity({}))
        # The org board travels the same way, so the Flow panel shows who is on what from the poll it
        # already makes. It is derived from the same artifacts as `activity`, so the two cannot
        # disagree about what is happening.
        status.setdefault("flow", self._cmd_flow({}))
        # The default the org runs on travels too, so the Providers panel's Defaults editor shows the
        # effective pair from the poll it already makes rather than a second round trip.
        status.setdefault("defaults", self._cmd_defaults({}))
        # The portfolio travels too, so the console can show every org the principal runs — the
        # register, not the live picture (that is `portfolio_live`, sent when the panel is open).
        status.setdefault("portfolio", self._cmd_portfolio({}))
        # The onboarding journey travels here so setup can show the *whole* path — every step, what it
        # is for, and what it unlocks — from the poll the wizard already makes. The engine owns the
        # decision (`onboarding.journey_payload`) and the app decodes it, which is the same rule the
        # capability descriptions follow: the words a person reads and the thing that gates them cannot
        # drift if there is one copy. Absent rather than fatal when the config cannot be read: setup is
        # exactly the state in which the config is often wrong, and a wizard that dies while you are
        # fixing it is the worst moment to lose it.
        journey = self._journey()
        if journey is not None:
            status["journey"] = journey
        # The plan a person can still approve travels with the snapshot too, read from the checkpoint the
        # engine already keeps for the prepared run rather than a second copy of it. This is what makes
        # the card survive a restart: `proposedGraph` is event-sourced and the app can miss the event, so
        # the poll — the fallback for a missed event everywhere else — carries the plan as well.
        proposal = self._pending_proposal()
        if proposal is not None:
            status["proposal"] = proposal
        return status

    def _journey(self) -> dict[str, Any] | None:
        """The setup journey for this workspace, or `None` when it cannot be determined.

        `None` rather than `{}` so the app can tell "the engine has not been asked yet" from "the
        engine says there are no steps" — the first is a spinner, the second would be a claim.

        `probe=False` deliberately. The journey is read on every `status` poll, and the default probe
        reaches the network to check each provider — so a console open in the background would probe
        every few seconds forever. The gate does not need a live probe to say which step you are on;
        the model step's own `Test` button is where a reachability check belongs.
        """
        from .onboarding import inspect, journey_payload

        try:
            report = inspect(config_path=getattr(self.config, "path", None),
                             workspace=self.workspace, probe=False)
        except Exception as exc:  # noqa: BLE001 - setup must survive a half-written config
            _log(f"serve: could not compute the setup journey: {exc}")
            return None
        try:
            return journey_payload(report.gate)
        except Exception as exc:  # noqa: BLE001
            _log(f"serve: could not render the setup journey: {exc}")
            return None

    def _pending_proposal(self) -> dict[str, Any] | None:
        """The plan a person can still approve, read from the run's checkpoint — or `None`.

        **Why the checkpoint and not a field on the server.** A prepared run is already persisted
        (`prepare` writes `run.plan.manifest` and the phase into `run_state.json`), so the poll reads
        *that* rather than the engine keeping a second copy that could disagree with the run it
        describes. It is also what makes the plan survive a restart: `proposedGraph` in the app is
        event-sourced, so a console relaunched after a "Plan only" would otherwise have no plan to draw.

        Only the state a person can act on is reported — `awaiting_approval`, nothing executing — so an
        already-running or already-settled run is not offered as something to approve. `approvable` and
        `reason` are the engine's own verdict on the *command* that would follow, so the app renders no
        control the engine would refuse.
        """
        from .orchestrator import RunPhase

        workspace = self.workspace
        if workspace is None:
            return None
        # A cheap in-memory guard before touching the disk: when an orchestrator is loaded and its run
        # is past approval, there is nothing to offer, and the poll then does no file read at all. The
        # read is only for the state the card exists for — parked, or a run this process has not loaded
        # yet (a relaunch), where the checkpoint is the only copy of the plan.
        live_run = getattr(self.orchestrator, "_run", None) if self.orchestrator is not None else None
        if live_run is not None and live_run.phase.value != RunPhase.AWAITING_APPROVAL.value:
            return None
        try:
            raw = workspace.read_checkpoint_raw()
        except Exception as exc:  # noqa: BLE001 - an unreadable checkpoint is "no plan", not a failure
            _log(f"serve: could not read the run checkpoint for a pending plan: {exc}")
            return None
        if not isinstance(raw, dict):
            return None
        # The *orchestrator's* checkpoint, in the one state that has a plan to approve. A runner
        # checkpoint has no `phase`, and a settled run has a terminal one, so both fall through.
        if str(raw.get("phase") or "") != RunPhase.AWAITING_APPROVAL.value:
            return None
        plan = raw.get("plan") if isinstance(raw.get("plan"), dict) else {}
        manifest = plan.get("manifest") if isinstance(plan.get("manifest"), dict) else {}
        validation = plan.get("validation") if isinstance(plan.get("validation"), dict) else {}
        approvable, reason = self._proposal_verdict(validation, raw.get("manifest_path"))
        return _proposal_document(
            run_id=str(raw.get("run_id") or ""), slug=str(raw.get("slug") or self.slug),
            goal=str(raw.get("goal") or ""), manifest=manifest, validation=validation,
            staffing_gaps=list(raw.get("staffing_gaps") or []),
            manifest_path=raw.get("manifest_path"), adopted=not bool(plan),
            approvable=approvable, reason=reason)

    def _proposal_verdict(self, validation: dict[str, Any],
                          manifest_path: Any) -> tuple[bool, str]:
        """Whether the parked plan can be approved now, and the engine's reason when it cannot.

        The verdict mirrors what `approve_plan` itself would answer, so the card and the command cannot
        disagree: a plan with no manifest file cannot be approved; an unvalidated graph is refused by
        `Orchestrator.approve`; and a run already in flight is refused by the same liveness guard `start`
        uses. `True` with an empty reason is the ordinary parked state.
        """
        if not manifest_path:
            return False, "there is no plan file to approve; the run was parked without one"
        if validation.get("valid") is False:
            errors = ", ".join(str(e) for e in (validation.get("errors") or []))
            return False, (f"the engine did not validate this graph: {errors}" if errors
                           else "the engine did not validate this graph")
        if self._run_is_live():
            return False, "a run is already in flight; pause or abort it before approving another"
        return True, ""

    def _cmd_activity(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The activity timeline: what the org is doing, why it stopped, and what is next.

        Reads the same artifacts the panels already do, folded into one ordered story. Bounded like
        the terminal buffer, so a long run yields a summary rather than a file dump.
        """
        from .activity import build_activity

        org = getattr(self.orchestrator, "org", None)
        goal_status = self._cmd_goal_status({}).get("goal")
        run_status = self.orchestrator.status() if self.orchestrator is not None else {}
        try:
            return build_activity(
                self.workspace, run_status=run_status, goal_status=goal_status, org=org,
                staffing=list(run_status.get("staffing_gaps") or []),
                limit=int(payload.get("limit") or 120))
        except Exception as exc:  # noqa: BLE001 - a snapshot must never break the poll
            _log(f"serve: activity snapshot failed: {exc}")
            return {"headline": "activity unavailable", "timeline": [], "counts": {}}

    def _cmd_attention(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every workspace under this projects root that is waiting on a person.

        **Why the console needs this at all.** `serve` is bound to one workspace and every run command it
        answers acts on that one, so a run parked in another folder was invisible here *and*
        unreachable — the app could not find it and no command could act on it. This is the one command
        that is not scoped to the workspace: it enumerates the siblings and reports what each is waiting
        for, plus the step that makes one adoptable (`portfolio_add`, the only write not bound to a
        workspace — see `attention.org_link`).

        The same document the CLI's `attention` prints, assembled once in `engine/attention.py`, so the
        two surfaces cannot say different things about one run. Read from the checkpoints rather than
        from live orchestrators — a waiting state is written to disk *before* the engine parks — so this
        works for folders this process has never loaded, and a workspace that cannot be read is skipped
        rather than failing the list.
        """
        from .attention import build_attention

        root = getattr(self.workspace, "root", None)
        return build_attention(root, portfolio=self._load_portfolio())

    def _cmd_flow(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The org board: which agent has which work, what crossed between them, and what came back.

        Derived from the same artifacts the other panels read, so the CLI's `flow` and the console's
        Flow panel cannot disagree. Bounded by the trace tail, like the activity report, so a long run
        yields a board rather than a file dump.
        """
        from .flow import build_flow

        org = getattr(self.orchestrator, "org", None)
        run_status = self.orchestrator.status() if self.orchestrator is not None else {}
        try:
            return build_flow(
                self.workspace, run_status=run_status, org=org,
                limit=int(payload.get("limit") or 800),
                org_id=str(getattr(org, "id", "") or ""),
                org_name=str(getattr(org, "name", "") or ""))
        except Exception as exc:  # noqa: BLE001 - a snapshot must never break the poll
            _log(f"serve: flow snapshot failed: {exc}")
            return {"flow_version": "1.0.0", "headline": "flow unavailable",
                    "rows": [], "handoffs": [], "counts": {}, "agents": []}

    def _workspace_info(self) -> dict[str, Any]:
        """Where this run is working: the attached folder, or the managed project."""
        ws = self.workspace
        return {
            "path": str(getattr(ws, "path", "")),
            "name": str(getattr(ws, "display_name", getattr(ws, "slug", ""))),
            "attached": bool(getattr(ws, "is_attached", False)),
            "state_dir": str(getattr(ws, "state_dir", "")),
        }

    def _cmd_org(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"org": self._roster()}

    def _cmd_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every model the console may bind, with its window and provenance."""
        catalog = self._catalog()
        return {
            "models": [entry.as_dict() for entry in catalog.list_models()],
            "providers": catalog.status(),
        }

    def _catalog(self) -> Any:
        """The server's discovery catalog, built once and reused.

        Reused for two reasons, both of which matter at the panel's refresh rate:

        - **The TTL cache only works if the object survives.** `ModelCatalog` caches per-provider
          discovery for `refresh_ttl_s` (900s by default). Building a new one per call discards that
          cache, so every panel load would re-probe every provider.
        - **A down provider is expensive.** Each probe spends the transport's full retry budget with
          backoff, so ten unreachable providers on a two-second poll is a UI that never settles.

        Rebuilt only when the configuration changes, which is what `_reload_config` signals by
        dropping this reference.
        """
        from .catalog import ModelCatalog
        from .providers.registry import build_providers

        existing = getattr(self, "_discovery", None)
        if existing is not None:
            return existing
        providers, _ = build_providers(self.config)
        self._discovery = ModelCatalog(self.config, providers)
        return self._discovery

    def _cmd_providers(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every configured provider, its discovery status, and which models it offers.

        Kept in one answer because the console's provider editor needs all three at once: the entry to
        edit, whether it is reachable, and the list to choose a model from. A shape that required three
        round trips would leave the panel half-rendered between them.

        **No key is ever returned.** `has_key` and `api_key_env` are what the UI shows; the value
        itself stays in the file, because a secret that crosses the socket is a secret in a log, a
        screenshot and a crash report.

        **`kinds` is the engine's own list of provider dialects.** It travels here rather than the form
        spelling them out: `config.SUPPORTED_KINDS` is the set a write is accepted for, and the app's
        picker is built from this reply, so a fourth dialect appears in the console the moment the
        engine declares it — the same "one copy" rule `serve._cmd_system` follows for the capability
        list (`syscap.console_payload`) and `_cmd_agents` for the hire levels.
        """
        from .catalog import ModelCatalog
        from .config import SUPPORTED_KINDS
        from .providers.registry import build_providers

        _, skipped = build_providers(self.config)
        catalog = self._catalog()

        entries: list[dict[str, Any]] = []
        # The catalog caches discovery for `refresh_ttl_s` (900s by default), which is the whole point
        # of it. Reading status before that cache is populated reported every provider as "unknown",
        # so the refresh has to happen — but through the *shared* catalog, not a fresh one per call.
        # A fresh catalog per call would re-probe every provider on every panel load, and a provider
        # that is down costs its full retry budget each time: the console would appear to hang.
        catalog.refresh()
        status = catalog.status()
        for pid, spec in sorted(self.config.providers.items()):
            models = []
            try:
                models = [e.as_dict() for e in catalog.list_models(provider_id=pid)]
            except Exception:  # noqa: BLE001 - a discovery failure must not break the list
                models = []
            entries.append({
                "id": pid,
                "kind": spec.kind,
                "base_url": spec.base_url,
                "api_key_env": spec.api_key_env or "",
                # Whether a key is *present*, never the key. `resolve_key` checks the environment
                # first, so this is true when the variable is set even if the file has no literal.
                "has_key": bool(spec.resolve_key()),
                "api_version": spec.api_version or "",
                # Derived, never stored: present only when the configured base was corrected on load,
                # so an existing config with a full endpoint explains itself in the panel.
                "base_url_note": spec.base_url_note,
                "timeout_s": spec.timeout_s,
                "max_retries": spec.max_retries,
                "concurrency": spec.concurrency,
                "model_aliases": dict(spec.model_aliases),
                # Header *names* only: a value may be a credential (see `ProviderConfig.__repr__`).
                "headers": sorted(spec.extra_headers),
                "status": (status.get(pid) or {}).get("status", "unknown"),
                "error": (status.get(pid) or {}).get("error"),
                "model_count": len(models),
                "models": models,
            })
        return {
            "providers": entries,
            "skipped": list(skipped),
            # Derived, never restated: this is the same tuple `config.load` validates a write against.
            "kinds": list(SUPPORTED_KINDS),
            # Tell the console which file an edit will touch, so the panel can show the path rather
            # than the operator having to guess which of the candidates was loaded.
            "config_path": str(self.config.path) if self.config.path else "",
        }

    def _cmd_provider_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Test a provider entry *before* it is saved, and return the models it offers.

        The candidate is built in memory from the payload, so testing cannot half-write a
        configuration. Testing an unsaved entry is the whole point: "add a key and a URL, fetch the
        models" has to work on the values in the form, not on values already committed to disk.
        """
        from .catalog import ModelCatalog
        from .config import Config, ConfigError, ProviderConfig
        from .providers.registry import build_provider

        candidate = self._provider_spec_from_payload(payload)
        try:
            adapter = build_provider(candidate)
        except ConfigError as exc:
            # A configuration problem (bad kind, missing key) is an *answer* to "test this", not a
            # server error — it is reported with the same success flag a network failure uses.
            return {"ok": False, "reason": str(exc), "models": []}

        # A config carrying only the candidate: the probe must not accidentally discover the models
        # of a *different* provider that happens to share an id in the saved file.
        single = Config(providers={candidate.id: candidate}, known_models=dict(self.config.known_models))
        catalog = ModelCatalog(single, {candidate.id: adapter})
        try:
            models = [e.as_dict() for e in catalog.list_models(provider_id=candidate.id, refresh=True)]
        except Exception as exc:  # noqa: BLE001 - an unreachable endpoint is a result, not a crash
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "models": []}

        status = catalog.status().get(candidate.id) or {}
        status_name = str(status.get("status") or "")
        # The catalog appends `+configured` when it falls back to the offline model table, so the
        # *reachability* verdict is the base word: `down+configured` means the endpoint is down and
        # the model list came from configuration. Testing the whole string would report "down" as
        # reachable — the one answer this command exists to give.
        base_status = status_name.split("+", 1)[0]
        reachable = base_status not in ("down", "error", "")
        # A reachable endpoint with no models is a real state (a gateway that lists nothing); report
        # it as reachable so the operator sees "connected, no models" rather than a bare failure.
        return {
            "ok": reachable and bool(models),
            "reachable": reachable,
            "reason": status.get("error") or ("" if models else "the endpoint returned no models"),
            "status": status_name,
            "models": models,
            "model_count": len(models),
            # The base actually probed, and why it differs from what was typed. This is what turns
            # "it didn't work" into "your URL was the full endpoint; I used the base" — the difference
            # between a user re-checking a correct key and one who knows what happened.
            "base_url": candidate.base_url,
            "note": candidate.base_url_note,
        }

    def _cmd_provider_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add or update one provider in `credentials.json`, then reload the live config.

        The config is reloaded in place rather than left stale, so the very next `models` or `start`
        sees the new endpoint. A roster change without a reload is how "I added it and nothing used it"
        happens.
        """
        from .config import ConfigError, load, write_provider

        if not self.config.path:
            raise ServerError("this engine was started without a credentials file, so there is "
                              "nowhere to save a provider")
        spec = self._provider_spec_from_payload(payload)
        entry: dict[str, Any] = {
            "kind": spec.kind,
            "base_url": spec.base_url,
            "timeout_s": spec.timeout_s,
            "max_retries": spec.max_retries,
            "concurrency": spec.concurrency,
        }
        # Both are written when both were given. The variable stays the *preferred* source — it is the
        # documented safe path and `resolve_key` reads it first — but dropping the literal made
        # "paste a key and also name a variable" a configuration with no usable key at all, which the
        # engine then reported as "has no API key" to someone who had just pasted one.
        #
        # That was reachable from the app's own form the moment the variable field held anything, and
        # it is the worst kind of wrong: the person's input was silently discarded and the error blamed
        # them. Keeping both cannot leak anything further — the file is 0600 and gitignored, and the
        # literal was going to be written whenever the variable field was empty.
        if spec.api_key_env:
            entry["api_key_env"] = spec.api_key_env
        if spec.api_key:
            entry["api_key"] = spec.api_key
        if spec.api_version:
            entry["api_version"] = spec.api_version
        if spec.extra_headers:
            entry["extra_headers"] = dict(spec.extra_headers)
        if spec.model_aliases:
            entry["model_aliases"] = dict(spec.model_aliases)

        try:
            target = write_provider(self.config.path, entry, provider_id=spec.id)
        except ConfigError as exc:
            raise ServerError(str(exc)) from exc

        self._reload_config()
        self.emit(Event(seq=0, type=EventType.MODEL_CATALOG_REFRESHED,
                        payload={"provider_id": spec.id, "reason": "provider saved"}))
        return {"saved": str(target), "provider_id": spec.id,
                # The normalised base and, when it differs from what was typed, why. The console
                # shows this so the correction is *told*, not silently applied — the user pasted a
                # real endpoint and deserves to know it was understood.
                "base_url": spec.base_url,
                "note": spec.base_url_note,
                "providers": self._cmd_providers({})["providers"]}

    def _cmd_provider_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove one provider from `credentials.json`, refusing to remove the last one.

        **The refusal is the point.** `load` refuses a document with no providers
        (`config._build_providers`: "config defines no providers; at least one is required"), so
        writing one would leave a file the *next launch* cannot read — and the running server hides
        it, because `_reload_config` swallows the failure and keeps serving the stale object. The
        breakage would then surface only on restart, which is the worst moment to discover it.
        Refusing is the fix rather than silently keeping or inventing a replacement: choosing which
        endpoint takes over is a routing decision the user did not make, the same reason
        `config._remove_provider_references` drops a dangling default instead of guessing another
        provider. So the reason names the way forward — add the replacement first, then remove this
        one.

        **And the blast radius is reported.** A roster agent bound to the endpoint keeps that binding
        (the roster is re-written verbatim), so it stops being callable rather than being re-pointed.
        The reply names those agents, so the console can say *which* agents a removal broke from the
        engine's own answer instead of asserting it in prose of its own.
        """
        from .config import ConfigError, provider_ids, write_provider

        pid = str(payload.get("provider_id") or payload.get("id") or "").strip()
        if not pid:
            raise ServerError("provider_remove needs a provider_id")
        if not self.config.path:
            raise ServerError("this engine was started without a credentials file")
        try:
            remaining = [other for other in provider_ids(self.config.path) if other != pid]
        except ConfigError as exc:
            raise ServerError(str(exc)) from exc
        if not remaining:
            raise ServerError(
                f"{pid!r} is the only provider configured, so removing it would leave a credentials "
                "file no launch can read — the loader requires at least one provider. Add its "
                "replacement first, then remove this one."
            )
        try:
            write_provider(self.config.path, {}, provider_id=pid, remove=True)
        except ConfigError as exc:
            raise ServerError(str(exc)) from exc
        self._reload_config()
        self.emit(Event(seq=0, type=EventType.MODEL_CATALOG_REFRESHED,
                        payload={"provider_id": pid, "reason": "provider removed"}))
        # Read *after* the reload, from the live roster: the built-in company is re-derived from the
        # resolved default on every load, so an endpoint that was only the default re-points those
        # agents and they are correctly not reported. What is left is the persisted binding — an agent
        # the Owner hired keeps the provider it was hired onto, and nothing moves it.
        bound = self._agents_bound_to(pid)
        return {"removed": pid, "providers": self._cmd_providers({})["providers"],
                "agents": bound, "agent_count": len(bound)}

    def _agents_bound_to(self, provider_id: str) -> list[dict[str, str]]:
        """The live-roster agents still naming a provider, by id and name.

        `{id, name}` rather than the whole spec: this travels with a removal's reply so the console can
        name the agents that just stopped being callable, and a full spec would drag a model, a budget
        and a capability list along with it. Sorted by name so two removals of the same endpoint report
        the same order.

        A console with no workspace — the CLI's `providers` command resolves none by design, because
        it edits `credentials.json` and nothing else — has no project of its own, so the list is empty.
        Discovering from the process's working directory would read whatever roster happens to sit
        above it, which is a claim about a project the caller never named; the app always has a
        workspace, so this only ever reports nothing for a surface that read no roster.
        """
        if self.workspace is None:
            return []
        org = self._people().load(project=self._project_dir())
        return [{"id": spec.id, "name": spec.name}
                for spec in sorted(org.agents.values(), key=lambda a: a.name)
                if spec.provider == provider_id]

    def _provider_spec_from_payload(self, payload: dict[str, Any]) -> Any:
        """Build a `ProviderConfig` from a console payload, validating what it must.

        Shared by test and add so the two cannot disagree about what a valid entry is — a provider
        that tests green and then fails to save is worse than one that fails both.
        """
        from .config import ProviderConfig, SUPPORTED_KINDS

        pid = str(payload.get("provider_id") or payload.get("id") or "").strip()
        if not pid:
            raise ServerError("a provider needs an id")
        kind = str(payload.get("kind") or SUPPORTED_KINDS[0]).strip().lower()
        # The accepted set is `config.SUPPORTED_KINDS`, not a tuple written here: a dialect added to the
        # config must be writable from the console without a second edit, and a second copy is a copy
        # that can refuse what the loader would accept.
        if kind not in SUPPORTED_KINDS:
            raise ServerError(f"unsupported provider kind {kind!r}; use "
                              f"{', '.join(SUPPORTED_KINDS)}")
        base_url = str(payload.get("base_url") or "").strip()
        if not base_url:
            raise ServerError("a provider needs a base_url")
        # The mistake the picker used to invite: `ollama` targets a *local server's* `/api/chat`, while
        # Ollama's cloud speaks the OpenAI dialect at `/v1`. Choosing "Ollama" for `ollama.com` produced
        # `…/chat/completions/api/chat`, and the resulting 404 is indistinguishable from a bad key —
        # so the person re-pasted their key and changed the URL, neither of which was wrong.
        #
        # Refused with the correction rather than silently rewritten: the kinds are genuinely different
        # protocols, and quietly switching one for the other would leave the stored config describing
        # something the person did not choose.
        if kind == "ollama" and "ollama.com" in base_url.lower():
            raise ServerError(
                "ollama.com is Ollama's cloud endpoint, which speaks the OpenAI dialect — choose "
                "'OpenAI-compatible' as the kind and use `https://ollama.com/v1` as the base URL. "
                "The 'Local Ollama' kind is for a server on this machine, whose route is `/api/chat`.")

        raw_headers = payload.get("extra_headers") or payload.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise ServerError("extra_headers must be an object of name -> value")
        headers = {str(k).strip(): str(v) for k, v in raw_headers.items() if str(k).strip()}

        api_key_env = str(payload.get("api_key_env") or "").strip() or None
        # An explicit null/empty key means "no key" — a local endpoint — rather than "leave it
        # unchanged", which is what an absent field would mean if this merged. It does not merge.
        api_key = payload.get("api_key")
        api_key = str(api_key).strip() if isinstance(api_key, str) and api_key.strip() else None

        try:
            timeout_s = float(payload.get("timeout_s") or 120)
            max_retries = int(payload.get("max_retries") or 3)
            concurrency = int(payload.get("concurrency") or 2)
        except (TypeError, ValueError) as exc:
            raise ServerError(f"timeout, retries and concurrency must be numbers: {exc}") from exc

        return ProviderConfig(
            id=pid, kind=kind, base_url=base_url, api_key=api_key, api_key_env=api_key_env,
            api_version=str(payload.get("api_version") or "").strip() or None,
            timeout_s=timeout_s, max_retries=max_retries, concurrency=concurrency,
            extra_headers=headers)

    def _reload_config(self) -> None:
        """Re-read the configuration in place after a provider edit.

        In place, because the server and every panel read `self.config`: swapping the object would
        leave the console talking to a stale config while the file on disk said something else.

        The **same file** is re-read rather than re-discovered. `load()` with no path walks its
        candidate list, which resolves `$AGENTORG_CREDENTIALS` and then `./credentials.json` — so a
        server started with an explicit path would silently reload a *different* config, and the edit
        would appear to have been ignored.
        """
        from .config import load

        try:
            fresh = load(self.config.path, warn=False) if self.config.path else load(warn=False)
        except Exception as exc:  # noqa: BLE001 - a broken edit must not kill the server
            _log(f"serve: could not reload the configuration: {exc}")
            return
        self.config = fresh
        # Drop the cached discovery so the next read probes the new endpoint rather than serving
        # models from the provider that was just replaced. This is the one signal that the catalog
        # must be rebuilt — otherwise the TTL would keep a stale provider list for 15 minutes.
        self._discovery = None

    def _default_window(self, provider: str, model: str) -> tuple[int | None, int | None, str]:
        """The window (and max output) the system will bind the default model with, and its source.

        Resolved as a hire resolves it: an explicit `defaults.context_window` wins, then the live
        catalog's probed value, then the declared table. The console and the CLI must report the same
        number — they did not, because this handler read only the declared table.
        """
        spec = self.config.default_model_spec()
        if spec.context_window and spec.model_id == model:
            return int(spec.context_window), spec.max_output, spec.source
        try:
            from .catalog import ModelCatalog

            entry = self._catalog().resolve(provider, model)
            if entry is not None and entry.window_known:
                return int(entry.context_window), entry.max_output, entry.source
        except Exception:  # noqa: BLE001 - a probe failure degrades to "unknown"
            pass
        return None, None, ""

    def _cmd_defaults(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The default provider/model everyone uses, and how autonomous a goal is by default.

        Reports the **effective** pair, not just the file: a declared default that a removed provider
        invalidated resolves to a usable one, and the reason travels so the panel can say why. No key
        is ever returned.
        """
        # The posture on the *document*, not the effective one: the engine has a default either way,
        # and only the record tells "the person answered" from "the engine filled it in". Read from
        # `onboarding.posture_recorded` rather than by looking at the config's own field, so this
        # cannot become a second answer to the question the journey step is decided by.
        from .onboarding import posture_recorded

        config = self.config
        provider, model, reason = config.default_pair()
        # The window the system will *actually* bind with — catalog first, declared table second —
        # exactly as the CLI reports it and as `hire` resolves it. Reading only the declared table told
        # the console `window: None` for `Olla/deepseek-v4.1-flash` (a probed model the config never
        # declares) while the same run bound 1048576, so the panel and the behaviour disagreed.
        window, max_output, window_source = self._default_window(provider, model)
        return {
            "provider": provider,
            "model": model,
            "reason": reason,
            "context_window": window,
            "window_source": window_source,
            "max_output": max_output,
            "temperature": config.default.temperature,
            "declared": config.default.as_dict(),
            "reviewer": {"provider": config.default.reviewer_provider,
                         "model": config.default.reviewer_model},
            "autonomy": {
                "auto_pass_auto_gates": config.goal.auto_pass_auto_gates,
                "auto_hire_missing": config.goal.auto_hire_missing,
                "persist_auto_hires": config.goal.persist_auto_hires,
                "auto_hire_max_tier": config.goal.auto_hire_max_tier,
                "token_budget": config.goal.token_budget,
                # The posture a new goal inherits, and whether the *file* records one. Two fields
                # because they are two different facts: the engine has a default either way, and the
                # journey must still tell "the person has not answered yet" from "the person chose
                # Unattended". `onboard.posture_recorded` is the one rule, so a second reading here
                # cannot disagree with the step the wizard is showing.
                "posture": config.goal.default_posture,
                "posture_recorded": posture_recorded(config),
            },
            "usable": config._usable_providers(),
            "providers": sorted(config.providers),
            "config_path": str(config.path) if config.path else "",
        }

    def _cmd_defaults_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set the default provider and/or model in `credentials.json`, atomically.

        Validated against what is actually configured *before* the write: a default naming a provider
        that does not exist would resolve to a fallback and silently not be what the person chose, so
        it is refused with the list of real providers.
        """
        from .config import ConfigError, set_defaults

        if not self.config.path:
            raise ServerError("this engine was started without a credentials file")
        provider = str(payload.get("provider") or "").strip()
        model = str(payload.get("model") or "").strip()
        if provider and provider not in self.config.providers:
            raise ServerError(
                f"unknown provider {provider!r}; configured: "
                f"{', '.join(sorted(self.config.providers)) or '(none)'}")
        window = payload.get("context_window")
        try:
            set_defaults(
                self.config.path,
                provider=provider,
                model=model,
                reviewer_provider=str(payload.get("reviewer_provider") or "").strip(),
                reviewer_model=str(payload.get("reviewer_model") or "").strip(),
                context_window=int(window) if window not in (None, "") else None,
            )
        except (ConfigError, ValueError) as exc:
            raise ServerError(str(exc)) from exc
        self._reload_config()
        self.emit(Event(seq=0, type=EventType.MODEL_CATALOG_REFRESHED,
                        payload={"reason": "defaults set", "provider": provider,
                                 "model": model}))
        return self._cmd_defaults({})

    def _cmd_autonomy_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set how autonomous a goal is by default, in `credentials.json`, atomically.

        Kept separate from the model default on purpose: changing which model the org runs on must not
        silently change whether a run needs a person. A boolean passed as `None` is left untouched, so
        a panel can send only the switch that changed.

        **`posture` is accepted here now, and it had to be.** The app's first-run wizard asks this
        question, and until this key existed the only thing the answer changed was a value the window
        kept to itself — `goal.default_posture` stayed absent from the file, so the engine's own
        journey reported the step as outstanding forever while the window had already marked it done
        and retired. A first-run step a person has answered that the engine still calls unanswered is
        the "still confusing about onboarding" report in its purest form: the checklist and the engine
        disagree, and re-running setup shows the same question again with nothing saying why.

        Validated here rather than left to `GoalConfig.__post_init__`, because an invalid posture
        written to the file would make the *whole configuration* unloadable on the next read — a bad
        value must be refused at the write, not discovered as a broken engine afterwards.
        """
        from .config import ConfigError, set_autonomy
        from .goal import Posture

        if not self.config.path:
            raise ServerError("this engine was started without a credentials file")
        goal: dict[str, Any] = {}
        for key, payload_key in (("auto_pass_auto_gates", "auto_pass_auto_gates"),
                                 ("auto_hire_missing", "auto_hire_missing"),
                                 ("persist_auto_hires", "persist_auto_hires")):
            value = payload.get(payload_key)
            if value is not None:
                goal[key] = bool(value)
        if payload.get("auto_hire_max_tier") is not None:
            goal["auto_hire_max_tier"] = int(payload["auto_hire_max_tier"])
        posture = str(payload.get("posture") or "").strip().lower()
        if posture:
            # The accepted set is `goal.Posture`, the enum that decides what each posture means — not
            # the two words written here. A third posture would otherwise be refused by this guard
            # while `GoalPolicy` accepted it, which is the drift the CLI's `--posture` was cured of.
            accepted = {p.value for p in Posture}
            if posture not in accepted:
                raise ServerError(
                    f"unknown posture {posture!r}; expected one of {', '.join(sorted(accepted))}")
            goal["default_posture"] = posture
        if not goal:
            raise ServerError("autonomy_set needs at least one setting")
        try:
            set_autonomy(self.config.path, goal=goal)
        except ConfigError as exc:
            raise ServerError(str(exc)) from exc
        self._reload_config()
        self.emit(Event(seq=0, type=EventType.POLICY_CHANGED,
                        payload={"reason": "autonomy set", **goal}))
        return self._cmd_defaults({})

    def _cmd_system_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write the machine-access switches, so a panel can grant what it has just described.

        The counterpart to `system`, which only *reads*: without this the console could render every
        capability, its reach and its caution, and still have no way to say yes — the person was told
        "the machine tools are off" with no switch to turn. Granting is a state change on their machine,
        so it is a separate command rather than a side effect of reading the description.

        Only the keys present in the payload are written, so a panel can send one switch without
        restating the others — the same rule `autonomy_set` follows, for the same reason: a UI that
        resends everything it knows will overwrite a change made elsewhere in between.
        """
        from .config import ConfigError, set_system

        if not self.config.path:
            raise ServerError("this engine was started without a credentials file, so there is nowhere "
                              "to record this")
        update: dict[str, Any] = {}
        for key in ("enabled", "allow_full_access"):
            if payload.get(key) is not None:
                update[key] = bool(payload[key])
        for key in ("allow_apps", "allow_automation", "allow_shortcuts"):
            value = payload.get(key)
            if value is not None:
                if not isinstance(value, (list, tuple)):
                    raise ServerError(f"system_set: {key} must be a list of names")
                update[key] = [str(v) for v in value]
        if payload.get("screenshot_dir") is not None:
            update["screenshot_dir"] = str(payload["screenshot_dir"])
        if not update:
            raise ServerError("system_set needs at least one setting")
        try:
            set_system(self.config.path, system=update)
        except ConfigError as exc:
            raise ServerError(str(exc)) from exc
        # Reloaded in place for the same reason a provider edit is: the next tool call and the next
        # `system` read must see the grant that was just written, not the section it replaced.
        self._reload_config()
        self.emit(Event(seq=0, type=EventType.POLICY_CHANGED,
                        payload={"reason": "system access set", **update}))
        return self._cmd_system({})

    def _cmd_cache(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The cache picture for this run: hit rate, tokens, and what it has saved.

        Accumulated from the forwarded `llm.response` events rather than read from a gateway, because
        the gateway lives in the executing subprocess — the parent sees only what crosses the bus.
        Every figure is `None` when no provider reported a cache, because a hit rate invented from
        silence would claim caching works when nobody has looked.
        """
        totals = getattr(self, "_cache_totals", None) or {
            "turns": 0, "cache_hit_tokens": None, "cache_miss_tokens": None,
            "cache_saving_usd": None, "cache_reported": False,
        }
        hit = totals.get("cache_hit_tokens")
        miss = totals.get("cache_miss_tokens")
        total = (hit or 0) + (miss or 0)
        return {
            "turns": totals.get("turns", 0),
            "cache_reported": bool(totals.get("cache_reported")),
            "cache_hit_tokens": hit,
            "cache_miss_tokens": miss,
            "cache_hit_rate": (round((hit or 0) / total, 4)
                               if totals.get("cache_reported") and total > 0 else None),
            "cache_saving_usd": totals.get("cache_saving_usd"),
        }

    def _accumulate_cache(self, event: Any) -> None:
        """Fold one forwarded event's cache figures into the run totals."""
        payload = getattr(event, "payload", None) or {}
        cost = payload.get("cost") if isinstance(payload, dict) else None
        if not isinstance(cost, dict):
            return
        totals = getattr(self, "_cache_totals", None)
        if totals is None:
            totals = {"turns": 0, "cache_hit_tokens": None, "cache_miss_tokens": None,
                      "cache_saving_usd": None, "cache_reported": False}
            self._cache_totals = totals
        totals["turns"] += 1
        for key in ("cache_hit_tokens", "cache_miss_tokens"):
            value = cost.get(key)
            if value is not None:
                totals[key] = (totals[key] or 0) + int(value)
                totals["cache_reported"] = True
        saving = cost.get("cache_saving_usd")
        if saving is not None:
            totals["cache_saving_usd"] = (totals["cache_saving_usd"] or 0.0) + float(saving)

    def _cmd_pool(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The task pool, so a console can show queued work and who can claim it."""
        from .pool import TaskPool

        pool = TaskPool(self.workspace.pool_path)
        return {"summary": pool.summary(),
                "tasks": [t.as_dict() for t in pool.tasks.values()]}

    def _cmd_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._cmd_status(payload)

    # -- the run lifecycle --------------------------------------------------

    def _cmd_start(self, payload: dict[str, Any], *, cmd_id: str = "") -> Any:
        """Plan (and unless asked not to, execute) a goal.

        The `start` command the app sends covers both: it proposes a graph and, having done so,
        proceeds — the Owner's approve gate is where a run actually waits.

        The graph then executes on a **thread of its own** (`_start_run`), and that thread sends this
        command's ack when the graph settles. Both halves are deliberate:

        - Its own thread, because the command worker is the app's only path to `status` and to
          `pause`/`abort`. A run executing inline there is what froze every panel and made Pause a
          no-op until the run had ended.
        - Acked on settle, because the ack's detail **is** the run's outcome (`phase`, `outcome`,
          `gated`), and a run that cannot execute is a failed `start` — `ok=false` with the reason,
          exactly as before. The app awaits this ack with the engine's 600s command budget, inside a
          `Task`, so nothing in the console is blocked by it.
        """
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise ServerError("start needs a goal")
        if self._run_is_live():
            # Refused before `_orchestrator` and `prepare`, both of which would clobber the live run:
            # building an orchestrator replaces the one holding its `_host`, and `prepare` re-points
            # `_run` at a second graph and persists it over the first one's checkpoint. The run thread
            # is what takes the run off the worker now, so the worker no longer serialises a second
            # `start` behind the first by accident — this is that serialisation, said out loud.
            raise ServerError(
                "a run is already in flight; pause or abort it before starting another")
        slug = str(payload.get("slug") or self.slug)
        self.slug = slug
        orch = self._orchestrator(slug)
        # `auto_staff` is left to the goal's policy (and then the config default), so the console does
        # not have to send a second flag — arming a goal *is* the authorisation. An explicit
        # `auto_staff` in the payload overrides, which is what a "report the gap, do not fill it"
        # button would send.
        auto_staff = payload.get("auto_staff")
        run = orch.prepare(goal, slug=slug,
                           max_iterations=int(payload.get("max_iterations") or 3),
                           auto_staff=None if auto_staff is None else bool(auto_staff))
        self._forward_bus(orch)
        # The plan event and the `status` payload carry the **same** document (`_proposal_document`), so
        # the card cannot read one shape from the event and a different one from the two-second poll.
        # `approvable` is decided here rather than inferred by the app: a dry run parks the graph for
        # approval, while an ordinary `start` is already handing it to the run thread — so the card
        # offers no control for the case where the engine would refuse it.
        approvable = bool(payload.get("dry_run"))
        self.emit(Event(seq=0, type=EventType.MANIFEST_PROPOSED,
                        payload=_proposal_document(
                            run_id=run.run_id, slug=run.slug, goal=goal,
                            manifest=run.plan.manifest if run.plan else {},
                            validation=run.plan.validation.as_dict() if run.plan else {},
                            staffing_gaps=run.staffing_gaps,
                            manifest_path=run.manifest_path, adopted=run.plan is None,
                            approvable=approvable,
                            reason="" if approvable else "the engine is already starting this plan")))
        detail = {"run_id": run.run_id, "slug": run.slug, "phase": run.phase.value}

        if payload.get("dry_run"):
            return detail

        self._start_run(cmd_id, orch, run, detail)
        return _ACK_DEFERRED

    def _cmd_approve_plan(self, payload: dict[str, Any], *, cmd_id: str = "") -> Any:
        """Approve the graph a run is parked on and execute it.

        The route the console lacked. `approve` is `Orchestrator.decide`, which resolves a **gate**, and
        `Orchestrator.prepare` never sets `run.gate` — so a plan parked by a dry run ("Plan only") had no
        command that could approve it, and the app could show the graph but not act on it. This is
        `start` minus the prepare step: the prepared run goes to the very same `_start_run`, so the graph
        executes on the run's own thread, `status` and `pause`/`abort` stay answerable, and the ack is the
        run's outcome sent when it settles — the convention `start` already follows.

        The guards are `start`'s and `resume`'s, reused rather than restated: a second graph on one
        workspace is the failure `_run_is_live` exists to prevent. `approve`'s own refusals (an invalid
        graph, a missing manifest) are left to `Orchestrator.approve`, which the run thread calls — so
        the reason on the wire is the engine's, not a second copy of it written here.
        """
        from .orchestrator import RunPhase

        if self._run_is_live():
            raise ServerError(
                "a run is already in flight; pause or abort it before approving another")
        if self.orchestrator is None:
            # After a restart the prepared run is on disk and nothing is loaded yet, so build the
            # orchestrator the same way `start` does; `_current_run` then reads the checkpoint.
            self._orchestrator(self.slug)
        orch, run = self._current_run()
        if run is None:
            raise ServerError(
                "there is no prepared plan to approve; `start` with `dry_run` parks one")
        if run.phase != RunPhase.AWAITING_APPROVAL:
            raise ServerError(
                f"the run is {run.phase.value}, not awaiting approval, so there is nothing to approve")
        detail = {"run_id": run.run_id, "slug": run.slug, "phase": run.phase.value}
        self._start_run(cmd_id, orch, run, detail)
        return _ACK_DEFERRED

    def _run_is_live(self) -> bool:
        """Whether a graph is executing right now.

        Read from the run *thread* rather than from the orchestrator's `running` flag: that flag is set
        inside `execute`, which is already on the run thread, so a `start` landing in the window before
        it would slip past a flag-based check and put two graphs on one workspace.
        """
        thread = self._run_thread
        return thread is not None and thread.is_alive()

    def _start_run(self, cmd_id: str, orch: Any, run: Any, detail: dict[str, Any]) -> None:
        """Execute an approved graph on its own thread, acking `start` when it settles.

        The thread is a daemon, like the command worker, so a shut-down engine cannot be held open by a
        run that is still going. The worker's in-flight token ends here — it is released as soon as this
        returns `_ACK_DEFERRED` — because the run is no longer the worker's work to account for, and
        `_drain`'s wait is bounded (a run is not).
        """
        thread = threading.Thread(
            target=self._run_graph, args=(cmd_id, orch, run, detail),
            name=f"agentorg-run-{run.run_id}", daemon=True)
        try:
            self._run_thread = thread
            thread.start()
        except RuntimeError:
            # No thread to hand the work to, and `_ACK_DEFERRED` was never returned, so the worker
            # answers this command itself — as a failure, with the reason.
            self._run_thread = None
            raise

    def _run_graph(self, cmd_id: str, orch: Any, run: Any, detail: dict[str, Any]) -> None:
        """Approve, execute, and acknowledge — off the command worker."""
        try:
            orch.approve(run)
            self.emit(Event(seq=0, type=EventType.MANIFEST_APPROVED,
                            payload={"run_id": run.run_id, "slug": run.slug}))
            outcome = orch.execute(run)
            detail["phase"] = run.phase.value
            detail["outcome"] = (outcome.summary or {}).get("outcome") or outcome.state.value
            detail["gated"] = bool(getattr(outcome, "gated", False))
            self.ack(cmd_id, ok=True, detail=detail)
        except Exception as exc:  # noqa: BLE001 - a run that cannot execute is an answer, not a crash
            _log(f"serve: run {run.run_id} failed: {exc}")
            self.ack(cmd_id, ok=False, error=str(exc))
        finally:
            # Only the liveness marker: the in-flight counter is the worker's, and this thread never
            # held a token in it. Cleared last, so `_run_is_live` is false only once the run's ack has
            # been written — a `start` arriving in between would otherwise be refused for a run that had
            # already finished.
            self._run_thread = None

    def _cmd_approve(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch, run = self._current_run()
        if run is None:
            raise ServerError("no run is loaded to approve")
        orch.decide(True, run=run, note=str(payload.get("note") or ""))
        return {"run_id": run.run_id, "phase": run.phase.value}

    def _cmd_reject(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch, run = self._current_run()
        if run is None:
            raise ServerError("no run is loaded to reject")
        orch.decide(False, run=run, note=str(payload.get("note") or ""))
        return {"run_id": run.run_id, "phase": run.phase.value}

    def _cmd_instruct(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch, run = self._current_run()
        if run is None:
            raise ServerError("no run is loaded to instruct")
        text = str(payload.get("text") or payload.get("instruction") or "").strip()
        if not text:
            raise ServerError("instruct needs text")
        orch.instruct(text, run=run, as_constraint=bool(payload.get("constraint")))
        return {"run_id": run.run_id, "instruction": text}

    def _cmd_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Continue a paused run from its checkpoint.

        Refused while a graph is executing, exactly as `start` is, and for a sharper reason:
        `Orchestrator.resume` sets the run's phase to READY, clears its gate and persists it — while the
        run thread that is still executing the graph writes its own phases to the same run and the same
        checkpoint. The two disagreeing is not a cosmetic problem: the file on disk says "ready" for a
        run that is mid-node, and a resume after a crash would start a second execution of a graph that
        never stopped.
        """
        orch = self.orchestrator
        if orch is None:
            raise ServerError("no run is loaded to resume")
        if self._run_is_live():
            raise ServerError(
                "a run is already in flight; pause or abort it before resuming")
        run = orch.resume()
        return {"run_id": run.run_id, "phase": run.phase.value}

    def _cmd_abort(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Stop the run, keeping its checkpoint so it can be resumed.

        A best-effort signal: the app posts it without awaiting, so it must never raise for the
        ordinary case of nothing running.
        """
        orch = self.orchestrator
        if orch is None:
            return {"aborted": False, "reason": "no run is loaded"}
        try:
            orch.abort()
            return {"aborted": True}
        except Exception as exc:  # noqa: BLE001 - aborting nothing is not an error worth raising
            return {"aborted": False, "reason": str(exc)}

    def _cmd_discard_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Discard a settled run's checkpoints, so the board stops reporting work nobody can act on.

        The console's half of `engine.cli discard`, and the **same engine operation** rather than a
        second implementation: both call `Workspace.discard_run`, so the count, the backup path and
        the kept list a person reads here are the ones the terminal printed. A Swift copy of that
        account would drift the first time the operation changed.

        `_run_is_live()` is passed in as the liveness judgement rather than a second rule written
        here — a run on this server's run thread is exactly the "checkpoint being written by a running
        node" the operation refuses to move, and reusing the helper the `start`/`resume` guards use
        keeps one answer to "is a run in flight" instead of two.
        """
        from .state import StateError

        try:
            report = self.workspace.discard_run(
                live=self._run_is_live(),
                include_record=bool(payload.get("include_record")))
        except StateError as exc:
            # A live run and an unreadable state directory are both refusals a person must see:
            # `ServerError` is what turns one into `ok: false` carrying the reason, rather than an ack
            # that looks like success while nothing moved.
            raise ServerError(str(exc)) from exc
        if report["discarded"] and self.orchestrator is not None:
            # The orchestrator may still hold the run it loaded before the files moved, and `status`
            # reads *that object* — so without this a poll taken a moment later would go on describing
            # the run the board has already forgotten, and the app and the terminal would disagree
            # about a workspace this command just reconciled. Only the run is dropped: the roster, the
            # policy and the bus are not what was discarded.
            self.orchestrator._run = None  # noqa: SLF001 - the same field `_current_run` reads
        return report

    def _cmd_pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self.orchestrator
        if orch is None:
            return {"paused": False, "reason": "no run is loaded"}
        try:
            orch.pause()
            return {"paused": True}
        except Exception as exc:  # noqa: BLE001
            return {"paused": False, "reason": str(exc)}

    def _cmd_reassign(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch, run = self._current_run()
        if run is None:
            raise ServerError("no run is loaded to reassign")
        node = str(payload.get("node") or "").strip()
        agent_id = str(payload.get("agent_id") or "").strip()
        if not node or not agent_id:
            raise ServerError("reassign needs node and agent_id")
        orch.reassign(node, agent_id, run=run)
        return {"node": node, "agent_id": agent_id}

    def _cmd_takeover(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch, run = self._current_run()
        if run is None:
            raise ServerError("no run is loaded to take over")
        node = str(payload.get("node") or "").strip()
        if not node:
            raise ServerError("takeover needs a node")
        orch.takeover(node, run=run)
        return {"node": node}

    def _cmd_hire(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Hire an agent from the console, into the same roster the CLI writes."""
        people = self._people()
        org = people.load(project=self._project_dir())
        spec = people.hire(self._hire_request(payload), org=org)
        self._sync_org()
        return {"agent": spec.as_dict(), "warnings": people.warnings}

    def _cmd_agents(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every agent in the effective roster, with what the console needs to edit one.

        The built-ins are included, because they *are* the org until the Owner changes it — a panel
        that listed only hired agents would look empty on a fresh install while seven agents were
        actually running. `origin` distinguishes them, and `editable` says which can be changed.

        **`levels` and `roles` are the vocabularies a hire may name.** They travel with this reply —
        the one the hire form already fetches for the roster and the skills — so the form's pickers are
        built from `people.LEVELS` and `people.HIRE_ROLES` rather than spelling them out. That was not
        cosmetic: the app listed five of `LEVELS`' six names, so `mid` could not be chosen from the
        console at all. `levels` is the dict's own order (the declared order) and `roles` the tuple's.
        """
        from .people import HIRE_ROLES, LEVELS

        people = self._people()
        org = people.load(project=self._project_dir())
        agents: list[dict[str, Any]] = []
        for spec in sorted(org.agents.values(), key=lambda a: (_is_hired(a), a.name)):
            entry = spec.as_dict()
            entry["editable"] = spec.id != "ag_owner"
            entry["origin"] = spec.origin
            entry["skills"] = list(spec.skills)
            entry["provider"] = spec.provider
            entry["model"] = spec.model
            entry["context_window"] = spec.context_window
            entry["level"] = spec.level.value if hasattr(spec.level, "value") else str(spec.level)
            # `hired` is the Owner's own agents; `built-in` is the default company; the Owner
            # principal is neither, so the panel can present it as authority rather than headcount.
            if spec.id == "ag_owner":
                entry["status"] = "owner"
            else:
                entry["status"] = "hired" if _is_hired(spec) else "built-in"
            agents.append(entry)
        return {
            "agents": agents,
            "count": len(agents),
            # The Owner principal is *not* a hire — it is the terminal authority the engine always
            # has. Counting it would make a fresh install report one hired agent that nobody hired.
            "hired": sum(1 for a in agents
                         if a["origin"] == "owner" and a.get("id") != "ag_owner"),
            "roster_path": str(self._roster_file()),
            "skills": self._skill_names(),
            # The hire vocabulary, from the engine's own constants. `levels` is every name `hire`
            # resolves `--level` through — including the `mid` alias the app used to drop.
            "levels": list(LEVELS),
            "roles": list(HIRE_ROLES),
        }

    def _cmd_agent_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Change an agent's name, model, skill level or limits — keeping its id and history."""
        from .people import HireError

        agent_id = str(payload.get("agent_id") or payload.get("id") or "").strip()
        if not agent_id:
            raise ServerError("agent_update needs an agent_id")
        people = self._people()
        people.load(project=self._project_dir())
        try:
            spec = people.update_agent(
                agent_id,
                name=payload.get("name") if isinstance(payload.get("name"), str) else None,
                provider=payload.get("provider") if isinstance(payload.get("provider"), str) else None,
                model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                context_window=(int(payload["context_window"])
                                if payload.get("context_window") else None),
                level=payload.get("level") if isinstance(payload.get("level"), str) else None,
                team=payload.get("team") if isinstance(payload.get("team"), str) else None,
                title=payload.get("title") if isinstance(payload.get("title"), str) else None,
                max_concurrency=(int(payload["max_concurrency"])
                                 if payload.get("max_concurrency") else None),
                )
        except (HireError, ValueError, TypeError) as exc:
            raise ServerError(str(exc)) from exc
        self._sync_org()
        return {"agent": spec.as_dict(), "warnings": people.warnings}

    def _cmd_agent_retire(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove an agent from the roster."""
        from .people import HireError

        agent_id = str(payload.get("agent_id") or payload.get("id") or "").strip()
        if not agent_id:
            raise ServerError("agent_retire needs an agent_id")
        people = self._people()
        people.load(project=self._project_dir())
        try:
            spec = people.retire_agent(agent_id, reason=str(payload.get("reason") or ""))
        except HireError as exc:
            raise ServerError(str(exc)) from exc
        self._sync_org()
        return {"retired": spec.as_dict(),
                "agents": self._cmd_agents({})["agents"]}

    def _cmd_skills(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The skills an agent can be hired for, so the console offers real ones rather than a guess."""
        return {"skills": self._skill_names()}

    def _cmd_system(self, payload: dict[str, Any]) -> dict[str, Any]:
        """What an agent may do on this machine, described for the person granting it.

        A *description* command, not a second source of truth: the console renders what this returns,
        so the two cannot disagree about what a switch does. The alternative — the prose living in
        Swift as well — drifts the first time a capability changes, and the failure mode is a console
        confidently describing a grant it does not enforce.

        Reports three things and keeps them separate:

        - `capabilities` — every declared grant with what it reaches, what changes, and its caution.
        - `enabled` / `full_access` — the two switches, so the console can show *why* the list is
          inert when the section is off.
        - `unavailable` — grants with no tool behind them yet, so a console can survive the tree being
          mid-build rather than offering a switch that does nothing.

        Read-only, and safe on a config that will not load: a console asking what a capability means
        should get an answer even when the engine is unhappy about something else.

        The assembly is `syscap.console_payload`, not a literal here, because the CLI's `system list`
        answers the same question and the whole point of `syscap` is that the two surfaces cannot
        render one grant two ways. This method is now only the choice of *whose* grants to report;
        the console asks about the capability set in the abstract, so it passes none.
        """
        from .syscap import console_payload, summary

        payload = console_payload(getattr(self.config, "system", None))
        # The acting holder, so the console can answer "and what may *you* do?" beside "what may an
        # agent do?" — the two readings the panel shows as separate figures. Without it the app could
        # only count the roster, which is how one machine came to report 6 of 12 in the app and 12 of
        # 12 in the terminal for the same config: the terminal adds this holder itself
        # (`systemcli.Holder`), and this reply did not carry one at all.
        #
        # The person's holder, not a named agent: a console asking about the capability set in the
        # abstract is acting as the person, which is what `_holder_for("")` returns.
        holder = self._holder_for("")
        payload["holder"] = holder.as_dict()
        # ...and the holder's own counts, *replacing* the description's abstract one. `console_payload`
        # reports the capability set with no holder, so its `summary` reads "0 of 12 … granted" — which
        # is false on a machine whose holder holds everything, and was printed as a headline for a
        # while. `systemcli.cmd_list` replaces it the same way, for the reason its own comment gives:
        # two counts in one document is how a reader picks the wrong one.
        counts = summary(holder.capabilities)
        payload["summary"] = counts["text"]
        payload["granted"] = counts["granted"]
        payload["granted_count"] = counts["granted_count"]
        payload["total"] = counts["total"]
        return payload

    def _cmd_system_consent(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Approve or withdraw one state-changing tool, for one holder, in this run's ledger.

        The console's counterpart to the CLI's `system consent grant|revoke`. Both reach the same
        function (`sysctl_tools.grant_consent` / `revoke_consent`), which is what makes an approval
        given in the app visible to the CLI and to an agent mid-run — one ledger, one decision.

        `by` is the principal, never an agent. `grant_consent` refuses a `by` naming an agent
        (`ag_*`) precisely so an approval cannot be self-issued, and that guard is left to the module
        rather than re-implemented here: a second copy of the rule is a second place for it to be
        wrong. The refusal is reported as a `ServerError` carrying the module's own message.
        """
        from .sysctl_tools import ConsentError, grant_consent, revoke_consent

        tool = str(payload.get("tool") or "").strip()
        if not tool:
            raise ServerError("system_consent needs a tool")
        holder = str(payload.get("agent_id") or payload.get("holder") or "").strip()
        if not holder:
            raise ServerError("system_consent needs an agent_id: an approval is per agent, and a grant "
                              "with no holder would read as a grant to everyone")
        approved = bool(payload.get("approved", True))
        note = str(payload.get("note") or "")
        fn = grant_consent if approved else revoke_consent
        try:
            fn(self._state_dir(), tool=tool, agent_id=holder, by=self._principal_id(), note=note)
        except ConsentError as exc:
            raise ServerError(str(exc)) from exc
        return {"tool": tool, "agent_id": holder, "approved": approved,
                "system": self._cmd_system({})}

    def _cmd_system_invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one capability through the *same* gate the agents go through, and return its result.

        A read-only panel tells a person what they could allow but not what it does, which leaves the
        hardest question — "is this grant safe to hand over?" — unanswered at exactly the moment it is
        asked. This is the console's answer, and it is deliberately not a second implementation:
        `ToolRegistry.call` is the one gate, so an invocation from the panel obeys `system.enabled`,
        the holder's grant, the allowlists, the consent ledger and the bounds *identically* to the
        same call from an agent mid-run. A panel with its own shortcut would be a way to reach the
        machine that the ledger does not record.

        A refusal is returned as a normal result with `refused: true` rather than raised: the reason is
        the useful part, and the console shows it verbatim. Raising would reach the UI as a generic
        failure and lose the sentence that says which grant to add.
        """
        tool = str(payload.get("tool") or "").strip()
        if not tool:
            raise ServerError("system_invoke needs a tool")
        raw_args = payload.get("args")
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        holder = str(payload.get("agent_id") or "").strip()
        try:
            registry = self._system_registry(holder)
        except Exception as exc:  # noqa: BLE001 - an unusable holder is a refusal, not a crash
            raise ServerError(str(exc)) from exc
        if tool not in registry.names():
            # Named rather than silent: a tool that is not offered is usually `system.enabled` being
            # off or the catalogue not knowing the name, and both need different fixes.
            raise ServerError(
                f"{tool!r} is not offered to this holder, so it cannot be invoked. Either the machine "
                f"tools are off (`system.enabled`), or {tool!r} is not a tool this build knows.")
        result = registry.call(tool, args)
        text = getattr(result, "text", str(result))
        return {"tool": tool, "args": args, "text": text,
                "refused": "refused:" in text,
                "ok": not getattr(result, "is_error", False) and "refused:" not in text}

    def _system_registry(self, holder: str) -> Any:
        """A tool registry bound to one holder, for an invocation from the console.

        Built per call rather than cached: a cached registry would keep a grant list from before the
        last `system_set`, so a person who had just granted a capability would be refused by the
        registry still holding the previous answer.

        The holder is turned into the same minimal shape the executor passes — an object with `id` and
        `capabilities` — rather than a second code path. `ToolRegistry` reads `agent.capabilities` to
        decide what to advertise and `agent.id` to key the consent ledger, so giving it the real
        roster entry for a named holder and a wildcard holder for the person keeps the panel on
        exactly the path an agent takes.
        """
        from .tools import ToolRegistry

        agent = self._holder_for(holder)
        return ToolRegistry(workspace_root=self._project_dir(), agent=agent,
                            system=self._system_section())

    def _holder_for(self, holder: str) -> Any:
        """The agent a console invocation acts as: the named one, or the person themselves.

        The person's holder carries `system:*` because a command the person types *is* the grant —
        the same rule `systemcli`'s Owner holder follows, so the app and the terminal cannot disagree
        about what the person may do. A named holder gets exactly its own capabilities, so choosing an
        agent in the panel is a way to *test that agent's* scope rather than to borrow the person's.

        `Holder` is imported from `systemcli` rather than building an `AgentSpec` here: that type exists
        precisely for this (a *view* of who is acting, carrying the two attributes `ToolRegistry` reads
        and an explanation of where the grants came from), and constructing a real `AgentSpec` would
        need a skill list it does not have — an agent with no skills is unassignable by design, so the
        person would have been refused for a reason that has nothing to do with permissions.
        """
        from .systemcli import Holder

        if holder:
            people = self._people()
            org = people.load(project=self._project_dir())
            for candidate in (getattr(org, "agents", None) or {}).values():
                if holder in (getattr(candidate, "id", ""), getattr(candidate, "name", "")):
                    return candidate
            raise ServerError(
                f"no agent named {holder!r} in this project's roster. Run `engine.cli agents` to "
                "see who exists, or omit agent_id to act as yourself.")
        return Holder(id="ag_owner", name="Owner", capabilities=("system:*",),
                      why="the console's own holder — a command you type is the grant")

    def _system_section(self) -> Any:
        """The live `[system]` section, so the registry enforces the current switches."""
        return getattr(self.config, "system", None)

    def _state_dir(self) -> Any:
        """Where this workspace keeps its ledger and run state."""
        return getattr(self.workspace, "state_dir", None) or (self.workspace.path / ".agent_state")

    def _principal_id(self) -> str:
        """Who is operating this console. A person, so the ledger records an attributable approval."""
        for attr in ("principal", "principal_id", "owner"):
            value = getattr(self, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "pr_owner"

    def _people(self) -> Any:
        """A roster manager over this workspace's project root.

        Built fresh per call so a hire or an edit always reads the current file — a cached manager
        would happily re-write a roster that another change had already moved on from.
        """
        from .catalog import ModelCatalog
        from .people import People
        from .providers.registry import build_providers

        providers, _ = build_providers(self.config)
        return People(library=self.library, config=self.config,
                      catalog=ModelCatalog(self.config, providers),
                      project=self._project_dir())

    def _project_dir(self) -> Any:
        """The project the console is pointed at — the directory a roster is discovered from.

        `workspace.path`, not `workspace.root`: for an attached project the root is the *parent*
        directory, so a hire would land one level above the repository and the user would never find
        it.
        """
        return getattr(self.workspace, "path", self.workspace.root)

    def _roster_file(self) -> Any:
        """The exact file a hire writes, so the console can show it rather than describe it vaguely."""
        from . import usercfg

        return usercfg.roster_path(project=self._project_dir())

    def _sync_org(self) -> None:
        """Point a loaded run's orchestrator at the refreshed roster.

        Without this a run already holding an `Org` keeps the old object, so a just-hired agent is
        absent from the run that is about to use it — the "I hired it and nothing changed" failure.
        """
        if self.orchestrator is not None:
            self.orchestrator.org = self._people().load(project=self._project_dir())

    def _hire_request(self, payload: dict[str, Any]) -> Any:
        """Turn a console payload into a validated hire request."""
        from .people import HireRequest

        return HireRequest(
            name=str(payload.get("name") or ""),
            skill=str(payload.get("skill") or ""),
            provider=str(payload.get("provider") or ""),
            model=str(payload.get("model") or ""),
            context_window=(int(payload["context_window"]) if payload.get("context_window") else None),
            level=str(payload.get("level") or "senior"),
            role=str(payload.get("role") or "worker"),
            team=str(payload.get("team") or ""),
            title=str(payload.get("title") or ""),
            purpose=str(payload.get("purpose") or ""),
            max_concurrency=int(payload.get("max_concurrency") or 1),
            as_reviewer=bool(payload.get("as_reviewer")),
            # What the agent may reach. An absent or empty list means "use the skill's default", which
            # is the engine's own least-privilege rule — so a console that never sends this field
            # cannot accidentally widen an agent, and one that does is stating the whole set.
            capabilities=[str(c) for c in (payload.get("capabilities") or []) if str(c).strip()],
        )

    def _skill_names(self) -> list[str]:
        """The skill names the library exposes, sorted. Empty when no library is attached.

        Built through a skill *source* rather than off the `Library` handle, because the names live in
        the source (the flat discovery layer plus the nested tree and the Owner's own roots) — reading
        the handle would return nothing and the hire form would offer an empty skill picker.
        """
        try:
            from .orchestrator import _skill_source

            source = _skill_source(self.library, project=self._project_dir())
            return sorted(str(name) for name in source.names())
        except Exception:  # noqa: BLE001 - a missing library yields an empty list, not a crash
            return []

    def _cmd_swarm(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The current swarm picture: any fan-out in flight, and the last vote tally.

        The console needs this as a *snapshot* command, not only as events, for the same reason the
        run status is polled: a UI that only listens can miss a transition and then show a stale
        swarm forever.
        """
        return {
            "fanout": getattr(self, "_last_fanout", None),
            "vote": getattr(self, "_last_vote", None),
            "running": bool(getattr(self, "_fanout_running", False)),
        }

    def _goal_orchestrator(self) -> Any:
        """The orchestrator a goal command acts on, created and subscribed if necessary.

        One accessor, because every goal command needs the same two things — an orchestrator that
        exists, and a bus the app is subscribed to. Doing it ad hoc per handler is how a command ends
        up mutating state silently.
        """
        orch = self.orchestrator
        if orch is None:
            orch = self._orchestrator(self.slug)
        self._forward_bus(orch)
        return orch

    def _cmd_goal_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set the objective and arm the loop, from the console.

        Accepts the per-goal autonomy flags, so the console's "human gate on/off" switch is one call:
        an unchanged objective with a changed policy replaces the policy and keeps the history, rather
        than forcing a clear-and-restart.
        """
        from .goal import GoalPolicy, Posture

        objective = str(payload.get("objective") or payload.get("goal") or "").strip()
        if not objective:
            raise ServerError("goal_set needs an objective")
        orch = self._goal_orchestrator()
        armed = not bool(payload.get("no_arm"))
        base = orch._default_goal_policy()
        # `posture` is the one word the console sends now; `human_gate` is still accepted so an older
        # build of the app keeps working. A posture in the payload wins over the legacy flag.
        posture = payload.get("posture")
        if posture:
            try:
                resolved = Posture(str(posture).strip().lower())
            except ValueError as exc:
                raise ServerError(
                    f"unknown posture {posture!r}; expected one of "
                    f"{', '.join(p.value for p in Posture)}") from exc
        else:
            resolved = (Posture.SUPERVISED if payload.get("human_gate")
                        else base.posture)
        policy = GoalPolicy(
            auto_approve=(base.auto_approve if payload.get("auto_approve") is None
                          else bool(payload.get("auto_approve"))),
            auto_hire=(base.auto_hire if payload.get("auto_hire") is None
                       else bool(payload.get("auto_hire"))),
            persist_hires=(base.persist_hires if payload.get("persist_hires") is None
                           else bool(payload.get("persist_hires"))),
            posture=resolved,
        )
        orch.goal_set(objective, armed=armed, by="app", policy=policy)
        return self._goal_detail(orch)

    def _cmd_proposals(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every proposal the improver has produced, newest first, plus what it refused.

        Both halves matter. A list that showed only promotions would hide the boundary working, and the
        refusals are how a person sees that a fix aimed at the eval gate was stopped rather than
        quietly dropped.
        """
        from .improver import PROPOSALS_DIRNAME

        directory = self.workspace.state_dir / PROPOSALS_DIRNAME
        proposals: list[dict[str, Any]] = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                data["file"] = str(path.with_suffix(".md"))
                proposals.append(data)
        proposals.sort(key=lambda p: str(p.get("at") or ""), reverse=True)
        # Each entry carries its lifecycle — whether `apply` would proceed, and the reason when it
        # would not — computed by the same module the CLI's `apply` uses. The panel offers a button on
        # `can_apply`, and a Swift copy of that rule would be offered whenever this one refuses.
        from .proposals import ProposalStore, annotate

        annotate(proposals, ProposalStore(workspace=self.workspace))

        refused: list[dict[str, Any]] = []
        rejected_path = directory / "rejected.jsonl"
        if rejected_path.is_file():
            for line in rejected_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    refused.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return {
            "proposals": proposals,
            "count": len(proposals),
            "refused": refused[-20:],
            "refused_count": len(refused),
            "directory": str(directory),
            # Stated so the console can say it plainly rather than implying otherwise.
            "applies_changes": False,
        }

    # ── the proposal lifecycle, driven from the console ─────────────────────
    #
    # Named `proposal_*` rather than a second `proposals` verb: the read command above is what the
    # status poll calls, and each of these is a distinct act a person takes on one proposal. The
    # implementation is `engine.proposals`, the same module the CLI drives, because two surfaces that
    # each decided when a change may land would eventually disagree about it.

    def _proposal_store(self) -> Any:
        from .proposals import ProposalStore

        return ProposalStore(workspace=self.workspace)

    def _cmd_proposal_show(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One proposal in full, with its patch inline so the panel can read a diff without a file."""
        from .proposals import ProposalLifecycleError

        store = self._proposal_store()
        try:
            proposal = store.load(str(payload.get("id") or ""))
        except ProposalLifecycleError as exc:
            raise ServerError(str(exc)) from exc
        return {
            **proposal.as_dict(),
            "file": str(store.directory / f"{proposal.proposal_id}-{proposal.finding.kind}.md"),
            "can_apply": store.can_apply(proposal),
            "why_not": store.why_not(proposal),
        }

    def _cmd_proposal_accept(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .proposals import ProposalLifecycleError

        store = self._proposal_store()
        try:
            proposal = store.accept(str(payload.get("id") or ""), by="app")
        except ProposalLifecycleError as exc:
            raise ServerError(str(exc)) from exc
        return {"proposal_id": proposal.proposal_id, "state": proposal.state, "applied": False,
                **self._cmd_proposals({})}

    def _cmd_proposal_reject(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .proposals import ProposalLifecycleError

        store = self._proposal_store()
        try:
            proposal = store.reject(str(payload.get("id") or ""),
                                    reason=str(payload.get("reason") or ""), by="app")
        except ProposalLifecycleError as exc:
            raise ServerError(str(exc)) from exc
        return {"proposal_id": proposal.proposal_id, "state": proposal.state,
                "reason": proposal.refusal, "applied": False, **self._cmd_proposals({})}

    def _cmd_proposal_apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply one proposal — the only console command that writes to the working tree.

        Runs on the worker thread like every other command, which matters here: the suite runs twice,
        and the read loop stays free so the console keeps answering while it does.

        A refusal raises rather than returning `applied: false`, because the app's `mutate` helper
        already turns a refusal into a notice and the person needs to read the reason. The refusal is
        also *safe to raise*: it is decided before anything is written.
        """
        from .proposals import ProposalLifecycleError

        store = self._proposal_store()
        try:
            outcome = store.apply(str(payload.get("id") or ""), by="app",
                                  force=bool(payload.get("force")))
        except ProposalLifecycleError as exc:
            raise ServerError(str(exc)) from exc
        if outcome.refused:
            raise ServerError(outcome.refused)
        return {**outcome.as_dict(), **self._cmd_proposals({})}

    def _cmd_proposal_undo(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .proposals import ProposalLifecycleError

        store = self._proposal_store()
        try:
            outcome = store.undo(str(payload.get("id") or ""), by="app")
        except ProposalLifecycleError as exc:
            raise ServerError(str(exc)) from exc
        if outcome.refused:
            raise ServerError(outcome.refused)
        return {**outcome.as_dict(), **self._cmd_proposals({})}

    def _cmd_improve(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one improver cycle, in the background, and report what it considered.

        Runs on this worker thread, which is what makes it *background*: the read loop stays free, so
        the console keeps answering while the cycle runs. Nothing is applied — the cycle's whole output
        is proposals on disk — so there is no state left half-changed if the app is quit mid-cycle.
        """
        from .improver import Improver

        improver = Improver(
            workspace=self.workspace,
            memory=getattr(self, "_memory", None),
        )
        considered = improver.run_once()
        self.emit(Event(seq=0, type=EventType.AGENT_LOG,
                        payload={"text": f"improver: considered {len(considered)} finding(s)",
                                 "stream": "stderr"}))
        return {
            "considered": [
                {"id": p.proposal_id, "kind": p.finding.kind, "state": p.state,
                 "improved": p.validation.improved, "regressions": p.validation.regressions,
                 "refusal": p.refusal}
                for p in considered
            ],
            "count": len(considered),
            "applies_changes": False,
            **self._cmd_proposals({}),
        }

    def _cmd_goal_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The goal picture. Answered even with no orchestrator, so the panel renders on first open."""
        orch = self.orchestrator
        if orch is None:
            return {"goal": {"objective": "", "state": "cleared", "live": False, "open": False,
                             "budget_enabled": False, "token_budget": 0, "pause_reason": "",
                             "summary": "", "blocked_reason": "", "slice": {}, "spend": {},
                             "history": []}}
        return {"goal": orch.goal_status()}

    def _cmd_subagent_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Page one child's transcript from the console.

        The same byte-addressed read the agent's `read_subagent_result` tool performs, exposed so a
        person can inspect what a child actually did without the panel holding the whole transcript.
        """
        from .subagents import ChildStore, SubagentError

        child_id = str(payload.get("child_id") or "").strip()
        if not child_id:
            raise ServerError("subagent_result needs a child_id")
        run_id = ""
        orch = self.orchestrator
        if orch is not None and getattr(orch, "_run", None) is not None:
            run_id = getattr(orch._run, "run_id", "") or ""
        store = ChildStore(self.workspace, run_id=run_id or "run")
        try:
            page = store.read(child_id=child_id,
                              offset_bytes=int(payload.get("offset_bytes") or 0),
                              limit_bytes=int(payload.get("limit_bytes") or 0))
        except SubagentError as exc:
            raise ServerError(str(exc)) from exc
        return page.as_dict()

    def _cmd_subagents(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every child this run started, as reference frames, for the Work panel's tree.

        Read from disk rather than held in memory, so the tree survives a page refresh and shows what
        actually ran — the console is a window onto the engine, not a second copy of its state.
        """
        from .subagents import ChildStore

        run_id = ""
        orch = self.orchestrator
        if orch is not None and getattr(orch, "_run", None) is not None:
            run_id = getattr(orch._run, "run_id", "") or ""
        store = ChildStore(self.workspace, run_id=run_id or "run")
        children = [ref.as_dict() for ref in store.children()]
        return {
            "children": children,
            "count": len(children),
            "running": sum(1 for c in children if c.get("status") == "running"),
            "failed": sum(1 for c in children if c.get("status") == "failed"),
        }

    def _cmd_goal_pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        if orch.goal() is None:
            raise ServerError("no goal is set")
        orch.goal_pause()
        return self._goal_detail(orch)

    def _cmd_goal_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        if orch.goal() is None:
            raise ServerError("no goal is set")
        orch.goal_resume(by="app")
        return self._goal_detail(orch)

    def _cmd_goal_clear(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        if orch.goal() is None:
            raise ServerError("no goal is set")
        orch.goal_clear()
        return self._goal_detail(orch)

    def _goal_detail(self, orch: Any) -> dict[str, Any]:
        """The command acknowledgement shape, so the app can update its row without re-polling."""
        return {"goal": orch.goal_status()}

    # ── mission ─────────────────────────────────────────────────────────────

    def _cmd_mission(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The mission snapshot: the standing purpose, its objectives and progress.

        A snapshot command rather than only events, for the same reason the goal is one: a UI that
        only listens can miss a transition and show a stale mission forever.
        """
        orch = self.orchestrator
        goal = orch.mission_status() if orch is not None else None
        if goal is None:
            return {"mission": {"statement": "", "state": "empty", "objectives": [],
                                "progress": {"done": 0, "total": 0, "next": ""}}}
        return {"mission": goal}

    def _cmd_mission_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        statement = str(payload.get("statement") or payload.get("mission") or "").strip()
        if not statement:
            raise ServerError("mission_set needs a statement")
        objectives = [str(o) for o in (payload.get("objectives") or []) if str(o).strip()]
        orch.mission_set(statement, objectives=objectives, armed=bool(payload.get("arm")))
        return self._mission_detail(orch)

    def _cmd_mission_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        text = str(payload.get("objective") or "").strip()
        if not text:
            raise ServerError("mission_add needs an objective")
        at = payload.get("at")
        orch.mission_add(text, at=int(at) if at is not None else None)
        return self._mission_detail(orch)

    def _cmd_mission_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_remove(int(payload.get("index") or 0))
        return self._mission_detail(orch)

    def _cmd_mission_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Hand an objective to a goal — the one place the mission decides what gets worked."""
        orch = self._goal_orchestrator()
        index = payload.get("index")
        detail = orch.mission_start(index=int(index) if index is not None else None,
                                    armed=not bool(payload.get("no_arm")), by="app")
        return detail

    def _cmd_mission_advance(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_advance(summary=str(payload.get("summary") or ""))
        return self._mission_detail(orch)

    def _cmd_mission_mark(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_mark(int(payload.get("index") or 0), str(payload.get("state") or "done"),
                          summary=str(payload.get("summary") or ""))
        return self._mission_detail(orch)

    def _cmd_mission_arm(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_arm(by="app")
        return self._mission_detail(orch)

    def _cmd_mission_pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_pause()
        return self._mission_detail(orch)

    def _cmd_mission_clear(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_clear()
        return self._mission_detail(orch)

    def _mission_detail(self, orch: Any) -> dict[str, Any]:
        return {"mission": orch.mission_status(), "goal": orch.goal_status()}

    # ── portfolio: one principal, several orgs ──────────────────────────────

    def _register_stamp(self) -> tuple[int, int] | None:
        """The register file's identity right now — ``(size, mtime_ns)``, or None when it is absent.

        Only ever used to notice that **another process** rewrote the file: see `_load_portfolio`.
        """
        from .portfolio import Portfolio

        try:
            stat = Portfolio.path_for().stat()
        except OSError:
            return None
        return (stat.st_size, stat.st_mtime_ns)

    def _load_portfolio(self) -> Any:
        """The portfolio, loaded once — and re-read whenever the file has moved on without us.

        None when there is none (a single-org setup).

        **Loaded is marked on success only.** It used to be marked *before* the attempt, which turned one
        transient read failure into a permanent one: the failure was remembered as "there is no
        portfolio" for the life of the process, and the next `portfolio_add` took that as licence to
        build `Portfolio.new()` and `save()` it — replacing a register full of orgs with a one-org file.
        A register that could not be read is not a register that is absent, so the read is retried on
        the next command, and `_portfolio_error` remembers *why* it failed for the commands that must
        refuse rather than act on it.

        **Re-read when the file changed.** The register is one file with several writers: the CLI's
        `portfolio` commands (and `portfolio remove` among them), and any other `serve` on the same
        machine. This used to be read once and then answered from memory for the life of the process,
        so an org removed from a terminal went on being listed here, `active_org_id` went on naming an
        org the register no longer held, and every panel went on describing it. The console was a
        second copy of the register that could not be corrected — which is what "the engine is the
        single source of truth" exists to forbid. A stat pair rather than a read, so the common case
        (an unchanged register, asked once per 2s poll) still costs nothing.
        """
        # Read *before* the file, deliberately: a write landing between this stat and the read leaves
        # the recorded stamp older than the content, which the next call re-reads. The other order
        # would record a stamp for content never read, which is a change nobody would ever notice.
        stamp = self._register_stamp()
        if self._portfolio_loaded and stamp == self._portfolio_stamp:
            return self._portfolio
        from .portfolio import Portfolio, PortfolioError

        try:
            # In place when an object is already held — the fleet and every reader have it, and
            # swapping it would leave one of them acting on a register the file no longer agrees with.
            portfolio = (self._portfolio.reload() if self._portfolio is not None
                         else Portfolio.load())
        except PortfolioError as exc:
            # A broken register must not kill the server — but it must not be papered over either.
            _log(f"serve: cannot read the portfolio: {exc}")
            self._portfolio_error = str(exc)
            # The stamp is deliberately left alone: a register that could not be read is retried on
            # the next command rather than remembered as read.
            return None
        self._portfolio_error = ""
        # A register that has gone from disk is *no register*, and holding the orgs it used to have
        # would be the stale copy this method exists to prevent. `_cmd_portfolio_add` then builds a new
        # one, which is what "the file is gone" licenses and what a failed read must never license.
        self._portfolio = portfolio
        self._portfolio_loaded = True
        self._portfolio_stamp = stamp
        return self._portfolio

    def _fleet_for(self, portfolio: Any) -> Any:
        """The fleet for this server, built once so its org runtimes persist across commands.

        Built once rather than per command because the fleet *is* the concurrency: an org left running
        on its thread must survive the next command, so the object that holds it must too.
        """
        from .fleet import Fleet

        if self._fleet is None:
            self._fleet = Fleet(config=self.config, library=self.library, portfolio=portfolio,
                                on_event=self._fleet_event)
        return self._fleet

    def _fleet_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Forward a fleet event to the console, so org starts and finishes are visible live."""
        try:
            self.emit({"type": kind, **payload})
        except Exception:  # noqa: BLE001 - an emit failure must not break the fleet
            pass

    def _cmd_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The portfolio snapshot: the principal and every org, loaded or not.

        A snapshot command rather than only events, so the console shows the whole portfolio from the
        poll it already makes. It does **not** load every org — that is `_cmd_portfolio_live` — because
        reading ten rosters on every 2s poll would be wasteful; the register is enough to list them.

        `error` travels in **both** branches, like every other key here: a register that could not be
        read renders as an empty one otherwise, and "no orgs" is precisely what invites a person to
        create one — which is the overwrite `_cmd_portfolio_add` now refuses. An empty string means the
        read succeeded (or there is no register, which is not an error).
        """
        portfolio = self._load_portfolio()
        if portfolio is None:
            return {"portfolio": None, "principal": None, "active_org_id": "", "orgs": [],
                    "error": self._portfolio_error,
                    "counts": {"orgs": 0, "enabled": 0, "missing": 0}}
        return {
            "portfolio": {"principal": portfolio.principal.as_dict(),
                          "active_org_id": portfolio.active_org_id,
                          "counts": portfolio.inspect()["counts"]},
            "principal": portfolio.principal.as_dict(),
            "active_org_id": portfolio.active_org_id,
            "orgs": portfolio.inspect()["orgs"],
            "error": "",
        }

    def _cmd_portfolio_live(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The live cross-org view: load every org and gather its mission, spend and blockers.

        Expensive by nature — it builds an orchestrator per org — so it is a deliberate command the
        Portfolio panel sends when open, not part of the status poll.
        """
        portfolio = self._load_portfolio()
        if portfolio is None:
            return {"rollup": None, "fleet": None}
        fleet = self._fleet_for(portfolio)
        for entry in portfolio.orgs:
            try:
                fleet._runtime_for(entry.id)
            except Exception as exc:  # noqa: BLE001 - one bad org must not blank the view
                _log(f"serve: org {entry.slug} failed to load: {exc}")
        return {"rollup": fleet.rollup(), "fleet": fleet.status()}

    def _cmd_portfolio_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one org from the console, in parallel with any other org already running."""
        portfolio = self._load_portfolio()
        if portfolio is None:
            raise ServerError("no portfolio; add an org from the CLI first")
        ref = str(payload.get("org") or "").strip()
        if not ref:
            raise ServerError("portfolio_run needs an org")
        fleet = self._fleet_for(portfolio)
        try:
            handle = fleet.run_org(ref, goal=str(payload.get("goal") or ""),
                                   manifest=str(payload.get("manifest") or ""),
                                   background=bool(payload.get("background", True)))
        except Exception as exc:  # noqa: BLE001 - a refusal is a result, said plainly
            raise ServerError(str(exc)) from exc
        return {"handle": handle.as_dict(), "fleet": fleet.status()}

    def _cmd_portfolio_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        portfolio = self._load_portfolio()
        if portfolio is None:
            raise ServerError("no portfolio")
        ref = str(payload.get("org") or "").strip()
        fleet = self._fleet_for(portfolio)
        try:
            detail = fleet.stop_org(ref)
        except Exception as exc:  # noqa: BLE001
            raise ServerError(str(exc)) from exc
        return {"stopped": detail, "fleet": fleet.status()}

    def _cmd_portfolio_select(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Select the active org — the one bare console commands act on.

        Selecting has to do more than write a field. `active_org_id` is a *label*: the server's own
        `workspace` and `orchestrator` are what every bare command and every panel actually reads, and
        they were built once at startup for the project the console was launched on. So switching used
        to change the name in the register while the roster, the mission, the goal and the run history
        on screen all went on describing the *previous* org — a switch that looked like it worked and
        changed nothing a person could see. That is the complaint in one line.

        So this re-points the server as well: the org's folder becomes the workspace, and the
        orchestrator is dropped so the next bare command rebuilds against it. Rebuild-on-next-use
        rather than building here, because building loads a roster and a workspace and this command is
        on the path a person clicks a switcher — the cost belongs to the command that needs it.

        A run already in flight is a **refusal, not a re-root**. The executing subprocess holds the
        workspace path, so mutating it underneath a live run is how half the artifacts of one org land
        in another's folder. `portfolio run` is the parallel path and does not touch this server's own
        run, so a person running several orgs at once is never blocked by that — only a bare `start`
        run is, and for that the answer is to stop it first.
        """
        portfolio = self._load_portfolio()
        if portfolio is None:
            raise ServerError("no portfolio")
        ref = str(payload.get("org") or "").strip()
        try:
            entry = portfolio.set_active(ref)
            portfolio.save()
        except Exception as exc:  # noqa: BLE001
            raise ServerError(str(exc)) from exc

        detail: dict[str, Any] = {"active_org_id": entry.id, "active_org": entry.as_dict(),
                                  "repointed": False, "repoint_reason": ""}
        if self._has_live_run():
            detail["repoint_reason"] = (
                "a run is in flight in this window, so the engine is still pointed at it; "
                "stop it to switch")
            return detail
        try:
            workspace = self._workspace_for_org(entry)
        except Exception as exc:  # noqa: BLE001 - a missing folder is reported, not fatal
            # The register still moves: `portfolio use` from the CLI does the same, and refusing the
            # whole switch would make a broken org impossible to select away *from*. What must not
            # happen is silence about it — the console then says the engine stayed put and why.
            detail["repoint_reason"] = str(exc)
            return detail
        workspace.ensure()
        self.workspace = workspace
        self.slug = entry.slug
        # Dropped rather than rebuilt: an orchestrator holding the old workspace would answer the next
        # bare command from the previous org, which is the failure this whole method exists to prevent.
        self.orchestrator = None
        detail["repointed"] = True
        detail["workspace"] = self._workspace_info()
        return detail

    def _has_live_run(self) -> bool:
        """Whether this server's own orchestrator has a run in flight.

        Asked through `status` because that is the orchestrator's own answer, and a second liveness
        rule written here would be a copy that drifts from the one the UI renders.
        """
        orch = self.orchestrator
        if orch is None:
            return False
        try:
            return bool(orch.status().get("running"))
        except Exception:  # noqa: BLE001 - an unreadable run is not a reason to refuse a switch
            return False

    def _workspace_for_org(self, entry: Any) -> Any:
        """The workspace an org runs in: its own folder, or a managed project keyed on its slug.

        The same rule the fleet uses, so an org looked at through the switcher and an org run in
        parallel resolve to one directory rather than two — one resolver, `portfolio.workspace_for`,
        rather than a copy here that could drift from the fleet's.
        """
        from .portfolio import workspace_for

        # The same managed root this server was started with, so an org registered with no folder
        # lands under the projects directory the console is already reading rather than beside it.
        return workspace_for(entry, root=getattr(self.workspace, "root", None))

    def _cmd_portfolio_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Register an org from the console.

        **Refused when the register could not be read.** The `None` branch used to build
        `Portfolio.new()` and `save()` it unconditionally, and `Portfolio.save` replaces the file — so a
        single failed read (a transient I/O error, a register another process was rewriting, a corrupt
        byte) meant the next `Add org` replaced a register of ten orgs with a one-org file. The user's
        orgs are the one thing this command must not be able to destroy, so the read failure is carried
        up as a refusal: nothing is written, and the reason names the failure rather than the symptom.
        """
        from .portfolio import Portfolio

        portfolio = self._load_portfolio()
        if portfolio is None:
            if self._portfolio_error:
                raise ServerError(
                    f"the portfolio could not be read ({self._portfolio_error}), so adding an org "
                    "would replace it; fix or move the register first")
            portfolio = Portfolio.new()
            self._portfolio = portfolio
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ServerError("portfolio_add needs a name")
        try:
            entry = portfolio.add_org(
                name=name, slug=str(payload.get("slug") or ""),
                path=str(payload.get("path") or ""), charter=str(payload.get("charter") or ""),
                daily_budget_usd=float(payload.get("daily_budget_usd") or 0.0),
                make_active=bool(payload.get("active")))
            portfolio.save()
        except Exception as exc:  # noqa: BLE001
            raise ServerError(str(exc)) from exc
        return {"org": entry.as_dict(), "portfolio": self._cmd_portfolio({})["portfolio"]}

    def _cmd_portfolio_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Forget an org from the register. **Its folder and run history are left on disk.**

        That is the engine's own documented behaviour (`portfolio.remove_org` and the CLI's
        `portfolio remove` help both say it), and it is the fact a confirmation has to state before a
        person agrees — "remove" that quietly leaves gigabytes behind is the surprise this method's
        caller must not create.

        Re-pointing when the *active* org is the one removed is not cosmetic. `remove_org` reassigns
        `active_org_id` to another entry, but this server's `workspace` and `orchestrator` were built
        for the org that just went — so without this the roster, mission, goal and run history would go
        on describing an org no longer in the register, and the switcher would show nothing selected
        while the panels showed the departed one.
        """
        portfolio = self._load_portfolio()
        if portfolio is None:
            raise ServerError("no portfolio")
        ref = str(payload.get("org") or "").strip()
        was_active = False
        try:
            entry = portfolio.org(ref)
            was_active = entry.id == portfolio.active_org_id
            entry = portfolio.remove_org(ref)
            portfolio.save()
        except Exception as exc:  # noqa: BLE001
            raise ServerError(str(exc)) from exc

        detail: dict[str, Any] = {
            "removed": entry.as_dict(),
            "portfolio": self._cmd_portfolio({})["portfolio"],
            "folder_kept": entry.path,
            "repointed": False, "repoint_reason": "",
        }
        if not was_active:
            return detail
        successor = portfolio.active_org()
        if successor is None:
            # The register is now empty. Nothing to point at, and saying so beats leaving the panels
            # describing an org the person just deleted.
            detail["repoint_reason"] = (
                "the register is now empty; the next org you add becomes the active one")
            self.orchestrator = None
            return detail
        if self._has_live_run():
            detail["repoint_reason"] = (
                f"a run is in flight in this window, so the engine is still pointed at {entry.slug}; "
                "stop it to switch")
            return detail
        try:
            workspace = self._workspace_for_org(successor)
        except Exception as exc:  # noqa: BLE001 - a broken successor must not fail the removal
            detail["repoint_reason"] = str(exc)
            return detail
        workspace.ensure()
        self.workspace = workspace
        self.slug = successor.slug
        self.orchestrator = None
        detail["repointed"] = True
        detail["active_org_id"] = successor.id
        detail["workspace"] = self._workspace_info()
        return detail

    def _cmd_portfolio_removal(self, payload: dict[str, Any]) -> dict[str, Any]:
        """What removing one org would take away, and what it would leave behind.

        A **preview**, asked before the confirmation is shown, so the sentence a person agrees to is
        the engine's account of the consequence rather than a guess written in Swift. It walks the
        org's folder to size it, which is why it is its own command and not a field on the portfolio
        snapshot: that snapshot is on the 2s poll, and a directory walk per org per poll is exactly the
        kind of silent cost this codebase avoids.

        `can_delete_folder` is reported as the engine's **actual** capability, not as a wish. It is
        false today (`portfolio.remove_org` has no delete arm, and the CLI's help says so), and the
        console therefore offers no switch that would not work.
        """
        portfolio = self._load_portfolio()
        if portfolio is None:
            raise ServerError("no portfolio")
        ref = str(payload.get("org") or "").strip()
        try:
            entry = portfolio.org(ref)
        except Exception as exc:  # noqa: BLE001 - a refusal is the answer, said plainly
            raise ServerError(str(exc)) from exc

        path = entry.workspace_path
        if path is None:
            # A managed org names no folder at all. Report that honestly as an empty folder and a null
            # size — never `.`, which `Path()` would have produced and which reads as a real directory
            # the org is about to lose. The console's "it works under the managed projects directory"
            # branch keys off exactly this empty string.
            folder, exists = "", False
        else:
            folder, exists = str(path), path.is_dir()
        return {
            "org": entry.as_dict(),
            "active": entry.id == portfolio.active_org_id,
            "folder": folder,
            "folder_exists": exists,
            # `None` rather than 0 when there is no folder: "no folder to size" and "an empty folder"
            # are different facts, and a 0 here would read as "nothing is on disk".
            "folder_bytes": _directory_bytes(path) if exists else None,
            "folder_kept": True,
            "can_delete_folder": False,
            "can_delete_folder_why": (
                "the engine forgets the register entry and never touches the folder — "
                f"`portfolio remove` leaves {entry.slug}'s state where it is, by design"),
        }

    def _cmd_schedules(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The schedule for this workspace: what is armed, what is due, and what a fire did."""
        from .schedules import ScheduleError, ScheduleStore

        try:
            store = ScheduleStore(self.workspace)
        except ScheduleError as exc:
            raise ServerError(str(exc)) from exc
        view = store.view()
        view["workspace"] = str(getattr(self.workspace, "path", ""))
        return view

    def _cmd_schedule_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Forget one scheduled entry, leaving the rest of the schedule alone.

        Destructive and unrecoverable — `ScheduleStore.remove` says as much, which is why it insists on
        an id when a slug is ambiguous — so the reply carries the removed entry back, whole, for the
        caller to read out. The engine's own refusals (no match, an ambiguous slug) surface unchanged.
        """
        from .schedules import ScheduleError, ScheduleStore

        ref = str(payload.get("schedule_id") or payload.get("ref") or "").strip()
        if not ref:
            raise ServerError("schedule_remove needs a schedule_id")
        try:
            store = ScheduleStore(self.workspace)
            entry = store.remove(ref)
        except ScheduleError as exc:
            raise ServerError(str(exc)) from exc
        return {"removed": entry.as_dict(), "schedule": self._cmd_schedules({})}

    def _cmd_fanout(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Start a fan-out from the console: a template, some items, and go.


        This is the native-app surface for the *fan-out* primitive — the one the engine could already
        do and no UI could reach. The validation is the same as the executor's, because both go
        through `plan_fanout`, so a template the CLI would refuse is refused here with the same words.
        """
        from .fanout import FanoutError, plan_fanout, run_fanout

        template = str(payload.get("template") or "").strip()
        raw_items = payload.get("items") or []
        if not isinstance(raw_items, list):
            raise ServerError("fanout needs an items array")
        items = [str(i) for i in raw_items]
        skill = str(payload.get("skill") or "code-reviewer")
        try:
            plan = plan_fanout(template, items, skill=skill)
        except FanoutError as exc:
            # Refused *before* anything runs, which is the point: a bad template would otherwise
            # cost N duplicate calls.
            raise ServerError(str(exc)) from exc

        orch = self.orchestrator
        if orch is None:
            raise ServerError("start a run first; a fan-out needs a roster to draw agents from")
        agents = [a.id for a in getattr(orch, "org", None).agents.values()
                  if not a.is_human] if getattr(orch, "org", None) is not None else []
        if not agents:
            raise ServerError("the roster has no agents to fan out to")

        concurrency = int(payload.get("max_parallel")
                          or getattr(getattr(self.config, "executor", None),
                                     "fanout_max_parallel", 4))
        self._fanout_running = True
        self._last_fanout = {"count": len(plan), "succeeded": 0, "failed": 0, "running": True,
                             "items": [i.as_dict() for i in plan.items]}

        def _emit(kind: str, event: dict[str, Any]) -> None:
            # Streamed as an event as well as kept in the snapshot, so the panel updates live rather
            # than only on the next poll.
            self.emit(Event(seq=0, type="swarm.fanout", payload={"kind": kind, **event}))
            if kind in ("fanout.item_done", "fanout.item_failed"):
                self._last_fanout["items"] = [i.as_dict() for i in plan.items]
                self._last_fanout["succeeded"] = sum(1 for i in plan.items if i.ok)
                self._last_fanout["failed"] = sum(1 for i in plan.items if not i.ok)

        # A console-driven fan-out calls the model directly rather than spawning a full node run:
        # the panel is asking "review these files", not "build me a graph".
        def _one(item: Any, agent_id: str) -> tuple[str, str, int]:
            from .providers.base import ChatRequest, Message, Role

            gateway = getattr(orch, "gateway", None)
            if gateway is None:
                return "", "no gateway is available for this run", 0
            agent = orch.org.get(agent_id)
            request = ChatRequest(model=agent.model,
                                  messages=[Message.text_message(Role.USER, item.prompt)],
                                  max_tokens=2048)
            response = gateway.complete(request, provider_id=agent.provider, agent_id=agent_id,
                                        node_id=f"fanout:{item.index}")
            usage = response.usage
            tokens = (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
            return response.text, "", tokens

        try:
            run_fanout(plan, _one, agents=agents, max_parallel=concurrency, on_event=_emit)
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised as a crash
            self._fanout_running = False
            raise ServerError(f"the fan-out could not run: {exc}") from exc
        self._fanout_running = False
        summary = plan.summary()
        self._last_fanout = {**summary, "running": False,
                             "items": [i.as_dict() for i in plan.items]}
        return self._last_fanout

    def _cmd_shutdown(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A clean stop: the app asked, so the loop ends rather than being killed.

        The stop flag is what ends the read loop *and* the worker — after this command, which is why
        `_shutdown` answers everything still queued rather than running it: the worker will not take
        another command, so a queued one is a command that will never run, and leaving it unanswered
        would leave the app waiting on it. The reason is recorded here so those refusals can say what
        stopped them.
        """
        self._stop_reason = self._stop_reason or "the app asked the engine to stop (shutdown command)"
        self._stop.set()
        return {"stopping": True}

    # ── helpers ─────────────────────────────────────────────────────────────

    def _orchestrator(self, slug: str) -> Any:
        """Build the orchestrator for a project, reusing the roster the Owner hired."""
        from .bus import EventBus
        from .catalog import ModelCatalog
        from .orchestrator import Orchestrator
        from .people import HireError, People
        from .providers.registry import build_providers
        from .state import Workspace

        # An attached workspace *is* the project: rebuilding from `root`/`slug` would silently point
        # the run at a managed directory beside the user's repository instead of at it.
        if getattr(self.workspace, "is_attached", False) and slug == self.slug:
            workspace = self.workspace
        else:
            workspace = Workspace.for_project(slug, root=self.workspace.root)
        workspace.ensure()
        bus = EventBus(run_id=f"serve_{slug}", trace_path=workspace.trace_path,
                       lifecycle=self.config, lifecycle_slug=workspace.display_name)
        org = None
        try:
            providers, _ = build_providers(self.config)
            org = People(library=self.library, config=self.config,
                         catalog=ModelCatalog(self.config, providers),
                         project=workspace.path).load(project=workspace.path)
        except HireError as exc:
            _log(f"serve: ignoring the user roster: {exc}")
        self.orchestrator = Orchestrator(
            config=self.config, library=self.library, workspace=workspace, bus=bus, org=org)
        self.workspace = workspace
        # Subscribe here rather than at each command that builds an orchestrator. A goal command
        # creates one lazily, so forwarding *only* on `start` meant the first `goal_set` produced
        # events nobody was listening for — the console would show a goal that never announced itself.
        self._forward_bus(self.orchestrator)
        return self.orchestrator

    def _current_run(self) -> tuple[Any, Any | None]:
        """The run a bare run-command acts on — the one already in memory, when there is one.

        `load()` re-reads the checkpoint and, in doing so, **replaces the orchestrator's own run with a
        copy loaded from disk**. That is exactly right on a server that has just started, where the only
        run there is lives on disk. It is wrong while a graph is executing: the run thread holds the
        other object, so an instruction appended to the copy is overwritten by the run's next persist —
        accepted, acknowledged, and then silently lost. One object, or the two writers disagree.

        Reachable now and not before: while the run executed on the command worker, no command could run
        beside it, so this never raced.
        """
        if self.orchestrator is None:
            return None, None
        live = getattr(self.orchestrator, "_run", None)
        if live is not None:
            return self.orchestrator, live
        try:
            return self.orchestrator, self.orchestrator.load(self.slug)
        except Exception:  # noqa: BLE001 - "no run" is reported by the caller, not raised here
            return self.orchestrator, None

    def _roster(self) -> list[dict[str, Any]]:
        org = getattr(self.orchestrator, "org", None)
        if org is None:
            return []
        return org.roster_view()

    def _cost(self) -> dict[str, Any]:
        """Cost for this workspace, read from the run's own budget. Never an invented figure.

        This is the *idle* branch — it is called when no orchestrator is attached, which is exactly
        the state after a run finishes. It used to return a hardcoded `{"runs": 0, "nodes": 0,
        "cost_usd": None}`, so the cost panel emptied the moment a run ended and the person could not
        see what the run they had just watched had cost. The figure lives in the run checkpoint, which
        the executor writes from the agent runtime's running totals (`spent_tokens`/`spent_usd`), so
        read it there rather than restating zero.

        `cost_usd` is `None` unless a positive total is recorded. A stored `0.0` does not distinguish
        "the provider never reported a price" from "this genuinely cost nothing", and the app renders
        `None` as *cost unknown* — so the ambiguous zero reports as unknown rather than as free. That
        is the same rule the ledger's own `cache_saving_usd` follows: unreported is not zero.
        """
        checkpoint = None
        try:
            checkpoint = self.workspace.load_checkpoint()
        except Exception:  # noqa: BLE001 - an unreadable checkpoint is "no data", not a failure
            checkpoint = None
        if checkpoint is None:
            return {"runs": 0, "nodes": 0, "cost_usd": None}
        budget = getattr(checkpoint, "budget", None) or {}
        try:
            usd = float(budget.get("usd_used") or 0.0)
        except (TypeError, ValueError):
            usd = 0.0
        # `cost_unreported_spans` is deliberately absent rather than 0: this branch has no ledger to
        # ask, and the app reads a missing key as "no claim" while a present 0 would assert that every
        # span reported its usage — a claim nothing here can support.
        return {"runs": 1, "nodes": len(getattr(checkpoint, "nodes", None) or {}),
                "cost_usd": usd if usd > 0 else None}

    def _forward_bus(self, orch: Any) -> None:
        """Forward the run's own events to the app.

        Tapping the existing bus rather than building a second stream is what keeps the console and
        the engine from disagreeing about what happened: there is one producer of events.
        """
        bus = getattr(orch, "bus", None)
        if bus is None or id(bus) in self._forwarded:
            return
        self._forwarded.add(id(bus))
        subscribe = getattr(bus, "subscribe", None)
        if subscribe is None:
            return

        def _on_event(event: Any) -> None:
            try:
                if getattr(event, "type_value", "") == "llm.response":
                    self._accumulate_cache(event)
                self.emit(event)
            except Exception as exc:  # noqa: BLE001 - a forward must not break the run
                _log(f"serve: could not forward an event: {exc}")

        try:
            subscribe(_on_event)
        except Exception as exc:  # noqa: BLE001 - a bus that cannot be tapped is not fatal
            _log(f"serve: could not subscribe to the run bus: {exc}")


def _is_hired(spec: Any) -> bool:
    """Whether an agent was hired by the Owner, rather than being part of the built-in company.

    `origin == "owner"` is not sufficient on its own: the built-in Owner *principal* also carries that
    origin, so a fresh install would report a hired agent nobody hired.
    """
    return (str(getattr(spec, "origin", "")) == "owner"
            and str(getattr(spec, "id", "")) != "ag_owner")


def _proposal_document(*, run_id: str, slug: str, goal: str, manifest: dict[str, Any],
                       validation: dict[str, Any], staffing_gaps: Iterable[dict[str, Any]],
                       manifest_path: Any, adopted: bool, approvable: bool,
                       reason: str) -> dict[str, Any]:
    """The proposed graph, in the one shape `manifest.proposed` and `status`'s `proposal` both carry.

    **One document, two carriers.** The card is fed by an event (`manifest.proposed`, emitted by
    `_cmd_start`) and by the poll (`status.proposal`, read from the checkpoint), and it must not be able
    to read one shape from one and a different shape from the other. So both go through here: the event
    passes the run it just prepared, the poll passes what the checkpoint holds. `nodes`, `gates` and
    `loops` are reduced to what the card draws (ids, and each loop's bound), and `approvable`/`reason`
    are the engine's verdict so the app never has to infer whether its Approve control would be refused.
    """
    return {
        "run_id": run_id, "slug": slug, "goal": goal,
        "validated": validation.get("valid"),
        "adopted": adopted,
        "nodes": [str(n.get("id")) for n in (manifest.get("nodes") or [])
                  if isinstance(n, dict)],
        "loops": [{"id": loop.get("id"), "max_iterations": loop.get("max_iterations")}
                  for loop in (manifest.get("loops") or []) if isinstance(loop, dict)],
        "gates": [str(g.get("id")) for g in (manifest.get("gates") or [])
                  if isinstance(g, dict)],
        "staffing_gaps": [dict(gap) for gap in staffing_gaps],
        "manifest_path": str(manifest_path) if manifest_path else None,
        "approvable": bool(approvable),
        "reason": reason,
    }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def _pid() -> int:
    import os

    return os.getpid()


def _pid_alive(pid: int) -> bool | None:
    """Whether a process is still running: True, False, or **None when it cannot be determined**.

    `os.kill(pid, 0)` is the portable existence test, but it returns success for a **zombie**: a dead
    process not yet reaped still has a pid, and signalling it "succeeds". That is not a corner case
    here, it is the normal case — the engine's parent is the app, and when the app dies its status
    becomes `Z` until launchd reaps it. A liveness check that reports "alive" for a process that is
    already dead is worse than no check, because it is a check that cannot fail.

    So the real status is read where the platform exposes it: `/proc/<pid>/stat` on Linux, and
    `proc_pidinfo` on macOS (there is no `/proc` there, and shelling out to `ps` is slow and blocked in
    some sandboxes — which would silently degrade to "alive" and make this useless).

    **Why the third answer exists, and why it was the bug.** This used to collapse "I could not tell"
    into "alive". That made the watchdog's question unfalsifiable in exactly the case it was written
    for: a platform or an errno the status read does not describe produced "alive" for ever, so the
    poll ran on against a pid that no longer existed and the engine outlived its app while holding the
    project. `None` states that the platform did not answer instead of mistaking silence for a verdict;
    deciding what to do about it belongs to the caller, and `_parent_is_gone` is where that decision is
    made.
    """
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else — still alive for our purposes
    except OSError:
        return False

    # It exists; find out whether it is *running* rather than a zombie.
    try:
        proc_stat = Path(f"/proc/{pid}/stat")
        if proc_stat.is_file():
            fields = proc_stat.read_text().rsplit(")", 1)
            if len(fields) == 2:
                return not fields[1].strip().startswith("Z")
    except OSError:
        pass

    try:
        running = _pid_alive_via_libproc(pid)
        if running is not None:
            return running
    except Exception:  # noqa: BLE001 - any failure here must not stop a working engine
        pass

    # The pid exists (`os.kill` said so) but nothing here could describe what state it is in.
    return None


def _parent_is_gone(expected: int, *, started_as_child: bool) -> bool:
    """Whether the process that started this engine is gone. Always answers, and answers with a reason.

    **One question the platform always answers about our own parent.** Every check above is about the
    pid; this one is about the *relationship* the pid was named for. `expected` is the app that spawned
    us, so it is our parent until it exits, and a process is reparented (to launchd, pid 1) exactly
    when its parent goes away. That single fact resolves the three-valued answer `_pid_alive` can give:

    1. **`False` — the platform described it as dead or a zombie.** Gone, the ordinary case.
    2. **We were its child and are not any more.** Gone, whatever the pid says. This is also the only
       check that catches the pid having been *reused* by an unrelated process, which every other check
       would report as alive for ever.
    3. **`None` — the pid exists but its state could not be read.** If it is still our parent it exists,
       and "alive" is the honest reading of a read that failed; if it is not our parent, nothing here
       connects the pid to the app that started us, and the verdict is **gone**.

    The unknown case therefore ends in "gone" — the direction of error that cannot leak. An engine that
    stops when it should not have is a restartable inconvenience; an engine that survives its app keeps
    a project its owner has walked away from, runs code nobody is watching, and makes the next launch
    start a second engine on the same checkpoint. This does not weaken the rule the named pid exists
    for: a live app is *alive* on the first check and needs none of the rest.
    """
    alive = _pid_alive(expected)
    if alive is False:
        return True
    if started_as_child and os.getppid() != expected:
        return True
    if alive is None:
        return os.getppid() != expected
    return False


def _pid_alive_via_libproc(pid: int) -> bool | None:
    """macOS: read the process status with `proc_pidinfo`, returning None when it cannot be determined.

    `PROC_PIDTBSDINFO` (3) fills a `proc_bsdinfo` whose first field is the `pbi_status`: 1 is born, 2 is
    running, 3 is sleeping, 4 is idle and **5 is a zombie**. Reading it directly is what makes a zombie
    distinguishable without a shell command.

    A return of 0 bytes is not "unknown": `errno` says why. `ESRCH` (3) means the pid is gone, which is
    exactly what the caller needs to know — an earlier version treated 0 as undeterminable and reported
    "alive", so the watchdog never fired for the case it exists to catch.
    """
    import ctypes
    import ctypes.util

    libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
    buffer = ctypes.create_string_buffer(256)
    ctypes.set_errno(0)
    written = libproc.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(3), ctypes.c_uint64(0),
                                   buffer, ctypes.c_int(256))
    if written <= 0:
        errno = ctypes.get_errno()
        if errno == 3:      # ESRCH — no such process
            return False
        return None         # genuinely undeterminable (a permission limit, say)
    status = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32)).contents.value
    return status != 5      # 5 == SZOMB


def serve(*, config: Any, library: Any, project: str = "console",
          root: Any = None, project_dir: Any = None,
          stdin: Any = None, stdout: Any = None) -> int:
    """Start the server for a project. Returns an exit code.

    ``project_dir`` attaches an existing folder (the console's *Open Project…*); ``project``/``root``
    are the managed alternative. Exactly one is meaningful, and ``project_dir`` wins because naming a
    folder is more specific than naming a directory of folders.
    """
    from .state import Workspace

    if project_dir:
        workspace = Workspace.attach(project_dir)
    else:
        workspace = Workspace.for_project(project, root=root)
    workspace.ensure()
    server = Server(config=config, library=library, workspace=workspace, slug=project,
                    stdin=stdin, stdout=stdout)
    return server.serve_forever()
