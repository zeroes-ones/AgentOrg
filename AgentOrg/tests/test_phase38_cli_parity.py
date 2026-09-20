#!/usr/bin/env python3
"""Phase 38 — CLI/console parity: the operations the app could do and a terminal could not.

WHY THIS EXISTS
---------------
The console speaks 42 protocol commands; the CLI had no equivalent for fifteen of them. That is
backwards, because the CLI is supposed to be the *complete* surface and the app a convenience over it
— so a person at a terminal could not stop a run, could not move a node to another agent, could not
edit the roster or a provider, and could not reach the self-improvement loop at all.

What these tests pin is not that the commands *exist* but that they agree with the console:

- **The same effect.** Each new command is driven twice — once through `engine.cli`, once through
  `serve.Server` — and the resulting state is compared. Two paths that both return 0 while writing
  different things are the failure this whole task exists to prevent, and an exit code cannot see it.
- **`--json` is only JSON on stdout.** Diagnostics go to stderr. This is load-bearing for `| jq` and it
  is easy to break with one stray `print`.
- **Exit codes mean what they say**: 0 success, 1 a check failed, 2 a usage error — including for the
  commands that legitimately refuse (an unknown agent, a provider the console would reject).
- **A refusal names the next move.** "cannot do that" is not a diagnosis; `_slug_for`'s "give one of
  --slug/--project/--org" is the standard this file holds every new refusal to.
- **Nothing about the improver implies it applies changes.** The loop is propose-only by design, and a
  help text or a payload that suggested otherwise would be a lie about the safety boundary.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.cli import EXIT_CHECK_FAILED, EXIT_OK, EXIT_USAGE, build_parser, main
from engine.config import load
from engine.library import resolve
from engine.serve import Server
from engine.state import Workspace


# ── driving both surfaces ────────────────────────────────────────────────────


def run_cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the CLI as a subprocess, so the real entry point is exercised."""
    merged = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, "-m", "engine.cli", *args],
                          capture_output=True, text=True, cwd=str(ROOT), env=merged)


def cli_json(*args: str, env: dict | None = None) -> dict:
    """Run with `--json` and parse stdout. stderr is ignored on purpose — warnings live there."""
    result = run_cli("--json", *args, env=env)
    assert result.returncode == EXIT_OK, f"exit {result.returncode}: {result.stderr[:400]}"
    return json.loads(result.stdout)


class CapturedOut:
    """A stdout stand-in that records whole lines, for driving the console in-process."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    def flush(self) -> None:
        return None

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.lines if line.strip()]


_CREDS = {
    "version": "1.0.0",
    "providers": {"ollama": {"kind": "ollama", "base_url": "http://127.0.0.1:9",
                             "timeout_s": 1, "max_retries": 0}},
    "models": {"known": {"qwen2.5-coder:7b": {"context_window": 32768}}},
    "defaults": {"provider": "ollama", "model": "qwen2.5-coder:7b"},
}


def creds(tmp_path: pathlib.Path, mutate=None) -> pathlib.Path:
    """A hermetic credentials file, so these tests never read the developer's own."""
    document = json.loads(json.dumps(_CREDS))
    if mutate:
        mutate(document)
    path = tmp_path / "creds.json"
    path.write_text(json.dumps(document))
    os.chmod(path, 0o600)
    return path


_RUN_MANIFEST = """name: clirun
version: "1.0.0"
description: CLI run probe
payloads:
  handoff-v1:
    - status
    - summary
start: dev
nodes:
  - id: dev
    skill: backend-developer
    outputs: [change]
gates:
  - id: release
    type: gate
    kind: human
    requires: [change]
    description: Owner release approval
edges:
  - from: dev
    to: release
    when: dev.status == done
    payload: handoff-v1
end: [release]
"""

_RUN_STUB = '''CRITERIA = [
    "Every accepted finding addressed with a concrete diff",
    "No finding silently dropped",
    "Open items declared in open questions rather than hidden",
]


def execute_node(node_id, state, ctx):
    if node_id == "release":
        return {"status": "needs_review", "verdict": "awaiting_owner",
                "summary": "gate reached", "evidence": ["g"]}
    return {"status": "done", "verdict": "ok", "summary": "implemented",
            "evidence": ["src/app.py#abc"], "criteria_met": list(CRITERIA),
            "artifacts": [{"name": "change", "path": "src/app.py", "sha": "abc",
                           "type": "change"}]}
'''


@pytest.fixture
def run_project(tmp_path):
    """A project with one-manifest graph that reaches a human gate, plus a stub executor."""
    root = tmp_path / "projects"
    project = root / "clirun"
    project.mkdir(parents=True)
    (project / "clirun.yaml").write_text(_RUN_MANIFEST)
    (project / "stub.py").write_text(_RUN_STUB)
    return root, project, creds(tmp_path)


def park_at_the_gate(root, project, creds_path) -> None:
    """Drive a run until it waits at its human gate, at which point a run exists to act on."""
    result = run_cli("--config", str(creds_path), "run", "--manifest",
                     str(project / "clirun.yaml"), "--slug", "clirun", "--root", str(root),
                     "--executor", str(project / "stub.py"))
    assert result.returncode == EXIT_OK, result.stderr[:400]


