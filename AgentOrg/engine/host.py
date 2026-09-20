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
- **Preemption is forceful, in two steps.** SIGTERM, then SIGKILL after the grace period. The runner
  is deliberately *not* modified, and it installs no SIGTERM handler of its own, so SIGTERM ends it —
  it does not checkpoint. What makes that safe is the runner's per-node checkpoint file, so a
  continuation resumes at the node boundary; what a mid-node kill loses is the current node.
- **A runner never outlives the engine.** Two halves, because neither covers the other. The engine
  reaps what it started on its own way out (:func:`shutdown_runners`, installed as an `atexit`
  handler), which covers a clean stop and anything that runs interpreter shutdown — but not
  `os._exit`, which skips `atexit` by definition, and not a SIGKILL. So the executor plugin the host
  generates also watches the parent from *inside* the child and takes the child's whole process group
  down if the engine disappears. A runner left behind keeps executing and keeps spending against the
  same checkpoint while the app believes it stopped, which is the failure both halves exist to stop.
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

import atexit
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

__all__ = ["HostError", "RunHandle", "RunOutcome", "RunnerHost", "RunnerState",
           "shutdown_runners"]


class HostError(RuntimeError):
    """Raised when the runner cannot be started or supervised."""


# ── the live-runner registry ─────────────────────────────────────────────────
#
# Every runner this process has started and not yet reaped, keyed by the *process group* the host
# signals. Module-level rather than per-`RunnerHost`, because the thing that leaks is the process and
# the thing that has to find it at shutdown is whatever is running when the interpreter exits — which
# is not necessarily the object that spawned it.

_live_runners: dict[int, subprocess.Popen] = {}
_live_lock = threading.Lock()
_reaper_installed = False


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
    #: The two pipe readers. Held here so the supervisor can wait for them *before* it reads the
    #: stderr tail it reports: the thread that explains a crash is the *last* line the runner printed,
    #: and it is written to this list by another thread, so reading the tail without joining is a race
    #: that reports "the runner exited 1" instead of the reason.
    drains: list[threading.Thread] = field(default_factory=list)

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


def _last_stderr_cause(tail: str) -> str:
    """The runner's own last diagnostic line, when a non-zero exit left no reason in the state file.

    A runner that refuses its own manifest — an invalid node, a skill its validator cannot resolve —
    prints why to stderr and exits 1 *before* writing a checkpoint, so the summary has nothing to
    report and the outcome says only "the runner exited 1". That hides the one line that says what to
    fix, in a run whose whole failure was that line.
    """
    for line in reversed((tail or "").splitlines()):
        text = line.strip().lstrip("- ").strip()
        if text:
            return text[:300]
    return ""


def _register_runner(process: subprocess.Popen) -> None:
    """Record a live runner so the engine's own exit can reap it. Idempotent per pid."""
    global _reaper_installed
    with _live_lock:
        _live_runners[process.pid] = process
        if _reaper_installed:
            return
        atexit.register(_reap_at_exit)
        _reaper_installed = True


def _unregister_runner(process: subprocess.Popen) -> None:
    """Forget a runner that has been reaped by the thread that was supervising it."""
    with _live_lock:
        _live_runners.pop(process.pid, None)


def _kill_group(process: subprocess.Popen, sig: int) -> None:
    """Signal the runner's whole process group, falling back to the process alone.

    The group, not the process: the child led its own session (`start_new_session`), so the group
    holds whatever it spawned — a provider subprocess, a tool — and signalling only the leader would
    leave those running with no parent to report to.
    """
    if platform.system() == "Windows":  # pragma: no cover - not a target platform
        try:
            process.send_signal(sig)
        except (OSError, ProcessLookupError):
            pass
        return
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(sig)
        except (OSError, ProcessLookupError):
            pass


