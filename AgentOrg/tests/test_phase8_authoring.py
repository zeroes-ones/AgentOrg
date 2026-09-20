#!/usr/bin/env python3
"""Phase 8 tests — hiring, skill authoring, and the layered roots.

These are the surfaces that make "create your own agents and skills" real, so the tests focus on the
things that would make them *look* real without working: a hire written to a directory nothing reads,
an authored skill the engine will not load, a project roster that silently shadows the built-ins.

Everything runs in a temp directory with `$AGENTORG_HOME` redirected, so no test touches a real home
directory and the suite stays offline.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import usercfg
from engine.authoring import AuthoringError, SkillTemplate, scaffold, slugify, write_skill
from engine.config import load
from engine.library import resolve
from engine.people import HireError, HireRequest, People
from engine.planner import Planner, emit_safe_yaml
from engine.skills import FilesystemSkillSource
from engine.skills.overlay import OverlaySkillSource


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A redirected global root, so `ensure_roots`/`global_root` never touch the real home."""
    global_root = tmp_path / "home" / ".agentorg"
    monkeypatch.setenv("AGENTORG_HOME", str(global_root))
    return global_root


@pytest.fixture
def project(tmp_path):
    """A project directory with a `.git` marker, which anchors the project root."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    return proj


@pytest.fixture(scope="module")
def library():
    return resolve()


@pytest.fixture(scope="module")
def config():
    """The shipped example, not `load()`.

    A bare `load()` prefers the developer's own `credentials.json`, whose default model may be one the
    provider *probes* rather than one the config *declares* — and a hire resolves a window from the
    declared table. So a test that read the developer's file failed or passed on who ran it. Pinning
    the example makes the fixture describe what it tests.
    """
    example = pathlib.Path(__file__).resolve().parent.parent / "credentials.example.json"
    return load(example)


# ── the layered roots ────────────────────────────────────────────────────────


def test_the_project_root_is_found_by_walking_up(home, project):
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    # Resolved on both sides: macOS reaches the temp dir through the /private symlink, so an
    # unresolved comparison would fail for a reason that has nothing to do with the walk-up.
    assert usercfg.project_root(nested) == (project / ".agentorg").resolve()


def test_roots_put_the_project_before_the_global_one(home, project):
    ordered = usercfg.roots(project=project)
    assert ordered[0] == (project / ".agentorg").resolve()
    assert ordered[1].name == ".agentorg", "the global root must be the lower priority"


def test_a_read_path_creates_nothing(home, project):
    """Inspecting the configuration must not leave directories behind."""
    usercfg.roots(project=project)
    usercfg.skills_dir(project=project)
    assert not (project / ".agentorg").exists()


# ── authoring ────────────────────────────────────────────────────────────────


def test_an_authored_skill_is_parsed_identically_to_a_library_one(home, project, library):
    """The whole point: same format, same parser, same enforcement."""
    write_skill(SkillTemplate(name="db-migrator", purpose="safe migrations"),
                criteria=["Migration is reversible", "Lock impact is measured"],
                checklist=["Down migration exists"], root=project / ".agentorg")
    source = OverlaySkillSource(FilesystemSkillSource(library), project=project,
                                include_global=False)
    bundle = source.load("db-migrator")
    assert list(bundle.contract.criteria) == ["Migration is reversible", "Lock impact is measured"]
    assert bundle.contract.evidence_required is True
    assert bundle.checklist_ids() == ["DM1"], bundle.checklist_ids()


def test_the_scaffold_is_readable_without_pyyaml(home, project):
    """The scaffolder's output must satisfy the engine's *own* fallback parser, not just PyYAML.

    This is the defect CI found and a developer machine could not. `_yaml_block_scalar` emitted the
    folded indicator on a line of its own — `description:` then `  >-` — which is valid YAML and
    PyYAML reads it, while the stdlib parser a PyYAML-less machine uses refuses it ("line is neither a
    'key: value' mapping nor a sequence item: '>-'"). So `skills new` wrote a skill the engine could
    only read where PyYAML happened to be installed, and five tests in this file passed locally and
    failed in CI for exactly that reason.

    Asserted against `prefer_pyyaml=False` rather than by hiding the import, so the guard holds on a
    machine that has PyYAML — which is every developer machine, and that is the problem.
    """
    from engine.skills.frontmatter import parse_frontmatter

    document = scaffold(SkillTemplate(name="stdlib-readable", purpose="survives a bare interpreter"),
                        criteria=["It parses"], checklist=["one item"])
    front, _ = parse_frontmatter(document, prefer_pyyaml=False)
    assert front["name"] == "stdlib-readable"
    assert front["description"] == "survives a bare interpreter"
    # The canonical shape: the indicator follows the key on the same line, as every library skill does.
    assert "description: >-" in document
    assert "\n  >-\n" not in document


def test_authoring_requires_at_least_one_criterion(home, project):
    """A skill with no criteria cannot gate a node, so it would never be enforced."""
    with pytest.raises(AuthoringError, match="criteria"):
        write_skill(SkillTemplate(name="empty"), criteria=[], checklist=["x"],
                    root=project / ".agentorg")


def test_authoring_refuses_to_clobber_a_hand_edited_skill(home, project):
    write_skill(SkillTemplate(name="mine"), criteria=["c"], checklist=["k"],
                root=project / ".agentorg")
    with pytest.raises(AuthoringError, match="already exists"):
        write_skill(SkillTemplate(name="mine"), criteria=["c"], checklist=["k"],
                    root=project / ".agentorg")
    # ...but overwrite is available when asked for explicitly.
    write_skill(SkillTemplate(name="mine"), criteria=["c"], checklist=["k"],
                root=project / ".agentorg", overwrite=True)


def test_scaffold_names_every_checklist_id_and_covers_criteria(home, project):
    text = scaffold(SkillTemplate(name="db-migrator"), criteria=["A", "B"], checklist=["one"])
    assert "[DM1]" in text
    assert "evidence: required" in text
    assert "A" in text and "B" in text


def test_a_slug_is_derived_from_a_phrase():
    assert slugify("Database Migration!") == "database-migration"
    assert slugify("  spaced  ") == "spaced"


def test_an_authored_skill_joins_the_library_rather_than_replacing_it(home, project, library):
    base = FilesystemSkillSource(library)
    write_skill(SkillTemplate(name="extra-skill"), criteria=["c"], checklist=["k"],
                root=project / ".agentorg")
    source = OverlaySkillSource(base, project=project, include_global=False)
    assert "extra-skill" in source.names()
    assert len(source.names()) == len(base.names()) + 1
    # A library skill still resolves through the overlay unchanged.
    assert source.load("code-reviewer").name == "code-reviewer"


def test_a_user_skill_wins_a_name_clash(home, project, library):
    """Stated behaviour, so an override is not a silent no-op."""
    write_skill(SkillTemplate(name="code-reviewer"), criteria=["MY OWN RULE"], checklist=["k"],
                root=project / ".agentorg")
    source = OverlaySkillSource(FilesystemSkillSource(library), project=project,
                                include_global=False)
    assert "MY OWN RULE" in list(source.load("code-reviewer").contract.criteria)


def test_a_project_skill_beats_a_global_one_of_the_same_name(home, project, library):
    write_skill(SkillTemplate(name="shared-name"), criteria=["GLOBAL"], checklist=["k"],
                root=home, global_=True)
    write_skill(SkillTemplate(name="shared-name"), criteria=["PROJECT"], checklist=["k"],
                root=project / ".agentorg")
    source = OverlaySkillSource(FilesystemSkillSource(library), project=project)
    assert list(source.load("shared-name").contract.criteria) == ["PROJECT"]


# ── the authored skill reaches a plan and a run ──────────────────────────────


def _manifest_naming(skill: str) -> dict:
    """A minimal valid graph whose first node names `skill`."""
    return {
        "name": "authored-node",
        "version": "1.0.0",
        "description": "a node that names an authored skill",
        "start": "work",
        "nodes": [{"id": "work", "skill": skill, "max_iterations": 1},
                  {"id": "fix", "skill": "backend-developer", "max_iterations": 1}],
        "edges": [{"from": "work", "to": "fix", "when": "work.status == done",
                   "payload": "handoff-v1"}],
        "loops": [{"id": "l", "nodes": ["fix", "work"], "exit_when": "work.verdict == pass",
                   "max_iterations": 2, "escalate_to": "fix"}],
        "end": ["fix"],
    }


def test_the_planner_plans_a_node_the_goal_names_when_the_owner_authored_it(home, project, library):
    """A genuinely new skill must be able to reach a plan, or authoring is inert.

    The composition tables can only name library skills, so without a path from the goal's own words
    to the overlay a skill could be created, hired against, loaded — and never planned.
    """
    write_skill(SkillTemplate(name="db-migrator"), criteria=["reversible"], checklist=["down"],
                root=project / ".agentorg")
    source = OverlaySkillSource(FilesystemSkillSource(library), project=project,
                                include_global=False)
    plan = Planner(source).plan("Run the db-migrator over the payments schema", slug="migrate")
    assert "db-migrator" in plan.skills_used, plan.skills_used
    assert any(node.get("skill") == "db-migrator" for node in plan.nodes)
    assert plan.validation.valid, plan.validation.errors


def test_a_goal_that_merely_mentions_the_subject_does_not_drag_the_skill_in(home, project,
                                                                         library):
    """The match is the skill's *name*, not any word in common — or every plan would collect skills."""
    write_skill(SkillTemplate(name="db-migrator"), criteria=["reversible"], checklist=["down"],
                root=project / ".agentorg")
    source = OverlaySkillSource(FilesystemSkillSource(library), project=project,
                                include_global=False)
    plan = Planner(source).plan("Build a booking API with a migrations folder", slug="booking")
    assert "db-migrator" not in plan.skills_used, plan.skills_used


def test_the_validator_resolves_an_authored_skill_and_still_refuses_an_unknown_one(home, project,
                                                                                 library):
    """Widening the validator's catalogue must not become "any name at all goes"."""
    write_skill(SkillTemplate(name="db-migrator"), criteria=["reversible"], checklist=["down"],
                root=project / ".agentorg")
    planner = Planner(OverlaySkillSource(FilesystemSkillSource(library), project=project,
                                         include_global=False))
    assert planner.validate(_manifest_naming("db-migrator")).valid

    verdict = planner.validate(_manifest_naming("no-such-skill"))
    assert not verdict.valid
    assert any("does not resolve" in error for error in verdict.errors), verdict.errors
    # The refusal must carry the library's own words. Reading only `message` from an error the
    # library reports as `{"error": …}` printed the reason as the string "None".
    assert all(error and error != "None" for error in verdict.errors), verdict.errors


#: Run in a *fresh* process, because the generated plugin builds its overlay at import time — the
#: only moment the child's view of the skill roots exists, and therefore the only place this defect
#: was visible. The plugin path arrives as argv[1], so what is exercised is the run's own generated
#: file rather than a reconstruction of it.
_CHILD_PROBE = '''
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("generated_executor", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["generated_executor"] = module
spec.loader.exec_module(module)

skills = module._context.skills
print("ROOTS " + json.dumps([str(p) for p in skills.roots()]))
for name in ("project-marker", "global-marker"):
    print(f"RESOLVES {name} {skills.has(name)}")
    if skills.has(name):
        print(f"CRITERIA {name} {list(skills.load(name).contract.criteria)}")
'''


def test_the_process_that_runs_a_node_sees_every_root_the_planner_planned_with(home, project,
                                                                              config, library,
                                                                              tmp_path):
    """The child's overlay must be the parent's overlay, or a planned skill silently does not exist.

    The overlay used to be rebuilt inside the generated plugin from the project root alone, so a
    *global* skill planned and hired successfully and was then absent from the process that ran the
    node — an invisible failure, since the run proceeds with a prompt that quietly lost the skill.
    """
    from engine import runcontext
    from engine.host import RunnerHost
    from engine.orchestrator import _skill_source
    from engine.runcontext import RunContext

    write_skill(SkillTemplate(name="project-marker"), criteria=["PROJECT"], checklist=["k"],
                root=project / ".agentorg")
    write_skill(SkillTemplate(name="global-marker"), criteria=["GLOBAL"], checklist=["k"],
                root=home, global_=True)

    # The parent side: the roots the orchestrator records (orchestrator.py `_write_run_context`).
    source = _skill_source(library, project=project)
    parent_roots = [str(path) for path in source.roots()]
    assert any(path.startswith(str(home)) for path in parent_roots), parent_roots

    workspace = project / "projects" / "proof"
    workspace.mkdir(parents=True)
    manifest = workspace / "proof.yaml"
    manifest.write_text("name: proof\n", encoding="utf-8")
    runcontext.write(workspace, RunContext(org={}, bindings={}, skill_roots=parent_roots))

    plugin = RunnerHost(config=config, library=library, workspace=workspace).plugin_paths(
        manifest_path=manifest, run_id="proof", workflow="proof", project="proof")["executor"]

    probe = workspace / "probe.py"
    probe.write_text(_CHILD_PROBE, encoding="utf-8")
    # The runner runs from the library root, and the library root is pinned the same way here so the
    # probe resolves the same checkout the fixture did.
    result = subprocess.run(
        [sys.executable, str(probe), str(plugin)], cwd=str(library.files.root),
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "AGENTORG_SKILLS_ROOT": str(library.files.root)},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(_output_line(result.stdout, "ROOTS ")) == parent_roots
    assert "RESOLVES project-marker True" in result.stdout
    assert "RESOLVES global-marker True" in result.stdout
    assert "CRITERIA global-marker ['GLOBAL']" in result.stdout


def _output_line(stdout: str, prefix: str) -> str:
    """The payload of the probe's single `<prefix>…` line."""
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    raise AssertionError(f"the child printed no {prefix!r} line:\n{stdout}")


def test_the_runner_refuses_a_manifest_naming_a_new_skill_and_says_so(home, project, config,
                                                                     library):
    """A host that was never told the catalogue still refuses an authored name, and says why.

    This is the old behaviour, kept because it is still the behaviour of a host that was handed no
    skill source: the composed view is opt-in, so nothing here widened by accident. What must not
    happen is the refusal arriving as a bare exit code — the runner's own line is the whole diagnosis,
    and here the engine's own planner is out of the picture entirely.
    """
    from engine.host import RunnerHost

    write_skill(SkillTemplate(name="db-migrator"), criteria=["reversible"], checklist=["down"],
                root=project / ".agentorg")
    workspace = project / "projects" / "wall"
    workspace.mkdir(parents=True)
    manifest = workspace / "wall.yaml"
    manifest.write_text(emit_safe_yaml(_manifest_naming("db-migrator")), encoding="utf-8")

    outcome = RunnerHost(config=config, library=library, workspace=workspace).run(
        manifest_path=manifest, run_id="wall", workflow="wall", project="wall")
    assert outcome.broken, outcome.as_dict()
    assert "db-migrator" in outcome.error, outcome.as_dict()


# ── the runner's own catalogue ───────────────────────────────────────────────
#
# The runner validates every node's `skill:` against the `skills/` directory beside its own
# `scripts/`, so an authored name was refused by a process that had never been told the skill
# existed. `engine.runner_view` composes the catalogue the engine actually uses — the pinned library
# plus the Owner's roots — into a directory the runner can be invoked *from*, and these tests pin
# both halves: that the wall is real against the library alone, and that the view moves it without
# the library being touched or a name nobody wrote becoming acceptable.


def _invoke_runner(runner: pathlib.Path, manifest: pathlib.Path,
                   state: pathlib.Path) -> subprocess.CompletedProcess:
    """Invoke the pinned runner the way the host does, with no executor and so no model call.

    `--executor` is left off on purpose: the runner then answers each node with its own stub, which is
    what keeps these tests about the manifest validation in front of execution rather than about a
    provider. The cwd is the root the runner *path* presents, exactly as `host.py` sets it, because
    that root is what `validate-workflows.py` scans for skills.
    """
    return subprocess.run(
        [sys.executable, str(runner), "--manifest", str(manifest), "--state", str(state)],
        cwd=str(runner.parent.parent), capture_output=True, text=True, timeout=300,
    )


def _runner_fixture(project: pathlib.Path, skill: str) -> tuple[pathlib.Path, pathlib.Path]:
    """A one-file project holding a manifest whose first node names `skill`.

    The manifest's *filename* has to equal its `name`, which the library's validator enforces, and
    `_manifest_naming` names it `authored-node`.
    """
    workspace = project / "runner-view"
    workspace.mkdir(parents=True, exist_ok=True)
    manifest = workspace / "authored-node.yaml"
    manifest.write_text(emit_safe_yaml(_manifest_naming(skill)), encoding="utf-8")
    return workspace, manifest


def _tree_fingerprint(root: pathlib.Path) -> dict[str, tuple[int, int]]:
    """Every path under `root` with its size and mtime, so a run that rewrote it would show."""
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
    }


