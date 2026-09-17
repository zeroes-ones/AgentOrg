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
- **Slow subscribers are marked, not blocked.** A subscriber that raises is disabled
  and reported; a subscriber that is simply slow lets the bus continue. The run must
  never die because a terminal is busy.
- **Append-only, flush-per-line trace.** Torn last lines are tolerated on replay
  (`parse_stream` skips them), which is what makes crash recovery work.
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

from .config import redact
from .protocol import Event, EventType, ProtocolError, encode

__all__ = ["EventBus", "Subscriber", "BusStats"]

Subscriber = Callable[[Event], None]


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
        When given, every event is appended as NDJSON. Written with one `write` per
        line and flushed, so an abrupt kill loses at most the final partial line.
    """

    def __init__(
        self,
        *,
        run_id: str | None = None,
        history_size: int = 20_000,
        trace_path: os.PathLike | str | None = None,
        redact_payloads: bool = True,
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
        self._trace_fh = None
        if self._trace is not None:
            self._trace.parent.mkdir(parents=True, exist_ok=True)
            # Line-buffered append; never truncate, so a resumed run extends its trace.
            self._trace_fh = open(self._trace, "a", encoding="utf-8")

    # ── lifecycle ───────────────────────────────────────────────────────────

    def close(self) -> None:
        """Flush and release the trace handle. Idempotent."""
        with self._lock:
            if self._trace_fh is not None:
                try:
                    self._trace_fh.flush()
                    self._trace_fh.close()
                finally:
                    self._trace_fh = None

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
        fh = self._trace_fh
        if fh is None:
            return
        try:
            fh.write(encode(ev) + "\n")
            fh.flush()
            self._stats.written_to_trace += 1
        except (OSError, ProtocolError) as exc:
            # A trace failure must not take down the run, but it must be visible.
            self._stats.trace_errors += 1
            self._disabled[-1] = f"trace write failed: {exc}"

    def _deliver(self, ev: Event) -> None:
        # Snapshot under the lock, dispatch outside it: a subscriber that emits a
        # nested event would otherwise deadlock on the non-reentrant delivery loop.
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
