#!/usr/bin/env python3
"""Phase 18 tests — isolated subagents: their own context, and results the parent can page.

The engine could already dispatch many agents (a swarm votes, a fan-out splits). What it could not do
was *isolate* them, and four properties follow from doing so — these are what the tests pin:

1. **A child is a session, not a node.** Its reads do not accumulate in the parent's window, so N
   children cost the parent N *previews* rather than N transcripts.
2. **Isolation is the log, never the prefix.** A child shares the parent's pinned prefix, because the
   cacheable bytes are `(skill, tools)`-scoped; folding a child's identity into the prefix is the
   documented mistake that cost 58% on every swarm.
3. **Paging is byte-addressed and honest.** Every read says how much came back and whether more
   remains, because a model that believes it saw the whole result is the confident-wrong-output
   failure the eval suite exists to catch.
4. **Recursion and cost are bounded.** A nestable delegation with no depth cap or no budget carve is an
   unbounded bill, which is the failure this project treats as unacceptable everywhere else.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import ExecutorConfig, load
from engine.state import Workspace
from engine.subagents import (
    MAX_SUBAGENT_DEPTH,
    ChildPage,
    ChildStore,
    SubagentError,
    SubagentRunner,
)
from engine.tools import ToolRegistry


# ── the store ────────────────────────────────────────────────────────────────


def _ws(tmp_path, slug="sub"):
    ws = Workspace.for_project(slug, root=tmp_path / "projects")
    ws.ensure()
    return ws


def _store(tmp_path, run_id="run_1"):
    return ChildStore(_ws(tmp_path), run_id=run_id)


def test_a_child_starts_running_with_an_empty_transcript(tmp_path):
    """An empty transcript is a valid page; a *missing* one is an error. Creating it up front keeps
    those two distinguishable, which matters because they look identical to a model."""
    store = _store(tmp_path)
    ref = store.open(child_id="sub_1", agent_id="ag_1", skill="code-reviewer", task="review a.ts")
    assert ref.status == "running"
    assert store.read(child_id="sub_1").total_bytes == 0


def test_a_child_id_must_be_safe(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(SubagentError, match="invalid child id"):
        store.open(child_id="../../etc/passwd")


def test_the_depth_cap_is_enforced_on_the_child(tmp_path):
    """Checked at `open`, so the bound holds no matter which caller starts a child."""
    store = _store(tmp_path)
    with pytest.raises(SubagentError, match="exceeds the cap"):
        store.open(child_id="deep", depth=MAX_SUBAGENT_DEPTH + 1)


def test_the_summary_survives_a_reload(tmp_path):
    """The on-disk frame carries the child's full summary; the preview is a reader's choice.

    Writing only the preview would silently truncate a resumed child's result to whatever size the last
    reader happened to want.
    """
    ws = _ws(tmp_path)
    store = ChildStore(ws, run_id="r1")
    store.open(child_id="sub_1", agent_id="ag_1", task="t")
    long_summary = "x" * 5000
    store.close(child_id="sub_1", status="done", summary=long_summary, steps=4)

    reloaded = ChildStore(ws, run_id="r1").ref("sub_1")
    assert reloaded.summary == long_summary
    assert reloaded.steps == 4


def test_a_reference_preview_is_bounded(tmp_path):
    """The reference is what costs the parent context, so its preview is capped by design."""
    store = _store(tmp_path)
    store.open(child_id="sub_1", task="t")
    ref = store.close(child_id="sub_1", status="done", summary="y" * 9000)
    payload = ref.as_dict(preview_bytes=1200)
    assert payload["preview_bytes"] <= 1200
    assert payload["bytes"] >= 0
    # The full summary is still available on the ref itself; only the preview is trimmed.
    assert len(ref.summary) == 9000


def test_close_rejects_an_unknown_status(tmp_path):
    store = _store(tmp_path)
    store.open(child_id="sub_1", task="t")
    with pytest.raises(SubagentError, match="unknown child status"):
        store.close(child_id="sub_1", status="banana")


def test_children_lists_in_creation_order(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        store.open(child_id=f"sub_{i}", task=f"t{i}")
    assert [c.child_id for c in store.children()] == ["sub_0", "sub_1", "sub_2"]


def test_a_finished_child_is_not_resumable_but_a_review_one_is(tmp_path):
    """A child that reached a verdict can be continued; one that crashed is better re-run, because
    appending to a partial turn builds on a half-thought."""
    store = _store(tmp_path)
    store.open(child_id="done1", task="t")
    store.close(child_id="done1", status="done")
    assert store.resumable("done1") is False

    store.open(child_id="review1", task="t")
    store.close(child_id="review1", status="needs_review")
    assert store.resumable("review1") is True


def test_transcripts_are_append_only(tmp_path):
    """Rewriting an earlier record would invalidate every byte offset after it — which is what paging
    relies on."""
    store = _store(tmp_path)
    store.open(child_id="sub_1", task="t")
    store.append(child_id="sub_1", kind="turn", text="first")
    first = store.transcript_path("sub_1").read_bytes()
    store.append(child_id="sub_1", kind="turn", text="second")
    after = store.transcript_path("sub_1").read_bytes()
    assert after.startswith(first), "an append must not rewrite earlier bytes"


# ── paging ───────────────────────────────────────────────────────────────────


def test_paging_is_byte_addressed_and_reports_truncation(tmp_path):
    store = _store(tmp_path)
    store.open(child_id="sub_1", task="t")
    for i in range(4):
        store.append(child_id="sub_1", kind="turn", text=f"turn {i} " + "z" * 200)

    page = store.read(child_id="sub_1", limit_bytes=120)
    assert page.offset_bytes == 0
    assert page.returned_bytes == 120
    assert page.more is True
    assert page.total_bytes > 120

    tail = store.read(child_id="sub_1", offset_bytes=page.returned_bytes, limit_bytes=120)
    assert tail.offset_bytes == 120
    assert tail.text != page.text


def test_a_read_returns_the_whole_small_transcript(tmp_path):
    store = _store(tmp_path)
    store.open(child_id="sub_1", task="t")
    store.append(child_id="sub_1", kind="turn", text="short")
    page = store.read(child_id="sub_1", limit_bytes=8192)
    assert page.more is False
    assert "short" in page.text


def test_a_page_clamps_an_oversized_limit(tmp_path):
    """A model cannot ask for a 40MB transcript and blow its own window."""
    from engine.subagents import MAX_PAGE_BYTES

    store = _store(tmp_path)
    store.open(child_id="sub_1", task="t")
    store.append(child_id="sub_1", kind="turn", text="a" * (MAX_PAGE_BYTES + 5000))
    page = store.read(child_id="sub_1", limit_bytes=10_000_000)
    assert page.returned_bytes <= MAX_PAGE_BYTES


def test_reading_an_unknown_child_is_an_error(tmp_path):
    with pytest.raises(SubagentError, match="no transcript"):
        _store(tmp_path).read(child_id="nope")


def test_page_reports_the_next_offset(tmp_path):
    page = ChildPage(child_id="c", text="abc", offset_bytes=0, returned_bytes=3, total_bytes=10)
    assert page.more is True
    assert page.as_dict()["next_offset_bytes"] == 3

    final = ChildPage(child_id="c", text="abc", offset_bytes=7, returned_bytes=3, total_bytes=10)
    assert final.more is False
    assert final.as_dict()["next_offset_bytes"] is None


# ── dispatch ─────────────────────────────────────────────────────────────────


class _FakeChildren:
    """A child runner that records what it was asked to do. No provider, no network."""

    def __init__(self, store, status="done"):
        self.store = store
        self.status = status
        self.seen: list[dict[str, object]] = []

    def __call__(self, *, prompt, skill, child_id, agent_id, depth, budget):
        self.seen.append({"prompt": prompt, "agent_id": agent_id, "depth": depth,
                          "budget": budget, "skill": skill})
        self.store.append(child_id=child_id, kind="turn", text=f"working on {prompt}")
        return (f"result for {prompt}", self.status, 100, 20, 3)


def _runner(tmp_path, *, agents=("ag_1", "ag_2"), parent_tokens=0, parent_depth=0,
            status="done", max_parallel=4):
    store = ChildStore(_ws(tmp_path), run_id="run_1")
    child = _FakeChildren(store, status=status)
    runner = SubagentRunner(store=store, run_child=child, agents=list(agents),
                            max_parallel=max_parallel, parent_depth=parent_depth,
                            parent_tokens=parent_tokens)
    return runner, child, store


def test_task_runs_one_child(tmp_path):
    runner, child, _ = _runner(tmp_path)
    ref = runner.run_task(prompt="review a.ts", skill="code-reviewer")
    assert ref["status"] == "done"
    assert ref["steps"] == 3
    assert len(child.seen) == 1


def test_fleet_runs_one_child_per_task_in_order(tmp_path):
    runner, child, _ = _runner(tmp_path)
    refs = runner.run_fleet(tasks=["a", "b", "c"])
    assert len(refs) == 3
    assert [r["task"] for r in refs] == ["a", "b", "c"]
    assert len(child.seen) == 3


def test_fleet_spreads_work_across_agents(tmp_path):
    """Distinct children on distinct agents is what makes a fleet independent rather than serial."""
    runner, child, _ = _runner(tmp_path)
    runner.run_fleet(tasks=["a", "b"])
    assert {c["agent_id"] for c in child.seen} == {"ag_1", "ag_2"}


def test_delegation_deeper_than_the_cap_is_refused(tmp_path):
    runner, _, _ = _runner(tmp_path, parent_depth=MAX_SUBAGENT_DEPTH)
    with pytest.raises(SubagentError, match="the cap is"):
        runner.run_task(prompt="x")


def test_dispatch_without_an_agent_is_refused(tmp_path):
    """Offering a tool with nobody to run it is worse than not offering it."""
    runner, _, _ = _runner(tmp_path, agents=())
    with pytest.raises(SubagentError, match="no agent is available"):
        runner.run_task(prompt="x")


def test_a_fleet_carves_the_parent_budget(tmp_path):
    """A fleet that each assumed the parent's whole budget would let N children spend N times it."""
    runner, child, _ = _runner(tmp_path, parent_tokens=10_000)
    runner.run_fleet(tasks=["a", "b", "c"])
    budgets = [c["budget"] for c in child.seen]
    assert budgets == [2500, 2500, 2500]
    assert sum(budgets) <= 10_000


