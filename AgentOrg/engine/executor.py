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

Usage:
    executor = NodeExecutor(context=ctx)
    result = executor.execute_node("fixer", state, {"pass": 1})
"""

from __future__ import annotations

import json
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
    Org,
    Router,
    RouteContext,
    RouteClass,
    declared_policy,
)
from .prompts import TRAILER_FENCE, PromptBuilder, TaskContext, TrailerError, extract_trailer
from .skills.bundle import SkillBundle

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
    max_output_tokens: int = 4096
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = ArtifactStore(workspace_root=self.workspace)


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

    Thread-safe: supervisor fan-out runs several nodes concurrently, and the per-agent session map and
    the artifact store are both shared.
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
        attempt = int(ctx.get("pass") or self._attempt_from_state(node_id, state) or 1)
        return self._run(node_id, node, skill, state, attempt=attempt, ctx=ctx)

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

    def _run(self, node_id: str, node: dict[str, Any], skill: str, state: dict[str, Any], *,
             attempt: int, ctx: dict[str, Any]) -> dict[str, Any]:
        """The full node protocol: bind, project, prompt, call, parse, persist, return.

        One agent in the normal case, or several when the node was bound as a swarm. The swarm is
        resolved here rather than in the caller because the binding is what decides it — a node is a
        swarm only if its policy says so.
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

        # ── fan-out, when the node splits work rather than answers one question ──
        # A node declaring `fanout` with a `{{item}}` template and an `items` list spreads the work
        # across agents. Distinct from the SWARM binding below, which is a *vote*: this splits a job,
        # that decides one. Both are resolved here because the node's own declaration is what chooses
        # between them, and neither is reachable unless something reads it.
        if node.get("fanout") or node.get("items"):
            return self._run_fanout(node_id=node_id, node=node, skill=skill, state=state,
                                    attempt=attempt, bundle=bundle, binding=binding,
                                    inputs=inputs, findings=findings)

        if binding.policy is BindingPolicy.SWARM and len(binding.agents) > 1:
            return self._run_swarm(node_id=node_id, node=node, skill=skill, state=state,
                                   attempt=attempt, bundle=bundle, binding=binding,
                                   is_reviewer=is_reviewer, inputs=inputs, findings=findings,
                                   pooled=pooled)

        return self._run_one(node_id=node_id, node=node, skill=skill, state=state,
                             attempt=attempt, bundle=bundle, binding=binding,
                             is_reviewer=is_reviewer, inputs=inputs, findings=findings,
                             agent_id=binding.primary, pooled=pooled)

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

        prompt = self.builder.node_prompt(
            bundle,
            TaskContext(
                node_id=node_id,
                instruction=self._pooled_instruction(
                    pooled,
                    instruction_override
                    or self._instruction_for(node_id, node, skill, state)),
                inputs=inputs,
                handoff=self._handoff_payload(state),
                recalled=prepared["recall"],
                findings=findings,
                attempt=attempt,
                max_attempts=self._max_attempts(state),
                injected_constraints=prepared["constraints"],
                is_reviewer=is_reviewer,
                may_delegate=bool(self.ctx.config and
                                  getattr(self.ctx.config.delegation, "max_depth", 0)),
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
                              bundle=bundle, node=node)
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
            repaired = self._repair_trailer(agent, node_id=node_id, session=session, reply=reply,
                                            bundle=bundle, attempt=attempt,
                                            reason="parse" if parse_error else "criteria")
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
        different files, which is the point), and the node is `done` only when *every* item succeeded.
        One failed item makes the whole node `needs_review` with the failures named, because a
        partially-reviewed change set that reported "done" would be a lie.
        """
        from .fanout import FanoutError, plan_fanout, run_fanout

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

        def _one(item: Any, agent_id: str) -> tuple[str, str, int]:
            """Run one item as a full node execution. Injected so the plan owns only the policy."""
            result = self._run_one(node_id=node_id, node=node, skill=skill, state=state,
                                   attempt=attempt, bundle=bundle, binding=binding,
                                   is_reviewer=True, inputs=inputs, findings=findings,
                                   agent_id=agent_id, instruction_override=item.prompt)
            usage = result.get("usage") or {}
            tokens = int(usage.get("tokens_in") or 0) + int(usage.get("tokens_out") or 0)
            if str(result.get("status")) == "done":
                return (str(result.get("summary") or ""), "", tokens)
            return ("", str(result.get("summary") or result.get("status") or "not done"), tokens)

        run_fanout(plan, _one, agents=voters, max_parallel=max_parallel)
        summary = plan.summary()
        self._log("fanout.result", node_id=node_id, detail=summary)

        winner = next((r for r in (self.history.get(node_id, None),) if r is not None), None)
        base = self._fanout_result(node_id=node_id, plan=plan, summary=summary,
                                   skill=skill, bundle=bundle, inputs=inputs)
        if winner is not None:
            # The node's own recorded result carries the artifacts and usage; the fan-out detail is
            # added to it rather than replacing it, so nothing the executor already recorded is lost.
            base.setdefault("artifacts", [])
        return base

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
            "artifacts": [],
            "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0},
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
        """
        for other_id, exec_result in self.history.items():
            if other_id != node_id and not exec_result.findings:
                return exec_result.agent_id
        for other_id, agent_id in self.producers.items():
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
        new_message = self._instruction_for(node_id, node, bundle.name, {})

        projection = project(session, system=system, skill_body=skill_body, pinned=pinned,
                             recall=recall, new_message=new_message,
                             reserve=session.output_reserve)

        compacted = 0
        rotated = 0
        # Compact when warranted, then re-measure: one pass may not be enough, and the rotation
        # decision must be based on the post-compaction figure rather than the pre- one.
        if projection.must_compact:
            result = compact(session, compact_at=compact_at, evict_at=evict_at,
                             overflow_at=overflow_at)
            if result.effective:
                compacted = 1
                self._log("session.compact", node_id=node_id, agent_id=agent_id,
                          session_id=session.session_id,
                          detail={"band": result.band_before.value,
                                  "recovered": result.recovered,
                                  "preserved_verbatim": result.pinned_after})
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

        self._log("session.rotate", node_id=node_id, agent_id=agent_id,
                  session_id=fresh.session_id,
                  detail={"trigger": decision.trigger.value, "reason": decision.reason[:200],
                          "from": session.session_id, "constraints_carried": len(fresh.pinned)})
        self._emit_rotation_span(agent_id, fresh, decision, len(fresh.pinned))
        return fresh, 1

    # ── the call ────────────────────────────────────────────────────────────

    def _call(self, agent: Any, prompt: Any, *, node_id: str, attempt: int, session: Session,
              bundle: SkillBundle, node: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make the model call, guarded by the effect journal.

        The journal matters here for a subtle reason: a retry of the same attempt must not re-spend. If
        the effect were already applied, the recorded result is returned instead of calling again.

        A node that declares `tools: true` runs the **agentic loop** instead of one call: the model
        reads files, then writes, then answers. That is what lets an agent work on a real project
        rather than only on the artifacts a previous node handed it.
        """
        from .providers.base import ChatRequest, Message, Role

        if node is not None and self._tools_enabled(node):
            return self._call_with_tools(agent, prompt, node_id=node_id, attempt=attempt,
                                         session=session, bundle=bundle, node=node)

        request = ChatRequest(
            model=agent.model,
            messages=[Message.text_message(Role.USER, prompt.text)],
            system=prompt.system,
            max_tokens=min(self.ctx.max_output_tokens,
                           int(agent.max_output or self.ctx.max_output_tokens)),
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
        return ToolRegistry(
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
        )

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
        """The parent's remaining budget, which a fleet is carved from."""
        ledger = getattr(self.ctx, "ledger", None) or getattr(self, "ledger", None)
        try:
            snapshot = ledger.snapshot() if ledger is not None else {}
        except Exception:  # noqa: BLE001 - a missing ledger means "unknown", not zero spend
            return 0
        remaining = snapshot.get("remaining_tokens") if isinstance(snapshot, dict) else None
        try:
            return int(remaining) if remaining is not None else 0
        except (TypeError, ValueError):
            return 0

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
        try:
            bundle = self.ctx.skills.bundle(skill) if skill else None
        except Exception:  # noqa: BLE001 - an unknown skill yields a generic child, not a crash
            bundle = None
        body = getattr(bundle, "body", "") if bundle is not None else ""
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
                         node: dict[str, Any]) -> dict[str, Any]:
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

        loop = AgentLoop(complete=_complete, tools=registry, max_steps=max_steps,
                         can_continue=_gate, max_output_tokens=self.ctx.max_output_tokens,
                         on_step=lambda step: self._log("node.tool_step", node_id=node_id,
                                                        agent_id=agent.id, detail=step))
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
                        ) -> tuple[dict[str, Any], str] | None:
        """Ask once, in a focused turn, for the trailer the first reply omitted or mis-scoped.

        The original reply is *carried into the request* rather than replaced, so the model restates
        its own work as evidence instead of inventing new work. Nothing here is graded: if the repair
        turn also fails to parse, the node keeps its `needs_review` outcome and the original error.
        """
        from .providers.base import ChatRequest, Message, Role

        checklist = ", ".join(bundle.checklist_ids()) or "(none)"
        criteria = "\n".join(f"- {c}" for c in bundle.contract.criteria) or "- (none declared)"
        if reason == "criteria":
            framing = (
                "You have already produced the work below. It is complete; do not redo it.\n\n"
                "Your previous reply's `criteria_satisfied` did NOT cover THIS node's own completion "
                "criteria — most likely because you reported the upstream node's criteria, which "
                "happened to be in your context. The criteria that gate THIS node are the ones listed "
                "below, and only those count.\n\n"
            )
        else:
            framing = (
                "You have already produced the work below. It is complete; do not redo it.\n\n"
                "Your previous reply is in the user message. It is MISSING the required machine-readable "
                "trailer, so the engine could not record your result.\n\n"
            )
        instruction = (
            framing
            + "Reply with ONLY a fenced block tagged "
            f"`{TRAILER_FENCE}` containing one JSON object that describes the work you already did. "
            "No prose before or after.\n\n"
            "The object must have:\n"
            '- `status`: "done" if the work is complete, otherwise "needs_review" or "blocked".\n'
            '- `criteria_satisfied`: for every criterion below, an object with `criterion` '
            '(verbatim, copied from the list) `satisfied` (true/false) and `evidence` (a path, a '
            'hash, or command output). Cover ALL of them.\n'
            f'- `checklist`: one entry per id for every id here: {checklist}. Each entry is '
            '`{"id":…,"status":"PASS|FAIL|N/A","evidence":…}`. Do not omit an id.\n'
            '- `artifacts`: the files you produced, each as '
            '`{"type":…,"path":…,"content":…}`.\n\n'
            "THE COMPLETION CRITERIA THAT GATE THIS NODE (copy each verbatim):\n"
            f"{criteria}\n\n"
            "Rules: valid JSON only inside the fence; `evidence` must be concrete; do not claim a "
            "pass you cannot evidence."
        )
        request = ChatRequest(
            model=agent.model,
            messages=[Message.text_message(Role.USER,
                                           f"{instruction}\n\n--- YOUR PREVIOUS REPLY ---\n\n"
                                           f"{reply[:6000]}")],
            system="You emit one JSON object in a fenced block, and nothing else.",
            max_tokens=min(self.ctx.max_output_tokens, 2048),
        )
        self._log("trailer.repair", node_id=node_id, agent_id=agent.id,
                  detail={"original_chars": len(reply)})
        try:
            response = self.ctx.gateway.complete(
                request, provider_id=agent.provider, agent_id=agent.id,
                node_id=node_id, session_id=session.session_id,
            )
        except Exception as exc:  # noqa: BLE001 - a failed repair must not fail the node
            self._log("trailer.repair.failed", level="warning", node_id=node_id,
                      agent_id=agent.id, message=str(exc))
            return None
        trailer, error = self._parse(response.text)
        if error:
            return None
        self._log("trailer.repair.ok", node_id=node_id, agent_id=agent.id)
        return trailer, ""

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
        if not criteria:
            return []
        satisfied = trailer.get("criteria_satisfied") or []
        normalised = [c.strip() for c in criteria]
        met: list[str] = []
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
            # is the whole point — see the docstring.
        # De-duplicate while preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for entry in met:
            if entry not in seen:
                seen.add(entry)
                out.append(entry)
        return out

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
                "line": int(entry.get("line") or 0),
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
        for exec_result in self.history.values():
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
                         state: dict[str, Any]) -> str:
        """What this node is being asked to do.

        Built from the node's identity and its declared outputs, plus the run's own goal. The goal
        matters on the first node: it has no upstream artifacts to inherit the task from, and the
        prompt's own intake block tells the model "the task statement is the input" — so omitting the
        goal here leaves the entry node with literally nothing to do, and a competent model correctly
        replies that it received no input. Downstream nodes see the goal restated for the same reason:
        it is cheap, and it keeps every node anchored to what was actually asked for.
        """
        outputs = ", ".join(node.get("outputs") or []) or "the node's declared outputs"
        inputs = ", ".join(node.get("inputs") or []) or "everything produced so far"
        role = "Review" if self._is_reviewer(skill, node) else "Produce"
        goal = str(self._manifest(state).get("description") or "").strip()
        statement = (
            f"The goal for this run is: {goal}\n\n" if goal else ""
        )
        return (
            f"{statement}"
            f"{role} as node `{node_id}` using the {skill} skill. "
            f"Consume: {inputs}. Produce: {outputs}. "
            f"Work to the completion criteria and checklist below, and report every checklist id "
            f"with its evidence."
        )

    def _open_questions(self) -> list[dict[str, Any]]:
        """Open questions accumulated, for a rotation handoff."""
        out: list[dict[str, Any]] = []
        for exec_result in self.history.values():
            out.extend(getattr(exec_result, "open_questions", []) or [])
        return out[:3]

    def _ledger_decisions(self) -> list[dict[str, Any]]:
        """Decisions recorded so far, for a rotation handoff.

        Only what the nodes reported, since the executor does not own the ledger — the orchestrator
        does. Passing them keeps a rotated session consistent with the run's decision history.
        """
        out: list[dict[str, Any]] = []
        for exec_result in self.history.values():
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
            attempts=int(trailer.get("attempt") or 1),
            tokens=(usage.get("tokens_in") or 0) + (usage.get("tokens_out") or 0),
            cost_usd=usage.get("cost_usd"),
            artifacts=list(artifacts),
            findings=list(result.get("findings") or []),
            criteria_met=list(result.get("criteria_met") or []),
            rotated=int(prepared.get("rotated") or 0),
            compacted=int(prepared.get("compacted") or 0),
        )
        self.history[node_id] = executed
        # An open_questions / decisions list is not carried on ExecutedNode; attach for the handoff.
        executed.open_questions = list(trailer.get("open_questions") or [])
        executed.decisions = list(trailer.get("decisions") or [])
        if not executed.findings:
            self.producers[node_id] = agent.id

    def record_review(self, node_id: str, findings: list[dict[str, Any]]) -> None:
        """Record a rejection so the next developer pass receives it.

        Called by the orchestrator after writing `review_feedback.json`, so the executor's in-memory
        view and the on-disk dossier agree.
        """
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
        return {
            node_id: executed.as_dict() for node_id, executed in sorted(self.history.items())
        }

    def session_report(self) -> list[dict[str, Any]]:
        """Every live session, with its saturation and rotation count."""
        with self.ctx.lock:
            return [session.as_dict(include_turns=False)
                    for session in self.ctx.sessions.values()]


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