def server_for(creds_path, slug="clirun", root=None, config=None) -> Server:
    """A console pointed at the same workspace the CLI uses, for the parity comparisons."""
    workspace = Workspace.for_project(slug, root=root)
    workspace.ensure()
    return Server(config=config or load(str(creds_path), warn=False), library=resolve(),
                  workspace=workspace, slug=slug, stdin=io.StringIO(""), stdout=CapturedOut())


def argparse_args(**kwargs) -> argparse.Namespace:
    """A Namespace with every command's global flag present, as `main()` normalises them to."""
    base = {"json": False, "config": None, "library": None, "project": None, "org": None}
    base.update(kwargs)
    return argparse.Namespace(**base)


# ── the parser surface ───────────────────────────────────────────────────────


def test_every_new_operation_resolves_to_a_command():
    parser = build_parser()
    for argv in (["abort"], ["reassign", "dev", "--agent", "Alice"], ["takeover", "dev"],
                 ["subagents", "list"], ["subagents", "result", "sub_1"],
                 ["agent", "update", "Alice", "--title", "x"], ["agent", "retire", "Alice"],
                 ["providers", "list"], ["providers", "add", "groq", "--base-url", "https://x/v1"],
                 ["providers", "test", "groq", "--base-url", "https://x/v1"],
                 ["providers", "remove", "groq"], ["improve"], ["proposals"]):
        assert parser.parse_args(argv).func is not None, f"{argv} does not resolve to a command"


def test_the_already_covered_operations_were_not_duplicated():
    """`decide`, `defaults set` and `defaults autonomy` already had homes — adding a second noun for
    one of them is worse than the gap it closes, so this pins that no such noun appeared."""
    names = set(build_parser()._subparsers._group_actions[0].choices)  # noqa: SLF001
    for duplicate in ("approve", "reject", "defaults_set", "autonomy_set"):
        assert duplicate not in names, f"{duplicate} would be a second way to do one thing"
    # And the existing homes still work, unchanged: `approve`/`reject` are `decide`'s flags.
    parser = build_parser()
    assert parser.parse_args(["decide", "--approve"]).func is not None
    assert parser.parse_args(["decide", "--reject"]).func is not None
    assert parser.parse_args(["defaults", "set", "--model", "m"]).func is not None
    assert parser.parse_args(["defaults", "autonomy", "--no-auto-gates"]).func is not None


def test_every_new_command_is_documented_in_usage_md():
    """A command nobody can discover is only half built. USAGE.md's table is the surface a person
    reads, so every new one appears there — and the doc still describes commands that run."""
    text = (ROOT / "USAGE.md").read_text(encoding="utf-8")
    for documented in ("`abort --slug s`", "`reassign --slug s <node> --agent A`",
                       "`takeover --slug s <node>`", "`subagents list --slug s`",
                       "`subagents result <child> --slug s`", "`agent update <name>",
                       "`agent retire <name>`", "`providers list`", "`providers add <id>",
                       "`providers test <id>", "`providers remove <id>`", "`improve`", "`proposals`"):
        assert documented in text, f"{documented} is not in USAGE.md"


def test_abort_is_told_apart_from_decide_in_the_help():
    """`decide` resolves a gate and the run carries on spending; `abort` ends it. Under pressure a
    person reaches for the wrong one, so the difference has to be in the help text."""
    text = build_parser().format_help()
    line = _help_line(text, "abort")
    assert "STOP" in line
    assert "decide" in line, "it must name the command it is not"


def test_the_improver_commands_say_nothing_is_applied():
    """The loop is propose-only by construction. Help text that implied otherwise would be a lie
    about the one boundary the design refuses to move."""
    text = build_parser().format_help()
    for prog in ("improve", "proposals", "propose"):
        line = _help_line(text, prog)
        lowered = line.lower()
        assert "never applies" in lowered or ("nothing" in lowered and "applied" in lowered), line


def _help_line(help_text: str, prog: str) -> str:
    """One command's help line, wrapped lines included, so a per-command assertion cannot target
    the wrong command."""
    lines = help_text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(prog))
    collected = [lines[start].strip()]
    indent = len(lines[start]) - len(lines[start].lstrip())
    for line in lines[start + 1:]:
        if not line.strip():
            break
        if (len(line) - len(line.lstrip())) <= indent:
            break
        collected.append(line.strip())
    return " ".join(collected)


# ── abort ────────────────────────────────────────────────────────────────────


def test_abort_ends_the_run_and_keeps_the_checkpoint(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)

    result = run_cli("--config", str(creds_path), "abort", "--slug", "clirun", "--root", str(root))
    assert result.returncode == EXIT_OK, result.stderr[:400]
    assert "aborted" in result.stdout

    status = cli_json("--config", str(creds_path), "status", "--slug", "clirun", "--root", str(root))
    assert status["phase"] == "aborted"
    # The checkpoint is what "keeping its checkpoint" means, and it is the difference from a kill.
    assert (root / "clirun" / ".agent_state" / "run_state.json").is_file()


