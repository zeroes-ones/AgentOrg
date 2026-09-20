#!/usr/bin/env python3
"""runner.py — run the behavioural suite and compare it against a frozen baseline.

WHY THIS EXISTS
---------------
Unit tests prove the engine does what the code says. They cannot prove the engine makes *good
decisions*, because a judgment — did the reviewer reject correctly, did compaction keep the rule, did
an under-confident router ask instead of guessing — has no boolean to assert.

The failure mode of this system is not a crash. It is **confident wrong output**: a run that completes
and is wrong. Only scenarios with expected outcomes catch that, and only a *baseline* makes a change in
behaviour visible. The library's own rule is explicit: no eval without a baseline.

DESIGN
------
- **Scenarios are data**, in a JSON file, so adding a case is editing a file rather than writing code.
- **Every scenario declares its expectation** — which outcome, which invariant must hold, which
  property must be preserved — so a scenario cannot pass vacuously.
- **The baseline is frozen**, and the gate reports a delta rather than an absolute: a suite that got
  *better* on one scenario and worse on three is a regression even at the same total.
- **The gate blocks on a regression**, so a change that trades a safety property for a feature cannot
  merge quietly.
- **A scenario that cannot run is a failure, not a skip.** A silently skipped safety check is how a
  suite decays into decoration.

Usage:
    python3 run_tests.py                       # the unit suite
    python3 -m engine.evals.runner             # the behavioural suite, with the gate
    python3 -m engine.evals.runner --freeze    # record the current results as the baseline
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# The scenarios exercise the real engine, so the import path is the repository root.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.config import load  # noqa: E402
from engine.context import (  # noqa: E402
    Session,
    assemble_session_prompt,
    build_handoff,
    compact,
    decide_rotation,
    project,
)
from engine.library import resolve  # noqa: E402
from engine.org import (  # noqa: E402
    AgentSpec,
    Binder,
    Budget,
    DelegationError,
    HiringDesk,
    HealthMonitor,
    LadderEvidence,
    Ledger,
    PolicyError,
    PolicyResolver,
    Requisition,
    RouteClass,
    RouteContext,
    Router,
    RouterError,
    default_company,
)
from engine.org.handoff import (  # noqa: E402
    Handoff,
    build_context_pass_through,
    validate_delegation_context,
    validate_handoff,
)
from engine.planner import Planner  # noqa: E402
from engine.skills import FilesystemSkillSource  # noqa: E402

__all__ = ["Outcome", "ScenarioResult", "run_suite", "load_scenarios", "compare_to_baseline"]

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_USAGE = 2

SCENARIOS_PATH = Path(__file__).resolve().parent / "scenarios.json"
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"


@dataclass
class Outcome:
    """The result of exercising one scenario."""

    name: str
    passed: bool
    check: str
    detail: str = ""
    error: str = ""
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "check": self.check,
                "detail": self.detail, "error": self.error,
                "duration_ms": round(self.duration_ms, 2)}


@dataclass
class ScenarioResult:
    """The suite's result: every outcome plus the aggregate."""

    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if not o.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / len(self.outcomes) if self.outcomes else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenarios": len(self.outcomes),
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate, 4),
            "results": {o.name: o.as_dict() for o in self.outcomes},
        }


# ── the scenarios ────────────────────────────────────────────────────────────
#
# Each check is a small function that either returns a detail string (passing) or raises (failing).
# Raising rather than returning False keeps the *reason* in the failure, which is the useful part.


def _source() -> FilesystemSkillSource:
    """A skill source, loaded once per suite run."""
    return FilesystemSkillSource(resolve())


def _org():
    return default_company(
        provider="ollama", model="qwen2.5-coder:7b", context_window=32768,
        reviewer_provider="anthropic", reviewer_model="claude-sonnet-4-20250514",
        reviewer_context_window=200000,
    )


