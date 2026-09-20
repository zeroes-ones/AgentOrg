#!/usr/bin/env python3
"""runner_view.py — show the pinned runner the skill catalogue the engine actually runs against.

WHY THIS EXISTS
---------------
`workflow-runner.py` validates every node's `skill:` against the `skills/` directory beside its own
`scripts/` directory: it derives `ROOT` from its own `__file__`, and `validate-workflows.py` scans
`ROOT/skills`. The engine plans, hires and loads against a wider catalogue — the pinned library
*plus* the Owner's own roots, the overlay in :mod:`engine.skills.overlay`. So a skill the Owner
authored resolves everywhere in the engine and nowhere in the one process that decides whether the
graph may start:

    cannot start this run: ... node write: skill 'notes-writer' does not resolve under skills/

Every clause of that sentence is false except the one about the runner's own view: the name does
resolve, under the skills directory the Owner authored it in, and the Owner cannot act on a refusal
that names the library's directory as the place it should have been. The runner is a pinned,
SHA256-manifested artifact other tools consume, so the fix is not to change it — it is to *present*
it the view the engine already uses, which is the same principle as the overlay itself.

Python keeps `__file__` as the path it was given, so spawning `<view>/scripts/workflow-runner.py`,
where `<view>/scripts` is one symlink to the real one, makes the runner compute `ROOT = <view>` and
validate against `<view>/skills`. This module composes that directory.

DESIGN
------
- **The library is not written to, at all.** Every entry in the view is a symlink and nothing under
  the pinned tree is created, moved or opened for writing — a run leaves the library byte-identical,
  which is what keeps a content pin meaningful. (It is also the reason this belongs in the engine:
  the pin is precisely the rule that says the library may not be edited.)
- **The view is the overlay, not a second opinion.** The library's domain directories come from the
  library handle; the authored directories come from the *same source* the executing process loads
  through, asked rather than re-derived. Two scans of two root lists is how the runner's catalogue
  and the loader's came to disagree in the first place.
- **Composed only when it changes something.** A source with no authored skills answers with nothing
  and this returns ``None``, so the host invokes the pinned runner exactly as it did before. A
  library-only org is not affected by this module existing, and neither is a workspace the engine
  cannot write to.
- **Maintained entry by entry, never rebuilt wholesale.** Each link is created, re-pointed or pruned
  on its own. Replacing the directory would open a window in which the runner sees a partial
  catalogue — and it cannot be done atomically, because the runner reads a skill's `workflow:`
  contract lazily *during* a run, so a view removed under a live runner is not litter but a broken
  read. For the same reason the view is not a temp directory: it has no cleanup path to forget, it
  is reused by every run in the workspace, and it is still there when a refusal has to be explained.
- **A link that does not resolve is refused by name.** A dangling *domain* link would not crash
  anything: it would make every skill in that domain unresolvable and the run would refuse with
  "does not resolve under skills/", naming a node whose skill is sitting in the pinned library. That
  is the same unactionable refusal this module exists to remove, so it is turned into a loud one.

Usage:
    view = runner_view(library=lib, workspace=ws, source=overlay)
    runner = view.runner if view is not None else lib.files.runner
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import ENGINE_STATE_DIRNAME

__all__ = ["RunnerView", "ViewError", "runner_view", "VIEW_DIRNAME", "AUTHORED_DIRNAME"]


class ViewError(RuntimeError):
    """Raised when the composed view cannot be built, so the run must not start."""


#: The view lives under the workspace's `.agent_state/`, beside the runner's checkpoint and the
#: plugins the host generates. Deliberately not a temporary directory: it is reused across runs,
#: inspectable after a refusal, and has no cleanup path that a failure exit could skip.
VIEW_DIRNAME = "runner-view"
#: The one directory under the composed `skills/` that is not a library domain. Underscore-prefixed
#: so it cannot collide with a domain directory (`NN-name`), and so a person reading the view can
#: tell which entries are the Owner's own at a glance.
AUTHORED_DIRNAME = "_authored"


@dataclass(frozen=True)
class RunnerView:
    """A library root holding the composed skill catalogue, to invoke the runner from."""

    root: Path

    @property
    def runner(self) -> Path:
        """The runner *as this view presents it*: the same file, via a path that moves `ROOT`.

        The whole mechanism is this one link. Nothing in the runner, and nothing in the library it
        loads its siblings from, is aware that a view exists.
        """
        return self.root / "scripts" / "workflow-runner.py"


def runner_view(*, library: Any, workspace: Any, source: Any = None) -> RunnerView | None:
    """Compose the view a run needs, or return ``None`` when the plain library will do.

    ``None`` is the answer whenever no skill comes from one of the Owner's own roots — including
    when no source is passed at all, so a caller that never had a catalogue to present keeps the
    behaviour it had before. The host then invokes the pinned runner from the pinned library.

    Raises
    ------
    ViewError
        When a source did name authored skills but the view cannot be composed — an unwritable
        workspace, a library whose skills directory has gone, or a link that does not resolve.
    """
    authored = _authored(source)
    if not authored:
        return None

    root = Path(workspace) / ENGINE_STATE_DIRNAME / VIEW_DIRNAME
    skills = root / "skills"
    domains = _library_domains(library)
    try:
        skills.mkdir(parents=True, exist_ok=True)
        _link(root / "scripts", Path(library.files.root) / "scripts")
        for domain in domains:
            _link(skills / domain.name, domain)
        # The top-level prune keeps the namespace as a whole and lets `_compose_authored` prune its
        # contents, because only that function knows which names are still wanted — a prune here could
        # only guess, and guessing is how a stale name survives.
        _prune(skills, keep={domain.name for domain in domains} | {AUTHORED_DIRNAME})
        _compose_authored(skills / AUTHORED_DIRNAME, authored)
    except OSError as exc:
        raise ViewError(
            f"cannot compose the runner's view of this project's skills at {root}: {exc}.\n"
            f"  The engine writes it under {ENGINE_STATE_DIRNAME}/, so that directory has to be "
            "writable for a node whose skill the Owner authored to run."
        ) from exc
    return RunnerView(root=root)


# ── the catalogue ────────────────────────────────────────────────────────────


def _authored(source: Any) -> dict[str, Path]:
    """The Owner's skill directories, name -> directory, as the *source* resolves them.

    Asked of the source rather than derived from a root list here, because the source already owns
    the two rules this must not restate: which roots are searched, and in which order one wins. A
    source that reads only the pinned library answers with nothing, which is what makes the view
    opt-in per project instead of a new fixed step in every run.
    """
    lookup = getattr(source, "authored_dirs", None)
    if not callable(lookup):
        return {}
    try:
        found = dict(lookup())
    except OSError as exc:
        # Named rather than swallowed. An enumerated-but-unreadable root would otherwise drop those
        # skills from the runner's catalogue silently, and the node naming one would be refused with
        # "does not resolve" — the misleading refusal this module exists to remove.
        raise ViewError(f"cannot enumerate the Owner's own skills: {exc}") from exc
    return {str(name): Path(path) for name, path in sorted(found.items())}


def _library_domains(library: Any) -> list[Path]:
    """The library's own `skills/<domain>` directories, in name order.

    Only the top level, because the runner's scan is depth-2 (`skills/<domain>/<name>/SKILL.md`) and
    that is exactly the shape the library uses. A domain is linked whole rather than per skill: the
    library is a pinned corpus whose names the runner already resolves, so the only thing the view
    has to add is the Owner's.
    """
    nested = Path(library.files.nested_skills)
    try:
        domains = [entry for entry in sorted(nested.iterdir()) if entry.is_dir()]
    except OSError as exc:
        raise ViewError(f"cannot read the library's skills at {nested}: {exc}") from exc
    if not domains:
        raise ViewError(
            f"the library at {nested} holds no skill domains, so a node naming any library skill "
            "would be refused as unresolvable — refusing the run rather than reporting that"
        )
    return domains


def _compose_authored(namespace: Path, authored: dict[str, Path]) -> None:
    """Link each authored skill into the namespace directory, and prune the ones that have gone."""
    namespace.mkdir(parents=True, exist_ok=True)
    for name, directory in authored.items():
        _link(namespace / name, directory)
        # The link resolving is not the same as the skill being there: a directory with no readable
        # SKILL.md links fine and then enumerates as nothing, so a node naming it is refused by name.
        if not (namespace / name / "SKILL.md").is_file():
            raise ViewError(
                f"the skill {name!r} at {directory} has no readable SKILL.md, so it cannot be shown "
                "to the runner even though the source lists it"
            )
    _prune(namespace, keep=set(authored))


# ── links ────────────────────────────────────────────────────────────────────


def _link(link: Path, target: Path) -> None:
    """Point `link` at `target`, idempotently, and prove it resolves.

    Idempotence is what makes the view reusable rather than rebuilt: a link already aimed at the
    right target is left alone, so the second and the hundredth run in a workspace do nothing but
    look. ``target`` is written verbatim rather than resolved, so the view shows where a skill lives
    and a person debugging a refusal can follow it.

    The proof is the point. A *dangling* link does not crash the runner — it makes everything behind
    it unresolvable, and the run then refuses a node whose skill is right there in the pinned
    library. That is the same unactionable refusal this module exists to remove, so it is refused
    here instead, by name.
    """
    want = str(target)
    if link.is_symlink() and os.readlink(link) == want and link.exists():
        return
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.is_dir():
        # Never `rmtree`: the view is the engine's own directory, but a real directory inside it is
        # something the engine did not put there, and deleting it would be a guess.
        raise ViewError(
            f"{link} is a real directory where the runner's view keeps a link. Refusing to remove "
            "it — delete it by hand to let a run compose the view again."
        )
    try:
        os.symlink(want, link)
    except OSError as exc:
        raise ViewError(f"cannot link {link} -> {want}: {exc}") from exc
    if not link.exists():
        raise ViewError(
            f"{link} -> {want} does not resolve, so every skill behind it would be reported as "
            "missing"
        )


def _prune(directory: Path, keep: set[str]) -> None:
    """Remove the entries under `directory` that are no longer part of the catalogue.

    Stale links outlive their cause — a skill the Owner deleted, a domain a library upgrade dropped —
    and a stale name is worse than a missing one: the node validates, starts, and then fails at bind
    in the executing process, which is a failure that describes nothing. Pruning means the runner's
    catalogue falls back to refusing it by name, which is the honest answer.
    """
    for entry in sorted(directory.iterdir()):
        if entry.name in keep:
            continue
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        else:
            raise ViewError(
                f"{entry} is a real directory inside the runner's view, which holds only links. "
                "Refusing to remove it — delete it by hand to let a run compose the view again."
            )