def test_abort_and_the_console_leave_the_same_state(run_project):
    """The whole point: the CLI and the app must agree about what aborting *does*."""
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    cli_result = cli_json("--config", str(creds_path), "abort", "--slug", "clirun",
                          "--root", str(root))

    # Reset to the same starting point and abort through the console instead.
    park_at_the_gate(root, project, creds_path)
    server = server_for(creds_path, root=root)
    server.orchestrator = server._orchestrator("clirun")  # noqa: SLF001 - the app's own bootstrap
    server.orchestrator.load("clirun")
    detail = server._cmd_abort({})  # noqa: SLF001

    assert cli_result["aborted"] is True
    assert detail == {"aborted": True}
    for path in (root / "clirun" / ".state.json", root / "clirun" / ".agent_state" / "run_state.json"):
        if path.is_file():
            assert json.loads(path.read_text())["phase"] == "aborted"


def test_abort_refuses_a_run_that_has_already_ended(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    run_cli("--config", str(creds_path), "abort", "--slug", "clirun", "--root", str(root))

    result = run_cli("--config", str(creds_path), "abort", "--slug", "clirun", "--root", str(root))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "already" in result.stderr


def test_abort_refuses_when_there_is_no_run_and_names_the_next_move(run_project):
    root, _project, creds_path = run_project
    result = run_cli("--config", str(creds_path), "abort", "--slug", "never-ran",
                     "--root", str(root))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "no run found" in result.stderr
    assert "engine.cli status" in result.stderr, "a refusal must say what to do next"


def test_abort_json_is_only_json(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    result = run_cli("--json", "--config", str(creds_path), "abort", "--slug", "clirun",
                     "--root", str(root))
    assert result.returncode == EXIT_OK
    payload = json.loads(result.stdout)          # must not raise: no human line on stdout
    assert payload["aborted"] is True
    assert payload["previous_phase"] == "awaiting_gate"
    assert "aborted run_" not in result.stdout


def test_abort_without_a_workspace_is_a_usage_error():
    """`_slug_for`'s own refusal, reached through the new command: an unresolvable target is usage."""
    result = run_cli("abort")
    assert result.returncode == EXIT_USAGE
    assert "give one of --slug" in result.stderr


# ── reassign and takeover ────────────────────────────────────────────────────


def test_reassign_pins_the_node_and_matches_the_console(run_project):
    """Both surfaces must produce the same *binding*, not merely both exit 0.

    Each agent id differs between two loads of the same built-in roster, so the console pins a
    *different* agent and the assertion is that both surfaces write the same *shape* of binding: a
    pinned policy on the node, naming the one agent asked for.
    """
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)

    result = cli_json("--config", str(creds_path), "reassign", "dev", "--agent", "Alice",
                      "--slug", "clirun", "--root", str(root))
    cli_binding = result["binding"]

    from engine.catalog import ModelCatalog
    from engine.people import People
    from engine.providers.registry import build_providers

    config = load(str(creds_path), warn=False)
    providers, _ = build_providers(config)
    org = People(library=resolve(), config=config,
                 catalog=ModelCatalog(config, providers)).load()
    alice = next(a for a in org.agents.values() if a.name == "Alice")
    server = server_for(creds_path, root=root, config=config)
    server.orchestrator = server._orchestrator("clirun")  # noqa: SLF001
    server.orchestrator.org = org
    server.orchestrator.load("clirun")
    detail = server._cmd_reassign({"node": "dev", "agent_id": alice.id})  # noqa: SLF001

    assert detail == {"node": "dev", "agent_id": alice.id}
    assert cli_binding["policy"] == "pinned"
    assert cli_binding["pinned_id"] == result["agent_id"]
    # The console's write is on disk in the same place, as the same pinned binding.
    console_binding = server.orchestrator._run.bindings["dev"]  # noqa: SLF001
    assert console_binding["policy"] == "pinned"
    assert console_binding["pinned_id"] == alice.id
    on_disk = json.loads((root / "clirun" / ".agent_state" / "run_state.json").read_text())
    assert on_disk["bindings"]["dev"]["policy"] == "pinned"


def test_reassign_refuses_an_agent_without_the_skill(run_project):
    """The router's invariant is not bypassable by the Owner, and the refusal says which one broke."""
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    result = run_cli("--config", str(creds_path), "reassign", "dev", "--agent", "Sana",
                     "--slug", "clirun", "--root", str(root))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "backend-developer" in result.stderr


def test_reassign_refuses_an_unknown_agent_with_the_names_that_exist(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    result = run_cli("--config", str(creds_path), "reassign", "dev", "--agent", "Nobody",
                     "--slug", "clirun", "--root", str(root))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "engine.cli agents" in result.stderr


def test_reassign_refuses_a_node_not_in_the_run(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    result = run_cli("--config", str(creds_path), "reassign", "nosuch", "--agent", "Alice",
                     "--slug", "clirun", "--root", str(root))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "not in this run's plan" in result.stderr


def test_reassign_json_is_only_json(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    result = run_cli("--json", "--config", str(creds_path), "reassign", "dev", "--agent", "Alice",
                     "--slug", "clirun", "--root", str(root))
    assert result.returncode == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["node"] == "dev"
    assert "pinned to" not in result.stdout


def test_takeover_records_the_owner_as_the_actor(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    result = cli_json("--config", str(creds_path), "takeover", "dev", "--slug", "clirun",
                      "--root", str(root))
    assert result["node"] == "dev"
    assert result["by"] == "ag_owner"
    assert result["decision"]["action"] == "takeover"

    # The console records the same decision, so the audit trail is one story rather than two.
    server = server_for(creds_path, root=root)
    server.orchestrator = server._orchestrator("clirun")  # noqa: SLF001
    server.orchestrator.load("clirun")
    assert server._cmd_takeover({"node": "dev"}) == {"node": "dev"}  # noqa: SLF001
    run = server.orchestrator.load("clirun")
    actions = [d["action"] for d in run.decisions]
    assert actions.count("takeover") == 2, actions


def test_takeover_refuses_a_node_the_run_does_not_have(run_project):
    """The orchestrator records a takeover without checking, so the check has to be here — otherwise
    it writes a line to a log that changes nothing."""
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    result = run_cli("--config", str(creds_path), "takeover", "nosuch", "--slug", "clirun",
                     "--root", str(root))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "not in this run's plan" in result.stderr


def test_takeover_json_is_only_json(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    result = run_cli("--json", "--config", str(creds_path), "takeover", "dev", "--slug", "clirun",
                     "--root", str(root))
    assert result.returncode == EXIT_OK
    assert json.loads(result.stdout)["by"] == "ag_owner"
    assert "taken over by" not in result.stdout


# ── subagents ────────────────────────────────────────────────────────────────


def _seed_child(root, run_id="run_seeded") -> tuple[pathlib.Path, str]:
    """A child on disk, so `list` and `result` have something real to read."""
    from engine.subagents import ChildStore

    workspace = Workspace.for_project("clirun", root=root)
    workspace.ensure()
    store = ChildStore(workspace, run_id=run_id)
    store.open(child_id="sub_1", agent_id="ag_1", skill="code-reviewer", task="review src/app.py")
    store.append(child_id="sub_1", kind="turn", text="found a missing null check\n")
    store.close(child_id="sub_1", status="done", summary="one finding: missing null check")
    return workspace, run_id


def _write_checkpoint_with_run_id(workspace, run_id: str) -> None:
    """Make the run id the store is scoped to match the run the commands will load."""
    raw = json.loads(workspace.checkpoint_path.read_text())
    raw["run_id"] = run_id
    workspace.checkpoint_path.write_text(json.dumps(raw))


def test_subagents_list_reads_the_run_and_matches_the_console(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    workspace, run_id = _seed_child(root)
    _write_checkpoint_with_run_id(workspace, run_id)

    result = cli_json("--config", str(creds_path), "subagents", "list", "--slug", "clirun",
                      "--root", str(root))
    assert result["count"] == 1
    assert result["children"][0]["child_id"] == "sub_1"

    server = server_for(creds_path, root=root)
    server.orchestrator = server._orchestrator("clirun")  # noqa: SLF001
    server.orchestrator.load("clirun")
    assert server._cmd_subagents({}) == result  # noqa: SLF001


def test_subagents_list_reports_an_empty_run_without_failing(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    result = cli_json("--config", str(creds_path), "subagents", "list", "--slug", "clirun",
                      "--root", str(root))
    assert result == {"children": [], "count": 0, "running": 0, "failed": 0}


def test_subagents_result_pages_a_transcript(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    workspace, run_id = _seed_child(root)
    _write_checkpoint_with_run_id(workspace, run_id)

    page = cli_json("--config", str(creds_path), "subagents", "result", "sub_1",
                    "--offset", "0", "--limit", "20", "--slug", "clirun", "--root", str(root))
    assert page["child_id"] == "sub_1"
    assert page["offset_bytes"] == 0
    assert page["returned_bytes"] <= 20
    assert page["more"] is True
    assert page["next_offset_bytes"] == page["returned_bytes"]

    rest = cli_json("--config", str(creds_path), "subagents", "result", "sub_1",
                    "--offset", str(page["next_offset_bytes"]), "--slug", "clirun", "--root", str(root))
    assert rest["more"] is False
    assert "missing null check" in page["text"] + rest["text"]


def test_subagents_result_refuses_an_unknown_child(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    result = run_cli("--config", str(creds_path), "subagents", "result", "nosuch",
                     "--slug", "clirun", "--root", str(root))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "no transcript" in result.stderr
    assert "subagents list" in result.stderr, "a refusal must name the next move"


def test_subagents_json_is_only_json(run_project):
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    workspace, run_id = _seed_child(root)
    _write_checkpoint_with_run_id(workspace, run_id)
    result = run_cli("--json", "--config", str(creds_path), "subagents", "result", "sub_1",
                     "--slug", "clirun", "--root", str(root))
    assert result.returncode == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["child_id"] == "sub_1"
    assert "bytes 0.." not in result.stdout


# ── the roster: agent update and retire ──────────────────────────────────────


def _attached(tmp_path) -> pathlib.Path:
    """An attached project folder, so a hire lands where the loader looks for it."""
    project = tmp_path / "Attached"
    (project / ".git").mkdir(parents=True)
    return project


def test_agent_update_changes_only_what_was_named(tmp_path):
    project = _attached(tmp_path)
    creds_path = creds(tmp_path)
    run_cli("--config", str(creds_path), "hire", "Reviewer2", "--skill", "code-reviewer",
            "--project", str(project))

    before = cli_json("--config", str(creds_path), "agents", "--project", str(project))
    hired = next(a for a in before["agents"] if a["name"] == "Reviewer2")
    result = cli_json("--config", str(creds_path), "agent", "update", "Reviewer2",
                      "--title", "Staff Reviewer", "--project", str(project))
    after = result["agent"]

    assert result["changed"] == ["title"]
    assert after["id"] == hired["id"], "editing must keep the id its history is keyed on"
    assert after["title"] == "Staff Reviewer"
    assert after["model"] == hired["model"]
    assert after["skills"] == hired["skills"]


def test_agent_update_matches_the_console(tmp_path):
    """Both surfaces edit the same roster file, so the console must see the CLI's change."""
    project = _attached(tmp_path)
    creds_path = creds(tmp_path)
    run_cli("--config", str(creds_path), "hire", "Reviewer2", "--skill", "code-reviewer",
            "--project", str(project))
    cli_json("--config", str(creds_path), "agent", "update", "Reviewer2", "--concurrency", "3",
             "--project", str(project))

    from engine.catalog import ModelCatalog
    from engine.people import People
    from engine.providers.registry import build_providers

    config = load(str(creds_path), warn=False)
    providers, _ = build_providers(config)
    people = People(library=resolve(), config=config, catalog=ModelCatalog(config, providers),
                    project=project)
    org = people.load(project=project)
    spec = next(a for a in org.agents.values() if a.name == "Reviewer2")
    assert spec.max_concurrency == 3


def test_agent_update_refuses_nothing_to_change(tmp_path):
    project = _attached(tmp_path)
    creds_path = creds(tmp_path)
    run_cli("--config", str(creds_path), "hire", "Reviewer2", "--skill", "code-reviewer",
            "--project", str(project))
    result = run_cli("--config", str(creds_path), "agent", "update", "Reviewer2",
                     "--project", str(project))
    assert result.returncode == EXIT_USAGE
    assert "nothing to change" in result.stderr


def test_agent_update_refuses_an_agent_that_does_not_exist(tmp_path):
    project = _attached(tmp_path)
    creds_path = creds(tmp_path)
    result = run_cli("--config", str(creds_path), "agent", "update", "Nobody", "--title", "x",
                     "--project", str(project))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "engine.cli agents" in result.stderr


def test_agent_update_json_is_only_json(tmp_path):
    project = _attached(tmp_path)
    creds_path = creds(tmp_path)
    run_cli("--config", str(creds_path), "hire", "Reviewer2", "--skill", "code-reviewer",
            "--project", str(project))
    result = run_cli("--json", "--config", str(creds_path), "agent", "update", "Reviewer2",
                     "--title", "x", "--project", str(project))
    assert result.returncode == EXIT_OK
    assert json.loads(result.stdout)["agent"]["title"] == "x"
    assert "updated Reviewer2" not in result.stdout


def test_agent_retire_removes_it_and_the_console_agrees(tmp_path):
    project = _attached(tmp_path)
    creds_path = creds(tmp_path)
    run_cli("--config", str(creds_path), "hire", "Reviewer2", "--skill", "code-reviewer",
            "--project", str(project))
    result = cli_json("--config", str(creds_path), "agent", "retire", "Reviewer2",
                      "--reason", "trial over", "--project", str(project))
    assert result["retired"]["name"] == "Reviewer2"
    assert not any(a["name"] == "Reviewer2" for a in result["agents"])

    from engine.catalog import ModelCatalog
    from engine.people import People
    from engine.providers.registry import build_providers

    config = load(str(creds_path), warn=False)
    providers, _ = build_providers(config)
    org = People(library=resolve(), config=config,
                 catalog=ModelCatalog(config, providers)).load(project=project)
    assert not any(a.name == "Reviewer2" for a in org.agents.values())
    # The `--reason` the CLI advertises is recorded, not discarded: the roster keeps the tombstone.
    assert org.retired[-1]["reason"] == "trial over"
    assert org.retired[-1]["id"] == result["retired"]["id"]


def test_agent_retire_refuses_the_owner(tmp_path):
    """The org's own invariant: the Owner holds terminal authority, so it cannot be terminated."""
    project = _attached(tmp_path)
    creds_path = creds(tmp_path)
    result = run_cli("--config", str(creds_path), "agent", "retire", "Owner", "--project", str(project))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "Owner" in result.stderr


def test_agent_retire_refuses_an_agent_that_does_not_exist(tmp_path):
    project = _attached(tmp_path)
    creds_path = creds(tmp_path)
    result = run_cli("--config", str(creds_path), "agent", "retire", "Nobody", "--project", str(project))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "engine.cli agents" in result.stderr


# ── providers ────────────────────────────────────────────────────────────────


def test_providers_list_never_prints_a_key(tmp_path):
    """A secret that crosses a socket is a secret in a log, a screenshot and a crash report."""
    creds_path = creds(tmp_path, mutate=lambda d: d["providers"]["ollama"].update(
        {"api_key": "secret-value-that-must-not-appear"}))

    result = run_cli("--json", "--config", str(creds_path), "providers", "list")
    assert result.returncode == EXIT_OK, result.stderr[:400]
    payload = json.loads(result.stdout)
    assert "secret-value-that-must-not-appear" not in result.stdout
    entry = payload["providers"][0]
    assert "api_key" not in entry
    assert entry["has_key"] is True


def test_providers_add_writes_the_file_and_matches_the_console(tmp_path):
    creds_path = creds(tmp_path)
    result = cli_json("--config", str(creds_path), "providers", "add", "groq",
                      "--base-url", "https://api.groq.com/openai/v1", "--key-env", "GROQ_KEY")
    document = json.loads(creds_path.read_text())
    assert result["provider_id"] == "groq"
    assert "groq" in document["providers"]
    assert "ollama" in document["providers"], "a write must merge, not replace"
    assert document["models"]["known"]["qwen2.5-coder:7b"]["context_window"] == 32768

    # The console's own handler, on a second file holding the same starting document, writes the same
    # entry. That is the parity that matters: two surfaces, one definition of a valid provider.
    other = tmp_path / "other" / "credentials.json"
    other.parent.mkdir()
    other.write_text(json.dumps(_CREDS))
    os.chmod(str(other), 0o600)
    server = server_for(creds_path, slug="providers", config=load(str(other), warn=False))
    server.config.path = other
    server._cmd_provider_add({"provider_id": "groq", "kind": "openai",  # noqa: SLF001
                              "base_url": "https://api.groq.com/openai/v1",
                              "api_key_env": "GROQ_KEY"})
    console_document = json.loads(other.read_text())
    assert console_document["providers"]["groq"] == document["providers"]["groq"]
    assert "ollama" in console_document["providers"]


def test_providers_add_reduces_a_pasted_endpoint_and_says_so(tmp_path):
    """A full endpoint is what people are handed; the base is what a provider can append to."""
    creds_path = creds(tmp_path)
    result = cli_json("--config", str(creds_path), "providers", "add", "cloud",
                      "--base-url", "https://ollama.com/v1/chat/completions", "--key", "k" * 12)
    assert json.loads(creds_path.read_text())["providers"]["cloud"]["base_url"] == "https://ollama.com/v1"
    assert result["base_url"] == "https://ollama.com/v1"
    assert "chat/completions" in result["note"], "the correction must be told, not silent"


def test_providers_add_refuses_ollamas_cloud_under_the_local_kind(tmp_path):
    """The two are different protocols, and picking the wrong one looks like a bad key."""
    creds_path = creds(tmp_path)
    result = run_cli("--config", str(creds_path), "providers", "add", "cloud", "--kind", "ollama",
                     "--base-url", "https://ollama.com/v1")
    assert result.returncode == EXIT_CHECK_FAILED
    assert "OpenAI-compatible" in result.stderr


def test_providers_add_refuses_an_unknown_kind_as_a_usage_error(tmp_path):
    creds_path = creds(tmp_path)
    result = run_cli("--config", str(creds_path), "providers", "add", "x", "--kind", "banana",
                     "--base-url", "https://x/v1")
    assert result.returncode == EXIT_USAGE


def test_providers_add_json_is_only_json(tmp_path):
    creds_path = creds(tmp_path)
    result = run_cli("--json", "--config", str(creds_path), "providers", "add", "groq",
                     "--base-url", "https://api.groq.com/openai/v1", "--key-env", "GROQ_KEY")
    assert result.returncode == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["provider_id"] == "groq"
    assert "saved groq" not in result.stdout


def test_providers_test_reports_an_unreachable_endpoint_without_failing(tmp_path):
    """A failed test is the *answer* to "test this", not a refusal — the values may be right and the
    host down, and the person is the one who knows which."""
    creds_path = creds(tmp_path)
    result = cli_json("--config", str(creds_path), "providers", "test", "nope",
                      "--base-url", "http://127.0.0.1:9/v1", "--key", "x" * 20,
                      "--timeout-s", "1", "--max-retries", "0")
    assert result["ok"] is False
    assert result["reachable"] is False
    assert result["reason"]


def test_providers_test_never_needs_a_saved_provider(tmp_path):
    """Testing an unsaved entry is the whole point: it must work on the values in hand."""
    creds_path = creds(tmp_path)
    result = run_cli("--config", str(creds_path), "providers", "test", "brand-new",
                     "--base-url", "http://127.0.0.1:9/v1", "--key", "x" * 20,
                     "--timeout-s", "1", "--max-retries", "0")
    assert result.returncode == EXIT_OK
    assert "brand-new" in json.loads(
        run_cli("--json", "--config", str(creds_path), "providers", "test", "brand-new",
                "--base-url", "http://127.0.0.1:9/v1", "--key", "x" * 20,
                "--timeout-s", "1", "--max-retries", "0").stdout)["reason"]


def test_providers_remove_deletes_only_the_named_one(tmp_path):
    creds_path = creds(tmp_path)
    run_cli("--config", str(creds_path), "providers", "add", "groq",
            "--base-url", "https://api.groq.com/openai/v1", "--key-env", "GROQ_KEY")
    result = cli_json("--config", str(creds_path), "providers", "remove", "groq")
    document = json.loads(creds_path.read_text())
    assert result["removed"] == "groq"
    assert result["remaining"] == ["ollama"]
    assert "groq" not in document["providers"]
    assert "ollama" in document["providers"]


def test_providers_remove_refuses_the_last_provider(tmp_path):
    """The last provider cannot be removed, because no launch could read the document it would leave.

    `load` refuses a file with no providers, and the running server *hides* that — the reload failure
    is swallowed and the stale object keeps serving — so the breakage would surface only on the next
    start. The CLI reports the refusal as a failed check and writes nothing.
    """
    creds_path = creds(tmp_path)
    before = creds_path.read_text()
    result = run_cli("--config", str(creds_path), "providers", "remove", "ollama")
    assert result.returncode == EXIT_CHECK_FAILED
    assert "only provider" in result.stderr
    assert creds_path.read_text() == before, "a refused removal must not write anything"


def test_providers_remove_prunes_the_references_that_named_it(tmp_path):
    """A dangling default, concurrency limit or reviewer default is what made "remove a provider"
    brick the engine — all three are pruned in the same write."""
    creds_path = creds(tmp_path, mutate=lambda d: d.update(
        {"concurrency": {"per_provider_limits": {"ollama": 1, "groq": 3}},
         "defaults": {"provider": "ollama", "model": "qwen2.5-coder:7b",
                      "reviewer": {"provider": "groq", "model": "qwen2.5-coder:7b"}}}))
    run_cli("--config", str(creds_path), "providers", "add", "groq",
            "--base-url", "https://api.groq.com/openai/v1", "--key-env", "GROQ_KEY")
    run_cli("--config", str(creds_path), "providers", "remove", "groq")
    document = json.loads(creds_path.read_text())
    assert "groq" not in document.get("concurrency", {}).get("per_provider_limits", {})
    assert "provider" not in document["defaults"].get("reviewer", {})
    assert document["defaults"]["provider"] == "ollama", "a default that named another provider stays"


def test_providers_remove_json_is_only_json(tmp_path):
    creds_path = creds(tmp_path)
    run_cli("--config", str(creds_path), "providers", "add", "groq",
            "--base-url", "https://api.groq.com/openai/v1", "--key-env", "GROQ_KEY")
    result = run_cli("--json", "--config", str(creds_path), "providers", "remove", "groq")
    assert result.returncode == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload == {"removed": "groq", "remaining": ["ollama"]}
    assert "removed groq" not in result.stdout


# ── the improver: improve and proposals ──────────────────────────────────────


def test_proposals_lists_an_empty_directory_without_failing(tmp_path):
    creds_path = creds(tmp_path)
    result = cli_json("--config", str(creds_path), "proposals", "--slug", "improv",
                      "--root", str(tmp_path / "projects"))
    assert result == {"proposals": [], "count": 0, "refused": [], "refused_count": 0,
                      "directory": result["directory"], "applies_changes": False}
    assert "improv" in result["directory"]


def test_proposals_and_propose_list_read_the_same_directory(tmp_path):
    """Two commands, one answer: a second reader that could disagree is how a person learns to trust
    neither."""
    creds_path = creds(tmp_path)
    root = tmp_path / "projects"
    assert (cli_json("--config", str(creds_path), "proposals", "--slug", "improv", "--root", str(root))
            == cli_json("--config", str(creds_path), "propose", "--list", "--slug", "improv",
                        "--root", str(root)))


def test_improve_dry_run_reports_the_findings_it_measured(tmp_path):
    creds_path = creds(tmp_path)
    result = cli_json("--config", str(creds_path), "improve", "--dry-run", "--slug", "improv",
                      "--root", str(tmp_path / "projects"))
    assert result["count"] == len(result["findings"])
    assert result["applies_changes"] is False
    # Every finding carries the evidence that produced it; a finding without evidence does not exist.
    for finding in result["findings"]:
        assert "evidence" in finding and "kind" in finding


def test_improve_never_says_it_applied_anything(tmp_path):
    """The loop is propose-only by construction. Every payload says so, in the same words."""
    creds_path = creds(tmp_path)
    root = tmp_path / "projects"
    for argv in (["improve", "--dry-run", "--slug", "improv", "--root", str(root)],
                 ["improve", "--list", "--slug", "improv", "--root", str(root)]):
        assert cli_json("--config", str(creds_path), *argv)["applies_changes"] is False


def _seed_proposal(root: pathlib.Path, slug: str = "improv", proposal_id: str = "prop_1") -> None:
    """A promoted proposal on disk, so `proposals` has something real to list."""
    from engine.improver import PROPOSALS_DIRNAME, Finding, Proposal

    workspace = Workspace.for_project(slug, root=root)
    workspace.ensure()
    directory = workspace.state_dir / PROPOSALS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    proposal = Proposal(proposal_id=proposal_id,
                        finding=Finding(kind="stagnant_loop", path="engine/x.py", subject="loop",
                                        detail="never converged", evidence={"attempts": 3}))
    (directory / f"{proposal_id}-stagnant_loop.json").write_text(json.dumps(proposal.as_dict()))
    (directory / f"{proposal_id}-stagnant_loop.md").write_text("# Proposal\n")


def test_proposals_lists_a_promoted_proposal_and_points_at_the_file(tmp_path):
    """`--json` must expose what the console reads — including the `.md` a person actually opens."""
    creds_path = creds(tmp_path)
    root = tmp_path / "projects"
    _seed_proposal(root)

    payload = cli_json("--config", str(creds_path), "proposals", "--slug", "improv", "--root", str(root))
    assert payload["count"] == 1
    entry = payload["proposals"][0]
    assert entry["proposal_id"] == "prop_1"
    assert entry["finding"]["kind"] == "stagnant_loop"
    assert entry["file"].endswith(".md")

    console = _console(creds_path, root, "improv")._cmd_proposals({})  # noqa: SLF001
    assert console == payload, "the panel and the terminal must describe a proposal identically"


def _console(creds_path, root, slug: str) -> Server:
    """A console over a workspace, for the parity comparisons that need no orchestrator."""
    workspace = Workspace.for_project(slug, root=root)
    workspace.ensure()
    return Server(config=load(str(creds_path), warn=False), library=resolve(),
                  workspace=workspace, slug=slug, stdin=io.StringIO(""), stdout=CapturedOut())


def test_the_improver_surfaces_expose_the_same_fields(tmp_path):
    """`--json` must expose what the console reads, or the CLI and the panel describe it differently."""
    creds_path = creds(tmp_path)
    root = tmp_path / "projects"
    _seed_proposal(root)
    console = _console(creds_path, root, "improv")._cmd_proposals({})  # noqa: SLF001
    cli_payload = cli_json("--config", str(creds_path), "proposals", "--slug", "improv",
                           "--root", str(root))
    assert set(console) == set(cli_payload)
    assert set(console["proposals"][0]) == set(cli_payload["proposals"][0])
    assert console["applies_changes"] is False


def test_proposals_json_is_only_json(tmp_path):
    creds_path = creds(tmp_path)
    result = run_cli("--json", "--config", str(creds_path), "proposals", "--slug", "improv",
                     "--root", str(tmp_path / "projects"))
    assert result.returncode == EXIT_OK
    json.loads(result.stdout)                    # must not raise
    assert "proposal(s) in" not in result.stdout


# ── the exit-code contract, per new command ──────────────────────────────────


def test_success_is_zero_and_a_check_failure_is_one(run_project):
    """One table for the new commands: 0 for the operation, 1 for a refusal it explains."""
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)

    successes = [(["takeover", "dev", "--slug", "clirun", "--root", str(root)], EXIT_OK),
                 (["subagents", "list", "--slug", "clirun", "--root", str(root)], EXIT_OK),
                 (["providers", "list"], EXIT_OK),
                 (["proposals", "--slug", "improv", "--root", str(root)], EXIT_OK)]
    for argv, expected in successes:
        result = run_cli("--config", str(creds_path), *argv)
        assert result.returncode == expected, f"{argv}: {result.stderr[:300]}"

    failures = [(["takeover", "nosuch", "--slug", "clirun", "--root", str(root)], EXIT_CHECK_FAILED),
                (["subagents", "result", "nosuch", "--slug", "clirun", "--root", str(root)],
                 EXIT_CHECK_FAILED),
                (["abort", "--slug", "never-ran", "--root", str(root)], EXIT_CHECK_FAILED),
                (["reassign", "dev", "--agent", "Nobody", "--slug", "clirun", "--root", str(root)],
                 EXIT_CHECK_FAILED)]
    for argv, expected in failures:
        result = run_cli("--config", str(creds_path), *argv)
        assert result.returncode == expected, f"{argv}: {result.stdout[:300]}"
        assert result.stderr.strip(), "a refusal must explain itself"


def test_a_usage_error_is_two_for_every_new_command():
    """A missing required argument is usage, not a check that failed — a script branches on that."""
    for argv in (["reassign", "dev"], ["agent", "update"], ["agent", "retire"],
                 ["providers", "add"], ["providers", "test"], ["providers", "remove"]):
        result = run_cli(*argv)
        assert result.returncode == EXIT_USAGE, f"{argv}: {result.returncode}"


def test_main_returns_the_exit_code_rather_than_raising(monkeypatch, run_project):
    """Every command returns a code; a script has to be able to branch on it."""
    root, project, creds_path = run_project
    park_at_the_gate(root, project, creds_path)
    monkeypatch.setenv("AGENTORG_CREDENTIALS", str(creds_path))
    assert main(["proposals", "--slug", "improv", "--root", str(root)]) == EXIT_OK
    assert main(["abort", "--slug", "never-ran", "--root", str(root)]) == EXIT_CHECK_FAILED


# ── the shared resolver, rather than a hand-rolled one ───────────────────────


def test_the_new_commands_use_the_shared_workspace_resolver(tmp_path):
    """`--project` must aim every one of them, or a command would operate on a different project."""
    project = _attached(tmp_path)
    args = argparse_args(project=str(project))

    from engine.cli import _resolve_workspace, _slug_for

    slug = _slug_for(args)
    assert slug == "attached", "the slug comes from the folder, not a second identifier"
    assert _resolve_workspace(args, slug).path == project.resolve()
    # `abort` reaches the same resolver, so a bare invocation refuses with the resolver's own advice.
    assert run_cli("abort").returncode == EXIT_USAGE
