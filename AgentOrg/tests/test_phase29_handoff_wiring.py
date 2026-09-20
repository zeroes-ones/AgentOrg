#!/usr/bin/env python3
"""Phase 29 tests — the typed handoff, wired into the run path.

The engine shipped a complete handoff contract that *no production code called*: the executor passed
an untyped four-key dict across every node boundary, `Ledger.record` had no caller at all, and the
`handoff.*` events the flow board reads were emitted only by tests. So the contract was a
specification and the boundary it was written for did not exist.

What this file pins is that the boundary now exists and that its *refusals* are handled rather than
fatal. A handoff that fails a rule is a node whose work may not advance — which is the runner's
bounded rework loop's whole job — so every violation below is asserted to surface as a result and a
readable stop reason, never as an exception that discards work the node actually did.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.artifacts import ArtifactStore
from engine.bus import EventBus
from engine.config import load
from engine.diagnostics import Diagnostics
from engine.executor import ExecutorContext, NodeExecutor
from engine.flow import build_flow
from engine.gateway import Gateway
from engine.idempotency import EffectJournal
from engine.library import resolve
from engine.org import Handoff, Ledger, default_company
from engine.org.handoff import REQUIRED_FIELDS, validate_handoff
from engine.providers.fake import FakeProvider, json_reply
from engine.providers.base import ChatResponse, Usage
from engine.skills import FilesystemSkillSource
from engine.state import Workspace
from engine.tokens import TokenEstimator


# ── fixtures: one executor whose two nodes are joined by a real manifest edge ─


REVIEW_CRITERIA = [
    "Findings reference concrete files and lines",
    "Verdict states pass or changes_requested with rationale",
    "Severity grading matches the six-dimension severity model",
]
DEV_CRITERIA = [
    "Every accepted finding addressed with a concrete diff",
    "No finding silently dropped",
    "Open items declared in open questions rather than hidden",
]

#: Two nodes and one edge, so a boundary genuinely exists. A single-node manifest would make every
#: assertion below vacuous — the handoff has no successor to cross to.
_EDGES = [{"from": "dev", "to": "review", "when": "dev.status == done", "payload": "handoff-v1"}]


class RoutedProvider(FakeProvider):
    """Answers by *what was asked* rather than by call order.

    Ordering breaks the moment a repair turn or a rework pass changes the number of calls; answering
    by content is stable across that, so an assertion about the handoff is not also an assertion about
    how many model calls happened first.
    """

    def complete(self, request):
        if self._default is not None:
            return super().complete(request)
        self._record(request, streamed=False)
        text = (request.system or "") + "\n" + "\n".join(m.text for m in request.messages)
        if "code-reviewer" in text:
            reply = {
                "status": "done", "verdict": "pass", "summary": "no unresolved findings",
                "criteria_satisfied": [{"criterion": c, "satisfied": True,
                                        "evidence": "src/app.py:12"} for c in REVIEW_CRITERIA],
                "checklist": [{"id": "CR1", "status": "PASS", "evidence": "reviewed src/app.py"}],
                "artifacts": [{"type": "review-report", "path": "artifacts/review.md",
                               "content": "no unresolved findings"}],
            }
        else:
            reply = {
                "status": "done", "verdict": "fixed", "summary": "bound the SQL parameter",
                "criteria_satisfied": [{"criterion": c, "satisfied": True,
                                        "evidence": "src/app.py#12"} for c in DEV_CRITERIA],
                "checklist": [{"id": "PC1", "status": "PASS", "evidence": "pytest: 12 passed"}],
                "artifacts": [{"type": "change", "path": "src/app.py",
                               "content": "def login(user_id):\n    return query('?', user_id)\n"}],
                "decisions": [{"gate": "auth", "choice": "argon2id",
                               "rationale": "memory hardness", "reversible": False}],
                "open_questions": [{"question": "keep bcrypt for legacy rows?"}],
            }
        return ChatResponse(text=json_reply(reply).text,
                            usage=Usage(prompt_tokens=900, completion_tokens=200),
                            model=request.model, provider_id=self.provider_id)


@pytest.fixture(scope="module")
def library():
    return resolve()


@pytest.fixture(scope="module")
def skills(library):
    return FilesystemSkillSource(library)


@pytest.fixture
def config():
    return load()


def _executor(tmp_path, config, skills, library, *, manifest=None, nodes=None, ledger=None,
              diagnostics=True):
    """An executor over a two-node graph with a bus, a ledger and a diagnostics sink.

    All three sinks are real rather than mocked, because the point of the wiring is exactly that the
    crossing *reaches* them: a test that stubbed the bus would pass with no event ever emitted.
    """
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    state_dir = project / ".agent_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest or {
        "nodes": nodes or [{"id": "dev", "skill": "backend-developer", "outputs": ["change"]},
                           {"id": "review", "skill": "code-reviewer", "phase": "REVIEW"}],
        "edges": _EDGES,
    }
    (project / "wf.yaml").write_text(json.dumps(manifest))

    org = default_company(provider="ollama", model="qwen2.5-coder:7b", context_window=32768)
    for agent in org.agents.values():
        if agent.is_ai:
            agent.provider = "fake"
            agent.model = "fake-model"

    bus = EventBus(run_id="run_h", trace_path=state_dir / "trace.jsonl")
    gateway = Gateway(config, {"fake": RoutedProvider(provider_id="fake")},
                      estimator=TokenEstimator())
    ctx = ExecutorContext(
        org=org, gateway=gateway, skills=skills, workspace=project,
        store=ArtifactStore(workspace_root=project),
        journal=EffectJournal(path=state_dir / "effects.jsonl"),
        bus=bus, run_id="run_h", workflow="wf", config=config, manifest=manifest,
        manifest_path=project / "wf.yaml",
        ledger=ledger if ledger is not None else Ledger(path=state_dir / "ledger.jsonl"),
        diagnostics=Diagnostics(run_id="run_h", state_dir=state_dir) if diagnostics else None,
    )
    return NodeExecutor(ctx), project, state_dir


def _state(**extra) -> dict:
    base = {"nodes": {}, "artifacts": {}, "budget": {"steps_used": 0, "tokens_used": 0},
            "decisions": [], "open_questions": []}
    base.update(extra)
    return base


def _events(state_dir: pathlib.Path) -> list[dict]:
    path = state_dir / "trace.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _handoff_types(state_dir: pathlib.Path) -> list[str]:
    return [str(e["type"]) for e in _events(state_dir) if str(e["type"]).startswith("handoff.")]


def _dev_state() -> dict:
    """The run-state a second node would see, with `dev` already recorded as done.

    Written by hand rather than by running the node, because a rework pass after a refusal is a
    *second* call with the first pass's record still in run-state.
    """
    return _state(nodes={"dev": {"status": "done", "summary": "bound the SQL parameter"}})


# ── a successful edge: nine fields, clean validation, on disk, round-trippable ─


def test_a_successful_edge_produces_every_registry_field(tmp_path, config, skills, library):
    """The whole point of the registry: the receiver can rely on the shape."""
    executor, _, _ = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(), {"pass": 1})

    assert len(executor.handoffs) == 1
    handoff = next(iter(executor.handoffs.values()))
    for field in REQUIRED_FIELDS:
        assert field in handoff.payload, f"{field} was omitted from the payload"
    # The nine fields are present *and* populated where the data exists — an absent key and an empty
    # value are different failures, and the registry is about the first.
    assert handoff.payload["status"] == "done"
    assert handoff.payload["summary"]
    assert handoff.payload["artifacts"][0]["name"] == "change"
    assert handoff.payload["decisions"][0]["gate"] == "auth"
    assert handoff.payload["open_questions"][0]["question"].startswith("keep bcrypt")
    assert handoff.payload["verification_evidence"], "evidence must survive as a map"
    assert handoff.payload["next"]
    assert handoff.origin == "dev" and handoff.target == "review"


def test_the_context_field_carries_all_five_delegation_elements(tmp_path, config, skills, library):
    """A delegate missing an element re-discovers the problem from scratch and fixes it differently."""
    from engine.org.handoff import CONTEXT_ELEMENTS

    executor, _, _ = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(), {"pass": 1})
    context = next(iter(executor.handoffs.values())).payload["context"]

    for element in CONTEXT_ELEMENTS:
        assert element in context, f"delegation context is missing {element!r}"
    assert context["problem"]
    assert context["logs"]
    assert context["hypothesis"]


def test_the_budget_field_reports_the_runs_own_counters(tmp_path, config, skills, library):
    """The handoff must say what the crossing cost, from data already computed nearby."""
    executor, _, _ = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(budget={"steps_used": 7, "tokens_used": 1200}), {"pass": 1})

    budget = next(iter(executor.handoffs.values())).payload["budget"]
    assert budget["steps_used"] == 7
    assert budget["tokens_allocated"] >= 0
    assert 0.0 <= budget["session_saturation"] <= 1.0
    assert budget["context_window"] == 32768


def test_a_clean_edge_validates_at_every_stage(tmp_path, config, skills, library):
    """No rule may be tripped by the executor's own ordinary payload."""
    executor, _, _ = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(), {"pass": 1})
    handoff = next(iter(executor.handoffs.values()))

    assert validate_handoff(handoff, stage="propose").ok
    # It was accepted, started, delivered and fulfilled, so its state is the terminal one.
    assert handoff.state.value == "FULFILLED"


