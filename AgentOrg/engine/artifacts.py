#!/usr/bin/env python3
"""artifacts.py — atomic, hashed, contained artifact I/O.

WHY THIS EXISTS
---------------
Artifacts are the only durable output of a run, and three separate failure modes
threaten them:

1. **Torn writes.** The macOS UI reads `.agent_state` while the engine writes it. A
   half-written JSON file that parses as valid JSON but means nothing is worse than an
   obvious truncation. Every write here is temp-then-`os.replace`, which is atomic on
   POSIX.
2. **Path escape.** An agent that writes `../../.ssh/authorized_keys` because a model
   produced a creative filename is a real attack surface. Every path is resolved and
   checked to be inside the workspace root, with symlinks followed before the check.
3. **Unexplained provenance.** "Which code did the reviewer actually see?" must be
   answerable. Every artifact therefore carries a sha256, a producer and a phase.

DESIGN
------
- **Containment is checked on the resolved path**, not the textual one, so a symlink
  inside the workspace pointing outside is caught.
- **Hashing is streamed** so a large artifact does not have to fit in memory twice.
- **Writes are idempotent by content**: writing identical bytes to the same path leaves
  the same hash, which is what lets the review loop's no-progress guard compare hashes
  meaningfully rather than comparing timestamps.
- **Reads are bounded** to guard against a runaway artifact exhausting memory.

Usage:
    store = ArtifactStore(workspace_root=Path("projects/demo"))
    ref = store.write("src/app.py", code, producer="ag_7f3a", artifact_type="change")
    store.read("src/app.py")
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state import ENGINE_STATE_DIRNAME

__all__ = ["WorkspaceError", "ArtifactRef", "PathLock", "ArtifactStore", "sha256_bytes"]

# A generous ceiling on a single artifact. Larger outputs should be split rather than
# silently consuming the engine's memory during a hash or a read.
MAX_ARTIFACT_BYTES = 16 << 20

_UNSAFE_SEGMENTS = {"..", "."}
_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)


class WorkspaceError(RuntimeError):
    """Raised on containment violation, size violation, or an I/O failure."""


def sha256_bytes(data: bytes) -> str:
    """Hex sha256 of a byte string."""
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    """Streamed hex sha256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class ArtifactRef:
    """A produced artifact and everything needed to trust and locate it."""

    type: str
    path: str
    sha256: str
    bytes: int
    produced_by: str | None = None
    phase: str | None = None
    node_id: str | None = None
    version: str = "1.0.0"

    def as_dict(self) -> dict[str, Any]:
        """Wire form, matching the handoff payload registry's artifact shape."""
        return {
            "type": self.type,
            "path": self.path,
            "sha": self.sha256,
            "bytes": self.bytes,
            "produced_by": self.produced_by,
            "phase": self.phase,
            "node_id": self.node_id,
        }