def test_the_pinned_runner_refuses_an_authored_name_from_the_library_alone(home, project, library):
    """The wall, reproduced against the pinned runner with nothing about it patched.

    Asserted rather than assumed: if a later library version resolved the Owner's skills by itself,
    this test would fail and the composed view would be dead weight rather than the fix.
    """
    write_skill(SkillTemplate(name="db-migrator"), criteria=["reversible"], checklist=["down"],
                root=project / ".agentorg")
    workspace, manifest = _runner_fixture(project, "db-migrator")

    result = _invoke_runner(library.files.runner, manifest, workspace / "state.json")
    assert result.returncode == 1, result.stdout
    assert "db-migrator" in result.stderr
    assert "does not resolve under skills/" in result.stderr


def test_the_composed_view_lets_that_same_manifest_run(home, project, library):
    """The whole point: the same manifest, the same pinned runner, a catalogue it can resolve.

    The graph's *second* node names `backend-developer`, a library skill, so this also fails if the
    view lost the library — a view that resolved only the Owner's skills would move the wall rather
    than remove it.
    """
    from engine import runner_view

    write_skill(SkillTemplate(name="db-migrator"), criteria=["reversible"], checklist=["down"],
                root=project / ".agentorg")
    workspace, manifest = _runner_fixture(project, "db-migrator")

    source = OverlaySkillSource(FilesystemSkillSource(library), project=project,
                                include_global=False)
    view = runner_view.runner_view(library=library, workspace=workspace, source=source)
    assert view is not None
    assert view.runner.is_file(), view.runner

    result = _invoke_runner(view.runner, manifest, workspace / "state.json")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["outcome"] == "complete", summary
    assert set(summary["nodes"]) == {"work", "fix"}, summary["nodes"]


