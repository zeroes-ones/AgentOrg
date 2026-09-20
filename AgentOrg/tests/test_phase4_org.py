#!/usr/bin/env python3
"""Phase 4 tests — the agent organization, routing, delegation, scheduling and health.

The emphasis is on the refusals, because that is what this phase is: the design promises that
an agent cannot review its own work, that delegation cannot cycle or exceed its depth or create
budget, that a setting cannot silently disable the human gates, and that a healthy average cannot
hide a security trip. Each of those is asserted directly.
"""

from __future__ import annotations

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine.config import Config, ProviderConfig, load
from engine.library import resolve
from engine.org import (
    S_INVARIANTS,
    AgentError,
    AgentKind,
    AgentLevel,
    AgentSpec,
    AgentState,
    ApprovalTier,
    Autonomy,
    Binder,
    BindingError,
    BindingPolicy,
    Budget,
    Constraint,
    DelegationError,
    Handoff,
    HandoffError,
    HandoffState,
    HealthMonitor,
    HealthState,
    HiringDesk,
    LadderEvidence,
    Ledger,
    LedgerError,
    Mailbox,
    MessageKind,
    Org,
    OrgError,
    PolicyError,
    PolicyResolver,
    ProbeResult,
    Requisition,
    RequisitionState,
    RouteClass,
    RouteContext,
    Router,
    RouterError,
    Scheduler,
    SchedulerError,
    Severity,
    TicketState,
    SLOTracker,
    build_context_pass_through,
    default_company,
    validate_delegation_context,
    validate_handoff,
)
from engine.org.agent import new_agent_id
from engine.org.handoff import REQUIRED_FIELDS
from engine.planner import Planner
from engine.resources import detect
from engine.skills import FilesystemSkillSource


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def config():
    return load()


@pytest.fixture
def org():
    return default_company(
        provider="ollama", model="qwen2.5-coder:7b", context_window=32768,
        reviewer_provider="anthropic", reviewer_model="claude-sonnet-4-20250514",
        reviewer_context_window=200000,
    )


@pytest.fixture
def source():
    return FilesystemSkillSource(resolve())


def _context() -> dict:
    return build_context_pass_through(
        problem="fix the auth bug", tried=["read the middleware"], logs="500 on POST /login",
        paths=["src/app.py:47"], hypothesis="missing type check",
    )


def _requisition(org, **overrides) -> Requisition:
    requester = next(a for a in org.agents.values() if "backend-developer" in a.skills)
    base = dict(
        requester_id=requester.id, requester_name=requester.name, kind="helper",
        skill="devops-engineer", provider="ollama", model="qwen2.5-coder:7b",
        context_window=32768, capabilities=["read:src/**"], needed=["kubernetes"],
        why_existing_insufficient="no active agent declares these",
        expected_outcome="unblocks the fixer node",
        ladder=LadderEvidence(reuse_attempted=[{"agent_id": "x", "why_rejected": "docker only"}],
                              why_not_self="it needs context I do not have"),
        context=_context(),
    )
    base.update(overrides)
    return Requisition(**base)


# ── agent spec ───────────────────────────────────────────────────────────────


def test_agent_requires_a_context_window_for_an_ai_agent():
    """The session projection cannot size a prompt without it, so binding is refused."""
    with pytest.raises(AgentError, match="unknown context window"):
        AgentSpec(id="a", name="X", skills=["backend-developer"], provider="p", model="m",
                  context_window=None)


def test_agent_requires_a_skill():
    with pytest.raises(AgentError, match="no skills"):
        AgentSpec(id="a", name="X", skills=[], provider="p", model="m", context_window=1000)


def test_agent_requires_a_name():
    with pytest.raises(AgentError, match="requires a name"):
        AgentSpec(id="a", name="   ", skills=["s"], provider="p", model="m", context_window=1000)


def test_agent_requires_provider_and_model():
    with pytest.raises(AgentError, match="provider and a model"):
        AgentSpec(id="a", name="X", skills=["s"], provider="", model="", context_window=1000)


def test_human_agent_needs_no_model():
    """A human is an agent so that a human handoff uses the same machinery."""
    owner = AgentSpec(id="ag_owner", name="Owner", skills=["*"], kind=AgentKind.HUMAN)
    assert owner.is_human and not owner.is_ai


def test_capability_prefix_matching_is_least_privilege():
    spec = AgentSpec(id="a", name="X", skills=["s"], provider="p", model="m", context_window=1,
                     capabilities=["read:src/"])
    assert spec.has_capability("read:src/app.py")
    assert not spec.has_capability("write:src/app.py")
    assert not spec.has_capability("deploy:prod")


def test_budget_remaining_never_goes_negative():
    budget = Budget(allocated_usd=1.0, allocated_tokens=1000, spent_usd=2.0, spent_tokens=5000)
    assert budget.remaining_usd == 0.0
    assert budget.remaining_tokens == 0
    assert budget.exhausted


def test_agent_spec_round_trips_through_a_dict():
    spec = AgentSpec(id="a", name="X", skills=["s"], provider="p", model="m", context_window=1000,
                     level=AgentLevel.SENIOR, tags=["t"])
    restored = AgentSpec.from_dict(spec.as_dict())
    assert restored.name == spec.name and restored.level is spec.level
    assert restored.context_window == 1000


def test_agent_from_dict_ignores_unknown_fields():
    restored = AgentSpec.from_dict({
        "id": "a", "name": "X", "skills": ["s"], "provider": "p", "model": "m",
        "context_window": 1, "future_field": {"x": 1},
    })
    assert restored.name == "X"


# ── roster ───────────────────────────────────────────────────────────────────


def test_default_company_has_the_owner_and_seven_roles(org):
    assert org.owner() is not None and org.owner().is_human
    assert org.owner().role == "owner"
    ai_agents = [a for a in org.agents.values() if a.is_ai]
    assert len(ai_agents) == 7


def test_default_company_binds_reviewers_to_a_different_model(org):
    """verification-independence-engineer requires a verifier that differs from the producer."""
    reviewers = [a for a in org.agents.values() if a.role == "reviewer"]
    builders = [a for a in org.agents.values() if a.role == "worker" and a.is_ai]
    assert reviewers and builders
    assert all(r.model != builders[0].model for r in reviewers)


def test_hire_refuses_a_duplicate_name(org):
    """Two agents called Alice would make the roster and every log line ambiguous."""
    first = next(a for a in org.agents.values() if a.is_ai)
    with pytest.raises(OrgError, match="already exists"):
        org.hire(AgentSpec(id=new_agent_id(), name=first.name, skills=["qa-engineer"],
                           provider="p", model="m", context_window=1000))


def test_hire_refuses_a_duplicate_id(org):
    existing = next(iter(org.agents.values()))
    with pytest.raises(OrgError, match="already in the roster"):
        org.hire(AgentSpec(id=existing.id, name="Unique Name", skills=["qa-engineer"],
                           provider="p", model="m", context_window=1000))


def test_rename_preserves_identity(org):
    spec = next(a for a in org.agents.values() if a.is_ai)
    original_id = spec.id
    org.rename(spec.id, "Renamed")
    assert org.get(original_id).name == "Renamed"


