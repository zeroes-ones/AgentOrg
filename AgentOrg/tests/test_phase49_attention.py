#!/usr/bin/env python3
"""Phase 49 tests — "what needs me?", answered across every workspace, not one.

Every other reading in the engine is scoped to one workspace, and `serve` is bound to one — so a run
parked at a gate in a project nobody had registered was invisible to the app and unreachable from the
CLI. The user's report is exact: *"Not sure why PM is still blocked Priya, not good UI on how to
cleanup and take actions"* and *"I still don't understand what actions I need to take"*.

So these tests assert the three things that report has to be:

- **it enumerates** the workspaces under a root, with no slug required of the caller;
- **it is one assembly** — the CLI's `attention` and `serve`'s answer the same document;
- **its action is honest** — an "adopt this workspace as an org" step the engine's own `portfolio_add`
  route actually accepts, on a workspace that is not already an org.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.attention import build_attention
from engine.config import load
from engine.library import resolve
from engine.portfolio import Portfolio, workspace_for
from engine.serve import Server
from engine.state import Workspace


def _write(state_dir: pathlib.Path, name: str, data: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / name).write_text(json.dumps(data), encoding="utf-8")


def _gate_workspace(root: pathlib.Path, slug: str) -> Workspace:
    """A run parked at a human gate — the reported state."""
    ws = Workspace.for_project(slug, root=root)
    _write(ws.state_dir, "run_state.json", {
        "run_id": f"run_{slug}", "slug": slug, "phase": "awaiting_gate",
        "stop_reason": "pm: refused at the edge",
        "gate": {"gate_id": "pm", "kind": "human", "reason": "a completion contract was violated",
                 "requires": [], "present": []},
        "outcome": {"nodes": {"pm": {"status": "needs_review", "verdict": "contract-violation",
                                     "summary": "declared criteria not covered"}}},
    })
    return ws


def _plan_workspace(root: pathlib.Path, slug: str) -> Workspace:
    """A run parked awaiting approval of the graph — the second reported state."""
    ws = Workspace.for_project(slug, root=root)
    _write(ws.state_dir, "run_state.json", {
        "run_id": f"run_{slug}", "slug": slug, "phase": "awaiting_approval",
        "goal": "add a health endpoint", "manifest_path": str(ws.path / f"{slug}.yaml"),
        "plan": {"manifest": {"nodes": [{"id": "pm"}, {"id": "api"}]}, "validation": {"valid": True}},
        "outcome": {"nodes": {}},
    })
    return ws


def _quiet_workspace(root: pathlib.Path, slug: str) -> Workspace:
    """A project with nothing running and no goal: a normal answer, not a row."""
    ws = Workspace.for_project(slug, root=root)
    ws.ensure()
    return ws


# ── the enumeration ──────────────────────────────────────────────────────────


def test_only_the_workspaces_that_need_a_person_are_listed(tmp_path):
    """The defect in one assertion: the quiet project must not appear, the parked ones must."""
    root = tmp_path / "projects"
    _gate_workspace(root, "parked-at-a-gate")
    _plan_workspace(root, "parked-awaiting-approval")
    _quiet_workspace(root, "quiet")

    report = build_attention(root)
    assert report["attention_version"]
    assert report["root"] == str(root)
    assert report["count"] == 2
    assert {row["slug"] for row in report["workspaces"]} == {
        "parked-at-a-gate", "parked-awaiting-approval"}


def test_every_row_carries_what_it_waits_for_and_the_command_that_resolves_it(tmp_path):
    """A row that names a state but no step is the "I don't understand what to do" complaint."""
    root = tmp_path / "projects"
    _gate_workspace(root, "parked-at-a-gate")
    _plan_workspace(root, "parked-awaiting-approval")

    rows = {row["slug"]: row for row in build_attention(root)["workspaces"]}
    gate = rows["parked-at-a-gate"]
    assert gate["phase"] == "awaiting_gate"
    assert gate["waiting_for"] == "Decide gate 'pm'"
    assert gate["next_action"]["kind"] == "decide"
    assert "decide --slug parked-at-a-gate" in gate["next_action"]["command"]
    assert gate["headline"].startswith("Waiting on you")

    plan = rows["parked-awaiting-approval"]
    assert plan["phase"] == "awaiting_approval"
    assert plan["waiting_for"] == "Approve the parked plan and run it"
    assert plan["next_action"]["kind"] == "approve_plan"
    # The command the CLI's own `run --approve-plan` route takes the same arguments for.
    assert plan["next_action"]["command"] == \
        "engine.cli run --approve-plan --slug parked-awaiting-approval"


def test_a_decision_is_listed_before_work_that_is_merely_unstarted(tmp_path):
    """A reading order, not a second judgement: the blocking decision comes first."""
    root = tmp_path / "projects"
    _write(Workspace.for_project("idle-with-a-goal", root=root).state_dir, "goal.json", {
        "goal_version": "1.0.0", "objective": "keep going", "state": "armed"})
    _gate_workspace(root, "zzz-parked-at-a-gate")

    rows = build_attention(root)["workspaces"]
    assert [row["slug"] for row in rows][0] == "zzz-parked-at-a-gate"


def test_a_workspace_that_cannot_be_read_is_skipped_not_fatal(tmp_path):
    """A half-written checkpoint must cost that one row, not the whole answer."""
    root = tmp_path / "projects"
    _gate_workspace(root, "readable")
    broken = root / "not-a-slug"
    (broken / ".agent_state").mkdir(parents=True)
    (broken / ".agent_state" / "run_state.json").write_text("{not json", encoding="utf-8")

    report = build_attention(root)
    assert [row["slug"] for row in report["workspaces"]] == ["readable"]


def test_nothing_waiting_is_a_calm_answer(tmp_path):
    root = tmp_path / "projects"
    _quiet_workspace(root, "quiet")
    report = build_attention(root)
    assert report["count"] == 0 and report["workspaces"] == []


# ── one assembly, two surfaces ───────────────────────────────────────────────


def test_serve_answers_the_same_document_the_assembly_produced(tmp_path):
    """The rule `syscap.console_payload` states: the assembly lives with the descriptions."""
    root = tmp_path / "projects"
    _gate_workspace(root, "parked-at-a-gate")
    workspace = Workspace.attach(root / "parked-at-a-gate")
    server = Server(config=load(), library=resolve(), workspace=workspace,
                    slug=workspace.slug, stdin=io.StringIO(""), stdout=io.StringIO())

    served = server._cmd_attention({})
    assert served == build_attention(server.workspace.root)


# ── the action is one the engine's own route accepts ─────────────────────────


def test_an_unregistered_workspace_carries_an_adopt_step_portfolio_add_accepts(tmp_path):
    """**The action must work end to end.** `serve` is bound to one workspace, so the only write that
    can act on another one is `portfolio_add` — and the payload for it is the engine's own, not a
    sentence written on a surface. So the payload is fed to the route it names and has to be accepted,
    and the org it creates has to resolve to the very folder the row described.
    """
    root = tmp_path / "projects"
    _plan_workspace(root, "parked-awaiting-approval")
    row = build_attention(root)["workspaces"][0]

    assert row["org"]["registered"] is False
    adopt = row["org"]["adopt"]
    assert adopt["name"] == "parked-awaiting-approval"
    assert adopt["slug"] == "parked-awaiting-approval"
    # The resolved folder (`Workspace.attach` follows symlinks — `/tmp` is one on macOS), so the test
    # compares against the path the row itself names rather than a string it re-derived.
    assert pathlib.Path(adopt["path"]).resolve() == pathlib.Path(row["path"]).resolve()
    assert pathlib.Path(adopt["path"]).name == "parked-awaiting-approval"
    assert 'engine.cli portfolio add "parked-awaiting-approval"' in row["org"]["adopt_command"]

    portfolio = Portfolio.new()
    entry = portfolio.add_org(**adopt)
    assert workspace_for(entry, root=root).path.resolve() == pathlib.Path(adopt["path"]).resolve()
    # And the row now reads as an org, so a surface stops offering to add what is already there.
    assert build_attention(root, portfolio=portfolio)["workspaces"][0]["org"]["registered"] is True


def test_a_managed_org_is_matched_by_slug_an_external_one_by_its_folder(tmp_path):
    """Matching by folder first, then by slug *only* for a path-less entry.

    A path-less org *is* a managed project keyed on its slug, so the slug is its whole address. An org
    that names a folder elsewhere must never be matched by a slug it happens to share with a project
    under this root — that would report a different folder as already registered, and the row would
    offer no way to adopt the one in front of you.
    """
    root = tmp_path / "projects"
    _gate_workspace(root, "shared-name")

    managed = Portfolio.new()
    managed.add_org(name="Elsewhere", slug="shared-name")
    row = build_attention(root, portfolio=managed)["workspaces"][0]
    assert row["org"]["registered"] is True, "a path-less org is projects/<slug>"

    named_elsewhere = Portfolio.new()
    named_elsewhere.add_org(name="Elsewhere", slug="shared-name", path=tmp_path / "somewhere-else")
    row = build_attention(root, portfolio=named_elsewhere)["workspaces"][0]
    assert row["org"]["registered"] is False
    assert pathlib.Path(row["org"]["adopt"]["path"]).resolve() == \
        pathlib.Path(row["path"]).resolve()


def test_the_console_can_adopt_a_row_and_then_act_on_it(tmp_path, monkeypatch):
    """The app's two controls, driven through the engine's real routes, in order.

    "Adopt as an org" then "Switch to it and decide" is a *chain* of two writes, and if either were
    refused the console would be offering a sequence that cannot work — which is the thing rule four of
    this change forbids. So both are driven here against a real `Server`: the register is the user's own
    file, so the whole test runs against a throwaway global root (`AGENTORG_HOME`) and never the
    machine's real `~/.agentorg/portfolio.json`.
    """
    monkeypatch.setenv("AGENTORG_HOME", str(tmp_path / "home"))
    root = tmp_path / "projects"
    _gate_workspace(root, "parked")
    workspace = Workspace.attach(root / "parked")
    server = Server(config=load(), library=resolve(), workspace=workspace, slug="parked",
                    stdin=io.StringIO(""), stdout=io.StringIO())

    row = server._cmd_attention({})["workspaces"][0]
    created = server._cmd_portfolio_add(dict(row["org"]["adopt"]))["org"]
    assert created["slug"] == "parked"
    # Nothing was executed and nothing was decided on the way: the run is exactly as it was.
    assert server._cmd_activity({})["gate"]["gate_id"] == "pm"

    # The row now reads as an org, so the section stops offering to add what is already there.
    assert server._cmd_attention({})["workspaces"][0]["org"]["registered"] is True

    # And the switch re-points the server, which is what makes every control act on that workspace.
    detail = server._cmd_portfolio_select({"org": created["id"]})
    assert detail["repointed"] is True
    assert pathlib.Path(detail["workspace"]["path"]).resolve() == (root / "parked").resolve()
    # Proven by reading the run through the server that is now pointed at it.
    assert server._cmd_activity({})["next_action"]["kind"] == "decide"


# ── the CLI ──────────────────────────────────────────────────────────────────


def _run_cli(*args: str, home: pathlib.Path) -> subprocess.CompletedProcess:
    """The CLI as a subprocess, with its own global root so no real register is ever touched."""
    env = {**os.environ, "AGENTORG_HOME": str(home)}
    return subprocess.run([sys.executable, "-m", "engine.cli", *args],
                          capture_output=True, text=True, cwd=str(ROOT), env=env)


def test_the_cli_lists_what_needs_you_without_being_told_a_slug(tmp_path):
    root = tmp_path / "projects"
    _gate_workspace(root, "parked-at-a-gate")
    _plan_workspace(root, "parked-awaiting-approval")
    _quiet_workspace(root, "quiet")

    result = _run_cli("attention", "--root", str(root), home=tmp_path / "home")
    assert result.returncode == 0, result.stderr
    assert "2 workspace(s) need you" in result.stdout
    assert "Next: Decide gate 'pm'" in result.stdout
    assert "$ engine.cli decide --slug parked-at-a-gate" in result.stdout
    assert "Next: Approve the parked plan and run it" in result.stdout
    assert "$ engine.cli run --approve-plan --slug parked-awaiting-approval" in result.stdout
    assert "quiet" not in result.stdout, "a project that needs nothing is not a row"


def test_the_cli_json_is_the_document_the_app_is_given(tmp_path):
    root = tmp_path / "projects"
    _gate_workspace(root, "parked-at-a-gate")
    home = tmp_path / "home"
    result = _run_cli("--json", "attention", "--root", str(root), home=home)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == build_attention(root)


def test_the_cli_says_nothing_is_waiting_calmly(tmp_path):
    root = tmp_path / "projects"
    _quiet_workspace(root, "quiet")
    result = _run_cli("attention", "--root", str(root), home=tmp_path / "home")
    assert result.returncode == 0, result.stderr
    assert "0 workspace(s) need you" in result.stdout
    assert "nothing is waiting" in result.stdout


def test_the_cli_does_not_decide_anything(tmp_path):
    """A listing that answered a gate would be the engine deciding on the person's behalf."""
    root = tmp_path / "projects"
    workspace = _gate_workspace(root, "parked-at-a-gate")
    before = (workspace.state_dir / "run_state.json").read_text(encoding="utf-8")
    _run_cli("attention", "--root", str(root), home=tmp_path / "home")
    assert (workspace.state_dir / "run_state.json").read_text(encoding="utf-8") == before


def test_the_cli_accepts_the_approve_plan_route_it_names():
    """The command a row carries must be one the parser actually resolves."""
    from engine.cli import build_parser

    args = build_parser().parse_args(["run", "--approve-plan", "--slug", "x"])
    assert args.approve_plan is True and args.slug == "x"


@pytest.mark.parametrize("slug", ["parked-at-a-gate"])
def test_a_row_never_offers_an_action_the_engine_would_refuse(tmp_path, slug):
    """No `reassign`/`takeover`/`abort` — the verbs that answer `no run found` for a parked run."""
    root = tmp_path / "projects"
    _gate_workspace(root, slug)
    row = build_attention(root)["workspaces"][0]
    for refused in ("reassign", "takeover", "abort"):
        assert refused not in row["next_action"]["command"]