def test_the_handoff_is_persisted_and_round_trips(tmp_path, config, skills, library):
    """`from_dict` re-verifies the checksum (R4), so a round trip is an integrity check."""
    executor, project, state_dir = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(), {"pass": 1})
    handoff = next(iter(executor.handoffs.values()))

    path = state_dir / "handoffs" / f"{handoff.handoff_id}.json"
    assert path.is_file(), "every crossing must be persisted for the board and for review"
    restored = Handoff.from_dict(json.loads(path.read_text()))
    assert restored.handoff_id == handoff.handoff_id
    assert restored.checksum == handoff.checksum
    assert restored.payload == handoff.payload
    assert restored.origin == "dev" and restored.target == "review"
    # No temp file may be left behind, which is what makes the write atomic rather than merely fast.
    assert not any(".tmp." in p.name for p in (state_dir / "handoffs").iterdir())


def test_a_persisted_handoff_is_bounded(tmp_path, config, skills, library):
    """Rule R1 exists because a bloated receiver is the state rotation exists to avoid."""
    from engine.org.handoff import MAX_HANDOFF_TOKENS

    executor, _, state_dir = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(), {"pass": 1})
    handoff = next(iter(executor.handoffs.values()))

    assert handoff.estimated_tokens() <= MAX_HANDOFF_TOKENS
    size = (state_dir / "handoffs" / f"{handoff.handoff_id}.json").stat().st_size
    assert size < MAX_HANDOFF_TOKENS * 8, "a capped payload cannot write an unbounded blob"


