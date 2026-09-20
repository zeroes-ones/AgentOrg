#!/usr/bin/env python3
"""cachestore.py — the durable record of what the prefix cache did, so a restart can still answer
"is this run cache-warm?".

WHY THIS EXISTS
---------------
`engine/cache.py` measures the cache and `engine/pinning.py` holds the prefix fixed, but both live in
memory for the life of one process. Three consequences, all of them silent:

- **A resumed run forgets why the prefix changed.** `ShapeTracker` keeps only the previous shape, so
  the first call after a restart has no predecessor and a genuine miss is indistinguishable from a
  cold start. The sequence — *the tools hash changed, then a compaction, then it settled* — is the
  thing a person needs to fix a bill, and it was thrown away exactly when they came looking for it.
- **Nothing on disk can answer the only question a restart raises.** A provider's prefix cache
  outlives our process; our evidence about it did not. "Is the prefix I am about to send the same one
  I sent before?" had no answer that survived a crash, which is precisely when it matters.
- **The saving was reported but never accumulated** where it can be held against an invoice.

This module is that record. It is deliberately *evidence*, not a second source of truth: the tokens
and the cost come from the provider's own `Usage`, exactly as everywhere else in the engine.

DESIGN
------
- **Three facts, three files.** `prefix/<hash>.json` is one file per prefix, named by the digest the
  engine already computes for it, so "have I seen this prefix before?" is a file existence test.
  `shapes.jsonl` is every observation, append-only, because the sequence *is* the diagnosis.
  `savings.jsonl` is one line per request.
- **Replay on load.** Both streams rebuild into memory when the store opens, so a resumed process
  answers from the history its predecessor wrote. A torn final line — the normal result of a kill
  mid-write — is skipped rather than fatal, which is what makes the append-only form safe.
- **An unreported figure is stored as absent.** `null`, never `0`. A provider that said nothing about
  caching must not become a 0% hit rate by passing through a file: in memory those two facts are
  opposite, and a file cannot be allowed to change that.
- **Bounded, evicting oldest-first.** An unattended run appends a line per model call for as long as
  it runs, so unbounded growth is a disk-filling bug rather than a hypothetical. Each stream has a
  line budget and the prefix set has a count budget; the trim drops the oldest first. The bound is
  also what makes replay-on-load affordable — reading a capped file is cheap by construction.
- **Cost is recorded, never computed.** The saving is `Cost.cache_saving_usd`, which the gateway
  derived from the provider's counters and its price table. Nothing here estimates anything.
- **Two hash spaces, labelled.** The gateway observers a request-shaped digest
  (`engine.cache.capture_shape`) and the executor pins a `Prefix` digest over different bytes. Both
  are recorded, and each record carries its `source`, because a lookup that asked in the wrong space
  would report a prefix that *was* pinned as never seen — or confirm one it never held.

Usage:
    store = CacheStore(workspace.cache_dir)
    store.remember_prefix(prefix_hash=prefix.prefix_hash, skill="code-reviewer",
                          tool_names=["read_file"], chars=prefix.chars, source="pin")
    # after a restart, with the same skill and tools:
    check = store.verify_prefix(prefix_hash=prefix.prefix_hash, skill="code-reviewer",
                                tool_names=["read_file"], chars=prefix.chars)
    if check.known and check.unchanged:
        ...  # this run is still cache-warm
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "CacheStore", "CacheStoreError", "PrefixCheck", "PrefixRecord",
    "CACHE_DIRNAME", "PREFIX_DIRNAME", "SHAPES_FILENAME", "SAVINGS_FILENAME",
    "MAX_SHAPE_LINES", "MAX_SAVINGS_LINES", "MAX_PREFIX_FILES", "MAX_RUNS_REMEMBERED",
]

#: The store's own directory name inside `.agent_state/`. Named here so the workspace layout and this
#: module cannot drift into two spellings for one place.
CACHE_DIRNAME = "cache"
PREFIX_DIRNAME = "prefix"
SHAPES_FILENAME = "shapes.jsonl"
SAVINGS_FILENAME = "savings.jsonl"

#: The bounds. Stated as constants rather than defaults buried in a signature because the *number* is
#: the promise: an overnight run must not be able to grow these files without limit. One line per
#: model call is the natural append rate, so 4000 lines is a long run's worth of history and a few
#: hundred KB on disk; beyond that the oldest lines are dropped, because recent history is the part
#: that explains the bill in front of you.
MAX_SHAPE_LINES = 4_000
MAX_SAVINGS_LINES = 4_000
#: Distinct prefixes are bounded by the skills and tool sets a project uses — tens, not thousands.
#: The cap is a backstop against a genuinely unstable prefix, which would otherwise write a file per
#: call while never repeating one.
MAX_PREFIX_FILES = 512
#: How many runs one prefix's file remembers sending it. Bounded because a long-lived project reuses
#: the same prefix for ever, and the field exists to answer "who sent this", not to be a log.
MAX_RUNS_REMEMBERED = 8

#: A hash names a file, so it is validated like a slug: a hash containing a separator or a traversal
#: segment would let a caller write outside the store.
_HASH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class CacheStoreError(RuntimeError):
    """A store write that could not be honoured, named so the caller can report it precisely.

    Raised rather than swallowed here: the *caller* decides whether a missing cache record is
    tolerable. The gateway treats it as a warning, because a run must not die over its own
    bookkeeping; a diagnostic command may want to surface it.
    """


def _iso_now() -> str:
    """UTC timestamp with millisecond precision, matching the protocol's format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