def check_loop_termination() -> str:
    """Every generated plan must contain a bounded loop and a reachable terminal gate.

    The terminal authority is still a *human* gate, but exhaustion may now pass through a
    **bounded, agent-made reroute** first (`kind: agent`, capped by `max_reroutes`). So the invariant
    is stated as it now holds: the loop's escalation reaches a human gate in at most one bounded hop.
    An agent gate that did not eventually reach a human gate would be unbounded autonomy, which is
    exactly what this check exists to forbid.
    """
    plan = Planner(_source()).plan("Build a booking API with auth", slug="eval-loop")
    assert plan.validation.valid, f"plan did not validate: {plan.validation.errors}"
    loops = plan.loops
    assert loops, "a plan with no loop cannot iterate toward correctness"
    loop = loops[0]
    assert loop["exit_when"], "an unbounded loop would never terminate"
    assert loop["max_iterations"] >= 1
    by_id = {g["id"]: g for g in plan.gates}
    human = {gid for gid, g in by_id.items() if g.get("kind") == "human"}
    assert human, "a plan must end at a human gate"

    target = loop["escalate_to"]
    assert target in by_id, f"loop escalates to unknown gate {target!r}"
    hops = 0
    while by_id.get(target, {}).get("kind") == "agent":
        # A bounded reroute may stand between the loop and the human — once.
        assert hops < 1, "more than one agent gate between the loop and a human is unbounded"
        assert by_id[target].get("max_reroutes", 0) >= 1, "an agent gate must bound its reroutes"
        target = by_id[target].get("escalate_to")
        assert target in by_id, "an agent gate must escalate onward to a declared gate"
        hops += 1
    assert target in human, "loop exhaustion must ultimately reach the human gate"
    return (f"{len(plan.nodes)} nodes, loop bounded at {loop['max_iterations']}, "
            f"escalates to {loop['escalate_to']} → {target}")


def check_constraint_survival() -> str:
    """A NEVER rule must survive compaction, rotation and re-pinning into the primacy zone.

    Sized so both happen: compaction is applied first (which must not drop the rule), and saturation
    stays high enough that a rotation is genuinely warranted afterwards.
    """
    session = Session(agent_id="ag_1", node_id="fixer", window=1200, output_reserve=0)
    rule = "NEVER store passwords in plaintext — use a memory-hard KDF"
    session.pin(rule)
    for _ in range(5):
        session.append_text("assistant", "x" * 1200, tier=3)

    # Compaction runs and must preserve the rule. With only evictable tier-3 turns plus the pinned
    # rule, it frees history without touching the constraint.
    compact(session, target=0.5)
    assert rule in session.pinned, "compaction dropped the pinned constraint"
    assert len(session.pinned) == 1

    # Refill so rotation is warranted: the point is that a rotation carries the rule across.
    for _ in range(5):
        session.append_text("assistant", "y" * 1200, tier=3)
    decision = decide_rotation(session)
    assert decision.should_rotate, f"the fixture should warrant a rotation: {decision.reason}"

    handoff = build_handoff(session, decision, run_id="eval", agent_name="Alice",
                            skill="backend-developer", model="qwen2.5-coder:7b")
    carried = {c["value"] for c in handoff.constraints}
    assert rule in carried, "the rotation handoff lost the constraint"
    assert all(c["non_negotiable"] for c in handoff.constraints if c["value"] == rule)

    prompt = assemble_session_prompt(handoff, task="continue", agent_name="Alice",
                                     trailer_schema={"status": "done"})
    assert prompt.contains_in_primacy(rule[:40]), "the constraint did not reach the primacy zone"
    assert prompt.middle_zone_guardrails() == [], "a guardrail drifted into the middle band"
    return (f"survived compaction, carried non-negotiable through a {decision.trigger.value} "
            f"rotation, re-pinned to primacy")


def check_rotation_refuses_when_impossible() -> str:
    """Rotating cannot fix an irreducible overflow, so it must be refused rather than attempted."""
    session = Session(agent_id="a", window=4000, output_reserve=0)
    projection = project(session, system="s" * 14000, pinned="NEVER x", new_message="go")
    assert projection.irreducible_overflow, "the fixture should be an irreducible overflow"
    decision = decide_rotation(session, projection=projection)
    assert not decision.should_rotate, "an impossible rotation must not be attempted"
    assert decision.impossible
    assert "Lower the skill tier" in decision.reason, "the refusal must name the fix"
    return f"refused with the fix named (irreducible {projection.irreducible_saturation:.0%})"