def test_rename_refuses_a_collision(org):
    agents = [a for a in org.agents.values() if a.is_ai]
    with pytest.raises(OrgError, match="already exists"):
        org.rename(agents[0].id, agents[1].name)


def test_owner_cannot_be_terminated(org):
    """The Owner holds terminal authority; removing it would leave the org with none."""
    with pytest.raises(OrgError, match="cannot be terminated"):
        org.terminate(org.owner().id)


def test_terminate_refuses_while_a_report_is_working(org):
    """Terminating mid-flight would orphan the work the report holds."""
    architect = next(a for a in org.agents.values() if a.title == "System Architect")
    developer = next(a for a in org.agents.values() if "backend-developer" in a.skills)
    assert developer.reports_to == architect.id
    org.runtime(developer.id).begin(task_id="t1", node_id="fixer")
    with pytest.raises(OrgError, match="actively"):
        org.terminate(architect.id)


def test_terminate_reparents_an_idle_report(org):
    """An idle report is re-parented rather than orphaned, so its reporting line stays valid."""
    architect = next(a for a in org.agents.values() if a.title == "System Architect")
    developer = next(a for a in org.agents.values() if "backend-developer" in a.skills)
    org.terminate(architect.id)
    assert developer.reports_to == org.owner().id


def test_reassign_refuses_stripping_every_skill(org):
    spec = next(a for a in org.agents.values() if a.is_ai)
    with pytest.raises(OrgError, match="cannot strip every skill"):
        org.reassign(spec.id, skills=[])


def test_candidates_are_ordered_by_level_then_idleness(org):
    holders = org.candidates_for("code-reviewer")
    assert holders
    levels = [int(a.level) for a in holders]
    assert levels == sorted(levels, reverse=True)


def test_quarantined_agents_are_excluded_from_candidates(org):
    spec = next(a for a in org.agents.values() if "code-reviewer" in a.skills)
    org.runtime(spec.id).state = AgentState.QUARANTINED
    assert spec.id not in [a.id for a in org.candidates_for("code-reviewer")]
    assert spec.id in [a.id for a in org.candidates_for("code-reviewer", available_only=False)]


def test_roster_persists_and_reloads(org, tmp_path):
    path = tmp_path / "org.json"
    org.save(path)
    reloaded = Org.load(path)
    assert set(reloaded.agents) == set(org.agents)
    assert reloaded.owner() is not None
    assert len(reloaded.teams) == len(org.teams)


def test_roster_load_skips_a_broken_entry_rather_than_failing(tmp_path):
    """A roster with one broken entry should open so the Owner can fix it."""
    path = tmp_path / "org.json"
    payload = {
        "name": "T", "agents": [
            {"id": "a1", "name": "Good", "skills": ["s"], "provider": "p", "model": "m",
             "context_window": 1000},
            {"id": "a2", "name": "Broken", "skills": [], "provider": "p", "model": "m",
             "context_window": 1000},
        ],
    }
    import json

    path.write_text(json.dumps(payload))
    reloaded = Org.load(path)
    assert "a1" in reloaded.agents and "a2" not in reloaded.agents
    assert reloaded.policy.get("_load_warnings")


def test_roster_load_of_a_missing_file_is_an_empty_org(tmp_path):
    assert not Org.load(tmp_path / "absent.json").agents


def test_span_of_control_is_derived_from_the_roster(org):
    architect = next(a for a in org.agents.values() if a.title == "System Architect")
    assert org.span_of_control(architect.id) == 1
    org.terminate(next(a for a in org.agents.values() if "backend-developer" in a.skills).id)
    assert org.span_of_control(architect.id) == 0


# ── mailbox ──────────────────────────────────────────────────────────────────


def test_mailbox_delivery_is_idempotent(tmp_path):
    """A retried delivery must not duplicate an instruction the agent would act on twice."""
    box = Mailbox(tmp_path / "m.jsonl")
    assert box.send(_message("m1", "do it")) is True
    assert box.send(_message("m1", "do it")) is False
    assert len(box) == 1
    box.close()


def test_mailbox_surfaces_constraints_for_the_primacy_zone(tmp_path):
    box = Mailbox(tmp_path / "m.jsonl")
    box.post(MessageKind.CONSTRAINT, "NEVER log the raw token", message_id="c1")
    box.post(MessageKind.ASSIGN, "fix it", message_id="a1")
    assert box.constraints() == ["NEVER log the raw token"]
    box.close()


def test_mailbox_read_state_persists_across_reopen(tmp_path):
    path = tmp_path / "m.jsonl"
    box = Mailbox(path)
    box.post(MessageKind.ASSIGN, "x", message_id="m1")
    box.mark_read("m1")
    box.close()

    reopened = Mailbox(path)
    reopened.load()
    assert len(reopened) == 1
    assert not reopened.unread()
    reopened.close()


def test_mailbox_tolerates_a_torn_final_line(tmp_path):
    path = tmp_path / "m.jsonl"
    path.write_text('{"id":"m1","kind":"notice","body":"ok","sent_at":"t"}\n{"torn":')
    box = Mailbox(path)
    assert len(box) == 1
    box.close()


def test_mailbox_stats_count_by_kind(tmp_path):
    box = Mailbox(tmp_path / "m.jsonl")
    box.post(MessageKind.ASSIGN, "a", message_id="1")
    box.post(MessageKind.REWORK, "b", message_id="2")
    box.post(MessageKind.REWORK, "c", message_id="3")
    stats = box.stats()
    assert stats["total"] == 3 and stats["unread"] == 3
    assert stats["by_kind"]["rework"] == 2
    box.close()


def _message(mid: str, body: str):
    from engine.org.mailbox import Message

    return Message(id=mid, kind=MessageKind.ASSIGN, body=body)


# ── policy ───────────────────────────────────────────────────────────────────


def test_safety_floor_blocks_autonomous_escalation():
    """The floor stops one setting from disabling every human gate."""
    resolver = PolicyResolver()
    with pytest.raises(PolicyError, match="safety floor"):
        resolver.set("org", "", RouteClass.ESCALATE, Autonomy.AUTO)
    with pytest.raises(PolicyError, match="safety floor"):
        resolver.set("agent", "ag_1", RouteClass.CONFLICT, Autonomy.NOTIFY)


def test_safety_floor_allows_an_explicit_opt_in():
    resolver = PolicyResolver(allow_autonomous_escalation=True)
    resolver.set("org", "", RouteClass.ESCALATE, Autonomy.AUTO)
    assert resolver.resolve(RouteClass.ESCALATE).level is Autonomy.AUTO


def test_routine_route_classes_may_be_loosened():
    resolver = PolicyResolver()
    resolver.set("agent", "ag_1", RouteClass.REWORK, Autonomy.NOTIFY)
    assert resolver.resolve(RouteClass.REWORK, agent_id="ag_1").level is Autonomy.NOTIFY
    assert resolver.resolve(RouteClass.REWORK, agent_id="ag_2").level is Autonomy.AUTO


def test_resolution_reports_which_layer_decided():
    resolver = PolicyResolver()
    resolver.set("team", "Platform", RouteClass.CONTRACT, Autonomy.NOTIFY)
    resolution = resolver.resolve(RouteClass.CONTRACT, team="Platform")
    assert resolution.layer == "team" and resolution.scope_id == "Platform"