def test_the_workspace_offers_a_handoffs_directory(tmp_path):
    """The layout is owned in one place, so the executor does not invent a path of its own."""
    ws = Workspace.for_project("handoffs", root=tmp_path / "projects")
    ws.ensure()
    assert ws.handoffs_dir.is_dir()
    assert ws.handoffs_dir == ws.state_dir / "handoffs"

# ── the events flow.py reads ─────────────────────────────────────────────────


def test_the_handoff_events_are_emitted_in_lifecycle_order(tmp_path, config, skills, library):
    """proposed -> accepted -> fulfilled -> verified, which is the contract's own state machine."""
    executor, _, state_dir = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(), {"pass": 1})

    assert _handoff_types(state_dir) == [
        "handoff.proposed", "handoff.accepted", "handoff.fulfilled", "handoff.verified"]


def test_every_handoff_event_carries_the_keys_the_board_reads(tmp_path, config, skills, library):
    """`flow._handoffs` keys on `handoff_id` and labels rows from `from_node`/`to_node`/`summary`."""
    executor, _, state_dir = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(), {"pass": 1})
    handoff = next(iter(executor.handoffs.values()))

    for event in _events(state_dir):
        if not str(event["type"]).startswith("handoff."):
            continue
        payload = event["payload"]
        assert payload["handoff_id"] == handoff.handoff_id
        assert payload["from_node"] == "dev"
        assert payload["to_node"] == "review"
        assert payload["summary"], "a board row with no summary says nothing crossed"