def check_reviewer_independence() -> str:
    """A reviewer must never be the artifact's producer."""
    org = _org()
    binder = Binder(org)
    producer = org.candidates_for("backend-developer")[0]
    try:
        binder.assert_independent(producer, producer)
    except Exception:
        pass
    else:
        raise AssertionError("self-review was permitted")

    plan = Planner(_source()).plan("Build a booking API with auth", slug="eval-bind")
    bindings = binder.plan_bindings(plan.manifest, skip_unstaffed=True)
    developers = [n for n in plan.nodes if n["skill"] == "backend-developer"]
    reviewers = [n for n in plan.nodes if n["skill"] == "code-reviewer"]
    assert developers and reviewers, "the fixture needs both a developer and a reviewer"
    assert bindings[reviewers[0]["id"]].primary != bindings[developers[0]["id"]].primary

    producer_spec = org.agents[bindings[developers[0]["id"]].primary]
    reviewer_spec = org.agents[bindings[reviewers[0]["id"]].primary]
    basis = binder.independence_basis(reviewer_spec, producer_spec)
    assert "context_lineage" in basis["differs_by"], "independence must be demonstrable"
    assert basis["model_independent"], "the default company should differ by model"
    return f"self-review refused; {reviewer_spec.name} differs by {basis['differs_by']}"


def check_gate_integrity() -> str:
    """A node must not advance on a self-declared done with no evidence."""
    payload = {
        "status": "done", "summary": "s", "artifacts": [], "decisions": [],
        "open_questions": [], "verification_evidence": [], "context": {}, "budget": {}, "next": "",
    }
    # A complete payload passes.
    good = Handoff(payload=dict(payload), origin="a", target="b")
    assert validate_handoff(good, stage="propose").ok

    # A missing registry field is refused: a receiver that cannot see open_questions cannot know what
    # was left unresolved.
    thin = Handoff(payload={"status": "done", "summary": "s"}, origin="a", target="b")
    verdict = validate_handoff(thin, stage="propose")
    assert not verdict.ok and "REGISTRY" in verdict.rules

    # An irreversible decision with no ledger entry is refused.
    irreversible = Handoff(payload={**payload,
                                    "decisions": [{"gate": "auth", "choice": "x",
                                                   "reversible": False}]},
                           origin="a", target="b")
    assert "R5" in validate_handoff(irreversible, stage="propose").rules
    return "incomplete payload and unrecorded irreversible decision both refused"


def check_delegation_safety() -> str:
    """The delegation invariants must refuse their violations."""
    org = _org()
    alice = next(a for a in org.agents.values() if "backend-developer" in a.skills)
    ctx = build_context_pass_through(problem="p", tried=["x"], logs="l", paths=["a.py"],
                                     hypothesis="h")

    def requisition(**overrides) -> Requisition:
        base = dict(
            requester_id=alice.id, requester_name=alice.name, kind="helper",
            skill="devops-engineer", provider="ollama", model="qwen2.5-coder:7b",
            context_window=32768, capabilities=["read:src/**"], needed=["k8s"],
            why_existing_insufficient="none", expected_outcome="unblock",
            ladder=LadderEvidence(reuse_attempted=[{"agent_id": "x", "why_rejected": "docker"}],
                                  why_not_self="needs context"),
            context=ctx,
        )
        base.update(overrides)
        return Requisition(**base)

    desk = HiringDesk(org, max_depth=3, budget_share_max=0.5)

    # An unjustified request is rejected rather than forwarded.
    unjustified = desk.evaluate(requisition(ladder=LadderEvidence()))
    assert not unjustified.approved and "ladder evidence" in unjustified.reason

    # S1 depth, S2 cycle, S3 overspend and S5 incomplete context each refuse.
    checks = [
        ("S1", lambda: desk.evaluate(requisition(), depth=3,
                                     parent_budget=Budget(allocated_tokens=1_000_000))),
        ("S2", lambda: desk.evaluate(requisition(), active_chain=[alice.id],
                                     parent_budget=Budget(allocated_tokens=1_000_000))),
        ("S3", lambda: desk.evaluate(requisition(requested_tokens=900_000),
                                     parent_budget=Budget(allocated_tokens=100_000))),
        ("S5", lambda: desk.evaluate(requisition(context={"problem": "only one"}),
                                     parent_budget=Budget(allocated_tokens=1_000_000))),
    ]
    for expected, call in checks:
        try:
            call()
        except DelegationError as exc:
            assert exc.invariant == expected, f"expected {expected}, got {exc.invariant}"
        else:
            raise AssertionError(f"{expected} did not refuse")

    # S4: an auto-approved helper carries only what was requested, never the parent's authority.
    outcome = desk.evaluate(requisition(), parent_budget=Budget(allocated_tokens=1_000_000))
    assert outcome.approved and outcome.agent_id
    child = org.get(outcome.agent_id)
    assert child.capabilities == ["read:src/**"], "a child must not inherit the parent's authority"
    assert child.parent_id == alice.id, "lineage must be recorded"
    return "unjustified rejected; S1-S5 refused; S4 least privilege and S6 lineage held"


