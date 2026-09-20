#!/usr/bin/env python3
"""schedules.py — an objective that fires on a clock, and the rule that stops it firing forever.

WHY THIS EXISTS
---------------
A Goal answers *"keep working until this is done"* and a Mission answers *"which step is next"*, but
neither answers *"start this again tomorrow, and every day after"*. That is the last thing a person
running an org wants that the engine could not do, and without it the only honest advice is "run the
command yourself in a loop" — which is a shell loop with no record of what it started, no bound on
the spend, and no way to tell an operator what happened last night.

DESIGN
------
- **Durability is the file; firing is explicit.** The schedule lives in
  `.agent_state/schedules.json`, written atomically (temp + `os.replace` + fsync) and schema-versioned
  exactly like the goal beside it. Reading a schedule arms nothing: an entry only fires while a
  watcher is running, and a watcher is a foreground process someone started.
- **The file is the shared truth, not this process's copy of it.** A watcher holds a schedule in
  memory across ticks while the CLI and the app edit the same file from other processes, so every
  mutation reloads under an advisory lock before it writes. Without that, the watcher's next fire
  wrote its whole stale list back and a `schedules remove` — or the app's Forget — was undone: the
  entry a person had just deleted reappeared. See :meth:`ScheduleStore.reload`.
- **Due is a stored instant, not a derived guess.** Every entry carries `next_due_at`, set when it is
  created and advanced when it fires. So `due(now)` is one comparison, a fire cannot happen twice for
  one due time, and an entry that was missed while the machine slept fires once rather than N times.
- **The watcher refuses to re-arm a parked goal.** This is the whole safety argument of the module.
  A schedule is by construction a *repeat* of something that spends, so a failing objective would
  otherwise be an unattended spend loop running once a minute for ever — precisely the failure the
  entire autonomy design is built to avoid. So a fire that ends paused, blocked, gated or failed
  **disables the entry** and says why. Re-enabling is a deliberate act, exactly as arming a goal is.
- **The tick is bounded.** A watcher with a zero or unbounded tick is either a busy loop or a
  schedule nobody can reason about, so the interval has a floor and a ceiling and a value outside
  them is refused with the bounds named rather than silently clamped.
- **A fire is the same path as a manual run.** It sets an armed goal through the orchestrator with a
  resolved posture policy, plans, approves and executes — so a scheduled run parks at the gates its
  posture says, is recorded in the same ledger, and is auditable exactly as a run a person typed.

Usage:
    store = ScheduleStore(workspace)
    entry = store.add(objective="triage the overnight issues", interval_s=3600)
    for entry in store.due():                 # read-only
        fire(orch, entry, workspace, policy=policy)
        store.mark_fired(entry, outcome="done", run_id=run.run_id)
"""

from __future__ import annotations

import calendar
import contextlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # POSIX only. The engine's targets are macOS and Linux. A platform without `flock` still gets
    import fcntl  # the reload-before-every-write, which is the part that fixes the resurrected
except ImportError:  # pragma: no cover - not a target platform  removal; what it loses is the
    fcntl = None  # type: ignore[assignment]                       guarantee that no two processes
    # write at the same moment.

__all__ = [
    "ScheduleError",
    "ScheduleEntry",
    "ScheduleStore",
    "SCHEDULE_FILENAME",
    "SCHEDULE_VERSION",
    "MIN_TICK_S",
    "MAX_TICK_S",
    "DEFAULT_TICK_S",
    "PARKED_OUTCOMES",
    "parse_when",
    "parse_duration",
    "watch",
    "fire",
]

#: Beside the goal and the mission, inside `.agent_state/`: a schedule is run state for a workspace.
SCHEDULE_FILENAME = "schedules.json"
#: Bumped when the document's shape changes incompatibly.
SCHEDULE_VERSION = "1.0.0"

#: The tick bounds. The floor exists because a sub-five-second tick is a busy loop dressed as a
#: schedule; the ceiling because anything longer than an hour belongs to cron, not to a foreground
#: process someone is watching. A value outside these is refused rather than clamped, so the operator
#: learns the bound instead of silently getting a different schedule from the one they asked for.
MIN_TICK_S = 5
MAX_TICK_S = 3600
DEFAULT_TICK_S = 30

#: How many fire records an entry keeps. Bounded because an hourly schedule runs for years, and an
#: unbounded history is a file that grows until someone notices.
MAX_HISTORY = 20

#: How many entries one store holds. Same reasoning one level up: a schedule file is small, must be
#: readable by a person, and has no legitimate reason to hold thousands of entries.
MAX_ENTRIES = 200

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw]?)$", re.IGNORECASE)

#: Fire outcomes that mean *the goal is not running any more*, which is what forbids a re-arm. Named
#: as one tuple so the refusal rule is one comparison rather than four scattered conditions.
PARKED_OUTCOMES: tuple[str, ...] = ("paused", "blocked", "gated", "failed")

_DURATION_UNITS = {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}