def test_the_composed_view_still_refuses_a_skill_nobody_wrote(home, project, library):
    """A view is a catalogue, not a licence: the wall must move for authored names, not for invented ones.

    The words are the library's own, because the refusal is still the library's — nothing about the
    check was weakened, only the catalogue it checks against.
    """
    from engine import runner_view

    write_skill(SkillTemplate(name="db-migrator"), criteria=["reversible"], checklist=["down"],
                root=project / ".agentorg")
    workspace, _ = _runner_fixture(project, "db-migrator")
    manifest = workspace / "authored-node.yaml"
    manifest.write_text(emit_safe_yaml(_manifest_naming("no-such-skill")), encoding="utf-8")

    source = OverlaySkillSource(FilesystemSkillSource(library), project=project,
                                include_global=False)
    view = runner_view.runner_view(library=library, workspace=workspace, source=source)
    assert view is not None

    result = _invoke_runner(view.runner, manifest, workspace / "state.json")
    assert result.returncode == 1, result.stdout
    assert "no-such-skill" in result.stderr
    assert "does not resolve under skills/" in result.stderr


def test_composing_the_view_leaves_the_library_untouched(home, project, library):
    """Nothing under the pinned library may be created, moved or rewritten by a run.

    The pin is a content manifest of that tree, so a run that wrote into the library — even to add a
    skill it had every right to add — would break the guarantee that makes the library a dependency
    rather than a scratch directory. The view is a directory of symlinks in the workspace for exactly
    this reason.
    """
    from engine import runner_view

    write_skill(SkillTemplate(name="db-migrator"), criteria=["reversible"], checklist=["down"],
                root=project / ".agentorg")
    workspace, _ = _runner_fixture(project, "db-migrator")

    before = {name: _tree_fingerprint(library.files.root / name)
              for name in ("skills", "scripts")}
    source = OverlaySkillSource(FilesystemSkillSource(library), project=project,
                                include_global=False)
    view = runner_view.runner_view(library=library, workspace=workspace, source=source)
    assert view is not None

    after = {name: _tree_fingerprint(library.files.root / name) for name in ("skills", "scripts")}
    assert after == before
    # And the view itself is not inside the library: a directory under it would have been a link
    # *into* the library, which the fingerprint above already refuses, but nothing should point back.
    assert library.files.root.resolve() not in view.root.resolve().parents


