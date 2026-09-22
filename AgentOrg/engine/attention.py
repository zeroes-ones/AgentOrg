#!/usr/bin/env python3
"""attention.py — every workspace that is waiting on a person, in one answer.

WHY THIS EXISTS
---------------
Every other reading in the engine is scoped to *one* workspace. `status` answers "where is this run",
`activity` answers "what is happening here", `flow` answers "who is on what here" — and `serve` is
bound to a single workspace, so the console it drives can only ever ask those questions about the
project it was launched on. The register above them (`portfolio.json`) covers the orgs the principal
has *registered*, which on a real machine is not the same list as the projects that exist.

So a run parked at a gate in a folder nobody had registered was invisible to both surfaces: the app
said "nothing is waiting" because its one workspace had nothing waiting, and the CLI could only reach
the project by a slug you had to already know. The user's report of it is exact — "Not sure why PM is
still blocked Priya, not good UI on how to cleanup and take actions" and "I still don't understand
what actions I need to take" — and the fix is not a better panel on one workspace: it is one answer
that *enumerates* the workspaces, says what each is waiting for, and names the step that resolves it.

DESIGN
------
- **Derived, never stored.** Like `activity` and `flow`, this writes nothing. It reads the same
  checkpoint the other two read and folds it into a shorter shape.
- **One assembly, two surfaces.** The CLI's `attention` and the console's `attention` command render
  the *same* document from here, for the reason `syscap.console_payload` states: a fact assembled
  twice is a fact two surfaces can disagree about. Each surface only decides how to print it.
- **The per-workspace reading is `activity`'s, not a second one.** Whether a workspace is waiting is
  `activity._next_action`'s own answer — the string `none` is the engine's word for "nothing here
  needs a person" — so no predicate is re-written here and the roll-up cannot disagree with the story
  the same workspace's `activity` tells. The one difference is cost: `build_activity` is asked for a
  one-entry trace tail and a one-entry timeline, because this module renders neither.
- **Waiting states are on disk by construction.** The engine writes the checkpoint *before* it parks
  (a gate, a prepared plan), so reading `run_state.json` finds every waiting state without asking a
  live orchestrator — which is what makes a roll-up over folders this process never loaded possible.
- **Bounded and tolerant.** A workspace with no run, a half-written checkpoint or a folder that is not
  a workspace at all is skipped rather than fatal; "nothing is waiting" is a normal answer, said
  calmly, and not an error.
- **The workspace that is not an org is offered as one.** The one write that is *not* bound to a
  workspace is `portfolio_add` (see :func:`org_link`), so the engine composes that step here rather
  than letting a surface invent an action `serve` cannot perform.

Usage:
    from engine.attention import build_attention
    report = build_attention(root="AgentOrg/projects", portfolio=portfolio)
    report["workspaces"]                  # one row per workspace waiting on a person
    report["workspaces"][0]["next_action"]["command"]   # the exact command that resolves it
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .activity import build_activity

__all__ = ["build_attention", "attention_row", "org_link", "ATTENTION_VERSION"]

#: Bumped when the report's shape changes incompatibly, so a cached consumer can tell.
ATTENTION_VERSION = "1.0.0"

#: The reading order for the rows, keyed by the engine's own next-action kinds (`activity._next_action`).
#:
#: Deliberately *not* a second judgement of urgency — each workspace's urgency is its own `next_action`,
#: which the engine already ordered. This only groups what a person scans first: a decision that blocks
#: everything, then work nobody can do (a hire), then a stop to look at, then the rest. An unknown kind
#: sorts last rather than being dropped, the same safe reading `_NEXT_PERFORMABLE` follows.
_URGENCY: dict[str, int] = {
    "decide": 0, "approve_plan": 0,
    "hire": 1,
    "retry": 2, "investigate": 2,
    "resume": 3, "start": 3,
}


def _projects_root(root: Any = None) -> Path:
    """The directory of projects — the one `Workspace.for_project` would default to.

    Spelled once here so the roll-up and `state.Workspace.list_projects` cannot disagree about where
    the projects are; both default to the engine's own ``projects/`` beside this package.
    """
    if root is not None:
        return Path(root).expanduser()
    return Path(__file__).resolve().parent.parent / "projects"


def _workspaces_for(root: Any, workspaces: Iterable[Any] | None) -> list[Any]:
    """The workspaces to read: the ones given, or every project directory under the root.

    `list_projects` is the engine's own lister, and `Workspace.attach` is used for each name rather
    than `for_project` because the directory already exists: a folder whose name is not a valid slug
    would be refused by `for_project` and is a workspace all the same. A name that cannot be attached
    at all is skipped — a stray file under the projects root is not an error in a roll-up.
    """
    from .state import Workspace

    if workspaces is not None:
        return list(workspaces)
    base = _projects_root(root)
    found: list[Any] = []
    for name in Workspace.list_projects(base):
        try:
            found.append(Workspace.attach(base / name))
        except Exception:  # noqa: BLE001 - not a usable folder, so not a workspace to report
            continue
    return found


def _is_waiting(standing: dict[str, Any]) -> bool:
    """Whether the engine says a person is needed here — its own answer, not a new predicate."""
    kind = str((standing.get("next_action") or {}).get("kind") or "")
    return kind not in ("", "none")


def _org_for(portfolio: Any, workspace: Any) -> Any:
    """The org entry this workspace already is, or `None`.

    Matched by **folder** first, and by slug only for a path-less entry — because a path-less org *is*
    a managed project keyed on its slug (`portfolio.workspace_for`), so the slug is its whole address.
    An org that names a folder elsewhere is never matched by a slug it happens to share with a project
    under this root, which would have reported a different folder as already registered.
    """
    path = str(Path(str(getattr(workspace, "path", ""))).resolve())
    slug = str(getattr(workspace, "slug", ""))
    for entry in getattr(portfolio, "orgs", None) or []:
        named = str(getattr(entry, "path", "") or "")
        if named:
            if str(Path(named).expanduser().resolve()) == path:
                return entry
        elif str(getattr(entry, "slug", "")) == slug:
            return entry
    return None


def org_link(portfolio: Any, workspace: Any) -> dict[str, Any]:
    """Whether this workspace is already an org, and the exact step that would make it one.

    **Why an "adopt" step and not the decision itself.** `serve` is bound to one workspace and every run
    command it answers acts on that one, so "decide the gate in *that* project" is not a command this
    engine has — which is why a run in another folder could not be found *or* acted on. The one write
    that is not bound to a workspace is `portfolio_add`: it registers a folder, after which the org is
    listed in the register, selectable, and from there every run command works on it (the console's own
    Portfolio offers "Switch to it and decide"). So the engine composes that step here — the name, slug
    and absolute path the payload needs — and each surface only decides how to render it. That keeps
    the console from inventing an action the engine would refuse.

    An already-registered workspace carries `registered` and its `ref` and **no adopt payload**: there
    is nothing to add, and the entry is already addressable by `portfolio_select`.
    """
    entry = _org_for(portfolio, workspace) if portfolio is not None else None
    if entry is not None:
        return {"registered": True, "id": str(getattr(entry, "id", "")),
                "slug": str(getattr(entry, "slug", "")), "ref": str(getattr(entry, "id", "")),
                "adopt": None, "adopt_command": ""}
    name = str(getattr(workspace, "display_name", "") or getattr(workspace, "slug", ""))
    slug = str(getattr(workspace, "slug", ""))
    path = str(getattr(workspace, "path", ""))
    return {
        "registered": False, "id": "", "slug": slug, "ref": "",
        "adopt": {"name": name, "slug": slug, "path": path},
        "adopt_command": f'engine.cli portfolio add "{name}" --slug {slug} --path "{path}"',
    }


def attention_row(workspace: Any, standing: dict[str, Any], *,
                  portfolio: Any = None) -> dict[str, Any]:
    """One workspace that is waiting on a person, as the surfaces render it.

    Every sentence here is the engine's own, taken from the workspace's `activity` report: `headline`
    is what it is doing, `waiting_for` is the label of the step that resolves it, and `next_action`
    carries the command. Nothing is re-worded, so a row and the `activity` view of the same workspace
    cannot describe it two ways.
    """
    action = standing.get("next_action") or {}
    return {
        "slug": str(getattr(workspace, "slug", "")),
        "name": str(getattr(workspace, "display_name", "") or getattr(workspace, "slug", "")),
        "path": str(getattr(workspace, "path", "")),
        "phase": str(standing.get("phase") or "idle"),
        "headline": str(standing.get("headline") or ""),
        "objective": str(standing.get("objective") or ""),
        "stop_reason": str(standing.get("stop_reason") or ""),
        "gate": standing.get("gate"),
        "waiting_for": str(action.get("label") or ""),
        "next_action": action,
        "org": org_link(portfolio, workspace),
    }


def _order(row: dict[str, Any]) -> tuple[int, str]:
    """Group the rows a person would scan first, then order stably by name."""
    kind = str((row.get("next_action") or {}).get("kind") or "")
    return (_URGENCY.get(kind, len(_URGENCY)), str(row.get("name") or row.get("slug") or ""))


def build_attention(root: Any = None, *, portfolio: Any = None,
                    workspaces: Iterable[Any] | None = None) -> dict[str, Any]:
    """Assemble "who is waiting on me" across every workspace under a projects root.

    Parameters
    ----------
    root:
        The projects directory. Defaults to the engine's own ``projects/``.
    portfolio:
        The register, so a row can say whether its workspace is already an org and what the step to
        adopt it is. Optional: with no register every workspace is reported as an adoptable one, which
        is the honest reading of "there is no register".
    workspaces:
        Explicit `Workspace` objects, for a caller that already holds them (the tests). Omitted, every
        project directory under `root` is read.

    Returns the document the CLI's `attention` command and `serve`'s both render. A workspace with no
    run, or one whose engine reading raises, is skipped rather than failing the roll-up — a person
    asking "what needs me" is owed the list of the ones that do, not an error about the ones that do
    not.
    """
    rows: list[dict[str, Any]] = []
    for workspace in _workspaces_for(root, workspaces):
        try:
            # The same reading `activity` makes, at a one-line trace tail and a one-entry timeline:
            # this module renders neither, and a roll-up over every project must not parse every
            # project's whole history to answer a question about a handful of them.
            standing = build_activity(workspace, trace_tail=1, limit=1)
        except Exception:  # noqa: BLE001 - one unreadable workspace must not blank the whole list
            continue
        if not _is_waiting(standing):
            continue
        rows.append(attention_row(workspace, standing, portfolio=portfolio))
    rows.sort(key=_order)
    return {
        "attention_version": ATTENTION_VERSION,
        "root": str(_projects_root(root)),
        "count": len(rows),
        "workspaces": rows,
    }
