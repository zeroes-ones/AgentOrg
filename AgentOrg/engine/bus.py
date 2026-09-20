#!/usr/bin/env python3
"""bus.py — the thread-safe event fan-out: ring buffer, JSONL sink, redaction.

WHY THIS EXISTS
---------------
Everything the application knows, it learns from events. The bus is therefore on the
hot path of every node, every token and every state change, and it has three jobs that
must not interfere with each other:

1. **Deliver to live subscribers** (the CLI's stdout writer, the socket bridge) without
   letting a slow consumer stall a running graph.
2. **Persist a durable, replayable trace** (`trace.jsonl`) so a crash can be resumed
   and a run can be explained afterwards.
3. **Bound memory.** An unattended overnight run can emit hundreds of thousands of
   events; an unbounded list is a memory leak that eventually kills the engine.

DESIGN
------
- **Redaction happens once, at the boundary.** Every payload is scrubbed before it is
  buffered, written or delivered, so no subscriber can leak a secret it was handed.
- **Bounded ring buffer.** The in-memory history keeps the most recent N events and
  counts what it dropped, so the UI can show "1,204 earlier events dropped" instead of
  silently lying about history.
- **Subscribers that raise are marked, not honoured.** A subscriber that raises is disabled
  and reported, and the run continues: an error in a UI sink must never kill a graph. A
  subscriber that *blocks*, though, does hold the bus — delivery happens under the bus
  lock, so a sink that can block owes the bus a hand-off to its own thread. That is stated
  rather than implied because the module used to claim the opposite of both halves.
- **Append-only trace, bounded by bytes.** One complete line per `write`, taken under an
  advisory file lock so a second process appending to the same trace cannot interleave
  with it — and so the file can be compacted in place without clobbering a concurrent
  append. A torn last line is still tolerated on replay (`parse_stream` skips it), which
  is what makes crash recovery work when a process is killed mid-write.
- **Sequence numbers are assigned here**, in one place, so ordering is a property of
  the bus rather than a convention every caller must remember.

Usage:
    bus = EventBus(run_id="run_x", trace_path=Path(".../trace.jsonl"))
    bus.subscribe(lambda ev: print(encode(ev)))
    bus.emit(EventType.NODE_ENTER, node_id="fixer")
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

try:  # POSIX only. The engine's targets are macOS and Linux; a platform without `flock` writes
    import fcntl  # without cross-process mutual exclusion rather than refusing to record anything.
except ImportError:  # pragma: no cover - not a target platform
    fcntl = None  # type: ignore[assignment]

from .config import redact
from .protocol import Event, EventType, ProtocolError, encode

__all__ = ["EventBus", "Subscriber", "BusStats", "MAX_TRACE_BYTES"]

Subscriber = Callable[[Event], None]

#: The ceiling the trace file is kept under, in bytes, by compacting the oldest events away when it
#: is crossed. Not arbitrary: the engine's own traces measure 446 bytes per event (62,529 bytes over
#: 140 events in this repo's root `.agent_state/trace.jsonl`), so 8 MiB is ~18,800 events — about
#: fifty runs the size of the ones on disk here, which an engine would have to run for days to
#: accumulate. The bound exists because the trace had *none*: at 446 bytes an event, one event per
#: second is 38.5 MB a day and 1.2 GB a month, and the readers are not all tail-readers —
#: `load_trace` below does `readlines()` over the whole file under `orchestrator.cost_snapshot`,
#: which runs once per round, so an unbounded trace is a whole-file read into Python strings inside
#: the accounting path of every goal round rather than merely a large file.
MAX_TRACE_BYTES = 8 * 1024 * 1024


class BusStats:
    """A snapshot of bus counters, for diagnostics and the resources view."""

    __slots__ = ("emitted", "dropped_from_buffer", "subscriber_errors", "written_to_trace", "trace_errors")

    def __init__(self) -> None:
        self.emitted = 0
        self.dropped_from_buffer = 0
        self.subscriber_errors = 0
        self.written_to_trace = 0
        self.trace_errors = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "emitted": self.emitted,
            "dropped_from_buffer": self.dropped_from_buffer,
            "subscriber_errors": self.subscriber_errors,
            "written_to_trace": self.written_to_trace,
            "trace_errors": self.trace_errors,
        }


class EventBus:
    """Thread-safe event emitter, history buffer and trace writer.

    Parameters
    ----------
    run_id:
        Stamped onto every event lacking one, so a trace is self-describing.
    history_size:
        Ring-buffer capacity for the in-memory replay used by the UI on attach.
    trace_path:
        When given, every event is appended as NDJSON, one complete line per `write`.
    trace_max_bytes:
        The ceiling the trace is compacted down to. `0` disables compaction (an unbounded
        trace), which only a caller that knows the file is short-lived should ask for.
        A *second* writer of the same trace — the runner subprocess builds its own bus
        over the same path — has its own ceiling, so the effective bound is the smaller
        of the two.
    lifecycle:
        ``config.hooks`` / ``config.notify``, when the caller has them. Given, the bus
        installs the one subscriber every event passes through, so lifecycle hooks and
        notifications are covered for **every** event type without a dozen call sites
        knowing they exist. Opt-in and absent by default, deliberately: this module is
        imported by every reader in the engine and by half the suite, and the
        configuration is already resolved by the time a CLI or server builds a bus —
        threading it in from here would make the bus a config loader.
    lifecycle_slug:
        The project name stamped into a hook's environment. The event's own payload
        wins when it carries one, so a hook on `run.end` names the project even though
        the bus only ever knew a run id.
    """

    def __init__(
        self,
        *,
        run_id: str | None = None,
        history_size: int = 20_000,
        trace_path: os.PathLike | str | None = None,
        trace_max_bytes: int = MAX_TRACE_BYTES,
        redact_payloads: bool = True,
        lifecycle: Any = None,
        lifecycle_slug: str = "",
    ) -> None:
        self.run_id = run_id
        self._lock = threading.RLock()
        self._history: deque[Event] = deque(maxlen=max(1, history_size))
        self._subscribers: list[Subscriber] = []
        self._disabled: dict[int, str] = {}
        self._seq = 0
        self._stats = BusStats()
        self._redact = redact_payloads
        self._trace = Path(trace_path) if trace_path else None
        #: The raw descriptor, not a `TextIOWrapper`. A line must reach the file in one `write`, and
        #: the text layer's buffering is what would otherwise decide how many `write` calls a line
        #: takes — an implementation detail this module should not be relying on for a property it
        #: documents. `os.write` is one syscall by construction.
        self._trace_fd: int | None = None
        #: `0` means "no ceiling". Kept as an int so the comparison in `_write_trace` is one test.
        self._trace_max_bytes = max(0, int(trace_max_bytes))
        #: How many events compaction has discarded. Reported rather than silent, because "the trace
        #: starts mid-run" is otherwise a fact a person reading the file has no way to know.
        self.compacted_events = 0
        #: Why the trace could not be opened, when it could not. Recorded rather than raised so a
        #: read-only command (`goal status`, `flow`, `activity`) still works in a workspace whose
        #: state directory is not writable — losing the trace is a smaller failure than losing the
        #: command, and a raw `PermissionError` out of a *reader* is the opposite of "it just works".
        self.trace_error: str = ""
        if self._trace is not None:
            try:
                self._trace.parent.mkdir(parents=True, exist_ok=True)
                # Append, never truncate on open, so a resumed run extends its trace; bounded by
                # `_compact` rather than by the open mode.
                self._trace_fd = os.open(self._trace, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            except OSError as exc:
                self.trace_error = (
                    f"the event trace at {self._trace} is not writable ({exc}); events are held "
                    "in memory only and will not be persisted for this process."
                )
        #: The hooks/notifications subscriber this bus installed, when one was asked for.
        self.lifecycle: Any = None
        if lifecycle is not None:
            # Imported here rather than at module scope so this module stays what it is — the ring
            # buffer, the trace and the fan-out — and gains no dependency on the config schema.
            from .hooks import Lifecycle

            # The hook log's directory comes from the trace the bus already resolved: that is the
            # same `.agent_state/`, and the bus is the only object in this chain that knows where it
            # is. A bus with no trace path keeps its hooks in memory, which is what a subscriber
            # built over a throwaway bus gets.
            self.lifecycle = Lifecycle.attach(
                self, lifecycle, run_id=run_id or "", slug=lifecycle_slug,
                state_dir=self._trace.parent if self._trace is not None else None)

    # ── lifecycle ───────────────────────────────────────────────────────────

    def close(self) -> None:
        """Release the trace descriptor. Idempotent.

        No flush is needed: every line was one `write` that reached the file when it was made, so
        there is nothing buffered here to lose. That is the property the trace's durability rests on.
        """
        with self._lock:
            if self._trace_fd is not None:
                try:
                    os.close(self._trace_fd)
                except OSError:
                    pass
                finally:
                    self._trace_fd = None

    def __enter__(self) -> "EventBus":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── subscribers ─────────────────────────────────────────────────────────

    def subscribe(self, callback: Subscriber) -> Subscriber:
        """Register a live subscriber and return it (so it can be passed to `unsubscribe`)."""
        with self._lock:
            self._subscribers.append(callback)
        return callback

    def unsubscribe(self, callback: Subscriber) -> None:
        """Remove a subscriber. Safe to call for one that was never registered."""
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not callback]
            self._disabled.pop(id(callback), None)

    def disabled_subscribers(self) -> dict[int, str]:
        """Subscribers auto-disabled after raising, with the reason.

        Exposed rather than swallowed: a silently dead subscriber looks like a UI that
        has stopped updating, which is far harder to diagnose than an error report.
        """
        with self._lock:
            return dict(self._disabled)

    # ── emitting ────────────────────────────────────────────────────────────

    def emit(self, event_type: "EventType | str", *,
             payload: dict[str, Any] | None = None,
             event: Event | None = None,
             **correlation: Any) -> Event:
        """Emit an event. Either pass a pre-built ``event`` or a ``event_type``.

        Correlation kwargs (`agent_id`, `node_id`, `session_id`, `phase`, `run_id`)
        are accepted directly so call sites stay short and cannot forget the run id.
        Returns the emitted event, including its assigned `seq`.
        """
        with self._lock:
            self._seq += 1
            if event is None:
                ev = Event(
                    seq=self._seq,
                    type=event_type,
                    payload=payload or {},
                    run_id=correlation.get("run_id", self.run_id),
                    agent_id=correlation.get("agent_id"),
                    node_id=correlation.get("node_id"),
                    session_id=correlation.get("session_id"),
                    phase=correlation.get("phase"),
                )
            else:
                ev = event
                if ev.seq <= 0:
                    ev.seq = self._seq
                elif ev.seq > self._seq:
                    self._seq = ev.seq
                if ev.run_id is None:
                    ev.run_id = self.run_id

            if self._redact:
                ev.payload = _redact_deep(ev.payload)

            # Count the drop *before* appending: a full deque discards its oldest item
            # on the next append. Checking after the append miscounts by one, because
            # the append that merely fills the buffer drops nothing.
            if len(self._history) == self._history.maxlen:
                self._stats.dropped_from_buffer += 1
            self._history.append(ev)
            self._stats.emitted += 1

            self._write_trace(ev)
            self._deliver(ev)
            return ev

    def _write_trace(self, ev: Event) -> None:
        """Append one complete line, atomically, under the trace's own advisory lock.

        Called with `self._lock` held (from `emit`), which is what makes the in-process case safe:
        `flock` is held per *open file description*, so it cannot keep two threads of one process
        apart by itself — a second `flock` from the same description succeeds immediately, and the
        first thread's unlock would release it mid-write. The reentrant lock is the intra-process
        half and `flock` is the inter-process half; both are needed, and one without the other is
        the kind of half-fix that looks right until two processes share a workspace.

        The lock is also what makes compaction safe. Every writer of this trace takes it, including
        the runner subprocess, which builds its own bus over the same path — so a compaction cannot
        land between another writer's read of the tail and its write of the line.
        """
        fd = self._trace_fd
        if fd is None:
            return
        try:
            line = (encode(ev) + "\n").encode("utf-8")
        except ProtocolError as exc:
            # A frame too big to encode is not an I/O failure and will not be fixed by a retry, so it
            # is counted and dropped here rather than retried per event for the life of the run.
            self._stats.trace_errors += 1
            self._disabled[-1] = f"trace write failed: {exc}"
            return
        try:
            self._lock_trace(fd)
            try:
                if self._trace_max_bytes and (
                        os.fstat(fd).st_size + len(line) > self._trace_max_bytes):
                    self._compact()
                # One `os.write` of the whole line: the O_APPEND descriptor places it at the end of
                # the file atomically, so no reader can ever see half of it. The loop is for the
                # short write that a regular file does not produce but EOF/ENOSPC handling needs.
                written = 0
                while written < len(line):
                    written += os.write(fd, line[written:])
            finally:
                self._unlock_trace(fd)
            self._stats.written_to_trace += 1
        except OSError as exc:
            # A trace failure must not take down the run, but it must be visible.
            self._stats.trace_errors += 1
            self._disabled[-1] = f"trace write failed: {exc}"

    def _lock_trace(self, fd: int) -> None:
        """Take the trace's exclusive advisory lock, when the platform has one."""
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock_trace(self, fd: int) -> None:
        """Release it. Never raises: losing the lock is not a reason to lose the event."""
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass

    def _compact(self) -> None:
        """Keep the newest events and rewrite the file in place until it fits the ceiling.

        In place, not by rotation. The trace is written by two processes at once — the engine's own
        bus and the runner subprocess's — and a rotation (`os.replace`, or unlinking and recreating)
        would leave the *other* process appending to an inode nobody reads any more, silently
        losing every event from the half of the system that happened to write second. Truncating
        the head of the same inode keeps both descriptors valid: an `O_APPEND` write recomputes its
        offset from the file's current end, which is why the rewrite does not have to move it.

        Cut to three quarters of the ceiling, not to the ceiling itself. Cutting to the ceiling would
        leave the file full, so the very next event would compact again — an 8 MiB rewrite per event,
        which turns a bound on disk usage into an I/O amplifier. The quarter of headroom is the
        hysteresis: one rewrite per `max_bytes / 4` of new events.

        The tail is cut at a line boundary rather than at a byte offset, so the file never begins
        with half a record. Nothing is lost that a reader wants: every reader of this trace —
        `flow`, `activity`, `chat`'s incremental narrator, `orchestrator.cost_snapshot` — takes the
        *most recent* records, so the oldest events are the ones that can go.

        The cost of doing it in place: a *reader* that does not take this lock can see the file
        mid-rewrite, where the kept tail has been written and the old bytes past it have not been
        truncated away yet. Every reader of this file is line-tolerant by design (`load_trace` and
        `parse_stream` skip a record they cannot parse), so the worst case is one event skipped from
        one read — against rotation, where the worst case is a whole process's events lost for ever.
        """
        if self._trace is None or self._trace_fd is None:
            return
        size = os.fstat(self._trace_fd).st_size
        keep = max(1, self._trace_max_bytes * 3 // 4)
        if size <= keep:
            return
        with open(self._trace, "rb") as source:
            source.seek(size - keep)
            blob = source.read()
        # Drop the leading partial line: the offset we seeked to is arbitrary, and a file whose
        # first line is a fragment would make `load_trace` skip a record that is actually there.
        newline = blob.find(b"\n")
        blob = blob[newline + 1:] if newline >= 0 else b""
        # A second descriptor, deliberately without O_APPEND: an append descriptor ignores the file
        # offset, so it cannot be told to write at byte 0.
        rewriter = os.open(self._trace, os.O_WRONLY)
        try:
            os.lseek(rewriter, 0, os.SEEK_SET)
            done = 0
            while done < len(blob):
                done += os.write(rewriter, blob[done:])
            os.ftruncate(rewriter, done)
            os.fsync(rewriter)
        finally:
            os.close(rewriter)
        self.compacted_events += 1

    def trace_bytes(self) -> int:
        """The trace's current size on disk, or 0 when nothing is being written."""
        fd = self._trace_fd
        if fd is None:
            return 0
        try:
            return os.fstat(fd).st_size
        except OSError:
            return 0

    def _deliver(self, ev: Event) -> None:
        # The snapshot means a subscriber that subscribes or unsubscribes mid-delivery cannot change
        # the list this event is going to, and the reentrant lock held by `emit` means a subscriber
        # that emits a nested event re-enters rather than deadlocking.
        #
        # NOTE, because an earlier version of this comment claimed the opposite and was wrong:
        # delivery happens *inside* `emit`'s lock. So a subscriber that blocks — a socket write to a
        # client that has stopped reading — blocks that lock, and every other thread's `emit` queues
        # behind it until it returns. Subscribers that can block are expected to hand off to their own
        # thread rather than block here.
        snapshot = list(self._subscribers)
        for callback in snapshot:
            if id(callback) in self._disabled:
                continue
            try:
                callback(ev)
            except Exception as exc:  # noqa: BLE001 - a subscriber must never kill the run
                self._stats.subscriber_errors += 1
                self._disabled[id(callback)] = f"{type(exc).__name__}: {exc}"

    # ── history ─────────────────────────────────────────────────────────────

    def history(self, *, since_seq: int = 0, limit: int | None = None) -> list[Event]:
        """Return buffered events with `seq > since_seq`, oldest first.

        Lets the UI attach mid-run and replay what it missed without re-reading the
        trace file, and lets a reconnecting client resume from its last seen `seq`.
        """
        with self._lock:
            events = [ev for ev in self._history if ev.seq > since_seq]
        if limit is not None:
            events = events[-limit:]
        return events

    def stats(self) -> dict[str, int]:
        """Current counters as a plain dict."""
        with self._lock:
            return self._stats.as_dict()

    @property
    def last_seq(self) -> int:
        """Highest sequence number emitted so far."""
        with self._lock:
            return self._seq


def _redact_deep(value: Any) -> Any:
    """Recursively redact strings in a JSON-ish structure, copying rather than mutating.

    Copying matters: the caller may still hold the original payload and expect its own
    values intact, while what leaves the bus is safe to persist.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(v) for v in value]
    return value


def load_trace(path: os.PathLike | str, *, limit: int | None = None) -> list[Event]:
    """Read a `trace.jsonl` back into events, tolerating a torn final line.

    Used by crash-resume and by the diagnostics bundle. Malformed lines are skipped so
    a partially-written record at the moment of a kill does not invalidate the file.
    """
    from .protocol import parse_stream

    target = Path(path)
    if not target.is_file():
        return []
    with open(target, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    if limit is not None:
        lines = lines[-limit:]
    return parse_stream(lines, strict=False)


def trace_summary(path: os.PathLike | str) -> dict[str, Any]:
    """Summarise a trace file without loading every payload.

    Cheap enough to call from the resources view on a large trace: it parses each line
    for its `type` only and discards the payload.
    """
    target = Path(path)
    if not target.is_file():
        return {"exists": False, "events": 0, "types": {}, "bytes": 0}
    counts: dict[str, int] = {}
    total = 0
    with open(target, "r", encoding="utf-8") as fh:
        for line in fh:
            total += 1
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = str(data.get("type", "?"))
            counts[t] = counts.get(t, 0) + 1
    return {
        "exists": True,
        "events": total,
        "types": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "bytes": target.stat().st_size,
    }
