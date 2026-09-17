#!/usr/bin/env python3
"""orchestrator.py — the run lifecycle: plan, approve, execute, gate, resume.

WHY THIS EXISTS
---------------
Everything below this module is a component: the gateway makes calls, the executor runs nodes, the host
supervises a subprocess, the router chooses agents, policy decides autonomy. This module is the one
that *sequences* them into a run a person can reason about — and it is where the Owner's authority
lives.

Four responsibilities, and they are the four things a user actually does:

1. **Prepare a run.** Plan a goal into a graph, bind it to the roster, report the staffing gaps, and
   present it for approval. Nothing executes from a goal alone.
2. **Drive it.** Hand the approved graph to the runner and supervise it.
3. **Hold it.** When policy says `confirm`, or a gate is reached, or the loop exhausts, the run stops
   and waits for a decision rather than proceeding.
4. **Resume it.** A crash, a pause or an Owner decision all continue from the checkpoint.

DESIGN
------
- **State transitions are explicit and persisted.** Every phase change writes the checkpoint, so a
  killed process resumes at the transition rather than at the start.
- **The human gate is a first-class state, not an error.** A run at a gate is *successful* up to that
  point; reporting it as a failure would make the normal path look broken.
- **Policy is consulted before every consequential step**, and the deciding layer is recorded, so "why
  did it act without asking?" is answerable.
- **An Owner command is a normal command.** Approve, reject, instruct, reassign and abort all go
  through the same path as any other, so each is audited identically.

Usage:
    orch = Orchestrator(config=cfg, library=lib, workspace=ws)
    run = orch.prepare("Build a booking API with auth", slug="booking")
    run = orch.approve(run)                  # then execute
    orch.instruct(run, "prefer no new dependencies")
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from .artifacts import ArtifactStore
from .config import Config
from .diagnostics import Diagnostics
from .gateway import BudgetExceeded, Gateway
from .goal import Goal, GoalState
from .host import HostError, RunnerHost, RunOutcome, RunnerState
from .idempotency import EffectJournal
from .memory import MemoryEntry, MemoryStore, memory_entry_from_state
from .org import (
    AgentState,
    Binder,
    Handoff,
    HiringDesk,
    Ledger,
    LedgerError,
    Org,
    PolicyResolver,
    RouteClass,
    Router,
    RouterError,
    default_company,
)
from .org.router import RouteContext
from .planner import Plan, Planner, PlanError, emit_safe_yaml
from .protocol import EventType
from .state import Workspace
from .telemetry import SpanExporter
from .versioning import Registry, default_registry

__all__ = ["Orchestrator", "OrchestratorError", "Run", "RunPhase", "GateRequest"]


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"



class OrchestratorError(RuntimeError):
    """Raised when a run cannot proceed: an unusable plan, an unstaffed node, or a refused command."""


class RunPhase(str, Enum):
    """Where a run is.

    The gate phases are terminal-looking but resumable: a run parks there and continues on a decision.
    Modelling them as phases rather than as exceptions is what lets the UI show a paused run as a
    normal state rather than as a failure.
    """

    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    READY = "ready"
    RUNNING = "running"
    AWAITING_GATE = "awaiting_gate"        # a human gate, or policy asking for confirmation
    AWAITING_HUMAN = "awaiting_human"      # escalation: exhaustion or a critical finding
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"

    @property
    def terminal(self) -> bool:
        return self in (RunPhase.DONE, RunPhase.FAILED, RunPhase.ABORTED)

    @property
    def waiting(self) -> bool:
        """Whether the run is waiting on a person."""
        return self in (RunPhase.AWAITING_APPROVAL, RunPhase.AWAITING_GATE, RunPhase.AWAITING_HUMAN,
                        RunPhase.PAUSED)


@dataclass
class GateRequest:
    """A decision the Owner must make before the run continues."""

    gate_id: str
    kind: str                       # human | agent | policy
    reason: str
    requires: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)
    dossier: dict[str, Any] = field(default_factory=dict)
    asked_at: str = ""

    def __post_init__(self) -> None:
        if not self.asked_at:
            self.asked_at = _iso_now()

    def as_dict(self) -> dict[str, Any]:
        return {"gate_id": self.gate_id, "kind": self.kind, "reason": self.reason,
                "requires": list(self.requires), "present": list(self.present),
                "dossier": self.dossier, "asked_at": self.asked_at}


@dataclass
class Run:
    """One project execution, and everything needed to resume it."""

    run_id: str
    slug: str
    goal: str
    workspace: Workspace
    phase: RunPhase = RunPhase.PLANNING
    plan: Plan | None = None
    manifest_path: Path | None = None
    org: Org | None = None
    ledger: Ledger | None = None
    policy: PolicyResolver | None = None
    bindings: dict[str, Any] = field(default_factory=dict)
    staffing_gaps: list[dict[str, Any]] = field(default_factory=list)
    gate: GateRequest | None = None
    outcome: dict[str, Any] = field(default_factory=dict)
    # Owner input that must reach the nodes: instructions and non-negotiable constraints.
    instructions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    # The delegation approval queue, and the decisions taken.
    pending_requisitions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = field(default_factory=_iso_now)
    updated_at: str = field(default_factory=_iso_now)
    run_phase_version: str = "1.0.0"

    def touch(self) -> None:
        self.updated_at = _iso_now()

    @property
    def waiting(self) -> bool:
        return self.phase.waiting

    def as_dict(self) -> dict[str, Any]:
        """Serialisable state, for the checkpoint and for the UI."""
        return {
            "run_phase_version": self.run_phase_version,
            "run_id": self.run_id, "slug": self.slug, "goal": self.goal,
            "phase": self.phase.value,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "plan": self.plan.as_dict() if self.plan else None,
            "bindings": {k: (v.as_dict() if hasattr(v, "as_dict") else v)
                         for k, v in self.bindings.items()},
            "staffing_gaps": self.staffing_gaps,
            "gate": self.gate.as_dict() if self.gate else None,
            "outcome": self.outcome,
            "instructions": list(self.instructions),
            "constraints": list(self.constraints),
            "pending_requisitions": self.pending_requisitions,
            "decisions": self.decisions,
            "started_at": self.started_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, workspace: Workspace) -> "Run":
        """Rebuild a run from its checkpoint, tolerating unknown fields."""
        try:
            phase = RunPhase(str(data.get("phase") or "planning"))
        except ValueError:
            phase = RunPhase.PLANNING
        manifest_path = data.get("manifest_path")
        run = cls(
            run_id=str(data.get("run_id") or ""),
            slug=str(data.get("slug") or workspace.slug),
            goal=str(data.get("goal") or ""),
            workspace=workspace,
            phase=phase,
            manifest_path=Path(manifest_path) if manifest_path else None,
            staffing_gaps=list(data.get("staffing_gaps") or []),
            instructions=[str(i) for i in (data.get("instructions") or [])],
            constraints=[str(c) for c in (data.get("constraints") or [])],
            pending_requisitions=list(data.get("pending_requisitions") or []),
            decisions=list(data.get("decisions") or []),
            outcome=data.get("outcome") if isinstance(data.get("outcome"), dict) else {},
            started_at=str(data.get("started_at") or _iso_now()),
            updated_at=str(data.get("updated_at") or _iso_now()),
        )
        gate = data.get("gate")
        if isinstance(gate, dict):
            run.gate = GateRequest(
                gate_id=str(gate.get("gate_id") or ""), kind=str(gate.get("kind") or "human"),
                reason=str(gate.get("reason") or ""),
                requires=list(gate.get("requires") or []), present=list(gate.get("present") or []),
                dossier=gate.get("dossier") if isinstance(gate.get("dossier"), dict) else {},
                asked_at=str(gate.get("asked_at") or ""),
            )
        return run


class Orchestrator:
    """Sequences a run and holds the Owner's authority.

    Parameters
    ----------
    config:
        The validated configuration.
    library:
        The pinned Skills library.
    workspace:
        The project workspace: manifests, artifacts, run-state, trace.
    bus:
        Optional event bus, so the UI sees every transition.
    """

    def __init__(self, *, config: Config, library: Any, workspace: Workspace, bus: Any = None,
                 org: Org | None = None, diagnostics: Diagnostics | None = None) -> None:
        self.config = config
        self.library = library
        self.workspace = workspace
        self.bus = bus
        self.workspace.ensure()
        self.source = _skill_source(library, project=getattr(workspace, "root", None))
        self.planner = Planner(self.source, config=config)
        self.diagnostics = diagnostics or Diagnostics(
            run_id="", state_dir=workspace.state_dir)
        self.registry: Registry = default_registry(dict(config.schemas or {}))
        self.org = org or default_company(
            provider=config.defaults.get("provider", "ollama"),
            model=config.defaults.get("model", "qwen2.5-coder:7b"),
            context_window=self._default_window(),
        )
        self.store = ArtifactStore(workspace_root=workspace.path)
        self.memory = MemoryStore(workspace.state_dir / "memory")
        self.telemetry = SpanExporter(path=workspace.spans_path, run_id="")
        self.journal = EffectJournal(path=workspace.effects_path)
        self.ledger = Ledger(path=workspace.state_dir / "ledger.jsonl")
        self.policy = PolicyResolver.from_dict(
            {"defaults": (config.policy.default_autonomy or {}),
             "allow_autonomous_escalation": config.policy.allow_autonomous_escalation})
        self.router = Router(self.org, policy=self.policy)
        self.router.attach_skills(self.source)
        self.binder = Binder(self.org)
        self.desk = HiringDesk(
            self.org, max_depth=int(config.delegation.max_depth),
            span_of_control=int(config.delegation.span_of_control),
            allow_ephemeral=bool(config.delegation.allow_ephemeral),
            budget_share_max=float(config.delegation.budget_share_max),
            approval_tiers=dict(config.delegation.approval_tiers or {}),
        )
        self._run: Run | None = None
        self._host: RunnerHost | None = None
        self._lock = threading.RLock()
        #: The active goal for this workspace, when one has been set. Loaded **disarmed** — see
        #: `goal.Goal.load` — so nothing continues until an explicit `goal_resume`.
        self._goal: Goal | None = Goal.load(workspace)

    # ── preparing a run ─────────────────────────────────────────────────────

    def prepare(self, goal: str, *, slug: str | None = None,
                max_iterations: int = 3) -> Run:
        """Plan the goal, bind it to the roster, and present it for approval.

        Nothing executes here. The Owner approves a *graph*, and the graph is shown with its staffing
        gaps before it can be approved — because a plan needing a capability nobody holds would
        otherwise stop three nodes in, far from the cause.

        Raises
        ------
        OrchestratorError
            When the goal cannot be planned at all.
        """
        try:
            plan = self.planner.plan(goal, slug=slug, max_iterations=max_iterations)
        except PlanError as exc:
            raise OrchestratorError(f"cannot plan this goal: {exc}") from exc

        project = Workspace.for_project(plan.slug, root=self.workspace.root)
        project.ensure()
        manifest_path = project.path / f"{plan.slug}.yaml"
        manifest_path.write_text(emit_safe_yaml(plan.manifest), encoding="utf-8")

        run = Run(
            run_id=f"run_{int(time.time())}_{plan.slug}",
            slug=plan.slug, goal=goal, workspace=project,
            phase=RunPhase.AWAITING_APPROVAL, plan=plan, manifest_path=manifest_path,
            org=self.org, ledger=self.ledger, policy=self.policy,
        )
        run.staffing_gaps = self.binder.staffing_gaps(plan.manifest)
        try:
            run.bindings = self.binder.plan_bindings(plan.manifest, skip_unstaffed=True)
        except Exception as exc:  # noqa: BLE001 - a binding failure is reported, not fatal
            self.diagnostics.warning("run.bind.failed", message=str(exc))

        self._run = run
        self._persist(run)
        self._emit(EventType.MANIFEST_PROPOSED, {
            "run_id": run.run_id, "slug": run.slug, "goal": goal,
            "validated": plan.validation.valid,
            "nodes": plan.node_ids(),
            "loops": [{"id": l.get("id"), "max_iterations": l.get("max_iterations")}
                      for l in plan.loops],
            "gates": [g.get("id") for g in plan.gates],
            "staffing_gaps": run.staffing_gaps,
        })
        self.diagnostics.info("run.prepared", message=f"plan ready for approval: {run.slug}",
                              detail={"nodes": len(plan.nodes), "gaps": len(run.staffing_gaps)})
        return run

    def approve(self, run: Run | None = None) -> Run:
        """Approve the graph and mark the run ready to execute.

        Works for both an authored plan and an adopted manifest: what is being approved is the *graph*,
        and that is the manifest on disk in either case.

        Raises
        ------
        OrchestratorError
            When there is nothing to approve, or the graph did not validate. Approving an invalid graph
            would hand the runner something it will reject, which is a worse failure than refusing.
        """
        run = self._resolve(run)
        if run.manifest_path is None:
            raise OrchestratorError("there is no plan to approve; call prepare or adopt first")

        if run.plan is not None:
            if not run.plan.validation.valid:
                raise OrchestratorError(
                    f"refusing to approve an invalid graph: {run.plan.validation.errors}"
                )
            nodes = run.plan.node_ids()
        else:
            # An adopted manifest: validate it here, since it never went through the planner.
            validation = self.planner.validate(self._read_manifest(run.manifest_path))
            if not validation.valid:
                raise OrchestratorError(
                    f"refusing to approve an invalid manifest: {validation.errors}"
                )
            nodes = [str(n) for n in (run.outcome.get("nodes") or [])]

        run.phase = RunPhase.READY
        run.touch()
        self._persist(run)
        self._emit(EventType.MANIFEST_APPROVED, {"run_id": run.run_id, "slug": run.slug,
                                                 "nodes": nodes})
        self.diagnostics.info("run.approved", message=f"{run.slug} approved for execution")
        return run

    def adopt(self, manifest_path: os.PathLike | str, *, goal: str = "",
              slug: str | None = None) -> Run:
        """Adopt an existing manifest as a run, rather than planning one from a goal.

        Distinct from :meth:`prepare` on purpose. `prepare` authors a graph from a goal; this runs one
        that already exists — a manifest a person edited, or one generated earlier. The two must not be
        the same method, or `prepare` would silently overwrite a hand-written graph.

        Validation still runs, and binding still runs: an adopted manifest gets the same checks as a
        planned one, because "I wrote it by hand" is not evidence that it is executable.

        Raises
        ------
        OrchestratorError
            When the manifest is missing or does not validate.
        """
        source = Path(manifest_path)
        if not source.is_file():
            raise OrchestratorError(f"no manifest at {source}")

        validation = self.planner.validate(self._read_manifest(source))
        if not validation.valid:
            raise OrchestratorError(
                f"refusing to adopt an invalid manifest: {validation.errors}"
            )

        project_slug = slug or source.stem
        project = Workspace.for_project(project_slug, root=self.workspace.root)
        project.ensure()
        target = project.path / f"{project_slug}.yaml"

        # The library's validator requires the filename to equal the manifest's `name`, so adopting
        # under a different slug must rename it *inside* the file too — otherwise the runner rejects it
        # with "filename must equal name + .yaml" and the cause is not obvious from the run output.
        manifest = self._read_manifest(source)
        if str(manifest.get("name") or "") != project_slug:
            manifest["name"] = project_slug
        # Re-emit through the Safe YAML emitter rather than copying the bytes, so the rename is applied
        # in the subset the runner parses.
        target.write_text(emit_safe_yaml(manifest), encoding="utf-8")
        run = Run(
            run_id=f"run_{int(time.time())}_{project_slug}",
            slug=project_slug, goal=goal or f"adopted manifest {source.name}",
            workspace=project, phase=RunPhase.AWAITING_APPROVAL,
            manifest_path=target, org=self.org, ledger=self.ledger, policy=self.policy,
        )
        run.staffing_gaps = self.binder.staffing_gaps(manifest)
        try:
            run.bindings = self.binder.plan_bindings(manifest, skip_unstaffed=True)
        except Exception as exc:  # noqa: BLE001
            self.diagnostics.warning("run.bind.failed", message=str(exc))
        run.outcome = {"nodes": [str(n.get("id")) for n in manifest.get("nodes") or []]}

        self._run = run
        self._persist(run)
        self._emit(EventType.MANIFEST_PROPOSED, {
            "run_id": run.run_id, "slug": project_slug, "adopted": True,
            "nodes": [str(n.get("id")) for n in manifest.get("nodes") or []],
            "gates": [str(g.get("id")) for g in manifest.get("gates") or []],
            "staffing_gaps": run.staffing_gaps,
        })
        self.diagnostics.info("run.adopted",
                              message=f"adopted {source.name} as {project_slug}",
                              detail={"nodes": len(manifest.get("nodes") or [])})
        return run

    def _read_manifest(self, path: Path) -> dict[str, Any]:
        """Read a manifest in the library's Safe YAML Subset."""
        try:
            import importlib.util
            import sys as _sys

            scripts = Path(self.library.files.root) / "scripts"
            if str(scripts) not in _sys.path:
                _sys.path.insert(0, str(scripts))
            spec = importlib.util.spec_from_file_location(
                "_agentorg_safe_yaml_read", scripts / "lib" / "safe_yaml.py")
            if spec is None or spec.loader is None:
                raise OrchestratorError("the library's safe YAML parser is unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.parse(path.read_text(encoding="utf-8")) or {}
        except OrchestratorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OrchestratorError(f"cannot read the manifest at {path}: {exc}") from exc

    # ── executing ───────────────────────────────────────────────────────────

    def execute(self, run: Run | None = None, *,
                on_event: Callable[[str, dict[str, Any]], None] | None = None,
                executor: os.PathLike | str | None = None) -> RunOutcome:
        """Run the approved graph, blocking until it finishes, gates or is killed.

        Raises
        ------
        OrchestratorError
            When the run is not ready to execute. Executing an unapproved graph would bypass the
            approval the design requires.
        """
        run = self._resolve(run)
        if run.phase not in (RunPhase.READY, RunPhase.PAUSED, RunPhase.RUNNING):
            raise OrchestratorError(
                f"the run is {run.phase.value}; only a ready, paused or running run executes. "
                "Call approve first."
            )
        if run.manifest_path is None:
            raise OrchestratorError("the run has no manifest to execute")

        run.phase = RunPhase.RUNNING
        run.touch()
        self._persist(run)
        self._emit(EventType.RUN_START, {"run_id": run.run_id, "slug": run.slug})

        host = RunnerHost(
            config=self.config, library=self.library, workspace=run.workspace,
            on_event=self._host_event(run, on_event),
            on_stderr=lambda line: self._emit(EventType.AGENT_LOG, {
                "run_id": run.run_id, "stream": "stderr", "text": line[:500]}),
            heartbeat_s=float(self.config.concurrency.heartbeat_s),
            grace_s=float(self.config.concurrency.grace_s),
        )
        with self._lock:
            self._host = host

        # Hand the executing process everything the orchestrator already decided. Without this the
        # subprocess rebuilds the built-in company, the pinned library and no bindings — so a hire,
        # an authored skill and a chosen agent would all be discarded at the boundary.
        self._write_run_context(run)

        try:
            extra = ["--executor", str(executor)] if executor else []
            outcome = self._run_with_goal(run, extra=extra)
        except HostError as exc:
            run.phase = RunPhase.FAILED
            run.outcome = {"outcome": "failed", "error": str(exc)}
            run.touch()
            self._persist(run)
            self._emit(EventType.ERROR, {"run_id": run.run_id, "message": str(exc),
                                         "retryable": False})
            raise OrchestratorError(str(exc)) from exc
        finally:
            with self._lock:
                self._host = None

        self._settle(run, outcome)
        return outcome

    def _run_with_goal(self, run: Run, *, extra: list[str]) -> RunOutcome:
        """Execute the graph, and — when a goal is armed — keep going after it finishes.

        This is the idle driver from the design, and it is deliberately small because the *rule* is
        small: a Goal continues past a model final and past ordinary node completion, but it does not
        decide a question the Owner asked to decide.

        The loop terminates on any of:
          - the agent's own `update_goal(complete|blocked)` verdict,
          - a human/policy gate (the run parks; a Goal respects gates),
          - a configured budget being reached (pauses with reason `budget_spend`),
          - the run failing or being killed,
          - the goal being paused/cleared underneath us.
        """
        goal = self._goal
        outcome = self._host.run(manifest_path=run.manifest_path, run_id=run.run_id,
                                 workflow=run.slug, project=run.slug, extra_args=extra,
                                 goal_active=bool(goal is not None and goal.state.is_live))
        if goal is None or not goal.state.is_live:
            return outcome

        max_rounds = self._goal_max_rounds()
        for round_index in range(1, max_rounds + 1):
            # Fold what this round cost, so "no ceiling" never means "no idea".
            tokens, requests, cost = self._round_spend(run)
            goal.record_round(tokens=tokens, requests=requests, cost_usd=cost)

            decision = self._consume_goal_decision(run)
            if decision is not None:
                if decision.verdict == "complete":
                    goal.complete(decision.summary)
                    goal.save(run.workspace)
                    self._emit(EventType.GOAL_COMPLETED, {"objective": goal.objective,
                                                          "summary": decision.summary,
                                                          "spend": goal.spend.as_dict()})
                else:
                    goal.block(decision.summary)
                    goal.save(run.workspace)
                    self._emit(EventType.GOAL_BLOCKED, {"objective": goal.objective,
                                                        "reason": decision.summary,
                                                        "spend": goal.spend.as_dict()})
                return outcome

            # A gate still holds. Continuing past work finishing is the point; deciding for the Owner
            # is not, so a Goal parks here exactly as an ordinary run does.
            if run.gate is not None or run.phase in (RunPhase.AWAITING_GATE, RunPhase.AWAITING_HUMAN):
                goal.pause(reason="gate")
                goal.save(run.workspace)
                self._emit(EventType.GOAL_PAUSED, {"objective": goal.objective, "reason": "gate",
                                                   "spend": goal.spend.as_dict()})
                return outcome

            if goal.budget_reached():
                goal.pause(reason="budget_spend")
                goal.save(run.workspace)
                self._emit(EventType.GOAL_PAUSED, {"objective": goal.objective,
                                                   "reason": "budget_spend",
                                                   "spend": goal.spend.as_dict()})
                return outcome

            if outcome.killed or run.phase in (RunPhase.ABORTED, RunPhase.FAILED):
                return outcome

            goal.save(run.workspace)
            self._emit(EventType.GOAL_PROGRESS, {"objective": goal.objective, "round": round_index,
                                                 "spend": goal.spend.as_dict()})
            outcome = self._host.resume_run(
                manifest_path=run.manifest_path, run_id=run.run_id, workflow=run.slug,
                project=run.slug, goal_active=True)
            self._settle(run, outcome)
        return outcome

    def _round_spend(self, run: Run) -> tuple[int, int, float]:
        """The tokens, requests and cost this round's ledger recorded.

        Read from the ledger rather than estimated: an accounting figure the engine invented would be
        worse than no figure, because it would be believed.
        """
        try:
            snapshot = self.ledger.snapshot()
        except Exception:  # noqa: BLE001 - a missing ledger must not stop the loop
            return 0, 0, 0.0
        nodes = int(snapshot.get("nodes") or 0)
        runs = int(snapshot.get("runs") or 0)
        return int(snapshot.get("tokens") or 0), nodes or runs, float(snapshot.get("cost_usd") or 0.0)

    def _consume_goal_decision(self, run: Run) -> Any:
        """Read (and remove) the agent's verdict, written by `update_goal` in the subprocess."""
        from .goal import GoalDecision

        try:
            return GoalDecision.consume(run.workspace)
        except Exception:  # noqa: BLE001 - an unreadable decision is not a crash
            return None

    def _goal_max_rounds(self) -> int:
        """A hard stop on the *loop itself*, separate from the token budget.

        The token budget is opt-in (0 = off). This is the unconditional bound that keeps a bug in the
        loop — a gate that never settles, a decision never written — from spinning forever. It is high
        enough not to be the practical limit and low enough to be a backstop.
        """
        raw = getattr(getattr(self.config, "goal", None), "max_rounds", None)
        return max(1, int(raw)) if raw else 10_000

    # ── the goal API ────────────────────────────────────────────────────────

    def goal(self) -> Goal | None:
        """The active goal, or None when this workspace has none."""
        return self._goal

    def goal_status(self) -> dict[str, Any]:
        """The goal picture for the console and the CLI."""
        if self._goal is None:
            return {"objective": "", "state": GoalState.CLEARED.value, "live": False, "open": False,
                    "budget_enabled": False, "token_budget": 0, "pause_reason": "",
                    "summary": "", "blocked_reason": "", "slice": {}, "spend": {}, "history": []}
        return self._goal.public()

    def goal_set(self, objective: str, *, armed: bool = True, by: str = "cli") -> Goal:
        """Set the objective, and optionally arm it immediately.

        Arming is the deliberate act that begins spending, so `goal set` on an already-open objective
        replaces it only when asked — otherwise an in-flight objective would be silently overwritten.
        """
        existing = self._goal
        if existing is not None and existing.state.is_open and existing.objective == objective.strip():
            goal = existing
        else:
            budget = getattr(getattr(self.config, "goal", None), "token_budget", 0)
            goal = Goal.new(objective, token_budget=int(budget or 0))
            if existing is not None:
                goal.history = list(existing.history)[-20:]
        self._goal = goal
        if armed:
            goal.arm(by=by)
            self._emit(EventType.GOAL_ARMED, {"objective": goal.objective, "by": by,
                                              "token_budget": goal.token_budget})
        goal.save(self.workspace)
        return goal

    def goal_pause(self) -> Goal:
        """Stop the loop, keeping the objective."""
        goal = self._require_goal()
        goal.pause(reason="manual")
        goal.save(self.workspace)
        self._emit(EventType.GOAL_PAUSED, {"objective": goal.objective, "reason": "manual",
                                           "spend": goal.spend.as_dict()})
        return goal

    def goal_resume(self, *, by: str = "cli") -> Goal:
        """Continue, granting a **fresh slice** while keeping the cumulative statistics.

        Per-slice bounding is what makes a budget enforceable without losing sight of the total: each
        resume is an explicit act, and the spend across all of them stays visible.
        """
        goal = self._require_goal()
        goal.arm(by=by)
        goal.save(self.workspace)
        self._emit(EventType.GOAL_RESUMED, {"objective": goal.objective, "by": by,
                                            "spend": goal.spend.as_dict(),
                                            "token_budget": goal.token_budget})
        return goal

    def goal_clear(self) -> Goal:
        """Forget the objective, keeping the history."""
        goal = self._require_goal()
        goal.clear()
        goal.save(self.workspace)
        self._emit(EventType.GOAL_CLEARED, {"spend": goal.spend.as_dict()})
        return goal

    def _require_goal(self) -> Goal:
        if self._goal is None:
            raise OrchestratorError("no goal is set for this workspace; use `goal set` first")
        return self._goal

    def _write_run_context(self, run: Run) -> None:
        """Persist the roster, bindings and skill roots for the executing subprocess.

        Written beside the manifest so the generated plugin can find it from the manifest's own
        directory. A failure here is logged and tolerated: the plugin falls back to the built-in
        company, which runs — it simply runs the run as it was before this handoff existed.
        """
        from . import runcontext

        try:
            # The skill roots are taken from the same overlay the planner used, so a skill that
            # planned can also execute.
            roots: list[str] = []
            source = getattr(self, "source", None)
            if source is not None and hasattr(source, "roots"):
                roots = [str(p) for p in source.roots()]
            context = runcontext.RunContext(
                org=self.org.to_dict() if self.org is not None else {},
                bindings={
                    node_id: (binding.as_dict() if hasattr(binding, "as_dict")
                              else dict(binding or {}))
                    for node_id, binding in (run.bindings or {}).items()
                },
                skill_roots=roots,
                instructions=list(run.instructions or []),
                constraints=[str(c) for c in (run.constraints or [])],
                # The armed goal travels too, so the executing process advertises `update_goal` — and
                # knows which objective "complete" refers to.
                goal_active=bool(self._goal is not None and self._goal.state.is_live),
                goal_objective=(self._goal.objective if self._goal is not None else ""),
            )
            runcontext.write(run.workspace.path, context)
        except Exception as exc:  # noqa: BLE001 - the run must still start
            self._emit(EventType.ERROR, {
                "run_id": run.run_id, "retryable": False,
                "message": f"could not write the run context, so the executing process will use the "
                           f"built-in roster: {exc}"})

    def _settle(self, run: Run, outcome: RunOutcome) -> None:
        """Turn a runner outcome into a run phase, and record what it produced."""
        state = outcome.summary or {}
        run.outcome = outcome.as_dict()
        run.outcome.update({
            "nodes": {name: {"status": rec.get("status"), "verdict": rec.get("verdict")}
                      for name, rec in (state.get("nodes") or {}).items()},
            "artifacts": sorted((state.get("artifacts") or {}).keys()),
            "open_questions": state.get("open_questions") or [],
        })

        gate = self._detect_gate(state)
        if gate is not None:
            run.gate = gate
            run.phase = RunPhase.AWAITING_GATE if gate.kind == "human" else RunPhase.AWAITING_HUMAN
        elif outcome.killed:
            run.phase = RunPhase.ABORTED
        elif outcome.ok:
            run.phase = RunPhase.DONE
            self._write_memory(run, state)
        else:
            # A runner failure that reached a gate is a parked run, not a broken one; anything else is
            # a failure with the reason preserved.
            escalated = str(state.get("phase") or "") in ("escalated", "awaiting_human")
            run.phase = RunPhase.AWAITING_HUMAN if escalated else RunPhase.FAILED

        run.touch()
        self._persist(run)
        self._emit(EventType.RUN_END, run.outcome)
        self._log_phase(run)

    def _detect_gate(self, state: dict[str, Any]) -> GateRequest | None:
        """Find a gate the run reached and is waiting on."""
        for name, record in (state.get("nodes") or {}).items():
            if not isinstance(record, dict):
                continue
            verdict = str(record.get("verdict") or "")
            if verdict == "awaiting_owner" or record.get("status") == "needs_review":
                declaration = self._gate_declaration(name)
                return GateRequest(
                    gate_id=name, kind=str((declaration or {}).get("kind") or "human"),
                    reason=str((declaration or {}).get("description")
                               or record.get("summary") or "the run reached a gate"),
                    requires=[str(r) for r in ((declaration or {}).get("requires") or [])],
                    present=sorted((state.get("artifacts") or {}).keys()),
                    dossier={"node": name, "summary": record.get("summary"),
                             "attempts": record.get("iterations")},
                )
        if str(state.get("phase") or "") in ("escalated", "awaiting_human"):
            return GateRequest(
                gate_id="escalation", kind="policy",
                reason=str(state.get("outcome") or "the run escalated and needs a decision"),
                present=sorted((state.get("artifacts") or {}).keys()),
                dossier={"outcome": state.get("outcome"), "log_tail": (state.get("log") or [])[-5:]},
            )
        return None

    def _gate_declaration(self, node_id: str) -> dict[str, Any] | None:
        """The manifest's declaration for a gate node."""
        run = self._run
        if run is None or run.manifest_path is None or not run.manifest_path.is_file():
            return None
        try:
            text = run.manifest_path.read_text(encoding="utf-8")
            import importlib.util
            import sys as _sys

            scripts = Path(self.library.files.root) / "scripts"
            if str(scripts) not in _sys.path:
                _sys.path.insert(0, str(scripts))
            spec = importlib.util.spec_from_file_location(
                "_agentorg_safe_yaml_orch", scripts / "lib" / "safe_yaml.py")
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                manifest = module.parse(text) or {}
                for gate in manifest.get("gates") or []:
                    if str(gate.get("id")) == node_id:
                        return gate
        except Exception:  # noqa: BLE001 - a missing declaration is not fatal
            return None
        return None

    # ── Owner commands ──────────────────────────────────────────────────────

    def decide(self, approved: bool, *, run: Run | None = None, note: str = "") -> Run:
        """Resolve a gate: approve and continue, or reject and park.

        A rejection requires no reason from the Owner but records one if given, because a rejection the
        agents cannot read is one they will re-attempt identically.
        """
        run = self._resolve(run)
        gate = run.gate
        if gate is None:
            raise OrchestratorError("there is no gate to decide; the run is not waiting on one")

        run.decisions.append({
            "gate_id": gate.gate_id, "approved": approved, "note": note,
            "by": "owner", "at": _iso_now(),
        })
        self._emit(EventType.HUMAN_DECISION, {
            "run_id": run.run_id, "gate_id": gate.gate_id, "approved": approved,
            "note": note, "kind": gate.kind})

        if approved:
            if note:
                # An approval note is guidance, so it becomes an instruction for the next nodes.
                run.instructions.append(note)
            run.gate = None
            run.phase = RunPhase.READY
            self.diagnostics.info("human.decision", message=f"approved {gate.gate_id}")
        else:
            run.phase = RunPhase.PAUSED
            if note:
                run.instructions.append(f"Owner rejected at {gate.gate_id}: {note}")
            self.diagnostics.warning("human.decision",
                                     message=f"rejected {gate.gate_id}: {note}")
        run.touch()
        self._persist(run)
        return run

    def instruct(self, text: str, *, run: Run | None = None, as_constraint: bool = False) -> Run:
        """Push guidance into a running org.

        With `as_constraint`, the text becomes a non-negotiable constraint — which the AR-04 machinery
        then preserves verbatim across every compaction and rotation for the rest of the run. That is
        the difference between "please prefer X" and "X is required", and the Owner chooses.
        """
        run = self._resolve(run)
        cleaned = (text or "").strip()
        if not cleaned:
            raise OrchestratorError("an instruction cannot be empty")
        if as_constraint:
            run.constraints.append(cleaned)
        else:
            run.instructions.append(cleaned)
        run.touch()
        self._persist(run)
        self._emit(EventType.POLICY_CHANGED, {
            "run_id": run.run_id, "instruction": cleaned, "as_constraint": as_constraint})
        self.diagnostics.info("owner.instruct",
                              message=f"{'constraint' if as_constraint else 'instruction'} added",
                              detail={"text": cleaned[:200]})
        return run

    def reassign(self, node_id: str, agent_id: str, *, run: Run | None = None) -> Run:
        """Pin a node to a different agent — the manual form of the router's job.

        Works for an adopted manifest as well as an authored plan: the node list comes from whichever
        the run has, because an Owner reassigning a node should not have to care how the graph was made.

        Raises
        ------
        OrchestratorError
            When the node is not in the run's graph, the agent does not hold the node's skill, or the
            agent is the producer of the artifact a review node would judge. The last two are refusals
            the router makes; the Owner does not bypass them.
        """
        run = self._resolve(run)
        nodes = self._nodes_of(run)
        if not nodes:
            raise OrchestratorError("this run has no graph to reassign a node in")
        node = next((n for n in nodes if str(n.get("id")) == node_id), None)
        if node is None:
            raise OrchestratorError(f"node {node_id!r} is not in this run's plan")
        try:
            agent = self.org.get(agent_id)
        except Exception as exc:  # noqa: BLE001
            raise OrchestratorError(str(exc)) from exc
        skill = str(node.get("skill") or "")
        if skill and not agent.has_skill(skill):
            raise OrchestratorError(
                f"{agent.name!r} does not hold {skill!r}, so it cannot take node {node_id!r}"
            )
        producer = self._producer_of(node_id, run)
        if producer:
            try:
                self.binder.assert_independent(agent, self.org.get(producer))
            except Exception as exc:  # noqa: BLE001 - the independence rule is not bypassable
                raise OrchestratorError(str(exc)) from exc
        run.bindings[node_id] = {"agents": [agent_id], "policy": "pinned", "pinned_id": agent_id,
                                 "reason": "reassigned by the Owner"}
        run.touch()
        self._persist(run)
        self._emit(EventType.ROUTE_OVERRIDDEN, {
            "run_id": run.run_id, "node_id": node_id, "agent_id": agent_id, "by": "owner"})
        self.diagnostics.info("owner.reassign", message=f"{node_id} -> {agent.name}")
        return run

    def _nodes_of(self, run: Run) -> list[dict[str, Any]]:
        """The run's nodes, from its plan when it was authored and its manifest when adopted."""
        if run.plan is not None:
            return list(run.plan.nodes)
        if run.manifest_path and run.manifest_path.is_file():
            return list(self._read_manifest(run.manifest_path).get("nodes") or [])
        return []

    def takeover(self, node_id: str, *, run: Run | None = None) -> Run:
        """The Owner takes over a node as a human agent.

        The Owner is an agent, so this is the same machinery as any other assignment — and the artifact
        it produces records a human producer, which is what the audit trail needs.
        """
        run = self._resolve(run)
        owner = self.org.owner()
        if owner is None:
            raise OrchestratorError("the roster has no Owner, so a takeover has no actor")
        run.decisions.append({"node_id": node_id, "action": "takeover", "by": owner.id,
                              "at": _iso_now()})
        run.touch()
        self._persist(run)
        self._emit(EventType.HUMAN_TAKEOVER, {"run_id": run.run_id, "node_id": node_id,
                                              "agent_id": owner.id})
        self.diagnostics.info("human.takeover", message=f"Owner took over {node_id}")
        return run

    def pause(self) -> bool:
        """Ask the running process to pause at the next node boundary."""
        with self._lock:
            host = self._host
        if host is None:
            return False
        accepted = host.pause()
        if accepted and self._run is not None:
            self._run.phase = RunPhase.PAUSED
            self._persist(self._run)
        return accepted

    def resume(self, run: Run | None = None) -> Run:
        """Resume a paused run, continuing from its checkpoint."""
        run = self._resolve(run)
        with self._lock:
            host = self._host
        if host is not None:
            host.resume()
        run.phase = RunPhase.READY
        run.gate = None
        run.touch()
        self._persist(run)
        self._emit(EventType.RUN_RESUMED, {"run_id": run.run_id, "from_checkpoint": True})
        return run

    def abort(self, run: Run | None = None) -> Run:
        """Stop a run, keeping its checkpoint so it can be resumed or inspected."""
        run = self._resolve(run)
        with self._lock:
            host = self._host
        if host is not None:
            host.abort()
        run.phase = RunPhase.ABORTED
        run.touch()
        self._persist(run)
        self._emit(EventType.RUN_ABORTED, {"run_id": run.run_id, "checkpoint_kept": True})
        return run

    # ── the hiring desk ─────────────────────────────────────────────────────

    def requisition(self, request: Any, *, parent_tokens: int = 1_000_000) -> dict[str, Any]:
        """Evaluate a hire request through the desk, queueing anything needing the Owner.

        An auto-approved helper is hired immediately; a specialist or a privileged hire becomes a
        pending decision the Owner sees with its full justification.
        """
        from .org.agent import Budget

        try:
            outcome = self.desk.evaluate(
                request, active_chain=list(self._active_chain()),
                parent_budget=Budget(allocated_tokens=parent_tokens),
            )
        except Exception as exc:  # noqa: BLE001 - a refused hire is a result, not a crash
            self._emit(EventType.AGENT_SPAWN_DENIED, {
                "run_id": self._run.run_id if self._run else "", "reason": str(exc)})
            return {"approved": False, "needs_owner": False, "reason": str(exc)}

        if outcome.needs_owner and self._run is not None:
            self._run.pending_requisitions.append(request.as_dict())
            self._persist(self._run)
        self._emit(EventType.AGENT_SPAWN_APPROVED if outcome.approved
                   else (EventType.AGENT_SPAWN_REQUESTED if outcome.needs_owner
                         else EventType.AGENT_SPAWN_DENIED), {
            "run_id": self._run.run_id if self._run else "",
            "tier": outcome.tier.value, "agent_id": outcome.agent_id,
            "reason": outcome.reason,
            "requisition": request.as_dict() if outcome.needs_owner else None,
        })
        return outcome.as_dict()

    # ── resuming ────────────────────────────────────────────────────────────

    def load(self, slug: str | None = None) -> Run | None:
        """Load a run from its checkpoint, or None when there is nothing to resume.

        Returns the phase the run was in, so the UI can show a parked run as parked rather than
        presenting it as finished.
        """
        if slug is None:
            project = self.workspace
        else:
            # Resolve the slug under *this orchestrator's* root, not the default projects directory —
            # otherwise a run in a custom root would appear not to exist.
            project = (self.workspace if slug == self.workspace.slug
                       else Workspace.for_project(slug, root=self.workspace.root))
        raw = project.read_checkpoint_raw()
        if raw is None:
            return None
        try:
            self.registry.check(raw, "run_state")
        except Exception as exc:  # noqa: BLE001 - a newer schema is refused, not guessed at
            self._emit(EventType.SCHEMA_REFUSED, {"slug": project.slug, "reason": str(exc)})
            raise OrchestratorError(f"cannot open this run: {exc}") from exc
        run = Run.from_dict(raw, workspace=project)
        run.org = self.org
        run.ledger = self.ledger
        run.policy = self.policy
        self._run = run
        self.diagnostics.info("run.loaded",
                              message=f"resumed {run.slug} at {run.phase.value}")
        return run

    def status(self) -> dict[str, Any]:
        """Everything the UI needs about the current run, in one call."""
        run = self._run
        if run is None:
            return {"phase": "idle", "running": False, "goal": self.goal_status()}
        with self._lock:
            host = self._host
        return {
            **run.as_dict(),
            "running": host is not None and host.running,
            "liveness": host.wedged() if host is not None else None,
            "org": self.org.roster_view(),
            "policy": self.policy.effective(),
            "cost": self.telemetry.sli_rollup(),
            # The goal travels with status, so the console shows whether the loop will continue
            # without needing a second command it has to remember to send.
            "goal": self.goal_status(),
        }

    # ── internals ───────────────────────────────────────────────────────────

    def _resolve(self, run: Run | None) -> Run:
        """The run to act on, preferring an explicit one."""
        resolved = run or self._run
        if resolved is None:
            raise OrchestratorError("there is no run; call prepare first")
        return resolved

    def _persist(self, run: Run) -> None:
        """Write the checkpoint atomically, so a crash resumes at the last transition."""
        data = run.as_dict()
        data["org"] = self.org.to_dict()
        data["ledger"] = [e.as_dict() for e in self.ledger.all()]
        self.registry.save(run.workspace.checkpoint_path, data, kind="run_state")
        run.touch()

    def _host_event(self, run: Run,
                    sink: Callable[[str, dict[str, Any]], None] | None) -> Callable[..., None]:
        """Forward a host event onto the bus and to the caller's sink."""
        def handler(event: str, payload: dict[str, Any]) -> None:
            kind = {
                "run.start": EventType.RUN_START,
                "run.end": EventType.RUN_END,
                "run.paused": EventType.RUN_PAUSED,
                "run.resumed": EventType.RUN_RESUMED,
                "run.terminating": EventType.RUN_ABORTED,
                "run.log": EventType.AGENT_LOG,
                "watchdog.stall": EventType.WATCHDOG_RESTART,
                "watchdog.restart": EventType.WATCHDOG_RESTART,
            }.get(event, EventType.AGENT_STATUS)
            self._emit(kind, {"run_id": run.run_id, "host_event": event, **payload})
            if sink is not None:
                try:
                    sink(event, payload)
                except Exception:  # noqa: BLE001 - a sink must not break the run
                    pass
        return handler

    def _write_memory(self, run: Run, state: dict[str, Any]) -> None:
        """Record the completed run as durable memory."""
        try:
            entry = memory_entry_from_state(state, workflow=run.slug, run_id=run.run_id)
            entry.skills = list(run.plan.skills_used) if run.plan else entry.skills
            self.memory.write(entry)
            self._emit(EventType.RUN_CRITERIA_SATISFIED, {
                "run_id": run.run_id, "criteria_met": entry.outcome, "tokens": entry.tokens_used})
        except Exception as exc:  # noqa: BLE001 - memory is an optimisation
            self.diagnostics.warning("memory.write.failed", message=str(exc))

    def _log_phase(self, run: Run) -> None:
        """Record the phase transition, which is what the UI timeline reads."""
        self.diagnostics.info("run.phase", message=f"{run.slug} is {run.phase.value}",
                              detail={"outcome": run.outcome.get("outcome"),
                                      "gate": run.gate.gate_id if run.gate else None})

    def _active_chain(self) -> list[str]:
        """The delegation chain recorded for the current run."""
        run = self._run
        if run is None:
            return []
        chain: list[str] = []
        for decision in run.decisions:
            if decision.get("agent_id"):
                chain.append(str(decision["agent_id"]))
        return chain

    def _producer_of(self, node_id: str, run: Run) -> str:
        """Which agent produced the artifact a review node would judge, when known."""
        producer = (run.outcome.get("nodes") or {})
        for name in producer:
            if name != node_id:
                binding = run.bindings.get(name)
                if binding is not None:
                    agents = getattr(binding, "agents", None) or binding.get("agents") or []
                    if agents:
                        return str(agents[0])
        return ""

    def _default_window(self) -> int:
        """The default model's context window, or a conservative fallback.

        A conservative fallback rather than an invented large number: assuming a big window would let a
        prompt overflow, while assuming a small one only compacts earlier.
        """
        model = self.config.defaults.get("model", "")
        spec = self.config.model_spec(model)
        return int(spec.context_window or 8192)

    def _emit(self, event_type: Any, payload: dict[str, Any]) -> None:
        """Put an event on the bus. Never raises."""
        if self.bus is None:
            return
        try:
            self.bus.emit(event_type, payload=payload)
        except Exception:  # noqa: BLE001 - the bus must not break a run
            pass


def _skill_source(library: Any, *, project: Any = None) -> Any:
    """Build the skill source for a library handle, with user skills layered over it.

    The overlay is what makes an authored skill usable in a plan and a run, not merely present on
    disk — without it, `skills new` would write a file nothing reads.

    `project` matters: the user roots are resolved relative to it, so a run aimed at one project
    finds *that* project's skills. Omitting it falls back to the process's working directory, which
    is how a run picks up the wrong project's roster.
    """
    from .skills import FilesystemSkillSource
    from .skills.overlay import OverlaySkillSource

    return OverlaySkillSource(FilesystemSkillSource(library), project=project)
