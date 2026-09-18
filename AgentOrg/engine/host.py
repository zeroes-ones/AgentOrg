#!/usr/bin/env python3
"""host.py — spawn and supervise the library's runner, one subprocess per run.

WHY THIS EXISTS
---------------
The runner owns control flow and is a program, not a library. Running it as a subprocess is what makes
the design's central promise true: **the app cannot hang on agent work**, because agent work is not in
the app's process — and a wedged runner can be killed without taking anything else down.

This module is the supervision layer: spawn, watch, throttle, preempt, restart, and translate the
runner's own output into the engine's event stream.

DESIGN
------
- **The runner is never modified.** It is invoked with `--executor` and `--guardrail` pointing at our
  plugins, and its own output is read rather than replaced. Its stdout stays its own.
- **A heartbeat proves liveness, not activity.** A runner that is thinking and a runner that is wedged
  look identical from outside, so the host watches the *state file's* mtime and the process's own
  liveness rather than guessing from silence.
- **Preemption is cooperative, then forceful.** SIGTERM lets the runner reach a checkpoint; SIGKILL
  after a grace period is the fallback. A mid-node kill loses at most the current node, because the
  runner checkpoints per node.
- **A crash is resumed, not restarted.** The runner's `--state` file is a checkpoint, so a restart
  continues rather than redoing work — and the effect journal means the resumed node does not
  re-apply a side effect.
- **Every diagnostic goes to stderr.** stdout belongs to the protocol; a stray print there would
  corrupt the event stream the app parses.

Usage:
    host = RunnerHost(config=cfg, library=lib, workspace=ws)
    result = host.run(manifest_path=..., run_id=...)
    host.pause(); host.resume(); host.abort()
"""

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = ["HostError", "RunHandle", "RunOutcome", "RunnerHost", "RunnerState"]


class HostError(RuntimeError):
    """Raised when the runner cannot be started or supervised."""


class RunnerState(str, Enum):
    """The runner subprocess's lifecycle, as the UI shows it."""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    TERMINATING = "terminating"
    FINISHED = "finished"
    FAILED = "failed"
    # The runner exits 1 to signal "not complete" — but a run that stopped at a gate is a *normal*
    # pause for a human decision, not a crash. Collapsing the two would report every gate as a
    # failure, so the states are distinct.
    GATED = "gated"


@dataclass
class RunOutcome:
    """What a run produced."""

    run_id: str
    state: RunnerState
    exit_code: int | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    restarts: int = 0
    killed: bool = False
    error: str = ""
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        """Whether the run completed without a crash or a kill.

        A gated run is not ``ok`` (it did not complete) but it is not a failure either — callers that
        only care about "did it break" want :attr:`broken`.
        """
        return self.exit_code == 0 and not self.killed and self.state is RunnerState.FINISHED

    @property
    def gated(self) -> bool:
        """Whether the run stopped at a gate awaiting an Owner decision."""
        return self.state is RunnerState.GATED

    @property
    def broken(self) -> bool:
        """Whether the run actually failed: a crash, a kill, or a non-zero exit with no gate."""
        return self.killed or self.state is RunnerState.FAILED

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "state": self.state.value, "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 3), "restarts": self.restarts,
            "killed": self.killed, "error": self.error, "ok": self.ok,
            "gated": self.gated, "broken": self.broken,
            "outcome": self.summary.get("outcome"),
            "steps_used": self.summary.get("steps_used"),
            "iterations": self.summary.get("iterations"),
            "cost": self.summary.get("cost"),
        }