def test_a_failing_child_does_not_lose_the_others(tmp_path):
    """A fan-out is for independent work, so one failure must not discard the rest."""
    store = ChildStore(_ws(tmp_path), run_id="run_1")

    def flaky(*, prompt, skill, child_id, agent_id, depth, budget):
        if prompt == "b":
            raise RuntimeError("provider exploded")
        return (f"result for {prompt}", "done", 10, 2, 1)

    runner = SubagentRunner(store=store, run_child=flaky, agents=["ag_1"])
    refs = runner.run_fleet(tasks=["a", "b", "c"])
    assert [r["status"] for r in refs] == ["done", "failed", "done"]
    assert "provider exploded" in refs[1]["error"]


def test_a_child_that_needs_review_is_reported_as_such(tmp_path):
    """A truncated child must not be reported as a finished one."""
    runner, _, _ = _runner(tmp_path, status="needs_review")
    ref = runner.run_task(prompt="investigate")
    assert ref["status"] == "needs_review"


def test_reading_through_the_runner_pages_the_transcript(tmp_path):
    runner, _, _ = _runner(tmp_path)
    ref = runner.run_task(prompt="review a.ts")
    page = runner.read(child_id=ref["child_id"], limit_bytes=50)
    assert page.total_bytes > 0
    assert page.returned_bytes == min(50, page.total_bytes)


