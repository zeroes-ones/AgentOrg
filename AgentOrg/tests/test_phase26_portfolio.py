#!/usr/bin/env python3
"""Phase 26 tests — the Portfolio: one principal, several orgs.

The engine assumed one org per workspace. That is correct for a project and wrong for a *person*, who
in the real world runs several organisations at once — each with its own agents, missions, goals,
budget and risk. These tests pin the register that models that, and the org identity it points at.

The safety properties matter most: a portfolio runs nothing, spends nothing, and never re-arms work
on load; and an org's identity survives a rename.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.org import Org, default_company
from engine.portfolio import (
    DEFAULT_PRINCIPAL_ID,
    PORTFOLIO_VERSION,
    OrgEntry,
    Portfolio,
    PortfolioError,
    Principal,
)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "portfolio-root"


# ── the principal ────────────────────────────────────────────────────────────


def test_a_new_portfolio_has_one_principal_and_no_orgs():
    portfolio = Portfolio.new(principal_name="Elon")
    assert portfolio.principal.name == "Elon"
    assert portfolio.principal.id == DEFAULT_PRINCIPAL_ID
    assert portfolio.orgs == []
    assert portfolio.active_org() is None


def test_a_principal_needs_a_name():
    with pytest.raises(PortfolioError, match="needs a name"):
        Portfolio.new().ensure_principal("   ")


# ── registering orgs ─────────────────────────────────────────────────────────


def test_orgs_are_registered_with_stable_ids(root):
    portfolio = Portfolio.new(principal_name="Elon")
    tesla = portfolio.add_org(name="Tesla", slug="tesla", charter="EVs")
    spacex = portfolio.add_org(name="SpaceX", slug="spacex", charter="Space")
    assert tesla.id == "org_tesla" and spacex.id == "org_spacex"
    assert [o.name for o in portfolio.orgs] == ["Tesla", "SpaceX"]
    # The first org becomes active automatically, so a bare command is unambiguous.
    assert portfolio.active_org().name == "Tesla"


def test_a_duplicate_slug_or_name_is_refused(root):
    portfolio = Portfolio.new()
    portfolio.add_org(name="Tesla", slug="tesla")
    with pytest.raises(PortfolioError, match="slug"):
        portfolio.add_org(name="Tesla Motors", slug="tesla")
    with pytest.raises(PortfolioError, match="already exists"):
        portfolio.add_org(name="tesla")


def test_one_folder_is_one_org(root):
    portfolio = Portfolio.new()
    folder = root / "shared"
    portfolio.add_org(name="A", path=folder)
    with pytest.raises(PortfolioError, match="already the folder"):
        portfolio.add_org(name="B", path=folder)


def test_an_org_can_be_looked_up_by_name_slug_or_id(root):
    portfolio = Portfolio.new()
    entry = portfolio.add_org(name="Space Exploration", slug="spacex")
    assert portfolio.org("Space Exploration").id == entry.id
    assert portfolio.org("spacex").id == entry.id
    assert portfolio.org(entry.id).id == entry.id
    assert portfolio.has_org("spacex") and not portfolio.has_org("nope")
    with pytest.raises(PortfolioError, match="known orgs"):
        portfolio.org("nope")


def test_renaming_an_org_keeps_its_identity(root):
    """A rename changes the label, never the id — or a rename would orphan the roster and the spend."""
    portfolio = Portfolio.new()
    entry = portfolio.add_org(name="Tesla", slug="tesla")
    original_id = entry.id
    portfolio.update_org("tesla", name="Tesla, Inc.")
    assert portfolio.org("tesla").name == "Tesla, Inc."
    assert portfolio.org("tesla").id == original_id
    assert portfolio.org("tesla").slug == "tesla", "the slug (and so the folder) is stable"


def test_removing_an_org_only_forgets_the_pointer(root):
    """It must not delete the folder — the roster, missions and runs stay on disk."""
    portfolio = Portfolio.new()
    folder = root / "tesla"
    folder.mkdir(parents=True)
    entry = portfolio.add_org(name="Tesla", path=folder)
    portfolio.remove_org("tesla")
    assert portfolio.orgs == []
    assert folder.is_dir(), "removing an org from the register must not touch its folder"


def test_enabling_and_budget_are_editable(root):
    portfolio = Portfolio.new()
    portfolio.add_org(name="Tesla", slug="tesla")
    portfolio.update_org("tesla", enabled=False, daily_budget_usd=25.0)
    entry = portfolio.org("tesla")
    assert entry.enabled is False and entry.daily_budget_usd == 25.0


# ── persistence ──────────────────────────────────────────────────────────────


def test_a_portfolio_round_trips_through_disk(root):
    portfolio = Portfolio.new(principal_name="Elon")
    portfolio.add_org(name="Tesla", slug="tesla", path=root / "tesla", daily_budget_usd=50)
    portfolio.add_org(name="SpaceX", slug="spacex", path=root / "spacex", make_active=True)
    portfolio.save(root)

    assert Portfolio.path_for(root).is_file()
    loaded = Portfolio.load(root)
    assert loaded is not None
    assert loaded.principal.name == "Elon"
    assert [o.name for o in loaded.orgs] == ["Tesla", "SpaceX"]
    assert loaded.active_org().name == "SpaceX"
    assert loaded.org("tesla").daily_budget_usd == 50.0


def test_loading_when_there_is_none_is_none(root):
    """None, not an empty portfolio: "no register" and "a register with no orgs" are different."""
    assert Portfolio.load(root) is None


def test_a_corrupt_portfolio_is_refused(root):
    root.mkdir(parents=True, exist_ok=True)
    Portfolio.path_for(root).write_text("{ not json", encoding="utf-8")
    with pytest.raises(PortfolioError, match="corrupt"):
        Portfolio.load(root)


def test_a_version_mismatch_is_refused(root):
    data = Portfolio.new().as_dict()
    data["portfolio_version"] = "99.0.0"
    with pytest.raises(PortfolioError, match="not compatible"):
        Portfolio.from_dict(data)


def test_a_broken_org_entry_is_skipped_not_fatal(root):
    """A register is the place to record a problem; it must still open."""
    data = Portfolio.new().as_dict()
    data["orgs"] = [{"name": "no-id-here"}, {"id": "org_ok", "name": "OK", "slug": "ok"}]
    loaded = Portfolio.from_dict(data)
    assert [o.name for o in loaded.orgs] == ["OK"]


# ── inspection and roll-up ───────────────────────────────────────────────────


def test_inspect_reports_a_missing_folder_rather_than_raising(root):
    portfolio = Portfolio.new()
    portfolio.add_org(name="Gone", slug="gone", path=root / "does-not-exist")
    report = portfolio.inspect()
    assert report["counts"]["orgs"] == 1
    assert report["counts"]["missing"] == 1
    assert report["orgs"][0]["exists"] is False


def test_inspect_reports_state_presence(root):
    portfolio = Portfolio.new()
    folder = root / "real"
    (folder / ".agent_state").mkdir(parents=True)
    portfolio.add_org(name="Real", slug="real", path=folder)
    row = portfolio.inspect()["orgs"][0]
    assert row["exists"] is True and row["has_state"] is True


def test_rollup_marks_an_org_with_no_live_picture_as_not_loaded():
    """`no figure` and `zero` are different facts; the roll-up must not conflate them."""
    portfolio = Portfolio.new()
    portfolio.add_org(name="A", slug="a")
    portfolio.add_org(name="B", slug="b")
    rollup = portfolio.rollup(per_org=[{"id": "org_b",
                                        "mission": "ship", "spend_usd": 1.5, "running": True}])
    rows = {r["slug"]: r for r in rollup["orgs"]}
    assert rows["b"]["loaded"] is True and rows["b"]["spend_usd"] == 1.5
    assert rows["a"]["loaded"] is False and rows["a"]["spend_usd"] is None
    assert rollup["totals"]["orgs"] == 2
    assert rollup["totals"]["running"] == 1
    assert rollup["totals"]["spend_usd"] == 1.5


def test_rollup_counts_blockers_and_waiting():
    portfolio = Portfolio.new()
    portfolio.add_org(name="A", slug="a")
    portfolio.add_org(name="B", slug="b")
    rollup = portfolio.rollup(per_org=[
        {"id": "org_a", "blocked_nodes": 2},
        {"id": "org_b", "waiting_host": True},
        {"id": "org_a", "blocked_nodes": 2},
    ])
    assert rollup["totals"]["blocked"] >= 1
    assert rollup["totals"]["waiting"] >= 1


# ── the safety property ──────────────────────────────────────────────────────


def test_a_portfolio_has_no_way_to_spend():
    """It is a register. Running and spending live one level down, in an org's goal."""
    portfolio = Portfolio.new()
    for forbidden in ("token_budget", "spend", "arm", "run", "execute"):
        assert not hasattr(portfolio, forbidden), (
            f"a portfolio must not expose {forbidden!r} — it runs nothing")


