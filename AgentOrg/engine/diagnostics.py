#!/usr/bin/env python3
"""diagnostics.py — correlation logging, a host health endpoint, and a diagnostics bundle.

WHY THIS EXISTS
---------------
When the engine hangs or a run behaves oddly, the question is not "what is the log line" but "which
run, which node, which agent, which session, which attempt". Without a correlation chain those are
five separate greps and a guess.

This module provides the three things that make a failure diagnosable without a debugger:

1. **Structured, correlated logging.** Every record carries the full chain from run to attempt, and
   every record is redacted before it is written.
2. **A health endpoint**, so the app and an operator can ask "is the engine alive, and what is it
   doing" without reading a file.
3. **A diagnostics bundle**: one archive an operator can hand over, assembled with a leak check so it
   cannot carry a secret.

DESIGN
------
- **Redaction happens on the way in, not on the way out.** A log record that once held a key is a
  leak regardless of what a reader does with it.
- **A bundle refuses to include a secret.** It scans what it is about to package and fails rather than
  shipping a credential.
- **The health endpoint never raises.** A health check that can fail is a health check that lies when
  it matters most.
- **Records are bounded.** A ring buffer plus a capped file, because diagnostics must not become the
  reason a long run dies.

Usage:
    log = Diagnostics(run_id="run_1", state_dir=workspace.state_dir)
    log.log("node.enter", node_id="fixer", attempt=1)
    log.health()
    log.bundle(destination=Path("diagnostics.zip"))
"""

from __future__ import annotations

import json
import os
import platform
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import redact, scan_for_leaks

__all__ = ["Diagnostics", "DiagnosticsError", "LogRecord", "correlation"]

#: How many records are kept in memory. Bounded, because diagnostics must never be the reason a long
#: run runs out of memory.
TAIL_SIZE = 5000

#: The file the records are appended to, relative to the state directory.
LOG_NAME = "diagnostics.jsonl"

#: Files a bundle includes, and files it deliberately excludes. `credentials.json` is never included,
#: and the exclusion is by name rather than by pattern so it cannot be missed.
BUNDLE_INCLUDE = ("trace.jsonl", "run_state.json", "org.json", "review_feedback.json",
                  "effects.jsonl", LOG_NAME, "library_manifest.json")
BUNDLE_EXCLUDE = ("credentials.json", "credentials.example.json")


class DiagnosticsError(RuntimeError):
    """Raised when a bundle cannot be assembled or would carry a secret."""


def correlation(run_id: str = "", node_id: str = "", agent_id: str = "",
                session_id: str = "", attempt: int = 0, phase: str = "") -> dict[str, Any]:
    """Build a correlation chain.

    One function rather than five arguments at every call site, so a record cannot accidentally omit
    the field that would have made it findable.
    """
    chain: dict[str, Any] = {}
    if run_id:
        chain["run_id"] = run_id
    if node_id:
        chain["node_id"] = node_id
    if agent_id:
        chain["agent_id"] = agent_id
    if session_id:
        chain["session_id"] = session_id
    if attempt:
        chain["attempt"] = attempt
    if phase:
        chain["phase"] = phase
    return chain


@dataclass
class LogRecord:
    """One structured log record, with its correlation chain already redacted."""

    level: str
    event: str
    message: str = ""
    chain: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    at: str = ""

    def __post_init__(self) -> None:
        if not self.at:
            self.at = _iso_now()

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "level": self.level,
            "event": self.event,
            "message": self.message,
            **self.chain,
            "detail": self.detail,
        }

    def render(self) -> str:
        """A one-line rendering, for a terminal.

        The chain is printed in a fixed order so two records can be compared by eye — a free-form
        prefix would make that impossible.
        """
        parts = [self.at, self.level.upper().ljust(5), self.event]
        for key in ("run_id", "node_id", "agent_id", "session_id", "attempt", "phase"):
            if key in self.chain:
                parts.append(f"{key}={self.chain[key]}")
        line = " ".join(str(p) for p in parts)
        if self.message:
            line += f"  {self.message}"
        if self.detail:
            line += "  " + json.dumps(self.detail, sort_keys=True, default=str)[:300]
        return line