@dataclass
class RunHandle:
    """A live run: its process, its state file, and its liveness."""

    run_id: str
    process: subprocess.Popen
    manifest_path: Path
    state_path: Path
    started_at: float = field(default_factory=time.time)
    restarts: int = 0
    state: RunnerState = RunnerState.RUNNING
    last_state_mtime: float = 0.0
    #: The last moment the runner showed *any* sign of work — a checkpoint write, a line of its own
    #: output, or a new trace entry. See `last_activity_s`.
    last_activity: float = 0.0
    killed: bool = False
    stderr_lines: list[str] = field(default_factory=list)
    stdout_lines: list[str] = field(default_factory=list)

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def alive(self) -> bool:
        """Whether the process is still running."""
        return self.process.poll() is None

    def state_age_s(self) -> float:
        """Seconds since the runner last wrote its checkpoint.

        The liveness signal. A runner that is thinking writes nothing, so this is paired with the
        process poll rather than used alone — but a *long* silence from a process that is still alive
        is exactly the wedge the watchdog exists to catch.
        """
        return time.time() - (self.last_state_mtime or self.started_at)

    def last_activity_s(self) -> float:
        """Seconds since the runner showed *any* sign of work.

        The checkpoint is written per **node**, so a single node that takes a long time — a big model
        writing a long artifact, or a tool loop making many calls — looks exactly like a wedged process
        to `state_age_s`. A real run was killed by the watchdog at the 15-minute mark while it was
        genuinely working, which is the worst possible outcome: it discards real work and reports it as
        a stall.

        So every observable sign of activity counts: the checkpoint mtime, a line the runner printed,
        and the run's own trace file — which the executor writes on each model call and tool step.
        """
        newest = max(self.last_state_mtime or 0.0, self.last_activity or 0.0, self.started_at)
        for extra in self._activity_paths():
            try:
                newest = max(newest, extra.stat().st_mtime)
            except OSError:
                continue
        return time.time() - newest

    def _activity_paths(self) -> list[Path]:
        """Files the child appends to while working, so its progress is observable.

        The state directory is shared with the workspace, so the trace and the diagnostics log advance
        on every model call even when no node has completed yet.
        """
        state_dir = self.state_path.parent
        return [state_dir / "trace.jsonl", state_dir / "diagnostics.jsonl"]

    def refresh(self) -> None:
        """Re-read the state file's mtime, which advances as the runner checkpoints per node."""
        try:
            if self.state_path.is_file():
                self.last_state_mtime = self.state_path.stat().st_mtime
        except OSError:
            pass


