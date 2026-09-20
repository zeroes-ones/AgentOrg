#!/usr/bin/env python3
"""overlay.py — the library plus the Owner's own skills, as one source.

WHY THIS EXISTS
---------------
Authoring a skill is only useful if the engine will *use* it. The library source reads the pinned
checkout and nothing else — correctly, because the pin is what makes a prompt reproducible. So a
custom skill needs somewhere else to live (see :mod:`engine.usercfg`) and something to read it.

This is that reader. It is a decorator, not a fork: it delegates to the library source for every
library skill and only intercepts a name it actually holds. That means the pinned corpus keeps its
guarantees, and a custom skill is parsed by the exact same parser and enforced by the exact same
checklist machinery.

DESIGN
------
- **User skills win a name clash.** If you author a skill named `code-reviewer`, yours is the one that
  runs. That is what makes an override possible without editing the library — and it is stated here
  because silently preferring the library instead would make the override a no-op the user cannot see.
- **Both roots are searched, project first.** So a project-local skill beats a global one of the same
  name, consistent with every other user-content lookup.
- **A bad user skill fails loudly.** A skill that will not parse is a name that looks present and
  breaks at bind time; it is reported with its path instead.
- **Containment is enforced for user skills too.** A symlink out of the skills directory is refused,
  exactly as the library source refuses one out of the library.

Usage:
    source = OverlaySkillSource(library_source, project=Path("."))
    bundle = source.load("my-skill")      # mine if authored, else the library's
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from .. import usercfg
from .bundle import SkillBundle, SkillError, parse_skill
from .source import SkillSource

__all__ = ["OverlaySkillSource"]


class OverlaySkillSource(SkillSource):
    """The library's skills, with the Owner's skills layered over them.

    Parameters
    ----------
    base:
        The library-backed source. Every unknown name is delegated to it.
    project:
        The project root to search for ``.agentorg/skills``. Defaults to the current directory's
        project root, found by walking up.
    include_global:
        Whether ``~/.agentorg/skills`` is also searched. On by default; a caller that wants strict
        per-project isolation can turn it off.
    skill_roots:
        The roots to search, verbatim and highest priority first. When given, :meth:`roots` returns
        them and ``project``/``include_global`` are not consulted at all.
    """

    def __init__(self, base: Any, *, project: Path | str | None = None,
                 include_global: bool = True,
                 skill_roots: list[Path | str] | None = None) -> None:
        self.base = base
        self.project = project
        self.include_global = include_global
        #: An explicit root list, when the caller already knows it. This exists for the *child*: the
        #: generated executor plugin runs in another process, and the roots it must search are
        #: whatever the parent planned against. Re-deriving them there is how a global skill became
        #: invisible to the process that ran the node — the parent planned and hired against a skill
        #: the child had never heard of, and nothing said so.
        self.skill_roots = [Path(p).expanduser() for p in skill_roots] if skill_roots else None
        self._cache: dict[str, tuple[str, SkillBundle]] = {}
        self._lock = threading.RLock()
        self._names: list[str] | None = None
        #: Skill names that came from a user root, so a caller can report provenance.
        self.user_names: set[str] = set()

    # ── enumeration ─────────────────────────────────────────────────────────

    def roots(self) -> list[Path]:
        """The user skill roots, highest priority first.

        An explicit ``skill_roots`` wins outright so a handed-over list is used exactly as given —
        the point of passing it is that the two processes agree, which re-deriving cannot promise.
        """
        if self.skill_roots is not None:
            return list(self.skill_roots)
        return [r / "skills" for r in usercfg.roots(project=self.project,
                                                    include_global=self.include_global)]

    def _user_paths(self) -> dict[str, Path]:
        """Every user-authored skill, name -> ``SKILL.md``, project-first.

        Project-first means a later root does **not** overwrite an earlier one, so the project's
        version of a name survives the global one.
        """
        found: dict[str, Path] = {}
        for root in self.roots():
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                skill_md = entry / "SKILL.md"
                if entry.is_dir() and skill_md.is_file() and entry.name not in found:
                    found[entry.name] = skill_md
        return found

    def names(self) -> list[str]:
        with self._lock:
            if self._names is None:
                base_names = set(self.base.names())
                user = self._user_paths()
                self.user_names = set(user)
                self._names = sorted(base_names | set(user))
            return list(self._names)

    def has(self, name: str) -> bool:
        return name in self._user_paths() or self.base.has(name)

    def authored_dirs(self) -> dict[str, Path]:
        """The Owner's skill *directories*, name -> directory, project-first.

        The same mapping :meth:`_user_paths` builds, in the shape a caller that presents the
        catalogue rather than loading from it needs: :mod:`engine.runner_view` links these into the
        view the pinned runner validates against, so the runner's catalogue is *this* scan instead of
        a second one that could order the roots differently and agree about nothing.

        Directories, not ``SKILL.md`` paths, because a directory is what a symlink wants — and the
        *unresolved* directory, so a user's own symlink is followed by the reader exactly as
        :meth:`names` promises it will be.
        """
        return {name: path.parent for name, path in self._user_paths().items()}

    # ── loading ─────────────────────────────────────────────────────────────

    def _user_path(self, name: str) -> Path | None:
        path = self._user_paths().get(name)
        if path is None:
            return None
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        # Containment: a symlink pointing out of the user skills directory is refused, for the same
        # reason the library source refuses one out of the library.
        if not any(_within(resolved, root) for root in self.roots()):
            raise SkillError(
                f"custom skill {name!r} resolves outside the skills directory: {resolved}. "
                "Refusing to read it."
            )
        return resolved

    def text_of(self, name: str) -> str:
        """Raw text, for hashing and for the verbatim prompt slot."""
        path = self._user_path(name)
        if path is None:
            return self.base.text_of(name)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError(f"cannot read custom skill {name!r} at {path}: {exc}") from exc

    def load(self, name: str) -> SkillBundle:
        """Load one skill: the Owner's when present, otherwise the library's.

        Parsed with the *same* :func:`parse_skill` the library uses, so a custom skill is enforced
        identically — criteria, checklist ids and the evidence requirement all apply.
        """
        path = self._user_path(name)
        if path is None:
            return self.base.load(name)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError(f"cannot read custom skill {name!r} at {path}: {exc}") from exc
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._cache.get(name)
            if cached is not None and cached[0] == digest:
                return cached[1]
        try:
            bundle = parse_skill(name, text, source_path=str(path))
        except Exception as exc:  # noqa: BLE001 - name the file, not just the symptom
            raise SkillError(
                f"custom skill {name!r} at {path} could not be parsed: {exc}"
            ) from exc
        with self._lock:
            self._cache[name] = (digest, bundle)
        return bundle

    def bundle(self, name: str) -> SkillBundle:
        """Load one skill — the Owner's when present, otherwise the library's.

        Defined rather than left to :meth:`__getattr__`: the passthrough would reach the *base*
        source, and a project skill sharing a library skill's name would be silently ignored — the
        one thing the overlay exists to prevent. `load` already resolves overlay-first, so this
        stays a one-line alias rather than a second resolution order to keep in step.
        """
        return self.load(name)

    def load_many(self, names: list[str]) -> dict[str, SkillBundle]:
        return {name: self.load(name) for name in names}

    # ── passthrough ─────────────────────────────────────────────────────────

    def __getattr__(self, item: str) -> Any:
        """Delegate anything else — `library`, `library_root`, `fingerprint` — to the base source.

        Delegation rather than reimplementation: the wrapped source owns the library semantics, and a
        copy here would be a second place for them to drift.
        """
        return getattr(self.base, item)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False
