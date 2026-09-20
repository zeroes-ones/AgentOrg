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
import re
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
from .mission import Mission, MissionError, MissionState, ObjectiveState
from .host import (
    TERMINATION_ABORTED,
    TERMINATION_DIED,
    TERMINATION_SHUTDOWN,
    TERMINATION_WEDGED,
    HostError,
    RunnerHost,
    RunOutcome,
    RunnerState,
)
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


#: The runner's own log `action`s that mean "this stopped the run", mapped to a plain-English cause.
#: A guardrail block and a contract violation are *decisions*, not crashes, so they are described as
#: the decision rather than as a failure. Only the actions that name a specific, actionable cause are
#: listed — an `escalate` entry is handled generically below with its own detail.
_STOP_ACTIONS: dict[str, str] = {
    "guardrail": "a hand-off payload was blocked by the edge guardrail",
    "contract": "a node's completion contract was violated",
    "error": "a node raised an error",
}

#: What a terminated run says about *why* it was terminated, keyed by the host's own vocabulary
#: (`host.TERMINATION_*`). Imported rather than respelled, so the word the monitor reports and the
#: word the summary prints cannot drift apart — a person who saw `wedged` in the UI must not read
#: `stalled` here and wonder whether those are two different conditions.
_TERMINATION_CLAUSES: dict[str, str] = {
    TERMINATION_WEDGED: "the stall watchdog stopped the run",
    TERMINATION_ABORTED: "the run was aborted by the Owner",
    TERMINATION_SHUTDOWN: "the engine shut down and stopped the run",
    TERMINATION_DIED: "the runner exited on its own while the run was being stopped",
}


def _termination_reason(outcome: Any) -> str:
    """One line naming *why* a terminated run was terminated, with the host's evidence attached.

    `killed` is a boolean that four different events set — the stall watchdog, the Owner's own abort,
    an engine shutdown reaping the runner, and a process that died before it could be signalled — so a
    summary that reads only the boolean can say only "the run was aborted". That sentence distinguishes
    nothing and sends the reader to the checkpoint to guess whether the fix is a longer stall window, a
    different model, or their own stop command.

    The host records the cause and its detail; this turns them into the sentence. A cause the host did
    not record falls back to the old wording rather than inventing one, because a summary that guesses
    is worse than a summary that admits it does not know.
    """
    cause = str(getattr(outcome, "termination", "") or "")
    detail = str(getattr(outcome, "termination_detail", "") or "").strip()
    clause = _TERMINATION_CLAUSES.get(cause, "")
    if not clause:
        return "the run was aborted"
    return f"{clause} — {detail}" if detail else clause


def _derive_stop_reason(state: dict[str, Any], outcome: Any,
                        rendered: dict[str, Any]) -> str:
    """One human-readable line explaining why a run stopped.

    Derived from the runner's own record — its `log` actions and per-node verdicts — so the reason is
    the runner's, not a guess. Priority is deliberate: a named cause (a guardrail block, an exhausted
    loop) beats a generic outcome, because the cause is what a person can act on.

    Empty string means "nothing to explain" — a run that completed cleanly has no stop reason.
    """
    log = state.get("log") or []
    nodes = state.get("nodes") or {}
    reason = str(state.get("outcome") or "") or str(rendered.get("outcome") or "")

    # 1. A named blocking action in the log, most recent last, is the most specific cause.
    for entry in reversed(log):
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "")
        if action in ("guardrail", "contract", "error"):
            node = entry.get("node") or "a node"
            detail = str(entry.get("detail") or "").strip()
            clause = _STOP_ACTIONS.get(action, action)
            # A blocked node also owns a summary that states the reason in the agent's own words.
            summary = ""
            record = nodes.get(node) if isinstance(nodes, dict) else None
            if isinstance(record, dict):
                summary = str(record.get("summary") or "").strip()
            tail = f" — {detail}" if detail else (f" — {summary}" if summary else "")
            return f"{node}: {clause}{tail}"

    # 2. Every node that ended blocked, with its own stated reason.
    blocked = [
        (name, rec) for name, rec in (nodes.items() if isinstance(nodes, dict) else [])
        if isinstance(rec, dict) and str(rec.get("status")) == "blocked"
    ]
    if blocked:
        name, rec = blocked[0]
        verdict = str(rec.get("verdict") or "blocked")
        detail = str(rec.get("summary") or "").strip()
        suffix = f" — {detail}" if detail else ""
        more = f" (and {len(blocked) - 1} more)" if len(blocked) > 1 else ""
        return f"{name} is blocked ({verdict}){suffix}{more}"

    # 3. An escalation with no blocked node — a loop exhausted, a budget hit, a cost ceiling.
    if reason and reason not in ("complete", "None", ""):
        loop_entry = next((e for e in reversed(log)
                           if isinstance(e, dict) and e.get("action") == "escalate"), None)
        detail = str((loop_entry or {}).get("detail") or "").strip() if loop_entry else ""
        # The escalating node, when the entry names one. A contract rework that spends its window
        # logs its exhaustion *at the node*, and "which node could not fix its payload" is the first
        # thing a person needs — a reason that says only what happened, not where, sends them to the
        # checkpoint to find out. A budget escalate logs `node: None`, which reads as it always did.
        node = str((loop_entry or {}).get("node") or "").strip() if loop_entry else ""
        prefix = f"{node}: " if node else ""
        return f"{prefix}run ended: {reason}" + (f" — {detail}" if detail else "")

    if getattr(outcome, "killed", False):
        return _termination_reason(outcome)
    error = str(getattr(outcome, "error", "") or "").strip()
    if error:
        return error
    return ""


class OrchestratorError(RuntimeError):
    """Raised when a run cannot proceed: an unusable plan, an unstaffed node, or a refused command."""


#: Keys the **library runner's** checkpoint carries and the orchestrator's never does. `workflow` and
#: `manifest_sha` are written by the runner's `save_state`; the orchestrator's `Run.as_dict` writes
#: `run_id` and `run_phase_version` instead, so the two shapes are distinguishable without guessing.
_RUNNER_CHECKPOINT_KEYS = ("workflow", "manifest_sha")


def _is_runner_checkpoint(doc: dict[str, Any]) -> bool:
    """Whether a document at `run_state.json` is the workflow runner's checkpoint, not ours.

    Needed because both sides used to write this one file. A workspace that ran before they were
    separated has only the runner's shape on disk, and treating it as our checkpoint crashed `status`
    with a schema error that read like corruption. Detection is by the runner's own keys, and the
    presence of our markers is checked first so a document that is genuinely ours — a newer schema the
    registry must be allowed to refuse — is never mistaken for the runner's.
    """
    if not isinstance(doc, dict):
        return False
    if "run_id" in doc or "run_phase_version" in doc:
        return False
    return any(key in doc for key in _RUNNER_CHECKPOINT_KEYS)


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
    #: Which org this run belongs to, and the principal who owns it. Denormalised from `org` so the
    #: checkpoint, the trace and the console can name the org without walking the roster — which is
    #: what makes a *fleet* of runs, one per org, legible in one place.
    org_id: str = ""
    principal_id: str = ""
    ledger: Ledger | None = None
    policy: PolicyResolver | None = None
    bindings: dict[str, Any] = field(default_factory=dict)
    staffing_gaps: list[dict[str, Any]] = field(default_factory=list)
    gate: GateRequest | None = None
    outcome: dict[str, Any] = field(default_factory=dict)
    #: *Why* the run stopped, in one human-readable line. Derived from the runner's own log (a
    #: guardrail block, a contract violation, an exhausted loop) rather than guessed — because a run
    #: that died with `pm = blocked / guardrail-blocked` and no explanation is unusable.
    stop_reason: str = ""
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
            "org_id": self.org_id, "principal_id": self.principal_id,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "plan": self.plan.as_dict() if self.plan else None,
            "bindings": {k: (v.as_dict() if hasattr(v, "as_dict") else v)
                         for k, v in self.bindings.items()},
            "staffing_gaps": self.staffing_gaps,
            "gate": self.gate.as_dict() if self.gate else None,
            "outcome": self.outcome,
            "stop_reason": self.stop_reason,
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
        run.stop_reason = str(data.get("stop_reason") or "")
        run.org_id = str(data.get("org_id") or "")
        run.principal_id = str(data.get("principal_id") or "")
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


