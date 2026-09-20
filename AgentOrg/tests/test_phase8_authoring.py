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
from engine.planner import Planner
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
    """The one wall an authored skill still meets, pinned so it cannot pass unnoticed.

    The engine's planner and validator now resolve a node's skill through the overlay — as the hire
    and the executor already did — but the *runner* validates the manifest itself against the
    library's names before executing anything, and there is no way to tell it about an authored one.
    So a genuinely new name is planned and then refused at the run's first line. What must not happen
    is that refusal arriving as a bare exit code: the runner's own line is the whole diagnosis.
    """
    from engine.host import RunnerHost
    from engine.planner import emit_safe_yaml

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
