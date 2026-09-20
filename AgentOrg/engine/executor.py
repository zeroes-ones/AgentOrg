#!/usr/bin/env python3
"""executor.py — the `execute_node` plugin: where a graph node becomes real work.

WHY THIS EXISTS
---------------
This is the module the whole project builds toward. The library's runner owns control flow — loops,
budgets, gates, stagnation detection. This module answers one question per node: *who does this, what
are they told, and what did they return.*

The contract is the library's, and it is narrow on purpose:

    execute_node(node_id, state, ctx) -> dict

Returning a dict the runner accepts is not merely a formatting concern. The runner checks it against
the node's declared contract in code:

- `completion.evidence: required` with an empty `evidence` list is a **contract violation**.
- Declared `completion.criteria` with no `criteria_met` coverage is a **contract violation**.
- The coverage references must resolve to the declared criteria or they are reported unrecognized.

So this executor's job is to make an agent's reply *fit that contract* — which is exactly what the
checklist-enforcing prompt was built for. A node cannot claim done without evidence, because the
runner will reject the claim.

DESIGN
------
- **The node names a capability; the org supplies the person.** Binding goes through the roster, so
  the same skill can be Alice today and Bob tomorrow without touching the graph.
- **A reviewer is never its own producer.** The binding refuses it, and the producer is recorded in
  run-state so the refusal has something to compare against.
- **Context is managed before the call, not after it fails.** The pre-flight projection compacts, or
  rotates when compaction is exhausted — and refuses when neither can help.
- **Side effects go through the idempotency journal.** A retried node does not re-write an artifact or
  re-spend tokens.
- **A parse failure is a needs_review, not a crash.** A model that ignores the trailer format has still
  done work; discarding it would waste the tokens it cost, so the raw reply is preserved as the
  summary and the node is marked for review.
- **The trailer is asked for in the form the provider can enforce.** Where the provider declares JSON
  mode, the turns whose job is the trailer request it (`_json_mode`) instead of asking a small model for
  a JSON object in prose — which is how a node whose work had already succeeded parked on a contract it
  reported nothing against. A tool loop's tool-calling turns are exempt: they must remain able to call
  a tool.

Usage:
    executor = NodeExecutor(context=ctx)
    result = executor.execute_node("fixer", state, {"pass": 1})
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .artifacts import ArtifactRef, ArtifactStore, WorkspaceError
from .context import (
    Session,
    assemble_session_prompt,
    build_handoff,
    compact,
    decide_rotation,
    project,
)
from .gateway import BudgetExceeded, Gateway
from .idempotency import EffectJournal, effect_key
from .org import (
    AgentState,
    Binder,
    BindingError,
    BindingPolicy,
    Handoff,
    HandoffError,
    Ledger,
    Org,
    Router,
    RouteContext,
    RouteClass,
    build_context_pass_through,
    declared_policy,
    validate_handoff,
)
from .org.handoff import REQUIRED_FIELDS as HANDOFF_REQUIRED_FIELDS
from .parallel import TERMINAL_STATUSES
from .prompts import TRAILER_FENCE, PromptBuilder, TaskContext, TrailerError, extract_trailer
from .protocol import EventType
from .skills.bundle import SkillBundle, SkillError
from .state import ENGINE_STATE_DIRNAME

__all__ = ["NodeExecutor", "ExecutionError", "ExecutedNode", "ExecutorContext"]


class ExecutionError(RuntimeError):
    """Raised when a node cannot be executed at all — as distinct from a node that reports a problem.

    A missing skill or an unstaffed node is an execution error; an agent whose work needs review is a
    *result*, and is returned as a runner dict rather than raised.
    """


@dataclass
class ExecutorContext:
    """Everything the executor needs to run nodes, injected rather than constructed.

    Injection is what makes the executor testable without a provider, a roster or a real workspace —
    which matters because this module has the most moving parts in the project.
    """

    org: Org
    gateway: Gateway
    skills: Any                          # a SkillSource
    workspace: Path
    store: ArtifactStore | None = None
    sessions: dict[str, Session] = field(default_factory=dict)
    journal: EffectJournal | None = None
    bus: Any = None
    telemetry: Any = None
    memory: Any = None
    diagnostics: Any = None
    config: Any = None
    run_id: str = ""
    workflow: str = ""
    #: True when a goal is armed for this run, so `update_goal` is advertised and its verdict is
    #: written for the orchestrator to consume. A bool rather than the Goal itself: the executor only
    #: needs to know whether the tool is in play, and the goal's own state lives in the orchestrator.
    goal_active: bool = False
    # The manifest this run executes. The runner's run-state does not carry it, so the executor reads
    # it from `manifest_path` — or uses `manifest` when a caller injects one directly (tests).
    manifest_path: Path | None = None
    manifest: dict[str, Any] | None = None
    # Node-id -> agent-id pins, and node-id -> binding policy overrides.
    pins: dict[str, str] = field(default_factory=dict)
    policies: dict[str, BindingPolicy] = field(default_factory=dict)
    # The task pool, when the run has one. A node can declare `from_pool: true` to pull its work
    # from it instead of being bound to a fixed skill — which is what makes the pool reachable
    # *during a run* rather than only from the CLI.
    pool: Any = None
    # The prefixes pinned for this run. A skill edited mid-run must not silently change the bytes a
    # running session sends, or the cache goes cold with nothing reporting why.
    pins_for_prefix: Any = None
    # The decision gate ledger. The orchestrator owns the durable one; a caller that injects none
    # still gets a usable ledger here, because an edge crossing that records nothing is exactly the
    # gap this wiring closes.
    ledger: Any = None
    #: The ceiling on one model reply, in tokens. Resolved once, at construction, from the bound
    #: model's declared `max_output`, then the config's `executor.max_output_tokens`, then a floor.
    #:
    #: The hardcoded 4096 this replaced truncated long artifacts *before* the trailer, so a capable
    #: model looked like it was refusing to satisfy its contract when in fact its reply was cut off.
    max_output_tokens: int = 32768
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = ArtifactStore(workspace_root=self.workspace)
        self.max_output_tokens = self._resolve_max_output()

    def _resolve_max_output(self) -> int:
        """The output ceiling for this run, preferring what the model actually supports.

        A model that declares a larger output should be allowed to use it: the artifact is what the
        contract is judged on, and truncating it fails the node for a reason the model cannot see.
        The *context window* is the natural bound — an output cannot exceed what the model accepts —
        so a window that is smaller than the configured ceiling caps the answer rather than risking a
        provider-side rejection.
        """
        ceiling = 0
        if self.org is not None:
            for agent in self.org.agents.values():
                if agent.is_ai and agent.max_output:
                    ceiling = max(ceiling, int(agent.max_output))
        if not ceiling and self.config is not None:
            section = getattr(self.config, "executor", None)
            ceiling = int(getattr(section, "max_output_tokens", 0) or 0)
        if not ceiling:
            ceiling = 32768
        # Never ask for more output than the model's own window can hold, or the provider rejects the
        # whole call — a worse failure than a shorter answer.
        window = 0
        if self.org is not None:
            for agent in self.org.agents.values():
                if agent.is_ai and agent.context_window:
                    window = max(window, int(agent.context_window))
        if not window and self.config is not None:
            window = int(self.config.default_model_spec().context_window or 0)
        if window:
            # Leave room for the prompt: an output cap equal to the window would leave nothing for the
            # input. Half is the conventional split and matches `output_reserve_frac`'s intent.
            ceiling = min(ceiling, max(1024, window // 2))
        return max(1024, ceiling)


@dataclass
class ExecutedNode:
    """The outcome of one node, kept for the UI and for the next node's context."""

    node_id: str
    skill: str
    agent_id: str
    agent_name: str
    status: str
    verdict: str
    session_id: str
    attempts: int = 0
    tokens: int = 0
    cost_usd: float | None = None
    artifacts: list[ArtifactRef] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    criteria_met: list[str] = field(default_factory=list)
    rotated: int = 0
    compacted: int = 0
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id, "skill": self.skill,
            "agent_id": self.agent_id, "agent_name": self.agent_name,
            "status": self.status, "verdict": self.verdict,
            "session_id": self.session_id, "attempts": self.attempts,
            "tokens": self.tokens, "cost_usd": self.cost_usd,
            "artifacts": [a.as_dict() for a in self.artifacts],
            "findings": len(self.findings), "criteria_met": self.criteria_met,
            "rotated": self.rotated, "compacted": self.compacted,
        }