def check_autonomy_floor() -> str:
    """No single setting may silently disable every human gate."""
    resolver = PolicyResolver()
    for route_class in (RouteClass.ESCALATE, RouteClass.CONFLICT):
        try:
            resolver.set("agent", "ag_1", route_class, "auto")
        except PolicyError:
            pass
        else:
            raise AssertionError(f"{route_class.value} was allowed below the safety floor")

    # Routine classes may be loosened, which is what makes the floor tolerable rather than rigid.
    resolver.set("agent", "ag_1", RouteClass.REWORK, "notify")
    assert resolver.resolve(RouteClass.REWORK, agent_id="ag_1").level.value == "notify"
    assert resolver.resolve(RouteClass.ESCALATE).level.value == "confirm"

    # And the opt-in works when it is deliberate.
    permissive = PolicyResolver(allow_autonomous_escalation=True)
    permissive.set("org", "", RouteClass.ESCALATE, "auto")
    assert permissive.resolve(RouteClass.ESCALATE).level.value == "auto"

    # The goal's posture is the *other* door onto the same floor, and an unattended goal may now
    # release a terminal gate. So the property this check exists for is asserted on the posture too:
    # choosing `supervised` must park every gate, and the autonomous posture must still refuse to pass
    # one that is not mechanically decidable.
    from ..goal import GoalPolicy, Posture

    assert GoalPolicy().posture is Posture.UNATTENDED, "the autonomous posture is the default"
    supervised = GoalPolicy(posture=Posture.SUPERVISED).effective()
    assert supervised.auto_approve is False and supervised.auto_hire is False, (
        "a supervised goal must narrow every automatic decision to nothing")
    assert not supervised.unattended, "a supervised goal must not answer its own gates"
    # An unattended goal narrows nothing — but the release path, not this policy, is what constrains a
    # terminal gate; see `Orchestrator._release_terminal_gate` and the phase-28 tests for its refusals.
    assert GoalPolicy().effective().unattended, "an unattended goal may answer the gates it can"

    return ("floor refused R-ESCALATE/R-CONFLICT below confirm; routine class loosened; opt-in "
            "honoured; a supervised goal narrows every automatic decision")


def check_router_asks_when_unsure() -> str:
    """A low-confidence or gated route must propose rather than act."""
    org = _org()
    router = Router(org)

    # A routine route acts.
    routine = router.route(RouteContext(node_id="fixer", skill="backend-developer",
                                        route_class=RouteClass.CONTRACT))
    assert routine.ok, f"a routine route should act: {routine.reason}"

    # A gated route proposes, with ranked candidates the Owner can judge.
    gated = router.route(RouteContext(node_id="x", skill="backend-developer",
                                      route_class=RouteClass.ESCALATE))
    assert gated.proposed and not gated.chosen
    assert gated.ranked(3), "a proposal must carry ranked candidates"
    assert gated.autonomy.value == "confirm"
    assert gated.decided_by, "the proposal must name which layer decided"

    # The producer is filtered out of a review.
    producer = org.candidates_for("backend-developer")[0]
    review = router.route(RouteContext(node_id="review", skill="code-reviewer",
                                       artifacts=["change"], producer_id=producer.id,
                                       is_reviewer=True))
    assert review.chosen != producer.id
    return f"routine acted; gated proposed {len(gated.ranked(3))} candidate(s); producer filtered"