def test_the_view_is_not_composed_when_the_project_authored_nothing(home, project, library):
    """A library-only org keeps the plain invocation, and nothing is written for it.

    This is what makes the view safe to have: with no authored skills there is nothing a catalogue
    could add, so the host asks for the runner exactly where it has always asked for it.
    """
    from engine import runner_view

    workspace = project / "runner-view"
    workspace.mkdir(parents=True)
    source = OverlaySkillSource(FilesystemSkillSource(library), project=project,
                                include_global=False)
    assert runner_view.runner_view(library=library, workspace=workspace, source=source) is None
    # A source that has no notion of authored skills at all answers the same way.
    assert runner_view.runner_view(library=library, workspace=workspace,
                                   source=FilesystemSkillSource(library)) is None
    assert not (workspace / ".agent_state" / "runner-view").exists()


def test_the_view_prunes_a_skill_the_owner_deleted(home, project, library):
    """A name that outlives its skill is worse than a missing one.

    It resolves in the runner and then fails at bind in the process that loads the skill — a failure
    that describes nothing. Pruning is what turns that back into a refusal by name.
    """
    from engine import runner_view

    for name in ("db-migrator", "kept-writer"):
        write_skill(SkillTemplate(name=name), criteria=["reversible"], checklist=["down"],
                    root=project / ".agentorg")
    workspace, _ = _runner_fixture(project, "db-migrator")

    def compose():
        source = OverlaySkillSource(FilesystemSkillSource(library), project=project,
                                    include_global=False)
        return runner_view.runner_view(library=library, workspace=workspace, source=source)

    view = compose()
    assert view is not None
    authored = view.root / "skills" / runner_view.AUTHORED_DIRNAME
    assert (authored / "db-migrator" / "SKILL.md").is_file()
    assert (authored / "kept-writer" / "SKILL.md").is_file()

    shutil.rmtree(project / ".agentorg" / "skills" / "db-migrator")
    view = compose()
    assert view is not None
    assert not (authored / "db-migrator").exists()
    assert (authored / "kept-writer" / "SKILL.md").is_file()


