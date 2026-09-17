#!/usr/bin/env python3
"""Phase 9 tests — the swarm, and the context that crosses the process boundary.

Two things are tested here that were previously untrue in execution:

1. **A swarm actually runs several agents.** `BindingPolicy.SWARM` bound every candidate and the
   executor ran only the first, so the policy was dead code. A quorum that never votes is worse than
   no quorum, because it reads as a guarantee.
2. **A run's roster, bindings and skill roots reach the subprocess that executes.** They were
   discarded at the process boundary, so a hired agent and an authored skill were invisible to the
   only process that could use them.

The regression these guard against is specific: a claim that holds in the API and not in execution.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load
from engine.executor import ExecutorContext, NodeExecutor
from engine.org.binding import declared_policy
from engine.gateway import Gateway
from engine.library import resolve
from engine.org import default_company
from engine.org.agent import AgentLevel, AgentSpec
from engine.org.binding import BindingPolicy
from engine.planner import emit_safe_yaml
from engine.providers.base import ChatResponse, FinishReason, Usage
from engine.providers.fake import FakeProvider
from engine.runcontext import RunContext, RunContextError, read, write
from engine.skills import FilesystemSkillSource
from engine.tokens import TokenEstimator


@pytest.fixture(scope="module")
def config():
    return load()


@pytest.fixture(scope="module")
def source():
    return FilesystemSkillSource(resolve())


def _reviewer_criteria(source) -> list[str]:
    return list(source.load("code-reviewer").contract.criteria)


class VotingProvider(FakeProvider):
    """A provider where each named agent votes its own way.

    The vote is chosen by the agent's name appearing in the *user turn*, which is where identity now
    lives. It used to be read from the system prompt — and that move is the whole point of the cache
    work: the system prompt is the shared, cacheable head of every request, so an agent's name there
    would make two voters' prefixes differ from character 8 and cost the swarm its discount.

    Reading it from the message exercises the same path a real swarm takes, and would fail loudly if
    the identity ever drifted back into the prefix.
    """

    def __init__(self, votes: dict[str, str], **kwargs):
        super().__init__(**kwargs)
        self.votes = votes
        self.seen: list[str | None] = []

    def complete(self, request):
        system = request.system or ""
        body = "\n".join(m.text for m in request.messages)
        # Match the identity *line*, not a bare substring. A bare search for "Rita" also matches
        # prose about other agents, and it would pick whichever name happened to come first in the
        # dict rather than the agent that actually ran — which is how a correct prompt can look like
        # a mis-attributed one.
        who = next((name for name in self.votes
                    if re.search(rf"WHO YOU ARE\s+You are {re.escape(name)}\.", body)), None)
        # A guard, not an assertion in the fixture: if the name is back in the system prompt the
        # cache alignment has regressed, and the swarm tests should say so rather than quietly pass.
        if any(name.lower() in system.lower() for name in self.votes):
            raise AssertionError(
                "an agent's name is in the system prompt again, which breaks the shared prefix")
        self.seen.append(who)
        criteria = list(self._criteria or [])
        payload = {
            "status": "done",
            "verdict": self.votes.get(who, "pass"),
            "summary": f"{who} voted {self.votes.get(who)}",
            "criteria_satisfied": [{"criterion": c, "satisfied": True, "evidence": "src/app.py:12"}
                                   for c in criteria],
            "checklist": [{"id": i, "status": "PASS", "evidence": "checked"}
                          for i in (self._checklist or [])],
            "artifacts": [{"type": "review-report", "path": "artifacts/review.md",
                           "content": f"verdict from {who}"}],
        }
        return ChatResponse(text=json.dumps(payload),
                            usage=Usage(prompt_tokens=10, completion_tokens=5,
                                        reported_cost_usd=0.0),
                            model=request.model, provider_id=self.provider_id,
                            finish_reason=FinishReason.STOP)


def make_org(source, reviewers=("Rita", "Rob", "Rex")):
    """The built-in company plus named reviewers, so a swarm has several holders of one skill."""
    org = default_company(provider="fake", model="fake-model", context_window=32768)
    for index, name in enumerate(reviewers):
        org.hire(AgentSpec(id=f"ag_v{index}", name=name, title="Code Reviewer",
                           skills=["code-reviewer"], provider="fake", model="fake-model",
                           context_window=32768, role="reviewer", level=AgentLevel.STAFF))
    return org


def make_executor(config, source, votes, *, policy=None, manifest=None, workspace=None,
                  reviewers=None):
    provider = VotingProvider(votes, provider_id="fake")
    provider._criteria = _reviewer_criteria(source)          # noqa: SLF001 - test wiring
    provider._checklist = source.load("code-reviewer").checklist_ids()  # noqa: SLF001
    ctx = ExecutorContext(
        org=make_org(source, reviewers=reviewers) if reviewers else make_org(source),
        gateway=Gateway(config, {"fake": provider}, estimator=TokenEstimator()),
        skills=source,
        workspace=workspace or pathlib.Path(tempfile.mkdtemp()),
        config=config, run_id="run", workflow="wf",
        policies={"reviewer": policy} if policy else {},
        manifest_path=manifest,
    )
    state = {"nodes": {"reviewer": {"status": "pending"}}, "artifacts": {},
             "budget": {"steps_used": 0, "iterations": {}}}
    return NodeExecutor(ctx), provider, state


# ── the swarm actually votes ─────────────────────────────────────────────────


def test_a_swarm_runs_every_bound_agent(config, source):
    """The defect: SWARM bound N agents and the executor ran one."""
    executor, provider, state = make_executor(config, source,
                                              {"Rita": "pass", "Rob": "pass", "Rex": "pass"},
                                              policy=BindingPolicy.SWARM)
    result = executor.execute_node("reviewer", state, {"skill": "code-reviewer"})
    voters = [name for name in provider.seen if name]
    assert len(voters) == 3, f"expected three voters, saw {voters}"
    assert result["swarm"]["quorum"] == 2


def test_the_majority_verdict_wins(config, source):
    executor, _, state = make_executor(config, source,
                                       {"Rita": "changes_requested", "Rob": "pass", "Rex": "pass"},
                                       policy=BindingPolicy.SWARM)
    result = executor.execute_node("reviewer", state, {"skill": "code-reviewer"})
    assert result["verdict"] == "pass"
    assert result["swarm"]["tally"] == {"pass": 2, "changes_requested": 1}


def test_a_minority_cannot_outvote_a_majority(config, source):
    """The reason a swarm exists: one confident dissent does not carry the node."""
    executor, _, state = make_executor(config, source,
                                       {"Rita": "pass",
                                        "Rob": "changes_requested", "Rex": "changes_requested"},
                                       policy=BindingPolicy.SWARM)
    result = executor.execute_node("reviewer", state, {"skill": "code-reviewer"})
    assert result["verdict"] == "changes_requested"


def test_a_single_agent_run_is_not_a_swarm(config, source):
    """Without the policy the node is bound normally, and must not gain swarm bookkeeping."""
    executor, provider, state = make_executor(config, source, {"Rita": "pass"})
    result = executor.execute_node("reviewer", state, {"skill": "code-reviewer"})
    assert result.get("swarm") is None
    assert len(provider.seen) == 1, f"exactly one call, saw {provider.seen}"


def test_a_disagreeing_swarm_keeps_the_disagreement_visible(config, source):
    """A 2–1 split is a real split, and the tally is what makes it auditable."""
    executor, _, state = make_executor(config, source,
                                       {"Rita": "pass", "Rob": "changes_requested",
                                        "Rex": "changes_requested"},
                                       policy=BindingPolicy.SWARM)
    result = executor.execute_node("reviewer", state, {"skill": "code-reviewer"})
    assert result["swarm"]["unique_answers"] == 2
    assert result["swarm"]["tally"] == {"changes_requested": 2, "pass": 1}
    # Every voter's own verdict is retained, so "who dissented?" is answerable.
    assert len(result["swarm"]["verdicts"]) == 3


def test_an_unreached_quorum_is_flagged_in_the_summary(config, source):
    """The safety path: three distinct answers is not consensus.

    Reachable only with three or more distinct values, which a binary verdict cannot produce — so
    this drives a worker node whose voters each report a different status.
    """
    provider = VotingProvider({"Rita": "pass"}, provider_id="fake")
    provider._criteria = _reviewer_criteria(source)          # noqa: SLF001
    provider._checklist = source.load("code-reviewer").checklist_ids()  # noqa: SLF001
    statuses = iter(["done", "blocked", "needs_review"])
    original = VotingProvider.complete

    def distinct(request):
        response = original(provider, request)
        payload = json.loads(response.text)
        payload["status"] = next(statuses, "done")
        payload["verdict"] = payload["status"]            # make the verdict distinct too
        response.text = json.dumps(payload)
        return response

    provider.complete = distinct                            # type: ignore[method-assign]
    ctx = ExecutorContext(
        org=make_org(source),
        gateway=Gateway(config, {"fake": provider}, estimator=TokenEstimator()),
        skills=source, workspace=pathlib.Path(tempfile.mkdtemp()),
        config=config, run_id="run", workflow="wf",
        policies={"reviewer": BindingPolicy.SWARM},
    )
    state = {"nodes": {"reviewer": {"status": "pending"}}, "artifacts": {},
             "budget": {"steps_used": 0, "iterations": {}}}
    result = NodeExecutor(ctx).execute_node("reviewer", state, {"skill": "code-reviewer"})
    assert result["swarm"]["agreement"] is False
    assert "quorum" in result["summary"].lower(), result["summary"]


def test_swarm_cost_is_summed_over_every_voter(config, source):
    """Reporting only the winner's tokens would understate what the node cost."""
    executor, _, state = make_executor(config, source,
                                       {"Rita": "pass", "Rob": "pass", "Rex": "pass"},
                                       policy=BindingPolicy.SWARM)
    result = executor.execute_node("reviewer", state, {"skill": "code-reviewer"})
    usage = result["usage"]
    assert usage["tokens_in"] == 30, "three voters at 10 prompt tokens each"
    assert usage["tokens_out"] == 15