# ── the tools ────────────────────────────────────────────────────────────────


def _registry(tmp_path, **kw):
    runner, child, store = _runner(tmp_path, **kw)
    return ToolRegistry(workspace_root=store.state_dir.parent, subagents=runner), runner, store


def test_subagent_tools_are_absent_without_a_runner(tmp_path):
    """A node with no way to collect a child must not be offered the means to start one."""
    ws = _ws(tmp_path)
    registry = ToolRegistry(workspace_root=ws.path)
    for name in ("task", "fleet", "read_subagent_result"):
        assert name not in registry.names()


def test_subagent_tools_are_offered_with_a_runner(tmp_path):
    registry, _, _ = _registry(tmp_path)
    for name in ("task", "fleet", "read_subagent_result"):
        assert name in registry.names()


def test_task_tool_returns_a_reference_not_a_transcript(tmp_path):
    """The whole point of isolation: the default view is a preview plus a pointer."""
    registry, _, store = _registry(tmp_path)
    result = registry.call("task", {"prompt": "review a.ts"})
    assert result.ok
    assert "subagent sub_001" in result.text
    assert "read_subagent_result" in result.text
    # The transcript's own body is not in the reference.
    body = store.transcript_path("sub_001").read_text()
    assert body.splitlines()[0] not in result.text