#: What an auto-created helper may touch: read anywhere in the project, write anywhere inside it.
#:
#: One definition, used both to *classify* the helper's risk and to *create* it, because those two
#: must describe the same agent — a cap computed from a different capability set than the one granted
#: would bound a fiction. `write:src/**` rather than `write:*` is what keeps this a normal grant: the
#: tool layer contains it to the project (`ToolRegistry._resolve` refuses absolute, `~` and `..`
#: paths, and anything resolving outside the workspace), so it does not reach beyond what the run was
#: pointed at.
_AUTO_HELPER_CAPABILITIES: tuple[str, ...] = ("read:*", "write:src/**")


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
        # The *project directory*, not the root that holds projects. `root` is a directory *of*
        # projects (`projects/`, or the parent of an attached folder), so resolving the user skill
        # roots against it looked for `projects/.agentorg/skills` or `/tmp/.agentorg/skills` and
        # never found an attached project's own skills — the overlay then held nothing the project
        # authored and the planner's validator, told the same catalogue, refused the node by name.
        self.source = _skill_source(library, project=getattr(workspace, "path", None))
        self.diagnostics = diagnostics or Diagnostics(
            run_id="", state_dir=workspace.state_dir)
        self.registry: Registry = default_registry(dict(config.schemas or {}))
        self.org = org or default_company(
            provider=config.default_pair()[0] or "ollama",
            model=config.default_pair()[1] or config.default_model_spec().model_id or "",
            context_window=self._default_window(),
        )
        # The planner gets the roster so it can report which needed skills nobody holds — the gap is
        # cheapest to fix before approval, when the Owner is looking at the graph.
        self.planner = Planner(self.source, config=config, org=self.org)
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
        #: The durable *why* above the goal. Loaded disarmed for the same reason: a restart must not
        #: resume a sequence of objectives nobody asked to continue.
        try:
            self._mission: Mission | None = Mission.load(workspace)
        except MissionError as exc:  # noqa: BLE001 - a bad mission must not block a run
            self.diagnostics.warning("mission.load.failed", message=str(exc))
            self._mission = None

    # ── preparing a run ─────────────────────────────────────────────────────

    def _run_workspace(self, slug: str) -> Workspace:
        """Where a run's manifest and state live.

        An **attached** workspace is the user's own folder, so a run against it must write *there* —
        `run_state.json` beside the trace the orchestrator already writes into it. Rebuilding a managed
        `root/<slug>` workspace instead is the split-brain this guards against: the run's state landed
        in a sibling directory of the project while the trace landed inside it, so `status` (reading the
        attached folder) saw no run at all and the app showed an idle project with work in flight.

        A managed workspace keeps the old behaviour: the plan's own slug names a project under `root`.
        """
        if getattr(self.workspace, "is_attached", False):
            return self.workspace
        return Workspace.for_project(slug, root=self.workspace.root)

    def prepare(self, goal: str, *, slug: str | None = None,
                max_iterations: int = 3, auto_staff: bool | None = None,
                honour_armed_goal: bool = False) -> Run:
        """Plan the goal, bind it to the roster, and present it for approval.

        Nothing executes here. The Owner approves a *graph*, and the graph is shown with its staffing
        gaps before it can be approved — because a plan needing a capability nobody holds would
        otherwise stop three nodes in, far from the cause.

        `auto_staff` closes those gaps before binding when a goal authorises it: a node whose skill
        nobody holds gets a helper on the default model, so the plan is runnable rather than blocked on
        an administrative accident of the roster. `None` means "ask the goal's policy", which is the
        autonomous default; `False` forces the gap to be reported instead.

        Raises
        ------
        OrchestratorError
            When the goal cannot be planned at all.
        """
        try:
            plan = self.planner.plan(goal, slug=slug, max_iterations=max_iterations)
        except PlanError as exc:
            raise OrchestratorError(f"cannot plan this goal: {exc}") from exc

        # An explicit execution request may adopt an on-disk armed goal, so a `run` from a *new*
        # process continues the loop the previous `goal set` armed. Without this the CLI could never
        # drive a goal — every command is a new process, and each one disarmed the goal it read.
        if honour_armed_goal and self._goal is not None:
            self._goal.adopt()
            self._goal.save(self.workspace)

        project = self._run_workspace(plan.slug)
        project.ensure()
        manifest_path = project.path / f"{plan.slug}.yaml"
        manifest_path.write_text(emit_safe_yaml(plan.manifest), encoding="utf-8")

        run = Run(
            run_id=f"run_{int(time.time())}_{plan.slug}",
            slug=plan.slug, goal=goal, workspace=project,
            phase=RunPhase.AWAITING_APPROVAL, plan=plan, manifest_path=manifest_path,
            org=self.org, ledger=self.ledger, policy=self.policy,
            org_id=str(getattr(self.org, "id", "") or ""),
            principal_id=str(getattr(self.org, "principal_id", "") or ""),
        )
        # Close the gaps first when the goal authorises it, then measure what is left — so the graph
        # the Owner is shown is the graph that will actually run.
        staffing = self._auto_staff(plan, enabled=auto_staff, run=run)
        run.staffing_gaps = staffing["gaps"]
        if staffing["created"]:
            run.decisions.append({
                "action": "auto-staff", "by": "goal", "at": _iso_now(),
                "detail": [f"{c['skill']} -> {c['agent']}" for c in staffing["created"]],
            })
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
            "auto_staffed": staffing["created"],
        })
        self.diagnostics.info("run.prepared", message=f"plan ready for approval: {run.slug}",
                              detail={"nodes": len(plan.nodes), "gaps": len(run.staffing_gaps),
                                      "auto_staffed": len(staffing["created"])})
        return run

    def _auto_staff(self, plan: Any, *, enabled: bool | None, run: Run) -> dict[str, Any]:
        """Fill a plan's staffing gaps with helpers on the default model, when authorised.

        This is the "if the person does not exist, create one" half. The gap is computed against the
        real roster first, so an existing holder is always preferred — a helper is only ever created
        for a capability *nobody* has. Creation happens in the org the run will actually use, so the
        very next bind sees the helper.

        Two decisions, both from the goal's policy:

        - **ephemeral or durable.** By default the helper is ephemeral: it does the work and leaves no
          roster entry to clean up. With `persist_hires` it is saved to the roster root, so the
          capability is reusable and visible in `agents`.
        - **how risky a hire.** `auto_hire_max_tier` (config) caps the delegation tier. Every node in a
          plan is *work*, not a privileged operation, so the helper's own grant — read the project,
          write inside it — is the safest tier there is. Above the cap the gap is *reported* with its
          tier and the reason, so it reaches the Owner as a staffing gap rather than as a silent hire.
        """
        from .org.agent import AgentKind, AgentLevel, AgentSpec, Budget, new_agent_id
        from .skills.roles import is_verifier

        created: list[dict[str, Any]] = []
        gaps = self.binder.staffing_gaps(plan.manifest)
        if enabled is not None:
            # An explicit override beats both the goal and the config — that is what makes
            # `--no-auto-hire` on a single run meaningful.
            allow = bool(enabled)
            persist = bool(getattr(self.config.goal, "persist_auto_hires", False))
        else:
            # Otherwise the *goal's* policy decides when a goal exists, and the configured default
            # applies when it does not — so a plain `run --goal` staffs its own gaps too, which is
            # exactly "create the person if they do not exist".
            policy = self._effective_goal_policy()
            if policy is not None:
                allow = bool(policy.auto_hire)
                persist = bool(policy.persist_hires)
            else:
                allow = bool(getattr(self.config.goal, "auto_hire_missing", True))
                persist = bool(getattr(self.config.goal, "persist_auto_hires", False))
        if not allow or not gaps:
            return {"gaps": gaps, "created": created}

        provider, model, reason = self.config.default_pair()
        # Resolve the window the same way a *hire* does — catalog first, declared table second — so a
        # default on a model the provider reports (but the config never declared) can still be staffed.
        # Using only the declared table refused exactly that case in a real run, leaving the gap
        # unfilled and the plan unrunnable.
        window, max_output = self._auto_staff_window(provider, model)
        if not provider or not model or not window:
            # Nothing to create an agent *on*. The gap is reported with the reason rather than
            # silently producing a helper that cannot be projected or called.
            self.diagnostics.warning(
                "goal.autostaff.unavailable",
                message="cannot staff the gap: no default provider/model with a known context window",
                detail={"provider": provider, "model": model, "context_window": window})
            return {"gaps": gaps, "created": created}

        # The risk ceiling for an auto-created helper, resolved once. `goal.auto_hire_max_tier` caps
        # the delegation tier the engine may grant itself; above it a helper is *reported* rather than
        # created, so the gap reaches the Owner as a staffing gap instead of as a silent hire.
        #
        # Classified by the desk's own `classify_tier` rather than by a second opinion here, because
        # the two must not be able to disagree about what tier a helper is — the cap is only
        # meaningful if the thing it bounds is the same thing the desk would have judged had this
        # gone through `evaluate`.
        max_tier = int(getattr(self.config.goal, "auto_hire_max_tier", 0) or 0)
        helper_tier, helper_tier_why = self._helper_tier(skill="", provider=provider, model=model,
                                                         window=window)
        if self._tier_rank(helper_tier) > max_tier:
            self.diagnostics.warning(
                "goal.autostaff.capped",
                message=f"staffing gaps left for the Owner: a helper for these skills would be "
                        f"{helper_tier.value}, above the auto-hire cap T{max_tier}",
                detail={"tier": helper_tier.value, "cap": f"T{max_tier}",
                        "why": helper_tier_why, "gaps": [g.get("skill") for g in gaps]})
            self._emit(EventType.HUMAN_GATE, {
                "run_id": run.run_id, "kind": "policy", "gate_id": "auto-hire-tier",
                "waiting_on": "owner",
                "why": f"an auto-created helper would be {helper_tier.value}, above the "
                       f"configured cap T{max_tier}",
                "tier": helper_tier.value, "cap": f"T{max_tier}",
                "gaps": [{"node_id": g.get("node_id"), "skill": g.get("skill")} for g in gaps]})
            return {"gaps": gaps, "created": created}

        for gap in gaps:
            skill = str(gap.get("skill") or "")
            if not skill:
                continue
            name = self._helper_name(skill)
            helper = AgentSpec(
                id=new_agent_id(),
                name=name,
                title=gap.get("node_id", skill).replace("-", " ").title(),
                skills=[skill],
                provider=provider,
                model=model,
                context_window=int(window),
                max_output=max_output,
                kind=AgentKind.AI,
                role="reviewer" if is_verifier(skill) else "worker",
                level=AgentLevel.SENIOR,
                team="Platform",
                capabilities=list(_AUTO_HELPER_CAPABILITIES),
                budget=Budget(),
                max_concurrency=1,
                # `goal` origin, not `owner`: this employee was created by the engine on the goal's
                # authority, and the roster and audit trail must be able to tell the difference.
                origin="goal" if persist else "ephemeral",
            )
            try:
                self.org.hire(helper, team=helper.team)
            except Exception as exc:  # noqa: BLE001 - a hire failure must not break the plan
                self.diagnostics.warning("goal.autostaff.failed",
                                         message=f"could not staff {skill}: {exc}")
                continue
            created.append({"skill": skill, "node_id": gap.get("node_id"), "agent": helper.name,
                            "agent_id": helper.id, "provider": provider, "model": model,
                            "persisted": persist, "reason": reason})
            self._emit(EventType.AGENT_SPAWN, {
                "run_id": run.run_id, "agent_id": helper.id, "name": helper.name,
                "skills": [skill], "provider": provider, "model": model,
                "kind": "helper", "origin": helper.origin, "persisted": persist,
                "why": "the plan needed a capability no agent held"})
            self.diagnostics.info("goal.autostaff",
                                  message=f"staffed {skill} with {helper.name}",
                                  detail={"provider": provider, "model": model,
                                          "persisted": persist})

        if persist and created:
            self._persist_helpers(created)
        # Re-measure, so the graph shown to the Owner reflects the helpers just created.
        return {"gaps": self.binder.staffing_gaps(plan.manifest), "created": created}

    def _helper_tier(self, *, skill: str, provider: str, model: str,
                     window: int | None) -> tuple[Any, str]:
        """The delegation tier a helper created for a gap would land in, and why.

        Built from the same shape `_auto_staff` is about to create, so the cap governs the real
        thing: the helper's own capabilities, budget and kind. A synthetic requisition is used
        because `HiringDesk.evaluate` runs the *full* approval path (budget partitioning, ladder
        evidence, lineage) — all of which are for an agent *asking* to delegate, whereas auto-staffing
        is the engine filling a gap on the goal's authority. What matters here is one question: how
        risky is the hire, which is what `classify_tier` answers.
        """
        from .org.delegation import Requisition

        request = Requisition(
            requester_id="goal", requester_name="goal", kind="helper", skill=skill or "helper",
            provider=provider, model=model, context_window=window,
            capabilities=list(_AUTO_HELPER_CAPABILITIES),
            requested_tokens=0, requested_usd=0.0,
        )
        try:
            return self.desk.classify_tier(request)
        except Exception as exc:  # noqa: BLE001 - an unclassifiable hire is not an auto-approvable one
            from .org.delegation import ApprovalTier

            return ApprovalTier.T3, f"could not classify this helper ({exc}); treated as the most \
gated tier rather than the least"

    @staticmethod
    def _tier_rank(tier: Any) -> int:
        """A tier's ordinal, so a cap can be compared rather than string-matched.

        Read off the enum's own value (`T0`..`T3`) so a new tier added below the cap is admitted and
        one added above it is not — the property a numeric comparison buys and a membership test
        would lose.
        """
        text = str(getattr(tier, "value", tier) or "").strip().upper()
        digits = "".join(ch for ch in text if ch.isdigit())
        return int(digits) if digits else 3

    def _auto_staff_window(self, provider: str, model: str) -> tuple[int | None, int | None]:
        """The context window (and max output) for an auto-created helper.

        Resolved exactly as a *hire* resolves it — the live catalog first, the declared table second —
        because a default provider the engine probed (`Olla`) reports models the config never declared,
        and reading only the declared table then refused to staff a perfectly usable model. A config
        override still wins, since it is the person's explicit statement about a window the provider
        cannot report.
        """
        if not provider or not model:
            return None, None
        # An explicit `defaults.context_window` beats everything: it exists for the model the provider
        # cannot report, which is the commonest first-run failure.
        spec = self.config.default_model_spec()
        if spec.context_window and spec.model_id == model:
            return int(spec.context_window), spec.max_output
        try:
            from .catalog import ModelCatalog
            from .providers.registry import build_providers

            providers, _ = build_providers(self.config)
            entry = ModelCatalog(self.config, providers).resolve(provider, model)
            if entry is not None and entry.window_known:
                return int(entry.context_window), entry.max_output
        except Exception as exc:  # noqa: BLE001 - a catalog failure falls through to the table
            self.diagnostics.warning("goal.autostaff.catalog.failed", message=str(exc))
        return None, None

    def _helper_name(self, skill: str) -> str:
        """A readable, unique name for an auto-created helper.

        Derived from the skill so the roster reads as *what it does*, and suffixed only when a clash
        would otherwise make two helpers indistinguishable — the roster refuses a duplicate name, and a
        refusal here would leave the gap unfilled.
        """
        base = "".join(part[:1].upper() + part[1:] for part in skill.split("-")) or "Helper"
        candidate, index = base, 2
        taken = {a.name.lower() for a in self.org.agents.values()}
        while candidate.lower() in taken:
            candidate = f"{base}{index}"
            index += 1
        return candidate

    def _persist_helpers(self, created: list[dict[str, Any]]) -> None:
        """Write the durable helpers to the roster root, best-effort.

        A failure to persist must not undo a hire that is already usable in this run: the helper still
        does the work, and the person is told it will not be there next time.
        """
        try:
            from .people import People

            people = People(library=self.library, config=self.config)
            people.save(org=self.org, roster_root=self._roster_root())
            self.diagnostics.info("goal.autostaff.persisted",
                                  message=f"persisted {len(created)} helper(s) to the roster")
        except Exception as exc:  # noqa: BLE001 - persistence is a nicety, not the work
            self.diagnostics.warning("goal.autostaff.persist.failed", message=str(exc))

    def _roster_root(self) -> Path | None:
        """Where a durable helper is written: the **project's** `.agentorg`.

        The project directory, not `workspace.root`. For an attached workspace `root` is the project's
        *parent* (`Workspace.attach` sets `root=resolved.parent`), so writing to `root/.agentorg` put
        the roster beside the repository rather than in it — a real run wrote five helpers to
        `/tmp/.agentorg`, where nothing would ever read them again. `workspace.path` is the project
        directory in both modes.
        """
        project = getattr(self.workspace, "path", None)
        if project:
            return Path(project) / ".agentorg"
        root = getattr(self.workspace, "root", None)
        return Path(root) / ".agentorg" if root else None

    def _effective_goal_policy(self) -> Any:
        """The active goal's policy with `human_gate` applied, or None when there is no goal."""
        goal = self._goal
        if goal is None:
            return None
        return goal.policy.effective()

    def _contract_rework_attempts(self, run: Run) -> int:
        """How many times a contract refusal outside a loop may be retried for this run.

        Read from the *goal's posture*, because it decides who recovers: an unattended goal repairs
        its own payload, a supervised one asks. `run` is accepted so the decision has somewhere to
        record itself as the run grows one, but nothing is read from it yet — the posture is the only
        input, and inventing a second one (a per-run override) before it is asked for would be a knob
        with no caller.

        With no goal at all (a plain `run --manifest`), the config answers — the same polarity the
        rest of the engine uses: the goal's authority when there is a goal, and the configured
        default when there is not.
        """
        section = getattr(self.config, "executor", None)
        width = max(0, int(getattr(section, "contract_rework", 0) or 0))
        policy = self._effective_goal_policy()
        if policy is None:
            return width
        return width if policy.unattended else 0


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
        project = self._run_workspace(project_slug)
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
            org_id=str(getattr(self.org, "id", "") or ""),
            principal_id=str(getattr(self.org, "principal_id", "") or ""),
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
            # The catalogue the run was planned against, so the runner is shown it instead of only
            # the pinned library. Without it a node naming a skill the Owner authored is refused by
            # the runner's own validator with "does not resolve under skills/" — the last wall between
            # authoring a skill and executing it.
            skills=self.source,
            on_event=self._host_event(run, on_event),
            on_stderr=lambda line: self._emit(EventType.AGENT_LOG, {
                "run_id": run.run_id, "stream": "stderr", "text": line[:500]}),
            heartbeat_s=float(self.config.concurrency.heartbeat_s),
            grace_s=float(self.config.concurrency.grace_s),
            # The stall window is a configured bound like the other two, and it is passed here for the
            # same reason: the host a *run* builds must use the number in the file. Left to the host's
            # own resolution it would still be right, but naming it here is what makes the three
            # supervision bounds read as one block rather than two configured and one inherited.
            stall_timeout_s=float(self.config.concurrency.stall_timeout_s),
        )
        # Who recovers from a contract refusal is the goal's decision, so the posture sets the width
        # here — the one place the run's host is built. An unattended goal gets the bounded window
        # (a refused payload is a fixable defect, and fixing it is the whole point of being able to
        # leave the run alone); a supervised one gets `None`, which resolves to the config's 0 and
        # escalates on the first refusal exactly as it always did.
        host.with_contract_rework(self._contract_rework_attempts(run))
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
        # Reconcile the mission with the goal's verdict, so a mission whose step finished advances
        # on its own — the autonomy the hierarchy exists for. Conservative: only a goal's own
        # explicit complete/blocked moves an objective. Tolerated if it fails: a mission problem
        # must never turn a finished run into an error.
        try:
            self.mission_sync()
        except Exception as exc:  # noqa: BLE001
            self.diagnostics.warning("mission.sync.failed", message=str(exc))
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
          - a round that advanced nothing (see `_runner_state_signature`; pauses with reason
            `run-complete`),
          - the run failing or being killed,
          - the goal being paused/cleared underneath us.
        """
        goal = self._goal
        outcome = self._host.run(manifest_path=run.manifest_path, run_id=run.run_id,
                                 workflow=run.slug, project=run.slug, extra_args=extra,
                                 goal_active=bool(goal is not None and goal.state.is_live))
        if goal is None or not goal.state.is_live:
            return outcome

        policy = goal.policy.effective()
        max_rounds = self._goal_max_rounds()
        for round_index in range(1, max_rounds + 1):
            # Fold what this round cost, so "no ceiling" never means "no idea". A round whose cost was
            # never reported is folded as unreported rather than as zero — the ledger's own rule.
            tokens, requests, cost, unknown_calls = self._round_spend(run)
            goal.record_round(tokens=tokens, requests=requests, cost_usd=cost,
                              unknown_cost_calls=unknown_calls)

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

            # A gate still holds. With autonomy on (the default) the org decides the gates it *can*
            # decide and keeps going; a terminal gate — release, close, spend — is never passed, and a
            # goal that chose a human gate parks here exactly as an ordinary run does.
            if run.gate is not None or run.phase in (RunPhase.AWAITING_GATE, RunPhase.AWAITING_HUMAN):
                if not self._auto_pass(run, policy):
                    goal.pause(reason="gate")
                    goal.save(run.workspace)
                    self._emit(EventType.GOAL_PAUSED, {"objective": goal.objective, "reason": "gate",
                                                       "spend": goal.spend.as_dict()})
                    return outcome
                # The gate was passed and the run is ready again: fall through to the next round.

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
                                                 "spend": goal.spend.as_dict(),
                                                 "autonomous": not policy.human_gate})
            # What the graph looked like before this round, so the round can be judged by whether it
            # *moved* anything rather than by whether it exited cleanly. A resume against a finished
            # checkpoint returns `complete` in a fraction of a second having done nothing, and the
            # driver used to treat that as progress and resume again — measured: 361 rounds in under
            # four minutes, 721 runner processes, one no-op resume every ~0.3s, all of it bounded only
            # by `goal.max_rounds` (10,000), which the design calls "a backstop, not the practical
            # limit". It was the practical limit, and it is the one outcome an unattended run must not
            # produce: `done` that never reports and a machine that will not stop working.
            before = self._runner_state_signature(run)
            outcome = self._host.resume_run(
                manifest_path=run.manifest_path, run_id=run.run_id, workflow=run.slug,
                project=run.slug, goal_active=True, extra_args=extra)
            self._settle(run, outcome)
            # A pending gate is *work*: a decision someone has to make. A round that ends at one has
            # not stalled, and stopping here would pause a goal that is one decision from finishing —
            # which is exactly what happened the first time this check was written, because the round
            # that discovers the terminal gate is also a round whose checkpoint looks unchanged. The
            # wait is read from the settled run, so it is the same reading the next round's gate
            # branch acts on (auto-pass for an unattended goal, a pause for the Owner).
            waiting = run.gate is not None or run.phase in (RunPhase.AWAITING_GATE,
                                                            RunPhase.AWAITING_HUMAN)
            if not waiting and self._runner_state_signature(run) == before:
                # A round that changed nothing has nothing left to advance. The goal is *paused*
                # rather than completed, and that polarity is the whole reason this is safe: only an
                # agent's `update_goal(complete)` may claim the objective was met (DESIGN-GOAL §6),
                # so the engine says the honest smaller thing — the approved graph is finished and no
                # node has work left — and asks for an explicit resume if more is wanted. A paused
                # goal cannot spin, and `goal resume` is one command away.
                goal.pause(reason="run-complete")
                goal.save(run.workspace)
                self._emit(EventType.GOAL_PAUSED, {"objective": goal.objective,
                                                   "reason": "run-complete",
                                                   "spend": goal.spend.as_dict()})
                self.diagnostics.info(
                    "goal.run_complete",
                    message=(f"round {round_index} advanced nothing: every node is terminal, so the "
                             "goal is paused rather than resumed again"))
                return outcome
        return outcome

    def _auto_pass(self, run: Run, policy: Any) -> bool:
        """Decide and pass an auto-approvable gate, so the loop can continue.

        Returns False when the Owner must decide — a terminal gate, or a goal that asked for a human
        gate. Returns True once the gate has been resolved (approved) and the run is ready to advance.

        Two refusals make this safe rather than a rubber stamp:

        - **A terminal gate is never passed.** The manifest marks it `kind: human`; that is the release,
          close and spend authority, and autonomy does not extend to it.
        - **An agent gate is answered by the org.** The reroute gate is `kind: agent`: the runner already
          computed which channels were tried and which channel should lead the next pass, so approving
          it is *recording a decision the org made*, not inventing one. When the runner offers no
          channel (`requires` empty, dossier has no route), the gate is left for the Owner rather than
          approved blind — an approval with nothing to act on would loop.
        """
        gate = run.gate
        if gate is None:
            # An AWAITING_* phase with no gate object: nothing to decide, so do not loop on it.
            return False
        if policy.human_gate or not policy.auto_approve:
            return False
        if not self._gate_is_auto_approvable(gate):
            # A terminal gate is the one case a goal may still answer, and only when the posture says
            # so. Everything else that reaches here is genuinely the Owner's: an unknown gate kind, or
            # a policy class the config already answered with `confirm`.
            if str(gate.kind or "").strip().lower() == "human" and policy.unattended:
                return self._release_terminal_gate(run, gate)
            self._emit(EventType.HUMAN_GATE, {
                "run_id": run.run_id, "gate_id": gate.gate_id, "kind": gate.kind,
                "reason": gate.reason, "waiting_on": "owner",
                "why": "a terminal gate is only passable by the Owner"})
            return False
        if gate.kind == "agent" and not self._gate_has_a_route(run, gate):
            self._emit(EventType.HUMAN_GATE, {
                "run_id": run.run_id, "gate_id": gate.gate_id, "kind": gate.kind,
                "reason": gate.reason, "waiting_on": "owner",
                "why": "the org gate found no untried route, so there is nothing to approve"})
            return False
        try:
            self.decide(True, run=run, note="auto-approved by the goal policy",
                        by="goal")
        except OrchestratorError:
            return False
        self._emit(EventType.POLICY_CHANGED, {
            "run_id": run.run_id, "gate_id": gate.gate_id, "approved": True,
            "by": "goal", "kind": gate.kind,
            "why": "the goal authorises the org to decide this gate"})
        self.diagnostics.info("goal.autopass",
                              message=f"auto-approved {gate.gate_id} ({gate.kind})")
        return True

    def _gate_is_auto_approvable(self, gate: GateRequest) -> bool:
        """Whether the org, rather than the Owner, may decide this gate.

        Three cases, and each is a *refusal* first:

        - `kind: human` is terminal authority (release, close, spend). It is not auto-approvable
          *here* — `_auto_pass` routes it to `_release_terminal_gate`, which applies the extra
          evidence and safety checks an unattended release requires. Keeping it out of this predicate
          means "mechanically decidable" and "the goal released it" stay distinguishable facts.
        - `kind: agent` is a bounded reroute — the runner already computed the decision, so the goal
          only records it.
        - `kind: policy` is a route class the *config* already answered, so it is passed only when the
          policy matrix actually permits action for that class. This is what keeps the documented
          `R-ESCALATE` safety floor intact: an escalation is `confirm` by default, and a goal does not
          get to overrule the config's own answer — `policy.allow_autonomous_escalation` is the one
          explicit opt-in, exactly as before.
        - An unknown kind defaults to *not* auto-approvable, the safe polarity for a gate type this
          build does not understand.
        """
        kind = str(gate.kind or "").strip().lower()
        if kind == "agent":
            return True
        if kind == "policy":
            return self._policy_permits_escalation()
        return False

    #: Stop reasons that must never be released by a goal. A guardrail block or a contract violation
    #: is a safety control *firing*, and autonomy may decide that work is done — it may not decide that
    #: a control which fired was wrong. Matched as substrings against `run.stop_reason`.
    _UNRELEASABLE_STOP_REASONS: tuple[str, ...] = (
        "guardrail", "contract", "error",
    )

    def _release_terminal_gate(self, run: Run, gate: GateRequest) -> bool:
        """Release the terminal gate on the goal's authority, or refuse and park.

        This is the one decision that makes an unattended goal able to *finish*, and it is deliberately
        the most guarded path in the file. Four refusals, each with its own reason so the trace says
        which one fired:

        1. **The evidence must be present.** The gate declares `requires` — for the composed plan that
           is one `<node>.summary` per reviewer. A release with no evidence is an approval of nothing,
           so the gate parks exactly as it always did. Note that these are *node summary* specs, not
           artifact names, so they are resolved against the run's node records rather than against the
           artifact index (which the gate's `present` list holds, and which therefore never intersects
           `requires` — a release keyed on that intersection would refuse forever).
        2. **No safety control may have fired.** See `_UNRELEASABLE_STOP_REASONS`.
        3. **No node may be blocked.** A blocked node is a stated, concrete failure; releasing over it
           would record "done" against a run that said it could not proceed.
        4. **The ledger must accept the record.** The release is written as a decision so "who released
           this, and why" is answerable afterwards. If the ledger refuses (an irreversible decision
           already stands for this gate), the run parks rather than releasing unrecorded.
        """
        evidence = self._gate_evidence(run, gate)
        if not evidence["complete"]:
            self._emit(EventType.HUMAN_GATE, {
                "run_id": run.run_id, "gate_id": gate.gate_id, "kind": gate.kind,
                "reason": gate.reason, "waiting_on": "owner",
                "why": "the gate's evidence is not present, so there is nothing for the goal to "
                       "release",
                "missing": evidence["missing"]})
            self.diagnostics.info(
                "goal.release.refused",
                message=f"{gate.gate_id}: evidence missing ({', '.join(evidence['missing']) or 'none'})")
            return False

        stop_reason = str(run.stop_reason or "")
        fired = next((name for name in self._UNRELEASABLE_STOP_REASONS
                      if name in stop_reason.lower()), "")
        if fired:
            self._emit(EventType.HUMAN_GATE, {
                "run_id": run.run_id, "gate_id": gate.gate_id, "kind": gate.kind,
                "reason": gate.reason, "waiting_on": "owner",
                "why": f"a safety control fired ({fired}); the goal may not release this",
                "stop_reason": stop_reason})
            self.diagnostics.warning(
                "goal.release.refused",
                message=f"{gate.gate_id}: a {fired} control fired, so the Owner must decide")
            return False

        blocked = evidence["blocked_nodes"]
        if blocked:
            self._emit(EventType.HUMAN_GATE, {
                "run_id": run.run_id, "gate_id": gate.gate_id, "kind": gate.kind,
                "reason": gate.reason, "waiting_on": "owner",
                "why": "a node ended blocked, so the work is not done",
                "blocked": blocked[:5]})
            self.diagnostics.warning(
                "goal.release.refused",
                message=f"{gate.gate_id}: node(s) blocked: {', '.join(blocked[:3])}")
            return False

        try:
            self.ledger.record(
                gate=gate.gate_id, choice="released", by="goal",
                rationale=("unattended goal released the terminal gate; evidence present: "
                           + (", ".join(evidence["present"]) or "none")),
                reversible=True, confidence="medium",
            )
        except Exception as exc:  # noqa: BLE001 - an unrecordable release must not happen
            self._emit(EventType.HUMAN_GATE, {
                "run_id": run.run_id, "gate_id": gate.gate_id, "kind": gate.kind,
                "reason": gate.reason, "waiting_on": "owner",
                "why": f"the release could not be recorded in the ledger: {exc}"})
            self.diagnostics.warning("goal.release.refused", message=str(exc))
            return False

        try:
            self.decide(True, run=run,
                        note="released by the unattended goal; evidence present",
                        by="goal")
        except OrchestratorError:
            return False
        self._emit(EventType.POLICY_CHANGED, {
            "run_id": run.run_id, "gate_id": gate.gate_id, "approved": True,
            "by": "goal", "kind": gate.kind, "posture": "unattended",
            "why": "the goal's posture is unattended and the gate's evidence is present"})
        self.diagnostics.info(
            "goal.release",
            message=f"released terminal gate {gate.gate_id} on the goal's authority")
        return True

    def _gate_evidence(self, run: Run, gate: GateRequest) -> dict[str, Any]:
        """What the gate asked for, what is present, and whether that is enough to release.

        Two evidence shapes, because the engine has two:

        - A **node-summary spec** (`<node>.summary`, what the composed plan's terminal gate declares)
          resolves against `run.outcome["nodes"]` and counts as present when the named node finished
          and wrote a non-empty summary.
        - Anything else is an **artifact name**, resolved against the run's artifact index — the same
          index the executor uses for `requires`/`present` (`executor.Executor._gate`).

        `complete` is true only when every declared requirement is satisfied. A gate that declares
        nothing is treated as incomplete rather than trivially complete: a release needs something to
        point at, and "the gate required nothing" is not evidence that the work is done.
        """
        outcome = run.outcome if isinstance(run.outcome, dict) else {}
        nodes = outcome.get("nodes") if isinstance(outcome.get("nodes"), dict) else {}
        artifacts = {str(name) for name in (outcome.get("artifacts") or [])}

        requires = [str(r) for r in (gate.requires or [])]
        present: list[str] = []
        missing: list[str] = []
        for requirement in requires:
            if requirement.endswith(".summary"):
                node = requirement[: -len(".summary")]
                record = nodes.get(node) if isinstance(nodes, dict) else None
                summary = str((record or {}).get("summary") or "").strip() \
                    if isinstance(record, dict) else ""
                (present if summary else missing).append(requirement)
            else:
                (present if requirement in artifacts else missing).append(requirement)

        blocked_nodes = [
            str(name) for name, record in (nodes.items() if isinstance(nodes, dict) else [])
            if isinstance(record, dict) and str(record.get("status")) == "blocked"
        ]
        return {
            "requires": requires,
            "present": present,
            "missing": missing,
            "complete": bool(requires) and not missing,
            "blocked_nodes": blocked_nodes,
        }

    def _policy_permits_escalation(self) -> bool:
        """Whether the policy matrix lets the org proceed on an escalation without asking.

        Read from the resolver rather than re-implemented, so the goal's autonomy and the router's
        answer cannot disagree: `R-ESCALATE` is `confirm` until `allow_autonomous_escalation` is set,
        and that floor is the whole reason it exists.
        """
        resolver = getattr(self, "policy", None)
        if resolver is None:
            return False
        try:
            return bool(resolver.may_act(RouteClass.ESCALATE))
        except Exception:  # noqa: BLE001 - an unresolvable policy means "do not auto-pass"
            return False

    def _gate_has_a_route(self, run: Run, gate: GateRequest) -> bool:
        """Whether an agent gate actually carries a decision to record.

        The runner's reroute gate puts the untried channels in `dossier`/`requires`. Nothing to route
        to means an approval would advance the graph with no corrective action — the definition of an
        infinite loop — so the gate is left to the Owner instead.
        """
        dossier = gate.dossier if isinstance(gate.dossier, dict) else {}
        if gate.requires:
            return True
        for key in ("route", "channel", "channels", "untried", "next"):
            value = dossier.get(key)
            if value:
                return True
        return False


    def cost_snapshot(self, run: Run | None = None) -> dict[str, Any] | None:
        """The run's cost picture, as the process that spent the money recorded it.

        The :class:`~engine.gateway.CostLedger` that saw the provider's counters lives in the runner
        subprocess, so this process cannot hold the object — the same split that keeps credentials out
        of workers puts the ledger beyond reach. What the gateway *did* persist is its own
        `ledger.snapshot()` on every `llm.response`, and the bus writes those to the run's
        `trace.jsonl`. Reading the last one is therefore reading the real ledger's numbers rather than
        re-deriving them; `None` means no readable trace, which is "unknown" and never a zero.

        The keys are the ledger's own vocabulary — `total_usd`, `total_tokens`, `calls`,
        `unknown_cost_calls`, `cost_complete` — so a caller cannot ask for a fact under a name the
        ledger never wrote (`remaining_tokens`, `nodes`, `runs`). `cost_complete` is False whenever a
        call was unmeasured, which makes `total_usd` a floor; that distinction is the reason this
        method exists rather than a bare number.
        """
        resolved = run or self._run
        if resolved is None:
            return None
        trace_path = getattr(getattr(resolved, "workspace", None), "trace_path", None)
        if trace_path is None:
            return None
        path = Path(trace_path)
        try:
            if not path.is_file():
                return None
            from .bus import load_trace

            events = load_trace(path)
        except Exception:  # noqa: BLE001 - an unreadable trace is "unknown", not zero spend
            return None
        for event in reversed(events):
            if getattr(event, "type_value", "") != EventType.LLM_RESPONSE.value:
                continue
            payload = getattr(event, "payload", None) or {}
            budget = payload.get("budget") if isinstance(payload, dict) else None
            if isinstance(budget, dict):
                return budget
        # A readable trace with no model call: the run genuinely spent nothing, and that is a fact
        # rather than a gap. Stated with explicit zeros so it stays distinguishable from `None`.
        return {"total_usd": 0.0, "total_tokens": 0, "calls": 0,
                "unknown_cost_calls": 0, "cost_complete": True}

    def _round_spend(self, run: Run) -> tuple[int, int, float | None, int]:
        """What the round just finished spent, from the ledger the spending process kept.

        Read from that ledger rather than estimated: an accounting figure the engine invented would be
        worse than no figure, because it would be believed. The figure comes from the run's trace (see
        :meth:`cost_snapshot`) and the *round's* share is the cumulative total minus what the goal has
        already recorded, because `Goal.record_round` accumulates.

        Returns `(tokens, requests, cost_usd, unknown_cost_calls)`. `cost_usd` is `None` when the run
        left no readable trace, which the caller records as unreported rather than as `$0.00`; a
        readable run that made no call returns a genuine zero. `unknown_cost_calls` is the ledger's
        count of calls whose cost the provider never reported, diffed to this round, so a partly
        measured round reports its floor *and* the fact that it is one.
        """
        snapshot = self.cost_snapshot(run)
        if snapshot is None:
            return 0, 0, None, 0
        recorded = getattr(self._goal, "spend", None) if self._goal is not None else None
        already_tokens = int(recorded.tokens) if recorded is not None else 0
        already_requests = int(recorded.requests) if recorded is not None else 0
        already_cost = float(recorded.cost_usd) if recorded is not None else 0.0
        already_unknown = int(recorded.unknown_cost_calls) if recorded is not None else 0
        # A trace that reset underneath a resumed goal can read lower than what was recorded; clamping
        # at zero reports no new spend rather than a negative one the caller would then subtract.
        tokens = max(0, int(snapshot.get("total_tokens") or 0) - already_tokens)
        requests = max(0, int(snapshot.get("calls") or 0) - already_requests)
        cost = max(0.0, float(snapshot.get("total_usd") or 0.0) - already_cost)
        unknown = max(0, int(snapshot.get("unknown_cost_calls") or 0) - already_unknown)
        return tokens, requests, cost, unknown

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

        It is a backstop *only* because a round that advances nothing now ends the loop itself
        (`_runner_state_signature`). Measured before that check existed: 361 no-op rounds in under four
        minutes, each spawning a runner process, all of them bounded by this number alone — so for a
        finished graph this was not the backstop, it was the practical limit, and the "done" the run
        had reached was never reported.
        """
        raw = getattr(getattr(self.config, "goal", None), "max_rounds", None)
        return max(1, int(raw)) if raw else 10_000

    def _runner_state_signature(self, run: Run) -> str:
        """A digest of what a *round* can advance, so a round that advanced nothing is visible.

        The idle driver decides whether to continue by resuming the run and looking at what came
        back. A resume against a finished checkpoint is *indistinguishable from progress* by its exit
        status: the runner returns `complete` in ~0.3s having run nothing, which is exactly what a
        round that did real work also reports. So "did this round do anything" cannot be read off the
        outcome, and it cannot be read off the clock either (a slow no-op and a fast node are the same
        shape of report).

        It can be read off the checkpoint. This is the state a node's work changes and nothing else
        does — the node records, the phase, the artifact index, the reroute book. `log`, `handoff`,
        `node` and `_pass_stamps` are deliberately excluded: they are written by bookkeeping that runs
        whether or not work happened, and including them would make every round look like progress,
        which is the defect this exists to catch.

        Returns `""` when there is no readable checkpoint, which never equals a real signature — a
        missing state file is a run that has not started, not one that finished, and treating it as
        "nothing changed" would stop a loop that never began.
        """
        state_path = getattr(run.workspace, "runner_state_path", None)
        if state_path is None or not Path(state_path).is_file():
            return ""
        try:
            state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(state, dict):
            return ""
        budget = state.get("budget") if isinstance(state.get("budget"), dict) else {}
        material = {
            "nodes": state.get("nodes"),
            "phase": state.get("phase"),
            "artifacts": state.get("artifacts"),
            "reroutes": state.get("reroutes"),
            "steps_used": budget.get("steps_used"),
            "iterations": budget.get("iterations"),
        }
        return json.dumps(material, sort_keys=True, default=str)

    # ── the goal API ────────────────────────────────────────────────────────

    def goal(self) -> Goal | None:
        """The active goal, or None when this workspace has none."""
        return self._goal

    def goal_status(self) -> dict[str, Any]:
        """The goal picture for the console and the CLI."""
        if self._goal is None:
            default = self._default_goal_policy()
            return {"objective": "", "state": GoalState.CLEARED.value, "live": False, "open": False,
                    "budget_enabled": False, "token_budget": 0, "pause_reason": "",
                    "summary": "", "blocked_reason": "", "slice": {}, "spend": {}, "history": [],
                    # Even with no objective set, the posture the *next* goal will inherit is reported,
                    # so the console renders one consistent control instead of an empty state that
                    # looks like a different feature.
                    "posture": default.posture.value, "unattended": default.unattended,
                    "decides_gates": default.auto_approve, "staffs_gaps": default.auto_hire}
        return self._goal.public()

    def goal_set(self, objective: str, *, armed: bool = True, by: str = "cli",
                 policy: Any = None) -> Goal:
        """Set the objective, and optionally arm it immediately.

        Arming is the deliberate act that begins spending, so `goal set` on an already-open objective
        replaces it only when asked — otherwise an in-flight objective would be silently overwritten.

        `policy` is the goal's autonomy: whether it may pass gates, release its terminal gate, and
        staff its own gaps. Omitting it uses the configured default (unattended), so the common case
        needs no argument; passing `GoalPolicy(posture=Posture.SUPERVISED)` is the "I want to be
        involved" choice, stated per goal.
        """
        from .goal import GoalPolicy

        if policy is None:
            policy = self._default_goal_policy()
        elif isinstance(policy, dict):
            policy = GoalPolicy.from_dict(policy)

        existing = self._goal
        if (existing is not None and existing.state.is_open
                and existing.objective == objective.strip()
                and existing.policy.as_dict() == policy.as_dict()):
            goal = existing
        else:
            budget = getattr(getattr(self.config, "goal", None), "token_budget", 0)
            goal = Goal.new(objective, token_budget=int(budget or 0), policy=policy)
            if existing is not None:
                goal.history = list(existing.history)[-20:]
        self._goal = goal
        if armed:
            goal.arm(by=by)
            self._emit(EventType.GOAL_ARMED, {"objective": goal.objective, "by": by,
                                              "token_budget": goal.token_budget,
                                              "policy": goal.policy.as_dict()})
        goal.save(self.workspace)
        return goal

    def _default_goal_policy(self) -> Any:
        """The autonomy a new goal gets from configuration, in one place.

        `goal.default_posture` is the headline setting, and `goal.auto_pass_auto_gates` /
        `goal.auto_hire_missing` are the narrower switches beneath it. A goal inherits all three at
        creation and may then overrule them per objective. Inheriting rather than consulting the
        config live is deliberate: a goal's authority should not change under it because someone
        edited credentials.json mid-run.
        """
        from .goal import GoalPolicy, Posture

        cfg = getattr(self.config, "goal", None)
        posture = str(getattr(cfg, "default_posture", "unattended") or "unattended").strip().lower()
        return GoalPolicy(
            auto_approve=bool(getattr(cfg, "auto_pass_auto_gates", True)),
            auto_hire=bool(getattr(cfg, "auto_hire_missing", True)),
            persist_hires=bool(getattr(cfg, "persist_auto_hires", False)),
            posture=Posture.SUPERVISED if posture == "supervised" else Posture.UNATTENDED,
        )

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

    # ── the mission API ─────────────────────────────────────────────────────
    #
    # A Mission is the *why* above the Goal. It owns an ordered list of objectives and activates one
    # at a time; it deliberately does not plan, route or spend (see `mission.py`). The orchestrator's
    # job here is only to load it, persist it, and connect one action — `mission start` — to the
    # existing goal machinery, so "work this objective" is a goal whose objective is that step.

    def mission(self) -> Mission | None:
        """The active mission, or None when this workspace has none."""
        return self._mission

    def mission_status(self) -> dict[str, Any]:
        """The mission picture for the console and the CLI, in one call."""
        if self._mission is None:
            return {"statement": "", "state": MissionState.EMPTY.value, "live": False,
                    "armed": False, "pause_reason": "", "counts": {"total": 0},
                    "progress": {"done": 0, "total": 0, "fraction": 0.0, "next": ""},
                    "now": None, "objectives": [], "history": []}
        return self._mission.public()

    def mission_set(self, statement: str, *, objectives: Iterable[str] = (),
                    armed: bool = False, by: str = "cli") -> Mission:
        """Create the mission, replacing an empty/cleared one.

        Refuses to clobber a mission that is still open unless it is finished — replacing a live
        mission would silently drop objectives someone is working.
        """
        existing = self._mission
        if existing is not None and existing.statement and not existing.state().terminal:
            if existing.statement.strip() != statement.strip():
                raise MissionError(
                    "this workspace already has an open mission; `mission clear` it first, or "
                    "add objectives to the existing one"
                )
            mission = existing
        else:
            mission = Mission.new(statement)
            if existing is not None:
                mission.history = list(existing.history)[-20:]
        for text in objectives:
            try:
                mission.add_objective(str(text))
            except MissionError:
                continue  # a duplicate objective in the initial list is not worth refusing the set
        if armed and mission.objectives:
            mission.arm(by=by)
        self._mission = mission
        mission.save(self.workspace)
        self._emit(EventType.AGENT_LOG, {
            "stream": "stdout", "text": f"mission set: {mission.statement[:120]}"})
        return mission

    def mission_add(self, text: str, *, at: int | None = None) -> Mission:
        mission = self._require_mission()
        mission.add_objective(text, at=at)
        mission.save(self.workspace)
        return mission

    def mission_remove(self, index: int) -> Mission:
        mission = self._require_mission()
        mission.remove_objective(index)
        mission.save(self.workspace)
        return mission

    def mission_arm(self, *, by: str = "cli") -> Mission:
        mission = self._require_mission()
        mission.arm(by=by)
        mission.save(self.workspace)
        self._emit(EventType.AGENT_LOG, {
            "stream": "stdout",
            "text": f"mission armed: {mission.statement[:120]}"})
        return mission

    def mission_pause(self, *, reason: str = "manual") -> Mission:
        mission = self._require_mission()
        mission.pause(reason=reason)
        mission.save(self.workspace)
        return mission

    def mission_advance(self, *, summary: str = "") -> Mission:
        """Finish the active objective, activate the next, and return the mission.

        This does not start work — it moves the cursor. Use `mission start` to hand the active
        objective to a goal.
        """
        mission = self._require_mission()
        mission.advance(summary=summary)
        mission.save(self.workspace)
        return mission

    def mission_mark(self, index: int, state: ObjectiveState | str, *,
                     summary: str = "", run_id: str = "", goal_id: str = "") -> Mission:
        mission = self._require_mission()
        mission.mark(index, state, summary=summary, run_id=run_id, goal_id=goal_id)
        mission.save(self.workspace)
        return mission

    def mission_clear(self) -> Mission:
        mission = self._require_mission()
        mission.clear()
        mission.save(self.workspace)
        return mission

    def mission_start(self, index: int | None = None, *, armed: bool = True,
                      by: str = "cli") -> dict[str, Any]:
        """Hand the active (or named) objective to a Goal, so the mission does real work.

        This is the one place the mission and the goal machinery meet, and the join is deliberately
        thin: it activates the objective, then sets a goal whose objective *is* that step's text. The
        mission does not arm anything itself — the caller's ``armed`` decides, exactly as `goal set`
        does — so the spend decision stays in one place.
        """
        mission = self._require_mission()
        if index is not None:
            mission.activate(index)
        objective = mission.objective_now() or mission.activate()
        if objective is None:
            raise MissionError(
                "this mission has no pending objective to start; add one, or `mission advance`")
        active_index = mission.active_index() or 0
        # Set the goal first, so its text can be recorded on the objective as the linkage from the
        # step to the goal (and, through the run, to the artifacts).
        goal = self.goal_set(objective.text, armed=armed, by=by)
        mission.mark(active_index, ObjectiveState.ACTIVE,
                     goal_id=str(getattr(goal, "objective", "") or objective.text))
        mission.save(self.workspace)
        return {"mission": mission.public(), "goal": self.goal_status(),
                "objective": objective.as_dict()}

    def mission_sync(self) -> Mission | None:
        """Reconcile the active objective with the goal's own verdict, when a goal has one.

        Called after a run settles: if the active objective's goal reported complete or blocked, the
        objective follows it, and the mission advances. This is what makes a mission progress *without*
        a person clicking through — the autonomy the hierarchy is for. It is conservative: only a
        goal's own explicit verdict moves an objective, never a heuristic.
        """
        mission = self._mission
        goal = self._goal
        if mission is None or goal is None:
            return mission
        index = mission.active_index()
        if index is None:
            return mission
        objective = mission.objectives[index]
        if goal.state is GoalState.COMPLETED and objective.state is ObjectiveState.ACTIVE:
            mission.mark(index, ObjectiveState.DONE, summary=goal.summary or "goal reported complete")
            mission.advance()
            mission.save(self.workspace)
            self._emit(EventType.AGENT_LOG, {
                "stream": "stdout",
                "text": f"mission advanced: {objective.text[:80]} done"})
        elif goal.state is GoalState.BLOCKED and objective.state is ObjectiveState.ACTIVE:
            mission.mark(index, ObjectiveState.BLOCKED,
                         summary=goal.blocked_reason or "goal reported blocked")
            mission.save(self.workspace)
            self._emit(EventType.AGENT_LOG, {
                "stream": "stdout",
                "text": f"mission blocked at: {objective.text[:80]}"})
        return mission

    def _require_mission(self) -> Mission:
        if self._mission is None:
            raise OrchestratorError("no mission is set for this workspace; use `mission set` first")
        return self._mission

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
            # Keep the per-node `summary` and `iterations` too, not just status/verdict. When a node
            # blocks, the summary is *why* — dropping it left a run that said `blocked` and nothing
            # else, which is the single most confusing state the product can be in.
            "nodes": {
                name: {
                    "status": rec.get("status"),
                    "verdict": rec.get("verdict"),
                    "summary": rec.get("summary") or rec.get("detail") or "",
                    "iterations": rec.get("iterations"),
                    "cost_usd": rec.get("cost_usd"),
                }
                for name, rec in (state.get("nodes") or {}).items()
            },
            "artifacts": sorted((state.get("artifacts") or {}).keys()),
            "open_questions": state.get("open_questions") or [],
            # The runner's own log tail travels with the run so the reason for a stop is recoverable
            # from the checkpoint alone, without re-reading `run_state.json` by hand.
            "log_tail": (state.get("log") or [])[-20:],
        })
        run.stop_reason = _derive_stop_reason(state, outcome, run.outcome)

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
        """Find a gate the run reached and is waiting on.

        A gate node that has already been *decided* is not waiting on anything, and saying otherwise
        makes a released run unresumable: the runner keeps the gate's verdict (`awaiting_owner`) even
        after the release marks its status `done`, so a detector keyed on the verdict alone re-parked
        the run on the very gate that had just been approved, for ever. `done` is therefore checked
        first — it is the state a decided node reaches, and no pending decision can carry it.

        **A stalled node is not a gate.** This detector used to mint a human gate from any node whose
        status was `needs_review`, and the manifest was consulted only for the *wording* of a gate it
        had already decided to raise. So a node that failed a handoff rule — `R6: 9 open questions
        exceed the 3 ceiling`, a fixable payload problem — parked a run that had `--posture unattended`
        at node one, with every downstream node still `pending`.

        The two readings are separated by which one carries the declaration:

        - `verdict == "awaiting_owner"` **is** the declaration. The executor emits it only after
          reading the manifest's `kind: human` for that node, so it parks whatever the manifest says
          now — requiring a re-read would make a decided gate depend on a file being present.
        - `status == "needs_review"` is ambiguous on its own: a model saying "I am not done", an
          uncovered criterion, and a refused handoff all produce it. It parks when the manifest
          declares the node a gate, **or** when the run has genuinely stopped there and this node is
          the reason — see `_stalled_node`. While the run is still moving, a refused node is being
          repaired, and parking on it would gate a run that is about to fix itself.
        """
        for name, record in (state.get("nodes") or {}).items():
            if not isinstance(record, dict):
                continue
            status = str(record.get("status") or "")
            verdict = str(record.get("verdict") or "")
            if status == "done":
                continue
            declaration = self._gate_declaration(name)
            awaiting_owner = verdict == "awaiting_owner"
            if awaiting_owner or (status == "needs_review"
                                  and (declaration is not None or self._stalled_node(state, name))):
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

    def _stalled_node(self, state: dict[str, Any], node: str) -> bool:
        """Whether the run has genuinely stopped at `node`, rather than still working through it.

        This is the line between "the run needs a person" and "the run is mid-repair". A refused node
        is `needs_review` in *both* cases — the runner keeps the failed attempt in its checkpoint while
        the bounded rework retries it — so the status alone cannot tell them apart. The runner's own
        phase can: `execute` is its word for "a node is running", and it stays `execute` for the whole
        of a rework window, which is exactly the period during which this node must not gate anything.

        The node id is not consulted, and that is deliberate rather than an omission. What the check
        answers is "has the run stopped", and the caller has already established that *this* node is
        the not-done one; when the frontier empties there is exactly one such node, which is the node
        the original code parked on. Reading the node's own `max_iterations` instead would let a
        field about the payload decide a question about the graph, and it is usually `1` here.

        The effect is the behaviour that had to be preserved: a refusal that ends the run still parks
        as a human gate, named for the node, exactly as it did before — while a refusal the rework is
        still working through does not gate anything.

        The phase answers it alone, because `execute` is set by the runner's own traversal loop and
        is never restored to anything else by a rework pass — so a checkpoint read mid-node is the
        one case with no stop to report, whatever the node's record says.
        """
        return str(state.get("phase") or "") != "execute"

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

    def decide(self, approved: bool, *, run: Run | None = None, note: str = "",
               by: str = "owner") -> Run:
        """Resolve a gate: approve and continue, or reject and park.

        A rejection requires no reason from the Owner but records one if given, because a rejection the
        agents cannot read is one they will re-attempt identically.

        `by` names who decided. It is `owner` for a person and `goal` for an auto-approval the goal's
        policy authorised — recorded distinctly, because "the org passed this" and "you passed this"
        are different facts about a run, and conflating them would make the audit trail lie.
        """
        run = self._resolve(run)
        gate = run.gate
        if gate is None:
            raise OrchestratorError("there is no gate to decide; the run is not waiting on one")

        run.decisions.append({
            "gate_id": gate.gate_id, "approved": approved, "note": note,
            "by": by, "at": _iso_now(),
        })
        self._emit(EventType.HUMAN_DECISION, {
            "run_id": run.run_id, "gate_id": gate.gate_id, "approved": approved,
            "note": note, "kind": gate.kind, "by": by})

        if approved:
            if note:
                # An approval note is guidance, so it becomes an instruction for the next nodes.
                run.instructions.append(note)
            run.gate = None
            run.phase = RunPhase.READY
            self.diagnostics.info("human.decision", message=f"approved {gate.gate_id}",
                                  detail={"by": by})
            # Move the runner's checkpoint past the node that parked, so continuing ADVANCES instead
            # of parking again. See `_release_node_for_resume` for why this is needed at all.
            self._release_node_for_resume(run, gate)
        else:
            run.phase = RunPhase.PAUSED
            if note:
                run.instructions.append(f"Owner rejected at {gate.gate_id}: {note}")
            self.diagnostics.warning("human.decision",
                                     message=f"rejected {gate.gate_id}: {note}")
        run.touch()
        self._persist(run)
        return run

    def _release_node_for_resume(self, run: Run, gate: GateRequest) -> None:
        """Advance the runner's checkpoint past an approved node, so continuing moves forward.

        This is the last link in the chain that made gates unresolvable, and it is worth stating in
        full because three separate defects hid behind one symptom.

        The library runner seeds its frontier from `start`: `active = [] if start in done else [start]`.
        When the *start* node parks at a gate, approving it clears the gate but leaves that node
        `needs_review` in the checkpoint — which the runner counts as terminal — so on resume its
        frontier is empty, every successor stays `pending`, and it re-escalates. Approving again does
        the same thing. The gate is resolvable and the run can never move: a real approval did exactly
        this, on the very first node of a six-node graph.

        So on approval the engine does what a person means by "continue": it marks the node the gate
        named as `done` and points `start` at that node's successor, which is the frontier the runner
        could not compute for itself. A gate that names no node (a policy escalation) is left alone.

        `done` rather than `needs_review` is the operative word: the Owner has accepted the work, so
        downstream nodes should treat it as available rather than as an unresolved question.
        """
        node_id = str(gate.dossier.get("node") or "") if isinstance(gate.dossier, dict) else ""
        if not node_id:
            node_id = str(gate.gate_id)
        state_path = getattr(run.workspace, "runner_state_path", None)
        if state_path is None or not Path(state_path).is_file():
            return
        try:
            state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        record = (state.get("nodes") or {}).get(node_id)
        if not isinstance(record, dict):
            # The gate names something the runner never recorded (an escalation, or a gate node
            # itself). Nothing to advance, and inventing a node would corrupt the checkpoint.
            return
        if record.get("status") == "done":
            return

        # Which node did the Owner effectively release? The producer whose work the gate judged.
        manifest_path = run.manifest_path
        successor = ""
        if manifest_path is not None and Path(manifest_path).is_file():
            manifest = self._read_manifest(Path(manifest_path))
            for edge in manifest.get("edges") or []:
                if str(edge.get("from")) == node_id:
                    candidate = str(edge.get("to") or "")
                    if candidate:
                        successor = candidate
                        break
        record["status"] = "done"
        record["verdict"] = record.get("verdict") or "approved"
        state["phase"] = "ready"
        state["node"] = successor or node_id
        try:
            Path(state_path).write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 - a failed release is reported, not fatal
            self.diagnostics.warning("gate.release.failed", message=str(exc))
            return
        # Advance the manifest past the released node. The runner's frontier is `[start]` unless
        # `start` is terminal, and `start` comes from the **manifest** — never from run-state — so
        # without this the run re-parks on the node the Owner just accepted, for ever.
        if successor and manifest_path is not None and Path(manifest_path).is_file():
            self._advance_manifest_past(Path(manifest_path), released=node_id, successor=successor)
        self.diagnostics.info(
            "gate.released",
            message=f"released {node_id} on approval; resuming at {successor or '(end)'}",
            detail={"gate": gate.gate_id, "by": "owner", "successor": successor})

    def _advance_manifest_past(self, path: Path, *, released: str, successor: str) -> None:
        """Rewrite a manifest so a resumed run begins at the released node's successor.

        The library runner seeds its frontier from the manifest's `start` and only skips it when that
        node is terminal — so the single way to move past an accepted node is to move `start`, and the
        released node must then be **removed** from the graph. Leaving it in fails the library's own
        validator with `nodes not reachable from start`, which surfaces as `the runner exited 1` and
        looks like a crash rather than a refusal. Pruning it is not a loss: the node is finished, its
        artifact is recorded, and the remaining graph is exactly the work still to do.

        This parses and re-emits rather than editing lines, because the graph is a *structure* and a
        line-oriented edit has to re-derive it: an earlier textual version dropped the released node's
        edge block by scanning for the next `- from:`, which does not exist when the released node was
        the last edge — so the scan ran past the `edges:` list into `end:`, `loops:` and `gates:`,
        truncating the manifest. The result was a graph with a `start` that no edge could reach and a
        `gates:` header with nothing under it. Re-emitting is also what every other manifest write in
        this engine does, so the rewritten file is the same shape as the planned one.

        Whatever comes out is validated by the library's own validator on the next spawn, so a bad
        rewrite is refused loudly rather than executed.
        """
        try:
            manifest = self._read_manifest(path)
        except Exception as exc:  # noqa: BLE001 - an unreadable manifest is not rewritable
            self.diagnostics.warning("gate.manifest.rewrite.unreadable", message=str(exc))
            return
        if not isinstance(manifest, dict):
            return

        gates = [g for g in (manifest.get("gates") or [])
                 if str(g.get("id")) != released]
        nodes = [n for n in (manifest.get("nodes") or [])
                 if str(n.get("id")) != released]
        # Both directions: an edge *into* the released node has nothing left to deliver to, and one
        # *out of* it describes a handoff that has already been recorded on the released node.
        edges = [e for e in (manifest.get("edges") or [])
                 if str(e.get("from")) != released and str(e.get("to")) != released]
        ends = [e for e in (manifest.get("end") or []) if str(e) != released]

        # Prune everything `successor` can no longer reach. Removing the released node orphans its
        # ancestors, and the library's own validator refuses a graph with unreachable nodes — which
        # surfaced as `the runner exited 1`, a message that says nothing about the real cause. Walking
        # the remaining edges forward from `successor` keeps exactly the work still to do.
        #
        # Gates are seeded into the reachable set rather than derived from it. A gate is a graph
        # *endpoint*: the composed plan lists it in `nodes:` as well, but a hand-written manifest like
        # the gate-probe fixture declares it only under `gates:`, with no edge leaving it. Treating
        # "no outgoing edge" as "unreachable" would delete the very gate the release just decided.
        reachable: set[str] = {successor}
        reachable |= {str(g.get("id")) for g in gates}
        frontier = [successor]
        adjacency: dict[str, list[str]] = {}
        for edge in edges:
            adjacency.setdefault(str(edge.get("from")), []).append(str(edge.get("to")))
        while frontier:
            current = frontier.pop()
            for nxt in adjacency.get(current, []):
                if nxt not in reachable:
                    reachable.add(nxt)
                    frontier.append(nxt)

        declared = {str(n.get("id")) for n in nodes} | {str(g.get("id")) for g in gates}
        if successor not in declared:
            self.diagnostics.warning(
                "gate.manifest.rewrite.skipped",
                message=f"cannot advance past {released}: successor {successor!r} is not declared "
                        "in the remaining graph")
            return

        pruned_nodes = [n for n in nodes if str(n.get("id")) in reachable]
        pruned_edges = [e for e in edges
                        if str(e.get("from")) in reachable and str(e.get("to")) in reachable]
        kept_gates = [g for g in gates if str(g.get("id")) in reachable]

        rewritten = dict(manifest)
        rewritten["start"] = successor
        rewritten["nodes"] = pruned_nodes
        rewritten["edges"] = pruned_edges
        rewritten["end"] = [e for e in ends if str(e) in reachable] or [successor]
        if "gates" in manifest:
            rewritten["gates"] = kept_gates
        # A loop naming the released node would re-enter a node that is no longer in the graph.
        if rewritten.get("loops"):
            kept_loops = []
            for loop in rewritten["loops"]:
                members = [str(n) for n in (loop.get("nodes") or [])
                           if str(n) in reachable and str(n) != released]
                if not members:
                    continue
                trimmed = dict(loop)
                trimmed["nodes"] = members
                kept_loops.append(trimmed)
            rewritten["loops"] = kept_loops

        header = (
            f"# `start` moved to {successor} after {released} was approved at a gate, and the\n"
            f"# finished node was pruned. The library runner seeds its frontier from `start` and\n"
            f"# only skips a terminal node, so this is how a resumed run advances past an accepted\n"
            f"# node instead of parking on it again; pruning keeps the graph reachable for the\n"
            f"# library's own validator.\n")
        try:
            path.write_text(header + emit_safe_yaml(rewritten), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001
            self.diagnostics.warning("gate.manifest.rewrite.failed", message=str(exc))

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
        elif getattr(self.workspace, "is_attached", False):
            # An *attached* workspace IS the project: its slug is the folder name, and the run's slug
            # is the workflow's. Looking up `root/slug` would point at a managed directory beside the
            # repository — which does not exist — so `load` returned None and `decide`, `status` and
            # `flow` all reported "no run found" for a run that was sitting right there. A real
            # `decide --approve` failed on exactly this, leaving a gate that could not be resolved.
            project = self.workspace
        else:
            # Resolve the slug under *this orchestrator's* root, not the default projects directory —
            # otherwise a run in a custom root would appear not to exist.
            project = (self.workspace if slug == self.workspace.slug
                       else Workspace.for_project(slug, root=self.workspace.root))
        raw = project.read_checkpoint_raw()
        if raw is None:
            return None
        if _is_runner_checkpoint(raw):
            # The file at `run_state.json` is the **library runner's** per-node checkpoint, not the
            # orchestrator's, so there is no orchestrator run here to resume.
            #
            # This is a legacy-workspace case and it used to crash. Both sides wrote their own shape to
            # this one file and the runner wrote last, so on a workspace that ran before the two files
            # were separated (see `Workspace.runner_state_path`) the orchestrator's checkpoint is simply
            # gone — replaced by the runner's. `read_checkpoint_raw` then returned the runner's
            # `{workflow, manifest_sha, nodes, …}`, which carries no `run_state_version`, and the schema
            # registry refused it with "no migration path for run_state from 0.0.0 to 1.0.0". That
            # surfaced as a hard error in `status`, `decide` and `instruct` — `status` crashed outright
            # — on a run whose node outcomes the Flow and Activity panels were still showing happily,
            # because they read the file directly.
            #
            # Returning None rather than raising is the honest answer: this is not our document, so it is
            # not a schema violation to report. The run's outcomes stay visible through `flow`,
            # `activity` and the memory store, which is where the UI reads them.
            self.diagnostics.info(
                "run.checkpoint.foreign",
                message=(f"{project.slug}: run_state.json is the workflow runner's checkpoint, not a "
                         "resumable orchestrator run; showing it from flow/activity instead"))
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
            return {"phase": "idle", "running": False, "goal": self.goal_status(),
                    "mission": self.mission_status()}
        with self._lock:
            host = self._host
        return {
            **run.as_dict(),
            # The run's own goal text, kept under a distinct key because `goal` below is the
            # *goal-status* document the app renders (state, live, spend) — the two are different
            # things and collapsing them made `status` print a dict where the objective belonged.
            "run_goal": run.goal,
            "running": host is not None and host.running,
            "liveness": host.wedged() if host is not None else None,
            "org": self.org.roster_view(),
            "policy": self.policy.effective(),
            "cost": self.telemetry.sli_rollup(),
            # The goal travels with status, so the console shows whether the loop will continue
            # without needing a second command it has to remember to send.
            "goal": self.goal_status(),
            # The mission travels too, so the console shows the standing purpose and which step is
            # active without a second round trip.
            "mission": self.mission_status(),
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
            # The *host* reports through the event bus, which writes the run's trace — not through
            # `Diagnostics`, which is the other file a person reads. So a run the watchdog stopped
            # left `diagnostics.jsonl` holding `run.prepared`, a `node.bind` and `run.phase: aborted`
            # and nothing that said a watchdog had fired at all: the one channel that is supposed to
            # explain a run was the one channel the explanation never reached. Recorded here, and
            # warning-level because every one of these is a run being stopped and nobody is watching.
            if event in ("watchdog.stall", "run.terminating", "watchdog.restart",
                         "run.drain_incomplete"):
                self.diagnostics.warning(
                    event, message=str(payload.get("detail") or payload.get("action")
                                      or payload.get("cause") or ""),
                    detail=dict(payload), run_id=run.run_id)
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
        """Record the phase transition, which is what the UI timeline reads.

        The termination cause travels with it. `run.phase: aborted` alone is the record that sent a
        person to the checkpoint to guess; the cause and its detail are what turn that line into an
        answer — and for a stall, the window that expired is in the detail.
        """
        level = "warning" if run.phase in (RunPhase.ABORTED, RunPhase.FAILED) else "info"
        self.diagnostics.log("run.phase", level=level, message=f"{run.slug} is {run.phase.value}",
                             detail={"outcome": run.outcome.get("outcome"),
                                     "termination": run.outcome.get("termination"),
                                     "termination_detail": run.outcome.get("termination_detail"),
                                     "stop_reason": run.stop_reason,
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
        prompt overflow, while assuming a small one only compacts earlier. The window comes from the
        one resolved default (including a `defaults.context_window` override), so the built-in company
        and a hire agree on it.
        """
        spec = self.config.default_model_spec()
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