def test_the_swarm_size_is_capped_and_the_cap_is_reported(config, source):
    """Voting multiplies cost and latency, so the cap is explicit rather than silent."""
    many = tuple(f"V{i}" for i in range(7))
    executor, provider, state = make_executor(config, source, {n: "pass" for n in many},
                                              policy=BindingPolicy.SWARM)
    result = executor.execute_node("reviewer", state, {"skill": "code-reviewer"})
    assert len(result["swarm"]["voters"]) <= 3
    assert result["swarm"]["capped_from"], "a truncation must be reported, not hidden"


def test_a_manifest_node_can_request_a_swarm(config, source):
    """A plan that wants three reviewers should say so in the graph."""
    workspace = pathlib.Path(tempfile.mkdtemp())
    manifest = {"name": "wf", "version": "1.0.0", "start": "reviewer",
                "nodes": [{"id": "reviewer", "skill": "code-reviewer", "binding": "swarm",
                           "phase": "REVIEW", "inputs": ["change"], "outputs": ["review-report"]}]}
    (workspace / "wf.yaml").write_text(emit_safe_yaml(manifest))
    executor, provider, state = make_executor(config, source,
                                              {"Rita": "pass", "Rob": "pass", "Rex": "pass"},
                                              manifest=workspace / "wf.yaml", workspace=workspace)
    result = executor.execute_node("reviewer", state, {})
    assert result["swarm"]["tally"] == {"pass": 3}