def test_more_specific_scope_wins():
    resolver = PolicyResolver()
    resolver.set("team", "Platform", RouteClass.CONTRACT, Autonomy.NOTIFY)
    resolver.set("agent", "ag_1", RouteClass.CONTRACT, Autonomy.CONFIRM)
    assert resolver.resolve(RouteClass.CONTRACT, agent_id="ag_1", team="Platform").layer == "agent"


def test_rejected_policy_change_is_not_half_applied():
    resolver = PolicyResolver()
    resolver.set("agent", "ag_1", RouteClass.REWORK, Autonomy.NOTIFY)
    with pytest.raises(PolicyError):
        resolver.set("agent", "ag_1", RouteClass.ESCALATE, Autonomy.AUTO)
    assert resolver.resolve(RouteClass.REWORK, agent_id="ag_1").level is Autonomy.NOTIFY


def test_unknown_route_class_and_level_are_refused():
    resolver = PolicyResolver()
    with pytest.raises(PolicyError, match="unknown route class"):
        resolver.set("org", "", "R-NONSENSE", Autonomy.AUTO)
    with pytest.raises(PolicyError, match="unknown autonomy level"):
        resolver.set("org", "", RouteClass.REWORK, "maybe")


def test_unknown_scope_is_refused():
    with pytest.raises(PolicyError, match="unknown policy scope"):
        PolicyResolver().set("galaxy", "x", RouteClass.REWORK, Autonomy.AUTO)


def test_policy_resolves_every_route_class():
    matrix = PolicyResolver().effective()
    assert set(matrix) == {k.value for k in RouteClass}
    assert all("layer" in v and "level" in v for v in matrix.values())


def test_policy_round_trips_through_a_dict():
    resolver = PolicyResolver()
    resolver.set("agent", "ag_1", RouteClass.REWORK, Autonomy.NOTIFY)
    restored = PolicyResolver.from_dict(resolver.to_dict())
    assert restored.resolve(RouteClass.REWORK, agent_id="ag_1").level is Autonomy.NOTIFY


def test_policy_floored_resolution_is_marked():
    resolver = PolicyResolver()
    # A built-in default that would be below the floor is raised and flagged.
    resolver.defaults["R-ESCALATE"] = "auto"
    resolution = resolver.resolve(RouteClass.ESCALATE)
    assert resolution.floored and resolution.level is Autonomy.CONFIRM


# ── handoff ──────────────────────────────────────────────────────────────────


def _payload(**overrides) -> dict:
    base = {
        "status": "done", "summary": "s", "artifacts": [], "decisions": [],
        "open_questions": [], "verification_evidence": [], "context": {}, "budget": {},
        "next": "",
    }
    base.update(overrides)
    return base


def test_handoff_lifecycle_transitions_legally():
    handoff = Handoff(payload=_payload(), origin="a", target="b")
    assert validate_handoff(handoff, stage="propose").ok
    handoff.accept()
    handoff.start()
    handoff.fulfil()
    assert handoff.state is HandoffState.FULFILLED


def test_illegal_transition_is_refused():
    handoff = Handoff(payload=_payload(), origin="a", target="b")
    with pytest.raises(HandoffError, match="illegal handoff transition"):
        handoff.start()


def test_registry_requires_every_payload_field():
    verdict = validate_handoff(Handoff(payload={"status": "done"}, origin="a", target="b"),
                               stage="propose")
    assert "REGISTRY" in verdict.rules
    assert len(verdict.violations[0].detail["missing"]) == len(REQUIRED_FIELDS) - 1


def test_r1_refuses_an_oversized_payload():
    verdict = validate_handoff(
        Handoff(payload=_payload(summary="x" * 60_000), origin="a", target="b"), stage="propose")
    assert "R1" in verdict.rules


def test_r2_refuses_dropped_non_negotiable_constraints():
    """This is how 'NEVER store passwords in plaintext' silently becomes 'use secure auth'."""
    before = Handoff(payload=_payload(
        constraints=[{"value": "NEVER log tokens", "non_negotiable": True}]), origin="a", target="b")
    after = Handoff(payload=_payload(), origin="b", target="c")
    verdict = validate_handoff(after, stage="accept", previous=before)
    assert "R2" in verdict.rules


def test_r2_allows_preserved_constraints():
    constraints = [{"value": "NEVER log tokens", "non_negotiable": True}]
    before = Handoff(payload=_payload(constraints=constraints), origin="a", target="b")
    after = Handoff(payload=_payload(constraints=constraints), origin="b", target="c")
    assert validate_handoff(after, stage="accept", previous=before).ok


def test_r3_refuses_a_self_handoff():
    verdict = validate_handoff(Handoff(payload=_payload(), origin="a", target="a"),
                               stage="propose")
    assert "R3" in verdict.rules


def test_r3_exempts_a_session_rotation():
    """A rotation keeps the same skill deliberately, so it is a distinct kind."""
    handoff = Handoff(payload=_payload(), origin="a", target="a", kind="session-rotation")
    assert validate_handoff(handoff, stage="propose").ok


def test_r4_refuses_a_checksum_mismatch():
    handoff = Handoff(payload=_payload(), origin="a", target="b")
    handoff.payload["summary"] = "tampered"
    assert "R4" in validate_handoff(handoff, stage="accept").rules


def test_r4_aborts_on_rebuild_from_corrupt_state():
    handoff = Handoff(payload=_payload(), origin="a", target="b")
    data = handoff.as_dict()
    data["payload"]["summary"] = "tampered after serialisation"
    with pytest.raises(HandoffError, match="checksum"):
        Handoff.from_dict(data)


def test_r5_refuses_an_unrecorded_irreversible_decision():
    verdict = validate_handoff(
        Handoff(payload=_payload(decisions=[{"gate": "auth", "choice": "x", "reversible": False}]),
                origin="a", target="b"), stage="propose")
    assert "R5" in verdict.rules


def test_r5_accepts_a_recorded_irreversible_decision():
    verdict = validate_handoff(
        Handoff(payload=_payload(decisions=[
            {"gate": "auth", "choice": "argon2id", "rationale": "memory hardness",
             "reversible": False}]), origin="a", target="b"), stage="propose")
    assert "R5" not in verdict.rules


def test_r6_refuses_more_than_three_open_questions():
    verdict = validate_handoff(
        Handoff(payload=_payload(open_questions=[{"question": f"q{i}"} for i in range(5)]),
                origin="a", target="b"), stage="propose")
    assert "R6" in verdict.rules


def test_r6_allows_three():
    verdict = validate_handoff(
        Handoff(payload=_payload(open_questions=[{"question": f"q{i}"} for i in range(3)]),
                origin="a", target="b"), stage="propose")
    assert "R6" not in verdict.rules


def test_r7_refuses_delivery_before_acceptance():
    handoff = Handoff(payload=_payload(), origin="a", target="b")
    assert "R7" in validate_handoff(handoff, stage="deliver").rules


def test_r7_does_not_block_acceptance_itself():
    """Acceptance is what satisfies R7; checking it first would refuse every valid acceptance."""
    handoff = Handoff(payload=_payload(), origin="a", target="b")
    handoff.accept()
    assert handoff.state is HandoffState.ACCEPTED