def test_task_tool_requires_a_prompt(tmp_path):
    registry, _, _ = _registry(tmp_path)
    assert not registry.call("task", {"prompt": "  "}).ok


def test_fleet_tool_requires_tasks(tmp_path):
    registry, _, _ = _registry(tmp_path)
    assert not registry.call("fleet", {"tasks": []}).ok


def test_fleet_tool_reports_every_child(tmp_path):
    registry, _, _ = _registry(tmp_path)
    result = registry.call("fleet", {"tasks": ["a", "b", "c"]})
    assert result.ok
    assert "3 subagent(s)" in result.text


def test_read_subagent_result_pages_and_says_more_remains(tmp_path):
    registry, _, store = _registry(tmp_path)
    registry.call("task", {"prompt": "review a.ts"})
    store.append(child_id="sub_001", kind="turn", text="q" * 400)

    result = registry.call("read_subagent_result", {"child_id": "sub_001", "limit_bytes": 80})
    assert result.ok
    assert "more remains" in result.text
    assert result.truncated is True


def test_read_subagent_result_reports_the_end(tmp_path):
    registry, _, _ = _registry(tmp_path)
    registry.call("task", {"prompt": "tiny"})
    result = registry.call("read_subagent_result", {"child_id": "sub_001"})
    assert result.ok
    assert "end of transcript" in result.text


def test_read_subagent_result_needs_a_child_id(tmp_path):
    registry, _, _ = _registry(tmp_path)
    assert not registry.call("read_subagent_result", {}).ok


def test_read_subagent_result_reports_an_unknown_child(tmp_path):
    registry, _, _ = _registry(tmp_path)
    result = registry.call("read_subagent_result", {"child_id": "ghost"})
    assert not result.ok
    assert "cannot read" in result.text


# ── configuration ────────────────────────────────────────────────────────────


def test_subagents_are_off_by_default(tmp_path):
    """A fan-out of tool-using children multiplies cost, so it is opt-in rather than incidental."""
    cfg = ExecutorConfig()
    assert cfg.subagents_enabled is False
    assert cfg.subagent_fleet_enabled is False


def test_subagent_parallelism_must_be_positive():
    with pytest.raises(Exception):
        ExecutorConfig(subagent_max_parallel=0)


def test_loaded_config_has_the_subagent_section():
    cfg = load()
    assert hasattr(cfg.executor, "subagents_enabled")
    assert cfg.executor.subagents_enabled is False
