#!/usr/bin/env python3
"""Phase 27 tests — the Fleet: several orgs running at once, bounded.

A portfolio registers orgs; an orchestrator runs one. The fleet is the layer between: it turns the
register into live orchestrators, one per org, and lets several work at once — which is the whole
point for a person who runs several companies.

These tests pin the two ceilings that make concurrency safe (a global one and a per-org budget), the
lazy load that keeps a fleet cheap, the isolation that comes from one orchestrator per org, and the
honest refusals a person can act on.
"""

from __future__ import annotations

import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load
from engine.fleet import Fleet, FleetError, OrgRunHandle, OrgRuntime
from engine.library import resolve
from engine.portfolio import Portfolio


#: A stub executor that satisfies the developer contract and parks at the gate, so a run needs no
#: provider. Mirrors the orchestrator suite's own stub.
_STUB = '''CRITERIA = [
    "Every accepted finding addressed with a concrete diff",
    "No finding silently dropped",
    "Open items declared in open questions rather than hidden",
]


def execute_node(node_id, state, ctx):
    if node_id.startswith("human") or node_id.endswith("gate"):
        return {"status": "needs_review", "verdict": "awaiting_owner",
                "summary": "gate reached", "evidence": ["gate"]}
    return {"status": "done", "verdict": "ok", "summary": "did the work",
            "evidence": ["src/app.py#abc"], "criteria_met": list(CRITERIA),
            "artifacts": [{"name": "change", "path": "src/app.py", "sha": "abc123",
                           "type": "change"}]}
'''


@pytest.fixture(scope="module")
def config():
    return load()


@pytest.fixture(scope="module")
def library():
    return resolve()


@pytest.fixture
def portfolio(tmp_path):
    p = Portfolio.new(principal_name="Elon")
    p.add_org(name="Tesla", slug="tesla", path=tmp_path / "tesla", charter="EVs")
    p.add_org(name="SpaceX", slug="spacex", path=tmp_path / "spacex", charter="Space")
    p.add_org(name="xAI", slug="xai", path=tmp_path / "xai", charter="AI")
    return p


def _fleet(config, library, portfolio, *, ceiling=2):
    return Fleet(config=config, library=library, portfolio=portfolio, max_concurrent_orgs=ceiling)


def _stub_path(portfolio, slug="tesla"):
    """Write the stub executor into an org's folder and return its path."""
    entry = portfolio.org(slug)
    entry.workspace_path.mkdir(parents=True, exist_ok=True)
    path = entry.workspace_path / "stub.py"
    path.write_text(_STUB)
    return str(path)


# ── lazy loading and isolation ───────────────────────────────────────────────


def test_a_new_fleet_has_loaded_nothing(config, library, portfolio):
    """Building a fleet must not read every roster — it happens on first use."""
    fleet = _fleet(config, library, portfolio)
    assert fleet.status()["running"] == 0
    assert all(not row["loaded"] for row in fleet.status()["orgs"])
    assert fleet.runtime("tesla") is None


def test_loading_an_org_builds_its_own_orchestrator(config, library, portfolio):
    fleet = _fleet(config, library, portfolio)
    runtime = fleet._runtime_for("tesla")
    assert runtime.loaded
    # The org carried its identity, so the register can find it by id, not by display name.
    org = runtime.orchestrator.org
    assert org.id == "org_tesla"
    assert org.principal_id == portfolio.principal.id
    assert org.name == "Tesla"


def test_each_org_gets_its_own_roster(config, library, portfolio):
    """Isolation is by construction: one orchestrator per org, so A's agents never see B's."""
    fleet = _fleet(config, library, portfolio)
    tesla = fleet._runtime_for("tesla").orchestrator
    spacex = fleet._runtime_for("spacex").orchestrator
    assert tesla.org is not spacex.org
    assert tesla.workspace.path != spacex.workspace.path
    # A hire in one org must not appear in the other.
    from engine.people import HireRequest, People

    people = People(library=library, config=config, catalog=None,
                    project=tesla.workspace.path)
    people.hire(HireRequest(name="OnlyInTesla", skill="code-reviewer", provider="ollama",
                            model="qwen2.5-coder:7b", context_window=32768),
                org=tesla.org, roster_root=None, save=False)
    assert any(a.name == "OnlyInTesla" for a in tesla.org.agents.values())
    assert not any(a.name == "OnlyInTesla" for a in spacex.org.agents.values())


def test_an_org_identity_is_stable_across_a_reload(config, library, portfolio):
    fleet = _fleet(config, library, portfolio)
    first = fleet._runtime_for("tesla").orchestrator.org.id
    fleet.forget_org("tesla")
    second = fleet._runtime_for("tesla").orchestrator.org.id
    assert first == second == "org_tesla"