def check_health_evidence_over_noise() -> str:
    """Noise must not quarantine; a severe fault must not hide behind a healthy average."""
    config = load()
    monitor = HealthMonitor(config=config)

    # A new hire survives its first failure.
    first = monitor.observe("newbie", outcome="failure", breached=True)
    assert first.current.value == "healthy", "a first failure must not quarantine a new hire"

    # Ten successes do not save an agent that then breaches three times consecutively.
    for _ in range(10):
        monitor.observe("mixed", outcome="success")
    for _ in range(3):
        transition = monitor.observe("mixed", outcome="success", breached=True)
    assert transition.current.value == "quarantined", "a hard trigger must override a healthy average"
    assert "breach" in transition.hard_trigger

    # Recovery requires evidence, and lands in degraded rather than healthy.
    from engine.org import ProbeResult

    recovering = HealthMonitor(config=config, probe=lambda a, s: ProbeResult(
        agent_id=a, skill=s, passed=True, cases_run=6, cases_passed=6))
    for _ in range(6):
        recovering.observe("bad", outcome="failure", breached=True)
    recovered = recovering.try_recover("bad", "backend-developer")
    assert recovered.current.value == "degraded", "trust is rebuilt with work, not granted on a test"
    return "min-sample held; hard trigger quarantined; probe restored to degraded"


def check_ledger_override_is_explicit() -> str:
    """An override must be marked, and an irreversible decision must not be re-made silently."""
    ledger = Ledger()
    ledger.record(gate="auth-strategy", choice="argon2id", rationale="memory hardness",
                  by="ag_1", reversible=False)
    try:
        ledger.record(gate="auth-strategy", choice="md5", rationale="faster")
    except Exception:
        pass
    else:
        raise AssertionError("an irreversible decision was re-made silently")

    try:
        ledger.supersede("auth-strategy", choice="bcrypt", rationale="")
    except Exception:
        pass
    else:
        raise AssertionError("an override was accepted with no rationale")

    ledger.supersede("auth-strategy", choice="bcrypt", rationale="legacy interop requirement")
    previous = ledger.history("auth-strategy")[0]
    assert previous.superseded and previous.marker == "SUPERSEDED"
    assert ledger.current("auth-strategy").choice == "bcrypt"
    # A superseded decision must not travel in a handoff, where it would contradict the live one.
    assert [d["choice"] for d in ledger.as_handoff_block()] == ["bcrypt"]
    return "irreversible re-make refused; unmarked override refused; superseded entry marked"


def check_context_thresholds() -> str:
    """The ladder must act differently in each band, and only evict when warranted."""
    def session_at(window: int, chars: int, turns: int) -> Session:
        s = Session(agent_id="a", window=window, output_reserve=0)
        for _ in range(turns):
            s.append_text("assistant", "x" * chars, tier=3)
        return s

    healthy = session_at(40000, 1000, 1)
    assert healthy.band.value == "healthy"
    assert compact(healthy).action.value == "none"

    warning = session_at(1000, 520, 6)
    assert 0.70 <= warning.saturation < 0.85, warning.saturation
    assert compact(warning).action.value == "prepare"
    assert warning.used_tokens > 0, "the warning band must not evict"

    critical = session_at(1000, 1200, 3)
    assert critical.band.value == "critical"
    result = compact(critical, target=0.5)
    assert result.action.value == "evict_tier3" and result.recovered > 0

    overflow = session_at(1000, 1000, 5)
    assert overflow.band.value == "overflow"
    assert compact(overflow, target=0.3).action.value == "emergency"
    return "healthy none; warning prepared; critical evicted; overflow emergency"


def check_planner_reports_gaps() -> str:
    """A plan may need a capability the roster lacks; the Owner must see which, before a run."""
    org = _org()
    plan = Planner(_source()).plan("Build a booking API with auth and payments",
                                   slug="eval-gaps")
    binder = Binder(org)
    gaps = binder.staffing_gaps(plan.manifest)
    assert gaps, "the fixture should leave at least one capability unstaffed"
    bound = binder.plan_bindings(plan.manifest, skip_unstaffed=True)
    staffed = {n["id"] for n in plan.nodes if n.get("skill")} - {g["node_id"] for g in gaps}
    assert set(bound) == staffed, "every staffed node must bind and every gap be reported"
    return f"{len(bound)} bound, {len(gaps)} gap(s) reported: {[g['skill'] for g in gaps]}"