class PathLock:
    """A per-path mutex, so two agents cannot interleave writes to one file.

    Keyed by the resolved path: two agents writing `src/app.py` serialise, while
    agents writing different files proceed in parallel. This is the mechanism behind
    the concurrency design's "no two developers clobber `src/app.py`" guarantee.
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def for_path(self, path: Path) -> threading.Lock:
        """Return the lock for a path, creating it on first use."""
        key = str(path)
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def held_count(self) -> int:
        """Number of distinct paths currently tracked. For diagnostics."""
        with self._guard:
            return len(self._locks)


@dataclass
class ArtifactStore:
    """Contained, atomic, hashed artifact storage for one workspace.

    Parameters
    ----------
    workspace_root:
        The project directory. Every path this store touches must resolve inside it.
    allow_outside:
        Escape hatch used by tests that intentionally probe containment. Default False.
    """

    workspace_root: Path
    allow_outside: bool = False
    locks: PathLock = field(default_factory=PathLock)
    _index: dict[str, ArtifactRef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    # ── containment ─────────────────────────────────────────────────────────

    def resolve(self, relative: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
        """Resolve a workspace-relative path, enforcing containment.

        The check runs on the *resolved* path so a symlink inside the workspace that
        points outside is rejected. A traversal segment is rejected outright before
        resolution, because on some filesystems resolution alone is insufficient.

        Raises
        ------
        WorkspaceError
            On an absolute path, a traversal segment, a Windows-reserved name, or any
            path that resolves outside the workspace root.
        """
        raw = str(relative).strip()
        if not raw:
            raise WorkspaceError("artifact path is empty")
        candidate = Path(raw)
        if candidate.is_absolute():
            raise WorkspaceError(
                f"artifact path must be workspace-relative, got absolute path: {raw}"
            )
        for part in candidate.parts:
            if part in _UNSAFE_SEGMENTS:
                raise WorkspaceError(f"artifact path contains a traversal segment: {raw}")
            if _WINDOWS_RESERVED.match(part):
                raise WorkspaceError(f"artifact path uses a reserved name: {part!r}")

        target = (self.workspace_root / candidate)
        # resolve(strict=False) normalises symlinks that exist; a non-existent tail is
        # kept literal so we can validate before creating anything.
        resolved = target.resolve(strict=False)
        if not self.allow_outside:
            try:
                resolved.relative_to(self.workspace_root)
            except ValueError:
                raise WorkspaceError(
                    f"artifact path escapes the workspace: {raw}\n"
                    f"  resolved: {resolved}\n  workspace: {self.workspace_root}"
                ) from None
        if must_exist and not resolved.is_file():
            raise WorkspaceError(f"artifact not found: {raw}")
        return resolved

    # ── writing ─────────────────────────────────────────────────────────────

    def write(self, relative: str, content: str | bytes, *,
              producer: str | None = None,
              artifact_type: str = "file",
              phase: str | None = None,
              node_id: str | None = None,
              encoding: str = "utf-8") -> ArtifactRef:
        """Atomically write an artifact and return its reference.

        Serialised per resolved path, written to a sibling temp file, fsynced, then
        `os.replace`d into place. A reader therefore sees either the previous complete
        content or the new complete content — never a blend.
        """
        data = content.encode(encoding) if isinstance(content, str) else content
        if len(data) > MAX_ARTIFACT_BYTES:
            raise WorkspaceError(
                f"artifact {relative!r} is {len(data)} bytes, over the "
                f"{MAX_ARTIFACT_BYTES} byte limit; split it or reference it by path"
            )
        target = self.resolve(relative)
        lock = self.locks.for_path(target)
        with lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
            try:
                with open(tmp, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
            except OSError as exc:
                # Clean up the temp file; leaving it would look like a real artifact
                # to a directory listing in the UI.
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise WorkspaceError(f"failed to write {relative}: {exc}") from exc
        digest = sha256_bytes(data)
        ref = ArtifactRef(
            type=artifact_type,
            path=str(target.relative_to(self.workspace_root)),
            sha256=digest,
            bytes=len(data),
            produced_by=producer,
            phase=phase,
            node_id=node_id,
        )
        self._index[ref.path] = ref
        return ref

    def append_line(self, relative: str, line: str, *, encoding: str = "utf-8") -> ArtifactRef:
        """Append one line to a growing file, returning a ref for the whole file.

        Used for journals and mailboxes where the record is the line, but the caller
        still wants a stable hash of the accumulated file to detect tampering.
        """
        target = self.resolve(relative)
        lock = self.locks.for_path(target)
        with lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(target, "a", encoding=encoding) as fh:
                    fh.write(line.rstrip("\n") + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                raise WorkspaceError(f"failed to append to {relative}: {exc}") from exc
        digest = _sha256_file(target)
        ref = ArtifactRef(
            type="journal",
            path=str(target.relative_to(self.workspace_root)),
            sha256=digest,
            bytes=target.stat().st_size,
        )
        self._index[ref.path] = ref
        return ref

    # ── reading ─────────────────────────────────────────────────────────────

    def read(self, relative: str, *, encoding: str = "utf-8") -> str:
        """Read a text artifact. Raises :class:`WorkspaceError` when absent or oversized."""
        return self.read_bytes(relative).decode(encoding)

    def read_bytes(self, relative: str) -> bytes:
        """Read an artifact's bytes, bounded and containment-checked."""
        target = self.resolve(relative, must_exist=True)
        size = target.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise WorkspaceError(
                f"artifact {relative!r} is {size} bytes, over the read limit; "
                "read it in slices or raise MAX_ARTIFACT_BYTES deliberately"
            )
        try:
            return target.read_bytes()
        except OSError as exc:
            raise WorkspaceError(f"failed to read {relative}: {exc}") from exc

    def exists(self, relative: str) -> bool:
        """True when the artifact exists inside the workspace."""
        try:
            return self.resolve(relative, must_exist=True).is_file()
        except WorkspaceError:
            return False

    def hash_of(self, relative: str) -> str | None:
        """Current on-disk sha256, or None when absent.

        The review loop's no-progress guard compares this across attempts: if the
        developer returned byte-identical code, rotating or retrying cannot help.
        """
        try:
            return _sha256_file(self.resolve(relative, must_exist=True))
        except (WorkspaceError, OSError):
            return None

    # ── index ───────────────────────────────────────────────────────────────

    def index(self) -> dict[str, ArtifactRef]:
        """Artifacts written by this store, keyed by workspace-relative path."""
        return dict(self._index)

    def index_dict(self) -> dict[str, dict[str, Any]]:
        """Serialisable index for persistence and events."""
        return {path: ref.as_dict() for path, ref in sorted(self._index.items())}

    def list_files(self, subdir: str = ".") -> list[str]:
        """Workspace-relative files under a subdirectory, sorted, secrets excluded.

        Used by the inspector view and the diagnostics bundle. The engine's own state directory is
        excluded for the same reason tools cannot reach it: it is not project content, and listing it
        would put checkpoints and effect journals in front of a person as though they were the user's
        files.
        """
        base = self.resolve(subdir) if subdir != "." else self.workspace_root
        if not base.exists():
            return []
        out: list[str] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            if "__pycache__" in path.parts:
                continue
            if ENGINE_STATE_DIRNAME in path.parts:
                continue
            out.append(str(path.relative_to(self.workspace_root)))
        return out
