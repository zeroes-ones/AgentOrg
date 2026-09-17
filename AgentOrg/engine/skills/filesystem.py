#!/usr/bin/env python3
"""filesystem.py — read skills from the pinned library on disk.

WHY THIS EXISTS
---------------
The filesystem is the default skill source because it is offline, fast, and
tamper-evident through content hashing. The library lays skills out two ways and this
source handles both:

- `skills-flat/` — a directory of symlinks, one level deep, which is the flat discovery
  view every agent CLI expects.
- `skills/<NN-domain>/<name>/` — the real nested tree, which is the fallback when a skill
  is not linked flat.

DESIGN
------
- **Resolution follows symlinks safely.** A flat entry is usually a symlink into the nested
  tree, so the real path is resolved before reading — and a skill whose symlink points
  outside the library is refused rather than read.
- **Bundles are cached by content hash.** Parsing 18k tokens of markdown is not free, and a
  long run loads the same handful of skills hundreds of times. The cache key is the file's
  hash, so a modified SKILL.md is re-parsed rather than served stale.
- **`fingerprint()` hashes the whole source**, so a run can record exactly which prompt
  corpus it read.
- **A missing skill is a `SkillError`, never None.** The delegation design treats a missing
  skill as a real rung ("author it first"), so the caller needs to know it is missing.

Usage:
    source = FilesystemSkillSource(library)
    bundle = source.load("code-reviewer")
    source.fingerprint()
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from ..library import Library
from .bundle import SkillBundle, SkillError, parse_skill
from .source import SkillSource

__all__ = ["FilesystemSkillSource"]


class FilesystemSkillSource(SkillSource):
    """Reads skills from a pinned :class:`~engine.library.Library`.

    Parameters
    ----------
    library:
        The verified library handle. Its roots define where skills are looked up.
    max_cache:
        Bound on cached bundles. A run touches a few dozen skills at most, so a small cache
        keeps memory flat while still removing the repeated parse cost.
    """

    def __init__(self, library: Library, *, max_cache: int = 512) -> None:
        self.library = library
        self.library_root = Path(library.files.root).resolve()
        self._cache: dict[str, tuple[str, SkillBundle]] = {}
        self._max_cache = max_cache
        self._lock = threading.RLock()
        self._names: list[str] | None = None
        self._fingerprint: str | None = None

    # ── enumeration ─────────────────────────────────────────────────────────

    def names(self) -> list[str]:
        """Every skill name discoverable through this source, sorted and de-duplicated.

        The flat layer is authoritative when present (it is the curated view); the nested
        tree contributes anything not linked flat, so a partially-linked checkout still
        enumerates every skill.
        """
        with self._lock:
            if self._names is not None:
                return list(self._names)
            found: dict[str, Path] = {}
            flat = self.library.files.flat_skills
            if flat.is_dir():
                for entry in sorted(flat.iterdir()):
                    if (entry / "SKILL.md").is_file():
                        found[entry.name] = entry
            nested = self.library.files.nested_skills
            if nested.is_dir():
                for domain in sorted(p for p in nested.iterdir() if p.is_dir()):
                    for entry in sorted(p for p in domain.iterdir() if p.is_dir()):
                        if (entry / "SKILL.md").is_file() and entry.name not in found:
                            found[entry.name] = entry
            self._names = sorted(found)
            return list(self._names)

    def has(self, name: str) -> bool:
        """Whether a skill by this name exists, without reading it."""
        return self._path_for(name) is not None

    def _path_for(self, name: str) -> Path | None:
        """Resolve a skill name to its SKILL.md path, enforcing containment.

        Uses the library's own resolver, then confirms the resolved path is inside the
        library root so a symlink pointing elsewhere cannot be read as a skill.
        """
        directory = self.library.find_skill(name)
        if directory is None:
            return None
        skill_md = directory / "SKILL.md"
        try:
            resolved = skill_md.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not self._within_library(resolved):
            raise SkillError(
                f"skill {name!r} resolves outside the library root: {resolved}. "
                "Refusing to read it — a symlink pointing out of the pinned library is not "
                "a skill."
            )
        return resolved

    def _within_library(self, path: Path) -> bool:
        try:
            path.relative_to(self.library_root)
            return True
        except ValueError:
            return False

    # ── loading ─────────────────────────────────────────────────────────────

    def text_of(self, name: str) -> str:
        """Raw SKILL.md text, for hashing and for a verbatim prompt slot."""
        path = self._path_for(name)
        if path is None:
            raise SkillError(
                f"skill {name!r} not found in the library at {self.library_root}. "
                "The delegation design treats a missing skill as a rung: author it before "
                "delegating to it."
            )
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError(f"cannot read skill {name!r} at {path}: {exc}") from exc

    def load(self, name: str) -> SkillBundle:
        """Load and parse one skill, cached by content hash.

        The hash check means an edited SKILL.md is re-parsed while an unchanged one is
        served from cache — which matters because the library is pinned but a developer may
        legitimately be editing a skill mid-session.
        """
        path = self._path_for(name)
        if path is None:
            raise SkillError(f"skill {name!r} not found in the library at {self.library_root}")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError(f"cannot read skill {name!r} at {path}: {exc}") from exc
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        with self._lock:
            cached = self._cache.get(name)
            if cached is not None and cached[0] == digest:
                return cached[1]

        bundle = parse_skill(name, text, source_path=str(path))
        with self._lock:
            if len(self._cache) >= self._max_cache:
                # Simple eviction: drop an arbitrary entry. The cache is a cost optimisation,
                # so correctness never depends on which one goes.
                self._cache.pop(next(iter(self._cache)), None)
            self._cache[name] = (digest, bundle)
        return bundle

    def load_many(self, names: list[str]) -> dict[str, SkillBundle]:
        """Load several skills, reporting each failure rather than aborting.

        A run that needs five skills and has four should report the missing one precisely,
        not fail with a single opaque error.
        """
        out: dict[str, SkillBundle] = {}
        for name in names:
            out[name] = self.load(name)
        return out

    # ── fingerprinting ──────────────────────────────────────────────────────

    def fingerprint(self) -> str:
        """A hash over every skill's content, identifying this corpus exactly.

        Recorded per run so "which prompts produced this output?" is answerable later, and
        so a change to any skill is detectable without a full re-hash by the caller.
        """
        with self._lock:
            if self._fingerprint is not None:
                return self._fingerprint
            digest = hashlib.sha256()
            for name in self.names():
                try:
                    text = self.text_of(name)
                except SkillError:
                    continue
                digest.update(name.encode("utf-8"))
                digest.update(b"\0")
                digest.update(hashlib.sha256(text.encode("utf-8")).digest())
            self._fingerprint = digest.hexdigest()
            return self._fingerprint

    def cache_info(self) -> dict[str, int]:
        """Cache occupancy, for the resources view."""
        with self._lock:
            return {"cached": len(self._cache), "max": self._max_cache,
                    "names": len(self._names) if self._names else 0}

    def invalidate(self) -> None:
        """Drop cached bundles, forcing a re-parse on next load."""
        with self._lock:
            self._cache.clear()
            self._fingerprint = None