def check_skill_enforceability() -> str:
    """Every skill must yield an enforceable bundle with criteria; a node cannot be ungated."""
    source = _source()
    names = source.names()
    assert len(names) > 300, f"expected the library, found {len(names)} skills"
    ungated: list[str] = []
    for name in names:
        try:
            bundle = source.load(name)
        except Exception as exc:  # noqa: BLE001 - any failure means the skill cannot be gated
            ungated.append(f"{name}: {exc}")
            continue
        if not bundle.contract.criteria:
            ungated.append(f"{name}: no criteria")
    assert not ungated, f"skills that cannot be gated: {ungated[:5]}"

    reviewer = source.load("code-reviewer")
    assert [i.id for i in reviewer.checklist] == [f"CR{i}" for i in range(1, 15)]
    assert len(reviewer.research_steps) == 8
    return f"{len(names)} skills all gated; code-reviewer yields CR1-CR14 and 8 research steps"


def check_memory_poisoning_guard() -> str:
    """Recalled memory must be labelled as context, never as instruction."""
    import tempfile

    from engine.memory import MemoryEntry, MemoryStore

    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "memory")
        store.write(MemoryEntry(workflow="w", run_id="r1", outcome="complete",
                                decisions=[{"gate": "auth", "choice": "argon2id",
                                            "rationale": "memory hardness"}],
                                cost_usd=None))
        block = store.context_block("w", limit=3)
        assert "CONTEXT ONLY" in block, "the recall block must be labelled"
        assert "background, not directives" in block
        assert "cost unknown" in block, "an unmeasured cost must not read as free"

        # And in a prompt, the label must survive into the context zone.
        prompt = assemble_session_prompt(task="work", recall=block)
        assert "CONTEXT ONLY" in prompt.context
        assert prompt.middle_zone_guardrails() == [], "our own notice is not a guardrail"
    return "recall labelled context-only in the store and in the prompt; unmeasured cost preserved"


def check_diagnostics_refuses_secrets() -> str:
    """A bundle is meant to be shared, so it must refuse to carry a credential."""
    import tempfile

    from engine.diagnostics import Diagnostics, DiagnosticsError

    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / ".agent_state"
        state.mkdir()
        (state / "trace.jsonl").write_text('{"note":"clean"}\n')
        diag = Diagnostics(run_id="eval", state_dir=state)
        diag.info("x", message="key sk-abcdefghijklmnopqrstuvwx")
        diag.close()
        # The log itself is redacted on the way in.
        assert "sk-" not in (state / "diagnostics.jsonl").read_text()

        diag.bundle(Path(td) / "ok.zip")
        assert (Path(td) / "ok.zip").is_file()

        # A leak in a *source* file stops the bundle.
        (state / "trace.jsonl").write_text('{"note":"key sk-111122223333444455556666"}\n')
        try:
            diag.bundle(Path(td) / "bad.zip")
        except DiagnosticsError:
            pass
        else:
            raise AssertionError("a bundle carried a secret")
        assert not (Path(td) / "bad.zip").exists(), "a refused bundle must not be left behind"
    return "log redacted on write; bundle assembled; a leaked source refused and left no file"


def check_idempotent_effects() -> str:
    """A retried effect must not be applied twice."""
    import tempfile

    from engine.idempotency import EffectJournal

    with tempfile.TemporaryDirectory() as td:
        journal = EffectJournal(path=Path(td) / "effects.jsonl")
        applied: list[int] = []

        def run(attempt: int) -> str:
            with journal.effect("write", run_id="r", node_id="n", attempt=attempt,
                                inputs_hash="h", target="src/app.py") as rec:
                if rec.replayed:
                    return "replay"
                applied.append(attempt)
                rec.record({"sha": "abc"})
                return "apply"

        assert run(1) == "apply"
        assert run(1) == "replay", "a retry of the same attempt must replay"
        assert run(2) == "apply", "a deliberate rework is a new effect"
        assert applied == [1, 2], f"the effect was applied {len(applied)} times, expected 2"
        journal.close()
    return "same-attempt retry replayed; new attempt applied; effect ran exactly twice"