@dataclass
class PrefixRecord:
    """One pinned prefix, as recorded on disk.

    The identity is the digest the engine computes, because that is the handle everything else in the
    cache layer already knows the prefix by. The rest is the context a human needs to act on a miss:
    which skill, which tools, how large. `first_seen` and
    `last_seen` are what separate "this prefix was introduced today" from "this prefix has been stable
    for a week and something else is invalidating the cache".
    """

    prefix_hash: str
    skill: str = ""
    tool_names: list[str] = field(default_factory=list)
    chars: int = 0
    first_seen: str = ""
    last_seen: str = ""
    #: How many times the prefix has been observed, which distinguishes a prefix used once from one in
    #: steady use.
    observations: int = 0
    #: Runs that have sent it, most recent first, capped. Answers "where did these bytes come from".
    runs: list[str] = field(default_factory=list)
    #: The store's own monotonic sequence number for the last time this prefix was seen. Kept beside
    #: the ISO timestamp because two writes inside one millisecond share a `last_seen`, and an
    #: oldest-first eviction needs a *total* order rather than a tie it has to break arbitrarily.
    last_touch: int = 0
    #: Which hash space this record came from: `gateway` for the request-shaped digest
    #: `capture_shape` computes, `pin` for the `Prefix` digest the executor pins. They are two
    #: different hashes over two different byte sequences, so a lookup must know which one it is
    #: asking in — otherwise a prefix that *was* pinned reads as never seen, which is the exact
    #: question this store exists to answer.
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "prefix_hash": self.prefix_hash,
            "skill": self.skill,
            "tool_names": list(self.tool_names),
            "chars": self.chars,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "observations": self.observations,
            "runs": list(self.runs),
            "last_touch": self.last_touch,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PrefixRecord":
        return cls(
            prefix_hash=str(data.get("prefix_hash") or ""),
            skill=str(data.get("skill") or ""),
            tool_names=[str(name) for name in (data.get("tool_names") or [])],
            chars=int(data.get("chars") or 0),
            first_seen=str(data.get("first_seen") or ""),
            last_seen=str(data.get("last_seen") or ""),
            observations=int(data.get("observations") or 0),
            runs=[str(run) for run in (data.get("runs") or [])],
            last_touch=int(data.get("last_touch") or 0),
            source=str(data.get("source") or ""),
        )