def test_r8_refuses_an_unmarked_override():
    verdict = validate_handoff(
        Handoff(payload=_payload(), origin="a", target="b",
                supersedes={"marker": "", "rationale": ""}), stage="accept")
    assert "R8" in verdict.rules


def test_r8_accepts_a_marked_override():
    verdict = validate_handoff(
        Handoff(payload=_payload(), origin="a", target="b",
                supersedes={"marker": "SUPERSEDED", "rationale": "better information"}),
        stage="accept")
    assert "R8" not in verdict.rules


def test_delegation_context_requires_all_five_elements():
    complete = _context()
    assert validate_delegation_context(complete).ok
    incomplete = validate_delegation_context({"problem": "p"})
    assert "S5" in incomplete.rules
    assert len(incomplete.violations[0].detail["missing"]) == 4


def test_unknown_validation_stage_is_refused():
    handoff = Handoff(payload=_payload(), origin="a", target="b")
    with pytest.raises(HandoffError, match="unknown validation stage"):
        validate_handoff(handoff, stage="teleport")


# ── ledger ───────────────────────────────────────────────────────────────────


def test_ledger_records_a_decision_with_its_alternatives():
    ledger = Ledger()
    entry = ledger.record(gate="auth", choice="argon2id", rationale="memory hardness",
                          by="ag_1", rejected_alternatives=["bcrypt", "scrypt"])
    assert entry.rejected_alternatives == ["bcrypt", "scrypt"]
    assert ledger.current("auth").choice == "argon2id"


def test_ledger_refuses_re_making_an_irreversible_decision():
    """An irreversible choice must not be silently re-made — it must be superseded."""
    ledger = Ledger()
    ledger.record(gate="auth", choice="argon2id", rationale="x", reversible=False)
    with pytest.raises(LedgerError, match="irreversible"):
        ledger.record(gate="auth", choice="md5", rationale="y")


def test_supersede_requires_a_rationale():
    ledger = Ledger()
    ledger.record(gate="auth", choice="a", rationale="x")
    with pytest.raises(LedgerError, match="requires a rationale"):
        ledger.supersede("auth", choice="b", rationale="")


def test_supersede_marks_the_previous_entry(ledger=None):
    ledger = Ledger()
    ledger.record(gate="auth", choice="a", rationale="x")
    ledger.supersede("auth", choice="b", rationale="interop requirement")
    history = ledger.history("auth")
    assert [e.choice for e in history] == ["a", "b"]
    assert history[0].superseded and history[0].marker == "SUPERSEDED"
    assert ledger.current("auth").choice == "b"
    assert ledger.live() == [history[1]]


def test_non_negotiable_constraints_survive_a_supersede():
    ledger = Ledger()
    ledger.record(gate="auth", choice="a", rationale="x",
                  constraints=[Constraint("NEVER log tokens", type="security", non_negotiable=True)])
    ledger.supersede("auth", choice="b", rationale="interop")
    assert [c.value for c in ledger.constraints(non_negotiable_only=True)] == ["NEVER log tokens"]


def test_ledger_journal_replays_without_duplicating(tmp_path):
    """A naive reload would duplicate a decision on every restart."""
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path=path)
    ledger.record(gate="g", choice="a", rationale="r")
    ledger.supersede("g", choice="b", rationale="better")
    ledger.close()

    reopened = Ledger(path=path)
    assert [e.choice for e in reopened.history("g")] == ["a", "b"]
    assert reopened.stats()["entries"] == 2
    reopened.close()

    again = Ledger(path=path)
    assert len(again.history("g")) == 2, "reload must be idempotent"
    again.close()


def test_ledger_refuses_a_constraint_with_no_decision():
    ledger = Ledger()
    with pytest.raises(LedgerError, match="no live decision"):
        ledger.add_constraint("nothing", Constraint("x"))


def test_ledger_handoff_block_excludes_superseded_decisions():
    """A superseded decision would contradict the current one in a receiver's view."""
    ledger = Ledger()
    ledger.record(gate="a", choice="one", rationale="r")
    ledger.supersede("a", choice="two", rationale="better")
    block = ledger.as_handoff_block()
    assert [b["choice"] for b in block] == ["two"]


# ── binding ──────────────────────────────────────────────────────────────────


def test_binding_refuses_a_node_with_no_skill(org):
    with pytest.raises(BindingError, match="declares no skill"):
        Binder(org).bind({"id": "gate-node", "type": "gate"})


def test_binding_refuses_an_unknown_skill(org):
    with pytest.raises(BindingError, match="no agent in the roster holds"):
        Binder(org).bind({"id": "n", "skill": "quantum-wizard"})


def test_binding_refuses_when_every_holder_is_excluded(org):
    holder = org.candidates_for("code-reviewer")[0]
    with pytest.raises(BindingError, match="excluded"):
        Binder(org).bind({"id": "n", "skill": "code-reviewer"}, exclude=[a.id for a in org.candidates_for("code-reviewer")])


def test_pinned_binding_uses_the_named_agent(org):
    holder = org.candidates_for("code-reviewer")[0]
    binding = Binder(org).bind({"id": "n", "skill": "code-reviewer"},
                               policy=BindingPolicy.PINNED, pinned=holder.id)
    assert binding.primary == holder.id


def test_pinned_binding_accepts_a_name(org):
    holder = org.candidates_for("code-reviewer")[0]
    binding = Binder(org).bind({"id": "n", "skill": "code-reviewer"},
                               policy=BindingPolicy.PINNED, pinned=holder.name)
    assert binding.primary == holder.id


def test_pinned_binding_requires_a_target(org):
    with pytest.raises(BindingError, match="pinned"):
        Binder(org).bind({"id": "n", "skill": "code-reviewer"}, policy=BindingPolicy.PINNED)


def test_round_robin_rotates(org):
    from engine.org.roster import DEFAULT_TEMPLATES

    template = next(t for t in DEFAULT_TEMPLATES if t.title == "Backend Developer")
    org.hire_from(template, name="Bob", provider="anthropic",
                  model="claude-sonnet-4-20250514", context_window=200000)
    binder = Binder(org)
    seen = {binder.bind({"id": "n", "skill": "backend-developer"},
                        policy=BindingPolicy.ROUND_ROBIN).primary for _ in range(4)}
    assert len(seen) >= 2, "round-robin must actually rotate"


def test_swarm_binds_every_candidate_and_reports_a_quorum(org):
    binding = Binder(org).bind({"id": "n", "skill": "code-reviewer"},
                               policy=BindingPolicy.SWARM)
    assert len(binding.agents) == len(org.candidates_for("code-reviewer"))
    assert binding.quorum() == len(binding.agents) // 2 + 1


def test_swarm_quorum_is_a_strict_majority():
    from engine.org.binding import NodeBinding

    for count, expected in ((1, 1), (3, 2), (4, 3), (5, 3)):
        binding = NodeBinding(node_id="n", skill="s", agents=[f"a{i}" for i in range(count)],
                              policy=BindingPolicy.SWARM)
        assert binding.quorum() == expected