def shutdown_runners(*, grace_s: float = 2.0) -> list[int]:
    """Abort every runner this process started, and reap it. Returns the pids it acted on.

    Called by the engine's shutdown paths, and registered as an `atexit` handler so that the ordinary
    way out is covered without anyone having to remember. Both matter, because the failure is silent:
    a runner that outlives the engine keeps executing and keeps spending against the same checkpoint
    while the app shows the run as stopped.

    `atexit` is not sufficient on its own and this is worth being exact about — a path that calls
    `os._exit` skips it entirely, and so does SIGKILL. `engine/serve.py` ends its force-stop path at
    `os._exit(0)` (`_stop_now`, which the parent-watch and every signal handler it installs call), so
    the orderly half here cannot be the only half; that is why the child-side guard lives inside the
    generated executor plugin. This function is the fast half: SIGTERM, a short grace period, then
    SIGKILL, then `wait` so the dead child is not left as a zombie of a dying process.
    """
    with _live_lock:
        live = list(_live_runners.values())
    pids: list[int] = []
    for process in live:
        if process.poll() is not None:
            _unregister_runner(process)
            continue
        pids.append(process.pid)
        _kill_group(process, signal.SIGTERM)
    deadline = time.time() + grace_s
    for process in live:
        if process.pid not in pids:
            continue
        while process.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            _kill_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError):
            pass
        _unregister_runner(process)
    return pids