def test_loading_does_not_start_anything(root):
    """Reading the register must never re-arm work."""
    portfolio = Portfolio.new()
    portfolio.add_org(name="A", slug="a")
    portfolio.save(root)
    loaded = Portfolio.load(root)
    assert loaded is not None
    # Nothing on a loaded portfolio says "running"; that state lives in each org, not the register.
    assert not hasattr(loaded, "running")


# ── org identity (roster integration) ────────────────────────────────────────


def test_default_company_carries_identity():
    org = default_company(provider="ollama", model="qwen2.5-coder:7b", context_window=32768,
                          name="Tesla", org_id="org_tesla", principal_id="pr_owner")
    assert org.name == "Tesla"
    assert org.id == "org_tesla"
    assert org.principal_id == "pr_owner"


def test_org_identity_survives_serialisation():
    org = default_company(provider="ollama", model="qwen2.5-coder:7b", context_window=32768,
                          name="SpaceX", org_id="org_spacex", principal_id="pr_owner")
    restored = Org.from_dict(org.to_dict())
    assert (restored.name, restored.id, restored.principal_id) == ("SpaceX", "org_spacex", "pr_owner")


def test_a_bare_org_has_no_identity_by_default():
    """A hand-built org in a test is not part of any register, so its id is empty, not invented."""
    assert Org().id == "" and Org().principal_id == ""


def test_an_old_roster_file_without_identity_still_loads():
    """A roster written before ids existed must open — identity is additive."""
    org = Org.from_dict({"name": "Legacy", "agents": [], "teams": []})
    assert org.name == "Legacy" and org.id == ""