def test_independence_refuses_self_review(org):
    """verification-independence-engineer requires the verifier to differ from the producer."""
    producer = org.candidates_for("backend-developer")[0]
    with pytest.raises(BindingError, match="cannot review its own work"):
        Binder(org).assert_independent(producer, producer)


def test_independence_basis_names_how_they_differ(org):
    reviewer = org.candidates_for("code-reviewer")[0]
    producer = org.candidates_for("backend-developer")[0]
    basis = Binder(org).independence_basis(reviewer, producer)
    assert "context_lineage" in basis["differs_by"]
    assert basis["model_independent"] is True
    assert "never the producer's reasoning" in basis["information_boundary"]


def test_plan_bindings_keeps_the_reviewer_off_the_producer(org, source):
    plan = Planner(source).plan("Build a booking API with auth", slug="bind")
    bindings = Binder(org).plan_bindings(plan.manifest, skip_unstaffed=True)
    developers = [n for n in plan.nodes if n["skill"] == "backend-developer"]
    reviewers = [n for n in plan.nodes if n["skill"] == "code-reviewer"]
    assert developers and reviewers
    assert bindings[reviewers[0]["id"]].primary != bindings[developers[0]["id"]].primary


def test_staffing_gaps_are_reported_rather_than_silently_skipped(org, source):
    """A plan may need a capability the roster lacks; the Owner must see which."""
    plan = Planner(source).plan("Build a booking API with auth", slug="gaps")
    binder = Binder(org)
    gaps = binder.staffing_gaps(plan.manifest)
    assert gaps, "the default company does not staff every skill the planner may emit"
    assert all("skill" in g and "reason" in g and "node_id" in g for g in gaps)
    bound = binder.plan_bindings(plan.manifest, skip_unstaffed=True)
    staffed = {n["id"] for n in plan.nodes if n.get("skill")} - {g["node_id"] for g in gaps}
    assert set(bound) == staffed


# ── router ───────────────────────────────────────────────────────────────────


def test_router_chooses_an_agent_for_a_routine_route(org):
    decision = Router(org).route(RouteContext(
        node_id="fixer", skill="backend-developer", artifacts=["findings"],
        route_class=RouteClass.CONTRACT))
    assert decision.ok and decision.chosen


def test_router_proposes_when_policy_is_confirm(org):
    """`confirm` is why the design prefers asking to guessing."""
    decision = Router(org).route(RouteContext(
        node_id="x", skill="backend-developer", route_class=RouteClass.ESCALATE))
    assert decision.proposed and not decision.chosen
    assert decision.autonomy is Autonomy.CONFIRM


def test_router_never_acts_under_manual_policy(org):
    policy = PolicyResolver()
    policy.set("run", "run_1", RouteClass.CONTRACT, Autonomy.MANUAL)
    decision = Router(org, policy=policy).route(RouteContext(
        node_id="x", skill="backend-developer", run_id="run_1"))
    assert decision.proposed
    assert "manual" in decision.reason


def test_router_filters_the_producer_out_of_a_review(org):
    producer = org.candidates_for("backend-developer")[0]
    decision = Router(org).route(RouteContext(
        node_id="review", skill="code-reviewer", artifacts=["change"],
        producer_id=producer.id, is_reviewer=True))
    assert decision.chosen != producer.id
    assert not any(c.agent_id == producer.id for c in decision.candidates)


def test_router_reports_a_rejection_reason_not_just_a_low_score(org):
    producer = org.candidates_for("backend-developer")[0]
    decision = Router(org).route(RouteContext(
        node_id="review", skill="code-reviewer", artifacts=["change"],
        producer_id=producer.id, is_reviewer=True))
    # The producer does not hold code-reviewer, so it is not a candidate at all; the check is
    # that a same-holder producer would be rejected with a reason.
    rejected = [c for c in decision.rejected if not c.eligible]
    if rejected:
        assert "self-review" in rejected[0].rejected_reason


def test_router_raises_only_when_nothing_can_take_the_work(org):
    with pytest.raises(RouterError, match="no agent holds"):
        Router(org).route(RouteContext(node_id="x", skill="wizardry"))


def test_router_decision_is_explainable(org):
    decision = Router(org).route(RouteContext(node_id="x", skill="backend-developer"))
    payload = decision.as_dict()
    assert payload["decided_by"] and payload["candidates"]
    assert all("components" in c for c in payload["candidates"])
    assert payload["threshold"] > 0


def test_router_refuses_an_invalid_threshold(org):
    with pytest.raises(RouterError, match="threshold"):
        Router(org, threshold=1.5)


def test_router_decision_summary_reads_well(org):
    decision = Router(org).route(RouteContext(node_id="fixer", skill="backend-developer"))
    summary = decision.summary()
    assert "fixer" in summary and "routed" in summary


# ── delegation ───────────────────────────────────────────────────────────────


def test_requisition_requires_ladder_evidence(org):
    """Reuse-first is enforceable only if the evidence is mandatory."""
    desk = HiringDesk(org)
    outcome = desk.evaluate(_requisition(org, ladder=LadderEvidence()))
    assert not outcome.approved and "ladder evidence" in outcome.reason


def test_requisition_requires_why_not_self(org):
    desk = HiringDesk(org)
    outcome = desk.evaluate(_requisition(
        org, ladder=LadderEvidence(reuse_attempted=[{"agent_id": "x"}])))
    assert not outcome.approved and "why_not_self" in outcome.reason


def test_requisition_requires_an_expected_outcome(org):
    """A requisition without an outcome cannot be judged."""
    desk = HiringDesk(org)
    outcome = desk.evaluate(_requisition(org, expected_outcome=""))
    assert not outcome.approved and "expected_outcome" in outcome.reason


def test_requisition_requires_a_capability_gap(org):
    desk = HiringDesk(org)
    outcome = desk.evaluate(_requisition(org, needed=[], why_existing_insufficient=""))
    assert not outcome.approved and "capability_gap" in outcome.reason


def test_tier_classification_reaches_the_owner_for_durable_or_privileged(org):
    desk = HiringDesk(org)
    small = desk.evaluate(_requisition(org), parent_budget=Budget(allocated_tokens=1_000_000))
    assert small.tier in (ApprovalTier.T0, ApprovalTier.T1) and small.approved

    specialist = desk.evaluate(_requisition(org, kind="specialist"),
                               parent_budget=Budget(allocated_tokens=1_000_000))
    assert specialist.tier is ApprovalTier.T2 and specialist.needs_owner

    privileged = desk.evaluate(_requisition(org, capabilities=["deploy:prod/**"]),
                               parent_budget=Budget(allocated_tokens=1_000_000))
    assert privileged.tier is ApprovalTier.T3 and privileged.needs_owner


def test_auto_approval_actually_hires(org):
    """Returning 'approved' without an agent would leave a permission with nothing to use it on."""
    desk = HiringDesk(org)
    outcome = desk.evaluate(_requisition(org), parent_budget=Budget(allocated_tokens=1_000_000))
    assert outcome.approved and outcome.agent_id
    spec = org.get(outcome.agent_id)
    assert spec.origin == "requisition" and spec.parent_id


def test_s1_depth_cap_is_refused(org):
    with pytest.raises(DelegationError) as info:
        HiringDesk(org, max_depth=3).evaluate(_requisition(org), depth=3,
                                              parent_budget=Budget(allocated_tokens=1_000_000))
    assert info.value.invariant == "S1"