# ── status and roll-up ───────────────────────────────────────────────────────


def test_status_reports_every_registered_org(config, library, portfolio):
    fleet = _fleet(config, library, portfolio)
    fleet._runtime_for("tesla")
    status = fleet.status()
    assert len(status["orgs"]) == 3
    loaded = {row["slug"]: row["loaded"] for row in status["orgs"]}
    assert loaded == {"tesla": True, "spacex": False, "xai": False}
    assert status["principal"]["name"] == "Elon"
    assert status["max_concurrent_orgs"] == 2


def test_rollup_counts_loaded_orgs_and_reports_the_rest_as_not_loaded(config, library, portfolio):
    fleet = _fleet(config, library, portfolio)
    fleet._runtime_for("tesla")
    rollup = fleet.rollup()
    rows = {r["slug"]: r for r in rollup["orgs"]}
    assert rows["tesla"]["loaded"] is True
    assert rows["spacex"]["loaded"] is False
    assert rows["spacex"]["spend_usd"] is None, "an unloaded org has no figure, not zero"


def test_org_spend_reads_the_cost_ledger_not_the_decision_ledger(config, library, portfolio):
    """The figure comes from the ledger the run spent against, not the one that records decisions.

    `orch.ledger` is the *decision* gate ledger: it has no `snapshot()` and no cost in it. Reading it
    raised, the exception branch returned None, and every org's spend was silently unknown.
    """
    fleet = _fleet(config, library, portfolio)
    orch = fleet._runtime_for("tesla").orchestrator
    orch.cost_snapshot = lambda: {  # type: ignore[assignment]
        "total_usd": 1.25, "total_tokens": 4200, "calls": 7,
        "unknown_cost_calls": 0, "cost_complete": True}
    assert fleet._org_spend(orch) == 1.25


def test_an_unreported_cost_is_unknown_not_zero(config, library, portfolio):
    """A total that is really a floor must not be presented as a figure."""
    fleet = _fleet(config, library, portfolio)
    orch = fleet._runtime_for("tesla").orchestrator
    orch.cost_snapshot = lambda: {  # type: ignore[assignment]
        "total_usd": 0.0, "total_tokens": 0, "calls": 1,
        "unknown_cost_calls": 1, "cost_complete": False}
    assert fleet._org_spend(orch) is None


def test_a_genuine_zero_is_reported_as_zero(config, library, portfolio):
    """A run that made no billable call spent nothing — that is known, not unknown."""
    fleet = _fleet(config, library, portfolio)
    orch = fleet._runtime_for("tesla").orchestrator
    orch.cost_snapshot = lambda: {  # type: ignore[assignment]
        "total_usd": 0.0, "total_tokens": 0, "calls": 0,
        "unknown_cost_calls": 0, "cost_complete": True}
    assert fleet._org_spend(orch) == 0.0


# ── refusals ─────────────────────────────────────────────────────────────────


def test_a_disabled_org_is_refused(config, library, portfolio):
    portfolio.update_org("spacex", enabled=False)
    fleet = _fleet(config, library, portfolio)
    with pytest.raises(FleetError, match="disabled"):
        fleet.run_org("spacex", goal="x")


def test_an_unknown_org_is_refused(config, library, portfolio):
    fleet = _fleet(config, library, portfolio)
    with pytest.raises(Exception, match="no org"):
        fleet.run_org("nope", goal="x")


def test_stopping_an_unloaded_org_is_refused(config, library, portfolio):
    fleet = _fleet(config, library, portfolio)
    with pytest.raises(FleetError, match="not loaded"):
        fleet.stop_org("tesla")


# ── running, and the two ceilings ────────────────────────────────────────────


def test_a_run_is_driven_by_the_stub_and_reaches_the_gate(config, library, portfolio):
    fleet = _fleet(config, library, portfolio, ceiling=1)
    stub = _stub_path(portfolio, "tesla")
    handle = fleet.run_org("tesla", goal="add cursor pagination", background=False, executor=stub)
    assert handle.finished
    assert handle.error == ""
    # The run parked at the gate — a normal pause, not a failure.
    row = next(r for r in fleet.status()["orgs"] if r["slug"] == "tesla")
    assert row["phase"] in ("awaiting_gate", "awaiting_human", "done")
    assert row["running"] is False


def test_the_global_ceiling_refuses_a_run_beyond_it(config, library, portfolio):
    """The orgs share one machine; without a cap, N orgs each start their own swarm."""
    fleet = _fleet(config, library, portfolio, ceiling=1)
    stub = _stub_path(portfolio, "tesla")
    stub2 = _stub_path(portfolio, "spacex")
    first = fleet.run_org("tesla", goal="work one", background=True, executor=stub)
    # Hold the first run "in flight" deterministically by checking the counter the ceiling reads.
    fleet.runtime("tesla").handle = first
    try:
        fleet.run_org("spacex", goal="work two", background=True, executor=stub2)
        refused = False
    except FleetError as exc:
        refused = "concurrency ceiling" in str(exc)
    fleet.wait(timeout=30)
    assert refused, "a second concurrent run must be refused at ceiling 1"


