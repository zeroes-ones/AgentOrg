#!/usr/bin/env python3
"""Phase 10 tests — the task pool: work agents pull, capability-routed.

The pool exists so work can be *discovered* rather than only planned, and so capability plus
availability decide who does it rather than a static score. The tests focus on the properties that
make a pool trustworthy: a capable agent gets the work, an incapable one does not, a dead worker's
task comes back, and a completion that does not match its schema is refused rather than stored.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.org.agent import AgentLevel, AgentSpec
from engine.pool import PoolError, TaskPool, TaskState, validate_output


def agent(agent_id: str, name: str, skills: list[str],
          capabilities: list[str] | None = None) -> AgentSpec:
    return AgentSpec(id=agent_id, name=name, title="Worker", skills=list(skills),
                     provider="ollama", model="qwen2.5-coder:14b", context_window=32768,
                     level=AgentLevel.SENIOR,
                     capabilities=list(capabilities or ["read:*"]))


@pytest.fixture
def pool(tmp_path):
    return TaskPool(tmp_path / "pool.json")


@pytest.fixture
def dba():
    return agent("ag_dba", "Dana", ["database-designer"], ["read:*", "write:src/**"])


@pytest.fixture
def reviewer():
    return agent("ag_rev", "Rita", ["code-reviewer"], ["read:*"])


# ── capability routing: the reason a pool exists ─────────────────────────────


def test_only_a_capable_agent_can_claim_a_task(pool, dba, reviewer):
    pool.create("migrate the schema", required_skills=["database-designer"])
    assert [t.description for t in pool.eligible(dba)] == ["migrate the schema"]
    assert pool.eligible(reviewer) == [], "a reviewer must not be able to take a migration"


def test_an_incapable_agent_gets_nothing_rather_than_a_refusal(pool, reviewer):
    """'Nothing for me right now' is the normal state of a pull-based worker, not an error."""
    pool.create("migrate the schema", required_skills=["database-designer"])
    assert pool.claim(reviewer) is None


def test_a_capability_wildcard_covers_its_namespace(pool):
    """`write:*` grants `write:src/**`, or least privilege becomes unusable."""
    holder = agent("ag_w", "Wanda", ["backend-developer"], ["read:*", "write:*"])
    pool.create("write the handler", required_skills=["backend-developer"],
                required_capabilities=["write:src/api/**"])
    assert pool.claim(holder) is not None


def test_a_missing_capability_blocks_the_claim(pool, dba):
    pool.create("deploy it", required_skills=["database-designer"],
                required_capabilities=["deploy:prod"])
    assert pool.claim(dba) is None, "read/write does not grant deploy"


def test_the_highest_priority_eligible_task_is_claimed_first(pool, dba):
    pool.create("low", required_skills=["database-designer"], priority=10)
    pool.create("high", required_skills=["database-designer"], priority=90)
    assert pool.claim(dba).description == "high"


def test_the_states_and_the_priority_range_are_read_from_the_class_not_restated(pool, dba):
    """A caller that needs the vocabulary asks for it, so a seventh state and a moved ceiling reach
    the surfaces that document them without a second list to remember.

    The readers here are the ones the terminal uses: `pool list --state` takes `TaskState.all()` as its
    `choices` and its priority help is built from the three constants, so this pins the *source* those
    read — a `TaskState` that grew a member without `all()` seeing it would be a state no filter could
    name, and a clamp written with its own 0/100 would be a ceiling that disagrees with the help.
    """
    from engine.pool import DEFAULT_PRIORITY, MAX_PRIORITY, MIN_PRIORITY

    states = TaskState.all()
    assert set(states) == {TaskState.POOL, TaskState.OFFERED, TaskState.CLAIMED, TaskState.DONE,
                           TaskState.FAILED, TaskState.BACKLOG}
    assert len(states) == len(set(states)), "each state appears once"

    assert (MIN_PRIORITY, DEFAULT_PRIORITY, MAX_PRIORITY) == (0, 50, 100)
    assert pool.create("unspecified", required_skills=["database-designer"]).priority \
        == DEFAULT_PRIORITY
    # The clamp is the constants, in both directions.
    assert pool.create("too high", priority=MAX_PRIORITY + 1).priority == MAX_PRIORITY
    assert pool.create("too low", priority=MIN_PRIORITY - 1).priority == MIN_PRIORITY


def test_two_workers_cannot_claim_the_same_task(pool, dba):
    pool.create("migrate", required_skills=["database-designer"])
    other = agent("ag_dba2", "Dax", ["database-designer"], ["read:*", "write:src/**"])
    assert pool.claim(dba) is not None
    assert pool.claim(other) is None


# ── leases: a dead worker must not strand work ───────────────────────────────


def test_an_expired_lease_returns_the_task_to_the_pool(tmp_path, dba):
    clock = [1000.0]
    pool = TaskPool(tmp_path / "p.json", now=lambda: clock[0])
    pool.create("migrate", required_skills=["database-designer"])
    pool.claim(dba, lease_s=10)
    assert pool.summary()["claimable"] == 0
    clock[0] = 2000.0
    # The read path reclaims, so a status view never reports a held task as claimed.
    assert pool.summary()["claimable"] == 1
    assert pool.claim(dba) is not None


def test_a_live_lease_is_respected(tmp_path, dba):
    clock = [1000.0]
    pool = TaskPool(tmp_path / "p.json", now=lambda: clock[0])
    pool.create("migrate", required_skills=["database-designer"])
    pool.claim(dba, lease_s=100)
    clock[0] = 1050.0
    assert pool.summary()["claimable"] == 0, "a live lease must hold"


# ── renewal: a slow worker is not a dead worker ──────────────────────────────


def test_a_renewed_lease_keeps_the_task_with_the_worker_running_it(tmp_path, dba):
    """F3.1: the lease was set once at claim and nothing could extend it.

    A node that legitimately runs longer than `DEFAULT_LEASE_S` had its task reclaimed by
    `_expire_leases` and handed to a second worker while the first was still working — the same task
    executed twice, and the first worker's result then refused as "not claimed by agent".
    """
    clock = [1000.0]
    pool = TaskPool(tmp_path / "p.json", now=lambda: clock[0])
    pool.create("migrate", required_skills=["database-designer"], task_id="task_slow")
    pool.claim(dba, task_id="task_slow", lease_s=10)
    second = agent("ag_dba2", "Dax", ["database-designer"])

    clock[0] = 1008.0
    pool.renew("task_slow", dba, lease_s=10)          # the heartbeat a running node sends
    clock[0] = 1015.0                                  # past the *original* deadline
    assert pool.summary()["claimable"] == 0, "a renewed lease must hold"
    assert pool.claim(second) is None, "a second worker must not be handed a task still running"
    assert pool.tasks["task_slow"].claimed_by == "ag_dba"


def test_a_renewal_after_the_deadline_keeps_a_task_nobody_else_took(tmp_path, dba):
    """The recovery case: the deadline passed, but no other worker has the task."""
    clock = [1000.0]
    pool = TaskPool(tmp_path / "p.json", now=lambda: clock[0])
    pool.create("migrate", required_skills=["database-designer"], task_id="task_slow")
    pool.claim(dba, task_id="task_slow", lease_s=10)
    clock[0] = 9999.0
    pool.renew("task_slow", dba, lease_s=10)
    assert pool.tasks["task_slow"].state == TaskState.CLAIMED
    assert pool.tasks["task_slow"].claimed_by == "ag_dba"
    assert pool.summary()["claimable"] == 0


def test_a_renewal_is_refused_for_a_task_another_worker_holds(tmp_path, dba):
    """The task was genuinely taken; stealing it back is the duplicate this prevents."""
    clock = [1000.0]
    pool = TaskPool(tmp_path / "p.json", now=lambda: clock[0])
    pool.create("migrate", required_skills=["database-designer"], task_id="task_slow")
    pool.claim(dba, task_id="task_slow", lease_s=10)
    clock[0] = 2000.0
    winner = agent("ag_dba2", "Dax", ["database-designer"])
    assert pool.claim(winner) is not None
    with pytest.raises(PoolError, match="held by"):
        pool.renew("task_slow", dba)
    assert pool.tasks["task_slow"].claimed_by == "ag_dba2", "the live claim stands"


def test_a_renewal_is_refused_for_a_worker_that_never_held_the_task(tmp_path, dba):
    """Otherwise `renew` is a claim with no capability check behind it."""
    pool = TaskPool(tmp_path / "p.json")
    pool.create("migrate", required_skills=["database-designer"], task_id="task_slow")
    with pytest.raises(PoolError, match="claim it again"):
        pool.renew("task_slow", dba)
    assert pool.tasks["task_slow"].state == TaskState.POOL


# ── concurrency: one task, one worker, whatever the writer count ─────────────


def test_concurrent_claims_hand_each_task_to_exactly_one_worker(tmp_path):
    """F3.2: `claim` was `eligible()` then mutate, with no lock — so both callers could win.

    Repeated rounds because a check-then-act race is a window rather than a certainty: the assertion
    is the invariant ("one task, one holder"), which must hold every round.
    """
    import threading

    for round_index in range(8):
        pool = TaskPool(tmp_path / f"pool{round_index}.json")
        for n in range(4):
            pool.create(f"task {n}", required_skills=["database-designer"])
        workers = [agent(f"ag_{n}", f"A{n}", ["database-designer"]) for n in range(10)]
        barrier = threading.Barrier(len(workers))
        claimed: list[tuple[str, str]] = []
        guard = threading.Lock()

        def pull(spec):
            barrier.wait()
            task = pool.claim(spec)
            if task is not None:
                with guard:
                    claimed.append((spec.id, task.id))

        threads = [threading.Thread(target=pull, args=(spec,)) for spec in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        by_task: dict[str, set[str]] = {}
        for agent_id, task_id in claimed:
            by_task.setdefault(task_id, set()).add(agent_id)
        assert len(claimed) == 4, f"round {round_index}: four tasks, {len(claimed)} claims"
        for task_id, holders in by_task.items():
            assert len(holders) == 1, f"round {round_index}: {task_id} was given to {holders}"


def test_two_pools_over_one_file_do_not_revert_each_others_work(tmp_path):
    """F3.3: the whole document is rewritten per save, so the second writer used to erase the first.

    This is the runner subprocess (`host.py:396`) and the orchestrator (`serve.py:1159`) over one
    `.agent_state/pool.json`: both build the pool at their own start, then each saves its whole
    document, so whichever saved last won and the other's claim, offer or completion vanished.
    """
    path = tmp_path / "pool.json"
    orchestrator = TaskPool(path)
    runner = TaskPool(path)                 # both loaded before either wrote anything
    orchestrator.create("from the orchestrator", required_skills=["database-designer"],
                        task_id="task_a")
    runner.create("from the runner", required_skills=["database-designer"], task_id="task_b")
    seen = TaskPool(path)
    assert set(seen.tasks) == {"task_a", "task_b"}, "neither writer may erase the other"


def test_a_claim_from_a_stale_copy_is_refused(tmp_path, dba):
    """Two instances, one file: the second must not hand out a task the first already claimed."""
    path = tmp_path / "pool.json"
    first = TaskPool(path)
    first.create("migrate", required_skills=["database-designer"], task_id="task_one")
    second = TaskPool(path)                 # its in-memory copy still says POOL
    other = agent("ag_dba2", "Dax", ["database-designer"])
    assert first.claim(dba, task_id="task_one") is not None
    assert second.claim(other) is None, "a stale copy must not hand out a claimed task"
    with pytest.raises(PoolError, match="not claimable"):
        second.claim(other, task_id="task_one")


def test_release_records_who_refused_it(tmp_path, dba):
    """The refusal used to be written with `claimed_by` already cleared, so it always named nobody."""
    pool = TaskPool(tmp_path / "pool.json")
    task = pool.create("migrate", required_skills=["database-designer"])
    pool.claim(dba, task_id=task.id)
    pool.release(task.id, reason="need more context")
    assert pool.tasks[task.id].refusals[0]["agent"] == "ag_dba"


# ── offers: accountability and a refusal reason ──────────────────────────────


def test_an_offer_can_be_rejected_with_a_reason(pool, reviewer):
    task = pool.create("review it", required_skills=["code-reviewer"])
    pool.offer(task.id, reviewer.id)
    assert pool.summary()["offered"] == 1
    pool.reject(task.id, reviewer, reason="mid-run on another task")
    assert pool.tasks[task.id].state == TaskState.POOL
    assert pool.tasks[task.id].refusals[0]["reason"] == "mid-run on another task"


def test_an_offer_to_the_wrong_agent_cannot_be_accepted(pool, dba, reviewer):
    task = pool.create("migrate", required_skills=["database-designer"])
    pool.offer(task.id, dba.id)
    with pytest.raises(PoolError, match="offered to"):
        pool.accept(task.id, reviewer)


# ── dependencies: a tree executes in order ───────────────────────────────────


def test_a_dependent_task_is_not_claimable_until_its_dependency_is_done(pool, dba):
    design = pool.create("design the migration", required_skills=["database-designer"])
    pool.create("implement it", required_skills=["database-designer"], depends_on=[design.id])
    assert [t.description for t in pool.eligible(dba)] == ["design the migration"]
    pool.claim(dba, task_id=design.id)
    pool.complete(design.id, dba, output="design done")
    assert [t.description for t in pool.eligible(dba)] == ["implement it"]


def test_an_unknown_dependency_is_refused(pool):
    with pytest.raises(PoolError, match="unknown task"):
        pool.create("orphan", depends_on=["task_nope"])


def test_children_reconstruct_a_decomposition(pool, dba):
    parent = pool.create("migrate everything", required_skills=["database-designer"])
    pool.create("part one", required_skills=["database-designer"], parent_id=parent.id)
    pool.create("part two", required_skills=["database-designer"], parent_id=parent.id)
    assert [t.description for t in pool.children(parent.id)] == ["part one", "part two"]


# ── output schemas: a caller that needs JSON gets JSON or a refusal ──────────


def test_a_completion_not_matching_the_schema_is_refused(pool, dba):
    schema = {"type": "object", "required": ["file"],
              "properties": {"file": {"type": "string"}}}
    task = pool.create("emit a path", required_skills=["database-designer"], output_schema=schema)
    pool.claim(dba, task_id=task.id)
    with pytest.raises(PoolError, match="output schema"):
        pool.complete(task.id, dba, output="not json")
    # The state did not change, so the worker can correct its output rather than lose the task.
    assert pool.tasks[task.id].state == TaskState.CLAIMED
    pool.complete(task.id, dba, output=json.dumps({"file": "migrations/001.sql"}))
    assert pool.tasks[task.id].state == TaskState.DONE


def test_a_missing_required_property_is_reported():
    schema = {"type": "object", "required": ["file", "sha"]}
    problem = validate_output(json.dumps({"file": "x"}), schema)
    assert "sha" in problem


def test_a_wrong_type_is_reported():
    problem = validate_output(json.dumps({"file": 7}),
                              {"type": "object", "properties": {"file": {"type": "string"}}})
    assert "expected string" in problem


def test_an_unsupported_schema_keyword_is_refused_not_skipped():
    """A validator that silently skips an unknown keyword returns 'valid' for unchecked output."""
    problem = validate_output(json.dumps({"a": 1}), {"type": "object", "oneOf": [{"type": "object"}]})
    assert "unsupported schema keyword" in problem


def test_an_enum_is_enforced():
    schema = {"type": "object", "properties": {"verdict": {"enum": ["pass", "fail"]}}}
    assert validate_output(json.dumps({"verdict": "pass"}), schema) == ""
    assert "not one of" in validate_output(json.dumps({"verdict": "maybe"}), schema)


def test_nested_objects_are_checked():
    schema = {"type": "object",
              "properties": {"meta": {"type": "object",
                                      "required": ["author"],
                                      "properties": {"author": {"type": "string"}}}}}
    assert "author" in validate_output(json.dumps({"meta": {}}), schema)


# ── durability ───────────────────────────────────────────────────────────────


def test_the_pool_survives_a_restart(tmp_path, dba):
    path = tmp_path / "pool.json"
    first = TaskPool(path)
    first.create("migrate", required_skills=["database-designer"], priority=70)
    first.claim(dba)

    second = TaskPool(path)
    assert second.summary()["claimed"] == 1
    assert second.tasks[next(iter(second.tasks))].claimed_by == "ag_dba"


def test_release_puts_a_task_back(pool, dba):
    task = pool.create("migrate", required_skills=["database-designer"])
    pool.claim(dba, task_id=task.id)
    pool.release(task.id, reason="need more context")
    assert pool.tasks[task.id].state == TaskState.POOL
    assert pool.tasks[task.id].claimed_by is None


def test_a_backlogged_task_is_not_claimable_until_restored(pool, dba):
    task = pool.create("later", required_skills=["database-designer"])
    pool.to_backlog(task.id)
    assert pool.eligible(dba) == []
    pool.from_backlog(task.id)
    assert [t.id for t in pool.eligible(dba)] == [task.id]


def test_a_failed_task_records_its_reason(pool, dba):
    task = pool.create("migrate", required_skills=["database-designer"])
    pool.claim(dba, task_id=task.id)
    pool.fail(task.id, dba, reason="the schema was gone")
    assert pool.tasks[task.id].state == TaskState.FAILED
    assert "schema was gone" in pool.tasks[task.id].failure_reason


# ── the pool reaches execution ───────────────────────────────────────────────


def test_a_node_declaring_from_pool_drains_a_pooled_task(tmp_path):
    """The regression: the pool was reachable only from the CLI, so a run could never drain it.

    A node that declares `from_pool: true` must claim a pooled task, put its text in front of the
    model, and settle the task from the node's outcome.
    """
    import json
    import tempfile

    from engine.config import load
    from engine.executor import ExecutorContext, NodeExecutor
    from engine.gateway import Gateway
    from engine.library import resolve
    from engine.org import default_company
    from engine.planner import emit_safe_yaml
    from engine.providers.base import ChatResponse, FinishReason, Usage
    from engine.providers.fake import FakeProvider
    from engine.skills import FilesystemSkillSource
    from engine.tokens import TokenEstimator

    config = load()
    source = FilesystemSkillSource(resolve())
    criteria = list(source.load("backend-developer").contract.criteria)
    checklist = source.load("backend-developer").checklist_ids()

    class RecordingProvider(FakeProvider):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.prompts: list[str] = []

        def complete(self, request):
            self.prompts.append("\n".join(m.text for m in request.messages))
            payload = {
                "status": "done", "verdict": "pass", "summary": "did it",
                "criteria_satisfied": [{"criterion": c, "satisfied": True,
                                        "evidence": "src/x.py:1"} for c in criteria],
                "checklist": [{"id": i, "status": "PASS", "evidence": "e"} for i in checklist],
                "artifacts": [{"type": "change", "path": "src/x.py", "content": "x=1"}],
            }
            return ChatResponse(text=json.dumps(payload),
                                usage=Usage(prompt_tokens=5, completion_tokens=5,
                                            reported_cost_usd=0.0),
                                model=request.model, provider_id=self.provider_id,
                                finish_reason=FinishReason.STOP)

    workspace = pathlib.Path(tempfile.mkdtemp())
    manifest = {"name": "wf", "version": "1.0.0", "start": "work",
                "nodes": [{"id": "work", "skill": "backend-developer", "from_pool": True,
                           "phase": "BUILD", "inputs": [], "outputs": ["change"]}]}
    (workspace / "wf.yaml").write_text(emit_safe_yaml(manifest))

    org = default_company(provider="fake", model="m", context_window=32768)
    pooled = TaskPool(workspace / "pool.json")
    pooled.create("backfill the new column for every tenant",
                  required_skills=["backend-developer"], priority=90)

    provider = RecordingProvider(provider_id="fake")
    executor = NodeExecutor(ExecutorContext(
        org=org, gateway=Gateway(config, {"fake": provider}, estimator=TokenEstimator()),
        skills=source, workspace=workspace, config=config, run_id="r", workflow="wf",
        manifest_path=workspace / "wf.yaml", pool=pooled))
    state = {"nodes": {"work": {"status": "pending"}}, "artifacts": {},
             "budget": {"steps_used": 0, "iterations": {}}}
    result = executor.execute_node("work", state, {})

    assert result["status"] == "done"
    assert pooled.summary()["done"] == 1, "the node must settle the task it claimed"
    assert "backfill the new column" in provider.prompts[0], \
        "the pooled task's own text must reach the model"


def test_a_node_not_declaring_from_pool_ignores_the_pool(tmp_path):
    """A pushed node must not silently consume pooled work meant for a puller."""
    from engine.executor import ExecutorContext, NodeExecutor
    from engine.gateway import Gateway
    from engine.library import resolve
    from engine.org import default_company
    from engine.providers.fake import FakeProvider
    from engine.skills import FilesystemSkillSource
    from engine.tokens import TokenEstimator
    from engine.config import load

    config = load()
    source = FilesystemSkillSource(resolve())
    org = default_company(provider="fake", model="m", context_window=32768)
    pooled = TaskPool()
    pooled.create("backfill", required_skills=["backend-developer"])
    executor = NodeExecutor(ExecutorContext(
        org=org, gateway=Gateway(config, {"fake": FakeProvider(provider_id="fake")},
                                 estimator=TokenEstimator()),
        skills=source, workspace=pathlib.Path(tmp_path), config=config,
        run_id="r", workflow="wf", pool=pooled))
    agent_id = next(a.id for a in org.agents.values() if not a.is_human)
    assert executor._claim_pooled("work", {"id": "work", "skill": "backend-developer"},
                                  agent_id) is None
    assert pooled.summary()["claimable"] == 1, "a pushed node must leave the pool alone"


def test_a_node_pulling_an_empty_pool_reports_rather_than_crashing(tmp_path):
    """'Nothing to do' is a reportable outcome, not a failure."""
    from engine.config import load
    from engine.executor import ExecutorContext, NodeExecutor
    from engine.gateway import Gateway
    from engine.library import resolve
    from engine.org import default_company
    from engine.providers.fake import FakeProvider
    from engine.skills import FilesystemSkillSource
    from engine.tokens import TokenEstimator

    config = load()
    source = FilesystemSkillSource(resolve())
    org = default_company(provider="fake", model="m", context_window=32768)
    executor = NodeExecutor(ExecutorContext(
        org=org, gateway=Gateway(config, {"fake": FakeProvider(provider_id="fake")},
                                 estimator=TokenEstimator()),
        skills=source, workspace=pathlib.Path(tmp_path), config=config,
        run_id="r", workflow="wf", pool=TaskPool()))
    agent_id = next(a.id for a in org.agents.values() if not a.is_human)
    assert executor._claim_pooled("work", {"from_pool": True}, agent_id) is None