def test_the_host_shows_the_runner_the_catalogue_it_was_given(home, project, config, library):
    """The wiring, asserted on its own, because a fix that is never handed to the host is inert.

    Two facts, both of them the contract: a host given a source with authored skills runs the
    composed view's runner from the view's root, and a host given nothing runs the pinned library's
    runner from the pinned library.
    """
    from engine import runner_view
    from engine.host import RunnerHost

    write_skill(SkillTemplate(name="db-migrator"), criteria=["reversible"], checklist=["down"],
                root=project / ".agentorg")
    source = OverlaySkillSource(FilesystemSkillSource(library), project=project,
                                include_global=False)

    shown = RunnerHost(config=config, library=library, workspace=project, skills=source)
    runner, root = shown._runner_and_root()
    # Resolved, because both sides resolve: `Workspace.attach` and the host hand over real paths, and
    # on macOS a temp directory's own path is a symlink target (`/var` vs `/private/var`).
    assert root == (project / ".agent_state" / runner_view.VIEW_DIRNAME).resolve()
    assert runner == root / "scripts" / "workflow-runner.py"
    assert runner.is_file()

    plain = RunnerHost(config=config, library=library, workspace=project)
    plain_runner, plain_root = plain._runner_and_root()
    assert plain_runner == pathlib.Path(library.files.runner)
    # The root is derived from the path, and for the pinned layout that is the library's own root —
    # which is what the runner's `ROOT` resolves to when nothing is composed.
    assert plain_root == pathlib.Path(library.files.runner).parent.parent