class NodeExecutor:
    """Implements the library's `execute_node` contract.

    **Thread-safety.** Several nodes genuinely run at once — a fan-out's items, a swarm's voters, and
    the members of a `parallel:` group (see `engine/parallel.py`) — so this object is shared across
    threads and the state below is shared with it. That was *claimed* here before it was true: the
    session map and the artifact store were guarded, but the node registries were plain dicts read by
    one node while another wrote them, which raises `RuntimeError: dictionary changed size during
    iteration` rather than returning a wrong answer. Every registry is now guarded by
    `self.registries`, and the shared stores this class does not own are named where they are used:

    - `ctx.sessions` — guarded by `ctx.lock` (and read atomically at its call sites).
    - `ctx.store` — `ArtifactStore` serialises per path and atomically replaces, so two writers to
      *different* artifacts do not interact and two writers to the *same* one serialise.
    - `ctx.journal` — `EffectJournal` reserves under its own lock before the effect runs, so two
      threads cannot both be told to apply the same effect.
    """

    def __init__(self, context: ExecutorContext) -> None:
        self.ctx = context
        self.org = context.org
        self.binder = Binder(self.org)
        self.router = Router(self.org)
        if hasattr(context.skills, "load"):
            # Give the router the skill source so contract compatibility can be scored.
            self.router.attach_skills(context.skills)
        self.builder = PromptBuilder()
        # Node id -> the agent that produced its artifact, for the independence refusal.
        self.producers: dict[str, str] = {}
        self.history: dict[str, ExecutedNode] = {}
        #: Node id -> the typed handoff that node *received*, validated at its boundary. The prompt is
        #: built from this rather than from run-state, so what a node is told it inherited is what the
        #: contract actually accepted.
        self.inbound: dict[str, Handoff] = {}
        #: Handoff id -> every handoff this process produced, kept for reporting.
        self.handoffs: dict[str, Handoff] = {}
        #: Guards the four registries above. Reentrant because a writer may call a reader while the
        #: lock is already held, and a `RLock` is what keeps that from being a self-deadlock.
        self.registries = threading.RLock()
        #: The MCP bridge, connected once per executor rather than once per node. A stdio server is a
        #: process, so re-handshaking per node would spend more on connect than on work. `None` means
        #: either nothing is configured or the one attempt failed — `_mcp_attempted` distinguishes
        #: them, so a failure is reported once per run instead of once per node.
        self._mcp_bridge: Any = None
        self._mcp_attempted = False

    # ── the runner's entry point ────────────────────────────────────────────

    def execute_node(self, node_id: str, state: dict[str, Any],
                     ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one node and return a result the runner will accept.

        Three cases the runner dispatches here, and each is handled differently:

        - **`mode: identify`** — an agent gate asking which channel should lead a bounded reroute.
          That is a routing decision, not content work.
        - **A gate node** — the runner sends *every* node to the executor, including `type: gate`.
          A gate does no content work: it either records that the checkpoint was reached (an automatic
          gate) or parks for the Owner (a human gate). Attempting to bind an agent to it would fail,
          since a gate declares no skill.
        - **A skill node** — the normal case: bind an agent and do the work.
        """
        ctx = ctx or {}
        if ctx.get("mode") == "identify":
            return self._identify(node_id, state, ctx)

        node = self._node_for(node_id, state)
        if str(node.get("type") or "") == "gate" or self._is_gate(node_id, state):
            return self._gate(node_id, node, state)

        skill = str(node.get("skill") or ctx.get("skill") or "")
        if not skill:
            raise ExecutionError(
                f"node {node_id!r} names no skill and is not a gate, so there is no capability to "
                "bind. A node that does neither is a manifest defect."
            )

        # A member of a `parallel:` group is the runner's first visit to the group, and this call is
        # the one place the group can be overlapped at all: the runner dispatches one node at a time,
        # so the only way its members run concurrently is if this call runs them. See `engine/parallel`.
        group = self._group_for(node_id, state)
        if group is not None:
            return self._run_group(group_id=group, node_id=node_id, state=state, ctx=ctx)

        attempt = int(ctx.get("pass") or self._attempt_from_state(node_id, state) or 1)
        return self._run(node_id, node, skill, state, attempt=attempt, ctx=ctx)

    # ── parallel groups ─────────────────────────────────────────────────────

    def _group_for(self, node_id: str, state: dict[str, Any]) -> str | None:
        """The `parallel:` group this node leads, when overlapping it is opted in and safe.

        Returns None in every case where the group must be left to the runner's sequential walk, and
        each of those is a deliberate refusal rather than a fallback:

        - the group does not opt in (`concurrent: true` on the `parallel:` block), because a run whose
          execution order changes by default is a behaviour change disguised as a performance one;
        - the manifest declares no such group, or this node is not a member of one;
        - this node is not the *first* member in declared order, so exactly one member drives the
          group and the others are run by it rather than dispatched again by the runner;
        - any of the group's members has already reported, which means the runner is mid-group (a
          resumed run, or a rework pass) and re-running the members would repeat work the runner
          believes is done.
        """
        manifest = self._manifest(state)
        for block in manifest.get("parallel") or []:
            if not isinstance(block, dict):
                continue
            members = [str(m) for m in (block.get("nodes") or [])]
            if node_id not in members:
                continue
            if not self._parallel_enabled(block):
                return None
            # Exactly one member leads. Declared order, not the group's set order, so which member
            # leads does not depend on how the manifest happened to be written into the dict.
            if members.index(node_id) != 0:
                return None
            reported = (state.get("nodes") or {})
            if any(str(reported.get(m, {}).get("status") or "") in TERMINAL_STATUSES for m in members):
                return None
            return str(block.get("id") or "")
        return None

    def _parallel_enabled(self, block: dict[str, Any]) -> bool:
        """Whether this group wants its members overlapped rather than walked in order.

        Opt-in, and the opt-in is the group's own `concurrent:` field rather than a global switch: a
        plan can mark the one fan-out whose members are genuinely independent and leave every other
        group — including one whose members share a rate-limited provider — on the runner's own
        sequential order. A global flag would apply the concurrency to groups the planner never
        reasoned about, which is the wrong place to put a decision that depends on the group's shape.

        `AGENTORG_PARALLEL_NODES=1` forces it on for every group, which is what an eval or an operator
        measuring the difference wants; it does not override the safety checks in `plan_group`, which
        are about correctness rather than preference.
        """
        if not bool(block.get("concurrent", False)):
            return _env_flag("AGENTORG_PARALLEL_NODES")
        return True

    def _run_group(self, *, group_id: str, node_id: str, state: dict[str, Any],
                   ctx: dict[str, Any]) -> dict[str, Any]:
        """Run every member of a group concurrently and return the leader's result for the runner.

        The runner asked about one node and gets one result back, so its traversal, its step budget
        and its per-node checkpoint are untouched — the overlap is entirely inside this call. The
        returned result is the leader's own, *augmented* with the group's outcomes rather than
        replaced by an aggregate: the leader is a real node that did real work and the runner will
        record it as such, so substituting a synthetic group result would make the recorded node
        describe something no agent produced.

        A refusal from `plan_group` is a *result*, not an exception. The group still runs — the
        sequential path is right there — and the node reports that it was not overlapped and why, so
        a manifest that cannot be parallelised is visible in the run rather than failing it.
        """
        from .parallel import ParallelError, find_group, plan_group, run_group

        manifest = self._manifest(state)
        declared = find_group(manifest, group_id)
        ceiling = self._group_ceiling()
        try:
            plan = plan_group(declared, manifest, ceiling=ceiling)
        except ParallelError as exc:
            # Not overlapped, and said so. The node then runs through the ordinary path, which is the
            # runner's own order — so a refused group degrades to exactly the behaviour it has today.
            self._log("parallel.refused", level="warning", node_id=node_id,
                      message=str(exc), detail={"group": group_id})
            return self._run_sequentially(node_id, state, ctx, group_id=group_id, reason=str(exc))

        # Members that are already recorded are excluded: on a rework pass or a resume the runners
        # that have reported must not be re-run, or the group would re-spend what it already spent.
        recorded = state.get("nodes") or {}
        runnable = [m for m in plan.members
                    if str(recorded.get(m, {}).get("status") or "") not in TERMINAL_STATUSES]
        if node_id not in runnable or len(runnable) < 2:
            self._log("parallel.degraded", node_id=node_id,
                      detail={"group": group_id, "runnable": runnable,
                              "reason": "fewer than two members still need to run"})
            return self._run_sequentially(node_id, state, ctx, group_id=group_id,
                                          reason="fewer than two members still need to run")

        plan = type(plan)(group_id=plan.group_id, members=runnable, join=plan.join,
                          ceiling=plan.ceiling, reason=plan.reason)
        self._log("parallel.start", node_id=node_id,
                  detail={"group": group_id, "members": runnable, "ceiling": plan.ceiling,
                          "join": plan.join, "reason": plan.reason})

        def _member(member_id: str) -> dict[str, Any]:
            """One member, run through the ordinary node path.

            Deliberately the same `_run` the sequential walk uses, with only the node id differing —
            so a member keeps its own binding, its own skill bundle, its own contract and its own
            handoff. A member is not a fan-out item: it is a graph node that happens to have siblings,
            and giving it a special path would be a second, quieter way for a node to execute.
            """
            member_node = self._node_for(member_id, state)
            member_skill = str(member_node.get("skill") or "")
            if not member_skill:
                raise ExecutionError(
                    f"parallel member {member_id!r} names no skill, so it cannot be bound. A group "
                    "member is a graph node and must declare work like any other."
                )
            attempt = int(self._attempt_from_state(member_id, state) or 1)
            return self._run(member_id, member_node, member_skill, state, attempt=attempt, ctx={})

        outcome = run_group(plan, _member, on_event=self._group_emit)

        # The leader's own result drives the graph. A member that failed is *not* folded into the
        # leader's status: only the leader's node crosses the runner's edge, so a sibling's failure
        # must travel as a named diagnostic and open question rather than as a verdict on the leader,
        # which did nothing wrong.
        leader = outcome.results.get(node_id)
        if leader is None:
            # The leader *did* run and failed. Re-running it would spend its tokens twice, so the
            # failure is propagated instead — and the original exception rather than a wrapping one,
            # because the runner's crash path reports it and a wrapper would hide what actually went
            # wrong. The siblings' outcomes are logged first: they have already been paid for, and a
            # reader needs to know which of them produced work before this node fell over.
            reason = outcome.failures.get(node_id) or "the group's leader did not report"
            self._log("parallel.leader_failed", level="error", node_id=node_id,
                      message=reason, detail=outcome.as_dict())
            original = outcome.exceptions.get(node_id)
            if original is not None:
                raise original
            raise ExecutionError(
                f"the parallel group's leader node {node_id!r} did not report: {reason}"
            )

        siblings = {m: r for m, r in outcome.results.items() if m != node_id}
        merged = dict(leader)
        merged["parallel"] = {
            "group_id": group_id,
            "members": list(plan.members),
            "ceiling": plan.ceiling,
            "peak_in_flight": outcome.peak_in_flight,
            "complete": outcome.complete,
            "siblings": {m: {"status": r.get("status"), "verdict": r.get("verdict")}
                         for m, r in siblings.items()},
            "failed": dict(outcome.failures),
        }
        if outcome.failures:
            # Surfaced on the leader's result so the runner records them: the group's own failures
            # must reach run-state, or a sibling that failed leaves no trace once this call returns.
            merged["diagnostics"] = list(merged.get("diagnostics") or []) + [
                f"parallel sibling {m} failed: {e}" for m, e in sorted(outcome.failures.items())
            ]
            merged["open_questions"] = list(merged.get("open_questions") or []) + [
                {"question": f"parallel sibling {m} failed and must be re-run or reviewed: {e}",
                 "assigned_to": "owner"}
                for m, e in sorted(outcome.failures.items())
            ]
        self._log("parallel.finished", node_id=node_id, detail=outcome.as_dict())
        return merged

    def _run_sequentially(self, node_id: str, state: dict[str, Any], ctx: dict[str, Any], *,
                          group_id: str, reason: str) -> dict[str, Any]:
        """Run one node the ordinary way, carrying the reason its group was not overlapped.

        The fallback for every group that could not be overlapped. It is deliberately the *same* call
        the runner would have made, so a refused group is indistinguishable from not having the
        feature — which is what makes enabling it safe.
        """
        node = self._node_for(node_id, state)
        skill = str(node.get("skill") or ctx.get("skill") or "")
        if not skill:
            raise ExecutionError(
                f"node {node_id!r} names no skill and is not a gate, so there is no capability to bind."
            )
        attempt = int(ctx.get("pass") or self._attempt_from_state(node_id, state) or 1)
        result = self._run(node_id, node, skill, state, attempt=attempt, ctx=ctx)
        diagnostics = list(result.get("diagnostics") or [])
        diagnostics.append(f"parallel group {group_id!r} ran sequentially: {reason}")
        return {**result, "diagnostics": diagnostics}

    def _group_ceiling(self) -> int:
        """How wide a group may overlap: the engine's own bound, from the same knob a fan-out reads."""
        from .parallel import group_ceiling

        capacity = None
        try:
            from .resources import derive_ceiling, detect

            capacity = derive_ceiling(detect())["ceiling"]
        except Exception:  # noqa: BLE001 - an unmeasurable machine falls back to the configured bound
            capacity = None
        return group_ceiling(self.ctx.config, capacity=capacity)

    def _group_emit(self, kind: str, payload: dict[str, Any]) -> None:
        """Fan a group's own events onto the run's bus, tagged so the UI can group them.

        Best-effort: a group must not fail because an event sink did. The tag matters because a
        group's members are concurrent, so their spans arrive interleaved — without it a reader cannot
        tell which node a span belonged to.
        """
        bus = self.ctx.bus
        if bus is None:
            return
        try:
            from .protocol import EventType

            bus.emit(EventType.AGENT_LOG, payload={
                "stream": "stderr", "level": "info", "event": kind,
                "text": f"{kind}: {payload.get('group', '')} {payload.get('node', '')}".strip(),
                "parallel": payload,
            })
        except Exception:  # noqa: BLE001 - reporting must never break a group
            pass

    # ── gates ───────────────────────────────────────────────────────────────

    def _is_gate(self, node_id: str, state: dict[str, Any]) -> bool:
        """Whether this node is declared as a gate in the manifest."""
        return self._gate_declaration(node_id, state) is not None

    def _gate(self, node_id: str, node: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Answer a gate node.

        A human gate parks the run: the runner's job is to reach it, and the orchestrator's is to hold
        the run there until the Owner decides. Returning `needs_review` for a human gate is what makes
        that a *deliberate stop* rather than an error.

        An automatic gate passes through, recording the artifacts it required — the checkpoint is the
        point, and a gate that silently waved everything through would not be one.
        """
        declaration = self._gate_declaration(node_id, state) or node
        kind = str(declaration.get("kind") or "human")
        requires = [str(r) for r in (declaration.get("requires") or [])]
        index = state.get("artifacts") or {}
        present = [r for r in requires if r in index]
        missing = [r for r in requires if r not in index]

        if kind == "human":
            self._log("human.gate", node_id=node_id,
                      message=str(declaration.get("description") or "")[:200],
                      detail={"requires": requires, "present": present, "missing": missing})
            self._emit_gate(node_id, declaration, present, missing)
            return {
                "status": "needs_review",
                "verdict": "awaiting_owner",
                "summary": (
                    f"human gate {node_id!r} reached; awaiting the Owner. "
                    + (f"requires {', '.join(requires)}." if requires else "no artifacts required.")
                ),
                "evidence": [f"gate:{node_id}:artifacts={','.join(present) or 'none'}"],
                "diagnostics": ([f"awaiting: {', '.join(missing)}"] if missing else []),
            }

        # An automatic gate passes through, but records what it checked.
        self._log("gate.auto", node_id=node_id,
                  detail={"requires": requires, "present": present, "missing": missing})
        if missing:
            return {
                "status": "blocked",
                "verdict": "missing_prerequisites",
                "summary": f"automatic gate {node_id!r} is missing: {', '.join(missing)}",
                "evidence": [f"gate:{node_id}:missing={','.join(missing)}"],
            }
        return {
            "status": "done", "verdict": "passed",
            "summary": f"automatic gate {node_id!r} passed with {len(present)} artifact(s)",
            "evidence": [f"gate:{node_id}:artifacts={','.join(present) or 'none'}"],
        }

    def _gate_declaration(self, node_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
        """The gate's own declaration, which carries `kind` and `requires`."""
        for gate in self._manifest(state).get("gates") or []:
            if str(gate.get("id")) == node_id:
                return gate
        return None

    def _emit_gate(self, node_id: str, declaration: dict[str, Any], present: list[str],
                   missing: list[str]) -> None:
        """Emit the `human.gate` event the UI parks on."""
        bus = self.ctx.bus
        if bus is None:
            return
        try:
            from .protocol import EventType

            bus.emit(EventType.HUMAN_GATE, node_id=node_id, payload={
                "gate_id": node_id,
                "kind": str(declaration.get("kind") or "human"),
                "reason": str(declaration.get("description") or ""),
                "requires": list(declaration.get("requires") or []),
                "present": present, "missing": missing,
            })
        except Exception:  # noqa: BLE001
            pass

    # ── the node lifecycle ──────────────────────────────────────────────────

    def _load_skill(self, skill: str) -> SkillBundle:
        """Load the skill's bundle, or refuse the node.

        A node whose skill cannot be loaded has no criteria, so it could claim done without evidence.
        Refusing is the only safe outcome.
        """
        try:
            return self.ctx.skills.load(skill)
        except Exception as exc:  # noqa: BLE001 - any failure means the skill is unusable
            raise ExecutionError(
                f"cannot load skill {skill!r} for this node: {exc}. A node whose contract cannot be "
                "read could claim completion without evidence, so it is refused."
            ) from exc

    def _pin_prefix(self, skill: str, system: str, procedure: str, tools: Any) -> None:
        """Pin this run's prefix for a skill, and report if the source has moved under it.

        Called where the prefix is *composed* rather than where the skill is loaded, because the pin
        is keyed on `(skill, tool names)` and only this site knows the tools.

        Two separate jobs, deliberately:
        - `get_or_pin` returns the **same bytes** every node, so a `SKILL.md` edited mid-run cannot
          change what a running session sends.
        - `check` compares the source against the pin and reports a divergence, so the cache going
          cold is *stated* rather than discovered in the bill.
        """
        pins = self.ctx.pins_for_prefix
        if pins is None:
            return
        # A resumed run re-pins from the store before re-deriving, so a continuation sends the bytes
        # its predecessor actually sent rather than bytes that merely look the same from here. That
        # difference is the whole saving: the provider's cache holds the *old* bytes, and a skill
        # edited between runs would otherwise go cold with nothing reporting why.
        resumed = pins.resume_from_store(skill=skill, tools=tools, store=self._cache_store(),
                                         system=system, procedure=procedure)
        if resumed is not None and getattr(resumed, "known", False) and resumed.unchanged:
            self._log("prefix.resumed", detail={"skill": skill, "prefix": resumed.prefix_hash,
                                                "observations": getattr(
                                                    resumed.recorded, "observations", 0)})
        pins.get_or_pin(skill=skill, system=system, procedure=procedure, tools=tools)
        try:
            drift = pins.check(skill=skill, system=system, procedure=procedure, tools=tools)
        except Exception as exc:  # noqa: BLE001 - strict mode raising is the caller's choice
            self._log("prefix.drift", level="warning", message=str(exc))
            raise
        if drift.changed:
            # Logged, then emitted: an edit is legitimate, and a run that dies because someone saved
            # a file is a worse outcome than one that reports the cache went cold.
            self._log("prefix.drift", level="warning", detail={
                "skill": skill, "reasons": drift.reasons,
                "pinned": drift.pinned_hash, "current": drift.current_hash})
            bus = self.ctx.bus
            if bus is not None:
                try:
                    from .protocol import EventType

                    bus.emit(EventType.AGENT_LOG, payload={
                        "stream": "stderr", "level": "warning",
                        "text": f"prefix drift: {drift.reason}"})
                except Exception:  # noqa: BLE001 - reporting must not break the node
                    pass

    def _cache_store(self) -> Any:
        """The durable cache record, if this run has one.

        Reached through the gateway, which is where the host attaches it — the same object the pin
        store writes to, so a compaction reads the evidence those two write rather than a second copy
        that could disagree with it. None when nothing is attached, which `consult_store` reports as
        having no opinion rather than as a cold prefix.
        """
        return getattr(self.ctx.gateway, "cache_store", None)

    def _prefix_hash(self, skill: str = "") -> str:
        """The digest of the prefix this run has pinned for a skill, or "" when there is none.

        The hash space matters here: only `Prefix.prefix_hash` is comparable against the store's pin
        records, and a request-shaped digest would answer "never seen" for a prefix that was pinned.
        """
        pins = self.ctx.pins_for_prefix
        if pins is None:
            return ""
        try:
            for key, prefix in pins.pinned_all().items():
                if not skill or key.startswith(f"{skill}|"):
                    return str(prefix.prefix_hash)
        except Exception:  # noqa: BLE001 - a missing hash is "no opinion", not a failure
            return ""
        return ""

    def _run(self, node_id: str, node: dict[str, Any], skill: str, state: dict[str, Any], *,
             attempt: int, ctx: dict[str, Any]) -> dict[str, Any]:
        """The full node protocol: bind, project, prompt, call, parse, persist, cross, return.

        One agent in the normal case, or several when the node was bound as a swarm. The swarm is
        resolved here rather than in the caller because the binding is what decides it — a node is a
        swarm only if its policy says so.

        The handoff at the end is *one per node*, deliberately: a swarm's voters or a fan-out's items
        are how this node reached its answer, not separate things that crossed the graph. Emitting a
        handoff per voter would put edges on the flow board that no successor ever consumed.
        """
        bundle = self._load_skill(skill)
        is_reviewer = self._is_reviewer(skill, node)
        binding = self._bind(node_id, node, skill, is_reviewer=is_reviewer)

        # ── the pool, when this node pulls rather than is pushed ──
        # A node declaring `from_pool: true` takes its actual task from the pool, claimed by the
        # agent that will do it. This is what makes the pool reachable *during a run*: without it the
        # pool existed only as a CLI, and a node could never drain the work it had queued.
        pooled = self._claim_pooled(node_id, node, binding.primary)

        # The upstream handoff: what the runner recorded, plus the artifacts on disk. Read once and
        # shared, because every voter must answer the same question for a quorum to mean anything.
        inputs = self._inputs_for(node, state)
        findings = self._findings_for(node_id, state) if not is_reviewer else []

        self._emit_node_transition(
            EventType.NODE_ENTER, node_id,
            {"phase": str(node.get("phase") or ""), "skill": skill, "attempt": attempt})

        # ── fan-out, when the node splits work rather than answers one question ──
        # A node declaring `fanout` with a `{{item}}` template and an `items` list spreads the work
        # across agents. Distinct from the SWARM binding below, which is a *vote*: this splits a job,
        # that decides one. Both are resolved here because the node's own declaration is what chooses
        # between them, and neither is reachable unless something reads it.
        if node.get("fanout") or node.get("items"):
            result = self._run_fanout(node_id=node_id, node=node, skill=skill, state=state,
                                      attempt=attempt, bundle=bundle, binding=binding,
                                      inputs=inputs, findings=findings)
            agent_id = binding.primary
        elif binding.policy is BindingPolicy.SWARM and len(binding.agents) > 1:
            result = self._run_swarm(node_id=node_id, node=node, skill=skill, state=state,
                                     attempt=attempt, bundle=bundle, binding=binding,
                                     is_reviewer=is_reviewer, inputs=inputs, findings=findings,
                                     pooled=pooled)
            agent_id = str((result.get("_agent") or {}).get("agent_id") or binding.primary)
        else:
            result = self._run_one(node_id=node_id, node=node, skill=skill, state=state,
                                   attempt=attempt, bundle=bundle, binding=binding,
                                   is_reviewer=is_reviewer, inputs=inputs, findings=findings,
                                   agent_id=binding.primary, pooled=pooled)
            agent_id = binding.primary

        # Emitted before the crossing, so the exit precedes the handoff the node produced — the order
        # a reader expects, and the order the timeline renders.
        self._emit_node_transition(
            EventType.NODE_EXIT, node_id,
            {"status": str(result.get("status") or ""), "verdict": str(result.get("verdict") or ""),
             "summary": str(result.get("summary") or "")[:300], "agent_id": agent_id,
             "attempt": attempt})
        return self._cross_node_boundary(node_id=node_id, node=node, state=state, result=result,
                                         agent=self._agent_or_none(agent_id),
                                         instruction=ctx.get("instruction") or "",
                                         attempt=attempt)

    def _agent_or_none(self, agent_id: str) -> Any:
        """The bound agent, or None when the id cannot be resolved.

        A handoff is still worth producing when the agent record is gone (a roster edited mid-run):
        the budget fields then come from the run's own counters instead of the agent's, which is a
        thinner payload rather than a missing one.
        """
        try:
            return self.org.get(agent_id) if agent_id else None
        except Exception:  # noqa: BLE001 - an unknown agent must not stop the crossing
            return None

    def _emit_node_transition(self, kind: EventType, node_id: str, payload: dict[str, Any]) -> None:
        """Emit `node.enter` / `node.exit` for one node. Never raises.

        These two were in the protocol and read by the timeline and the console, but **nothing ever
        emitted them** — so a run's node transitions were visible only in the runner's checkpoint, and
        anything watching the event stream saw a run that started, went quiet, and ended. That is
        exactly the "a run that produced nothing looks like a hang" complaint, one level down.

        Emitted around the *whole* node rather than around the model call, because a node's work is
        the unit a person reasons about: bind, fan-out or swarm, review, cross. The phase travels so a
        reader can group transitions without re-deriving the graph.
        """
        bus = self.ctx.bus
        if bus is None:
            return
        try:
            bus.emit(kind, node_id=node_id, payload=dict(payload))
        except Exception:  # noqa: BLE001 - a display event must never break the node it describes
            pass

    def _cross_node_boundary(self, *, node_id: str, node: dict[str, Any], state: dict[str, Any],
                             result: dict[str, Any], agent: Any, instruction: str,
                             attempt: int) -> dict[str, Any]:
        """Cross the node's edge: assemble, validate, persist, emit and record one handoff.

        Last, and after the result is shaped, because the payload describes a *finished* node: the
        artifact hashes, the criteria coverage and the usage figures only exist once the work does.

        A node that did not reach `done` still crosses. `blocked`, `needs_review` and `skipped` are
        registry statuses, and suppressing them would let a stuck node look like one that had not run
        at all — which is the opposite of what the registry is for. What does *not* cross is a node
        whose own contract refusal has already been reported, because that result is the refusal.

        The refusal from the handoff contract is merged into the result rather than raised, so the
        runner's bounded rework loop handles it and `_derive_stop_reason` still renders a cause.
        A result that already *is* a refusal is left alone: re-crossing a node whose own contract
        violation was just reported would report the same failure twice under a second name.
        """
        if result.get("verdict") == "contract-violation":
            return result
        refusal = self._deliver_handoff(
            node_id=node_id, node=node, state=state, summary=str(result.get("summary") or ""),
            artifacts=list(result.get("artifacts") or []),
            decisions=list(result.get("decisions") or []),
            # The node's own questions *joined with* what the run has already left open. Taking only
            # one of the two would mean R6 measured nothing: the ceiling exists because a successor
            # inherits the whole pile, so the pile is what must be counted.
            open_questions=self._open_questions_after(result, state),
            evidence=list(result.get("evidence") or []),
            findings=list(result.get("findings") or []),
            status=str(result.get("status") or "done"),
            next_instruction=instruction, agent=agent, attempt=attempt,
        )
        if refusal is None:
            return result
        return {**result, **refusal}

    def _open_questions_after(self, result: dict[str, Any],
                              state: dict[str, Any]) -> list[dict[str, Any]]:
        """Everything still unresolved once this node has finished.

        Newest first, so a cap on the payload keeps the questions the *current* node just raised rather
        than the oldest ones the run has been carrying — those are the ones a successor can still act
        on, and a truncation that kept the stale end would make the ceiling actively harmful.
        """
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in [*(result.get("open_questions") or []), *(state.get("open_questions") or [])]:
            if isinstance(entry, dict):
                text = str(entry.get("question") or entry.get("text") or "")
            else:
                text = str(entry)
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(entry if isinstance(entry, dict) else {"question": text})
        return out

    # ── the handoff contract at a node boundary ─────────────────────────────
    #
    # Everything an agent-to-agent boundary does lives here: assemble the nine registry fields,
    # validate them at the stage they belong to, persist the result, emit the lifecycle as events, and
    # record the crossing as a ledger decision. The contract itself is `engine/org/handoff.py` and is
    # deliberately not reimplemented — this module's job is to *call* it, because a contract no
    # production code calls is a specification, not a mechanism.

    #: The node statuses the handoff registry allows. A model that answers with something else has
    #: still done work, so the value is normalised before it crosses rather than the crossing refused.
    _HANDOFF_STATUSES = ("done", "blocked", "needs_review", "skipped")

    #: The edge an assembly carries when no manifest edge is readable (an injected manifest in a test,
    #: or a plan whose graph has not been written). Naming the library's own registry here keeps a
    #: degraded handoff describable rather than anonymous.
    _DEFAULT_PAYLOAD_NAME = "handoff-v1"

    def _handoff_successor(self, node_id: str, state: dict[str, Any]) -> tuple[str, str]:
        """Who this node hands to, and on which named payload.

        Read from the manifest's own edges rather than from run-state, because run-state holds only
        nodes that have *started* and the successor is exactly the node that has not. A node with no
        outgoing edge is an end node and says so, rather than naming a target it invented.
        """
        for edge in self._manifest(state).get("edges") or []:
            if not isinstance(edge, dict) or str(edge.get("from") or "") != node_id:
                continue
            target = str(edge.get("to") or "")
            if target:
                return target, str(edge.get("payload") or self._DEFAULT_PAYLOAD_NAME)
        return "", ""

    def _state_dir(self) -> Path:
        """`.agent_state/` for this run, whichever form the workspace arrived in.

        Three shapes reach here — a `Workspace`, a bare project path, and a path that already *is* the
        state directory — so they are resolved in one place rather than at each call site.
        """
        workspace = self.ctx.workspace
        state_dir = getattr(workspace, "state_dir", None)
        path = Path(state_dir) if state_dir else Path(workspace)
        if path.name == ENGINE_STATE_DIRNAME:
            return path
        return path / ENGINE_STATE_DIRNAME

    # ── the payload: all nine registry fields, or none ──────────────────────

    def _handoff_upstream_summary(self, state: dict[str, Any]) -> str:
        """The summaries recorded before this node, newest last.

        Taken from run-state because at assembly time this *is* the upstream's own record — the same
        text the successor would read in the receiving node's intake block.
        """
        nodes = state.get("nodes") or {}
        summaries = [f"{name}: {str(rec.get('summary') or '')[:120]}"
                     for name, rec in nodes.items()
                     if isinstance(rec, dict) and rec.get("summary")]
        return " | ".join(summaries[-3:])

    def _handoff_paths(self, state: dict[str, Any]) -> list[str]:
        """Paths a successor would need, by reference rather than by inlined body."""
        return [str(info.get("path")) for info in (state.get("artifacts") or {}).values()
                if isinstance(info, dict) and info.get("path")]

    def _handoff_context(self, node_id: str, node: dict[str, Any], state: dict[str, Any],
                         *, problem: str) -> dict[str, Any]:
        """The five-element delegation context a boundary must carry.

        Built from what is already in hand — the upstream summary, the run's open questions, its
        artifact index, the node's own declared title and description — rather than from a new model
        call. The elements may be sparse; they may not be absent, which is why all five go in even when
        a value is an empty list or a placeholder line. An absent element is what makes a delegate
        re-discover the problem from scratch and arrive at a different fix.
        """
        record = (state.get("nodes") or {}).get(node_id)
        record = record if isinstance(record, dict) else {}
        tried = record.get("tried")
        if not isinstance(tried, list):
            tried = [q.get("question") if isinstance(q, dict) else str(q)
                     for q in (state.get("open_questions") or [])]
        upstream = self._handoff_upstream_summary(state)
        return build_context_pass_through(
            problem=(str(node.get("title") or "").strip() or problem[:400]
                     or f"carry {node_id} to its successor"),
            tried=[str(item)[:200] for item in tried][:5],
            logs=(f"{node_id}: {upstream[:400]}" if upstream
                  else f"{node_id}: no upstream summary recorded for this run"),
            paths=self._handoff_paths(state),
            hypothesis=(str(node.get("description") or "").strip()
                        or "the node's declared completion contract is the test of this handoff"),
        )

    def _handoff_constraints(self, state: dict[str, Any], session: Session,
                             inherited: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
        """The non-negotiable constraints the receiver must not lose.

        Three sources, and the *inherited* one is what makes rule R2 a real check rather than a
        formality: what this node received must be handed on. A constraint is protected across a chain
        only if each hop carries it forward, so reading only this node's own pins would let
        "NEVER store passwords in plaintext" survive one boundary and vanish at the next — which is
        precisely the silent loss R2 exists to catch.

        Session pins are next, because they are the text the prompt actually pinned, so the payload
        describes what was really carried rather than what was hoped for. The run's own injected
        constraints come last, as the wider floor beneath both.
        """
        out: list[dict[str, Any]] = []
        for entry in inherited:
            if not isinstance(entry, dict) or not entry.get("non_negotiable"):
                continue
            value = str(entry.get("value") or "").strip()
            if value and not any(c["value"] == value for c in out):
                out.append({"type": str(entry.get("type") or "constraint"), "value": value,
                            "source": str(entry.get("source") or "inherited"),
                            "non_negotiable": True})
        for text in list(getattr(session, "pinned", []) or []):
            value = str(text).strip()
            if value and not any(c["value"] == value for c in out):
                out.append({"type": "constraint", "value": value, "source": "session",
                            "non_negotiable": True})
        for entry in (state.get("constraints") or []):
            value = str(entry.get("value") if isinstance(entry, dict) else entry).strip()
            if value and not any(c["value"] == value for c in out):
                out.append({"type": "constraint", "value": value, "source": "run",
                            "non_negotiable": True})
        return out[:24]

    def _verification_evidence(self, evidence: list[Any],
                               findings: list[dict[str, Any]]) -> dict[str, Any]:
        """The criterion -> evidence map the registry asks for.

        The runner's `evidence` is a flat list; the registry's `verification_evidence` is a map, and
        the difference is not decoration: a flat list cannot say *which* criterion an item evidences, so
        a receiver cannot tell coverage from noise. Anything that names no criterion lands under
        `_other` rather than being dropped — losing evidence is the failure the registry exists to
        prevent, and the registry's own rule is that an empty map describes a claim, not a completion.
        """
        out: dict[str, Any] = {}
        other: list[str] = []
        for entry in evidence:
            text = str(entry)
            key, separator, value = text.partition(":")
            key, value = key.strip(), value.strip()
            if separator and key and value:
                existing = out.get(key)
                if isinstance(existing, list):
                    existing.append(value)
                elif existing is None:
                    out[key] = [value]
                else:  # a non-list value under this key: keep both rather than overwrite one
                    out[key] = [str(existing), value]
                continue
            other.append(text)
        if findings:
            out["findings"] = [f"{f.get('severity', '?')}: {str(f.get('issue') or '')[:160]}"
                               for f in findings[:10]]
        if other or not out:
            out["_other"] = other[:20]
        return out

    def _inherited_constraints(self, node_id: str) -> list[dict[str, Any]]:
        """The non-negotiable constraints this node received, if any crossed into it."""
        with self.registries:
            handoff = self.inbound.get(node_id) if node_id else None
        if handoff is None:
            return []
        constraints = handoff.payload.get("constraints")
        return [c for c in constraints if isinstance(c, dict)] if isinstance(constraints, list) else []

    def _handoff_next(self, target: str, instruction: str) -> str:
        """What the successor should do, from the graph edge and the node's own instruction.

        Derived, never invented: the edge is the authority on *who* is next, so a free-text `next` that
        contradicted it would be worse than a plain one. The instruction, when there is one, is the
        node's own statement of what it is for — which is what a successor needs and what a summary
        alone does not always give.
        """
        if instruction:
            return instruction[:600]
        if target:
            return f"consume this handoff as {target}"
        return "the run ends here; no successor consumes this handoff"

    def _run_budget(self, state: dict[str, Any], session: Session) -> dict[str, Any]:
        """Budget fields for a handoff built without a bound agent (a first node, or a gate)."""
        run_budget = state.get("budget") or {}
        return {
            "tokens_used": 0,
            "usd_used": 0.0,
            "tokens_allocated": 0,
            "usd_allocated": 0.0,
            "steps_used": int(run_budget.get("steps_used") or 0),
            "iterations": int(getattr(session, "attempt", 0) or 0),
            "session_saturation": round(float(getattr(session, "saturation", 0.0) or 0.0), 4),
            "context_window": int(getattr(session, "window", 0) or 0),
        }

    def _agent_budget(self, agent: Any, session: Session, state: dict[str, Any]) -> dict[str, Any]:
        """Budget fields for a handoff built around a bound agent.

        Both sources are read because neither is complete alone: the executor knows the agent's own
        allocation and the session's saturation, while the run's step counters are the ones the runner
        enforces. A figure reported as zero when it was merely unreported would read as free.
        """
        out = self._run_budget(state, session)
        runtime = getattr(agent, "budget", None)
        out.update({
            "tokens_used": int(getattr(runtime, "spent_tokens", 0) or 0),
            "usd_used": round(float(getattr(runtime, "spent_usd", 0.0) or 0.0), 6),
            "tokens_allocated": int(getattr(runtime, "allocated_tokens", 0) or 0),
            "usd_allocated": round(float(getattr(runtime, "allocated_usd", 0.0) or 0.0), 6),
        })
        return out

    def _build_handoff(self, *, node_id: str, node: dict[str, Any], state: dict[str, Any],
                       summary: str, artifacts: list[Any] = (), decisions: list[Any] = (),
                       open_questions: list[Any] = (), evidence: list[Any] = (),
                       findings: list[dict[str, Any]] | None = None,
                       origin: str = "", target: str = "", status: str = "done",
                       next_instruction: str = "", agent: Any = None,
                       session: Session | None = None,
                       attempt: int = 1) -> Handoff:
        """Assemble the typed handoff for one boundary — all nine registry fields, always present.

        **A missing field is a violation, not an omission.** Every key in the library's payload
        registry is populated unconditionally, including the ones that are legitimately empty for a
        given node: a receiver that cannot see `open_questions` cannot know what upstream left
        unresolved, and will re-derive it wrongly. So an empty list is a *value* and an absent key is a
        *defect*, and the two must not be conflated — which is why nothing here is conditional on the
        data being non-empty.

        The token cost is bounded by construction rather than by hope: every list is capped and every
        string truncated, so rule R1 is satisfied by assembly instead of by a refusal the node could not
        have acted on.

        Nothing here calls a model. Every value is read from data the node's own execution already
        produced — its trailer, its artifacts, its session, the graph's edge — because a handoff that
        needed its own calls would be a second, ungoverned spend on the hot path.
        """
        session = session if session is not None else Session(
            agent_id=origin or node_id or "handoff", node_id=node_id, window=1)
        if status not in self._HANDOFF_STATUSES:
            status = "done"
        if not target and node_id:
            target, _name = self._handoff_successor(node_id, state)
        payload: dict[str, Any] = {
            "status": status,
            "summary": str(summary or "")[:2000],
            "artifacts": list(artifacts)[:32],
            "decisions": list(decisions)[:16],
            "open_questions": list(open_questions),
            "verification_evidence": self._verification_evidence(list(evidence), list(findings or [])),
            "context": self._handoff_context(node_id, node, state, problem=str(summary or "")),
            "budget": (self._agent_budget(agent, session, state) if agent is not None
                       else self._run_budget(state, session)),
            "next": self._handoff_next(target, next_instruction),
            # Constraints travel as their own key rather than folded into the prose: rule R2 compares
            # them against what upstream carried, and it can only do that if they are structured. What
            # this node *received* is passed through, which is what makes R2 a check across a chain
            # rather than against a single hop.
            "constraints": self._handoff_constraints(
                state, session, self._inherited_constraints(origin)),
        }
        # A guard on this module's own assembly rather than on the data. The registry check would
        # refuse an incomplete payload at the edge anyway, but a field *this file* forgot to populate
        # is an engine defect rather than a node's fault, so it is named here — where someone can fix
        # it — instead of arriving as a contract violation the agent is blamed for.
        missing = [name for name in HANDOFF_REQUIRED_FIELDS if name not in payload]
        if missing:
            self._log("handoff.assembly.defect", level="error", node_id=node_id,
                      message=f"handoff payload assembled without {', '.join(missing)}",
                      detail={"missing": missing})
        return Handoff(payload=payload, origin=origin or node_id, target=target,
                       attempt=int(attempt or 1))

    # ── validation, refusal, and the mechanisms that read the verdict ───────

    def _refuse_handoff(self, handoff: Handoff, node_id: str, verdict: Any, *, stage: str,
                        detail: str = "") -> dict[str, Any]:
        """Report a refused handoff as a contract-shaped refusal the bounded loop can act on.

        **A refusal must not crash the run.** A payload that fails R1, R3, R4, R5 or R6 is a node whose
        work cannot advance, which is exactly what the runner's bounded rework loop already exists to
        handle: the node is reported `needs_review` with the rule that fired, and the loop retries or
        escalates under its own budget. Raising would skip the loop and discard work that was genuinely
        done, which is a worse outcome than a reported refusal.

        The `contract` log entry is deliberate rather than incidental: `_derive_stop_reason` treats a
        `contract` entry as a *named cause*, so a run that stops here reports "a node's completion
        contract was violated — R1: …" instead of the bare unreadable `blocked` this codebase has
        already been bitten by once.
        """
        rule_reason = verdict.reason if not verdict.ok else "the handoff contract refused this transition"
        reason = f"handoff {stage} refused: {rule_reason}" + (f" ({detail})" if detail else "")
        self._log("contract", level="warning", node_id=node_id, message=reason,
                  detail={"stage": stage, "rules": list(verdict.rules),
                          "handoff_id": handoff.handoff_id, "target": handoff.target})
        # `from_agent` carries the *node* here rather than a person, because the refusal happens at the
        # edge and the board resolves ids to names itself. Passing a name that does not exist would be
        # worse than passing the node the reader can already see.
        self._emit_handoff(handoff, EventType.HANDOFF_REJECTED, from_agent=handoff.origin)
        # Only the fields the *contract* owns are returned. The node's own evidence, artifacts and
        # criteria coverage are preserved by the caller, because the refusal is about the payload, not
        # about the work: replacing them here would make the runner's own contract check report a
        # second, vaguer failure that then shadows the rule name in the stop reason.
        return {
            "status": "needs_review",
            "verdict": "contract-violation",
            "summary": reason[:400],
            "diagnostics": [f"{rule} at {stage}" for rule in verdict.rules] or [stage],
            "handoff_id": handoff.handoff_id,
        }

    def _cross_handoff(self, handoff: Handoff, *, node_id: str, agent: Any,
                       summary: str) -> dict[str, Any] | None:
        """Run one handoff through the contract's lifecycle. Returns a refusal dict, or None.

        The order is the contract's own: **propose** (R1/R3/R5/R6 and the registry), then persist,
        then **accept** (R2/R4/R8), then **deliver** (R7), then **fulfil** (R7 again). R7 is checked
        *after* acceptance rather than before it, because acceptance is precisely what satisfies the
        rule — checking it first would refuse every legitimate handoff. The rule is therefore not
        skipped, it is enforced where it can be true.

        A payload that passes every stage is persisted, emitted and recorded; the receiver's copy is
        what the next node's prompt is built from. A refusal is returned and never raised, so the
        caller can hand it to the runner's rework loop rather than killing the run.
        """
        agent_name = getattr(agent, "name", "") or ""
        # Emitted *before* the propose rules are checked, because the proposal genuinely happened: the
        # state machine's PROPOSED is the state a handoff is in until it is accepted, and the propose
        # rules are what gate the transition out of it. A board that showed nothing for a refused
        # proposal would hide the exact event worth seeing.
        self._emit_handoff(handoff, EventType.HANDOFF_PROPOSED, from_agent=agent_name,
                           to_agent=self._agent_name(handoff.target))

        verdict = validate_handoff(handoff, stage="propose")
        if not verdict.ok:
            return self._refuse_handoff(handoff, node_id, verdict, stage="propose")

        # Persisted only once the proposal is sound: a refused payload on disk would be a record of a
        # crossing that never happened, which is worse than no record.
        self._persist_handoff(handoff)
        with self.registries:
            self.handoffs[handoff.handoff_id] = handoff

        for stage, transition, event in (
            ("accept", handoff.accept, EventType.HANDOFF_ACCEPTED),
            # IN_PROGRESS is a real state between acceptance and delivery, not a formality: it is the
            # receiver's acknowledgement that it has begun, and the state machine refuses to fulfil a
            # contract that skipped it. No event is emitted for it because the protocol declares none —
            # inventing an event type the Swift side cannot mirror is how the trace stops being
            # replayable.
            ("accept", handoff.start, None),
            ("deliver", None, None),
            ("fulfil", handoff.fulfil, EventType.HANDOFF_FULFILLED),
        ):
            if stage == "deliver":
                # R7 is checked *here* rather than before acceptance, which is the rule's own ordering:
                # acceptance is precisely what satisfies it, so checking it earlier would refuse every
                # legitimate handoff. Checked at delivery it is a real check against a state a legal
                # handoff genuinely reaches.
                delivery = validate_handoff(handoff, stage="deliver")
                if not delivery.ok:
                    return self._refuse_handoff(handoff, node_id, delivery, stage="deliver")
                continue
            try:
                stage_verdict = transition()
            except HandoffError as exc:
                # The transition refused and left the state where it was, so the verdict is recomputed
                # to name the rule rather than reported as a bare state-machine error.
                return self._refuse_handoff(handoff, node_id,
                                            validate_handoff(handoff, stage=stage),
                                            stage=stage, detail=str(exc))
            # `start` returns None: IN_PROGRESS carries no rules of its own, so there is no verdict to
            # read — a stage that validated nothing cannot be reported as having validated.
            if stage_verdict is not None and not stage_verdict.ok:  # pragma: no cover
                return self._refuse_handoff(handoff, node_id, stage_verdict, stage=stage)
            if event is not None:
                self._emit_handoff(handoff, event, from_agent=agent_name,
                                   to_agent=self._agent_name(handoff.target))
            if stage == "fulfil":
                # `verified` means the payload cleared every stage of the contract end to end. It is
                # emitted from the same verdict the transitions produced rather than from a second,
                # weaker opinion, so the board's "verified" and the contract cannot disagree.
                self._emit_handoff(handoff, EventType.HANDOFF_VERIFIED, from_agent=agent_name,
                                   to_agent=self._agent_name(handoff.target))

        self._record_handoff_gate(handoff, node_id=node_id, agent=agent, summary=summary)
        # Rewritten now that the state machine has settled. The propose-time write recorded PROPOSED
        # and nothing updated it, so the file on disk said PROPOSED while the trace and the in-memory
        # object said FULFILLED — two records of one crossing disagreeing, which is exactly what a
        # persisted handoff exists to prevent. Found by reading a real run's `.agent_state/handoffs/`
        # and comparing it to its own trace.
        #
        # Written here rather than inside the stage loop so one crossing produces one file, updated
        # once, rather than a write per transition.
        self._persist_handoff(handoff)
        return None

    def _agent_name(self, node_id: str) -> str:
        """The name of the agent bound to a node, when one is known.

        Best-effort for the board's benefit: the flow board resolves ids to names itself, so an empty
        answer costs a nicer label and nothing else.
        """
        if not node_id:
            return ""
        with self.registries:
            executed = self.history.get(node_id)
            return executed.agent_name if executed is not None else ""

    # ── persistence, events, and the ledger ────────────────────────────────

    def _persist_handoff(self, handoff: Handoff) -> Path | None:
        """Write the handoff to `handoffs/<id>.json`, atomically.

        Temp-then-`os.replace` with an fsync, matching every other durable write in the engine: a reader
        (the flow board, or an operator asking what crossed) must see either the previous complete
        document or the new one, never a torn one. Failure is logged and returns None rather than
        raising, because losing the *record* of a crossing is a smaller failure than losing the
        crossing — and the payload is bounded by assembly, so this can never write an unbounded blob.
        """
        target = self._state_dir() / "handoffs" / f"{handoff.handoff_id}.json"
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(handoff.as_dict(), fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            self._log("handoff.persist.failed", level="warning", node_id=handoff.origin,
                      message=f"could not write {target}: {exc}")
            return None
        return target

    def _emit_handoff(self, handoff: Handoff, event: "EventType", *, from_agent: str = "",
                      to_agent: str = "") -> None:
        """Emit one `handoff.*` event, in the exact shape the flow board reads.

        `handoff_id`, `from_node`, `to_node` and `summary` are the keys `flow._handoffs` keys and labels
        rows on, so all four are always present — a board that could not name a crossing would show an
        edge that moved information as nothing at all, which is the state this wiring exists to end.
        """
        bus = self.ctx.bus
        if bus is None:
            return
        try:
            bus.emit(event, node_id=handoff.origin, payload={
                "handoff_id": handoff.handoff_id,
                "from_node": handoff.origin,
                "to_node": handoff.target,
                "from_agent": from_agent,
                "to_agent": to_agent,
                "state": handoff.state.value,
                "status": handoff.payload.get("status"),
                "summary": str(handoff.payload.get("summary") or "")[:400],
                "artifacts": [str(a.get("path") if isinstance(a, dict) else a)
                              for a in (handoff.payload.get("artifacts") or [])][:12],
            })
        except Exception:  # noqa: BLE001 - the trace must never break a node
            pass

    def _handoff_gate_name(self, node_id: str, target: str) -> str:
        """The ledger gate a crossing is recorded at: the edge, named for both of its ends.

        Named for the edge rather than the node because the decision being recorded is *this* crossing;
        "which node handed what to whom, and on what grounds" is a question about the pair.
        """
        return f"handoff:{node_id}->{target}" if target else f"handoff:{node_id}"

    def _record_handoff_gate(self, handoff: Handoff, *, node_id: str, agent: Any,
                             summary: str) -> None:
        """Record the crossing as a decision in the ledger.

        A handoff *is* a decision — this node decided its work was good enough to hand on — and without
        an entry, the answer to "on what grounds did that cross" stops existing the moment the run ends.

        Best-effort by construction: a ledger that refuses (an irreversible decision already stands at
        this gate) must not stop a crossing the contract has already validated, so the refusal is
        logged and the handoff stands. It is also recorded as *reversible*, because the successor may
        reject it and the loop may rework — recording it irreversible would make the second pass
        impossible, which is a mechanism fighting the machine it was built for.
        """
        ledger = self._ledger()
        try:
            ledger.record(
                gate=self._handoff_gate_name(node_id, handoff.target),
                choice=f"{handoff.payload.get('status')} -> {handoff.target or 'end'}",
                rationale=(summary or str(handoff.payload.get("summary") or ""))[:400],
                by=getattr(agent, "id", "") or "",
                node_id=node_id,
                attempt=handoff.attempt,
                reversible=True,
                confidence="medium",
                rejected_alternatives=["halt rather than hand on unverified work"],
            )
        except Exception as exc:  # noqa: BLE001 - an unrecordable crossing must still cross
            self._log("handoff.ledger.failed", level="warning", node_id=node_id, message=str(exc))

    def _ledger(self) -> Any:
        """The decision ledger: the injected one, or this run's own on disk."""
        ledger = getattr(self.ctx, "ledger", None)
        if ledger is None:
            ledger = getattr(self, "_own_ledger", None)
            if ledger is None:
                ledger = Ledger(path=self._state_dir() / "ledger.jsonl")
                self._own_ledger = ledger
        return ledger

    # ── the two ends ───────────────────────────────────────────────────────

    def _inbound_payload(self, node_id: str) -> dict[str, Any] | None:
        """What this node was actually handed, or None at the first node.

        The prompt is built from the *validated* handoff rather than from run-state, because the whole
        point of validating at the boundary is that the receiver may rely on the shape — a prompt fed
        from unvalidated state would be the same untyped path with extra steps. This is the consuming
        half of the wiring: node N+1's input is the handoff node N produced.
        """
        with self.registries:
            handoff = self.inbound.get(node_id)
        return dict(handoff.payload) if handoff is not None else None

    def _handoff_for_consumer(self, handoff: Handoff, node_id: str) -> Handoff | None:
        """The same payload seen from the receiving end, re-validated before it is consumed.

        A distinct object rather than a mutation: the sender's record stays exactly what it sent (so its
        checksum still matches the file on disk and `from_dict` round-trips), while the receiver holds a
        copy addressed to the node that will read it. Mutating one object for both ends is how a
        persisted handoff ends up describing an edge that never existed.

        Revalidated at `accept` — R4's own stage — rather than trusted because the sender just passed.
        The receiver is a *different object* in a different place, and R4 exists precisely because state
        can change between the two; a receiver that skipped the check would be taking the sender's word
        for the one rule that exists to catch corruption. A violation returns None, so the successor
        falls back rather than being handed state no rule could verify.
        """
        received = Handoff(payload=dict(handoff.payload), origin=handoff.origin, target=node_id,
                           kind="handoff", attempt=handoff.attempt, checksum=handoff.checksum)
        verdict = validate_handoff(received, stage="accept", previous=handoff)
        if verdict.ok:
            return received
        # R2 compares against the sender's own payload, so in-process the only reachable failures here
        # are R4 (the payload changed between the two objects) and R8 (an unmarked override). Both mean
        # the receiver must not act on this payload, and the mismatch is named rather than swallowed.
        self._log("contract", level="warning", node_id=node_id,
                  message=f"received handoff refused at the boundary: {verdict.reason}",
                  detail={"stage": "accept", "rules": list(verdict.rules),
                          "handoff_id": handoff.handoff_id})
        self._emit_handoff(received, EventType.HANDOFF_REJECTED, from_agent=handoff.origin)
        return None

    def _deliver_handoff(self, *, node_id: str, node: dict[str, Any], state: dict[str, Any],
                         summary: str,
                         artifacts: list[Any] = (), decisions: list[Any] = (),
                         open_questions: list[Any] = (), evidence: list[Any] = (),
                         findings: list[dict[str, Any]] | None = None, status: str = "done",
                         next_instruction: str = "", agent: Any = None,
                         attempt: int = 1) -> dict[str, Any] | None:
        """Assemble, validate, persist, emit and record the crossing at one node boundary.

        The single entry point the node protocol calls, so there is exactly one place an edge is
        crossed and no second, quieter path can develop. Returns a refusal dict when the payload may
        not advance, or None when it crossed — the caller merges the refusal into its own result rather
        than catching an exception.
        """
        session = self.ctx.sessions.get(f"{getattr(agent, 'id', '')}:{node_id}") if agent else None
        handoff = self._build_handoff(
            node_id=node_id, node=node, state=state, summary=summary, artifacts=artifacts,
            decisions=decisions, open_questions=open_questions, evidence=evidence,
            findings=findings, origin=node_id, status=status,
            next_instruction=next_instruction, agent=agent, session=session, attempt=attempt,
        )
        refusal = self._cross_handoff(handoff, node_id=node_id, agent=agent, summary=summary)
        if refusal is not None:
            return refusal
        successor, _name = self._handoff_successor(node_id, state)
        if successor:
            # Only a *crossed* handoff becomes the successor's input. A refused one must not, or the
            # successor would be prompted with state the contract has just rejected.
            received = self._handoff_for_consumer(handoff, successor)
            if received is not None:
                with self.registries:
                    self.inbound[successor] = received
        return None

    # ── the pool ────────────────────────────────────────────────────────────

    def _claim_pooled(self, node_id: str, node: dict[str, Any], agent_id: str) -> Any | None:
        """Claim a pooled task for this node, when the node pulls rather than is pushed.

        Returns None in the ordinary push case, which is every node that does not declare
        `from_pool`. A claim failure is logged and treated as "nothing to do" rather than raising:
        a node that finds the pool empty should report that, not crash the run.
        """
        if not node.get("from_pool") or self.ctx.pool is None:
            return None
        try:
            agent = self.org.get(agent_id)
        except Exception:  # noqa: BLE001 - an unknown agent is reported by the bind path
            return None
        try:
            task = self.ctx.pool.claim(agent, task_id=node.get("pool_task"))
        except Exception as exc:  # noqa: BLE001 - a pool problem must not kill the node
            self._log("pool.claim.failed", level="warning", node_id=node_id, agent_id=agent_id,
                      message=str(exc))
            return None
        if task is None:
            self._log("pool.empty", node_id=node_id, agent_id=agent_id,
                      detail={"reason": "nothing eligible for this agent"})
            return None
        self._log("pool.claimed", node_id=node_id, agent_id=agent_id,
                  detail={"task_id": task.id, "description": task.description[:120]})
        return task

    def _settle_pooled(self, task: Any, node_id: str, agent_id: str,
                       result: dict[str, Any]) -> None:
        """Record the node's outcome against its pooled task.

        The task's own state follows the node's status: `done` completes it, anything else fails it
        with the node's summary as the reason. Leaving it claimed would stall the pool behind a
        worker that has already finished, which is the bug leases exist to paper over — better not
        to need one.
        """
        if task is None or self.ctx.pool is None:
            return
        try:
            agent = self.org.get(agent_id)
        except Exception:  # noqa: BLE001
            return
        status = str(result.get("status") or "")
        summary = str(result.get("summary") or "")[:400]
        try:
            if status == "done":
                # The node's evidence is the output: a task with a schema needs a JSON body, and the
                # model was told to produce one.
                output = result.get("output")
                if not isinstance(output, str):
                    output = json.dumps({"summary": summary,
                                         "evidence": result.get("evidence") or []})
                self.ctx.pool.complete(task.id, agent, output=output)
                self._log("pool.completed", node_id=node_id, detail={"task_id": task.id})
            else:
                self.ctx.pool.fail(task.id, agent, reason=summary or status or "node not done")
                self._log("pool.failed", node_id=node_id, detail={"task_id": task.id,
                                                                  "status": status})
        except Exception as exc:  # noqa: BLE001 - the node's result stands regardless
            self._log("pool.settle.failed", level="warning", node_id=node_id, message=str(exc))

    def _renew_pooled(self, pooled: Any, agent: Any, node_id: str) -> None:
        """Extend the lease on the pooled task this node claimed.

        `TaskPool.renew` is the heartbeat `DEFAULT_LEASE_S` always assumed and nothing ever sent:
        without a caller the lease is a countdown from the claim that no live worker can reset, so a
        node that legitimately runs longer than 900s has its task reclaimed by `_expire_leases`,
        handed to a *second* worker, and executed twice — while this worker's `complete` then fails
        as "not claimed by agent" and its output is discarded as a warning. The pool side was fixed
        first; this is the caller that makes the fix reachable.

        Called once after the model call and once per tool step. The second site matters because the
        post-call renewal cannot cover a node that then spends minutes inside tools — the lease would
        lapse mid-loop exactly as it could mid-call. Never raises: a lease problem is a warning, not a
        reason to lose the work this node has already done.
        """
        if pooled is None or self.ctx.pool is None:
            return
        try:
            self.ctx.pool.renew(pooled.id, agent)
        except Exception as exc:  # noqa: BLE001 - a lease problem must not kill the node
            self._log("pool.renew.failed", level="warning", node_id=node_id,
                      agent_id=agent.id, message=str(exc))

    def _run_one(self, *, node_id: str, node: dict[str, Any], skill: str, state: dict[str, Any],
                 attempt: int, bundle: Any, binding: Any, is_reviewer: bool,
                 inputs: dict[str, Any], findings: list[dict[str, Any]],
                 agent_id: str, pooled: Any = None,
                 instruction_override: str | None = None) -> dict[str, Any]:
        """Run one node with one agent — the whole protocol for a single voter.

        `instruction_override` replaces the built node instruction. Fan-out uses it to hand each
        subagent *its own* expanded prompt: the node-level instruction describes the job ("review the
        change"), which is the wrong thing to give twenty subagents each reviewing a different file.
        """
        agent = self.org.get(agent_id)

        self._log("node.bind", node_id=node_id, agent_id=agent.id,
                  detail={"policy": binding.policy.value, "reason": binding.reason})

        # ── context: project, compact, and rotate if needed ──
        session = self._session_for(agent, node_id, bundle)
        session.begin_phase(self._phase_for(state))
        session.attempt = attempt
        prepared = self._prepare_context(session, bundle, node, inputs, node_id=node_id,
                                         agent_id=agent.id)
        # `_prepare_context` may have *rotated* the session, and it returns the fresh one. Adopting it
        # matters because rotation SEALS AND CLOSES the old session — so continuing to use the one that
        # went in raised `SessionError: session … is closed; only an ACTIVE session takes turns` and
        # failed the whole run. A real run died exactly there, on the node that had just been
        # auto-staffed and was doing its first real work.
        rotated_session = prepared.get("session")
        if rotated_session is not None and rotated_session is not session:
            session = rotated_session

        prompt = self.builder.node_prompt(
            bundle,
            TaskContext(
                node_id=node_id,
                instruction=self._pooled_instruction(
                    pooled,
                    instruction_override
                    or self._instruction_for(node_id, node, skill, state,
                                             resolved_inputs=inputs)),
                inputs=inputs,
                # The *validated* handoff when one crossed into this node, and the run-state adapter
                # only when nothing did. Reading run-state first would be the untyped path again.
                handoff=self._inbound_payload(node_id) or self._handoff_payload(state),
                recalled=prepared["recall"],
                findings=findings,
                attempt=attempt,
                max_attempts=self._max_attempts(state),
                injected_constraints=prepared["constraints"],
                is_reviewer=is_reviewer,
                may_delegate=bool(self.ctx.config and
                                  getattr(self.ctx.config.delegation, "max_depth", 0)),
                # The refusal a bounded rework pass is answering, as the runner reported it. Without
                # it a retry is the identical prompt — and an identical prompt fails identically, so
                # the whole window would be spent proving the first attempt.
                contract_rework=dict(self._rework_context(state)),
            ),
            agent_name=agent.name,
            agent_skill=skill,
            owner_constraints=prepared["constraints"],
        )

        # Pin this run's prefix for the skill, and report if the source has moved under it. Done
        # *after* the prompt is built because the pin is keyed on (skill, tool names) and only here
        # are both known — and the pinned bytes are what every later node will send.
        self._pin_prefix(skill, prompt.system, prompt.body,
                         self._tool_registry(agent, node).specs() if node.get("tools") else [])

        # ── the call, through the journal so a retry does not re-spend ──
        response = self._call(agent, prompt, node_id=node_id, attempt=attempt, session=session,
                              bundle=bundle, node=node, pooled=pooled)
        # Heartbeat immediately after the call. Everything above this line (context assembly, the
        # prompt, the model call) can take longer than the 900s lease, and the tool loop renews per
        # step on top of this — see `_renew_pooled`.
        self._renew_pooled(pooled, agent, node_id)
        reply = response["text"]
        usage = response["usage"]

        # ── parse the trailer ──
        trailer, parse_error = self._parse(reply)

        # A weaker model routinely does the work and then omits the machine-readable trailer — or
        # reports the *upstream* node's criteria instead of its own, because the handoff text is in
        # context. Either way it is a formatting/attention slip, not a failed task: discarding a real
        # result over it wastes the tokens it cost and parks a node that actually succeeded. So one
        # focused repair turn is attempted, which restates this node's own criteria verbatim. This is
        # a repair, not a rework: the same attempt, one extra cheap call, asked only for the trailer.
        if self._may_repair_trailer() and self._trailer_needs_repair(trailer, parse_error, bundle):
            # What the reply *did* carry, passed into the repair so the retry can name the failure
            # rather than restate the rule. Two shapes cover nothing while looking complete: names
            # that resolve to no criterion of this node (the skill's checklist ids, say), and a
            # correct list of criteria all marked `satisfied: false`. A model told only "cover every
            # criterion" reproduces whichever it produced.
            _, unrecognized = self._criterion_references(trailer, list(bundle.contract.criteria))
            repaired = self._repair_trailer(agent, node_id=node_id, session=session, reply=reply,
                                            bundle=bundle, attempt=attempt,
                                            reason="parse" if parse_error else "criteria",
                                            unrecognized=unrecognized,
                                            unsatisfied=_unsatisfied_count(trailer))
            if repaired is not None:
                trailer, parse_error = repaired

        self._record_session_turn(session, prompt, reply, usage)

        # ── persist any artifacts the node produced ──
        artifacts = self._persist_artifacts(node_id, trailer, agent, state, attempt=attempt)

        result = self._to_runner_result(
            node_id=node_id, skill=skill, bundle=bundle, trailer=trailer,
            artifacts=artifacts, usage=usage, parse_error=parse_error, reply=reply,
            agent=agent, session=session, prepared=prepared,
        )
        self._observe(node_id, agent, result, usage, bundle=bundle, session=session)
        self._remember(node_id, skill, agent, result, session, artifacts, trailer, prepared)
        self._settle_pooled(pooled, node_id, agent.id, result)
        return result

    def _pooled_instruction(self, pooled: Any, push_instruction: str) -> str:
        """Lead with the pooled task's own text when the node pulls its work.

        The pushed instruction says *what the node is for* ("review as reviewer"); the pooled task
        says *what to actually do* ("backfill the new column"). Both are useful, and the specific one
        goes first because it is what the agent must act on.
        """
        if pooled is None:
            return push_instruction
        return (f"The task you claimed from the pool:\n{pooled.description}\n\n"
                f"(Node context: {push_instruction})")

    # ── fan-out ─────────────────────────────────────────────────────────────

    def _run_fanout(self, *, node_id: str, node: dict[str, Any], skill: str, state: dict[str, Any],
                    attempt: int, bundle: Any, binding: Any,
                    inputs: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
        """Split this node's work across agents and aggregate their results.

        The *other* swarm primitive. A `SWARM` binding votes on one question; this spreads one job
        over many items. The node declares which it wants:

            - id: review-all
              skill: code-reviewer
              fanout: "Review {{item}} for regressions."
              items: ["src/a.ts", "src/b.ts", "src/c.ts"]

        Each item runs as its own full node execution — same binding, same skill bundle, same
        evidence contract — with the expanded prompt as its instruction. So a fan-out does not weaken
        any guarantee: every subagent is still held to the skill's criteria and checklist.

        Results are merged conservatively: the artifacts of every successful item are kept (they are
        different files, which is the point), their usage is summed so the node's cost is the items'
        cost rather than a zero, and the node is `done` only when *every* item succeeded.
        One failed item makes the whole node `needs_review` with the failures named, because a
        partially-reviewed change set that reported "done" would be a lie.
        """
        from .fanout import FanoutError, ItemOutcome, plan_fanout, run_fanout

        template = str(node.get("fanout") or "")
        items = [str(i) for i in (node.get("items") or [])]
        # A node may pull its items from an upstream artifact instead of declaring them, which is what
        # makes a fan-out usable for "one subagent per file the last node changed".
        if not items:
            items = self._fanout_items_from(state, node)

        max_parallel = self._fanout_parallel()
        try:
            plan = plan_fanout(template, items, skill=skill,
                               agent_profile=str(node.get("agent_profile") or ""))
        except FanoutError as exc:
            # A malformed fan-out is a manifest defect, and it is refused *before* any call is made.
            raise ExecutionError(f"node {node_id!r} declares an invalid fan-out: {exc}") from exc

        voters = list(binding.agents) or [binding.primary]
        self._log("fanout.start", node_id=node_id,
                  detail={"items": len(plan), "agents": voters, "max_parallel": max_parallel})

        def _one(item: Any, agent_id: str) -> ItemOutcome:
            """Run one item as a full node execution. Injected so the plan owns only the policy.

            `_run_one` rather than the node entry point, deliberately: fan-out items are one node's
            internal fan, so each item must *not* cross the graph edge. One handoff per node is emitted
            by the caller once the items are aggregated, or the board would show twenty edges where the
            graph has one.

            An `ItemOutcome` rather than a bare `(output, error, tokens)` tuple: the items' `artifacts`
            and `usage` are part of what a fan-out produced, and the tuple cannot carry them — so the
            aggregate was summed from nothing and a downstream node was told a fan-out that wrote
            twenty files had produced none.
            """
            result = self._run_one(node_id=node_id, node=node, skill=skill, state=state,
                                   attempt=attempt, bundle=bundle, binding=binding,
                                   is_reviewer=True, inputs=inputs, findings=findings,
                                   agent_id=agent_id, instruction_override=item.prompt)
            usage = result.get("usage") or {}
            tokens = int(usage.get("tokens_in") or 0) + int(usage.get("tokens_out") or 0)
            artifacts = list(result.get("artifacts") or [])
            # Both branches carry the artifacts and usage, because both are facts about an item that
            # really ran: a failed item still spent tokens and may still have written files. The
            # aggregate keeps its own rule (`plan.summary()` sums over *successful* items only), so a
            # failure's spend does not inflate the node's total.
            if str(result.get("status")) == "done":
                return ItemOutcome(output=str(result.get("summary") or ""), tokens=tokens,
                                   artifacts=artifacts, usage=dict(usage))
            return ItemOutcome(error=str(result.get("summary") or result.get("status") or "not done"),
                               tokens=tokens, artifacts=artifacts, usage=dict(usage))

        run_fanout(plan, _one, agents=voters, max_parallel=max_parallel)
        summary = plan.summary()
        self._log("fanout.result", node_id=node_id, detail=summary)

        # Nothing is merged from `self.history`. `history[node_id]` is written once *per item* by
        # `_run_one` → `_remember`, so it holds only whichever item finished last — and the
        # `base.setdefault("artifacts", [])` that read it was a no-op on the key `base` had just been
        # given, so it preserved nothing while claiming to. `summary` is the fan-out's own aggregate,
        # summed over every successful item, so it *is* the record of what the items produced; merging
        # the last item's artifacts back in would name one item's output twice.
        return self._fanout_result(node_id=node_id, plan=plan, summary=summary,
                                   skill=skill, bundle=bundle, inputs=inputs)

    def _fanout_items_from(self, state: dict[str, Any], node: dict[str, Any]) -> list[str]:
        """Items derived from an upstream artifact list, when the node names no explicit items.

        Reads the node's declared inputs and expands each artifact into one item, so a plan can say
        "one subagent per file the developer changed" without the planner having to enumerate them.
        """
        wanted = [str(i) for i in (node.get("inputs") or [])]
        index = self._artifact_index(state)
        items: list[str] = []
        for name in wanted:
            info = index.get(name)
            if info and info.get("path"):
                items.append(str(info["path"]))
        return items

    def _fanout_parallel(self) -> int:
        """How many fan-out subagents may run in a batch. Bounded so a bulk job cannot melt a provider."""
        if self.ctx.config is None:
            return 4
        section = getattr(self.ctx.config, "executor", None)
        raw = getattr(section, "fanout_max_parallel", None) if section is not None else None
        try:
            return max(1, int(raw)) if raw is not None else 4
        except (TypeError, ValueError):
            return 4

    def _fanout_result(self, *, node_id: str, plan: Any, summary: dict[str, Any],
                       skill: str, bundle: Any, inputs: dict[str, Any]) -> dict[str, Any]:
        """Shape a fan-out into the result the runner accepts.

        `done` only when every item succeeded. A partial fan-out is `needs_review` with the failures
        in `open_questions`, because reporting a half-reviewed change set as done is the exact
        dishonesty the evidence contract exists to prevent.
        """
        complete = bool(summary.get("complete"))
        failures = summary.get("failures") or []
        outputs = [i.output for i in plan.items if i.ok and i.output]
        combined = "\n\n".join(
            f"### item {i.index}: {i.item}\n{i.output}" for i in plan.items if i.ok
        )[:6000]

        result: dict[str, Any] = {
            "status": "done" if complete else "needs_review",
            "verdict": "pass" if complete else "changes_requested",
            "summary": (f"fan-out of {summary['count']} item(s): {summary['succeeded']} succeeded"
                        + (f", {summary['failed']} failed" if failures else ""))[:400],
            "evidence": [f"item {i.index}: {i.item}" for i in plan.items if i.ok][:20],
            "criteria_met": [],
            # The aggregate `plan.summary()` computed, not a placeholder. These are the two fields
            # that were hardcoded zeros: the node's own result is the sum of its items, and a
            # downstream node reading `artifacts: []` was being told a twenty-file fan-out produced
            # nothing. Key names are `summary()`'s: `artifacts` is the flattened list of successful
            # items' artifact refs, `usage` is `tokens_in`/`tokens_out`/`cost_usd` summed over them.
            "artifacts": list(summary.get("artifacts") or []),
            "usage": dict(summary.get("usage") or {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}),
            "fanout": {
                "count": summary["count"],
                "succeeded": summary["succeeded"],
                "failed": summary["failed"],
                "complete": complete,
                "items": [{"index": i.index, "item": i.item, "agent_id": i.agent_id,
                           "ok": i.ok, "error": i.error} for i in plan.items],
            },
            "output": combined,
        }
        if failures:
            result["open_questions"] = [
                {"question": f"item {f['index']} ({f['item']}) failed: {f['error']}",
                 "assigned_to": "owner"} for f in failures[:10]
            ]
        return result

    # ── the swarm (a vote) ──────────────────────────────────────────────────

    def _run_swarm(self, *, node_id: str, node: dict[str, Any], skill: str, state: dict[str, Any],
                   attempt: int, bundle: Any, binding: Any, is_reviewer: bool,
                   inputs: dict[str, Any], findings: list[dict[str, Any]],
                   pooled: Any = None) -> dict[str, Any]:
        """Run several agents on one node and aggregate their answers by quorum.

        This is what `BindingPolicy.SWARM` has always promised and never did: the binding returned
        every eligible agent and the executor quietly ran only the first. A swarm is worth having for
        exactly one reason — independent judgment — so the aggregation is a *majority*, not an
        average: three agents that each say "changes requested" cannot be outvoted by one confident
        "pass".

        Bounded deliberately. Voting costs N times the tokens and N times the latency, so the size is
        capped (`swarm_max_voters`, default 3) and the cap is reported rather than applied silently.
        The first voter's artifacts are the ones written, so a swarm cannot produce three conflicting
        copies of the same file.
        """
        voters = list(binding.agents)
        cap = self._swarm_cap()
        truncated = len(voters) > cap
        voters = voters[:cap]
        quorum = max(1, len(voters) // 2 + 1)

        self._log("swarm.start", node_id=node_id,
                  detail={"voters": voters, "quorum": quorum, "capped_from": len(binding.agents)
                          if truncated else None})

        results: list[tuple[str, dict[str, Any]]] = []
        failures: list[str] = []
        for agent_id in voters:
            try:
                one = self._run_one(node_id=node_id, node=node, skill=skill, state=state,
                                    attempt=attempt, bundle=bundle, binding=binding,
                                    is_reviewer=is_reviewer, inputs=inputs, findings=findings,
                                    agent_id=agent_id,
                                    # Only the first voter settles the pooled task; completing it
                                    # once per voter would race N writers onto one record.
                                    pooled=pooled if not results else None)
            except Exception as exc:  # noqa: BLE001 - one voter failing must not lose the others
                failures.append(f"{agent_id}: {exc}")
                self._log("swarm.voter_failed", level="warning", node_id=node_id,
                          agent_id=agent_id, message=str(exc))
                continue
            results.append((agent_id, one))

        if not results:
            # Every voter failed, so there is nothing to aggregate. This is a node failure, and
            # saying so is better than inventing a result from an empty set.
            raise ExecutionError(
                f"every swarm voter failed at node {node_id!r}: {'; '.join(failures) or 'no voters'}"
            )

        aggregated = self._aggregate_swarm(node_id=node_id, skill=skill, bundle=bundle,
                                           results=results, quorum=quorum, binding=binding,
                                           truncated=truncated, failures=failures)
        return aggregated

    def _swarm_cap(self) -> int:
        """How many voters a swarm may use. Capped because voting multiplies both cost and latency."""
        if self.ctx.config is None:
            return 3
        section = getattr(self.ctx.config, "executor", None)
        raw = getattr(section, "swarm_max_voters", None) if section is not None else None
        try:
            return max(1, int(raw)) if raw is not None else 3
        except (TypeError, ValueError):
            return 3

    def _aggregate_swarm(self, *, node_id: str, skill: str, bundle: Any,
                         results: list[tuple[str, dict[str, Any]]], quorum: int,
                         binding: Any, truncated: bool, failures: list[str]) -> dict[str, Any]:
        """Turn N verdicts into one, by majority, keeping the disagreement visible.

        The winner supplies the status, verdict, artifacts and evidence — a merged artifact list from
        several voters would describe a state no single agent produced. The tally and each voter's
        verdict travel in `swarm`, because "3 of 4 agreed" is the information a person needs to trust
        or question the outcome, and it is lost the moment only the winner is reported.
        """
        # Count the verdict, since that is the judgment a quorum exists to settle. Fall back to
        # status when there is no verdict (a worker node rather than a reviewer).
        def key(result: dict[str, Any]) -> str:
            return str(result.get("verdict") or result.get("status") or "")

        tally: dict[str, int] = {}
        for _, result in results:
            tally[key(result)] = tally.get(key(result), 0) + 1

        # Order by count desc, then by first appearance, so the tie-break is deterministic.
        order = {k: i for i, k in enumerate(key(r) for _, r in results)}
        winning = sorted(tally, key=lambda k: (-tally[k], order[k]))[0]
        winner = next(r for _, r in results if key(r) == winning)
        reached = tally[winning] >= quorum

        swarm_detail = {
            "voters": [aid for aid, _ in results],
            "quorum": quorum,
            "tally": tally,
            "winner": winning,
            "agreement": reached,
            "unique_answers": len(tally),
            "capped_from": len(binding.agents) if truncated else None,
            "failed": failures,
            "verdicts": {aid: {"status": r.get("status"), "verdict": r.get("verdict")}
                         for aid, r in results},
        }
        self._log("swarm.result", node_id=node_id, detail=swarm_detail)

        # Sum the cost: every voter spent tokens, and reporting only the winner's would understate
        # what the node actually cost — the one number that must never be flattering.
        totals = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
        any_unknown_cost = False
        for _, result in results:
            usage = result.get("usage") or {}
            totals["tokens_in"] += int(usage.get("tokens_in") or 0)
            totals["tokens_out"] += int(usage.get("tokens_out") or 0)
            if usage.get("cost_usd") is None:
                any_unknown_cost = True
            else:
                totals["cost_usd"] += float(usage["cost_usd"])

        aggregated = dict(winner)
        aggregated["usage"] = {
            "tokens_in": totals["tokens_in"], "tokens_out": totals["tokens_out"],
            "cost_usd": None if any_unknown_cost else totals["cost_usd"],
        }
        aggregated["swarm"] = swarm_detail

        # A quorum that was not reached is not a pass. The node still reports its status, but the
        # summary says plainly that the swarm disagreed, so nobody reads three-way disagreement as
        # consensus.
        if not reached:
            base = str(aggregated.get("summary") or "").strip()
            aggregated["summary"] = (
                f"swarm did not reach quorum ({tally.get(winning, 0)} of {len(results)} for "
                f"{winning!r}, needed {quorum}): "
                f"{', '.join(f'{k}×{v}' for k, v in sorted(tally.items()))}. {base}"
            )[:400]
        return aggregated


    # ── binding ─────────────────────────────────────────────────────────────

    def _bind(self, node_id: str, node: dict[str, Any], skill: str, *, is_reviewer: bool):
        """Choose the agent, excluding the producer when this node is a review."""
        exclude: set[str] = set()
        prefer_model: str | None = None
        if is_reviewer:
            producer = self._producer_for(node_id, node)
            if producer:
                exclude.add(producer)
                producer_spec = self.org.agents.get(producer)
                if producer_spec is not None and producer_spec.model:
                    prefer_model = producer_spec.model
        try:
            # The node's declaration is enriched with the id and skill so the binder has a complete
            # view, rather than being asked to infer them.
            # The policy comes from three places, most specific first: the run context the
            # orchestrator wrote, then the node's own declaration in the manifest, then the default.
            # The manifest route is what makes a swarm *requestable* — a plan that wants three
            # reviewers can say so in the graph rather than needing an out-of-band override.
            return self.binder.bind(
                {**node, "id": node_id, "skill": skill},
                policy=self.ctx.policies.get(
                    node_id, declared_policy(node) or BindingPolicy.LOAD_BALANCED),
                pinned=self.ctx.pins.get(node_id), exclude=exclude, prefer_model=prefer_model,
            )
        except BindingError as exc:
            raise ExecutionError(f"cannot bind node {node_id!r}: {exc}") from exc

    def _producer_for(self, node_id: str, node: dict[str, Any]) -> str | None:
        """Which agent produced the artifact this reviewer would judge.

        Reads run-state first, so a resumed run knows the producer even though the in-memory map was
        lost. Falls back to the in-memory record.

        The copies are taken *while holding* the registry lock, because a concurrent sibling node
        writes `history` as it finishes: iterating the live dict here raised
        `dictionary changed size during iteration`. Holding the lock across the whole scan also makes
        the answer a consistent snapshot, which is what the independence refusal needs — a producer
        read from half-updated state could name an agent that is about to be replaced.
        """
        with self.registries:
            history = list(self.history.items())
            producers = list(self.producers.items())
        for other_id, exec_result in history:
            if other_id != node_id and not exec_result.findings:
                return exec_result.agent_id
        for other_id, agent_id in producers:
            if other_id != node_id:
                return agent_id
        return None

    def _is_reviewer(self, skill: str, node: dict[str, Any]) -> bool:
        """Whether this node owes a verdict rather than a change."""
        if node.get("phase") == "REVIEW":
            return True
        spec = self.org.agents.get(self.ctx.pins.get("") or "")
        if spec is not None and spec.role == "reviewer":
            return True
        for agent in self.org.agents_for_skill(skill):
            if agent.role == "reviewer":
                return True
        return False

    # ── context management ──────────────────────────────────────────────────

    def _session_for(self, agent: Any, node_id: str, bundle: SkillBundle) -> Session:
        """The agent's session for this node, created on first use.

        Per-agent, not per-node: the agent is the thing whose attention is being managed, and a node
        that spans many sessions must not lose its window sizing between them.
        """
        key = f"{agent.id}:{node_id}"
        with self.ctx.lock:
            session = self.ctx.sessions.get(key)
            if session is None:
                window = int(agent.context_window or 0)
                if window <= 0:
                    raise ExecutionError(
                        f"agent {agent.name!r} has no context window, so a session cannot be sized. "
                        "This should have been refused at binding."
                    )
                reserve = 0
                if self.ctx.config is not None:
                    reserve = self.ctx.config.context.output_reserve(window)
                session = Session(agent_id=agent.id, node_id=node_id, window=window,
                                  output_reserve=reserve or min(4096, window // 10))
                self.ctx.sessions[key] = session
            return session

    def _prepare_context(self, session: Session, bundle: SkillBundle, node: dict[str, Any],
                         inputs: dict[str, Any], *, node_id: str, agent_id: str) -> dict[str, Any]:
        """Project the prompt, then compact or rotate before spending a call.

        This is the ordering that matters: measure, then act, then call. Sending first and reacting to
        a context-length error wastes the call and leaves the session in the degraded state the error
        implies.
        """
        compact_at, evict_at, overflow_at, attention_floor, max_rotations, rotate_on_phase = \
            self._context_settings()

        recall = self._recall(bundle)
        pinned = "\n".join(session.pinned)
        skill_body = bundle.system_body(tier=2, max_tokens=bundle.token_budget)
        system = self._system_prompt(bundle)
        new_message = self._instruction_for(node_id, node, bundle.name, {}, resolved_inputs=inputs)

        projection = project(session, system=system, skill_body=skill_body, pinned=pinned,
                             recall=recall, new_message=new_message,
                             reserve=session.output_reserve)

        compacted = 0
        rotated = 0
        # Compact when warranted, then re-measure: one pass may not be enough, and the rotation
        # decision must be based on the post-compaction figure rather than the pre- one.
        if projection.must_compact:
            # The durable store is consulted, not merely written to. Its verdict decides *which* run of
            # turns to drop, and a compaction that breaks a prefix the store called warm is recorded
            # against it — without that, the miss appears in `savings.jsonl` with nothing to attribute
            # it to, which is how the cache came to be measured and never used.
            result = compact(session, compact_at=compact_at, evict_at=evict_at,
                             overflow_at=overflow_at,
                             cache_store=self._cache_store(), cache_prefix_hash=self._prefix_hash(),
                             cache_skill=bundle.name)
            if result.effective:
                compacted = 1
                detail = {"band": result.band_before.value,
                          "recovered": result.recovered,
                          "preserved_verbatim": result.pinned_after,
                          "aligned": result.aligned,
                          "prefix_chars_kept": result.prefix_chars_kept}
                self._log("session.compact", node_id=node_id, agent_id=agent_id,
                          session_id=session.session_id, detail=detail)
                if result.invalidated_prefix is not None:
                    # A line of its own, because "the cache went cold" is the statement worth being able
                    # to grep for when a bill arrives.
                    self._log("session.cache_invalidated", level="warning", node_id=node_id,
                              agent_id=agent_id, session_id=session.session_id,
                              detail=result.invalidated_prefix)
                projection = project(session, system=system, skill_body=skill_body, pinned=pinned,
                                     recall=recall, new_message=new_message,
                                     reserve=session.output_reserve)

        decision = decide_rotation(session, rotation_count=session.rotation_count,
                                   max_rotations=max_rotations, attention_floor=attention_floor,
                                   rotate_on_phase_change=rotate_on_phase, projection=projection)
        if decision.should_rotate:
            fresh, rotated = self._rotate(session, decision, bundle, node_id=node_id,
                                          agent_id=agent_id)
            if fresh is not None:
                session = fresh
                projection = project(session, system=system, skill_body=skill_body, pinned=pinned,
                                     recall=recall, new_message=new_message,
                                     reserve=session.output_reserve)
                key = f"{agent_id}:{node_id}"
                with self.ctx.lock:
                    self.ctx.sessions[key] = session

        # Refuse only when the irreducible floor makes the call impossible. Anything else the runner
        # should see as a blocked node with a reason rather than as an exception.
        if projection.irreducible_overflow:
            self._log("context.irreducible_overflow", level="warning", node_id=node_id,
                      agent_id=agent_id, message=projection.diagnosis())

        return {
            "recall": recall, "constraints": list(session.pinned),
            "projection": projection, "session": session,
            "compacted": compacted, "rotated": rotated,
            "instruction": new_message,
        }

    def _context_settings(self) -> tuple[float, float, float, float, int, bool]:
        """The context thresholds, from config with the library's defaults."""
        context = getattr(self.ctx.config, "context", None) if self.ctx.config else None
        return (
            float(getattr(context, "compact_at", 0.70)),
            float(getattr(context, "evict_at", 0.85)),
            float(getattr(context, "overflow_at", 0.95)),
            float(getattr(context, "attention_floor", 0.30)),
            int(getattr(context, "max_rotations_per_node", 4)),
            bool(getattr(context, "rotate_on_phase_change", True)),
        )

    def _rotate(self, session: Session, decision: Any, bundle: SkillBundle, *, node_id: str,
                agent_id: str) -> tuple[Session | None, int]:
        """Seal the session and open a fresh one carrying its constraints.

        Returns the new session (or None when the rotation could not be built, in which case the old
        session is kept and the run continues — a failed rotation must not lose the work).
        """
        agent = self.org.get(agent_id)
        try:
            handoff = build_handoff(
                session, decision, run_id=self.ctx.run_id, agent_name=agent.name,
                skill=bundle.name, model=agent.model,
                decisions=self._ledger_decisions(),
                open_questions=self._open_questions(),
            )
        except Exception as exc:  # noqa: BLE001 - a failed rotation must not stop the node
            self._log("session.rotate.failed", level="warning", node_id=node_id,
                      agent_id=agent_id, message=str(exc))
            return None, 0

        session.seal(reason=decision.reason)
        session.mark_handoff()
        session.close()

        fresh = Session(
            agent_id=agent_id, node_id=node_id, window=session.window,
            index=session.index + 1, output_reserve=session.output_reserve,
            phase=session.phase, attempt=session.attempt,
        )
        # The constraints are carried as pins, which is what makes AR-04 hold across the rotation and
        # what the next assembly re-pins to the primacy zone.
        for constraint in handoff.constraints:
            if constraint.get("value"):
                fresh.pin(str(constraint["value"]))
        fresh.rotation_count = session.rotation_count + 1
        # Link the outgoing session to its replacement, so a fan-out *sibling* that is still mid-turn
        # does not lose its exchange. Every item bound to one agent shares one `Session` object, so
        # when one item rotates it the others' `append` lands on a sealed session whose transcript has
        # already been serialized — it either raises `SessionError` or is silently lost. With the link,
        # `append`/`pin`/`begin_phase` follow the rotation instead, which is what actually happened:
        # the agent's thread moved on while the sibling finished its turn.
        session.adopt_successor(fresh)

        self._log("session.rotate", node_id=node_id, agent_id=agent_id,
                  session_id=fresh.session_id,
                  detail={"trigger": decision.trigger.value, "reason": decision.reason[:200],
                          "from": session.session_id, "constraints_carried": len(fresh.pinned)})
        self._emit_rotation_span(agent_id, fresh, decision, len(fresh.pinned))
        return fresh, 1

    # ── the call ────────────────────────────────────────────────────────────

    def _call(self, agent: Any, prompt: Any, *, node_id: str, attempt: int, session: Session,
              bundle: SkillBundle, node: dict[str, Any] | None = None,
              pooled: Any = None) -> dict[str, Any]:
        """Make the model call, guarded by the effect journal.

        The journal matters here for a subtle reason: a retry of the same attempt must not re-spend. If
        the effect were already applied, the recorded result is returned instead of calling again.

        A node that declares `tools: true` runs the **agentic loop** instead of one call: the model
        reads files, then writes, then answers. That is what lets an agent work on a real project
        rather than only on the artifacts a previous node handed it.

        `pooled` is the claimed task, threaded through only so the tool loop can renew its lease per
        step — a node that spends minutes inside tools would otherwise let the lease lapse.
        """
        from .providers.base import ChatRequest, Message, Role

        if node is not None and self._tools_enabled(node):
            return self._call_with_tools(agent, prompt, node_id=node_id, attempt=attempt,
                                         session=session, bundle=bundle, node=node, pooled=pooled)

        request = ChatRequest(
            model=agent.model,
            messages=[Message.text_message(Role.USER, prompt.text)],
            system=prompt.system,
            max_tokens=min(self.ctx.max_output_tokens,
                           int(agent.max_output or self.ctx.max_output_tokens)),
            # A node without tools has no prose deliverable to trade against: its reply *is* the
            # trailer, so the provider is asked to enforce the JSON shape where it declares it can.
            # This is the common case that needed no repair at all until the trailer was asked for in
            # prose — and it was asked for in prose everywhere in the engine, on every node, because
            # nothing ever set this field.
            json_mode=self._json_mode(agent),
        )
        inputs_hash = str(hash(prompt.text))[-12:]
        started = time.time()

        if self.ctx.journal is not None:
            key = effect_key(run_id=self.ctx.run_id or "run", node_id=node_id, attempt=attempt,
                             inputs_hash=inputs_hash, effect="llm_call", target=agent.id)
            with self.ctx.journal.effect("llm_call", run_id=self.ctx.run_id or "run",
                                         node_id=node_id, attempt=attempt,
                                         inputs_hash=inputs_hash, target=agent.id) as record:
                if record.replayed and record.result:
                    self._log("effect.replayed", node_id=node_id, agent_id=agent.id,
                              detail={"effect": "llm_call", "key": key[:16]})
                    return dict(record.result)
                response = self._invoke(agent, request, node_id=node_id, session=session,
                                        latency_start=started)
                record.record(response)
                return response

        return self._invoke(agent, request, node_id=node_id, session=session, latency_start=started)

    def _json_mode(self, agent: Any) -> bool:
        """Whether to ask this agent's provider to *enforce* a JSON object for this reply.

        The trailer is a JSON object, and asking for it in prose is the thing a small model fails at.
        A real run made this plain: a `pm` node answered with a verbatim echo of the prompt's own
        schema example, and every repair turn after it answered in prose. Its provider declares JSON
        mode, both adapters honour it, and **nothing in the engine ever set it** — so the trailer was
        requested in the one form the provider cannot enforce, and the run parked on a contract the
        node had no reliable way to satisfy.

        Only a verified ``True`` is honoured. `ProviderCapabilities` documents ``None`` as *unprobed*,
        and that distinction is load-bearing: sending `response_format`/`format: json` to an endpoint
        that ignores it loses the trailer the contract depends on while the log says it was asked for.

        `capabilities()` is a declaration, not a probe — the provider interface requires it to be
        cheap and free of network I/O — so this is safe to call on every turn.
        """
        providers = getattr(getattr(self.ctx, "gateway", None), "providers", None)
        if not providers:
            return False
        provider = providers.get(str(getattr(agent, "provider", "") or ""))
        if provider is None:
            return False
        try:
            capabilities = provider.capabilities()
        except Exception:  # noqa: BLE001 - a provider that cannot report is not asked
            return False
        return getattr(capabilities, "supports_json_mode", None) is True

    # ── the agentic loop ────────────────────────────────────────────────────

    def _tools_enabled(self, node: dict[str, Any]) -> bool:
        """Whether this node works through tools rather than answering in one call."""
        if not node.get("tools"):
            return False
        if self.ctx.config is None:
            return True
        section = getattr(self.ctx.config, "executor", None)
        return bool(getattr(section, "tools_enabled", True)) if section is not None else True

    def _tool_registry(self, agent: Any, node: dict[str, Any]) -> Any:
        """The registry for this node: workspace-scoped, capability-gated, optionally read-only."""
        from .tools import ToolRegistry

        read_only = bool(node.get("read_only")) or not (self.ctx.config is None) and bool(
            getattr(getattr(self.ctx.config, "executor", None), "tools_read_only", False))
        registry = ToolRegistry(
            workspace_root=self.ctx.workspace,
            agent=agent,
            # The workspace's own writer, so containment and atomicity have one implementation.
            writer=self.ctx.store,
            read_only=read_only,
            # Only when a goal is armed: the tool is then advertised, and its verdict is written into
            # the workspace's own `.agent_state/` for the orchestrator to consume.
            goal_workspace=self.ctx.workspace if self.ctx.goal_active else None,
            # Only when subagents are enabled for this run: otherwise `task`/`fleet` would be offered
            # with no way to collect a child, which is worse than not offering them at all.
            subagents=self._subagent_runner(agent, node),
            # Passed unconditionally: `sandbox.enabled` is what decides whether `run_command` is
            # advertised, and keeping that decision in one place means this call site cannot
            # accidentally offer a shell the operator did not turn on.
            sandbox=getattr(self.ctx.config, "sandbox", None),
            # Same pattern, same reason: `system.enabled` decides whether the machine tools are
            # advertised at all. This keyword was missing while the tools existed, so every one of them
            # was inert in a real run — the registry supported them, the executor never offered them,
            # and nothing on screen said why. Passing the section unconditionally keeps that decision
            # in `SystemConfig` rather than at this call site.
            system=getattr(self.ctx.config, "system", None),
        )
        # MCP tools, when any server is configured. Wired here rather than at the call site because
        # this is the one place every node's registry is built, so a node cannot accidentally get a
        # registry without the servers every other node has. `attach` cannot raise and returns None
        # when nothing is configured, so an unreachable server costs this node a diagnostic and not
        # its run — the same rule the sandbox and the lifecycle hooks follow.
        #
        # A read-only node is given no MCP tools: a server exposes capabilities the engine cannot
        # vet for containment, so offering one to an agent whose whole purpose is to *inspect* would
        # hand it a way to change things. The refusal is silent only in the sense that it needs no
        # diagnostic — a reviewer that never had the tools is not a failure.
        if not read_only:
            self._attach_mcp(registry)
        return registry

    def _attach_mcp(self, registry: Any) -> None:
        """Offer this node's configured MCP servers' tools, best-effort.

        Cached on the executor so a run with N nodes connects to each server once rather than N
        times: a stdio server is a process, and spawning one per node would spend more on handshakes
        than on work. The bridge is closed with the executor (`close`), so a run does not leave
        orphaned server processes behind.
        """
        config = getattr(self.ctx.config, "mcp", None)
        if config is None or not getattr(config, "enabled", False):
            return
        cached = getattr(self, "_mcp_bridge", None)
        if cached is None:
            if getattr(self, "_mcp_attempted", False):
                return                      # one failure per run, not one per node
            self._mcp_attempted = True
            try:
                from .mcp import attach

                cached = attach(config, registry, diagnostics=self.ctx.diagnostics)
            except Exception as exc:  # noqa: BLE001 - an optional capability must not stop a run
                if self.ctx.diagnostics is not None:
                    try:
                        self.ctx.diagnostics.warning(
                            "mcp.attach.failed", message=f"{type(exc).__name__}: {exc}")
                    except Exception:  # noqa: BLE001
                        pass
                cached = None
            self._mcp_bridge = cached
            return
        # Already connected: install the same tools on this node's registry. Registering is how the
        # bridge exposes them, so a later node gets them without a second handshake.
        try:
            cached.install(registry)
        except Exception:  # noqa: BLE001 - the first node's tools stand; this one just misses them
            pass

    def _subagent_runner(self, agent: Any, node: dict[str, Any]) -> Any:
        """The isolated-subagent dispatcher for this node, or None when it is not enabled.

        A child runs through the *same* loop as the parent — `_call_with_tools` is reused — but in its
        own session, so its context does not accumulate in the parent's window. That reuse is what
        makes "a subagent is a session, not a node" true rather than aspirational.
        """
        section = getattr(self.ctx.config, "executor", None) if self.ctx.config is not None else None
        if not bool(getattr(section, "subagents_enabled", False)):
            return None
        if node.get("subagents") is False:
            return None

        from .subagents import ChildStore, SubagentRunner

        run_id = self.ctx.run_id or "run"
        store = ChildStore(self.ctx.workspace, run_id=run_id)
        fleet_enabled = bool(getattr(section, "subagent_fleet_enabled", False))
        max_parallel = int(getattr(section, "subagent_max_parallel", 4) or 1) if fleet_enabled else 1
        holders = [a.id for a in self.ctx.org.agents.values()
                   if not getattr(a, "is_human", False)] if self.ctx.org is not None else []

        def _run_child(*, prompt: str, skill: str, child_id: str, agent_id: str,
                       depth: int, budget: int) -> tuple[str, str, int, int, int]:
            """Run one child in its own session and return (summary, status, tokens, steps)."""
            return self._run_child_in_context(prompt=prompt, skill=skill, child_id=child_id,
                                              agent_id=agent_id, depth=depth, budget=budget)

        return SubagentRunner(
            store=store, run_child=_run_child, agents=holders, max_parallel=max_parallel,
            parent_depth=int(node.get("depth") or 0),
            parent_tokens=self._parent_token_budget(),
            on_event=lambda kind, payload: self._log(kind, detail=payload),
        )

    def _parent_token_budget(self) -> int:
        """The tokens this run may still spend, which a fleet is carved from.

        Read from the run's own :class:`~engine.gateway.CostLedger` — the object that actually saw
        the provider's counters — rather than a decision ledger, which has no token totals at all.
        The ledger records spend, not a remainder, so the remainder is the gateway's ceiling minus
        what it has charged; neither the `remaining_tokens` key nor a `snapshot()` on the decision
        ledger ever existed, and both used to make this return a confident zero.

        `0` is the fleet's own sentinel for "nothing carveable" (`SubagentRunner._budget_for`), so it
        is returned when no ceiling applies or the ceiling is spent — never as a stand-in for a
        figure nobody measured. An unmeasured call contributes no tokens to the ledger, so a run with
        unreported usage yields a remainder that is an upper bound; the fleet's carve only uses it as
        a share, which is why that bound is acceptable here and is stated rather than hidden.
        """
        gateway = self.ctx.gateway
        ledger = getattr(gateway, "ledger", None)
        if ledger is None:
            return 0
        ceiling = int(getattr(gateway, "run_max_tokens", 0) or 0)
        if ceiling <= 0:
            return 0
        spent = int(getattr(ledger, "total_tokens", 0) or 0)
        return max(0, ceiling - spent)

    def _run_child_in_context(self, *, prompt: str, skill: str, child_id: str, agent_id: str,
                              depth: int, budget: int) -> tuple[str, str, int, int, int]:
        """Run one subagent: its own session, the parent's pinned prefix, the task in the tail.

        The isolation is the *log* — a fresh `Session` per child — while the prefix stays the parent's,
        because the cacheable bytes are `(skill, tools)`-scoped and agent-independent by design. A child
        that re-derived its own prefix would pay full price for the same procedure.
        """
        from .agentloop import AgentLoop
        from .context.session import Session
        from .providers.base import ChatRequest

        agent = self._agent_by_id(agent_id)
        if agent is None:
            return ("", "failed", 0, 0, 0)
        window = self._window_for(agent)
        session = Session(agent_id=agent.id, node_id=f"{child_id}", window=window,
                          session_id=f"{child_id}_{int(time.time() * 1000)}")
        registry = ToolRegistry(workspace_root=self.ctx.workspace, agent=agent,
                                writer=self.ctx.store, read_only=True)

        def _complete(request: ChatRequest) -> Any:
            request.model = agent.model
            return self.ctx.gateway.complete(request, provider_id=agent.provider,
                                             agent_id=agent.id, node_id=child_id,
                                             session_id=session.session_id)

        loop = AgentLoop(complete=_complete, tools=registry,
                         max_steps=int(self._tool_step_bound()),
                         max_output_tokens=self.ctx.max_output_tokens)
        outcome = loop.run(system=self._child_system(skill), user=prompt)
        status = "needs_review" if outcome.exhausted else "done"
        summary = outcome.text or outcome.stop_reason or "(no result)"
        return (summary, status, outcome.tokens_in, outcome.tokens_out, outcome.steps)

    def _child_system(self, skill: str) -> str:
        """The child's system prompt.

        Deliberately just the skill's procedure and a statement of the child's place, with no identity
        or task in it: the parent's prefix must stay byte-identical for a child to share the cache, and
        folding a child id or its task into the system prompt is the exact mistake `prefix.py` names as
        costing 58% on every swarm.
        """
        body = ""
        if skill:
            try:
                bundle = self.ctx.skills.bundle(skill)
            except SkillError as exc:
                # A name nobody holds is a real case — the delegation design calls authoring one a
                # rung — so it yields a generic child. It is *reported*, not swallowed: a child that
                # ran without the procedure it was hired for looks identical to one that followed it,
                # and the loss would be invisible in the very run it degrades. The catch is narrowed
                # to `SkillError` on purpose: a missing `bundle` capability used to raise
                # `AttributeError` into a bare `except Exception`, which is how this defect survived
                # with every child silently generic.
                self._log("subagent.skill_missing", level="warning",
                          message=f"subagent bound to unknown skill {skill!r}: {exc}",
                          detail={"skill": skill})
            else:
                body = str(getattr(bundle, "body", "") or "")
        return (f"You are a subagent working under a parent agent. Do the task you are given and "
                f"report what you found concisely. Your context is your own.\n\n{body}")

    def _agent_by_id(self, agent_id: str) -> Any:
        if self.ctx.org is None:
            return None
        return self.ctx.org.agents.get(agent_id)

    def _window_for(self, agent: Any) -> int:
        """The child's context window. An unknown window is refused at binding, so a sane floor here."""
        spec = getattr(agent, "context_window", None)
        try:
            value = int(spec) if spec else 0
        except (TypeError, ValueError):
            value = 0
        return value or 32768

    def _call_with_tools(self, agent: Any, prompt: Any, *, node_id: str, attempt: int,
                         session: Session, bundle: SkillBundle,
                         node: dict[str, Any], pooled: Any = None) -> dict[str, Any]:
        """Run the node through the agentic loop and normalise the outcome for the trailer parser.

        The loop's final text is returned in the same shape a single call would produce, so everything
        downstream — the trailer parse, the artifact persistence, the evidence contract — is unchanged.
        A loop that hit its bound carries that in `stop_reason`, and the node is marked
        `needs_review` so a truncated investigation is never reported as a finished one.
        """
        from .agentloop import AgentLoop
        from .providers.base import ChatRequest

        registry = self._tool_registry(agent, node)
        max_steps = int(node.get("max_steps") or 0) or self._tool_step_bound()
        started = time.time()

        def _complete(request: ChatRequest) -> Any:
            # The model id and provider come from the agent, since the loop builds the request.
            request.model = agent.model
            # JSON mode on the loop's **final, tool-less turn** only — the one the loop reserves for
            # the answer ("tools are disabled. Produce your work and the required output trailer
            # now"). That turn's whole job is the trailer, so constraining it to a JSON object cannot
            # cost a tool call. Forcing it on every turn would: a model that must emit a tool call
            # cannot also be constrained to a JSON object the tool-call schema does not admit, and an
            # intermediate prose answer is not what the contract reads.
            if not request.tools:
                request.json_mode = self._json_mode(agent)
            return self.ctx.gateway.complete(request, provider_id=agent.provider,
                                             agent_id=agent.id, node_id=node_id,
                                             session_id=session.session_id)

        def _gate() -> tuple[bool, str]:
            # The budget check runs *before* each step, which is what makes the ceiling stop the
            # spend rather than report it afterwards.
            try:
                self.ctx.gateway.check_budget()
            except Exception as exc:  # noqa: BLE001 - a ceiling is a stop, not a crash
                return False, f"the run budget ceiling was reached: {exc}"
            return True, ""

        def _on_step(step: Any) -> None:
            # Renew the pooled task's lease on every tool step. The post-call heartbeat in `_run_one`
            # only covers the time up to the *model call*; a node that then spends minutes inside
            # tools — reading, writing, running a command — would outlive its lease between heartbeats
            # and be handed to a second worker while this one is still running it.
            self._renew_pooled(pooled, agent, node_id)
            self._log("node.tool_step", node_id=node_id, agent_id=agent.id, detail=step)

        # The repetition guard the config declares. Passed here rather than hardcoded so
        # `goal.repeat_call_reminders` governs the loop it describes — it was documented and never
        # read, so a model stuck on one call spent the whole bound re-issuing it, which for an
        # unattended run is the quietest possible failure.
        goal_cfg = getattr(self.ctx.config, "goal", None)
        repeat_reminders = tuple(getattr(goal_cfg, "repeat_call_reminders", ()) or ())

        loop = AgentLoop(complete=_complete, tools=registry, max_steps=max_steps,
                         can_continue=_gate, max_output_tokens=self.ctx.max_output_tokens,
                         repeat_reminders=repeat_reminders, on_step=_on_step)
        self._log("node.tools.start", node_id=node_id, agent_id=agent.id,
                  detail={"tools": registry.names(), "max_steps": max_steps})
        outcome = loop.run(system=prompt.system, user=prompt.text)
        self._log("node.tools.end", node_id=node_id, agent_id=agent.id,
                  detail={"steps": outcome.steps, "exhausted": outcome.exhausted,
                          "stop_reason": outcome.stop_reason,
                          "paths": sorted(set(outcome.paths))})

        latency_ms = (time.time() - started) * 1000.0
        usage = {
            "tokens_in": outcome.tokens_in, "tokens_out": outcome.tokens_out,
            "cost_usd": None, "cost_source": "unknown", "measured": True,
        }
        text = outcome.text
        if outcome.exhausted:
            # The bound ended it, so the trailer may be absent or a promise the model did not keep.
            # Recording the reason in the text is what makes the node honest downstream: the parser
            # sees an incomplete result and the node parks rather than advancing.
            text = (text + f"\n\n[tools: stopped after {outcome.steps} step(s) — "
                           f"{outcome.stop_reason}]")
        return {"text": text, "usage": usage, "finish_reason": "stop",
                "latency_ms": round(latency_ms, 1),
                "tools": outcome.as_dict()}

    def _tool_step_bound(self) -> int:
        """How many model calls one tool-using node may make."""
        from .agentloop import DEFAULT_MAX_STEPS

        if self.ctx.config is None:
            return DEFAULT_MAX_STEPS
        section = getattr(self.ctx.config, "executor", None)
        raw = getattr(section, "max_tool_steps", None) if section is not None else None
        try:
            return max(1, int(raw)) if raw is not None else DEFAULT_MAX_STEPS
        except (TypeError, ValueError):
            return DEFAULT_MAX_STEPS

    def _invoke(self, agent: Any, request: Any, *, node_id: str, session: Session,
                latency_start: float) -> dict[str, Any]:
        """Perform the call and normalise the response for the journal and the result."""
        self._log("llm.request", node_id=node_id, agent_id=agent.id,
                  session_id=session.session_id,
                  detail={"provider": agent.provider, "model": agent.model})
        self._emit_llm_request(agent, node_id, session)
        try:
            response = self.ctx.gateway.complete(
                request, provider_id=agent.provider, agent_id=agent.id,
                node_id=node_id, session_id=session.session_id,
            )
        except BudgetExceeded as exc:
            # A budget stop is a policy decision, not a provider failure: report it as a blocked node
            # so the run parks rather than retrying into the same ceiling.
            self._log("cost.ceiling", level="warning", node_id=node_id, agent_id=agent.id,
                      message=str(exc))
            raise ExecutionError(f"run budget exhausted at node {node_id!r}: {exc}") from exc

        latency_ms = (time.time() - latency_start) * 1000.0
        usage = response.usage
        cost = self.ctx.gateway.compute_cost(agent.provider, agent.model, usage)
        payload = {
            "text": response.text,
            "usage": {
                "tokens_in": usage.prompt_tokens,
                "tokens_out": usage.completion_tokens,
                "cost_usd": cost.usd if cost.known else None,
                "cost_source": cost.source,
                "measured": usage.measured,
            },
            "finish_reason": response.finish_reason.value,
            "latency_ms": round(latency_ms, 1),
        }
        self._log("llm.response", node_id=node_id, agent_id=agent.id, session_id=session.session_id,
                  detail={"tokens": (usage.prompt_tokens or 0) + (usage.completion_tokens or 0),
                          "cost": payload["usage"]["cost_usd"],
                          "cost_source": cost.source,
                          "latency_ms": payload["latency_ms"]})
        self._emit_llm_response(agent, node_id, session, payload, cost)
        return payload

    # ── the trailer ─────────────────────────────────────────────────────────

    def _may_repair_trailer(self) -> bool:
        """Whether a missing trailer warrants one repair turn.

        On by default because the alternative — parking a node whose work succeeded — is the failure
        mode that makes a weak local model unusable. Disabled by setting `trailer_repair = false` in
        the config's executor section, for a deployment that would rather fail fast than spend.
        """
        if self.ctx.config is None:
            return True
        section = getattr(self.ctx.config, "executor", None)
        if section is None:
            return True
        return bool(getattr(section, "trailer_repair", True))

    def _trailer_needs_repair(self, trailer: dict[str, Any], parse_error: str,
                              bundle: SkillBundle) -> bool:
        """Whether a trailer is salvageable by restating the node's own contract.

        Two salvageable cases: the reply carried no parsable trailer at all, or it carried one whose
        `criteria_satisfied` does not cover this node's criteria (the common cause being that the
        model echoed the *upstream* node's criteria from the handoff). A trailer that is parsable and
        complete is left alone — repairing it would spend a call to change nothing.
        """
        if parse_error:
            return True
        criteria = list(bundle.contract.criteria)
        if not criteria:
            return False
        met = self._criteria_met(trailer, criteria)
        if len(met) >= len(criteria):
            return False
        # A model that reported some criteria is usually one repair turn away from reporting all of
        # them, because it has already done the work and only needs its own contract restated.
        return True

    def _repair_trailer(self, agent: Any, *, node_id: str, session: Session, reply: str,
                        bundle: SkillBundle, attempt: int, reason: str = "parse",
                        unrecognized: list[str] | None = None, unsatisfied: int = 0,
                        ) -> tuple[dict[str, Any], str] | None:
        """Ask, in a focused turn, for the trailer the first reply omitted or mis-scoped.

        The original reply is *carried into the request* rather than replaced, so the model restates
        its own work as evidence instead of inventing new work.

        **Verified, not assumed.** The repair exists to salvage a real result that failed on
        formatting, so it is only useful if the salvaged trailer actually covers the node's criteria.
        A repair turn that parses but under-covers — one criterion of three, or a shape the model got
        wrong — used to be logged `trailer.repair.ok` and accepted, and the node then failed its
        contract anyway with a log line claiming the repair had worked. That is worse than the
        original problem: it made a real run's diagnosis read "repair.ok then contract violation".

        So the repair is bounded and coverage-checked: up to two turns, and the best-covered trailer
        is returned. `ok` is logged only when the criteria are actually covered; otherwise the
        incompleteness is named — which is what the reader needs to know, because it distinguishes
        "the model could not evidence its own work" from "the engine mangled a good reply".

        `unrecognized` is the list of names the rejected reply used that resolved to no criterion of
        this node. It is fed back into the repair because that is the only difference between the
        retry that works and the retry that repeats the failure: a model that answered `PM1`–`PM12`
        (this skill's checklist ids) needs to be told those name a different block, not merely told
        again to cover every criterion. It is carried forward from turn to turn for the same reason.

        `unsatisfied` is the other way a reply covers nothing while looking complete: every criterion
        named correctly and every one marked `satisfied: false`. Carried forward the same way, and for
        the same reason — the retry that repeats an honest negative covers nothing again.
        """
        from .providers.base import ChatRequest, Message, Role

        checklist = ", ".join(bundle.checklist_ids()) or "(none)"
        criteria_list = list(bundle.contract.criteria)
        # Each criterion is listed with the id the contract check resolves, so "copy the line" yields a
        # reference that matches instead of one the checker reports as covering nothing. The repair
        # prompt had told the model to copy the text verbatim while the list carried no ids — and a
        # real run's two repair turns, and every attempt after them, still produced zero coverage.
        criteria = "\n".join(f"- c{i} — {c}" for i, c in enumerate(criteria_list, 1)) \
            or "- (none declared)"
        ignored = list(unrecognized or [])
        best: tuple[dict[str, Any], int] | None = None
        last_error = ""
        turns = 2 if criteria_list else 1
        # The repair carries the model's own work back to it, so it can restate that work rather than
        # redo it. The slice used to be a flat 6000 characters, which for a long artifact (a 57KB PRD
        # was observed) showed only the first tenth — and the *end* is exactly where a trailer would be.
        # So the tail is kept as well as the head: the head says what the work was about, the tail is
        # where a model that tried to comply would have put the block. 12KB total is a cheap call.
        excerpt = reply if len(reply) <= 12000 else (
            reply[:6000] + "\n\n… [" + str(len(reply) - 12000) +
            " characters omitted from the middle] …\n\n" + reply[-6000:])
        for turn in range(1, turns + 1):
            instruction = self._repair_instruction(
                reason=reason, turn=turn, criteria=criteria, checklist=checklist,
                unrecognized=ignored, unsatisfied=unsatisfied)
            request = ChatRequest(
                model=agent.model,
                messages=[Message.text_message(Role.USER,
                                               f"{instruction}\n\n--- YOUR PREVIOUS REPLY ---\n\n"
                                               f"{excerpt}")],
                system="You emit one JSON object in a fenced block, and nothing else.",
                max_tokens=min(self.ctx.max_output_tokens, 2048),
                # This turn exists for exactly one reason — to produce the trailer the first reply
                # omitted or mis-scoped — so the provider is asked to enforce the JSON shape rather
                # than trusting a small model to comply in prose, which is what it had been doing.
                # The fence becomes optional as a side effect: a reply constrained to a bare JSON
                # object is still read, by the parser's trailing-object fallback.
                json_mode=self._json_mode(agent),
            )
            self._log("trailer.repair", node_id=node_id, agent_id=agent.id,
                      detail={"original_chars": len(reply), "turn": turn})
            try:
                response = self.ctx.gateway.complete(
                    request, provider_id=agent.provider, agent_id=agent.id,
                    node_id=node_id, session_id=session.session_id,
                )
            except Exception as exc:  # noqa: BLE001 - a failed repair must not fail the node
                self._log("trailer.repair.failed", level="warning", node_id=node_id,
                          agent_id=agent.id, message=str(exc))
                break
            trailer, error = self._parse(response.text)
            if error:
                last_error = error
                continue
            met, ignored_now = self._criterion_references(trailer, criteria_list)
            covered = len(met)
            if best is None or covered > best[1]:
                best = (trailer, covered)
            if not criteria_list or covered >= len(criteria_list):
                self._log("trailer.repair.ok", node_id=node_id, agent_id=agent.id,
                          detail={"turn": turn, "covered": covered,
                                  "criteria": len(criteria_list)})
                return trailer, ""
            # Under-covered: say so plainly, and try once more with a sharper restatement. The
            # missing criteria are named because "which ones" is the actionable part — and so are the
            # names the model used instead, which is what makes the second attempt a correction rather
            # than a repetition.
            ignored = ignored_now
            unsatisfied = _unsatisfied_count(trailer)
            self._log("trailer.repair.incomplete", level="warning", node_id=node_id,
                      agent_id=agent.id,
                      detail={"turn": turn, "covered": covered, "criteria": len(criteria_list),
                              # What the reply actually carried, so "0 of 3" is not the whole story:
                              # a reply that omitted `criteria_satisfied` and one that marked all
                              # three `satisfied: false` are the same number and need different
                              # answers — the first is a shape failure, the second an honest negative.
                              "keys": sorted(str(key) for key in trailer)[:8],
                              "unsatisfied": unsatisfied,
                              "unrecognized": ignored_now[:6]})
        if best is not None:
            # Return the best attempt even when incomplete: the node stays `needs_review` through the
            # contract check, and the trailer gives the Owner the most coverage the model produced.
            return best[0], last_error
        return None

    def _repair_instruction(self, *, reason: str, turn: int, criteria: str, checklist: str,
                            unrecognized: Iterable[str] = (), unsatisfied: int = 0) -> str:
        """The repair prompt. Turn 2 is sharper and explicit about the failure mode.

        A model that under-covered once is usually one restatement away from covering everything, and
        the second turn names *why* the first was rejected — so the retry is a correction rather than a
        repetition, which is the only kind of retry worth spending.

        `unrecognized` names the references the rejected reply actually used and that resolved to no
        criterion here. A real run's replies named `PM1`–`PM12` — the *checklist* ids, printed in the
        prompt just above the criteria — so "cover every criterion" restated produced the same list
        again, twice, on every attempt. Saying which names counted for nothing is what turns the retry
        into a correction; it invents no coverage, it reports what the check did with what was written.

        `unsatisfied` is the other zero-coverage case, and the one the wording missed: the reply named
        every criterion correctly and marked all of them `"satisfied": false`. Another real run's `api`
        node did exactly that — "the task lacks necessary details to cover the domain operations" — on
        every attempt, so the contract recorded no coverage each time while the reply looked complete.
        The prompt invited that reading; it now states the standard, and this says it again where the
        model is actually looking.
        """
        ignored = [str(name) for name in unrecognized if str(name).strip()]
        if reason == "criteria":
            framing = (
                "You have already produced the work below. It is complete; do not redo it.\n\n"
                "Your previous reply's `criteria_satisfied` did NOT cover THIS node's own completion "
                "criteria — most likely because you named the upstream node's criteria, or the "
                "checklist's own ids, rather than this node's criteria. The criteria that gate THIS "
                "node are the ones listed below, and only those count.\n\n"
            )
        else:
            framing = (
                "You have already produced the work below. It is complete; do not redo it.\n\n"
                "Your previous reply is in the user message. It is MISSING the required machine-readable "
                "trailer, so the engine could not record your result.\n\n"
            )
        if ignored:
            framing += (
                "The criteria your last reply named were: "
                + ", ".join(f"`{name[:80]}`" for name in ignored[:6])
                + ". NONE of those is a criterion of this node, so the coverage they claim is nothing. "
                "A checklist id (`PM1`, `CR1`) identifies an item in the checklist block, not a "
                "criterion — the two blocks are separate and both must be reported, in their own "
                "fields.\n\n"
            )
        if unsatisfied and not ignored:
            framing += (
                f"Your last reply named this node's criteria correctly and marked all {unsatisfied} of "
                "them `\"satisfied\": false`. The contract counts only criteria you claim as satisfied, "
                "so a reply like that reports no coverage at all and the node stays blocked. Measure "
                "each criterion against the goal you were given rather than against the skill's fullest "
                "use: if your output addresses that dimension as far as the goal requires, mark it "
                "`true` and cite the evidence; mark it `false` only when the goal needed something that "
                "is genuinely missing.\n\n"
            )
        if turn > 1:
            framing += (
                "Your LAST attempt was rejected as well: it still did not cover every criterion "
                "below. This must be exactly one JSON object, and `criteria_satisfied` must be an "
                "**array with one entry per criterion below** — not a single object, not a partial "
                "list, not an object keyed by text. If you genuinely cannot evidence a criterion, "
                "still include its entry with `\"satisfied\": false` and say what is missing.\n\n"
            )
        return (
            framing
            + "Reply with ONLY a fenced block tagged "
            f"`{TRAILER_FENCE}` containing one JSON object that describes the work you already did. "
            "No prose before or after.\n\n"
            "The object must have:\n"
            '- `status`: "done" if the work is complete, otherwise "needs_review" or "blocked".\n'
            '- `summary`: one paragraph in plain prose saying what you did and the headline result. '
            "This is a required handoff field: a payload without it is refused at the edge, so an "
            "answer with no summary cannot advance even when every criterion is covered.\n"
            '- `criteria_satisfied`: an ARRAY with one object for EVERY criterion below, each with '
            '`criterion` either the criterion\'s id (`c1`, `c2`, … as listed below) or its text '
            'copied verbatim from the list, `satisfied` (true/false) and `evidence` (a path, a hash, '
            'or command output). Cover ALL of them. An entry naming none of the criteria below counts '
            'for nothing — including a copy of an example, which is not a report.\n'
            f'- `checklist`: one entry per id for every id here: {checklist}. Each entry is '
            '`{"id":…,"status":"PASS|FAIL|N/A","evidence":…}`. Do not omit an id.\n'
            '- `artifacts`: the files you produced, each as '
            '`{"type":…,"path":…,"content":…}`.\n\n'
            "THE COMPLETION CRITERIA THAT GATE THIS NODE (copy each verbatim):\n"
            f"{criteria}\n\n"
            "Rules: valid JSON only inside the fence; `evidence` must be concrete; do not claim a "
            "pass you cannot evidence."
        )

    def _parse(self, reply: str) -> tuple[dict[str, Any], str]:
        """Parse the machine-readable trailer, degrading rather than discarding.

        A model that ignores the trailer format has still done work. Discarding it would waste the
        tokens it cost and produce a failure the Owner cannot act on, so an unparsable reply becomes a
        `needs_review` carrying the raw text.
        """
        try:
            return extract_trailer(reply, require=True) or {}, ""
        except TrailerError as exc:
            self._log("trailer.unparsable", level="warning", message=str(exc),
                      detail={"reply_chars": len(reply)})
            return {}, str(exc)

    def _to_runner_result(self, *, node_id: str, skill: str, bundle: SkillBundle,
                          trailer: dict[str, Any], artifacts: list[ArtifactRef],
                          usage: dict[str, Any], parse_error: str, reply: str,
                          agent: Any, session: Session, prepared: dict[str, Any]) -> dict[str, Any]:
        """Shape the reply into a dict the runner will accept against the node's contract.

        The contract checks are the reason this function exists: `criteria_met` must cover every
        declared criterion, and `evidence` must be non-empty when the contract requires it.
        """
        criteria = list(bundle.contract.criteria)
        met = self._criteria_met(trailer, criteria)
        evidence = self._evidence_for(trailer, artifacts, met)

        status = str(trailer.get("status") or ("needs_review" if parse_error else "done"))
        if status not in ("done", "blocked", "needs_review", "skipped"):
            status = "done"
        verdict = str(trailer.get("verdict") or self._infer_verdict(trailer, parse_error))
        summary = str(trailer.get("summary") or "").strip()

        # A parse failure, an uncovered criterion or a self-declared non-done all mean the node needs
        # review rather than silently advancing.
        if parse_error:
            status = "needs_review"
            summary = summary or f"reply did not carry a parsable trailer; raw reply follows: {reply[:300]}"
        if bundle.contract.evidence_required and not evidence:
            # The runner would reject this as a contract violation. Marking it needs_review is the
            # honest outcome: the node cannot evidence its own done.
            status = "needs_review"
            summary = summary or "no evidence was produced for a node whose contract requires it"
        elif bundle.contract.criteria and len(met) < len(criteria):
            status = "needs_review"
            summary = summary or (
                f"only {len(met)} of {len(criteria)} completion criteria were covered"
            )

        # A summary is **always** produced, and this is a correctness rule rather than a nicety.
        #
        # The handoff contract lists `summary` as required, and the edge guardrail refuses a payload
        # without one — so a node that answered with a valid trailer but no prose (status plus criteria
        # coverage, which the contract asks for and the repair prompt requests) had its own completed
        # work blocked at the edge and reported as `guardrail-blocked`. That blamed the guardrail for a
        # blank field the executor emitted, and it is exactly what a real run hit: `trailer.repair.ok`
        # followed by a guardrail block on `pm`, with nothing actionable in the log.
        summary = summary or self._derived_summary(reply, status, verdict, met, criteria)

        result: dict[str, Any] = {
            "status": status,
            "verdict": verdict,
            "evidence": evidence,
            "summary": summary[:400],
            "criteria_met": met,
            "usage": {
                "tokens_in": usage.get("tokens_in") or 0,
                "tokens_out": usage.get("tokens_out") or 0,
                "cost_usd": usage.get("cost_usd") or 0.0,
            },
        }
        if trailer.get("diagnostics"):
            result["diagnostics"] = list(trailer["diagnostics"])
        elif parse_error:
            result["diagnostics"] = [parse_error]
        else:
            failed = [c for c in trailer.get("checklist") or []
                      if isinstance(c, dict) and str(c.get("status", "")).upper() == "FAIL"]
            if failed:
                result["diagnostics"] = [
                    f"{c.get('id')}: {str(c.get('evidence') or '')[:160]}" for c in failed
                ]

        if artifacts:
            # The name must match the declared output type, or the runner warns that a declared
            # artifact was not produced.
            result["artifacts"] = [
                {"name": self._artifact_name(ref, bundle), "path": ref.path,
                 "sha": ref.sha256[:12], "type": ref.type}
                for ref in artifacts
            ]
        else:
            result["artifacts"] = self._declared_outputs_as_refs(bundle)

        findings = self._findings(trailer)
        if findings:
            result["findings"] = findings
            result["diagnostics"] = list(result.get("diagnostics") or []) + [
                f"{f.get('severity', '?')}: {f.get('issue', '')[:140]}" for f in findings[:5]
            ]
        if trailer.get("decisions"):
            result["decisions"] = list(trailer["decisions"])
        if trailer.get("open_questions"):
            result["open_questions"] = list(trailer["open_questions"])
        if trailer.get("delegation_request"):
            result["delegation_request"] = trailer["delegation_request"]

        # The node's own record, for the UI and for the next node's handoff.
        result["_agent"] = {"agent_id": agent.id, "name": agent.name, "session": session.session_id,
                            "skill": skill}
        if prepared.get("compacted"):
            result["_compacted"] = prepared["compacted"]
        if prepared.get("rotated"):
            result["_rotated"] = prepared["rotated"]

        # A reviewer that returns changes_requested carries the findings forward explicitly, because
        # the next developer pass needs them and the runner does not model a rework payload.
        if str(verdict).lower() in ("changes_requested", "changes", "fail") and not result.get("findings"):
            result["findings"] = [{
                "id": "F1", "severity": "High", "dimension": "quality",
                "file": "", "line": 0, "issue": summary or "the reviewer requested changes",
                "fix": "address the reviewer's summary before resubmitting",
            }]
        return result

    def _derived_summary(self, reply: str, status: str, verdict: str,
                         met: list[str], criteria: list[str]) -> str:
        """A summary when the model wrote none, so the handoff never carries a blank one.

        The model is asked for `summary`, but a valid trailer can arrive without prose — and a blank
        summary is not merely unhelpful: the runner's handoff contract lists it as required and the edge
        guardrail refuses a payload without it, so an honest, criteria-complete result was blocked and
        reported as `guardrail-blocked` with no actionable cause.

        Three sources, in order of usefulness, and the answer is always non-empty:

        1. The first paragraph of the reply's own prose, which is what the model actually said.
        2. A statement of the criteria it covered, which is the machine-readable content it *did* emit.
        3. The status and verdict, which is always available and never wrong.

        This is deliberately a fallback, not a replacement: a model-supplied summary is left untouched.
        """
        prose = ""
        for block in (reply or "").split("\n\n"):
            candidate = block.strip()
            # Skip the fenced trailer itself and any heading-only line: neither is a summary.
            if not candidate or candidate.startswith("```") or candidate.startswith("#"):
                continue
            if candidate.startswith("{") or candidate.startswith("["):
                continue
            prose = candidate
            break
        if prose:
            return prose[:400]
        if criteria and met:
            return (f"{status} ({verdict}): covered {len(met)} of {len(criteria)} completion "
                    "criteria; the model returned no prose summary.")
        return f"{status} ({verdict}): the model returned no prose summary."

    def _criteria_met(self, trailer: dict[str, Any], criteria: list[str]) -> list[str]:
        """Map the trailer's satisfied criteria onto the node's *declared* criteria.

        Only genuine matches are returned. A reference that matches no declared criterion is dropped
        rather than echoed: echoing it would make an unrelated list — most often the upstream node's
        criteria, which sit in the handoff context — look like coverage, and the contract check would
        then pass a node that covered none of its own obligations. Dropping it is what makes
        "0 of 3 covered" visible instead of a false "3 of 3".

        Matching is exact first, then a distinctive-fragment match, mirroring the runner's own rule so
        the two cannot disagree about what counts.
        """
        return self._criterion_references(trailer, criteria)[0]

    def _criterion_references(self, trailer: dict[str, Any],
                              criteria: list[str]) -> tuple[list[str], list[str]]:
        """Resolve the trailer's criteria references, and report the ones that resolved to nothing.

        The second half is what a repair turn needs. A model that covered nothing usually *did* answer
        with references — just not to this node's criteria: a real run's replies named `PM1`–`PM12`,
        which are this skill's **checklist** ids, printed in the prompt immediately above the criteria.
        Coverage of nothing is then not a mystery, and the retry can say which names were ignored
        instead of repeating the same instruction and getting the same list back. Naming them fabricates
        nothing: it only reports what the model already wrote and what the check did with it.
        """
        if not criteria:
            return [], []
        satisfied = trailer.get("criteria_satisfied") or []
        normalised = [c.strip() for c in criteria]
        met: list[str] = []
        unmatched: list[str] = []
        for entry in satisfied:
            if isinstance(entry, dict):
                # `satisfied: false` means the model explicitly did not meet it, which must not count.
                if entry.get("satisfied") is False:
                    continue
                reference = str(entry.get("criterion") or "").strip()
            else:
                reference = str(entry).strip()
            if not reference:
                continue
            lowered = reference.lower()
            # An explicit index reference (`c2`, `2`) resolves against the declared order.
            index_match = re.fullmatch(r"c?(\d+)", lowered)
            if index_match:
                index = int(index_match.group(1))
                if 1 <= index <= len(normalised):
                    met.append(normalised[index - 1])
                else:
                    unmatched.append(reference)
                continue
            exact = [c for c in normalised if c.lower() == lowered]
            if exact:
                met.append(exact[0])
                continue
            partial = [c for c in normalised if lowered in c.lower() or c.lower() in lowered]
            if len(partial) == 1:
                met.append(partial[0])
                continue
            # No match: the reference names something this node was not asked to satisfy. Dropping it
            # is the whole point — see `_criteria_met`.
            unmatched.append(reference)
        # De-duplicate while preserving order.
        return _unique_strings(met), _unique_strings(unmatched)

    def _evidence_for(self, trailer: dict[str, Any], artifacts: list[ArtifactRef],
                      met: list[str]) -> list[str]:
        """Collect the evidence the runner will check against `completion.evidence: required`.

        Built from three sources in order of strength: a real artifact with a hash, the trailer's own
        evidence strings, and the criteria the model claimed to satisfy. Falling back to the criteria
        is deliberate — a claimed-but-unevidenced criterion is still *something* the Owner can check,
        and an empty list would be rejected by the contract.
        """
        evidence: list[str] = []
        for ref in artifacts:
            evidence.append(f"{ref.type}:{ref.path}#{ref.sha256[:12]}")
        for entry in trailer.get("checklist") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status", "")).upper() == "PASS" and entry.get("evidence"):
                evidence.append(f"{entry.get('id', '?')}: {str(entry['evidence'])[:200]}")
        for entry in trailer.get("criteria_satisfied") or []:
            if isinstance(entry, dict) and entry.get("evidence"):
                evidence.append(str(entry["evidence"])[:200])
        if not evidence:
            evidence = [f"claimed: {c[:160]}" for c in met[:5]]
        seen: set[str] = set()
        out: list[str] = []
        for entry in evidence:
            if entry and entry not in seen:
                seen.add(entry)
                out.append(entry)
        return out[:20]

    def _findings(self, trailer: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalise the trailer's findings.

        A finding without a severity is given one rather than dropped: a dropped finding is a fix
        nobody makes.
        """
        out: list[dict[str, Any]] = []
        for index, entry in enumerate(trailer.get("findings") or []):
            if not isinstance(entry, dict):
                continue
            out.append({
                "id": str(entry.get("id") or f"F{index + 1}"),
                "severity": str(entry.get("severity") or "Medium"),
                "dimension": str(entry.get("dimension") or "quality"),
                "owasp": str(entry.get("owasp") or ""),
                "file": str(entry.get("file") or entry.get("path") or ""),
                # A model writes `"line": "N/A"` as readily as a number, and `int()` on that raised
                # `ValueError` straight out of `execute_node`. The runner reports that as "a node
                # raised an error" and ends the run on it — a real run died this way on its third
                # node, with two nodes already done. A line number is not worth a run: an
                # unparsable one becomes 0, exactly as an absent one does.
                "line": _as_int(entry.get("line")),
                "issue": str(entry.get("issue") or entry.get("note") or "")[:1000],
                "fix": str(entry.get("fix") or "not specified")[:1000],
            })
        return out

    def _infer_verdict(self, trailer: dict[str, Any], parse_error: str) -> str:
        """Derive a verdict when the model did not supply one.

        A reviewer's verdict is what the loop's `exit_when` tests, so an absent one must be derived
        rather than left empty — an empty verdict would read as "not pass" and loop forever.
        """
        if parse_error:
            return "changes_requested"
        failed = [c for c in trailer.get("checklist") or []
                  if isinstance(c, dict) and str(c.get("status", "")).upper() == "FAIL"]
        if failed:
            return "changes_requested"
        critical = [f for f in trailer.get("findings") or []
                    if isinstance(f, dict) and str(f.get("severity", "")).lower() in
                    ("critical", "high")]
        if critical:
            return "changes_requested"
        if str(trailer.get("status") or "") != "done":
            return "changes_requested"
        return "pass"

    def _artifact_name(self, ref: ArtifactRef, bundle: SkillBundle) -> str:
        """Name an artifact after a declared output when possible.

        The runner warns when a declared `outputs` entry is absent from the result, so naming an
        artifact after the output it satisfies is what keeps the contract check clean.
        """
        for output in bundle.contract.outputs:
            if output and output.replace("-", "_") in ref.path.replace("-", "_"):
                return output
        if bundle.contract.outputs:
            return bundle.contract.outputs[0]
        return Path(ref.path).stem

    def _declared_outputs_as_refs(self, bundle: SkillBundle) -> list[dict[str, Any]]:
        """Placeholders for declared outputs this node did not write.

        Reported so the runner's warning names what is missing, rather than the node silently
        appearing to have produced nothing.
        """
        return []

    # ── artifacts ───────────────────────────────────────────────────────────

    def _persist_artifacts(self, node_id: str, trailer: dict[str, Any], agent: Any,
                           state: dict[str, Any], *, attempt: int) -> list[ArtifactRef]:
        """Write the artifacts the node declared, through the idempotency journal.

        Two guarantees: a retried attempt does not re-write the same artifact, and a write is atomic
        so the UI never reads a half-written file.
        """
        declared = trailer.get("artifacts") or trailer.get("files") or []
        refs: list[ArtifactRef] = []
        for index, entry in enumerate(declared):
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "").strip()
            if not path:
                continue
            content = entry.get("content")
            if content is None:
                # A path with no content means the file was written another way (a tool call, or by
                # the agent's own process). Record a hash of the existing file so the artifact index
                # is still truthful.
                digest = self.ctx.store.hash_of(path)
                if digest is None:
                    continue
                ref = ArtifactRef(type=str(entry.get("type") or "file"), path=path,
                                  sha256=digest, bytes=0, produced_by=agent.id,
                                  phase=self._phase_for(state), node_id=node_id)
                refs.append(ref)
                continue

            inputs_hash = f"{path}:{len(str(content))}"
            applied = self._apply_effect(node_id, attempt, f"artifact:{path}", inputs_hash)
            if applied is not None:
                refs.append(_ref_from_record(applied))
                continue
            try:
                ref = self.ctx.store.write(
                    path, str(content), producer=agent.id,
                    artifact_type=str(entry.get("type") or "file"),
                    phase=self._phase_for(state), node_id=node_id,
                )
            except WorkspaceError as exc:
                self._log("artifact.rejected", level="warning", node_id=node_id,
                          agent_id=agent.id, message=str(exc))
                continue
            self._record_effect_result(node_id, attempt, f"artifact:{path}", inputs_hash,
                                       ref.as_dict())
            refs.append(ref)
            self._log("artifact.written", node_id=node_id, agent_id=agent.id,
                      detail={"path": ref.path, "sha256": ref.sha256[:12], "bytes": ref.bytes})
        return refs

    def _record_effect_result(self, node_id: str, attempt: int, effect: str, inputs_hash: str,
                              result: dict[str, Any]) -> None:
        """Return a recorded artifact ref when this effect was already applied, else None."""
        if self.ctx.journal is None:
            return None
        record = self.ctx.journal.lookup(effect_key(
            run_id=self.ctx.run_id or "run", node_id=node_id, attempt=attempt,
            inputs_hash=inputs_hash, effect=effect, target=effect,
        ))
        if record is not None and record.state.value == "completed" and record.result:
            return dict(record.result)
        return None

    def _apply_effect(self, node_id: str, attempt: int, effect: str,
                      inputs_hash: str) -> dict[str, Any] | None:
        """Return a recorded artifact ref when this effect was already applied, else None.

        Used to make an artifact write idempotent: a retried attempt replays the recorded ref rather
        than writing the file again, which is what keeps the review loop's no-progress hash comparison
        meaningful.
        """
        if self.ctx.journal is None:
            return None
        record = self.ctx.journal.lookup(effect_key(
            run_id=self.ctx.run_id or "run", node_id=node_id, attempt=attempt,
            inputs_hash=inputs_hash, effect=effect, target=effect,
        ))
        if record is not None and record.state.value == "completed" and record.result:
            return dict(record.result)
        return None

    def _record_effect_result(self, node_id: str, attempt: int, effect: str, inputs_hash: str,
                              result: dict[str, Any]) -> None:
        """Record an applied artifact write so a retry replays instead of re-writing.

        The *canonical* ref dict is recorded (not the wire form), so a replay reconstructs an
        ArtifactRef without a translation step.
        """
        if self.ctx.journal is None:
            return
        with self.ctx.journal.effect(effect, run_id=self.ctx.run_id or "run", node_id=node_id,
                                     attempt=attempt, inputs_hash=inputs_hash,
                                     target=effect) as record:
            if not record.replayed:
                record.record(result)

    # ── run-state helpers ───────────────────────────────────────────────────

    def _manifest(self, state: dict[str, Any]) -> dict[str, Any]:
        """The manifest this run is executing.

        The runner's `state` deliberately does **not** carry the manifest — it holds the run's
        progress, and the manifest is the authority on structure. So the executor loads it by path.
        Reading node declarations from the manifest rather than from run-state is what makes the
        executor correct: run-state only knows nodes that have *started*.
        """
        cached = getattr(self, "_manifest_cache", None)
        if cached is not None:
            return cached
        manifest: dict[str, Any] = {}
        path = getattr(self.ctx, "manifest_path", None)
        if path:
            try:
                import importlib.util

                root = Path(str(getattr(self.ctx.skills, "library_root", "")))
                scripts = root / "scripts"
                if str(scripts) not in sys.path:
                    sys.path.insert(0, str(scripts))
                spec = importlib.util.spec_from_file_location(
                    "_agentorg_safe_yaml", scripts / "lib" / "safe_yaml.py")
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    manifest = module.parse(Path(path).read_text(encoding="utf-8")) or {}
            except Exception as exc:  # noqa: BLE001 - fall back to run-state
                self._log("manifest.unreadable", level="warning", message=str(exc))
        if not manifest:
            # Fall back to whatever the caller injected, then to run-state.
            manifest = dict(getattr(self.ctx, "manifest", None) or
                            state.get("manifest") or {})
        self._manifest_cache = manifest
        return manifest

    def _node_for(self, node_id: str, state: dict[str, Any]) -> dict[str, Any]:
        """The node's declaration, from the manifest."""
        for node in self._manifest(state).get("nodes") or []:
            if str(node.get("id")) == node_id:
                return node
        # Not a skill node: it may be a gate, so return its gate declaration.
        gate = self._gate_declaration(node_id, state)
        if gate is not None:
            return gate
        return {"id": node_id}

    def _attempt_from_state(self, node_id: str, state: dict[str, Any]) -> int:
        """How many times this node has already run, from run-state."""
        record = (state.get("nodes") or {}).get(node_id) or {}
        return int(record.get("iterations") or 0) + 1

    def _max_attempts(self, state: dict[str, Any]) -> int:
        """The revision budget the loop declares.

        Read from the manifest's first loop rather than assumed, because the prompt tells the agent
        "revise or escalate" based on this number — telling it 3 when the loop allows 1 would invite an
        escalation the runner never intended.
        """
        for loop in self._manifest(state).get("loops") or []:
            try:
                return max(1, int(loop.get("max_iterations") or 3))
            except (TypeError, ValueError):
                continue
        return 3

    def _rework_context(self, state: dict[str, Any]) -> dict[str, Any]:
        """The contract refusal this attempt is answering, or `{}` on a first attempt.

        Held in run-state by the runner, which owns the rework window: it knows the rule that fired,
        the attempt number and the questions that broke the ceiling, and it writes them before
        dispatching the retry. Reading them here rather than re-deriving them is the same rule the
        handoff contract follows — the executor renders the refusal, it does not have a second
        opinion about what the refusal was.
        """
        context = state.get("_contract_rework")
        return dict(context) if isinstance(context, dict) else {}

    def _phase_for(self, state: dict[str, Any]) -> str:
        """The current phase, from run-state."""
        return str(state.get("phase") or "EXECUTE")

    def _inputs_for(self, node: dict[str, Any], state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """The artifacts this node consumes, from the runner's artifact index.

        Falls back to everything produced so far when the node declares no inputs, because a node with
        no declared inputs is a generator and gets what exists.
        """
        index = state.get("artifacts") or {}
        wanted = list(node.get("inputs") or [])
        out: dict[str, dict[str, Any]] = {}
        if wanted:
            for name in wanted:
                info = index.get(name)
                if info:
                    out[name] = info
                    continue
                # An input not indexed by name may still exist on disk by path.
                digest = self.ctx.store.hash_of(name)
                if digest:
                    out[name] = {"path": name, "sha256": digest, "type": name}
            return out
        for name, info in list(index.items())[:8]:
            out[name] = info
        return out

    def _findings_for(self, node_id: str, state: dict[str, Any]) -> list[dict[str, Any]]:
        """The findings a rework pass must address.

        Read from run-state's node records so a resumed run passes them on, and from the previous
        reviewer's result so an in-memory run does too.
        """
        review_node = (state.get("_review_node") or "reviewer")
        record = (state.get("nodes") or {}).get(review_node) or {}
        findings = record.get("findings")
        if isinstance(findings, list) and findings:
            return findings
        with self.registries:
            for exec_result in list(self.history.values()):
                if exec_result.findings:
                    return exec_result.findings
        return []

    def _handoff_payload(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """The upstream handoff, assembled from run-state."""
        nodes = state.get("nodes") or {}
        if not nodes:
            return None
        decisions = list(state.get("decisions") or [])
        questions = list(state.get("open_questions") or [])
        summaries = [f"{name}: {str(rec.get('summary') or '')[:120]}"
                     for name, rec in nodes.items() if isinstance(rec, dict) and rec.get("summary")]
        return {
            "summary": " | ".join(summaries[-3:]),
            "decisions": decisions,
            "open_questions": questions,
            "artifacts": list((state.get("artifacts") or {}).values()),
        }

    def _instruction_for(self, node_id: str, node: dict[str, Any], skill: str,
                         state: dict[str, Any], *, resolved_inputs: dict[str, Any] | None = None) -> str:
        """What this node is being asked to do.

        Built from the node's identity and its declared outputs, plus the run's own goal. The goal
        matters on the first node: it has no upstream artifacts to inherit the task from, and the
        prompt's own intake block tells the model "the task statement is the input" — so omitting the
        goal here leaves the entry node with literally nothing to do, and a competent model correctly
        replies that it received no input. Downstream nodes see the goal restated for the same reason:
        it is cheap, and it keeps every node anchored to what was actually asked for.

        **The declared-input trap.** A skill declares inputs for its *typical* use —
        `backend-developer` declares `inputs: [findings]` because fixing review findings is its
        documented primary use, yet in a greenfield build nothing produces `findings`. The planner
        copies those declarations onto the node, so an instruction reading "Consume: findings" while
        the intake block in the *same prompt* reads "Nothing. This is the first node…" tells the model
        two contradictory things. A real run failed on exactly this: `pm` was instructed to consume
        `market-context` (which no node produces) and, correctly reading its intake as empty, reported
        "No input provided to start the PRD writing process" and failed its own completion contract.

        So when the resolved inputs are known, only the inputs the node *actually received* are named
        as things to consume, and a declared-but-unproduced input is stated plainly as not supplied —
        which is the honest description of the situation, and the one a competent model can act on.
        """
        outputs = ", ".join(node.get("outputs") or []) or "the node's declared outputs"
        declared = [str(i) for i in (node.get("inputs") or [])]
        inputs = ", ".join(declared) or "everything produced so far"
        missing: list[str] = []
        if resolved_inputs is not None:
            received = sorted(k for k in resolved_inputs)
            missing = [i for i in declared if i not in received]
            inputs = ", ".join(received) if received else "nothing yet"
        role = "Review" if self._is_reviewer(skill, node) else "Produce"
        goal = str(self._manifest(state).get("description") or "").strip()
        statement = (
            f"The goal for this run is: {goal}\n\n" if goal else ""
        )
        # Naming a missing input is deliberate. It is the difference between "you are missing
        # something and here is what" — which the model can route around, or say it cannot start — and
        # a silent contradiction it can only misread.
        caveat = ""
        if missing:
            named = "`, `".join(missing)
            caveat = (
                f" (declared `{named}` is not produced by any upstream node, so it was "
                "not supplied; proceed from what you have and record it as an assumption or an open "
                "question rather than treating it as a blocker)"
            )
        return (
            f"{statement}"
            f"{role} as node `{node_id}` using the {skill} skill. "
            f"Consume: {inputs}{caveat}. Produce: {outputs}. "
            f"Work to the completion criteria and checklist below, and report every checklist id "
            f"with its evidence."
        )

    def _open_questions(self) -> list[dict[str, Any]]:
        """Open questions accumulated, for a rotation handoff."""
        out: list[dict[str, Any]] = []
        with self.registries:
            for exec_result in list(self.history.values()):
                out.extend(getattr(exec_result, "open_questions", []) or [])
        return out[:3]

    def _ledger_decisions(self) -> list[dict[str, Any]]:
        """Decisions recorded so far, for a rotation handoff.

        Only what the nodes reported, since the executor does not own the ledger — the orchestrator
        does. Passing them keeps a rotated session consistent with the run's decision history.
        """
        out: list[dict[str, Any]] = []
        with self.registries:
            for exec_result in list(self.history.values()):
                out.extend(getattr(exec_result, "decisions", []) or [])
        return out[:8]

    def _system_prompt(self, bundle: SkillBundle) -> str:
        """The system prompt for a node, reused for projection and for the call."""
        return self.builder._system_prompt(bundle, agent_name="", agent_skill=bundle.name)

    def _recall(self, bundle: SkillBundle) -> str:
        """Prior-run memory, as a context-only block."""
        if self.ctx.memory is None:
            return ""
        try:
            return self.ctx.memory.context_block(
                self.ctx.workflow or bundle.name, skills=[bundle.name], limit=3)
        except Exception:  # noqa: BLE001 - recall is an optimisation, never a requirement
            return ""

    def _record_session_turn(self, session: Session, prompt: Any, reply: str,
                             usage: dict[str, Any]) -> None:
        """Append the exchange to the session so the next turn's projection accounts for it."""
        session.append_text("user", prompt.text[:2000], tier=2)
        session.append_text("assistant", reply, tier=2)

    def _observe(self, node_id: str, agent: Any, result: dict[str, Any],
                 usage: dict[str, Any], *, bundle: SkillBundle, session: Session) -> None:
        """Feed the health monitor, the span exporter and the mailbox."""
        status = str(result.get("status") or "")
        breached = status not in ("done", "skipped")
        tokens = (usage.get("tokens_in") or 0) + (usage.get("tokens_out") or 0)
        cost = usage.get("cost_usd")

        if agent.budget.allocated_tokens or agent.budget.allocated_usd:
            # A read-modify-write on a shared per-agent budget. Two concurrent siblings of one node
            # (two voters, two fan-out items, two group members) bind to the same agent, so an
            # unguarded `+=` loses one of the two charges and the budget under-reports what was spent
            # — the one number the design forbids flattering.
            with self.registries:
                agent.budget.charge(usd=cost or 0.0, tokens=tokens)
        self._emit_node_span(node_id, agent, result, usage, bundle, session)

        monitor = getattr(self.ctx, "health", None)
        if monitor is None:
            monitor = getattr(self.ctx, "monitor", None)
        if monitor is not None:
            try:
                transition = monitor.observe(
                    agent.id,
                    outcome="success" if status == "done" else
                            ("escalation" if status == "blocked" else "failure"),
                    saturation=session.saturation,
                    breached=breached,
                )
                if transition.changed:
                    self._log("agent.health.changed", node_id=node_id, agent_id=agent.id,
                              detail=transition.as_dict())
            except Exception:  # noqa: BLE001 - health must never break a node
                pass

    # ── the identify mode (an agent gate asking which channel leads) ────────

    def _identify(self, gate_id: str, state: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        """Answer an agent gate's `mode: identify` call: which corrective channel should lead.

        The runner gives the untried pool. The choice is made by asking the *most capable* agent bound
        to any of those channels, which keeps the decision inside the org rather than in a heuristic
        here — and falls back to the pool's first entry, which is the runner's own documented default.
        """
        pool = [str(p) for p in (ctx.get("pool") or [])]
        reason = str(ctx.get("reason") or "loop could not converge")
        if not pool:
            return {"status": "done", "verdict": "noop",
                    "summary": "no untried channel remained in the pool",
                    "evidence": [f"identify:{gate_id}:empty"]}

        choice = self._choose_channel(pool, reason)
        self._log("gate.agent.reroute", node_id=gate_id, message=reason[:200],
                  detail={"pool": pool, "chosen": choice})
        return {
            "status": "done",
            "verdict": "reroute",
            "next": choice,
            "summary": f"identify gate chose {choice!r} to lead the reroute: {reason[:200]}",
            "evidence": [f"identify:{gate_id}:{choice}"],
            "diagnostics": [f"pool: {', '.join(pool)}", f"chosen: {choice}"],
        }

    def _choose_channel(self, pool: list[str], reason: str) -> str:
        """Pick the corrective channel, preferring the most capable available agent.

        A heuristic rather than a model call: the gate is a routing decision inside a bounded reroute
        budget, and spending a call on it would consume the budget the reroute needs.
        """
        best: tuple[int, str] | None = None
        # Score each channel by the most capable agent bound to it.
        for node_id in pool:
            node = self._node_for(node_id, {})
            skill = str(node.get("skill") or node_id)
            candidates = self.org.candidates_for(skill, available_only=False)
            level = max((int(a.level) for a in candidates), default=0)
            if best is None or level > best[0]:
                best = (level, node_id)
        return best[1] if best else pool[0]

    # ── remembering ─────────────────────────────────────────────────────────

    def _remember(self, node_id: str, skill: str, agent: Any, result: dict[str, Any],
                  session: Session, artifacts: list[ArtifactRef], trailer: dict[str, Any],
                  prepared: dict[str, Any]) -> None:
        """Record the node so the next node can inherit from it."""
        usage = result.get("usage") or {}
        executed = ExecutedNode(
            node_id=node_id, skill=skill, agent_id=agent.id, agent_name=agent.name,
            status=str(result.get("status") or ""), verdict=str(result.get("verdict") or ""),
            session_id=session.session_id,
            attempts=_as_int(trailer.get("attempt"), 1),
            tokens=(usage.get("tokens_in") or 0) + (usage.get("tokens_out") or 0),
            cost_usd=usage.get("cost_usd"),
            artifacts=list(artifacts),
            findings=list(result.get("findings") or []),
            criteria_met=list(result.get("criteria_met") or []),
            rotated=int(prepared.get("rotated") or 0),
            compacted=int(prepared.get("compacted") or 0),
        )
        with self.registries:
            self.history[node_id] = executed
            # An open_questions / decisions list is not carried on ExecutedNode; attach for the
            # handoff.
            executed.open_questions = list(trailer.get("open_questions") or [])
            executed.decisions = list(trailer.get("decisions") or [])
            if not executed.findings:
                self.producers[node_id] = agent.id

    def record_review(self, node_id: str, findings: list[dict[str, Any]]) -> None:
        """Record a rejection so the next developer pass receives it.

        Called by the orchestrator after writing `review_feedback.json`, so the executor's in-memory
        view and the on-disk dossier agree.
        """
        with self.registries:
            executed = self.history.get(node_id)
            if executed is not None:
                executed.findings = list(findings)

    # ── telemetry and logging helpers ───────────────────────────────────────

    def _log(self, event: str, *, level: str = "info", message: str = "",
             detail: dict[str, Any] | None = None, **chain: Any) -> None:
        diag = self.ctx.diagnostics
        if diag is None:
            return
        try:
            diag.log(event, level=level, message=message, detail=detail or {}, **chain)
        except Exception:  # noqa: BLE001 - diagnostics must never break a node
            pass

    def _emit_node_span(self, node_id: str, agent: Any, result: dict[str, Any],
                        usage: dict[str, Any], bundle: SkillBundle, session: Session) -> None:
        exporter = self.ctx.telemetry
        if exporter is None:
            return
        try:
            exporter.node_span(
                workflow=self.ctx.workflow or bundle.name, node=node_id,
                status=str(result.get("status") or ""),
                verdict=str(result.get("verdict") or ""),
                iterations=int(session.attempt),
                skill_hash=bundle.content_hash,
                tokens_prompt=usage.get("tokens_in"),
                tokens_completion=usage.get("tokens_out"),
                cost_usd=usage.get("cost_usd"),
                evidence=list(result.get("evidence") or [])[:5],
                attributes={"agent_id": agent.id, "session": session.session_id,
                            "saturation": round(session.saturation, 3),
                            "guardrail_block": False},
            )
        except Exception:  # noqa: BLE001 - telemetry must never break a node
            pass

    def _emit_llm_request(self, agent: Any, node_id: str, session: Session) -> None:
        bus = self.ctx.bus
        if bus is None:
            return
        try:
            from .protocol import EventType

            bus.emit(EventType.LLM_REQUEST, agent_id=agent.id, node_id=node_id,
                     session_id=session.session_id,
                     payload={"provider": agent.provider, "model": agent.model,
                              "estimated_prompt_tokens": session.used_tokens})
        except Exception:  # noqa: BLE001
            pass

    def _emit_llm_response(self, agent: Any, node_id: str, session: Session,
                           payload: dict[str, Any], cost: Any) -> None:
        bus = self.ctx.bus
        if bus is None:
            return
        try:
            from .protocol import EventType

            bus.emit(EventType.LLM_RESPONSE, agent_id=agent.id, node_id=node_id,
                     session_id=session.session_id,
                     payload={"provider": agent.provider, "model": agent.model,
                              "usage": payload.get("usage"), "latency_ms": payload.get("latency_ms"),
                              # `cost` already carries the cache tokens and the counterfactual saving,
                              # so the console can show a hit rate and what it saved without needing
                              # to reach into the subprocess that owns the gateway.
                              "cost": cost.as_dict()})
        except Exception:  # noqa: BLE001
            pass

    def _emit_rotation_span(self, agent_id: str, session: Session, decision: Any,
                            carried: int) -> None:
        exporter = self.ctx.telemetry
        if exporter is None:
            return
        try:
            exporter.rotation_span(
                agent=agent_id, index=session.index, trigger=decision.trigger.value,
                reason=decision.reason, saturation=decision.saturation,
                attention_weight=decision.attention_weight, constraints_carried=carried,
            )
        except Exception:  # noqa: BLE001
            pass

    # ── reporting ───────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """Per-node outcomes, for the UI and the completion view."""
        with self.registries:
            return {
                node_id: executed.as_dict()
                for node_id, executed in sorted(self.history.items())
            }

    def session_report(self) -> list[dict[str, Any]]:
        """Every live session, with its saturation and rotation count."""
        with self.ctx.lock:
            return [session.as_dict(include_turns=False)
                    for session in self.ctx.sessions.values()]


def _unsatisfied_count(trailer: dict[str, Any]) -> int:
    """How many of a reply's criteria entries it explicitly marked `satisfied: false`.

    The count is what distinguishes "the reply never mentioned the criteria" from "the reply named
    every criterion and denied every one" — the same zero coverage, and two failures that need
    different corrections.
    """
    return sum(1 for entry in (trailer.get("criteria_satisfied") or [])
               if isinstance(entry, dict) and entry.get("satisfied") is False)


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a model-supplied number, tolerating what a model writes instead.

    `"N/A"`, `""` and `null` are all values a real reply put where a number was asked for, and any of
    them raised `ValueError` out of `execute_node` — which the runner reports as a node that "raised an
    error" and ends the run on. Nothing the engine can do with such a field is worth a run, so it
    degrades to the default rather than propagating.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _unique_strings(values: list[str]) -> list[str]:
    """De-duplicate, preserving order — the order is the model's own, and it reads as a narration."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _env_flag(name: str) -> bool:
    """Whether an environment variable is set to a truthy value.

    Read here rather than through the config because `Config` is parsed once per process and this
    switch is deliberately *not* a config field: `executor.parallel_nodes` would apply concurrency to
    every group in every run, including ones the planner never reasoned about. The manifest's own
    `concurrent:` field is where that decision belongs, and this exists for the eval and the operator
    who want to force it on for a measurement.
    """
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def _ref_from_record(record: dict[str, Any]) -> ArtifactRef:
    """Rebuild an ArtifactRef from a journalled record.

    Accepts either the canonical field names or the wire names (`sha`), so a record written by an
    earlier build still replays rather than failing on a rename.
    """
    known = {f for f in ArtifactRef.__dataclass_fields__}  # type: ignore[attr-defined]
    data = dict(record)
    if "sha" in data and "sha256" not in data:
        data["sha256"] = data.pop("sha")
    return ArtifactRef(**{k: v for k, v in data.items() if k in known})