def test_the_flow_board_renders_a_row_from_the_emitted_trace(tmp_path, config, skills, library):
    """The board was populated only by tests before; now a real run fills it."""
    executor, project, _ = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(), {"pass": 1})
    executor.execute_node("review", _dev_state(), {"pass": 1})

    board = build_flow(Workspace.attach(project), org=executor.org)
    assert len(board["handoffs"]) == 2
    dev_edge = next(h for h in board["handoffs"] if h["from_node"] == "dev")
    # The row's endpoint fields are named for the nodes, which is the shape the emitted payload had
    # to match — `_handoffs` keys on `from_node`/`to_node` and nothing else.
    assert dev_edge["to_node"] == "review"
    assert dev_edge["state"] == "verified"
    assert dev_edge["tone"] == "good"
    assert dev_edge["summary"]


# ── refusals: a violation is handled, never fatal ────────────────────────────


def _assert_refused(result: dict) -> None:
    """A refusal is a reportable outcome, not an exception."""
    assert result["status"] == "needs_review"
    assert result["verdict"] == "contract-violation"
    assert result["summary"], "a refusal must name the rule that fired"


def test_r1_refuses_an_oversized_payload_as_a_result_not_a_crash(tmp_path, config, skills, library,
                                                                 monkeypatch):
    """R1: a receiver must not start its turn already bloated.

    The payload is inflated at the *assembly* boundary rather than by reaching into a built object,
    so the test exercises the path the engine actually takes: rule R1 is enforced by a refusal the
    caller can act on, and the oversized payload never reaches the successor.
    """
    executor, _, state_dir = _executor(tmp_path, config, skills, library)
    original = executor._build_handoff

    def _inflate(**kwargs):
        kwargs["artifacts"] = [{"path": f"src/f{i}.py", "content": "x" * 4000} for i in range(60)]
        return original(**kwargs)

    monkeypatch.setattr(executor, "_build_handoff", _inflate)
    result = executor.execute_node("dev", _state(), {"pass": 1})

    _assert_refused(result)
    assert "R1" in result["summary"]
    assert "R1 at propose" in result["diagnostics"]
    # Nothing was written for a payload that may not advance, and nothing crossed to the successor.
    assert not list((state_dir / "handoffs").glob("*.json"))
    assert executor._inbound_payload("review") is None


def test_r3_refuses_a_self_handoff_as_a_result_not_a_crash(tmp_path, config, skills, library):
    """R3: an agent must not hand off to itself — that is a loop or a mis-declared rotation."""
    # The edge points `dev` back at itself, which is a manifest defect the contract must catch rather
    # than a reason to kill the run.
    manifest = {"nodes": [{"id": "dev", "skill": "backend-developer", "outputs": ["change"]}],
                "edges": [{"from": "dev", "to": "dev", "when": "dev.status == done"}]}
    executor, _, state_dir = _executor(tmp_path, config, skills, library, manifest=manifest)

    result = executor.execute_node("dev", _state(), {"pass": 1})

    _assert_refused(result)
    assert "R3" in result["summary"]
    assert "handoff.proposed" in _handoff_types(state_dir)
    assert "handoff.rejected" in _handoff_types(state_dir)
    assert "handoff.accepted" not in _handoff_types(state_dir), "a refused payload must not cross"


def test_r4_refuses_a_checksum_mismatch_as_a_result_not_a_crash(tmp_path, config, skills, library,
                                                                monkeypatch):
    """R4: corrupted state aborts rather than propagating into the successor's work."""
    executor, _, _ = _executor(tmp_path, config, skills, library)
    original = executor._build_handoff

    def _tamper(**kwargs):
        handoff = original(**kwargs)
        # A mutation after the checksum was recorded is exactly what the rule exists to catch.
        handoff.payload["summary"] = "tampered between sender and receiver"
        return handoff

    monkeypatch.setattr(executor, "_build_handoff", _tamper)
    result = executor.execute_node("dev", _state(), {"pass": 1})

    _assert_refused(result)
    assert "R4" in result["summary"]