def test_the_orchestrator_resolves_skills_against_the_project_it_attached(home, project, config,
                                                                         library):
    """A project's own skills must be found through `--project`, not through its *parent*.

    The overlay was given the workspace's `root` — the directory projects live in, which for an
    attached folder is its parent — so the project's own skills directory was never searched. The
    engine then planned and hired against a catalogue that was missing exactly the skills the Owner
    had just authored, and refused the node by name. The roster resolver has always read the folder
    itself (`usercfg.project_root`), so anything that reads it from the parent disagrees with it.
    """
    from engine.orchestrator import Orchestrator
    from engine.state import Workspace

    write_skill(SkillTemplate(name="db-migrator"), criteria=["reversible"], checklist=["down"],
                root=project / ".agentorg")
    orch = Orchestrator(config=config, library=library, workspace=Workspace.attach(project))

    assert orch.source.roots()[0] == (project / ".agentorg" / "skills").resolve()
    assert "db-migrator" in orch.source.names()
    # And the planner's validator — which is handed the same catalogue — accepts the node it refused.
    assert orch.planner.validate(_manifest_naming("db-migrator")).valid



# ── hiring ───────────────────────────────────────────────────────────────────


def make_people(config, library, project):
    return People(library=library, config=config, project=project)


def test_the_base_company_runs_with_no_hires(home, project, config, library):
    """The built-ins are the floor: an empty roster must still be a runnable org."""
    people = make_people(config, library, project)
    org = people.load(project=project)
    assert len([a for a in org.agents.values() if not a.is_human]) == 7