def test_a_manifest_policy_typo_falls_back_rather_than_failing(config, source):
    """A typo in an authored manifest should not kill a run at bind time."""
    assert declared_policy({"binding": "swrm"}) is None
    assert declared_policy({"binding": "swarm"}) is BindingPolicy.SWARM
    assert declared_policy({"policy": "SWARM"}) is BindingPolicy.SWARM
    assert declared_policy({}) is None


def test_the_planner_side_binder_honours_a_declared_swarm(config, source):
    """The regression: `plan_bindings` read only the caller's map, so a manifest that declared
    `binding: swarm` was bound as a single agent and the declaration was silently ignored."""
    from engine.org.binding import Binder

    org = make_org(source)
    manifest = {"name": "wf", "version": "1.0.0", "start": "reviewer",
                "nodes": [{"id": "reviewer", "skill": "code-reviewer", "binding": "swarm",
                           "phase": "REVIEW", "inputs": ["change"],
                           "outputs": ["review-report"]}]}
    binding = Binder(org).plan_bindings(manifest, skip_unstaffed=True)["reviewer"]
    assert binding.policy is BindingPolicy.SWARM
    assert len(binding.agents) > 1, "a swarm must bind every eligible agent"


def test_an_explicit_override_still_beats_the_manifest(config, source):
    """Precedence: caller override, then the node's declaration, then the default."""
    from engine.org.binding import Binder

    org = make_org(source)
    manifest = {"name": "wf", "version": "1.0.0", "start": "reviewer",
                "nodes": [{"id": "reviewer", "skill": "code-reviewer", "binding": "swarm",
                           "phase": "REVIEW"}]}
    # A caller that pins the node must win over the node's own swarm declaration.
    target = org.candidates_for("code-reviewer")[0].id
    binding = Binder(org).plan_bindings(
        manifest, policies={"reviewer": BindingPolicy.PINNED},
        pins={"reviewer": target}, skip_unstaffed=True)["reviewer"]
    assert binding.policy is BindingPolicy.PINNED
    assert binding.agents == [target]