@dataclass(frozen=True)
class PrefixCheck:
    """The verdict on "is the prefix I would send the one that was pinned?".

    `known` and `unchanged` are separate because they answer different questions and the fix differs:
    a prefix never seen is a cold cache with nothing wrong with it, while a prefix seen and since
    changed is a cache that has gone cold — the one worth reporting.
    """

    known: bool
    unchanged: bool
    prefix_hash: str = ""
    recorded: PrefixRecord | None = None
    mismatches: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        """A sentence naming what moved, or the absence of any evidence."""
        if not self.known:
            return (f"prefix {self.prefix_hash} has not been seen before: this is a cold start, not "
                    "a regression")
        if self.unchanged:
            return (f"prefix {self.prefix_hash} is the same one recorded "
                    f"({self.recorded.observations if self.recorded else 0} observation(s))")
        return (f"prefix {self.prefix_hash} is recorded, but the re-pinned bytes differ in "
                f"{', '.join(self.mismatches) or 'unknown'} — the cache for this prefix has gone cold")

    def as_dict(self) -> dict[str, Any]:
        return {
            "known": self.known,
            "unchanged": self.unchanged,
            "prefix_hash": self.prefix_hash,
            "mismatches": list(self.mismatches),
            "reason": self.reason,
        }


class CacheStore:
    """The durable cache record for one workspace.

    Parameters
    ----------
    directory:
        `.agent_state/cache/`. Created on first *write* rather than on open, so pointing a store at a
        workspace that has no cache history costs nothing and changes nothing.
    max_shape_lines, max_savings_lines, max_prefix_files:
        The bounds, overridable so a test can drive the eviction path without writing thousands of
        lines to prove it works.
    """

    def __init__(self, directory: os.PathLike | str, *,
                 max_shape_lines: int = MAX_SHAPE_LINES,
                 max_savings_lines: int = MAX_SAVINGS_LINES,
                 max_prefix_files: int = MAX_PREFIX_FILES) -> None:
        self.directory = Path(directory)
        self.max_shape_lines = max(1, int(max_shape_lines))
        self.max_savings_lines = max(1, int(max_savings_lines))
        self.max_prefix_files = max(1, int(max_prefix_files))
        self._prefixes: dict[str, PrefixRecord] = {}
        self._shapes: list[dict[str, Any]] = []
        self._savings: list[dict[str, Any]] = []
        #: A monotonic counter stamped on every prefix observation. `_iso_now` has millisecond
        #: resolution, so two writes in the same millisecond share a `last_seen` and an oldest-first
        #: eviction would then be deciding by hash rather than by age.
        self._touch_seq = 0
        #: Set when a load had to skip something, so an inspector can tell an empty store from an
        #: unreadable one. Never raised: a store that cannot be read is a run that has no history,
        #: which is a degraded state rather than a fatal one.
        self.load_error: str = ""
        self._load()

    # ── layout ──────────────────────────────────────────────────────────────

    @staticmethod
    def for_workspace(workspace: Any) -> "CacheStore":
        """Open the store a workspace owns, using its own layout rather than a re-derived path."""
        base = getattr(workspace, "cache_dir", None)
        if base is None:
            base = Path(getattr(workspace, "state_dir", workspace)) / CACHE_DIRNAME
        return CacheStore(base)

    @property
    def prefix_dir(self) -> Path:
        """`prefix/` — one file per pinned prefix, named by its hash."""
        return self.directory / PREFIX_DIRNAME

    @property
    def shapes_path(self) -> Path:
        """`shapes.jsonl` — every shape observation, oldest first."""
        return self.directory / SHAPES_FILENAME

    @property
    def savings_path(self) -> Path:
        """`savings.jsonl` — one line per request that reported anything about caching."""
        return self.directory / SAVINGS_FILENAME

    # ── loading (replay) ────────────────────────────────────────────────────

    def _load(self) -> None:
        """Rebuild the in-memory history from disk.

        Replay rather than a summary file: the streams are already the history, and a second
        representation of the same facts is a second thing that can disagree with them.
        """
        self._prefixes.clear()
        self._shapes.clear()
        self._savings.clear()
        self._load_prefixes()
        # Resume the counter past everything already on disk, so a record written before this process
        # started cannot sort ahead of one it is about to write.
        self._touch_seq = max((r.last_touch for r in self._prefixes.values()), default=0)
        self._shapes = self._load_stream(self.shapes_path)
        self._savings = self._load_stream(self.savings_path)
        # A file that outgrew its budget while an older build was running is trimmed on the next
        # write; trimming here would make opening a store a write, which a read-only inspector
        # (`status`, `diagnostics`) must not be.

    def _load_prefixes(self) -> None:
        try:
            entries = sorted(self.prefix_dir.glob("*.json"))
        except OSError as exc:
            self.load_error = f"cannot list {self.prefix_dir}: {exc}"
            return
        for path in entries:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self.load_error = f"skipping unreadable prefix record {path.name}: {exc}"
                continue
            if not isinstance(data, dict):
                continue
            record = PrefixRecord.from_dict(data)
            # The filename is the identity when the body has none or disagrees with it: a record
            # whose hash field was lost must still be findable under the name it was written with.
            if not record.prefix_hash:
                record.prefix_hash = path.stem
            self._prefixes[record.prefix_hash] = record

    def _load_stream(self, path: Path) -> list[dict[str, Any]]:
        """Replay one JSONL stream, tolerating a torn final line.

        A partial last line is the expected shape of a killed process, so it is skipped rather than
        treated as corruption — the alternative is that a crash costs the whole history.
        """
        out: list[dict[str, Any]] = []
        try:
            if not path.is_file():
                return out
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict):
                        out.append(data)
        except OSError as exc:
            self.load_error = f"cannot read {path.name}: {exc}"
        return out

    # ── prefixes ────────────────────────────────────────────────────────────

    def remember_prefix(self, *, prefix_hash: str, skill: str = "",
                        tool_names: Iterable[str] = (), chars: int = 0,
                        run_id: str = "", source: str = "") -> PrefixRecord:
        """Record a pinned prefix, or refresh the one already recorded under this hash.

        Returns the record, so a caller can see `first_seen` and tell a prefix it just introduced from
        one that has been stable all along — the two call for opposite responses to the same bill.

        `source` says which hash space the digest came from — `pin` for the executor's pinned
        `Prefix`, `gateway` for a request-shaped `capture_shape`. The two are different hashes over
        different bytes and never collide in a way a caller could detect, so the label is kept: a
        lookup that mixed them would answer "never seen" for a prefix that was pinned, which is the
        wrong answer to the only question this store exists to answer.

        Raises
        ------
        CacheStoreError
            When the hash cannot name a file, or the record cannot be written. Both are the caller's
            to report; neither is silent.
        """
        key = self._valid_hash(prefix_hash)
        now = _iso_now()
        self._touch_seq += 1
        existing = self._prefixes.get(key)
        names = [str(name) for name in tool_names]
        if existing is None:
            record = PrefixRecord(prefix_hash=key, skill=skill, tool_names=names, chars=int(chars),
                                  first_seen=now, last_seen=now, observations=1,
                                  last_touch=self._touch_seq, source=source)
        else:
            record = existing
            # A later observation is allowed to fill in what an earlier caller did not know (the
            # gateway sees the tools and the size but not the skill; the executor knows the skill).
            # It is never allowed to blank a field that was known — including the source, because a
            # record whose provenance was erased could no longer be told apart from one in the other
            # hash space.
            record.skill = record.skill or skill
            record.tool_names = record.tool_names or names
            record.chars = record.chars or int(chars)
            record.source = record.source or source
            record.last_seen = now
            record.last_touch = self._touch_seq
            record.observations += 1
        if run_id:
            record.runs = [run_id, *(r for r in record.runs if r != run_id)][:MAX_RUNS_REMEMBERED]
        self._prefixes[key] = record
        self._write_json(self.prefix_dir / f"{key}.json", record.as_dict())
        self._evict_oldest_prefixes()
        return record

    def prefix(self, prefix_hash: str) -> PrefixRecord | None:
        """The recorded prefix for a hash, or None when it has not been seen."""
        return self._prefixes.get(str(prefix_hash))

    def prefixes(self) -> list[PrefixRecord]:
        """Every recorded prefix, most recently seen first."""
        return sorted(self._prefixes.values(),
                      key=lambda r: (r.last_touch, r.prefix_hash), reverse=True)

    def pinned_prefix(self, prefix_hash: str) -> PrefixRecord | None:
        """The record for a hash *known to come from the pinning hash space*, or None.

        The distinction matters at the call site that asks "is this run still cache-warm?": the
        executor has a `Prefix.prefix_hash`, and a record written by the gateway carries a digest from
        a different function over different bytes. Reporting the gateway's record as the pin's would
        be a false confirmation, which is worse than reporting nothing.
        """
        record = self._prefixes.get(str(prefix_hash))
        if record is None or record.source not in ("", "pin"):
            return None
        return record

    def latest_pinned(self, skill: str = "") -> PrefixRecord | None:
        """The most recently seen record from the *pin* hash space, optionally for one skill.

        The lookup for a caller that has a skill and no hash — a compaction deciding what to keep,
        which knows which prefix the *next* request will carry but not its digest until it composes
        one. Restricted to the pin space for the same reason `pinned_prefix` is: a gateway record is a
        digest over different bytes, and reporting it as the pinned one would be a false confirmation.
        """
        candidates = [record for record in self._prefixes.values() if record.source in ("", "pin")]
        if skill:
            candidates = [record for record in candidates if record.skill == skill]
        if not candidates:
            return None
        return max(candidates, key=lambda record: (record.last_touch, record.prefix_hash))

    def verify_prefix(self, *, prefix_hash: str, skill: str | None = None,
                      tool_names: Iterable[str] | None = None,
                      chars: int | None = None) -> PrefixCheck:
        """Compare a re-pinned prefix against what was recorded, and say whether it still matches.

        This is the answer to "is this run still cache-warm?" after a restart. Only the fields the
        caller supplies are compared, because a gateway that recorded a prefix from a request knows
        its tools but not its skill, and reporting the unknown field as a mismatch would cry wolf on
        every check.
        """
        record = self._prefixes.get(str(prefix_hash))
        if record is None:
            return PrefixCheck(known=False, unchanged=False, prefix_hash=str(prefix_hash))
        mismatches: list[str] = []
        if skill is not None and record.skill and record.skill != skill:
            mismatches.append("skill")
        if tool_names is not None:
            names = [str(name) for name in tool_names]
            if record.tool_names and record.tool_names != names:
                mismatches.append("tools")
        if chars is not None and record.chars and record.chars != int(chars):
            mismatches.append("chars")
        return PrefixCheck(known=True, unchanged=not mismatches, prefix_hash=str(prefix_hash),
                           recorded=record, mismatches=mismatches)

    def _evict_oldest_prefixes(self) -> list[str]:
        """Drop the least recently seen prefix files once the set exceeds its cap.

        Oldest-first by the store's own touch sequence rather than by write time or the ISO stamp:
        the file that matters is the one the run keeps sending, mtime ranks it by whatever the
        filesystem happened to do, and a millisecond-resolution timestamp ties on a fast loop.
        """
        if len(self._prefixes) <= self.max_prefix_files:
            return []
        ordered = sorted(self._prefixes.values(),
                         key=lambda r: (r.last_touch, r.first_seen, r.prefix_hash))
        dropped = ordered[:len(ordered) - self.max_prefix_files]
        for record in dropped:
            self._prefixes.pop(record.prefix_hash, None)
            try:
                (self.prefix_dir / f"{record.prefix_hash}.json").unlink(missing_ok=True)
            except OSError:
                # The record is gone from memory, so the next observation rewrites the file anyway.
                continue
        return [record.prefix_hash for record in dropped]

    # ── the streams ─────────────────────────────────────────────────────────

    def record_shape(self, diagnostics: Any, *, turn: int = 0, run_id: str = "",
                     agent_id: str = "", node_id: str = "") -> dict[str, Any]:
        """Append one `ShapeTracker` observation.

        The whole diagnosis is stored rather than only the changed/unchanged boolean, because a
        history of *why* the prefix moved is the only thing that turns "the cache is not helping"
        into a fix.
        """
        payload = dict(diagnostics.as_dict()) if hasattr(diagnostics, "as_dict") else dict(diagnostics)
        payload.update({"turn": int(turn), "at": _iso_now()})
        for key, value in (("run_id", run_id), ("agent_id", agent_id), ("node_id", node_id)):
            if value:
                payload[key] = value
        self._append(self.shapes_path, payload, self._shapes, self.max_shape_lines)
        return payload

    def record_usage(self, usage: Any, cost: Any = None, *, provider_id: str = "",
                     model: str = "", agent_id: str = "", node_id: str = "",
                     session_id: str = "", run_id: str = "") -> dict[str, Any]:
        """Append one request's cache counters and the cost delta they produced.

        Every figure is copied from `Usage`/`Cost`, and a figure the provider did not report is stored
        as `null`. Nothing is derived from anything but the provider's own counters, so a hit rate
        that was absent stays absent through the file and through every reload of it.
        """
        reported = bool(getattr(usage, "cache_reported", False))
        payload: dict[str, Any] = {
            # `None` here is a fact about the provider, not a gap to be filled with a zero.
            "read_tokens": getattr(usage, "cache_hit_tokens", None),
            "write_tokens": getattr(usage, "cache_write_tokens", None),
            "miss_tokens": getattr(usage, "cache_miss_tokens", None),
            # The rate is only stated when the provider reported enough to state one.
            "hit_rate": getattr(usage, "cache_hit_rate", None) if reported else None,
            "cache_reported": reported,
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "saving_usd": getattr(cost, "cache_saving_usd", None) if cost is not None else None,
            "cost_source": getattr(cost, "source", "") if cost is not None else "",
            "at": _iso_now(),
        }
        for key, value in (("provider_id", provider_id), ("model", model), ("agent_id", agent_id),
                           ("node_id", node_id), ("session_id", session_id), ("run_id", run_id)):
            if value:
                payload[key] = value
        self._append(self.savings_path, payload, self._savings, self.max_savings_lines)
        return payload

    def shapes(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """The shape history, oldest first, or the newest `limit` entries still in that order."""
        if limit is None:
            return list(self._shapes)
        return list(self._shapes[-max(0, int(limit)):])

    def savings(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """The per-request savings history, oldest first."""
        if limit is None:
            return list(self._savings)
        return list(self._savings[-max(0, int(limit)):])

    # ── the picture ─────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """The store's aggregate, with the same honesty rule the live tracker keeps.

        Two rules are enforced here rather than left to a caller:

        - An aggregate is `None` when no record reported that figure, never 0.
        - A hit *rate* is computed only over records that reported both halves of the fraction.
          Summing a hit count against an unreported miss would produce a confident 100% for a
          provider that never told us what missed — the exact number this module exists to refuse.
        """
        hits = [s["read_tokens"] for s in self._savings if s.get("read_tokens") is not None]
        misses = [s["miss_tokens"] for s in self._savings if s.get("miss_tokens") is not None]
        writes = [s["write_tokens"] for s in self._savings if s.get("write_tokens") is not None]
        savings = [s["saving_usd"] for s in self._savings if s.get("saving_usd") is not None]
        both = [s for s in self._savings
                if s.get("read_tokens") is not None and s.get("miss_tokens") is not None]
        eligible = sum((s["read_tokens"] or 0) + (s["miss_tokens"] or 0) for s in both)
        rate = (sum(s["read_tokens"] or 0 for s in both) / eligible) if eligible > 0 else None
        reported = bool(hits or misses or writes)

        reasons: dict[str, int] = {}
        for shape in self._shapes:
            for reason in shape.get("prefix_change_reasons") or []:
                reasons[str(reason)] = reasons.get(str(reason), 0) + 1

        return {
            "directory": str(self.directory),
            "prefixes": len(self._prefixes),
            "shapes": len(self._shapes),
            "savings_records": len(self._savings),
            "prefix_changes": sum(1 for shape in self._shapes if shape.get("prefix_changed")),
            "change_reasons": dict(sorted(reasons.items())),
            "cache_reported": reported,
            "cache_hit_tokens": sum(hits) if hits else None,
            "cache_miss_tokens": sum(misses) if misses else None,
            "cache_write_tokens": sum(writes) if writes else None,
            "cache_hit_rate": (round(rate, 4) if rate is not None else None),
            # How many records the rate was actually computed over, so a figure from two calls is not
            # read as a figure from two hundred.
            "rate_records": len(both),
            "cache_saving_usd": (round(sum(savings), 6) if savings else None),
            "unreported_records": sum(1 for s in self._savings if not s.get("cache_reported")),
            "bounds": {
                "max_shape_lines": self.max_shape_lines,
                "max_savings_lines": self.max_savings_lines,
                "max_prefix_files": self.max_prefix_files,
            },
            "load_error": self.load_error,
        }

    def as_dict(self) -> dict[str, Any]:
        """The whole store, for an inspector or a diagnostics bundle."""
        return {
            "summary": self.summary(),
            "prefixes": [record.as_dict() for record in self.prefixes()],
            "shapes": self.shapes(),
            "savings": self.savings(),
        }

    # ── writing ─────────────────────────────────────────────────────────────

    def _valid_hash(self, prefix_hash: str) -> str:
        """A hash that can name a file inside the store, refused loudly otherwise."""
        key = str(prefix_hash or "")
        if not _HASH_RE.match(key):
            raise CacheStoreError(
                f"invalid prefix hash {prefix_hash!r}: it names a file under {self.prefix_dir}, so it "
                "must not contain a path separator or a traversal segment"
            )
        return key

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        """Atomic whole-file write: temp, fsync, `os.replace`.

        A torn prefix record would be worse than a stale one — it is the evidence that this prefix was
        sent before, and half of it reads as a different prefix.
        """
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise CacheStoreError(f"failed to write {path}: {exc}") from exc

    def _append(self, path: Path, payload: dict[str, Any], records: list[dict[str, Any]],
                cap: int) -> None:
        """Append one JSONL record, trimming the stream when it exceeds its budget.

        The handle is opened per write rather than held, because the stream is occasionally rewritten
        whole by the bound; a held handle would keep appending to the inode `os.replace` had just
        detached, and the writes would vanish without an error.
        """
        line = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise CacheStoreError(f"failed to append to {path}: {exc}") from exc
        records.append(payload)
        if len(records) > cap:
            self._trim(path, records, cap)

    def _trim(self, path: Path, records: list[dict[str, Any]], cap: int) -> int:
        """Drop the oldest records so the stream stays inside its budget. Returns how many went."""
        dropped = len(records) - cap
        if dropped <= 0:
            return 0
        del records[:dropped]
        self._rewrite_lines(path, records)
        return dropped

    def _rewrite_lines(self, path: Path, records: list[dict[str, Any]]) -> None:
        """Replace a stream with the records that survived the bound, atomically.

        Written to a temp file and renamed, so a reader never sees a half-trimmed history — the
        inspector can be running while a long run trims underneath it.
        """
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record, separators=(",", ":"), sort_keys=True,
                                        default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise CacheStoreError(f"failed to trim {path}: {exc}") from exc