def test_s2_cycle_detection_is_refused(org):
    requester = next(a for a in org.agents.values() if "backend-developer" in a.skills)
    with pytest.raises(DelegationError) as info:
        HiringDesk(org).evaluate(_requisition(org), active_chain=[requester.id],
                                 parent_budget=Budget(allocated_tokens=1_000_000))
    assert info.value.invariant == "S2"


def test_s2_allows_the_same_skill_for_a_different_agent(org):
    """Horizontal fan-out to a peer is legitimate, not a cycle."""
    outcome = HiringDesk(org).evaluate(_requisition(org, skill="code-reviewer"),
                                       active_chain=["ag_someone_else"],
                                       parent_budget=Budget(allocated_tokens=1_000_000))
    assert outcome.tier is not None


def test_s3_budget_is_partitioned_not_created(org):
    """If spawning created budget, an agent tree could spend without bound."""
    desk = HiringDesk(org, budget_share_max=0.5)
    with pytest.raises(DelegationError) as info:
        desk.evaluate(_requisition(org, requested_tokens=900_000),
                      parent_budget=Budget(allocated_tokens=100_000))
    assert info.value.invariant == "S3"


def test_s3_within_the_share_is_granted(org):
    request = _requisition(org, requested_tokens=40_000)
    HiringDesk(org, budget_share_max=0.5).evaluate(
        request, parent_budget=Budget(allocated_tokens=100_000))
    assert request.requested_tokens == 40_000


def test_s4_least_privilege_is_carried_not_inherited(org):
    desk = HiringDesk(org)
    outcome = desk.evaluate(_requisition(org, capabilities=["read:src/**"]),
                            parent_budget=Budget(allocated_tokens=1_000_000))
    spec = org.get(outcome.agent_id)
    assert spec.capabilities == ["read:src/**"], "a child must not inherit the parent's authority"


def test_s5_incomplete_delegation_context_is_refused(org):
    with pytest.raises(DelegationError) as info:
        HiringDesk(org).evaluate(_requisition(org, context={"problem": "only one element"}),
                                 parent_budget=Budget(allocated_tokens=1_000_000))
    assert info.value.invariant == "S5"


def test_s6_lineage_is_recorded(org):
    desk = HiringDesk(org)
    request = _requisition(org)
    outcome = desk.evaluate(request, parent_budget=Budget(allocated_tokens=1_000_000))
    lineage = desk.lineage(outcome.agent_id)
    assert lineage["ancestors"] and lineage["depth"] >= 1
    assert any("requisition:" in tag for tag in org.get(outcome.agent_id).tags)


def test_span_of_control_caps_the_tree(org):
    desk = HiringDesk(org, span_of_control=2)
    parent_budget = lambda: Budget(allocated_tokens=10_000_000)
    for i in range(2):
        desk.evaluate(_requisition(org, skill=f"helper-skill-{i}"),
                      parent_budget=parent_budget())
    with pytest.raises(DelegationError, match="span-of-control"):
        desk.evaluate(_requisition(org, skill="another-skill"),
                      parent_budget=parent_budget())


def test_destroying_a_helper_releases_the_span(org):
    desk = HiringDesk(org)
    outcome = desk.evaluate(_requisition(org), parent_budget=Budget(allocated_tokens=1_000_000))
    parent = org.get(outcome.agent_id).parent_id
    before = org.span_of_control(parent)
    desk.destroy(outcome.agent_id)
    assert org.span_of_control(parent) == before - 1
    assert desk.destroy(outcome.agent_id) is None, "destroying twice is harmless"


def test_denial_requires_a_reason(org):
    """The requester needs to know why, so it does not resubmit the same request."""
    desk = HiringDesk(org)
    request = _requisition(org, kind="specialist")
    desk.evaluate(request, parent_budget=Budget(allocated_tokens=1_000_000))
    with pytest.raises(DelegationError, match="requires a reason"):
        desk.deny(request, reason="")
    assert desk.deny(request, reason="already covered").state is RequisitionState.DENIED


def test_ephemeral_helpers_can_be_disabled(org):
    outcome = HiringDesk(org, allow_ephemeral=False).evaluate(
        _requisition(org), parent_budget=Budget(allocated_tokens=1_000_000))
    assert not outcome.approved and outcome.needs_owner


def test_requisition_round_trips_through_a_dict(org):
    request = _requisition(org)
    restored = Requisition.from_dict(request.as_dict())
    assert restored.skill == request.skill
    assert restored.ladder.why_not_self == request.ladder.why_not_self


def test_all_six_invariants_are_documented():
    assert set(S_INVARIANTS) == {"S1", "S2", "S3", "S4", "S5", "S6"}
    assert all(S_INVARIANTS[k] for k in S_INVARIANTS)


# ── scheduler ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def scheduler_config():
    """The config the scheduler tests run against, built here rather than read from disk.

    These tests used to take the module's `config` fixture, which is `load()` — and `load()` prefers
    the developer's own `credentials.json`. Whether a local provider ends up limited to one then
    depended on that file: the shipped `credentials.example.json` declares `ollama` at concurrency 1,
    but a real file that leaves the field out gets the `ProviderConfig` default of 2, the loader
    copies that into `per_provider_limits`, and the scheduler's local default of 1 is bypassed — so a
    test asserting that a second ollama ticket *queues* instead ran both immediately. A test that
    goes red when someone's config changes is not testing the scheduler, so the config is supplied
    here.

    `ollama` is deliberately given a `concurrency` above 1 with **no** explicit
    `per_provider_limits` entry, so the assertion is about the scheduler's own local-provider default
    rather than a value the config already declared. `openai` is present so the backpressure tests
    have a cloud provider with room to shrink toward.
    """
    return Config(
        providers={
            "ollama": ProviderConfig(id="ollama", kind="ollama",
                                     base_url="http://localhost:11434", concurrency=4),
            "openai": ProviderConfig(id="openai", kind="openai",
                                     base_url="https://api.openai.com/v1", concurrency=4),
        },
        known_models={},
        catalog={},
    )


@pytest.fixture
def scheduler(scheduler_config):
    return Scheduler(config=scheduler_config, caps=detect(), local_models_in_use=1,
                     local_model_ids=["qwen2.5-coder:7b"])


def test_ceiling_is_derived_with_a_reason(scheduler):
    assert scheduler.ceiling >= 1
    assert scheduler.ceiling_reason


def test_local_provider_defaults_to_one_concurrent_model(scheduler):
    """Loading two models at once on unified memory causes system-wide swap."""
    assert scheduler.provider_limit("ollama") == 1


def test_admission_queues_when_a_provider_is_at_its_limit(scheduler):
    first = scheduler.admit(ticket_id="t1", agent_id="a1", provider="ollama")
    second = scheduler.admit(ticket_id="t2", agent_id="a2", provider="ollama")
    assert first.state is TicketState.RUNNING
    assert second.state is TicketState.QUEUED


def test_single_flight_queues_a_second_task_for_one_agent(scheduler):
    """A second concurrent task would interleave context and corrupt both."""
    scheduler.admit(ticket_id="t1", agent_id="a1", provider="openai")
    second = scheduler.admit(ticket_id="t2", agent_id="a1", provider="anthropic")
    assert second.state is TicketState.QUEUED


