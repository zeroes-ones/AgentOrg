#!/usr/bin/env python3
"""Phase 20 tests — creating and editing agents from a console.

The product modelled agents but could only create them from the CLI, so the console could show a
roster it could not change. These tests are about the two things that make editing safe:

1. **The id is the identity.** Editing keeps it, so the mailbox, session history, ledger entries and
   health record — all keyed on the id — survive. A fire-and-rehire would silently reset every one,
   and "I only changed the model" would look like a brand-new employee with no past.
2. **Save and load agree on one path.** A hire that appears to succeed and then vanishes is worse than
   one that fails, so the round trip is tested rather than assumed.
"""

from __future__ import annotations

import io
import os
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.catalog import ModelCatalog
from engine.config import load
from engine.library import resolve
from engine.org.agent import AgentLevel
from engine.people import HireError, HireRequest, People
from engine.providers.registry import build_providers
from engine.serve import Server
from engine.state import Workspace

# A name the built-in company does not use, so a test hire never collides with a default.
FRESH_NAME = "Nadia"


def _people(tmp_path):
    config = load()
    providers, _ = build_providers(config)
    return People(library=resolve(), config=config,
                  catalog=ModelCatalog(config, providers), project=tmp_path)


def _hired(tmp_path, name=FRESH_NAME, skill="code-reviewer", model="qwen2.5-coder:7b"):
    people = _people(tmp_path)
    org = people.load(project=tmp_path)
    spec = people.hire(HireRequest(name=name, skill=skill, provider="ollama", model=model),
                       org=org)
    return people, spec


def _server(tmp_path, slug="ag"):
    workspace = Workspace.for_project(slug, root=tmp_path)
    workspace.ensure()
    return Server(config=load(), library=resolve(), workspace=workspace, slug=slug,
                  stdin=io.StringIO(""), stdout=io.StringIO())


# ── editing keeps the identity ───────────────────────────────────────────────


def test_update_keeps_the_id_and_changes_the_model(tmp_path):
    """The id is what history is keyed on, so an edit must not mint a new one."""
    people, spec = _hired(tmp_path)
    updated = people.update_agent(spec.id, model="qwen2.5-coder:14b", context_window=32768,
                                  org=people.org)
    assert updated.id == spec.id
    assert updated.model == "qwen2.5-coder:14b"
    assert updated.context_window == 32768


def test_update_touches_only_what_was_passed(tmp_path):
    """A caller changing one thing should not have to restate the rest."""
    people, spec = _hired(tmp_path)
    original_title = spec.title
    original_model = spec.model
    people.update_agent(spec.id, level="senior", org=people.org)
    updated = people.org.agents[spec.id]
    assert updated.title == original_title
    assert updated.model == original_model
    assert updated.level == AgentLevel.SENIOR


def test_update_renames(tmp_path):
    people, spec = _hired(tmp_path)
    people.update_agent(spec.id, name="Nadia K", org=people.org)
    assert people.org.agents[spec.id].name == "Nadia K"


def test_update_refuses_a_name_that_is_taken(tmp_path):
    people, spec = _hired(tmp_path)
    other = people.hire(HireRequest(name="Omar", skill="code-reviewer", provider="ollama",
                                    model="qwen2.5-coder:7b"), org=people.org)
    with pytest.raises(HireError, match="already exists"):
        people.update_agent(spec.id, name="Omar", org=people.org)
    assert other.name == "Omar"


def test_update_refuses_a_model_with_no_known_window(tmp_path):
    """Binding to an unknown window is the overflow the whole projection exists to prevent."""
    people, spec = _hired(tmp_path)
    with pytest.raises(HireError, match="context window"):
        people.update_agent(spec.id, provider="openai", model="totally-unknown-model",
                            org=people.org)


def test_update_refuses_an_unknown_agent(tmp_path):
    people, _ = _hired(tmp_path)
    with pytest.raises(HireError, match="no agent"):
        people.update_agent("ag_ghost", level="senior", org=people.org)


def test_update_refuses_an_unknown_level(tmp_path):
    people, spec = _hired(tmp_path)
    with pytest.raises(HireError, match="unknown level"):
        people.update_agent(spec.id, level="wizard", org=people.org)


def test_update_moves_team_without_leaving_a_stale_member(tmp_path):
    people, spec = _hired(tmp_path)
    people.update_agent(spec.id, team="Quality", org=people.org)
    team = people.org.teams.get("Quality")
    assert team is not None and spec.id in team.members
    people.update_agent(spec.id, team="Platform", org=people.org)
    quality = people.org.teams.get("Quality")
    assert quality is None or spec.id not in quality.members


# ── retiring ─────────────────────────────────────────────────────────────────


def test_retire_removes_the_agent(tmp_path):
    people, spec = _hired(tmp_path)
    people.retire_agent(spec.id, org=people.org)
    assert spec.id not in people.org.agents


def test_the_owner_cannot_be_retired(tmp_path):
    """The Owner holds terminal authority; removing it would leave the org ungoverned."""
    people, _ = _hired(tmp_path)
    with pytest.raises(HireError, match="Owner"):
        people.retire_agent("ag_owner", org=people.org)