def test_a_declared_swarm_survives_into_the_run_context(config, source):
    """The end of the chain: a manifest-declared swarm must still be a swarm when it executes.

    The run context takes precedence over the manifest at execution, so recording the wrong policy
    there would downgrade the node even though the manifest asked for a vote.
    """
    from engine.org.binding import Binder

    org = make_org(source)
    manifest = {"name": "wf", "version": "1.0.0", "start": "reviewer",
                "nodes": [{"id": "reviewer", "skill": "code-reviewer", "binding": "swarm",
                           "phase": "REVIEW"}]}
    bindings = Binder(org).plan_bindings(manifest, skip_unstaffed=True)
    context = RunContext(org=org.to_dict(),
                         bindings={k: v.as_dict() for k, v in bindings.items()}, skill_roots=[])
    workspace = pathlib.Path(tempfile.mkdtemp())
    write(workspace, context)
    recorded = read(workspace).bindings["reviewer"]
    assert recorded["policy"] == "swarm"
    assert len(recorded["agents"]) > 1
    # And that recorded policy is what the executor would act on.
    assert BindingPolicy(recorded["policy"]) is BindingPolicy.SWARM


def test_the_swarm_cap_is_configurable(config, source):
    """The regression: `executor.swarm_max_voters` was read via getattr on a section that did not
    exist, so the knob was documented as configurable and silently was not."""
    from engine.config import ExecutorConfig

    assert config.executor.swarm_max_voters == 3
    assert hasattr(config, "executor"), "the executor config section must exist to be settable"
    # A bad value is refused rather than producing a swarm that cannot vote.
    with pytest.raises(Exception):
        ExecutorConfig(swarm_max_voters=0)


def test_raising_the_cap_lets_more_voters_run(config, source):
    """Six reviewers hired, cap raised to 5, so five must vote rather than the default three."""
    many = tuple(f"V{i}" for i in range(6))
    executor, provider, state = make_executor(config, source, {n: "pass" for n in many},
                                              policy=BindingPolicy.SWARM,
                                              reviewers=many)
    original = config.executor.swarm_max_voters
    try:
        # The default cap first, so the assertion proves the knob changed the outcome.
        assert len(executor.execute_node("reviewer", state,
                                         {"skill": "code-reviewer"})["swarm"]["voters"]) == 3
        config.executor.swarm_max_voters = 5
        result = executor.execute_node("reviewer", state, {"skill": "code-reviewer"})
        assert len(result["swarm"]["voters"]) == 5
    finally:
        config.executor.swarm_max_voters = original


# ── the run context ──────────────────────────────────────────────────────────


def test_a_run_context_round_trips():
    context = RunContext(org={"agents": []}, bindings={"n": {"agents": ["a"], "policy": "swarm"}},
                         skill_roots=["/tmp/skills"], instructions=["be careful"],
                         constraints=["never log a key"])
    workspace = pathlib.Path(tempfile.mkdtemp())
    write(workspace, context)
    loaded = read(workspace)
    assert loaded is not None
    assert loaded.bindings["n"]["policy"] == "swarm"
    assert loaded.skill_roots == ["/tmp/skills"]
    assert loaded.constraints == ["never log a key"]


def test_a_missing_context_is_none_not_an_error():
    """A workspace that predates the handoff must still run, on the built-in company."""
    assert read(pathlib.Path(tempfile.mkdtemp())) is None


def test_a_version_mismatch_is_refused():
    """Pairing a plugin with a context from another build must not be silent."""
    with pytest.raises(RunContextError, match="version"):
        RunContext.from_dict({"version": 999, "org": {}})


def test_the_context_carries_the_hired_roster(config, source):
    """The defect: the executing process rebuilt the built-in company and lost every hire."""
    from engine.org import Org

    org = make_org(source)
    context = RunContext(org=org.to_dict(), bindings={}, skill_roots=[])
    workspace = pathlib.Path(tempfile.mkdtemp())
    write(workspace, context)
    rebuilt = Org.from_dict(read(workspace).org)
    names = {a.name for a in rebuilt.agents.values()}
    assert {"Rita", "Rob", "Rex"} <= names
    assert "Priya" in names, "the built-ins must survive the round trip too"


def test_the_context_names_every_binding_the_orchestrator_decided(config, source):
    from engine.org.binding import NodeBinding

    binding = NodeBinding(node_id="reviewer", skill="code-reviewer",
                          agents=["ag_v0", "ag_v1", "ag_v2"], policy=BindingPolicy.SWARM)
    context = RunContext(org={}, bindings={"reviewer": binding.as_dict()}, skill_roots=[])
    workspace = pathlib.Path(tempfile.mkdtemp())
    write(workspace, context)
    loaded = read(workspace)
    # The policy has to survive as data, because the plugin decides how many agents to run from it.
    assert loaded.bindings["reviewer"]["policy"] == "swarm"
    assert loaded.bindings["reviewer"]["agents"] == ["ag_v0", "ag_v1", "ag_v2"]