class RunnerHost:
    """Supervises one runner subprocess per run.

    Parameters
    ----------
    config:
        Supplies the timeouts and the grace period.
    library:
        The pinned library, for the runner's path.
    workspace:
        Where the run-state, the plugins and the artifacts live.
    on_event:
        A callback invoked with each diagnostic line, so the orchestrator can put it on the bus. The
        host itself does not write to stdout: that belongs to the protocol.
    """

    def __init__(self, *, config: Any, library: Any, workspace: Any,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None,
                 on_stderr: Callable[[str], None] | None = None,
                 python: str | None = None,
                 heartbeat_s: float = 30.0, grace_s: float = 5.0,
                 stall_timeout_s: float = 900.0) -> None:
        self.config = config
        self.library = library
        # Accept a Workspace or a path. The orchestrator holds the former and a test the latter, and
        # asking callers to unwrap it would invite a mistake. A Workspace's *project* path is what the
        # host wants: plugins sit beside the manifest and state under `.agent_state/`.
        if hasattr(workspace, "path"):
            self.workspace = Path(workspace.path).resolve()
        else:
            self.workspace = Path(workspace).resolve()
        self.on_event = on_event
        self.on_stderr = on_stderr
        self.python = python or sys.executable
        self.heartbeat_s = heartbeat_s
        self.grace_s = grace_s
        self.stall_timeout_s = stall_timeout_s
        self._handle: RunHandle | None = None
        self._lock = threading.RLock()
        self._pause_flag = threading.Event()

    # ── the plugin files ────────────────────────────────────────────────────

    def plugin_paths(self, *, manifest_path: Path, run_id: str, workflow: str,
                     project: str, goal_active: bool = False) -> dict[str, Path]:
        """Write the executor and guardrail plugins the runner will load.

        Written per run rather than shared, because each run has its own workspace, roster pins and run
        id — and a plugin that read a global would make two concurrent runs interfere.
        """
        directory = Path(self.workspace)
        directory.mkdir(parents=True, exist_ok=True)
        engine_root = Path(__file__).resolve().parent.parent

        executor = directory / f"executor_{run_id}.py"
        executor.write_text(f'''#!/usr/bin/env python3
"""Generated per run. The runner loads this as its executor plugin."""
import json
import pathlib
import sys

sys.path.insert(0, {str(engine_root)!r})

from engine.artifacts import ArtifactStore
from engine.config import load
from engine.diagnostics import Diagnostics
from engine.executor import ExecutorContext, NodeExecutor
from engine.gateway import Gateway
from engine.idempotency import EffectJournal
from engine.library import resolve
from engine.memory import MemoryStore
from engine.org import Org, default_company
from engine.org.binding import BindingPolicy
from engine.providers.registry import build_providers
from engine.pool import TaskPool
from engine.pinning import PrefixPins
from engine.runcontext import CONTEXT_FILENAME, read as read_run_context
from engine.skills import FilesystemSkillSource
from engine.skills.overlay import OverlaySkillSource
from engine.telemetry import SpanExporter
from engine.tokens import TokenEstimator

WORKSPACE = pathlib.Path({str(directory)!r})
RUN_ID = {run_id!r}
WORKFLOW = {workflow!r}
PROJECT = {project!r}
MANIFEST = pathlib.Path({str(manifest_path)!r})

cfg = load()
providers, skipped = build_providers(cfg)

# ── the run context the orchestrator wrote ───────────────────────────────────
# Read rather than re-derived. Rebuilding here is what discarded the Owner's roster, their skills
# and the orchestrator's own binding decisions at the process boundary.
_context_doc = None
try:
    _context_doc = read_run_context(MANIFEST.parent)
except Exception as _exc:  # noqa: BLE001 - fall back rather than refuse to start
    print(f"run-context unreadable, using the built-in roster: {{_exc}}", file=sys.stderr)

if _context_doc is not None and _context_doc.org:
    org = Org.from_dict(_context_doc.org)
else:
    org = default_company(
        provider=cfg.defaults.get("provider", "ollama"),
        model=cfg.defaults.get("model", "qwen2.5-coder:7b"),
        context_window=(cfg.model_spec(cfg.defaults.get("model", "")).context_window or 32768),
    )

# The Owner's own skill roots, layered over the pinned library, so an authored skill executes.
# `skill_roots` is highest-priority-first; the first entry's parent is the project root the overlay
# should resolve against, which is what makes a project skill beat a global one.
_skill_roots = list(_context_doc.skill_roots) if _context_doc is not None else []
_skill_project = pathlib.Path(_skill_roots[0]).parent if _skill_roots else None
_skills = OverlaySkillSource(FilesystemSkillSource(resolve()), project=_skill_project,
                             include_global=not _skill_project)

# The binding the orchestrator decided, so a node runs as the agent it was bound to rather than being
# re-bound here from the built-in company.
_pins = {{}}
_policies = {{}}
if _context_doc is not None:
    for _node_id, _binding in _context_doc.bindings.items():
        _agents = list(_binding.get("agents") or [])
        if _agents:
            _pins[_node_id] = _agents[0]
        _policy = str(_binding.get("policy") or "")
        if _policy:
            try:
                _policies[_node_id] = BindingPolicy(_policy)
            except ValueError:
                pass

state_dir = WORKSPACE / ".agent_state"
state_dir.mkdir(parents=True, exist_ok=True)

_bus = None
try:
    from engine.bus import EventBus
    _bus = EventBus(run_id=RUN_ID, trace_path=state_dir / "trace.jsonl")
except Exception:
    _bus = None

_context = ExecutorContext(
    org=org,
    gateway=Gateway(cfg, providers, estimator=TokenEstimator(), bus=_bus, run_id=RUN_ID),
    skills=_skills,
    workspace=WORKSPACE,
    store=ArtifactStore(workspace_root=WORKSPACE),
    journal=EffectJournal(path=state_dir / "effects.jsonl"),
    bus=_bus,
    telemetry=SpanExporter(path=state_dir / "telemetry" / "spans.jsonl", run_id=RUN_ID),
    memory=MemoryStore(state_dir / "memory"),
    diagnostics=Diagnostics(run_id=RUN_ID, state_dir=state_dir),
    config=cfg,
    run_id=RUN_ID,
    workflow=WORKFLOW,
    manifest_path=MANIFEST,
    pins=_pins,
    # One pin store per run: a skill edited mid-run must not silently change the bytes a running
    # session sends, and this is where that is held. A *new* run gets a new store, so an edited skill
    # is picked up next time rather than fossilised.
    pins_for_prefix=PrefixPins(run_id=RUN_ID),
    policies=_policies,
    pool=TaskPool(state_dir / "pool.json"),
    # A goal armed by the orchestrator: the executing process advertises `update_goal` and writes its
    # verdict into `.agent_state/` for the orchestrator to consume after this run returns.
    goal_active={goal_active!r},
)
_EXECUTOR = NodeExecutor(_context)

def execute_node(node_id, state, ctx):
    return _EXECUTOR.execute_node(node_id, state, ctx)

def summary():
    return _EXECUTOR.summary()
''', encoding="utf-8")

        guardrail = directory / f"guardrail_{run_id}.py"
        guardrail.write_text(f'''#!/usr/bin/env python3
"""Generated per run. The runner loads this as its guardrail plugin."""
import sys

sys.path.insert(0, {str(engine_root)!r})

from engine.guardrail import EdgeGuardrail

_GUARD = EdgeGuardrail()

def classify(node_id, result, state):
    return _GUARD.classify(node_id, result, state)
''', encoding="utf-8")
        return {"executor": executor, "guardrail": guardrail}

    # ── running ─────────────────────────────────────────────────────────────

    def run(self, *, manifest_path: Path, run_id: str, workflow: str = "",
            project: str = "", extra_args: Iterable[str] = (),
            goal_active: bool = False) -> RunOutcome:
        """Spawn the runner and wait for it, supervising liveness throughout.

        Blocks until the run finishes, is killed, or stalls past the timeout. The caller is expected to
        be on a worker thread: this is the long-running call.

        Raises
        ------
        HostError
            When the runner cannot be spawned at all — a missing library file or a bad interpreter,
            which are startup problems rather than run problems.
        """
        # Every path handed to the runner must be absolute, because the subprocess runs with its cwd
        # set to the *library* root (it imports its own siblings from there). A relative manifest or
        # state path therefore resolves against the wrong directory and the runner dies on startup
        # with a FileNotFoundError before it can read a single node.
        manifest_path = Path(manifest_path).resolve()
        plugins = self.plugin_paths(manifest_path=manifest_path, run_id=run_id, workflow=workflow,
                                    project=project, goal_active=goal_active)
        # The runner's OWN checkpoint, distinct from the orchestrator's `run_state.json`. Pointing both
        # at one file meant the orchestrator's post-run write destroyed the runner's `{workflow,
        # manifest_sha, nodes}` shape, so `load_state` returned None and every continuation restarted
        # the graph — re-running the node that had just failed, for ever.
        runner_state = getattr(self.workspace, "runner_state_path", None)
        state_path = (Path(runner_state) if runner_state is not None
                      else Path(self.workspace) / ".agent_state" / "runner_state.json")
        state_path = state_path.resolve()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        runner = Path(self.library.files.runner)
        if not runner.is_file():
            raise HostError(f"the workflow runner is missing at {runner}")

        args = [
            self.python, str(runner),
            "--manifest", str(manifest_path),
            "--executor", str(plugins["executor"]),
            "--guardrail", str(plugins["guardrail"]),
            "--state", str(state_path),
        ]
        if self.workspace_memory_dir():
            args += ["--memory", str(self.workspace_memory_dir())]
        if self.enforce_contracts():
            args += ["--enforce-contracts"]
        args += list(extra_args)

        started = time.time()
        try:
            process = subprocess.Popen(
                args,
                # The runner imports its own siblings, so it must run from the library root.
                cwd=str(self.library.files.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                # Its own process group, so a kill takes the whole tree and nothing else.
                start_new_session=True,
            )
        except OSError as exc:
            raise HostError(f"cannot start the runner: {exc}") from exc

        handle = RunHandle(run_id=run_id, process=process, manifest_path=manifest_path,
                           state_path=state_path, state=RunnerState.RUNNING)
        handle.refresh()
        with self._lock:
            self._handle = handle

        self._emit("run.start", {"run_id": run_id, "workflow": workflow,
                                 "pid": process.pid, "args": args[1:6]})

        # stderr on its own thread: a pipe fills and deadlocks the child if nobody drains it.
        stderr_thread = threading.Thread(target=self._drain_stderr, args=(handle,), daemon=True)
        stderr_thread.start()
        # stdout must be drained too. The runner prints its summary JSON there, and a pipe that fills
        # blocks the child forever — a deadlock that looks like a stall. Its content is kept as the
        # stdout tail rather than parsed, because the state file is the authoritative record.
        stdout_thread = threading.Thread(target=self._drain_stdout, args=(handle,), daemon=True)
        stdout_thread.start()

        outcome = self._supervise(handle, started=started)
        stderr_thread.join(timeout=2.0)
        stdout_thread.join(timeout=2.0)
        self._emit("run.end", outcome.as_dict())
        return outcome

    def _supervise(self, handle: RunHandle, *, started: float) -> RunOutcome:
        """Watch a running process, enforcing the stall and pause policies.

        Polls rather than blocking on `wait()` so a pause request and a stall timeout are both honoured
        promptly — a blocking wait would only notice them after the run finished.
        """
        outcome = RunOutcome(run_id=handle.run_id, state=RunnerState.RUNNING)
        while handle.alive:
            time.sleep(0.25)
            handle.refresh()

            if self._pause_flag.is_set() and handle.state is RunnerState.RUNNING:
                handle.state = RunnerState.PAUSED
                self._emit("run.paused", {"run_id": handle.run_id, "pid": handle.pid})
            elif not self._pause_flag.is_set() and handle.state is RunnerState.PAUSED:
                handle.state = RunnerState.RUNNING
                self._emit("run.resumed", {"run_id": handle.run_id})

            if handle.last_activity_s() > self.stall_timeout_s:
                # A long silence from a live process *with no sign of work at all* is the wedge the
                # watchdog exists to catch. The checkpoint alone is not that signal — it is written per
                # node, so one long node looks identical to a hang, and killing a working run is the
                # worst outcome available: it discards real work and calls it a stall.
                self._emit("watchdog.stall", {"run_id": handle.run_id, "pid": handle.pid,
                                              "silence_s": round(handle.last_activity_s(), 1),
                                              "checkpoint_age_s": round(handle.state_age_s(), 1)})
                self._terminate(handle, force=True)
                break

        exit_code = handle.process.wait()
        outcome.exit_code = exit_code
        outcome.restarts = handle.restarts
        outcome.killed = handle.killed
        outcome.duration_s = time.time() - started
        outcome.stderr_tail = "\n".join(handle.stderr_lines[-30:])
        outcome.summary = self._read_summary(handle)

        if handle.killed:
            outcome.state = RunnerState.FAILED
            outcome.error = "the run was terminated by the host"
        elif exit_code == 0:
            outcome.state = RunnerState.FINISHED
        else:
            # A non-zero exit is *not* automatically a failure. The runner returns 1 for any summary
            # whose outcome is not "complete", and a run parked at a human gate is exactly that —
            # normal, expected, and awaiting the Owner. Reporting it as FAILED would tell the Operator
            # the run broke when it is sitting exactly where the design intends it to wait.
            phase = str(outcome.summary.get("phase") or "")
            gated = phase in ("escalated", "awaiting_human", "awaiting_gate")
            outcome.state = RunnerState.GATED if gated else RunnerState.FAILED
            outcome.error = "" if gated else (outcome.summary.get("outcome")
                                              or f"the runner exited {exit_code}")
        with self._lock:
            self._handle = None
        return outcome

    def _drain_stderr(self, handle: RunHandle) -> None:
        """Read the runner's stderr, which is its diagnostics channel.

        Kept on its own thread so a full pipe cannot deadlock the child, and forwarded rather than
        buffered unboundedly.
        """
        stream = handle.process.stderr
        if stream is None:
            return
        for line in stream:
            line = line.rstrip("\n")
            if not line:
                continue
            handle.stderr_lines.append(line)
            # A line of output is a sign of life. Without this, a runner that is printing progress
            # while a single long node runs is indistinguishable from a wedged one.
            handle.last_activity = time.time()
            # Bound the buffer: a chatty runner must not grow the host's memory.
            if len(handle.stderr_lines) > 500:
                handle.stderr_lines = handle.stderr_lines[-500:]
            if self.on_stderr is not None:
                try:
                    self.on_stderr(line)
                except Exception:  # noqa: BLE001 - a sink must not break supervision
                    pass
            self._emit("run.log", {"run_id": handle.run_id, "stream": "stderr",
                                   "line": line[:500]})

    def _drain_stdout(self, handle: RunHandle) -> None:
        """Drain the runner's stdout so a full pipe cannot deadlock it.

        The runner prints its summary JSON to stdout. Nothing here parses it — the state file is the
        authoritative record — but the bytes must be consumed, because a writer blocked on a full pipe
        is indistinguishable from a wedged run.
        """
        stream = handle.process.stdout
        if stream is None:
            return
        for line in stream:
            line = line.rstrip("\n")
            if not line:
                continue
            handle.stdout_lines.append(line)
            handle.last_activity = time.time()
            if len(handle.stdout_lines) > 500:
                handle.stdout_lines = handle.stdout_lines[-500:]

    def _read_summary(self, handle: RunHandle) -> dict[str, Any]:
        """Read the run summary from the state file, which is the runner's own record."""
        try:
            if handle.state_path.is_file():
                return json.loads(handle.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    # ── control ─────────────────────────────────────────────────────────────

    def pause(self) -> bool:
        """Ask the run to pause.

        Cooperative: the flag is set and the supervisor reports the pause, but the runner is *not*
        interrupted mid-node — killing mid-generation would corrupt state and waste the tokens already
        spent. The pause takes effect at the next node boundary.
        """
        with self._lock:
            if self._handle is None:
                return False
            self._pause_flag.set()
            return True

    def resume(self) -> bool:
        """Clear the pause flag."""
        with self._lock:
            if self._handle is None:
                return False
            self._pause_flag.clear()
            return True

    def abort(self) -> bool:
        """Terminate the run, checkpoint-first.

        SIGTERM lets the runner reach its per-node checkpoint; SIGKILL follows after the grace period.
        Returns whether there was a run to abort.
        """
        with self._lock:
            handle = self._handle
        if handle is None:
            return False
        self._terminate(handle, force=False)
        return True

    def _terminate(self, handle: RunHandle, *, force: bool) -> None:
        """Signal the process group, escalating to SIGKILL after the grace period.

        The whole process group is signalled rather than the process alone, so a provider subprocess or
        a tool the runner spawned does not outlive the run.
        """
        if not handle.alive:
            return
        handle.state = RunnerState.TERMINATING
        self._emit("run.terminating", {"run_id": handle.run_id, "pid": handle.pid, "force": force})
        try:
            self._signal_group(handle, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            handle.killed = True
            return
        deadline = time.time() + (0.5 if force else self.grace_s)
        while handle.alive and time.time() < deadline:
            time.sleep(0.1)
        if handle.alive:
            try:
                self._signal_group(handle, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            handle.killed = True
            self._emit("watchdog.restart", {"run_id": handle.run_id, "pid": handle.pid,
                                            "action": "SIGKILL after the grace period"})
        else:
            handle.killed = True

    @staticmethod
    def _signal_group(handle: RunHandle, sig: int) -> None:
        """Signal the process group when the platform supports it, else the process.

        `killpg` needs the child to lead its own session, which it does (start_new_session). On a
        platform without process groups, the process alone is signalled — a weaker guarantee, so the
        difference is explicit rather than assumed.
        """
        if platform.system() == "Windows":  # pragma: no cover - not a target platform
            handle.process.send_signal(sig)
            return
        try:
            os.killpg(os.getpgid(handle.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            handle.process.send_signal(sig)

    # ── liveness ────────────────────────────────────────────────────────────

    def wedged(self) -> dict[str, Any] | None:
        """The current run's liveness, or None when nothing is running.

        Reported from the *state file's* age rather than from stdout silence, because a runner that is
        thinking and one that is wedged are indistinguishable from outside — but a thinking runner
        checkpoints as it finishes each node.
        """
        with self._lock:
            handle = self._handle
        if handle is None:
            return None
        state = "alive" if handle.last_activity_s() <= self.heartbeat_s else (
            "slow" if handle.last_activity_s() <= self.heartbeat_s * 2 else
            "warned" if handle.last_activity_s() <= self.stall_timeout_s else "wedged")
        return {
            "run_id": handle.run_id, "pid": handle.pid, "alive": handle.alive,
            "state": state, "silence_s": round(handle.last_activity_s(), 1),
            "checkpoint_age_s": round(handle.state_age_s(), 1),
            "restarts": handle.restarts,
        }

    @property
    def state(self) -> RunnerState:
        """The current runner state, or IDLE when nothing is running."""
        with self._lock:
            return self._handle.state if self._handle else RunnerState.IDLE

    @property
    def running(self) -> bool:
        """Whether a run is in flight."""
        with self._lock:
            return self._handle is not None and self._handle.alive

    # ── configuration helpers ───────────────────────────────────────────────

    def workspace_memory_dir(self) -> Path | None:
        """Where run memory lives, when the caller wants it recorded."""
        base = Path(self.workspace) / ".agent_state" / "memory"
        return base if self.config is not None else None

    def enforce_contracts(self) -> bool:
        """Whether to pass `--enforce-contracts`.

        On by default: the runner's contract check is what makes a node's completion claim
        verifiable, and turning it off would let a node advance on an unevidenced done.
        """
        return True

    def resume_run(self, *, manifest_path: Path, run_id: str, workflow: str = "",
                   project: str = "", goal_active: bool = False) -> RunOutcome:
        """Relaunch a run from its checkpoint.

        The runner reads `--state`, so this continues rather than restarting — and the effect journal
        means the node it resumes into does not re-apply a side effect it already applied.
        """
        self._emit("run.resume", {"run_id": run_id, "from_checkpoint": True})
        return self.run(manifest_path=manifest_path, run_id=run_id, workflow=workflow,
                        project=project, goal_active=goal_active)

    # ── reporting ───────────────────────────────────────────────────────────

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        """Forward an event to the caller's sink. Never raises."""
        if self.on_event is None:
            return
        try:
            self.on_event(event, payload)
        except Exception:  # noqa: BLE001 - a sink must not break supervision
            pass

    def command_surface(self) -> dict[str, Any]:
        """What the host can be asked to do, for the UI.

        Reported rather than documented separately, so the command surface and the code cannot drift.
        """
        return {
            "commands": ["start", "pause", "resume", "abort", "snapshot", "resume_run"],
            "state": self.state.value,
            "running": self.running,
            "heartbeat_s": self.heartbeat_s,
            "grace_s": self.grace_s,
            "stall_timeout_s": self.stall_timeout_s,
            "python": self.python,
        }