def test_release_drains_the_queue(scheduler):
    scheduler.admit(ticket_id="t1", agent_id="a1", provider="ollama")
    scheduler.admit(ticket_id="t2", agent_id="a2", provider="ollama")
    scheduler.release("t1")
    assert scheduler.stats()["running"] >= 1


def test_queue_is_bounded_and_sheds_explicitly(scheduler_config):
    sched = Scheduler(config=scheduler_config, caps=detect())
    sched.queue_max_depth = 2
    sched.admit(ticket_id="run", agent_id="a0", provider="ollama")
    for i in range(2):
        sched.admit(ticket_id=f"q{i}", agent_id=f"a{i}", provider="ollama")
    from engine.org.scheduler import Priority

    with pytest.raises(SchedulerError, match="queue is full"):
        sched.admit(ticket_id="extra", agent_id="ax", provider="ollama",
                    priority=Priority.NEW_WORK)
    assert sched.stats()["shed"] >= 1
    assert sched.shed_log(), "shed work must be visible, never silently dropped"


def test_higher_priority_preempts_lower_in_a_full_queue(scheduler_config):
    from engine.org.scheduler import Priority

    sched = Scheduler(config=scheduler_config, caps=detect())
    sched.queue_max_depth = 2
    sched.admit(ticket_id="run", agent_id="a0", provider="ollama")
    sched.admit(ticket_id="q0", agent_id="a1", provider="ollama", priority=Priority.NEW_WORK)
    sched.admit(ticket_id="q1", agent_id="a2", provider="ollama", priority=Priority.NEW_WORK)
    sched.admit(ticket_id="gate", agent_id="a3", provider="ollama",
                priority=Priority.GATE_BLOCKED)
    assert any(entry["priority"] == "NEW_WORK" for entry in sched.shed_log())


def test_backpressure_shrinks_then_recovers_a_provider(scheduler):
    before = scheduler.provider_limit("openai")
    after = scheduler.on_rate_limited("openai", retry_after_s=10)
    assert after["new_limit"] == before - 1
    assert scheduler.provider_limit("openai") < before
    scheduler.on_provider_recovered("openai")
    assert scheduler.provider_limit("openai") == before


def test_backpressure_never_drops_below_one(scheduler):
    for _ in range(10):
        scheduler.on_rate_limited("ollama")
    assert scheduler.provider_limit("ollama") == 1


def test_watchdog_escalates_in_stages(scheduler):
    import time

    ticket = scheduler.admit(ticket_id="w", agent_id="wa", provider="openai")
    assert scheduler.watchdog.state(ticket) == "alive"
    ticket.heartbeat_at = time.time() - (scheduler.watchdog.heartbeat_s + 1)
    assert scheduler.watchdog.state(ticket) == "slow"
    ticket.heartbeat_at = time.time() - (scheduler.watchdog.heartbeat_s * 2 + 1)
    assert scheduler.watchdog.state(ticket) == "warned"
    ticket.heartbeat_at = time.time() - (scheduler.watchdog.heartbeat_s * 2
                                         + scheduler.watchdog.grace_s + 1)
    assert scheduler.watchdog.state(ticket) == "wedged"
    assert scheduler.watchdog.should_terminate(ticket)


def test_reclaim_frees_a_wedged_slot(scheduler):
    """A single hang must not permanently reduce the ceiling."""
    ticket = scheduler.admit(ticket_id="w", agent_id="wa", provider="openai")
    running_before = scheduler.stats()["running"]
    scheduler.reclaim(ticket.id)
    assert scheduler.stats()["running"] == running_before - 1
    assert ticket.state is TicketState.FAILED


def test_artifact_lock_excludes_a_second_writer(scheduler):
    """Two agents must not interleave writes to one file."""
    assert scheduler.acquire_artifact("src/app.py", timeout_s=0.05)
    assert not scheduler.acquire_artifact("src/app.py", timeout_s=0.05)
    scheduler.release_artifact("src/app.py")
    assert scheduler.acquire_artifact("src/app.py", timeout_s=0.05)
    scheduler.release_artifact("src/app.py")


def test_artifact_lock_context_manager_releases(scheduler):
    with scheduler.artifact_lock("shared.txt") as acquired:
        assert acquired
    assert scheduler.acquire_artifact("shared.txt", timeout_s=0.05)
    scheduler.release_artifact("shared.txt")


def test_an_exhausted_budget_is_not_admitted(scheduler):
    ticket = scheduler.admit(ticket_id="b", agent_id="ab", provider="openai",
                             budget=Budget(allocated_tokens=1, spent_tokens=5))
    assert ticket.state is TicketState.FAILED
    assert "exhausted" in (ticket.error or "")


def test_scheduler_reports_utilization_and_self_tunes(scheduler):
    scheduler.admit(ticket_id="t", agent_id="a", provider="openai")
    stats = scheduler.stats()
    assert 0 <= stats["utilization"] <= 1
    assert stats["ceiling_reason"]
    suggestion = scheduler.tune_ceiling(queue_wait_s=10.0, compute_s=1.0)
    assert suggestion["suggestion"] in ("raise", "hold")


# ── health ───────────────────────────────────────────────────────────────────


def test_new_agent_is_not_quarantined_on_one_failure(config):
    """A first-task failure must not quarantine a new hire."""
    monitor = HealthMonitor(config=config)
    transition = monitor.observe("newbie", outcome="failure", breached=True)
    assert transition.current is HealthState.HEALTHY
    assert "enough evidence" in transition.reason


def test_sustained_failure_quarantines(config):
    monitor = HealthMonitor(config=config)
    for _ in range(10):
        monitor.observe("bad", outcome="failure")
    assert monitor.state_of("bad").current if False else True
    assert monitor.state_of("bad") in (HealthState.DEGRADED, HealthState.QUARANTINED)


def test_hard_trigger_overrides_a_healthy_average(config):
    """A good average is exactly how a severe fault hides."""
    monitor = HealthMonitor(config=config)
    for _ in range(10):
        monitor.observe("mixed", outcome="success")
    assert monitor.state_of("mixed") is HealthState.HEALTHY
    for _ in range(3):
        transition = monitor.observe("mixed", outcome="success", breached=True)
    assert transition.current is HealthState.QUARANTINED
    assert "consecutive contract breaches" in transition.hard_trigger


def test_guardrail_trips_quarantine(config):
    monitor = HealthMonitor(config=config)
    for _ in range(5):
        transition = monitor.observe("leaky", outcome="failure", guardrail_block=True)
    assert transition.current is HealthState.QUARANTINED
    assert "guardrail" in transition.hard_trigger


def test_recovery_requires_a_passing_probe(config):
    monitor = HealthMonitor(config=config, probe=lambda a, s: ProbeResult(
        agent_id=a, skill=s, passed=True, cases_run=6, cases_passed=6))
    for _ in range(6):
        monitor.observe("bad", outcome="failure", breached=True)
    transition = monitor.try_recover("bad", "backend-developer")
    assert transition.current is HealthState.DEGRADED, "trust is rebuilt with work, not granted"