def test_a_duplicate_run_in_one_org_is_refused(config, library, portfolio):
    fleet = _fleet(config, library, portfolio, ceiling=3)
    stub = _stub_path(portfolio, "tesla")
    fleet.run_org("tesla", goal="work", background=True, executor=stub)
    try:
        fleet.run_org("tesla", goal="again", background=True, executor=stub)
        refused = False
    except FleetError as exc:
        refused = "already has a run" in str(exc)
    fleet.wait(timeout=30)
    assert refused


def test_the_per_org_budget_refuses_a_spent_org(config, library, portfolio):
    """One runaway org must not consume the principal's whole allowance before the others run."""
    portfolio.update_org("tesla", daily_budget_usd=0.01)
    fleet = _fleet(config, library, portfolio, ceiling=2)
    runtime = fleet._runtime_for("tesla")
    # A cost ledger that says the org has spent more than its ceiling. Patched on the accessor the
    # fleet actually reads — the *cost* ledger the run kept, not the orchestrator's decision ledger.
    runtime.orchestrator.cost_snapshot = lambda: {  # type: ignore[assignment]
        "total_usd": 5.0, "total_tokens": 10, "calls": 1,
        "unknown_cost_calls": 0, "cost_complete": True}
    with pytest.raises(FleetError, match="daily budget"):
        fleet.run_org("tesla", goal="x")


def test_two_orgs_run_on_their_own_threads(config, library, portfolio):
    fleet = _fleet(config, library, portfolio, ceiling=2)
    stub = _stub_path(portfolio, "tesla")
    stub2 = _stub_path(portfolio, "spacex")
    first = fleet.run_org("tesla", goal="one", background=True, executor=stub)
    second = fleet.run_org("spacex", goal="two", background=True, executor=stub2)
    assert first.thread is not None and second.thread is not None
    assert first.thread is not second.thread
    assert first.org_id != second.org_id
    assert fleet.wait(timeout=30)


def test_stopping_an_org_asks_it_to_pause(config, library, portfolio):
    fleet = _fleet(config, library, portfolio, ceiling=1)
    fleet._runtime_for("tesla")
    detail = fleet.stop_org("tesla")
    assert detail["org_id"] == "org_tesla"


# ── the safety property ──────────────────────────────────────────────────────


def test_the_fleet_never_spends_on_its_own(config, library, portfolio):
    """A fleet runs work a goal already authorised; it has no way to arm or grant budget itself."""
    fleet = _fleet(config, library, portfolio)
    for forbidden in ("arm", "goal_set", "mission_set", "grant_budget"):
        assert not hasattr(fleet, forbidden), (
            f"a fleet must not expose {forbidden!r} — running work is not the same as authorising it")


def test_an_event_observer_is_called_and_never_breaks_the_fleet(config, library, portfolio):
    seen: list[str] = []

    def observer(kind, payload):
        seen.append(kind)
        raise RuntimeError("an observer must not break the fleet")

    fleet = Fleet(config=config, library=library, portfolio=portfolio,
                  max_concurrent_orgs=1, on_event=observer)
    stub = _stub_path(portfolio, "tesla")
    fleet.run_org("tesla", goal="work", background=False, executor=stub)
    assert "fleet.org.started" in seen and "fleet.org.finished" in seen


# ── the run carries its org identity ─────────────────────────────────────────


def test_a_run_records_which_org_it_belongs_to(config, library, portfolio):
    fleet = _fleet(config, library, portfolio, ceiling=1)
    stub = _stub_path(portfolio, "tesla")
    handle = fleet.run_org("tesla", goal="add pagination", background=False, executor=stub)
    run = fleet.runtime("tesla").orchestrator.load(portfolio.org("tesla").slug)
    assert run is not None
    assert run.org_id == "org_tesla"
    assert run.principal_id == portfolio.principal.id


def test_the_run_identity_survives_the_checkpoint(config, library, portfolio):
    from engine.orchestrator import Run

    fleet = _fleet(config, library, portfolio, ceiling=1)
    stub = _stub_path(portfolio, "tesla")
    fleet.run_org("tesla", goal="work", background=False, executor=stub)
    runtime = fleet.runtime("tesla")
    run = runtime.orchestrator.load(portfolio.org("tesla").slug)
    restored = Run.from_dict(run.as_dict(), workspace=run.workspace)
    assert restored.org_id == "org_tesla"
    assert restored.principal_id == portfolio.principal.id