def test_a_hire_appears_in_the_roster_and_persists(home, project, config, library):
    people = make_people(config, library, project)
    org = people.load(project=project)
    spec = people.hire(HireRequest(name="Dana", skill="security-reviewer",
                                   provider="ollama", model="qwen2.5-coder:7b"),
                       org=org, roster_root=project / ".agentorg")
    assert spec.skills == ["security-reviewer"]
    assert spec.id in org.agents
    # Reload from disk: the hire must survive the process, not just the object.
    again = make_people(config, library, project).load(project=project)
    assert any(a.name == "Dana" for a in again.agents.values())


def test_the_roster_file_records_only_the_hire(home, project, config, library):
    """Otherwise the project file freezes a stale copy of the built-ins."""
    people = make_people(config, library, project)
    org = people.load(project=project)
    people.hire(HireRequest(name="Dana", skill="security-reviewer",
                            provider="ollama", model="qwen2.5-coder:7b"),
                org=org, roster_root=project / ".agentorg")
    document = json.loads((project / ".agentorg" / "roster.json").read_text())
    names = [a["name"] for a in document["agents"]]
    assert names == ["Dana"], names


def test_a_hire_is_reachable_from_a_run_not_just_a_listing(home, project, config, library):
    """A hire that never reaches execution would be a capability with no effect."""
    from engine.orchestrator import _skill_source

    people = make_people(config, library, project)
    org = people.load(project=project)
    people.hire(HireRequest(name="Dana", skill="security-reviewer",
                            provider="ollama", model="qwen2.5-coder:7b"),
                org=org, roster_root=project / ".agentorg")
    reloaded = make_people(config, library, project).load(project=project)
    assert "Dana" in {a.name for a in reloaded.agents.values()}
    assert _skill_source(library) is not None


def test_a_duplicate_name_is_refused(home, project, config, library):
    """Two agents called Alice would make the roster and every log line ambiguous."""
    people = make_people(config, library, project)
    org = people.load(project=project)
    with pytest.raises(HireError, match="already exists"):
        people.hire(HireRequest(name="Alice", skill="code-reviewer"), org=org)


def test_an_unknown_skill_is_refused_with_suggestions(home, project, config, library):
    people = make_people(config, library, project)
    org = people.load(project=project)
    with pytest.raises(HireError, match="no skill named"):
        people.hire(HireRequest(name="Zed", skill="code-reviewr"), org=org)


def test_an_unknown_level_is_refused(home, project, config, library):
    people = make_people(config, library, project)
    org = people.load(project=project)
    with pytest.raises(HireError, match="unknown level"):
        people.hire(HireRequest(name="Zed", skill="code-reviewer", level="wizard"), org=org)


def test_a_reviewer_sharing_the_producers_model_warns_rather_than_failing(home, project,
                                                                          config, library):
    """Refusing would make a reviewer unhirable on a single-model local setup, which is stricter
    than the engine's own `default_company` default."""
    people = make_people(config, library, project)
    org = people.load(project=project)
    people.hire(HireRequest(name="Dana", skill="security-reviewer",
                            provider="ollama", model="qwen2.5-coder:7b"), org=org)
    assert any("independence" in w for w in people.warnings)


def test_a_reviewer_skill_yields_a_reviewer_role_and_read_only_capabilities(home, project,
                                                                           config, library):
    people = make_people(config, library, project)
    org = people.load(project=project)
    spec = people.hire(HireRequest(name="Dana", skill="security-reviewer"), org=org)
    assert spec.role == "reviewer"
    assert "write:src/**" not in spec.capabilities, "a reviewer needs no write capability"


def test_a_hire_is_validated_against_authored_skills_too(home, project, config, library):
    """A skill the engine can run must be a skill a hire accepts — the same view, or one lies."""
    write_skill(SkillTemplate(name="db-migrator"), criteria=["reversible"], checklist=["down"],
                root=project / ".agentorg")
    people = make_people(config, library, project)
    org = people.load(project=project)
    spec = people.hire(HireRequest(name="Migrator", skill="db-migrator"), org=org)
    assert spec.skills == ["db-migrator"]