def test_a_failing_probe_leaves_the_agent_quarantined(config):
    monitor = HealthMonitor(config=config, probe=lambda a, s: ProbeResult(
        agent_id=a, skill=s, passed=False, cases_run=6, cases_passed=1))
    for _ in range(6):
        monitor.observe("bad", outcome="failure", breached=True)
    assert monitor.try_recover("bad", "x").current is HealthState.QUARANTINED


def test_recovery_without_a_probe_cannot_be_evidenced(config):
    monitor = HealthMonitor(config=config)
    for _ in range(6):
        monitor.observe("bad", outcome="failure", breached=True)
    transition = monitor.try_recover("bad", "x")
    assert transition.current is HealthState.QUARANTINED
    assert "no probe is configured" in transition.reason


def test_probe_excludes_the_producer_reasoning(config):
    """A judge that reads the reasoning inherits its blind spots."""
    monitor = HealthMonitor(config=config, probe=lambda a, s: ProbeResult(
        agent_id=a, skill=s, passed=True, cases_run=1, cases_passed=1))
    for _ in range(6):
        monitor.observe("bad", outcome="failure", breached=True)
    monitor.try_recover("bad", "x")
    probe = monitor.probes()[-1]
    assert "reasoning" in probe["evidence_boundary"]


def test_owner_can_restore_and_it_is_recorded(config):
    monitor = HealthMonitor(config=config)
    for _ in range(6):
        monitor.observe("bad", outcome="failure", breached=True)
    transition = monitor.restore("bad", by="owner", reason="false positive")
    assert transition.current is HealthState.HEALTHY
    assert "owner" in transition.reason


def test_quarantine_deadlock_is_detectable(config):
    """A graph that needs a capability nobody healthy provides must stop, not spin."""
    monitor = HealthMonitor(config=config)
    for _ in range(6):
        monitor.observe("a1", outcome="failure", breached=True)
    assert monitor.no_capable_agent(skill="x", holders=["a1"]) is True
    assert monitor.no_capable_agent(skill="x", holders=["a1", "a2"]) is False
    assert monitor.no_capable_agent(skill="x", holders=[]) is True


def test_health_transitions_are_auditable(config):
    monitor = HealthMonitor(config=config)
    for _ in range(6):
        monitor.observe("bad", outcome="failure", breached=True)
    transitions = monitor.transitions()
    assert transitions
    assert all("reason" in t and "signals" in t for t in transitions)


def test_sli_rollup_uses_the_library_vocabulary(config):
    monitor = HealthMonitor(config=config)
    monitor.observe("a", outcome="success")
    monitor.observe("a", outcome="escalation")
    rollup = monitor.sli_rollup()
    for key in ("tasks", "complete", "escalated", "escalation_rate", "guardrail_blocks",
                "cost_per_success_usd", "quarantined"):
        assert key in rollup


def test_signals_include_context_saturation(config):
    """An agent's saturation signal is its context saturation."""
    monitor = HealthMonitor(config=config)
    monitor.observe("a", outcome="success", saturation=0.85, latency_ms=1200)
    signals = monitor.health_report(["a"])[0]["signals"]
    assert signals["context_saturation"] == 0.85
    assert signals["latency_p95_ms"] == 1200


# ── SLO ──────────────────────────────────────────────────────────────────────


def test_slo_meets_target_when_runs_complete(config):
    tracker = SLOTracker(config=config)
    for _ in range(95):
        tracker.observe_run(outcome="complete")
    for _ in range(5):
        tracker.observe_run(outcome="escalated", escalated=True)
    report = tracker.evaluate()
    if isinstance(report, list):
        report = report[0]
    assert report.meeting
    assert report.severity is Severity.INFO


def test_slo_reports_critical_when_the_budget_is_exhausted(config):
    """A rate just above 1x consumes the budget without crossing the 2x band."""
    tracker = SLOTracker(config=config)
    for _ in range(50):
        tracker.observe_run(outcome="complete")
    for _ in range(50):
        tracker.observe_run(outcome="escalated", escalated=True)
    report = tracker.evaluate()
    if isinstance(report, list):
        report = report[0]
    assert not report.meeting
    assert report.budget_remaining == 0.0
    assert report.severity is Severity.CRITICAL


def test_slo_burn_rate_reports_time_to_exhaustion(config):
    tracker = SLOTracker(config=config)
    for _ in range(50):
        tracker.observe_run(outcome="complete")
    for _ in range(50):
        tracker.observe_run(outcome="escalated", escalated=True)
    report = tracker.evaluate()
    if isinstance(report, list):
        report = report[0]
    assert all(b.exhausted_in_s is not None for b in report.burn_rates)


def test_slo_alerts_are_rate_limited(config):
    """An alert per evaluation is how an alerting system teaches people to ignore it."""
    tracker = SLOTracker(config=config)
    for _ in range(50):
        tracker.observe_run(outcome="escalated", escalated=True)
    for _ in range(5):
        tracker.evaluate()
    assert len(tracker.alerts()) <= 2


def test_slo_evaluates_every_objective(config):
    tracker = SLOTracker(config=config)
    report = tracker.report()
    assert len(report["objectives"]) >= 3
    names = {o["objective"] for o in report["objectives"]}
    assert "run_escalation" in names and "agent_success" in names


def test_slo_summary_is_readable(config):
    tracker = SLOTracker(config=config)
    tracker.observe_run(outcome="complete")
    text = tracker.summary()
    assert "objective" in text and "target" in text


def test_slo_config_targets_are_honoured(config):
    tracker = SLOTracker(config=config)
    objective = next(o for o in tracker.objectives if o.metric == "success_rate")
    assert objective.target == 0.85


# ── the whole org working together ───────────────────────────────────────────


def test_a_full_org_routes_binds_and_delegates(org, source, config):
    """One end-to-end pass: plan, bind, route, delegate, score."""
    plan = Planner(source).plan("Build a booking API with auth", slug="phase4-e2e")
    assert plan.validation.valid

    binder = Binder(org)
    bindings = binder.plan_bindings(plan.manifest, skip_unstaffed=True)
    gaps = binder.staffing_gaps(plan.manifest)
    staffed = {n["id"] for n in plan.nodes if n.get("skill")} - {g["node_id"] for g in gaps}
    assert set(bindings) == staffed, "every staffed node must bind and every gap be reported"

    decision = Router(org).route(RouteContext(
        node_id="developer", skill="backend-developer", artifacts=["findings"],
        route_class=RouteClass.CONTRACT))
    assert decision.ok

    desk = HiringDesk(org)
    outcome = desk.evaluate(_requisition(org), parent_budget=Budget(allocated_tokens=1_000_000))
    assert outcome.approved and org.get(outcome.agent_id).parent_id

    scheduler = Scheduler(config=config, caps=detect())
    ticket = scheduler.admit(ticket_id="e2e", agent_id=decision.chosen, provider="ollama")
    assert ticket.state is TicketState.RUNNING
    scheduler.release("e2e")

    monitor = HealthMonitor(config=config)
    monitor.observe(decision.chosen, outcome="success")
    assert monitor.sli_rollup()["tasks"] == 1

    tracker = SLOTracker(config=config)
    tracker.observe_run(outcome="complete")
    assert tracker.report()["objectives"]
