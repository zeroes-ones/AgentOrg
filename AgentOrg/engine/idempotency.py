#!/usr/bin/env python3
"""idempotency.py — the effect journal, so a retry never applies a side effect twice.

WHY THIS EXISTS
---------------
Retries are not exceptional; they are guaranteed. The provider layer retries on 429 and
5xx, the watchdog restarts a hung runner, and crash-resume replays a run from its last
checkpoint. Each of those paths can re-enter the same node, and the node's side effects
are not naturally idempotent:

- writing an artifact (mostly harmless, but it changes the hash the review loop's
  no-progress guard compares)
- spending tokens (not reversible at all)
- appending to a mailbox (produces a duplicate instruction the agent will act on twice)
- counting cost (inflates the budget and can trip a ceiling spuriously)

The fix is a journal of what has already been done, keyed by an identity that is stable
across retries, consulted *before* an effect is applied and recorded *after*. That is
what makes the crash-resume promise real rather than aspirational.

DESIGN
------
- **The key is derived from meaning, not from time.** `sha(run_id|node_id|attempt|
  inputs_hash|effect|target)` — re-running the same node with the same inputs produces
  the same key, so the second run is recognised as a repeat. Including `attempt` keeps
  *deliberate* re-execution (a review-rework loop) distinct from an accidental retry.
- **The journal is append-only and fsynced.** A torn final line is tolerated on load,
  so a kill during a write loses at most the record of one in-flight effect.
- **Two-phase: reserve then complete.** An effect is reserved before it is applied and
  marked complete after, so a crash *during* an effect is detectable as `in_flight`
  rather than being silently treated as done or as never-started.
- **Replay returns the recorded result**, so the caller behaves identically on a fresh
  execution and a replayed one without special-casing.

Usage:
    journal = EffectJournal(path=state_dir / "effects.jsonl")
    with journal.effect("write_artifact", inputs_hash=h, target="src/app.py") as rec:
        if rec.replayed:
            return rec.result          # already done; do not touch the file again
        ref = store.write(...)
        rec.record(ref.as_dict())
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

__all__ = ["EffectState", "EffectRecord", "Outcome", "EffectJournal", "effect_key"]


class EffectState(str, Enum):
    """Lifecycle of one journalled effect."""

    IN_FLIGHT = "in_flight"   # reserved, not yet known to have completed
    COMPLETED = "completed"   # applied and recorded
    FAILED = "failed"         # attempted and raised; a retry is legitimate


class Outcome(str, Enum):
    """What :meth:`EffectJournal.effect` decided for this call."""

    APPLY = "apply"           # first time: the caller must perform the effect
    REPLAY = "replay"         # already completed: the caller must not repeat it
    RETRY = "retry"           # previously failed: the caller may try again


def effect_key(*, run_id: str, node_id: str, attempt: int, inputs_hash: str,
               effect: str, target: str = "") -> str:
    """Build the stable identity of one effect.

    Deliberately deterministic: the same logical operation attempted twice must hash
    identically so the journal can recognise the repeat. `target` distinguishes two
    artifacts written by one node, which are genuinely different effects.
    """
    material = "|".join([run_id, node_id, str(attempt), inputs_hash, effect, target])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass
class EffectRecord:
    """One journalled effect, and the handle the caller uses inside the context manager."""

    key: str
    effect: str
    run_id: str
    node_id: str
    attempt: int
    target: str = ""
    state: EffectState = EffectState.IN_FLIGHT
    result: dict[str, Any] | None = None
    error: str | None = None
    ts: str = ""
    # Set by the journal so the caller can branch without inspecting the journal again.
    outcome: Outcome = Outcome.APPLY
    # True once this record has been written to the journal. Not serialised; it exists
    # so __exit__ can persist a record the caller completed explicitly without writing
    # it twice.
    persisted: bool = False
    # Attached by EffectJournal.effect so a record can persist itself on completion.
    # Excluded from as_dict/repr because it is a back-reference, not journal content.
    _journal: Any = field(default=None, repr=False, compare=False)

    @property
    def replayed(self) -> bool:
        """True when the effect was already completed and must not be repeated."""
        return self.outcome is Outcome.REPLAY

    def record(self, result: dict[str, Any] | None = None) -> None:
        """Mark the effect completed and persist it immediately.

        Persisting here rather than only on scope exit means a crash *after* the effect
        was applied but *before* the context manager exits still leaves the effect
        recorded — which is what stops a resumed run from re-applying it.

        The journal reference is attached by :meth:`EffectJournal.effect`; a record
        built by hand (as in tests or a replay report) simply updates in memory.
        """
        self.state = EffectState.COMPLETED
        self.result = result or {}
        journal = getattr(self, "_journal", None)
        if journal is not None and not self.persisted:
            journal._append(self)
            self.persisted = True

    def fail(self, error: str) -> None:
        """Mark the effect failed, so a future attempt is allowed to retry it."""
        self.state = EffectState.FAILED
        self.error = error

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "effect": self.effect,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "attempt": self.attempt,
            "target": self.target,
            "state": self.state.value,
            "result": self.result,
            "error": self.error,
            "ts": self.ts,
        }


@dataclass
class EffectJournal:
    """Append-only journal of applied effects for one run.

    The journal is loaded into memory at construction (bounded by the number of node
    executions in a run, which is small) so lookups are O(1) and do not touch disk on
    the hot path.
    """

    path: Path
    _records: dict[str, EffectRecord] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _fh: Any = None
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.load()

    # ── persistence ─────────────────────────────────────────────────────────

    def load(self) -> int:
        """Read an existing journal into memory. Tolerates a torn last line.

        Returns the number of records loaded. A crash mid-append is expected, so a
        malformed final line is skipped rather than failing the whole resume.
        """
        self._records.clear()
        if not self.path.is_file():
            return 0
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    # Expected when the process was killed mid-write.
                    continue
                key = data.get("key")
                if not key:
                    continue
                try:
                    state = EffectState(data.get("state", EffectState.IN_FLIGHT.value))
                except ValueError:
                    state = EffectState.IN_FLIGHT
                self._records[key] = EffectRecord(
                    key=key,
                    effect=str(data.get("effect", "")),
                    run_id=str(data.get("run_id", "")),
                    node_id=str(data.get("node_id", "")),
                    attempt=int(data.get("attempt", 0)),
                    target=str(data.get("target", "")),
                    state=state,
                    result=data.get("result"),
                    error=data.get("error"),
                    ts=str(data.get("ts", "")),
                )
        return len(self._records)

    def _append(self, record: EffectRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8")
        line = json.dumps(record.as_dict(), separators=(",", ":"), sort_keys=True)
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        """Flush and close the journal handle. Idempotent."""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                finally:
                    self._fh = None

    # ── the effect protocol ─────────────────────────────────────────────────

    def lookup(self, key: str) -> EffectRecord | None:
        """Return the recorded effect for a key, if any."""
        with self._lock:
            return self._records.get(key)

    def effect(self, effect: str, *, run_id: str, node_id: str, attempt: int,
               inputs_hash: str, target: str = "") -> "_EffectContext":
        """Open a journalled effect.

        Returns a context manager. Inside it, inspect ``rec.outcome`` (or
        ``rec.replayed``) to decide whether to perform the work, then call
        ``rec.record(...)`` on success. Exiting with an exception marks the effect
        failed so a later attempt may legitimately retry it.
        """
        key = effect_key(run_id=run_id, node_id=node_id, attempt=attempt,
                         inputs_hash=inputs_hash, effect=effect, target=target)
        with self._lock:
            existing = self._records.get(key)
            if existing is not None and existing.state is EffectState.COMPLETED:
                record = existing
                record.outcome = Outcome.REPLAY
                record._journal = self
                return _EffectContext(self, record, replay=True)
            record = EffectRecord(
                key=key,
                effect=effect,
                run_id=run_id,
                node_id=node_id,
                attempt=attempt,
                target=target,
                state=EffectState.IN_FLIGHT,
                ts=_iso_now(),
                outcome=Outcome.RETRY if existing is not None else Outcome.APPLY,
                _journal=self,
            )
            self._records[key] = record
            # Reserve before applying: a crash during the effect then leaves an
            # in_flight record, which is a truthful "we do not know" rather than a
            # silent "not started".
            self._append(record)
            return _EffectContext(self, record, replay=False)

    # ── reporting ───────────────────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        """Record counts by state, for diagnostics and the resources view."""
        out = {state.value: 0 for state in EffectState}
        with self._lock:
            for record in self._records.values():
                out[record.state.value] += 1
        return out

    def in_flight(self) -> list[EffectRecord]:
        """Effects reserved but never completed.

        Surfaced deliberately: after a crash these are the operations whose outcome is
        genuinely unknown, and a resumed run should report them rather than assume.
        """
        with self._lock:
            return [r for r in self._records.values() if r.state is EffectState.IN_FLIGHT]

    def replayed_keys(self) -> list[str]:
        """Keys that were satisfied by replay in this process, for the audit trail."""
        with self._lock:
            return [r.key for r in self._records.values() if r.outcome is Outcome.REPLAY]

    def all_records(self) -> list[EffectRecord]:
        """Every record, sorted by timestamp then key for stable output."""
        with self._lock:
            return sorted(self._records.values(), key=lambda r: (r.ts, r.key))


class _EffectContext:
    """Context manager returned by :meth:`EffectJournal.effect`.

    On a clean exit the effect is marked completed (if the caller did not already
    record a result, an empty one is stored so the replay path still returns
    deterministically). On an exception the effect is marked failed.
    """

    __slots__ = ("_journal", "record", "_replay", "_finished")

    def __init__(self, journal: EffectJournal, record: EffectRecord, *, replay: bool) -> None:
        self._journal = journal
        self.record = record
        self._replay = replay
        self._finished = False

    def __enter__(self) -> EffectRecord:
        return self.record

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._replay:
            # Nothing to record; the journal already holds the completed result.
            return False
        if exc_type is not None:
            self.record.fail(f"{exc_type.__name__}: {exc}")
            self._journal._append(self.record)
            self.record.persisted = True
            return False
        if not self.record.persisted:
            # Covers two paths: the caller called record() without complete(), or the
            # caller performed the work and recorded nothing. Either way a completed
            # record must reach the journal, otherwise a later retry would re-apply it.
            if self.record.state is EffectState.IN_FLIGHT:
                self.record.record({})
            self._journal._append(self.record)
            self.record.persisted = True
        return False

    def complete(self, result: dict[str, Any] | None = None) -> None:
        """Record success explicitly. Delegates to the record, which persists it.

        Kept as a convenience so a caller can write either ``rec.record(...)`` inside
        the block or ``ctx.complete(...)``.
        """
        self.record.record(result)


def _iso_now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def replay_report(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare two journal states, for the "no duplicate effects" verification.

    A resume is correct when every key that was completed before is still completed
    after, and nothing new was completed for work that had already finished.
    """
    before_keys = {k for k, v in before.get("records", {}).items()
                   if str(v.get("state")) == EffectState.COMPLETED.value}
    after_keys = {k for k, v in after.get("records", {}).items()
                  if str(v.get("state")) == EffectState.COMPLETED.value}
    return {
        "completed_before": len(before_keys),
        "completed_after": len(after_keys),
        "newly_completed": sorted(after_keys - before_keys),
        "lost": sorted(before_keys - after_keys),
        "duplicated": sorted(k for k in before_keys & after_keys),
    }