def test_r6_refuses_more_than_three_open_questions(tmp_path, config, skills, library):
    """R6: above three, uncertainty compounds faster than it can be resolved downstream.

    The pile the *successor* would inherit is the node's own questions joined with the run's, which is
    what the ceiling is about — a cap that counted only one of the two would measure nothing.
    """
    executor, _, _ = _executor(tmp_path, config, skills, library)
    state = _state(nodes={}, open_questions=[{"question": f"q{i}"} for i in range(5)])

    result = executor.execute_node("dev", state, {"pass": 1})

    _assert_refused(result)
    assert "R6" in result["summary"]


def test_r5_refuses_an_unrecorded_irreversible_decision_as_a_result(tmp_path, config, skills,
                                                                    library):
    """R5: an irreversible choice with no ledger entry is invisible to every later agent.

    Reachable from the ordinary path rather than only by hand-building a payload: a node whose trailer
    reports an irreversible decision without naming the gate it was made at must not be able to hand
    that choice on as though it had been recorded.
    """
    executor, _, _ = _executor(tmp_path, config, skills, library)
    executor.ctx.gateway.providers["fake"].set_default(json_reply({
        "status": "done", "verdict": "fixed", "summary": "chose a storage engine",
        "criteria_satisfied": [{"criterion": c, "satisfied": True, "evidence": "x"}
                               for c in DEV_CRITERIA],
        # Reversible false, no gate and no rationale: exactly the shape R5 refuses.
        "decisions": [{"choice": "append-only journal", "reversible": False}],
    }))

    result = executor.execute_node("dev", _state(), {"pass": 1})

    _assert_refused(result)
    assert "R5" in result["summary"]


def test_a_refusal_stops_the_successor_receiving_the_rejected_payload(tmp_path, config, skills,
                                                                     library):
    """A refused handoff must not become the next node's input — that would defeat the validation."""
    manifest = {"nodes": [{"id": "dev", "skill": "backend-developer", "outputs": ["change"]},
                          {"id": "review", "skill": "code-reviewer", "phase": "REVIEW"}],
                "edges": [{"from": "dev", "to": "dev", "when": "dev.status == done"},
                          {"from": "dev", "to": "review"}]}
    executor, _, _ = _executor(tmp_path, config, skills, library, manifest=manifest)
    executor.execute_node("dev", _state(), {"pass": 1})

    assert "review" not in executor.inbound
    assert executor._inbound_payload("review") is None


def test_the_receiver_revalidates_before_it_consumes(tmp_path, config, skills, library, monkeypatch):
    """R4 belongs to the receiver — a receiver that trusted the sender would not be checking.

    The payload is mutated *after* the sender's own validation, which is precisely the window R4
    exists to close, and the successor must end up with no input rather than with corrupt state.
    """
    executor, _, state_dir = _executor(tmp_path, config, skills, library)
    original = executor._handoff_for_consumer

    def _tamper(handoff, node_id):
        handoff.payload["summary"] = "changed in flight"
        return original(handoff, node_id)

    monkeypatch.setattr(executor, "_handoff_for_consumer", _tamper)
    result = executor.execute_node("dev", _state(), {"pass": 1})

    # The sender's own crossing stood — it was valid when it crossed — and the *receiver* refused.
    assert result["status"] == "done"
    assert "review" not in executor.inbound
    assert "handoff.rejected" in _handoff_types(state_dir)


def test_a_refusal_is_recorded_as_a_contract_action_for_the_stop_reason(tmp_path, config, skills,
                                                                        library):
    """`_derive_stop_reason` lifts a `contract` log action into a readable cause."""
    from engine.diagnostics import Diagnostics

    diagnostics = Diagnostics(run_id="run_h")
    executor, _, _ = _executor(
        tmp_path, config, skills, library,
        manifest={"nodes": [{"id": "dev", "skill": "backend-developer"}],
                  "edges": [{"from": "dev", "to": "dev"}]},
        diagnostics=False)
    executor.ctx.diagnostics = diagnostics

    executor.execute_node("dev", _state(), {"pass": 1})
    contract = [r for r in diagnostics.tail() if r["event"] == "contract"]

    assert contract, "a refused handoff must be logged in the runner's own `contract` shape"
    assert contract[-1]["detail"]["stage"] == "propose"
    assert contract[-1]["detail"]["rules"] == ["R3"]

    # And the stop reason the orchestrator derives from that entry is a real explanation.
    from engine.orchestrator import _derive_stop_reason

    class Outcome:
        killed = False
        error = ""

    reason = _derive_stop_reason(
        {"outcome": "contract-violation", "phase": "escalated",
         "nodes": {"dev": {"status": "needs_review", "verdict": "contract-violation"}},
         "log": [{"node": "dev", "action": "contract",
                  "detail": contract[-1]["message"]}]},
        Outcome(), {})
    assert "dev" in reason and "contract" in reason