def _reap_at_exit() -> None:
    """The `atexit` handler. Never raises: an error here would mask the process's real exit status."""
    try:
        shutdown_runners()
    except Exception:  # noqa: BLE001 - a failed reap must not become the reason the engine died
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
        #: The explicit rework width, when the caller set one. `None` means "ask the config", which
        #: `contract_rework()` resolves — so an unconfigured host gets the conservative default.
        self._contract_rework: int | None = None
        self._handle: RunHandle | None = None
        self._lock = threading.RLock()
        self._pause_flag = threading.Event()

    # ── the plugin files ────────────────────────────────────────────────────

    def plugin_paths(self, *, manifest_path: Path, run_id: str, workflow: str,
                     project: str, goal_active: bool = False) -> dict[str, Path]:
        """Write the executor and guardrail plugins the runner will load.

        Written per run rather than shared, because each run has its own workspace, roster pins and run
        id — and a plugin that read a global would make two concurrent runs interfere.

        Each file is written to a temporary name and `os.replace`d into position. The runner *imports*
        these, so a half-written one is not a stale plugin but a syntax error that kills the run before
        its first node — and the two calls that write the same path (a run and a `resume_run` of the
        same run id) come from different threads.
        """
        directory = Path(self.workspace)
        directory.mkdir(parents=True, exist_ok=True)
        engine_root = Path(__file__).resolve().parent.parent

        executor = directory / f"executor_{run_id}.py"
        self._write_plugin(executor, f'''#!/usr/bin/env python3
"""Generated per run. The runner loads this as its executor plugin."""
import json
import os
import pathlib
import signal
import sys
import threading
import time

sys.path.insert(0, {str(engine_root)!r})

# ── do not outlive the engine ────────────────────────────────────────────────
# The runner is a child of the engine, in a session of its own, so nothing reaps it if the engine
# dies: it keeps executing and keeps spending against the same checkpoint while the app believes the
# run stopped. The engine aborts what it started on its own way out (see `shutdown_runners`), but that
# cannot cover the paths that skip interpreter shutdown entirely — `os._exit` in the engine's own
# `_stop_now`, and SIGKILL — so the guard is here, inside the child, where it survives those.
#
# `getppid()` changing is the signal. The engine is the parent; when it goes away this process is
# reparented to launchd or init and the number changes. It is polled rather than waited on because
# there is no portable way to be told, and a second of latency is nothing against a run that would
# otherwise keep spending for hours. Measured: 0.56-0.59s from the engine's death to this process's.
#
# The whole process *group* is signalled rather than just this process, because the group holds
# whatever the run spawned — a provider subprocess, a tool — and those are the other things that keep
# spending. Only when this process *is* the group leader, which it is because the host spawns the
# runner with `start_new_session=True`; without that check a child sharing the engine's own group
# would signal the engine and everything beside it, which is the opposite of the intent.
_PARENT_PID = os.getppid()


def _take_down(process_group):
    try:
        os.killpg(process_group, signal.SIGTERM)
        time.sleep(1.0)
        os.killpg(process_group, signal.SIGKILL)
    except Exception:
        pass


def _watch_parent():
    while True:
        time.sleep(1.0)
        if os.getppid() == _PARENT_PID:
            continue
        group = os.getpgrp()
        try:
            if group == os.getpid():
                _take_down(group)
            else:
                os.kill(os.getpid(), signal.SIGKILL)
        except Exception:
            pass
        # `_exit`, not `sys.exit`: this thread is a daemon and the engine is already gone, so there is
        # nothing here worth unwinding and no one left to unwind for.
        os._exit(1)


if _PARENT_PID > 1:
    threading.Thread(target=_watch_parent, name="agentorg-runner-parent-death",
                     daemon=True).start()

from engine.artifacts import ArtifactStore
from engine.cachestore import CacheStore
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
# They are taken verbatim from the run context — the *same* roots the planner planned against and
# the hire was validated against — rather than re-derived here. Re-deriving them was a silent
# defect: the overlay was rebuilt from the project root alone, so a global skill (or a global
# override) planned and hired successfully and then did not exist in the process that ran the node.
# Parent and child disagreeing about which skills exist is the worst version of this, because the
# node runs with a prompt that quietly lost the skill.
_skill_roots = list(_context_doc.skill_roots) if _context_doc is not None else []
if _skill_roots:
    _skills = OverlaySkillSource(FilesystemSkillSource(resolve()), skill_roots=_skill_roots)
else:
    # No recorded roots — a workspace written before this handoff existed. Fall back to the roots
    # this process can find for itself, which is the old behaviour and still runs.
    _skills = OverlaySkillSource(FilesystemSkillSource(resolve()))

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

# The durable cache record for this workspace, shared by the gateway (which sees the provider's own
# counters) and the pin store (which knows which prefixes this run actually sends). It is opened
# best-effort: a workspace with an unwritable state directory must still run, with its cache
# unrecorded, rather than refuse to start over bookkeeping.
try:
    _cache_store = CacheStore(state_dir / "cache")
except Exception:
    _cache_store = None

_context = ExecutorContext(
    org=org,
    gateway=Gateway(cfg, providers, estimator=TokenEstimator(), bus=_bus, run_id=RUN_ID,
                    cache_store=_cache_store),
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
    # is picked up next time rather than fossilised. The durable store records the pins across runs,
    # so a resumed run can still prove the prefix it sends is the one its predecessor pinned.
    pins_for_prefix=PrefixPins(run_id=RUN_ID, store=_cache_store),
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
''')

        guardrail = directory / f"guardrail_{run_id}.py"
        self._write_plugin(guardrail, f'''#!/usr/bin/env python3
"""Generated per run. The runner loads this as its guardrail plugin."""
import sys

sys.path.insert(0, {str(engine_root)!r})

from engine.guardrail import EdgeGuardrail

_GUARD = EdgeGuardrail()

def classify(node_id, result, state):
    return _GUARD.classify(node_id, result, state)
''')
        return {"executor": executor, "guardrail": guardrail}

    @staticmethod
    def _write_plugin(path: Path, text: str) -> None:
        """Write a plugin file atomically: temp in the same directory, then `os.replace`.

        Same directory because a rename across filesystems is a copy, and a copy is not atomic. The
        temp name carries the pid and the thread so two threads writing the same plugin — a run and a
        resume of the same run id — cannot collide on the temporary either.
        """
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise HostError(f"cannot write the runner plugin {path}: {exc}") from exc

    @staticmethod
    def _remove_plugins(plugins: dict[str, Path]) -> None:
        """Delete the per-run plugin files once the run that used them has ended.

        They are written into the *project* directory, not a temp directory, so leaving them behind
        leaks one pair per run for the life of the workspace — the projects in this repo carry a dozen
        `executor_run_*.py` files that no code will ever read again. The runner imports them at
        startup, so by the time a run has returned nothing holds them open.
        """
        for path in plugins.values():
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

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
        if self.contract_rework():
            args += ["--contract-rework", str(self.contract_rework())]
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
            # The plugins are already on disk at this point, and a spawn that failed is never going to
            # import them, so they are removed rather than left as litter in the project directory.
            self._remove_plugins(plugins)
            raise HostError(f"cannot start the runner: {exc}") from exc

        handle = RunHandle(run_id=run_id, process=process, manifest_path=manifest_path,
                           state_path=state_path, state=RunnerState.RUNNING)
        handle.refresh()
        with self._lock:
            self._handle = handle
        # From here on the runner is this process's responsibility, including on the way out. Registered
        # before the first drain thread so there is no window in which the process exists and the
        # shutdown path cannot see it.
        _register_runner(process)

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
        handle.drains = [stderr_thread, stdout_thread]

        try:
            outcome = self._supervise(handle, started=started)
        finally:
            # The supervisor joins these before it reports (see `_join_drains`); this is the guarantee
            # for the paths where it did not get that far — a raise out of the loop. A runner still
            # alive here is one nothing is supervising any more, so it is terminated rather than left.
            self._join_drains(handle)
            if handle.alive:
                self._terminate(handle, force=True)
                try:
                    handle.process.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            _unregister_runner(process)
            RunnerHost._remove_plugins(plugins)
            with self._lock:
                if self._handle is handle:
                    self._handle = None
        self._emit("run.end", outcome.as_dict())
        return outcome

    def _join_drains(self, handle: RunHandle, timeout_s: float = 2.0) -> bool:
        """Wait for the two pipe readers to finish. Returns whether they all did.

        Load-bearing, not tidiness. The stderr tail is the *last* thing the runner printed — for a
        runner that dies on startup it is the only explanation of why, and `_last_stderr_cause` reads
        exactly that from it — but it is put there by another thread. Reporting the tail without
        joining is a race the reader usually loses: the outcome says "the runner exited 1" while the
        line naming the cause is still in the pipe. The old code joined *after* `_supervise` had
        already sliced the list, which is why the join looked present and did nothing.

        Bounded, because the write end of a pipe is inherited by anything the run spawned: a grandchild
        that outlives the runner keeps the pipe open, so an unbounded join would hang the supervisor on
        a process that is already dead. When the bound is hit the caller is told, rather than left to
        wonder why the tail looks short.
        """
        for thread in handle.drains:
            thread.join(timeout=timeout_s)
        still_running = [t.name for t in handle.drains if t.is_alive()]
        if still_running:
            self._emit("run.drain_incomplete", {"run_id": handle.run_id,
                                                "threads": still_running,
                                                "detail": ("the runner's pipes were still held open "
                                                           "after it exited; the reported tail may "
                                                           "be short")})
            return False
        return True

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
        # Join the pipe readers *before* reading what they collected. The exit code arrives when the
        # process dies; the last lines of its stderr are in a pipe that may not have been read yet, and
        # those lines are the whole explanation for a startup failure.
        self._join_drains(handle)
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
                                              or _last_stderr_cause(outcome.stderr_tail)
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
        """Terminate the run.

        SIGTERM, then SIGKILL after the grace period. The runner installs no SIGTERM handler of its
        own — it is a pinned program this engine does not modify — so the grace period buys the
        *process group* a chance to shut down in its own order (a provider call, a temp file), not a
        cooperative checkpoint. What survives an abort is the per-node checkpoint the runner already
        wrote, so a continuation resumes at the node boundary. Returns whether there was a run to abort.
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

    def contract_rework(self) -> int:
        """Whether to pass `--contract-rework`, and how wide the window is.

        A property of the *host* like `enforce_contracts`, so the flag is supplied in exactly one
        place rather than at each spawn. Off by default: the window changes who recovers from a
        refusal, which is an autonomy decision, and a caller that has not made that decision should
        get the conservative answer. `Orchestrator._host` sets it from the goal's posture.

        The width comes from the config (`executor.contract_rework`), matching every other
        node-execution knob, so the number a person tunes is one they can find.
        """
        if self._contract_rework is None:
            section = getattr(self.config, "executor", None) if self.config is not None else None
            return max(0, int(getattr(section, "contract_rework", 0) or 0))
        return max(0, int(self._contract_rework))

    def with_contract_rework(self, attempts: int | None) -> "RunnerHost":
        """Return this host with the rework window set explicitly.

        A setter that returns the host rather than mutating in place, because the decision belongs to
        the caller that knows the posture — mutating a shared host would let one run's posture change
        the next round's behaviour.
        """
        self._contract_rework = attempts
        return self

    def resume_run(self, *, manifest_path: Path, run_id: str, workflow: str = "",
                   project: str = "", goal_active: bool = False,
                   extra_args: Iterable[str] = ()) -> RunOutcome:
        """Relaunch a run from its checkpoint.

        The runner reads `--state`, so this continues rather than restarting — and the effect journal
        means the node it resumes into does not re-apply a side effect it already applied.

        `extra_args` must be threaded through exactly as the first spawn received them. Without it a
        continuation lost its executor override and spawned the *generated* plugin instead of the one
        the caller named, so a run under test (or under a stub executor) silently switched executor
        mid-flight — the first round proving one thing and every later round another.
        """
        self._emit("run.resume", {"run_id": run_id, "from_checkpoint": True})
        return self.run(manifest_path=manifest_path, run_id=run_id, workflow=workflow,
                        project=project, goal_active=goal_active, extra_args=extra_args)

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