class ScheduleError(RuntimeError):
    """A schedule that cannot be read, written or fired, named so the reason is actionable."""


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def _iso_at(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + f".{int(epoch * 1000) % 1000:03d}Z"


def parse_when(text: str) -> float:
    """An ISO instant as epoch seconds, or a refusal naming the accepted form.

    Only the form the engine itself writes is accepted (`YYYY-MM-DDTHH:MM:SS[.fff]Z`, UTC). A relaxed
    parser would quietly read a local-time string as UTC and fire a schedule an hour early, which is
    the kind of silent wrongness that makes an operator distrust the whole feature.
    """
    raw = str(text or "").strip()
    if not raw:
        raise ScheduleError("a due time is required")
    core = raw.rstrip("Zz")
    for form in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return float(calendar.timegm(time.strptime(core[:19].split(".")[0], form)))
        except ValueError:
            continue
    raise ScheduleError(
        f"cannot read {text!r} as a time. Use a UTC ISO instant like 2026-09-18T07:30:00 "
        "(what `date -u +%Y-%m-%dT%H:%M:%S` prints)."
    )


def parse_duration(text: str) -> int:
    """A duration as seconds: ``90``, ``30s``, ``15m``, ``6h``, ``2d``, ``1w``.

    A bare number means minutes, because "every 30" in a schedule is a person meaning half an hour,
    and a bare-seconds reading would fire 30 times an hour.
    """
    raw = str(text or "").strip().lower()
    match = _DURATION_RE.match(raw)
    if match is None:
        raise ScheduleError(
            f"cannot read {text!r} as an interval. Use seconds with a unit — 30s, 15m, 6h, 2d — "
            "or a bare number of minutes."
        )
    amount = int(match.group(1))
    if amount <= 0:
        raise ScheduleError(
            "an interval must be at least one second; a schedule that fires continuously is a "
            "spend loop, not a schedule."
        )
    return amount * _DURATION_UNITS[match.group(2).lower()]


@dataclass
class ScheduleEntry:
    """One objective, and when it is next allowed to start.

    Parameters
    ----------
    slug:
        The workspace the fire lands in. Stored rather than implied so an entry is self-describing:
        a schedule file copied to another root still says what it schedules.
    interval_s:
        `0` for a one-shot. A positive value advances `next_due_at` after every fire.
    next_due_at:
        The instant the entry becomes due. This is the single field `due()` compares, which is what
        makes a fire idempotent per due time and a missed window fire once rather than N times.
    last_outcome:
        What the previous fire produced. `paused`/`blocked`/`gated`/`failed` disable the entry, so
        this field — not a heuristic about the goal's text — is what stops a failing schedule.
    """

    slug: str
    objective: str
    posture: str = "unattended"
    interval_s: int = 0
    next_due_at: str = ""
    enabled: bool = True
    id: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_fired_at: str = ""
    last_run_id: str = ""
    last_outcome: str = ""
    last_detail: str = ""
    disabled_reason: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    schedule_version: str = SCHEDULE_VERSION

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"sch_{uuid.uuid4().hex[:10]}"
        if not self.created_at:
            self.created_at = _iso_now()
        if not self.updated_at:
            self.updated_at = self.created_at
        if self.posture not in ("unattended", "supervised"):
            raise ScheduleError(
                f"unknown posture {self.posture!r}; expected unattended or supervised"
            )

    # ── the due predicate ───────────────────────────────────────────────────

    def due_at(self) -> float:
        """When this entry becomes due, as epoch seconds. An unset time is never due."""
        try:
            return parse_when(self.next_due_at)
        except ScheduleError:
            return float("inf")

    def is_due(self, now: float) -> bool:
        """Whether the entry should fire at `now`. An unset due time is never due."""
        if not self.enabled or not self.next_due_at:
            return False
        return self.due_at() <= now

    def is_oneshot(self) -> bool:
        """True when this entry fires once and is done.

        A one-shot is an entry with no interval. It is identified by that rather than by `--at`,
        because an entry that was created with `--at` and then given an interval is a repeat, and the
        interval is the field that decides.
        """
        return self.interval_s <= 0

    @property
    def parked(self) -> bool:
        """True when the last fire left the goal not running. This is what forbids a re-arm."""
        return self.last_outcome in PARKED_OUTCOMES

    # ── transitions ─────────────────────────────────────────────────────────

    def arm(self, *, at: float | None = None) -> None:
        """Make the entry due again (or for the first time) without touching its statistics."""
        moment = _iso_at(at) if at is not None else _iso_now()
        self.next_due_at = moment
        self.enabled = True
        self.disabled_reason = ""
        self.updated_at = _iso_now()

    def schedule_next(self, *, now: float) -> None:
        """Advance the due time after a successful fire.

        Advanced from `now` rather than from the previous due time: an entry that was missed while the
        machine was asleep fires once and then resumes on the clock, instead of replaying every missed
        window as a burst of runs.
        """
        self.next_due_at = _iso_at(now + max(1, self.interval_s))
        self.updated_at = _iso_now()

    def finish(self, *, now: float) -> None:
        """What a fire does when it succeeded: advance, or retire a one-shot."""
        if self.is_oneshot():
            self.enabled = False
            self.next_due_at = ""
            self.disabled_reason = ("fired once; this is a one-shot entry. Add it again with "
                                    "--every to repeat it.")
        else:
            self.schedule_next(now=now)
        self.updated_at = _iso_now()

    def disable(self, reason: str) -> None:
        """Stop the entry firing, and record why so `list` can say it.

        `next_due_at` is deliberately left in place: an operator who re-enables the entry should see
        when it was last due, not a blank they have to reconstruct from the history.
        """
        self.enabled = False
        self.disabled_reason = str(reason or "")[:400]
        self.updated_at = _iso_now()

    def record_fire(self, *, outcome: str, detail: str = "", run_id: str = "",
                    at: float | None = None) -> None:
        """Record what a fire produced, keeping the history bounded."""
        moment = _iso_at(at) if at is not None else _iso_now()
        self.last_fired_at = moment
        self.last_outcome = str(outcome or "")
        self.last_detail = str(detail or "")[:400]
        if run_id:
            self.last_run_id = str(run_id)
        self.history.append({"at": moment, "outcome": self.last_outcome, "detail": self.last_detail,
                             "run_id": self.last_run_id})
        self.history = self.history[-MAX_HISTORY:]
        self.updated_at = _iso_now()

    # ── serialisation ───────────────────────────────────────────────────────

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "objective": self.objective,
            "posture": self.posture,
            "interval_s": self.interval_s,
            "next_due_at": self.next_due_at,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_fired_at": self.last_fired_at,
            "last_run_id": self.last_run_id,
            "last_outcome": self.last_outcome,
            "last_detail": self.last_detail,
            "disabled_reason": self.disabled_reason,
            "history": list(self.history),
            "oneshot": self.is_oneshot(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleEntry":
        if not isinstance(data, dict):
            raise ScheduleError("a schedule entry must be a JSON object")
        slug = str(data.get("slug") or "")
        if not _SLUG_RE.match(slug):
            raise ScheduleError(
                f"schedule entry has an invalid slug {slug!r}; expected a lowercase slug matching "
                "[a-z0-9][a-z0-9._-]*"
            )
        objective = str(data.get("objective") or "").strip()
        if not objective:
            raise ScheduleError(
                f"schedule entry {data.get('id') or slug} has no objective, so it would arm nothing"
            )
        return cls(
            slug=slug,
            objective=objective,
            posture=str(data.get("posture") or "unattended").strip().lower(),
            interval_s=int(data.get("interval_s") or 0),
            next_due_at=str(data.get("next_due_at") or ""),
            enabled=bool(data.get("enabled", True)),
            id=str(data.get("id") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            last_fired_at=str(data.get("last_fired_at") or ""),
            last_run_id=str(data.get("last_run_id") or ""),
            last_outcome=str(data.get("last_outcome") or ""),
            last_detail=str(data.get("last_detail") or ""),
            disabled_reason=str(data.get("disabled_reason") or ""),
            history=list(data.get("history") or [])[-MAX_HISTORY:],
            schedule_version=str(data.get("schedule_version") or SCHEDULE_VERSION),
        )


class ScheduleStore:
    """The durable schedule file for one workspace, and every operation over it.

    Parameters
    ----------
    workspace:
        The workspace the file belongs to. Its own `state_dir` is used rather than a re-derived path,
        so a schedule lands beside the goal it will fire.
    """

    def __init__(self, workspace: Any) -> None:
        state_dir = getattr(workspace, "state_dir", None)
        if state_dir is None:
            raise ScheduleError(
                "a schedule needs a workspace: pass a Workspace (or anything with a `state_dir`), "
                "because the file lives beside the goal it fires"
            )
        self.directory = Path(state_dir)
        self.workspace = workspace
        self.entries: list[ScheduleEntry] = []
        #: Set when a load had to skip something, so an inspector can tell an empty schedule from an
        #: unreadable one. Never raised: a broken entry must not make the rest unfireable.
        self.load_error = ""
        self.version = SCHEDULE_VERSION
        #: Serialises the read-modify-write *within* this process. `flock` cannot do that job:
        #: it is held per open file description, so two threads of one process take the same lock
        #: and one of them releases it while the other is still mid-write.
        self._lock = threading.RLock()
        #: `(mtime_ns, size)` of the file as this process last read it, so a read-only query can
        #: notice another process's write without re-parsing on every call.
        self._stamp: tuple[int, int] | None = None
        self._load()

    @property
    def path(self) -> Path:
        """`.agent_state/schedules.json`."""
        return self.directory / SCHEDULE_FILENAME

    @property
    def lock_path(self) -> Path:
        """The file the read-modify-write is serialised on.

        A file of its own rather than `schedules.json` itself: `save` *replaces* that file, so a lock
        taken on it would be a lock on an inode that no longer carries the name — the next process to
        lock the path would take a different lock and both would believe they were alone.
        """
        return self.directory / (SCHEDULE_FILENAME + ".lock")

    # ── persistence ─────────────────────────────────────────────────────────

    def _read_entries(self) -> tuple[list[ScheduleEntry] | None, str]:
        """Parse the file into entries, or say why it could not be read.

        `None` means *unreadable*, which is deliberately distinct from an empty list: it is what stops
        a writer from overwriting a schedule it failed to understand with the copy it happens to be
        holding, which would turn a corrupt file into a silently truncated one.
        """
        if not self.path.is_file():
            return [], ""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"{self.path} is unreadable ({exc}); no entry will fire"
        if not isinstance(data, dict):
            return None, f"{self.path} must be a JSON object; no entry will fire"
        version = str(data.get("schedule_version") or SCHEDULE_VERSION)
        if version.split(".")[0] != SCHEDULE_VERSION.split(".")[0]:
            return None, (
                f"{self.path} has schedule_version {version}, which is not compatible with "
                f"{SCHEDULE_VERSION}; written by a different build. Refusing to fire from it."
            )
        entries: list[ScheduleEntry] = []
        error = ""
        for raw in data.get("entries") or []:
            try:
                entries.append(ScheduleEntry.from_dict(raw))
            except ScheduleError as exc:
                # Skipped rather than fatal: one bad entry must not make the rest unfireable. The
                # reason is kept so an inspector can tell this from an empty schedule.
                error = f"skipped an entry: {exc}"
        self.version = version
        return entries, error

    def _load(self) -> None:
        """Replay the file for the first time, skipping an unreadable entry rather than losing it all."""
        entries, error = self._read_entries()
        self.load_error = error
        if entries is None:
            return
        self.entries = entries
        self._stamp = self._file_stamp()

    def _file_stamp(self) -> tuple[int, int] | None:
        """`(mtime_ns, size)` of the schedule file, or None when it is not there."""
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def reload(self) -> bool:
        """Re-read the file into this store, keeping the objects already in memory by id.

        The identity merge is the point, not an optimisation. Callers hold `ScheduleEntry` objects —
        the watcher holds the one it is about to fire, a test holds the one it asserted on, the app's
        reply carries one back — and replacing the list wholesale would leave every one of those
        references pointing at a copy the file no longer agrees with. Updating the object in place
        keeps "the entry I am holding" and "the entry on disk" the same thing.

        Returns whether the file could be read at all. On a failure the in-memory entries are left
        alone and `load_error` says why: throwing away what we have because we cannot read the file
        would be a second failure on top of the first.
        """
        entries, error = self._read_entries()
        if entries is None:
            self.load_error = error
            return False
        known = {entry.id: entry for entry in self.entries}
        merged: list[ScheduleEntry] = []
        for fresh in entries:
            existing = known.get(fresh.id)
            if existing is None:
                merged.append(fresh)
            else:
                existing.__dict__.update(fresh.__dict__)
                merged.append(existing)
        self.entries = merged
        self.load_error = error
        self._stamp = self._file_stamp()
        return True

    def reload_if_changed(self) -> bool:
        """Reload only when the file has been written since this process last read it.

        Cheap enough for the watcher to call on every tick, which is what lets it notice an entry
        *added* from another process while it was sleeping rather than only noticing at the next fire.
        """
        stamp = self._file_stamp()
        if stamp is None or stamp == self._stamp:
            return False
        return self.reload()

    def save(self) -> Path:
        """Persist atomically, temp-then-`os.replace` with an fsync.

        A torn schedule is worse than a stale one: it could come back with an entry enabled whose
        last fire was a failure, which is the one state this module exists to prevent. The atomic
        replace also means a concurrent reader never sees a half-written file — it sees this write or
        the previous one — so the readers do not need the lock this module's writers take.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "schedule_version": SCHEDULE_VERSION,
            "updated_at": _iso_now(),
            "workspace": getattr(self.workspace, "slug", ""),
            "entries": [entry.as_dict() for entry in self.entries],
        }
        target = self.path
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ScheduleError(f"failed to write the schedule {target}: {exc}") from exc
        self._stamp = self._file_stamp()
        return target

    # ── the read-modify-write ───────────────────────────────────────────────

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        """Reload, then mutate, then save — with other *processes* excluded for the whole of it.

        This is the shape the resurrected removal forces. A watcher keeps a schedule in memory across
        ticks; the CLI and the app edit the same file from other processes. Writing the whole
        in-memory list back on every fire therefore overwrote whatever had changed in between, so a
        `schedules remove` landing between two fires was undone by the next one, and a person who
        pressed Forget watched the entry come back. Reloading *before* the mutation is what makes a
        removal stick; the lock is what makes the reload and the write one step rather than two.

        Chosen over "the watcher is the only writer" deliberately: it cannot be true. The app's Forget
        is served by a different process (`engine/serve.py` builds its own store), and the CLI writes
        the same file from a third — so making one process the owner would mean routing every edit
        through it, which is a protocol the engine does not have and could not enforce.
        """
        with self._lock:
            with self._exclusive_file_lock():
                if not self.reload():
                    raise ScheduleError(
                        f"refusing to write the schedule: {self.load_error}. Writing the copy this "
                        "process happens to hold would overwrite a file it could not read, so the "
                        "write is refused rather than made blind."
                    )
                yield

    @contextlib.contextmanager
    def _exclusive_file_lock(self) -> Iterator[None]:
        """An advisory lock over `schedules.json.lock`, held across the read and the write.

        Best-effort by design: a state directory that refuses the lock file still gets the reload
        before every write, which is the part that fixes the visible defect, without the lock, which
        is the part that narrows the remaining window to the microseconds around `os.replace`.
        """
        if fcntl is None:
            yield
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            handle = open(self.lock_path, "a+", encoding="utf-8")
        except OSError:
            yield
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    # ── mutation ────────────────────────────────────────────────────────────

    def add(self, *, objective: str, slug: str = "", posture: str = "unattended",
            every: str | int | None = None, at: str | None = None,
            due_now: bool = False, enabled: bool = True,
            now: float | None = None) -> ScheduleEntry:
        """Add an entry, and return it. Exactly one of `every` / `at` / `due_now` decides the time.

        Naming none of them is a usage error rather than a default, because the default would have to
        be a guess about intent: "fire immediately" and "fire in an hour" are both plausible readings
        of a bare objective, and firing is spending.

        Raises
        ------
        ScheduleError
            On a missing objective, an unreadable interval, a bare `--at`, an unknown slug, or a
            schedule already at its entry ceiling.
        """
        text = (objective or "").strip()
        if not text:
            raise ScheduleError("a schedule needs an objective; an entry with none would arm nothing")
        target = (slug or getattr(self.workspace, "slug", "") or "").strip()
        if not _SLUG_RE.match(target):
            raise ScheduleError(
                f"invalid slug {slug!r} for a schedule entry; expected a lowercase slug matching "
                "[a-z0-9][a-z0-9._-]*"
            )
        moment = time.time() if now is None else float(now)
        interval = parse_duration(str(every)) if every is not None else 0
        if sum(1 for value in (every, at, due_now if due_now else None) if value) > 1:
            raise ScheduleError(
                "give exactly one of --every, --at or --due-now: two of them would disagree about "
                "when the entry fires, and there is no sensible way to combine them"
            )
        if at is not None:
            due = _iso_at(parse_when(str(at)))
        elif interval:
            due = _iso_at(moment + interval)
        elif due_now:
            due = _iso_at(moment)
        else:
            raise ScheduleError(
                "say when it should fire: --every 1h (repeat), --at 2026-09-18T07:30:00 (once), "
                "or --due-now (at the next tick)"
            )
        entry = ScheduleEntry(slug=target, objective=text,
                              posture=str(posture or "unattended").strip().lower(),
                              interval_s=interval, next_due_at=due, enabled=enabled)
        if not enabled:
            entry.disabled_reason = "added disabled"
        # Inside the transaction, not before it: the ceiling is checked against the file as it is
        # *now*, so an add issued beside another process's add sees both rather than one.
        with self._transaction():
            if len(self.entries) >= MAX_ENTRIES:
                raise ScheduleError(
                    f"this schedule already holds {len(self.entries)} entries (the ceiling is "
                    f"{MAX_ENTRIES}). Remove one before adding another — a schedule file is meant "
                    "to be readable by a person."
                )
            self.entries.append(entry)
            self.save()
        return entry

    def remove(self, ref: str) -> ScheduleEntry:
        """Remove an entry by id, or by slug when the reference names one.

        Reloads first, under the lock, so the removal is applied to the schedule as it is rather than
        to the copy this process read at startup — which is what makes a Forget from the app and an
        add from the CLI both survive a watcher that is mid-tick.

        Raises
        ------
        ScheduleError
            When nothing matches, or when a slug matches more than one entry — removing the wrong
            schedule is not a mistake that can be undone, so the caller is asked to name the id.
        """
        wanted = str(ref or "").strip()
        with self._transaction():
            matches = [entry for entry in self.entries if entry.id == wanted]
            if not matches:
                matches = [entry for entry in self.entries if entry.slug == wanted]
            if not matches:
                raise ScheduleError(
                    f"no schedule entry matches {ref!r}. Run `schedules list` to see the entries and "
                    "their ids."
                )
            if len(matches) > 1:
                raise ScheduleError(
                    f"{ref!r} matches {len(matches)} entries; remove one by id: "
                    + ", ".join(entry.id for entry in matches)
                )
            entry = matches[0]
            self.entries.remove(entry)
            self.save()
        return entry

    def enable(self, ref: str, *, at: float | None = None) -> ScheduleEntry:
        """Re-arm an entry by id or slug. An explicit act, like arming a goal.

        The due time is *set* rather than kept: an entry disabled days ago has a due time in the past,
        and re-enabling it would fire it immediately on the next tick. Firing now is a legitimate
        thing to want, but it must not be what "enable" silently means.
        """
        with self._transaction():
            entry = self.find(ref)
            entry.arm(at=at)
            self.save()
        return entry

    def find(self, ref: str) -> ScheduleEntry:
        """One entry by id, else by slug. Raises rather than returning None — see `by_id`."""
        wanted = str(ref or "").strip()
        found = self.by_id(wanted)
        if found is not None:
            return found
        for entry in self.entries:
            if entry.slug == wanted:
                return entry
        raise ScheduleError(f"no schedule entry matches {ref!r}")

    def by_id(self, entry_id: str) -> ScheduleEntry | None:
        """One entry by its exact id, or None.

        The not-found-is-an-answer counterpart to `find`, and the reason it exists: a writer folding a
        fire back in has to be able to ask "is the entry I just ran still here?" without catching an
        exception, because "it was removed while the run was in flight" is a real answer rather than
        an error.
        """
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    # ── reading ─────────────────────────────────────────────────────────────

    def due(self, now: str | float | None = None) -> list[ScheduleEntry]:
        """Every entry that should fire, in due order. **Writes nothing.**

        Side-effect-free in the sense that matters: the answer to "what is due" must be safe to ask
        from an inspection command, and a query that advanced the clock would mean asking twice fired
        twice. It does re-read the file when another process has changed it, which is why a watcher
        notices an entry added beside it on the next tick rather than only when it next fires —
        a stale read is not a side effect, but it is a wrong answer.
        """
        moment = _as_epoch(now)
        self.reload_if_changed()
        return sorted((entry for entry in self.entries if entry.is_due(moment)),
                      key=lambda entry: (entry.due_at(), entry.slug))

    def next_due(self) -> ScheduleEntry | None:
        """The entry that becomes due first, or None when nothing is armed."""
        armed = [entry for entry in self.entries if entry.enabled and entry.next_due_at]
        if not armed:
            return None
        return min(armed, key=lambda entry: entry.due_at())

    def view(self, now: str | float | None = None) -> dict[str, Any]:
        """The whole schedule picture, for the CLI and the console."""
        moment = _as_epoch(now)
        upcoming = self.next_due()
        return {
            "schedule_version": SCHEDULE_VERSION,
            "path": str(self.path),
            "load_error": self.load_error,
            "counts": {
                "total": len(self.entries),
                "enabled": sum(1 for entry in self.entries if entry.enabled),
                "disabled": sum(1 for entry in self.entries if not entry.enabled),
                "due": len(self.due(moment)),
                "parked": sum(1 for entry in self.entries if entry.parked),
            },
            "next_due_at": upcoming.next_due_at if upcoming else "",
            "next_due_id": upcoming.id if upcoming else "",
            "entries": [entry.as_dict() for entry in self.entries],
        }


def _as_epoch(now: str | float | None) -> float:
    """Coerce the several ways a caller says "now" into epoch seconds."""
    if now is None:
        return time.time()
    if isinstance(now, (int, float)):
        return float(now)
    return parse_when(str(now))


# ── firing ───────────────────────────────────────────────────────────────────


def posture_policy(orch: Any, posture: str) -> Any:
    """The goal policy for a named posture, keeping whatever else the config already chose.

    Built by overriding the configured default rather than from nothing, so a scheduled
    `supervised` entry narrows the authority to "ask me" without also resetting auto-hire. Mirrors
    `cli._posture_policy`, which is the same resolution the manual `run --posture` path uses; the CLI
    passes its own object to :func:`fire` so a scheduled run cannot end up with a different authority
    from a typed one.
    """
    from .goal import GoalPolicy, Posture

    base = orch._default_goal_policy()
    resolved = Posture(str(posture or "unattended").strip().lower())
    return GoalPolicy(
        auto_approve=base.auto_approve if resolved is Posture.UNATTENDED else False,
        auto_hire=base.auto_hire,
        persist_hires=base.persist_hires,
        posture=resolved,
    )


def fire(orch: Any, entry: ScheduleEntry, workspace: Any, *, policy: Any = None,
         now: float | None = None, executor: Any = None,
         plan: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Start one scheduled goal, on the **same** path a manual run takes.

    The order is the manual order — set the armed goal with the resolved posture, plan it, approve the
    graph, execute — because a scheduled run that took a shortcut would be a run whose gates, ledger
    entries and stop reasons differ from one a person started, and the whole point of scheduling is to
    have *more* of the same thing rather than a second, less auditable kind of run.

    Returns a report whose `outcome` is one of `done`, `paused`, `blocked`, `gated`, `failed`,
    `completed`. `paused`/`blocked`/`gated`/`failed` are the entries in
    :data:`PARKED_OUTCOMES`, and the caller disables the entry for those — see :func:`watch`.

    `plan` is the seam a test injects a prepared run through, so the fire *decision* can be exercised
    without a provider; the default is the orchestrator's own `prepare`.
    """
    moment = time.time() if now is None else float(now)
    from .goal import GoalState

    resolved = policy if policy is not None else posture_policy(orch, entry.posture)
    prepared = plan or (lambda: orch.prepare(entry.objective, slug=entry.slug))

    try:
        orch.goal_set(entry.objective, armed=True, by="schedule", policy=resolved)
        run = prepared()
        orch.approve(run)
        outcome = orch.execute(run, executor=executor)
    except Exception as exc:  # noqa: BLE001 - a fire that cannot start is a report, not a crash
        # `str` of a bare exception is empty, and "the goal could not be started: " with nothing after
        # it is a line an operator cannot act on — so the exception's own name is the fallback.
        reason = str(exc).strip() or type(exc).__name__
        return {"entry_id": entry.id, "slug": entry.slug, "outcome": "failed",
                "detail": f"the goal could not be started: {reason}", "run_id": ""}

    goal = orch.goal()
    state = goal.state if goal is not None else None
    phase = getattr(getattr(run, "phase", None), "value", "")
    stop_reason = str(getattr(run, "stop_reason", "") or "")
    gate = getattr(run, "gate", None)

    if state is GoalState.BLOCKED:
        outcome_name, detail = "blocked", (goal.blocked_reason or "the goal reported a blocker")
    elif state is GoalState.PAUSED:
        reason = goal.pause_reason or "paused"
        outcome_name = "gated" if reason == "gate" else "paused"
        detail = {"gate": "a gate is waiting for the Owner",
                  "budget_spend": "the goal spent its slice budget",
                  "manual": "the goal was paused",
                  "restored": "the goal came back disarmed"}.get(reason, f"the goal paused: {reason}")
    elif gate is not None or phase in ("awaiting_gate", "awaiting_human", "awaiting_approval"):
        outcome_name, detail = "gated", f"the run is waiting at {gate.gate_id if gate else phase}"
    elif phase in ("failed", "aborted"):
        outcome_name, detail = "failed", (stop_reason or f"the run ended {phase}")
    elif state is GoalState.COMPLETED:
        outcome_name, detail = "completed", (goal.summary or "the goal reported completion")
    else:
        outcome_name, detail = "done", (stop_reason or f"the run ended {phase or 'done'}")

    return {
        "entry_id": entry.id,
        "slug": entry.slug,
        "objective": entry.objective,
        "posture": entry.posture,
        "outcome": outcome_name,
        "detail": str(detail)[:400],
        "run_id": str(getattr(run, "run_id", "") or ""),
        "phase": phase,
        "stop_reason": stop_reason,
        "goal_state": state.value if state is not None else "",
        "spend": (goal.spend.as_dict() if goal is not None else {}),
        "at": _iso_at(moment),
    }


def apply_fire(store: ScheduleStore, entry: ScheduleEntry, report: dict[str, Any], *,
               now: float | None = None) -> dict[str, Any]:
    """Fold one fire's report back into its entry, and obey the no-re-arm rule.

    The rule, stated once here because it is the reason this module is safe: **a fire that ended
    parked disables its entry**. A schedule repeats something that spends, so leaving a failing
    objective armed means it fires again on the next tick, and again on the one after — an unattended
    spend loop on a goal that has already told us it cannot proceed. Disabling is the refusal, and the
    reason is written to the entry so `list` can say what happened rather than leaving a silent
    stopped schedule.

    The other rule, and the one this function's reload exists for: **a removal beats a fire.** A fire
    takes minutes; the entry it belongs to can be removed while it runs, by the CLI or by the app's
    Forget. The entry is therefore looked up again in the file as it is *now* rather than written back
    from the copy this process started with — writing the copy back would recreate the entry the
    person just deleted, which is exactly the symptom (press Forget, watch it return) that made this
    necessary. When the entry is gone, nothing is written and the returned report says so.
    """
    moment = time.time() if now is None else float(now)
    outcome = str(report.get("outcome") or "failed")
    with store._transaction():
        live = store.by_id(entry.id)
        if live is None:
            return {**report, "entry_removed": True, "entry_enabled": False,
                    "removed_detail": ("the entry was removed while its run was in flight; the fire "
                                       "is recorded here but the removal is not undone"),
                    "next_due_at": "", "disabled_reason": ""}
        live.record_fire(outcome=outcome, detail=str(report.get("detail") or ""),
                         run_id=str(report.get("run_id") or ""), at=moment)
        if outcome in PARKED_OUTCOMES:
            live.disable(
                f"the previous fire ended {outcome}: {report.get('detail') or 'no detail'}. "
                "A schedule never re-arms a goal a previous fire left parked — fix the cause, then "
                "`schedules enable` to arm it deliberately."
            )
        else:
            live.finish(now=moment)
        store.save()
        if live is not entry:
            # The caller is holding a copy the file has since replaced. Fold the truth back so every
            # reference to this entry agrees — the watcher reads `entry.enabled` right after this.
            entry.__dict__.update(live.__dict__)
        return {**report, "entry_enabled": live.enabled, "next_due_at": live.next_due_at,
                "disabled_reason": live.disabled_reason}


def watch(store: ScheduleStore, *, resolve: Callable[[str], tuple[Any, Any]],
          tick_s: int = DEFAULT_TICK_S, ticks: int | None = None,
          max_fires: int | None = None, now: Callable[[], float] | None = None,
          sleep: Callable[[float], None] | None = None,
          fire_fn: Callable[..., dict[str, Any]] | None = None,
          policy_for: Callable[[Any, str], Any] | None = None,
          executor: Any = None,
          on_tick: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Watch the schedule and start what is due, in the foreground, until stopped.

    Parameters
    ----------
    resolve:
        `slug -> (orchestrator, workspace)`. Injected so the watcher does not own workspace
        resolution: the CLI already resolves it one way for every command, and a second way here
        would be a second answer to "which project did that fire land in".
    tick_s:
        The tick. Refused outside :data:`MIN_TICK_S`–:data:`MAX_TICK_S` rather than clamped, so the
        operator gets the interval they asked for or a reason they did not.
    ticks:
        Stop after this many ticks. `None` runs until interrupted, which is what the CLI does; a test
        passes a number, which is what makes the loop testable at all.
    max_fires:
        Stop after this many *fires* in total. A second bound beyond the no-re-arm rule: it is the
        ceiling for a watch started by a script that has no one to press Ctrl-C.

    Every state change is saved through the store's atomic write before the next tick, so a watcher
    killed mid-tick leaves a schedule that reflects the fires it actually made — and every save
    reloads the file first, so a change made beside the watcher is never written away by it. An entry
    that another process removes mid-fire is reported in `removed` and left removed.
    """
    if not (MIN_TICK_S <= int(tick_s) <= MAX_TICK_S):
        raise ScheduleError(
            f"a tick of {tick_s}s is outside the permitted range ({MIN_TICK_S}–{MAX_TICK_S}s). "
            "Below the floor a watcher is a busy loop; above the ceiling the schedule belongs to cron "
            "rather than to a foreground process. Refusing to run with a tick that was not asked for."
        )
    if store.load_error:
        raise ScheduleError(
            f"refusing to watch: {store.load_error}. A schedule that cannot be read cannot be "
            "trusted to fire the right things."
        )
    clock = now or time.time
    pause = sleep or time.sleep
    launch = fire_fn or fire
    resolve_policy = policy_for or posture_policy

    report: dict[str, Any] = {"ticks": 0, "fired": [], "disabled": [], "removed": [], "stopped": "",
                              "started_at": _iso_now(), "tick_s": int(tick_s)}
    fired = 0
    while True:
        moment = clock()
        tick_report: dict[str, Any] = {"at": _iso_at(moment), "due": [], "fired": [], "disabled": [],
                                       "removed": []}
        for entry in store.due(moment):
            if max_fires is not None and fired >= int(max_fires):
                report["stopped"] = (f"reached --max-fires={max_fires}; stopping rather than "
                                     "starting more runs")
                report["records"] = tick_report
                return report
            try:
                orch, workspace = resolve(entry.slug)
            except Exception as exc:  # noqa: BLE001 - an unresolvable slug disables the entry
                folded = apply_fire(store, entry,
                                    {"outcome": "failed",
                                     "detail": (f"cannot resolve the workspace for {entry.slug!r}: "
                                                f"{exc}")},
                                    now=moment)
                if folded.get("entry_removed"):
                    tick_report["removed"].append({"id": entry.id, "slug": entry.slug,
                                                   "detail": folded["removed_detail"]})
                    report["removed"].append(tick_report["removed"][-1])
                    continue
                tick_report["disabled"].append({"id": entry.id, "slug": entry.slug,
                                                "reason": folded.get("disabled_reason", "")})
                report["disabled"].append(dict(tick_report["disabled"][-1]))
                continue
            tick_report["due"].append(entry.id)
            try:
                entry_report = launch(orch, entry, workspace,
                                      policy=resolve_policy(orch, entry.posture), now=moment,
                                      executor=executor)
            except Exception as exc:  # noqa: BLE001 - a fire that raised is a failed fire
                entry_report = {"outcome": "failed", "detail": str(exc), "run_id": ""}
            folded = apply_fire(store, entry, entry_report, now=moment)
            fired += 1
            tick_report["fired"].append({"id": entry.id, "slug": entry.slug,
                                         "outcome": folded["outcome"],
                                         "detail": folded.get("detail", ""),
                                         "run_id": folded.get("run_id", "")})
            report["fired"].append(dict(tick_report["fired"][-1]))
            if folded.get("entry_removed"):
                # The run happened and its report is above; what must not happen is the entry coming
                # back, so this is recorded separately rather than dressed up as a disable.
                tick_report["removed"].append({"id": entry.id, "slug": entry.slug,
                                               "detail": folded["removed_detail"]})
                report["removed"].append(dict(tick_report["removed"][-1]))
            elif not folded.get("entry_enabled", entry.enabled):
                # Read from the folded report, not from `entry`: the report is the value that was
                # written, and the object is only guaranteed to agree because `apply_fire` syncs it.
                tick_report["disabled"].append({"id": entry.id, "slug": entry.slug,
                                                "reason": folded.get("disabled_reason", "")})
                report["disabled"].append(dict(tick_report["disabled"][-1]))

        report["ticks"] += 1
        if on_tick is not None:
            on_tick(tick_report)
        if ticks is not None and report["ticks"] >= int(ticks):
            report["stopped"] = f"reached {ticks} tick(s)"
            report["records"] = tick_report
            return report
        pause(float(tick_s))