# ── the ledger receives the decision ─────────────────────────────────────────


def test_the_ledger_records_the_crossing_at_the_edge_gate(tmp_path, config, skills, library):
    """`Ledger.record` had no production caller; this is what makes "who handed what, and why"
    answerable after the fact."""
    ledger = Ledger()
    executor, _, _ = _executor(tmp_path, config, skills, library, ledger=ledger)
    executor.execute_node("dev", _state(), {"pass": 1})

    entry = ledger.current("handoff:dev->review")
    assert entry is not None, "the crossing must be recorded at the edge, not only at the node"
    assert entry.choice.startswith("done")
    assert entry.rationale, "a decision with no rationale cannot be evaluated, only obeyed"
    assert entry.rejected_alternatives
    assert entry.reversible is True, "the loop must be able to rework a handoff it recorded"


def test_the_ledger_handoff_block_reflects_the_crossing(tmp_path, config, skills, library):
    """`as_handoff_block` is what a successor's prompt is built from, so it must carry the entry."""
    ledger = Ledger()
    executor, _, _ = _executor(tmp_path, config, skills, library, ledger=ledger)
    executor.execute_node("dev", _state(), {"pass": 1})

    block = ledger.as_handoff_block()
    assert [b["gate"] for b in block] == ["handoff:dev->review"]
    assert block[0]["reversible"] is True
    assert block[0]["by"], "the ledger must name who decided"


def test_the_ledger_journal_survives_a_reload(tmp_path, config, skills, library):
    """The record is only durable if it is on disk; the ledger's own journal is what makes it so."""
    state_dir = tmp_path / "project" / ".agent_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(path=state_dir / "ledger.jsonl")
    executor, _, _ = _executor(tmp_path, config, skills, library, ledger=ledger)
    executor.execute_node("dev", _state(), {"pass": 1})
    ledger.close()

    reopened = Ledger(path=state_dir / "ledger.jsonl")
    assert reopened.current("handoff:dev->review") is not None
    reopened.close()


def test_a_refused_crossing_is_not_recorded_as_a_decision(tmp_path, config, skills, library):
    """Recording a refused handoff as a decision would make a rejection look like a delivery."""
    ledger = Ledger()
    executor, _, _ = _executor(
        tmp_path, config, skills, library, ledger=ledger,
        manifest={"nodes": [{"id": "dev", "skill": "backend-developer"}],
                  "edges": [{"from": "dev", "to": "dev"}]})
    executor.execute_node("dev", _state(), {"pass": 1})

    assert ledger.all() == []


# ── the successor consumes the typed handoff, not the raw dict ───────────────


def test_the_successors_prompt_is_built_from_the_validated_handoff(tmp_path, config, skills, library):
    """The consuming half of the wiring: node N+1's input is the handoff N produced."""
    executor, _, _ = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(), {"pass": 1})

    inbound = executor.inbound.get("review")
    assert inbound is not None
    assert inbound.target == "review", "the receiver's copy is addressed to the receiving node"
    assert inbound.payload["summary"] == "bound the SQL parameter"

    executor.execute_node("review", _dev_state(), {"pass": 1})
    prompt = executor.ctx.gateway.providers["fake"].requests[-1].last_user_text
    assert "bound the SQL parameter" in prompt, "the upstream summary must reach the successor"
    assert "keep bcrypt for legacy rows?" in prompt, "and so must what upstream left open"


