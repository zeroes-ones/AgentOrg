#!/usr/bin/env python3
"""Phase 16 tests — attaching an existing project folder.

Before this, a workspace was *managed*: `projects/<slug>/`, created and owned by the engine, with the
agents' file tools rooted inside it. That is the right shape for a demo graph and the wrong shape for
the thing people actually want, which is to point an agent at the repository they already have.

These tests are about the difference between the two, and about the one thing that makes the wider
blast radius tolerable:

1. **The folder is the workspace.** The real path is used, never re-derived from a normalised slug, and
   attaching never creates anything the user did not ask for (`docs/`, `src/`).
2. **Containment still holds.** Pointing tools at a real repository puts the engine's own state — its
   checkpoint, effect journal and trace — inside the tree they can reach, and repository history under
   `.git/`. Both are refused, because an agent that can rewrite its own checkpoint can fabricate a
   resume, and one that can write a ref cannot be undone by resuming a run.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.artifacts import ArtifactStore
from engine.state import ENGINE_STATE_DIRNAME, StateError, Workspace
from engine.tools import ToolRegistry


# ── attach ───────────────────────────────────────────────────────────────────


def _folder(tmp_path, name="My_App"):
    """A stand-in for a real repository: an existing folder with a file in it."""
    target = tmp_path / name
    (target / "src").mkdir(parents=True)
    (target / "src" / "main.go").write_text("package main\n")
    return target


def test_attach_uses_the_real_path(tmp_path):
    """The path is the folder itself, not a managed `<root>/<slug>` beside it."""
    target = _folder(tmp_path)
    ws = Workspace.attach(target)
    assert ws.is_attached
    assert ws.path == target.resolve()
    assert ws.state_dir == target.resolve() / ENGINE_STATE_DIRNAME


def test_attach_normalises_the_slug_but_keeps_the_display_name(tmp_path):
    """`My_App` is a valid folder and an invalid slug; both must survive.

    The name a person recognises is shown, while the identifier stays inside the slug grammar — and
    crucially the *path* is the real folder, so normalisation never reaches the filesystem.
    """
    ws = Workspace.attach(_folder(tmp_path, "My_App"))
    assert ws.slug == "my_app"
    assert ws.display_name == "My_App"
    assert ws.path.name == "My_App"


def test_attach_refuses_a_missing_folder(tmp_path):
    """A typo must not silently create a project somewhere unintended."""
    with pytest.raises(StateError, match="does not exist"):
        Workspace.attach(tmp_path / "not-here")


def test_attach_refuses_a_file(tmp_path):
    a_file = tmp_path / "README.md"
    a_file.write_text("hi\n")
    with pytest.raises(StateError, match="not a directory"):
        Workspace.attach(a_file)


def test_attach_does_not_create_docs_or_src(tmp_path):
    """`ensure()` on an attached folder touches only the engine's own state.

    Creating an empty `src/` inside a Go module would be a visible, wrong mutation of the user's tree —
    performed by a command that may only have meant to inspect it.
    """
    target = tmp_path / "svc"
    target.mkdir()
    ws = Workspace.attach(target)
    ws.ensure()
    assert ws.state_dir.is_dir()
    assert not (target / "docs").exists()
    assert not (target / "src").exists()


def test_attach_is_idempotent(tmp_path):
    """Re-opening a project is not an error, matching managed workspaces."""
    target = _folder(tmp_path)
    ws = Workspace.attach(target)
    ws.ensure()
    ws.ensure()
    ws.save_checkpoint({"node": "fixer", "iteration": 1})
    assert ws.load_checkpoint() is not None


def test_managed_workspace_is_unchanged(tmp_path):
    """The managed path must behave exactly as before — attachment is additive."""
    ws = Workspace.for_project("demo", root=tmp_path / "projects")
    assert not ws.is_attached
    ws.ensure()
    for directory in (ws.state_dir, ws.docs_dir, ws.src_dir):
        assert directory.is_dir()
    assert ws.display_name == "demo"


# ── containment under attachment ─────────────────────────────────────────────


def _registry(ws):
    store = ArtifactStore(workspace_root=ws.path)
    return store, ToolRegistry(workspace_root=ws.path, writer=store)


def test_agents_can_edit_the_real_tree(tmp_path):
    """The point of attaching: a write lands in the user's file, not a managed copy."""
    ws = Workspace.attach(_folder(tmp_path))
    ws.ensure()
    _, registry = _registry(ws)
    result = registry.call("write_file", {"path": "src/main.go", "content": "package main\n\nvar x = 1\n"})
    assert result.ok
    assert "var x = 1" in (ws.path / "src" / "main.go").read_text()


def test_agents_cannot_read_engine_state(tmp_path):
    """The checkpoint and effect journal are not project content.

    An agent that can read its own checkpoint can reason about state it should not see; one that can
    write it can fabricate a resume.
    """
    ws = Workspace.attach(_folder(tmp_path))
    ws.ensure()
    ws.save_checkpoint({"node": "fixer"})
    _, registry = _registry(ws)
    read = registry.call("read_file", {"path": f"{ENGINE_STATE_DIRNAME}/run_state.json"})
    assert not read.ok
    assert ENGINE_STATE_DIRNAME in read.text

    write = registry.call("write_file", {"path": f"{ENGINE_STATE_DIRNAME}/run_state.json",
                                         "content": "{}"})
    assert not write.ok
    assert (ws.state_dir / "run_state.json").read_text() != "{}"


def test_agents_cannot_write_into_dot_git(tmp_path):
    """History is the one part of a repository that cannot be re-derived by resuming a run."""
    ws = Workspace.attach(_folder(tmp_path))
    ws.ensure()
    (ws.path / ".git").mkdir()
    (ws.path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    _, registry = _registry(ws)
    result = registry.call("write_file", {"path": ".git/HEAD", "content": "ref: refs/heads/evil\n"})
    assert not result.ok
    assert (ws.path / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"


def test_listing_hides_engine_state(tmp_path):
    """The inspector and diagnostics bundle must not present run state as the user's files."""
    ws = Workspace.attach(_folder(tmp_path))
    ws.ensure()
    ws.save_checkpoint({"node": "fixer"})
    store = ArtifactStore(workspace_root=ws.path)
    listed = store.list_files()
    assert "src/main.go" in listed
    assert not any(ENGINE_STATE_DIRNAME in path for path in listed)


# ── the CLI resolver ─────────────────────────────────────────────────────────


def test_cli_resolver_prefers_project_over_root(tmp_path):
    """Naming a folder is more specific than naming a directory of folders."""
    from engine.cli import _resolve_workspace, _slug_for

    target = _folder(tmp_path, "api")
    args = type("Args", (), {"project": str(target), "root": str(tmp_path / "projects"),
                             "slug": None})()
    ws = _resolve_workspace(args, "ignored")
    assert ws.is_attached
    assert ws.path == target.resolve()
    assert _slug_for(args) == "api"


def test_cli_resolver_without_project_is_managed(tmp_path):
    from engine.cli import _resolve_workspace

    args = type("Args", (), {"project": None, "root": str(tmp_path / "projects"), "slug": "demo"})()
    ws = _resolve_workspace(args, "demo")
    assert not ws.is_attached
    assert ws.path == (tmp_path / "projects" / "demo")


def test_cli_slug_requires_one_of_slug_or_project():
    """Neither identifier given is a usage error, said plainly rather than guessed at."""
    from engine.cli import _slug_for

    args = type("Args", (), {"project": None, "slug": None})()
    with pytest.raises(SystemExit):
        _slug_for(args)