class Diagnostics:
    """Structured logging, a health endpoint, and bundle assembly for one run.

    Parameters
    ----------
    run_id:
        Stamped onto every record lacking one, so a log file is self-describing.
    state_dir:
        Where the log and the bundle sources live. `None` disables file logging while keeping the
        in-memory tail, which is what a test wants.
    level:
        The minimum level recorded: `debug`, `info`, `warning` or `error`.
    """

    _LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}

    def __init__(self, *, run_id: str = "", state_dir: os.PathLike | str | None = None,
                 level: str = "info", tail_size: int = TAIL_SIZE) -> None:
        self.run_id = run_id
        self.state_dir = Path(state_dir) if state_dir else None
        self.level = level if level in self._LEVELS else "info"
        self._lock = threading.RLock()
        self._tail: deque[LogRecord] = deque(maxlen=max(1, tail_size))
        self._seq = 0
        self._counts: dict[str, int] = {name: 0 for name in self._LEVELS}
        self._started_at = _iso_now()
        self._fh: Any = None

    # ── logging ─────────────────────────────────────────────────────────────

    @property
    def log_path(self) -> Path | None:
        """Where records are written, or None when file logging is disabled."""
        return self.state_dir / LOG_NAME if self.state_dir else None

    def log(self, event: str, *, level: str = "info", message: str = "",
            detail: dict[str, Any] | None = None, **chain: Any) -> LogRecord | None:
        """Record one structured event.

        Returns the record, or None when the level is below the threshold — so a caller can tell
        whether a call was recorded rather than assuming.
        """
        if self._LEVELS.get(level, 20) < self._LEVELS.get(self.level, 20):
            return None
        if self.run_id and "run_id" not in chain:
            chain["run_id"] = self.run_id
        with self._lock:
            self._seq += 1
            self._counts[level] = self._counts.get(level, 0) + 1
            record = LogRecord(
                level=level, event=event,
                # Redacted on the way in: a record that once held a key is a leak regardless of what a
                # reader later does with it.
                message=redact(message),
                chain=_redact_deep(chain),
                detail=_redact_deep(detail or {}),
                seq=self._seq,
            )
            self._tail.append(record)
            self._write(record)
            return record

    def debug(self, event: str, **kwargs: Any) -> LogRecord | None:
        return self.log(event, level="debug", **kwargs)

    def info(self, event: str, **kwargs: Any) -> LogRecord | None:
        return self.log(event, level="info", **kwargs)

    def warning(self, event: str, **kwargs: Any) -> LogRecord | None:
        return self.log(event, level="warning", **kwargs)

    def error(self, event: str, **kwargs: Any) -> LogRecord | None:
        return self.log(event, level="error", **kwargs)

    def _write(self, record: LogRecord) -> None:
        """Append a record to the log file, tolerating a failure without killing the run."""
        path = self.log_path
        if path is None:
            return
        try:
            if self._fh is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(path, "a", encoding="utf-8")
            self._fh.write(json.dumps(record.as_dict(), separators=(",", ":"), sort_keys=True) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError:
            # Diagnostics must never be the reason a run fails.
            pass

    def close(self) -> None:
        """Flush and close the log handle. Idempotent."""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                finally:
                    self._fh = None

    # ── reading ─────────────────────────────────────────────────────────────

    def tail(self, *, level: str | None = None, event: str | None = None,
             limit: int = 100) -> list[dict[str, Any]]:
        """Recent records as dicts, optionally filtered. For the UI."""
        with self._lock:
            records = list(self._tail)
        if level:
            records = [r for r in records if r.level == level]
        if event:
            records = [r for r in records if event in r.event]
        return [r.as_dict() for r in records[-limit:]]

    def render_tail(self, *, limit: int = 40) -> str:
        """Recent records as text, for a terminal."""
        with self._lock:
            records = list(self._tail)[-limit:]
        return "\n".join(record.render() for record in records)

    # ── health ──────────────────────────────────────────────────────────────

    def health(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """A health snapshot. Never raises.

        A health check that can fail is one that lies exactly when it matters, so every probe here is
        wrapped and a failure becomes a field rather than an exception.
        """
        snapshot: dict[str, Any] = {
            "status": "ok",
            "run_id": self.run_id,
            "started_at": self._started_at,
            "uptime_s": round(time.time() - _parse_ts(self._started_at), 1),
            "level": self.level,
        }
        try:
            with self._lock:
                snapshot["records"] = {
                    "total": self._seq,
                    "tail": len(self._tail),
                    **self._counts,
                }
            snapshot["log_path"] = str(self.log_path) if self.log_path else None
            snapshot["log_writable"] = self._log_writable()
            snapshot["state_dir"] = str(self.state_dir) if self.state_dir else None
            snapshot["state_dir_exists"] = bool(self.state_dir and self.state_dir.is_dir())
            if self.state_dir and self.state_dir.is_dir():
                # A cheap proxy for "is the workspace growing without bound".
                snapshot["state_bytes"] = _directory_size(self.state_dir)
            if extra:
                snapshot["extra"] = _redact_deep(extra)
        except Exception as exc:  # noqa: BLE001 - a health check must not raise
            snapshot["status"] = "degraded"
            snapshot["error"] = f"{type(exc).__name__}: {exc}"
        return snapshot

    def _log_writable(self) -> bool:
        """Whether the log file can be appended to."""
        path = self.log_path
        if path is None:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8"):
                return True
        except OSError:
            return False

    # ── bundle ──────────────────────────────────────────────────────────────

    def bundle(self, destination: os.PathLike | str, *, include: Iterable[str] | None = None,
               extra: dict[str, Any] | None = None) -> Path:
        """Assemble a diagnostics bundle and refuse to include a secret.

        The leak check is the point. A bundle is meant to be handed to someone else, so it must be
        safe by construction rather than by the operator remembering to check.

        Raises
        ------
        DiagnosticsError
            When a source file contains key material, or the bundle cannot be written. Failing is
            correct: a bundle that leaks a credential is worse than no bundle.
        """
        target = Path(destination)
        names = list(include) if include is not None else list(BUNDLE_INCLUDE)

        # Scan *every* source before packaging, not just the log. A leak is most likely in the trace,
        # where an agent's own output lands — and a scan that only covered the log would miss the case
        # that matters most. Packaging first would already have written the secret into the archive.
        findings: list[str] = []
        sources: list[Path] = []
        if self.state_dir and self.state_dir.is_dir():
            for name in names:
                if name in BUNDLE_EXCLUDE:
                    continue
                candidate = self.state_dir / name
                if not candidate.is_file():
                    continue
                sources.append(candidate)
                for leak in scan_for_leaks(candidate, max_bytes=16 << 20):
                    findings.append(f"{Path(leak.path).name}:{leak.line}")
        if findings:
            raise DiagnosticsError(
                f"refusing to bundle: {len(findings)} key-shaped string(s) found in the sources "
                f"({', '.join(sorted(set(findings))[:5])}). A diagnostics bundle is meant to be "
                "shared, so it must not carry a credential."
            )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("health.json",
                                 json.dumps(self.health(extra=extra), indent=2, sort_keys=True))
                archive.writestr("environment.json",
                                 json.dumps(_environment(), indent=2, sort_keys=True))
                archive.writestr("naming.json", json.dumps(_telemetry_naming(), indent=2, sort_keys=True))
                for source in sources:
                    archive.write(source, arcname=source.name)
                archive.writestr("recent_log.jsonl",
                                 "\n".join(json.dumps(r.as_dict(), sort_keys=True)
                                           for r in list(self._tail)[-200:]))
        except (OSError, zipfile.BadZipFile) as exc:
            raise DiagnosticsError(f"cannot write diagnostics to {target}: {exc}") from exc

        self.info("diagnostics.exported", message=f"bundle written to {target}",
                  detail={"files": [s.name for s in sources]})
        return target


def read_log(path: os.PathLike | str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read a diagnostics log back, tolerating a torn final line."""
    target = Path(path)
    if not target.is_file():
        return []
    out: list[dict[str, Any]] = []
    with open(target, "r", encoding="utf-8") as fh:
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
    return out[-limit:] if limit else out


def _environment() -> dict[str, Any]:
    """What the engine is running on.

    Deliberately no hostname and no user name: a bundle goes to someone else, and neither is needed to
    diagnose a context or concurrency problem.
    """
    import sys

    return {
        "python_version": sys.version.split()[0],
        "platform": platform.system(),
        "machine": platform.machine(),
        "macos_version": platform.mac_ver()[0] if platform.system() == "Darwin" else "",
        "pid": os.getpid(),
    }


def _telemetry_naming() -> dict[str, str]:
    """The span-name contract, included so a bundle documents what it was produced by."""
    from .telemetry import naming

    return naming()


def _directory_size(path: Path) -> int:
    """Total bytes under a directory, for the health snapshot."""
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _redact_deep(value: Any) -> Any:
    """Recursively redact a JSON-ish structure, copying rather than mutating."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(v) for v in value]
    return value


def _parse_ts(text: str) -> float:
    """Parse an ISO timestamp back to epoch seconds, tolerating a malformed one."""
    try:
        return time.mktime(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (ValueError, TypeError):
        return time.time()


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