def test_retire_refuses_an_unknown_agent(tmp_path):
    people, _ = _hired(tmp_path)
    with pytest.raises(HireError, match="unknown agent"):
        people.retire_agent("ag_ghost", org=people.org)


# ── persist and reload agree ─────────────────────────────────────────────────


def test_a_hire_is_found_by_a_fresh_manager(tmp_path):
    """Save and load must agree on one path: a hire that appears to succeed and then vanishes is
    worse than one that fails."""
    _, spec = _hired(tmp_path)
    fresh = _people(tmp_path)
    org = fresh.load(project=tmp_path)
    assert spec.id in org.agents


def test_an_edit_persists(tmp_path):
    people, spec = _hired(tmp_path)
    people.update_agent(spec.id, model="qwen2.5-coder:14b", context_window=32768, org=people.org)
    people.save(org=people.org)

    fresh = _people(tmp_path)
    org = fresh.load(project=tmp_path)
    assert org.agents[spec.id].model == "qwen2.5-coder:14b"


def test_a_retire_persists(tmp_path):
    people, spec = _hired(tmp_path)
    people.retire_agent(spec.id, org=people.org)

    fresh = _people(tmp_path)
    org = fresh.load(project=tmp_path)
    assert spec.id not in org.agents


def test_the_built_in_company_is_never_frozen_into_the_roster(tmp_path):
    """Writing the defaults would shadow a later change to the built-ins with a stale snapshot."""
    people, _ = _hired(tmp_path)
    import json

    document = json.loads((tmp_path / ".agentorg" / "roster.json").read_text())
    ids = [a["id"] for a in document["agents"]]
    assert ids == [a["id"] for a in document["agents"] if a.get("origin") == "owner"]
    assert "ag_owner" not in ids


# ── the console surface ──────────────────────────────────────────────────────


def test_the_agent_list_reports_built_ins_and_hires(tmp_path):
    server = _server(tmp_path)
    payload = server._cmd_agents({})
    assert payload["count"] >= 7
    assert payload["hired"] == 0, "a fresh install has hired nobody"
    statuses = {a["status"] for a in payload["agents"]}
    assert "built-in" in statuses
    assert "owner" in statuses


def test_the_owner_principal_is_not_counted_as_a_hire(tmp_path):
    """It is terminal authority the engine always has, not headcount."""
    server = _server(tmp_path)
    owner = [a for a in server._cmd_agents({})["agents"] if a["id"] == "ag_owner"][0]
    assert owner["status"] == "owner"
    assert owner["editable"] is False


def test_the_agent_list_offers_real_skills(tmp_path):
    """A hire form with an empty skill picker is worse than no form."""
    server = _server(tmp_path)
    skills = server._cmd_agents({})["skills"]
    assert len(skills) > 50
    assert "code-reviewer" in skills


def test_the_agent_list_names_the_roster_file(tmp_path):
    server = _server(tmp_path)
    assert pathlib.Path(server._cmd_agents({})["roster_path"]).name == "roster.json"


def test_the_console_can_hire(tmp_path):
    server = _server(tmp_path)
    result = server._cmd_hire({"name": FRESH_NAME, "skill": "code-reviewer",
                               "provider": "ollama", "model": "qwen2.5-coder:7b"})
    assert result["agent"]["name"] == FRESH_NAME
    assert server._cmd_agents({})["hired"] == 1


def test_the_console_can_edit_and_keeps_the_id(tmp_path):
    server = _server(tmp_path)
    spec = server._cmd_hire({"name": FRESH_NAME, "skill": "code-reviewer",
                             "provider": "ollama", "model": "qwen2.5-coder:7b"})["agent"]
    updated = server._cmd_agent_update({"agent_id": spec["id"], "model": "qwen2.5-coder:14b",
                                        "context_window": 32768})["agent"]
    assert updated["id"] == spec["id"]
    assert updated["model"] == "qwen2.5-coder:14b"


def test_the_console_can_retire(tmp_path):
    server = _server(tmp_path)
    spec = server._cmd_hire({"name": FRESH_NAME, "skill": "code-reviewer",
                             "provider": "ollama", "model": "qwen2.5-coder:7b"})["agent"]
    result = server._cmd_agent_retire({"agent_id": spec["id"]})
    assert result["retired"]["id"] == spec["id"]
    assert server._cmd_agents({})["hired"] == 0


def test_the_console_cannot_retire_the_owner(tmp_path):
    server = _server(tmp_path)
    with pytest.raises(Exception):
        server._cmd_agent_retire({"agent_id": "ag_owner"})


def test_an_edit_needs_an_id(tmp_path):
    server = _server(tmp_path)
    with pytest.raises(Exception):
        server._cmd_agent_update({})


def test_a_hire_survives_a_new_server(tmp_path):
    """The console's hire must be visible to the next process, not only the one that made it."""
    _server(tmp_path)._cmd_hire({"name": FRESH_NAME, "skill": "code-reviewer",
                                 "provider": "ollama", "model": "qwen2.5-coder:7b"})
    fresh = _server(tmp_path)
    names = [a["name"] for a in fresh._cmd_agents({})["agents"] if a["status"] == "hired"]
    assert FRESH_NAME in names


def test_the_console_can_list_skills(tmp_path):
    server = _server(tmp_path)
    assert "code-reviewer" in server._cmd_skills({})["skills"]