def check_plan_validates_on_disk() -> str:
    """A plan that validates in memory must be readable by the library's own validator."""
    import subprocess
    import tempfile

    from engine.planner import emit_safe_yaml

    library = resolve()
    plan = Planner(_source()).plan("Build a booking API with auth", slug="eval-disk")
    assert plan.validation.valid
    text = emit_safe_yaml(plan.manifest)
    assert "{" not in text and "}" not in text, "flow maps are outside the Safe YAML Subset"

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "eval-disk.yaml"
        target.write_text(text)
        result = subprocess.run(
            [sys.executable, str(library.files.validator), "--manifest", str(target)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"validator rejected the file: {result.stdout}{result.stderr}"
    return "emitted as Safe YAML and accepted by the library's validator on disk"


#: name -> (description, check, category). The category groups the suite for reporting.
CHECKS: dict[str, tuple[str, Callable[[], str], str]] = {
    "loop-termination": ("every generated loop terminates at a reachable gate",
                         check_loop_termination, "graph"),
    "constraint-survival": ("a NEVER rule survives compaction, rotation and re-pinning",
                            check_constraint_survival, "context"),
    "rotation-refuses-when-impossible": ("an irreducible overflow is refused, not looped on",
                                         check_rotation_refuses_when_impossible, "context"),
    "reviewer-independence": ("a reviewer is never its own producer",
                              check_reviewer_independence, "independence"),
    "gate-integrity": ("no advance on an unevidenced or unrecorded decision",
                       check_gate_integrity, "gates"),
    "delegation-safety": ("the delegation invariants refuse their violations",
                          check_delegation_safety, "delegation"),
    "autonomy-floor": ("no setting disables every human gate",
                       check_autonomy_floor, "policy"),
    "router-asks-when-unsure": ("a gated or low-confidence route proposes rather than acts",
                                check_router_asks_when_unsure, "routing"),
    "health-evidence-over-noise": ("noise does not quarantine; a severe fault does not hide",
                                   check_health_evidence_over_noise, "health"),
    "ledger-override-is-explicit": ("an override is marked; an irreversible decision is not re-made",
                                    check_ledger_override_is_explicit, "gates"),
    "context-thresholds": ("the ladder acts differently in each band",
                           check_context_thresholds, "context"),
    "planner-reports-gaps": ("unstaffed plan nodes are reported before a run",
                             check_planner_reports_gaps, "graph"),
    "skill-enforceability": ("every library skill yields criteria, so no node is ungated",
                             check_skill_enforceability, "skills"),
    "memory-poisoning-guard": ("recall is context, never instruction",
                               check_memory_poisoning_guard, "memory"),
    "diagnostics-refuses-secrets": ("a shared bundle never carries a credential",
                                    check_diagnostics_refuses_secrets, "security"),
    "idempotent-effects": ("a retried effect is not applied twice",
                           check_idempotent_effects, "idempotency"),
    "plan-validates-on-disk": ("a plan is readable by the library's validator",
                               check_plan_validates_on_disk, "graph"),
}


# ── running ──────────────────────────────────────────────────────────────────


def load_scenarios(path: Path | None = None) -> dict[str, Any]:
    """Read the scenario file. Raises if a declared scenario has no implementation.

    A scenario named in the file but unimplemented is a *failure*, not a skip: a silently skipped
    safety check is how a suite decays into decoration.
    """
    target = path or SCENARIOS_PATH
    if not target.is_file():
        raise FileNotFoundError(f"scenario file not found: {target}")
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"scenario file {target} must be a JSON object")
    return data


def run_suite(*, only: Iterable[str] = (), scenarios: dict[str, Any] | None = None) -> ScenarioResult:
    """Run the behavioural suite.

    Parameters
    ----------
    only:
        Restrict to these scenario names, for a focused run while developing.
    scenarios:
        Pre-loaded scenarios; loaded from disk when omitted.
    """
    declared = scenarios if scenarios is not None else load_scenarios()
    wanted = set(only)
    result = ScenarioResult()

    for name, spec in sorted(declared.get("scenarios", {}).items()):
        if wanted and name not in wanted:
            continue
        started = time.time()
        entry = CHECKS.get(name)
        if entry is None:
            result.outcomes.append(Outcome(
                name=name, passed=False, check="unimplemented",
                error=f"scenario {name!r} is declared in scenarios.json but has no implementation",
                duration_ms=(time.time() - started) * 1000,
            ))
            continue
        description, check, category = entry
        try:
            detail = check()
            result.outcomes.append(Outcome(name=name, passed=True, check=category,
                                           detail=str(detail)[:300],
                                           duration_ms=(time.time() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 - a failing scenario is data, not a crash
            result.outcomes.append(Outcome(
                name=name, passed=False, check=category,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.time() - started) * 1000,
            ))
    return result


# ── the baseline gate ────────────────────────────────────────────────────────


def compare_to_baseline(result: ScenarioResult, baseline: dict[str, Any] | None, *,
                        subset: bool = False) -> list[str]:
    """Compare a run against a frozen baseline, returning the regressions.

    A *delta* comparison rather than an absolute one, because a suite that gained on one scenario and
    lost on three is a regression even at the same total — and a total is exactly what hides that.

    Parameters
    ----------
    subset:
        Set when the run covered only part of the suite (`--only`). A subset run cannot meaningfully
        compare against a full baseline, so the lost-coverage check is skipped — otherwise a focused
        development run would always "fail" on the scenarios it deliberately did not run.
    """
    if not baseline:
        return []
    previous = baseline.get("results") or {}
    regressions: list[str] = []
    for outcome in result.outcomes:
        was = previous.get(outcome.name)
        if was is None:
            continue
        if was.get("passed") and not outcome.passed:
            regressions.append(
                f"{outcome.name}: was passing, now failing — {outcome.error or 'no detail'}"
            )
    # A scenario that disappeared entirely is also a regression: coverage was lost. Only meaningful
    # for a full run, since a subset run is *asked* to skip the rest.
    if not subset:
        for name, was in previous.items():
            if was.get("passed") and name not in {o.name for o in result.outcomes}:
                regressions.append(f"{name}: was passing, no longer runs")
    return regressions


def freeze_baseline(result: ScenarioResult, path: Path | None = None) -> Path:
    """Record the current results as the baseline."""
    target = path or BASELINE_PATH
    payload = {
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **result.as_dict(),
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    """Run the suite, apply the gate, and report. Returns an exit code."""
    parser = argparse.ArgumentParser(
        description="Run the AgentOrg behavioural suite and apply the regression gate.",
    )
    parser.add_argument("--only", action="append", default=[],
                        help="run only this scenario (repeatable)")
    parser.add_argument("--freeze", action="store_true",
                        help="record the current results as the baseline")
    parser.add_argument("--baseline", help="a baseline file to compare against")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-gate", action="store_true",
                        help="report regressions without failing the exit code")
    args = parser.parse_args(argv)

    try:
        result = run_suite(only=args.only)
    except (FileNotFoundError, ValueError) as exc:
        print(f"cannot run the suite: {exc}", file=sys.stderr)
        return EXIT_USAGE

    baseline: dict[str, Any] | None = None
    baseline_path = Path(args.baseline) if args.baseline else BASELINE_PATH
    if baseline_path.is_file() and not args.freeze:
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            baseline = None

    regressions = compare_to_baseline(result, baseline, subset=bool(args.only))

    if args.freeze:
        frozen = freeze_baseline(result)
        print(f"baseline frozen at {frozen}: {result.passed}/{len(result.outcomes)} passing",
              file=sys.stderr)

    if args.json:
        payload = result.as_dict()
        payload["regressions"] = regressions
        payload["baseline"] = str(baseline_path) if baseline else None
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        by_category: dict[str, list[Outcome]] = {}
        for outcome in result.outcomes:
            by_category.setdefault(outcome.check, []).append(outcome)
        for category in sorted(by_category):
            print(f"\n{category}")
            for outcome in by_category[category]:
                mark = "PASS" if outcome.passed else "FAIL"
                print(f"  {mark} {outcome.name}")
                if outcome.passed:
                    print(f"       {outcome.detail}")
                else:
                    print(f"       {outcome.error}")
        print()
        print(f"{result.passed} passed, {result.failed} failed "
              f"({result.pass_rate:.0%} pass rate)")
        if args.only:
            print(f"(focused run: {len(result.outcomes)} of the suite; lost-coverage checking "
                  "is skipped for a subset)")
        if regressions:
            print()
            print("REGRESSIONS against the baseline:")
            for entry in regressions:
                print(f"  - {entry}")

    if regressions and not args.no_gate:
        print(file=sys.stderr)
        print(f"gate: {len(regressions)} regression(s) — refusing to pass", file=sys.stderr)
        return EXIT_REGRESSION
    return EXIT_OK if result.failed == 0 else EXIT_REGRESSION


if __name__ == "__main__":
    raise SystemExit(main())
