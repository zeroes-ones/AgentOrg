#!/usr/bin/env python3
"""usercfg.py — where the Owner's own content lives, and how the two roots combine.

WHY THIS EXISTS
---------------
The Skills library is **immutable by design**: it is pinned to a commit and every file is hashed into
a manifest, so writing a custom skill into it would either fail verification or silently corrupt the
pin. The same is true of the roster: it is yours, not the library's.

So user content needs its own home, and there are two sensible places for it:

- **Global** — ``~/.agentorg/`` — your personal library and roster, shared by every project.
- **Project** — ``./.agentorg/`` — content that belongs to one repository and should travel with it.

The rule is **project wins, otherwise global**. That gives the common cases without a config dialect:
put a reusable skill in the global root once, or override it for one project by creating the same
name locally.

DESIGN
------
- **Precedence is per item, not per root.** A project that defines *one* agent still inherits every
  global agent; the roots are merged item by item with the project's version winning a name clash.
  A root-level override would force a project with one custom skill to redeclare all the others.
- **Discovery is explicit and ordered.** :func:`roots` returns the roots highest-priority first, so a
  caller that just wants "the first file named X" gets the right one by construction.
- **Nothing here writes outside the two roots**, and the project root is gitignored by convention so
  a personal roster cannot be committed by accident.
- **An absent root is not an error.** Most projects will have no ``.agentorg/`` at all; that is the
  normal case and must not raise.

Usage:
    from engine import usercfg
    for root in usercfg.roots(project=Path(".")):      # highest priority first
        ...
    usercfg.skills_dir(project=Path(".")).mkdir(parents=True, exist_ok=True)
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "GLOBAL_DIRNAME", "PROJECT_DIRNAME", "ENV_OVERRIDE",
    "global_root", "project_root", "roots",
    "skills_dir", "roster_path", "ensure_roots",
]

#: The per-user directory, created on demand. Kept out of the repository on purpose.
GLOBAL_DIRNAME = ".agentorg"
#: The per-project directory. Same name so the layout is obvious from either side.
PROJECT_DIRNAME = ".agentorg"
#: An explicit override, so a test — or a user with an unusual layout — can pin the global root.
ENV_OVERRIDE = "AGENTORG_HOME"


def global_root() -> Path:
    """The user-global root, honouring ``$AGENTORG_HOME`` when set.

    The environment variable exists so the whole module is testable without touching a real home
    directory, and so a user who keeps their dotfiles elsewhere can point at them.
    """
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()
    return Path.home() / GLOBAL_DIRNAME


def project_root(start: os.PathLike | str | None = None) -> Path:
    """The nearest project root, by walking up for a ``.agentorg`` or a repository marker.

    Walking up matters: a command run from a subdirectory of a project should still find that
    project's content, which is what a person expects and what a per-project roster requires.
    """
    current = Path(start or Path.cwd()).expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / PROJECT_DIRNAME).is_dir():
            return candidate / PROJECT_DIRNAME
        # A repository marker also anchors the project, so the first `hire` in a fresh checkout lands
        # beside the code rather than in the home directory.
        if (candidate / ".git").exists():
            return candidate / PROJECT_DIRNAME
    return current / PROJECT_DIRNAME


def roots(*, project: os.PathLike | str | None = None,
          include_global: bool = True) -> list[Path]:
    """The content roots, highest priority first.

    Project before global, because the project is the more specific context. Callers iterate this
    order and take the first hit, which is what makes "project wins" true by construction rather
    than by a comparison each caller has to remember to make.
    """
    ordered: list[Path] = [project_root(project)]
    if include_global:
        ordered.append(global_root())
    return ordered


def skills_dir(*, project: os.PathLike | str | None = None,
               global_: bool = False) -> Path:
    """Where custom skills live: ``skills/`` under the chosen root.

    Skills are stored one directory per skill, each holding a ``SKILL.md``, because that is the
    library's own layout — so a user skill and a library skill are the same shape and the same parser
    reads both. A different layout here would mean a second parser, which is how two formats drift.
    """
    base = global_root() if global_ else project_root(project)
    return base / "skills"


def roster_path(*, project: os.PathLike | str | None = None,
                global_: bool = False) -> Path:
    """Where the Owner's roster is persisted: ``roster.json`` under the chosen root."""
    base = global_root() if global_ else project_root(project)
    return base / "roster.json"


def ensure_roots(*, project: os.PathLike | str | None = None,
                 global_: bool = False) -> Path:
    """Create the chosen root's ``skills/`` directory and return the root.

    Created lazily, on the first write. A read path never creates anything, so inspecting the
    configuration cannot leave directories behind in a directory nobody meant to modify.
    """
    root = global_root() if global_ else project_root(project)
    (root / "skills").mkdir(parents=True, exist_ok=True)
    return root