def test_a_reviewer_receives_the_upstream_artifacts_it_must_judge(tmp_path, config, skills, library):
    """An artifact is the thing a review is *of*; losing it on the boundary makes the review empty."""
    executor, _, _ = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(), {"pass": 1})

    artifacts = executor.inbound["review"].payload["artifacts"]
    assert any(a.get("path") == "src/app.py" for a in artifacts)


def test_the_inbound_payload_is_a_copy_not_the_senders_object(tmp_path, config, skills, library):
    """Mutating the receiver's view must not rewrite the record that was persisted."""
    executor, _, state_dir = _executor(tmp_path, config, skills, library)
    executor.execute_node("dev", _state(), {"pass": 1})
    sender = next(iter(executor.handoffs.values()))
    persisted = json.loads(
        (state_dir / "handoffs" / f"{sender.handoff_id}.json").read_text())

    executor.inbound["review"].payload["summary"] = "mutated by the receiver"
    assert persisted["payload"]["summary"] == "bound the SQL parameter"
    assert json.loads(
        (state_dir / "handoffs" / f"{sender.handoff_id}.json").read_text()
    )["payload"]["summary"] == "bound the SQL parameter"


def test_non_negotiable_constraints_survive_a_two_hop_chain(tmp_path, config, skills, library):
    """R2's whole purpose: a constraint that is dropped one hop later is dropped."""
    manifest = {"nodes": [{"id": "dev", "skill": "backend-developer", "outputs": ["change"]},
                          {"id": "review", "skill": "code-reviewer", "phase": "REVIEW"},
                          {"id": "ship", "skill": "backend-developer"}],
                "edges": [{"from": "dev", "to": "review", "when": "dev.status == done"},
                          {"from": "review", "to": "ship", "when": "review.status == done"}]}
    executor, _, _ = _executor(tmp_path, config, skills, library, manifest=manifest)
    state = _state(constraints=[{"value": "NEVER log tokens", "non_negotiable": True}])

    executor.execute_node("dev", state, {"pass": 1})
    state["nodes"]["dev"] = {"status": "done", "summary": "bound the SQL parameter"}
    executor.execute_node("review", state, {"pass": 1})

    for handoff in executor.handoffs.values():
        values = [c["value"] for c in handoff.payload["constraints"]]
        assert "NEVER log tokens" in values, (
            f"{handoff.origin}->{handoff.target} dropped a non-negotiable constraint")
        assert validate_handoff(handoff, stage="propose").ok


def test_a_gate_does_not_produce_a_handoff(tmp_path, config, skills, library):
    """A gate does no content work, so it has nothing to hand on — and an invented handoff would put
    an edge on the board that no work crossed."""
    manifest = {"nodes": [{"id": "dev", "skill": "backend-developer"}],
                "gates": [{"id": "release", "type": "gate", "kind": "human",
                           "requires": ["change"], "description": "owner approval"}],
                "edges": [{"from": "dev", "to": "release"}]}
    executor, _, state_dir = _executor(tmp_path, config, skills, library, manifest=manifest)
    result = executor.execute_node("release", _state(), {})

    assert result["verdict"] == "awaiting_owner"
    assert executor.handoffs == {}
    assert _handoff_types(state_dir) == []


def test_one_handoff_per_node_even_when_the_node_fans_out(tmp_path, config, skills, library):
    """A fan-out's items are how *one* node reached its answer, not separate edges in the graph."""
    manifest = {"nodes": [{"id": "reviewall", "skill": "code-reviewer", "phase": "REVIEW",
                           "fanout": "Review {{item}} for regressions.",
                           "items": ["src/a.py", "src/b.py"], "inputs": [], "outputs": ["review-report"]},
                          {"id": "ship", "skill": "backend-developer"}],
                "edges": [{"from": "reviewall", "to": "ship", "when": "reviewall.status == done"}]}
    executor, _, state_dir = _executor(tmp_path, config, skills, library, manifest=manifest)
    result = executor.execute_node("reviewall", _state(), {"pass": 1})

    assert result["fanout"]["count"] == 2
    assert len(executor.handoffs) == 1, "two items are not two node boundaries"
    assert _handoff_types(state_dir) == [
        "handoff.proposed", "handoff.accepted", "handoff.fulfilled", "handoff.verified"]
