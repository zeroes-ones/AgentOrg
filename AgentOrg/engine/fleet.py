#!/usr/bin/env python3
"""fleet.py — running several orgs at once, bounded.

WHY THIS EXISTS
---------------
`portfolio.py` is the register: *which* orgs a principal runs. `orchestrator.py` runs *one* org. This
module is the layer between them — it turns the register into live orchestrators, one per org, and lets
several of them work **at the same time** without any of them being able to spend or starve the others.

That concurrency is the whole point. A person with three companies does not work them one at a time,
and neither should their orgs: a mission in one org can be running while another waits at a gate and a
third is idle. So a `Fleet` holds one `Orchestrator` per org and runs each org's work on its own
thread, gated by two ceilings that exist precisely because they are now independent:

1. **A global concurrency ceiling.** The orgs share one machine, one set of providers and one budget;
   without a cap, N orgs each starting a run would each start their own swarm, and the sum is what
   melts the machine. The ceiling is the same machine-derived number the scheduler already computes —
   reused, not reinvented.
2. **A per-org daily budget.** The global budget in `credentials.json` bounds the *whole* principal;
   a fleet adds a per-org ceiling so one runaway org cannot consume the principal's entire allowance
   before the others get a turn. `OrgEntry.daily_budget_usd` is it, and `0` means "use the default".

DESIGN
------
- **One orchestrator per org, created lazily and cached.** Building one loads that org's roster and
  workspace; doing it eagerly for ten orgs on startup would read ten rosters nobody asked about. So a
  fleet is cheap until you actually run an org.
- **Isolation is by construction.** Each orchestrator already owns its own workspace, bus, ledger,
  diagnostics and org. The fleet adds *no* shared mutable state except the two ceilings and the
  thread registry — which is what keeps "the agents of org A never see org B" true without a single
  new isolation check.
- **A run is a future, not a blocking call.** `run_org` submits work and returns a handle immediately,
  so the caller (the serve loop, the CLI) stays responsive and can poll or stop it. The underlying
  `Orchestrator.execute` is already blocking and already thread-safe.
- **The fleet never spends on its own.** It runs work an org's *goal* already authorised. A fleet has
  no arm, no budget of its own to grant, and no way to start a mission — it is a runner, not a
  decision-maker, mirroring `portfolio.py` being a register.
- **Refusals are named.** A disabled org, an org whose folder is missing, a run refused by the
  ceiling, a run refused by the org's budget — each is a distinct refusal a person can act on, never
  a silent no-op.

Usage:
    fleet = Fleet(config=cfg, library=lib, portfolio=portfolio)
    handle = fleet.run_org("tesla", goal="add cursor pagination")
    fleet.status()                       # every org's live picture in one call
    fleet.rollup()                       # the cross-org summary for the console
    fleet.stop_org("tesla")
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = ["Fleet", "FleetError", "OrgRuntime", "OrgRunHandle"]


class FleetError(RuntimeError):
    """A fleet operation that cannot be honoured, named so the reason is actionable."""


@dataclass
class OrgRunHandle:
    """One in-flight run in one org, and how to watch it."""

    org_id: str
    run_id: str
    started_at: float = field(default_factory=time.time)
    thread: threading.Thread | None = None
    error: str = ""
    finished: bool = False

    @property
    def elapsed_s(self) -> float:
        return round(time.time() - self.started_at, 3)

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def as_dict(self) -> dict[str, Any]:
        return {"org_id": self.org_id, "run_id": self.run_id, "running": self.running,
                "finished": self.finished, "error": self.error, "elapsed_s": self.elapsed_s}


@dataclass
class OrgRuntime:
    """One org's live state inside the fleet: its orchestrator, and what it is doing."""

    org_id: str
    name: str
    slug: str
    orchestrator: Any = None
    handle: OrgRunHandle | None = None
    load_error: str = ""

    @property
    def loaded(self) -> bool:
        return self.orchestrator is not None

    @property
    def running(self) -> bool:
        return self.handle is not None and self.handle.running


class Fleet:
    """Runs the orgs a portfolio registers, one orchestrator each, several at once.

    Parameters
    ----------
    config, library:
        The shared stack. Config is shared (one set of providers and credentials), which is exactly
        why the fleet enforces a *global* ceiling: the orgs are independent but the machine is not.
    portfolio:
        The register. The fleet reads it; it never edits it.
    max_concurrent_orgs:
        The global ceiling on orgs whose work may run at once. Defaults to the machine-derived ceiling
        the scheduler already uses, so the fleet does not invent a second number.
    org_daily_budget_usd:
        The default per-org daily ceiling, used when an `OrgEntry` names none (`0`). `0` here means
        the fleet applies no per-org ceiling beyond the global budget in the config.
    """

    def __init__(self, *, config: Any, library: Any, portfolio: Any,
                 max_concurrent_orgs: int | None = None,
                 org_daily_budget_usd: float = 0.0,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.config = config
        self.library = library
        self.portfolio = portfolio
        self.org_daily_budget_usd = max(0.0, float(org_daily_budget_usd or 0.0))
        self.on_event = on_event
        self._lock = threading.RLock()
        self._runtimes: dict[str, OrgRuntime] = {}
        self._max_concurrent = (max(1, int(max_concurrent_orgs))
                                if max_concurrent_orgs is not None
                                else self._default_ceiling())

    # ── the ceiling ─────────────────────────────────────────────────────────

    def _default_ceiling(self) -> int:
        """The machine-derived concurrency ceiling, reused from `resources.derive_ceiling`.

        Deliberately the same number the scheduler uses rather than a new constant: the orgs share one
        machine, and a second, larger ceiling here would let the fleet promise more concurrency than
        the machine can hold.
        """
        try:
            from . import resources

            caps = resources.detect()
            derived = resources.derive_ceiling(
                caps,
                cpu_headroom=int(getattr(self.config.concurrency, "cpu_headroom", 1)),
                configured_ceiling=getattr(self.config.concurrency, "global_ceiling", None),
            )
            return max(1, int(derived.get("ceiling") or 1))
        except Exception:  # noqa: BLE001 - an unreadable machine is a conservative cap
            return 1

    @property
    def max_concurrent_orgs(self) -> int:
        return self._max_concurrent

    def _running_count(self) -> int:
        with self._lock:
            return sum(1 for rt in self._runtimes.values() if rt.running)

    # ── building runtimes ───────────────────────────────────────────────────

    def _runtime_for(self, org_ref: str) -> OrgRuntime:
        """The live runtime for one org, built lazily and cached.

        Building one loads the org's roster and workspace; doing it eagerly for every registered org
        would read rosters nobody asked about, so it happens on first use.
        """
        entry = self.portfolio.org(org_ref)
        with self._lock:
            existing = self._runtimes.get(entry.id)
            if existing is not None and existing.loaded:
                return existing
            runtime = OrgRuntime(org_id=entry.id, name=entry.name, slug=entry.slug)
            self._runtimes[entry.id] = runtime
        try:
            runtime.orchestrator = self._build_orchestrator(entry)
        except Exception as exc:  # noqa: BLE001 - a load failure is reported, not fatal
            runtime.load_error = str(exc)
        return runtime

    def _build_orchestrator(self, entry: Any) -> Any:
        """Build the orchestrator for one org: its workspace, its roster, its identity.

        The workspace is the org's own folder when it has one, so its roster, missions, goals and runs
        live inside it exactly as they do when the org is run alone. The org is loaded with its
        identity (id + principal) so it can be found by the register rather than by its display name.
        """
        from .bus import EventBus
        from .orchestrator import Orchestrator
        from .people import HireError, People
        from .state import Workspace

        path = entry.workspace_path
        if not str(path):
            # No folder named: a managed workspace keyed on the slug, under the default projects root.
            workspace = Workspace.for_project(entry.slug)
        else:
            path.mkdir(parents=True, exist_ok=True)
            workspace = Workspace.attach(path)
        workspace.ensure()
        bus = EventBus(run_id=f"fleet_{entry.slug}", trace_path=workspace.trace_path)
        org = None
        try:
            providers, _ = self._providers()
            from .catalog import ModelCatalog

            org = People(library=self.library, config=self.config,
                         catalog=ModelCatalog(self.config, providers),
                         project=workspace.path).load(
                project=workspace.path, org_id=entry.id,
                principal_id=getattr(self.portfolio.principal, "id", ""), name=entry.name)
        except HireError as exc:  # noqa: BLE001 - a broken roster must not stop the org loading
            org = None
            entry_error = str(exc)
        else:
            entry_error = ""
        if org is None:
            raise FleetError(f"could not load the roster for org {entry.name!r}: {entry_error}")
        return Orchestrator(config=self.config, library=self.library, workspace=workspace,
                            bus=bus, org=org)

    def _providers(self) -> tuple[dict[str, Any], list[str]]:
        from .providers.registry import build_providers

        return build_providers(self.config)

    # ── status ──────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Every org's live picture, in one call.

        Only *loaded* orgs have a live picture; an unloaded org is reported as such rather than
        silently omitted, so the console can show the whole portfolio and mark what it has not
        started yet.
        """
        with self._lock:
            runtimes = dict(self._runtimes)
        rows: list[dict[str, Any]] = []
        for entry in self.portfolio.orgs:
            runtime = runtimes.get(entry.id)
            row: dict[str, Any] = {
                "id": entry.id, "name": entry.name, "slug": entry.slug,
                "enabled": entry.enabled, "charter": entry.charter,
                "path": entry.path, "daily_budget_usd": entry.daily_budget_usd,
                "loaded": bool(runtime and runtime.loaded),
                "running": bool(runtime and runtime.running),
                "load_error": runtime.load_error if runtime else "",
            }
            if runtime is not None and runtime.loaded:
                row.update(self._org_live(runtime))
            rows.append(row)
        return {
            "principal": self.portfolio.principal.as_dict(),
            "max_concurrent_orgs": self._max_concurrent,
            "running": self._running_count(),
            "orgs": rows,
        }

    def _org_live(self, runtime: OrgRuntime) -> dict[str, Any]:
        """The live fields for one loaded org, tolerating an orchestrator that has no run yet."""
        orch = runtime.orchestrator
        live: dict[str, Any] = {"phase": "idle", "running": runtime.running, "stop_reason": ""}
        try:
            status = orch.status() or {}
        except Exception as exc:  # noqa: BLE001 - a status failure is shown, not fatal
            live["load_error"] = str(exc)
            return live
        live["phase"] = str(status.get("phase") or "idle")
        live["stop_reason"] = str(status.get("stop_reason") or "")
        live["waiting_host"] = bool(status.get("gate")) or live["phase"] in (
            "awaiting_gate", "awaiting_human", "awaiting_approval", "paused")
        nodes = (status.get("outcome") or {}).get("nodes") or {}
        live["blocked_nodes"] = sum(
            1 for rec in nodes.values()
            if isinstance(rec, dict) and str(rec.get("status")) == "blocked")
        # The mission and the objective it is on, so the portfolio shows *what* each org is for.
        mission = status.get("mission") or {}
        live["mission"] = str(mission.get("statement") or "")
        live["mission_state"] = str(mission.get("state") or "")
        progress = mission.get("progress") or {}
        live["objective_now"] = str((mission.get("now") or {}).get("text") or progress.get("next") or "")
        # Spend, from the ledger, honestly: an unknown figure is None, never zero.
        live["spend_usd"] = self._org_spend(orch)
        live["headline"] = self._headline(live)
        return live

    def _org_spend(self, orch: Any) -> float | None:
        try:
            snapshot = orch.ledger.snapshot()
            value = snapshot.get("cost_usd")
            return float(value) if value is not None else None
        except Exception:  # noqa: BLE001 - an unreadable ledger is "unknown", not zero
            return None

    @staticmethod
    def _headline(live: dict[str, Any]) -> str:
        """One line per org: what it is doing, or why it is not."""
        if live.get("waiting_host") and live.get("phase") == "awaiting_gate":
            return "waiting on you: a gate"
        if live.get("blocked_nodes"):
            return f"blocked — {live['blocked_nodes']} node(s)"
        if live.get("running"):
            return f"running ({live.get('phase')})"
        if live.get("stop_reason"):
            return f"stopped — {live['stop_reason'][:80]}"
        if live.get("objective_now"):
            return f"on: {live['objective_now'][:80]}"
        return "idle"

    def rollup(self) -> dict[str, Any]:
        """The cross-org summary: the portfolio's own rollup, fed the fleet's live picture."""
        return self.portfolio.rollup(per_org=self.status()["orgs"])

    # ── running ─────────────────────────────────────────────────────────────

    def run_org(self, org_ref: str, *, goal: str = "", manifest: str = "",
                background: bool = True, executor: str = "") -> OrgRunHandle:
        """Start one org's work, on its own thread by default.

        This is the concurrency the fleet exists for: several orgs may each have a run in flight. Two
        refusals bound it — the global ceiling, and the per-org daily budget — and each names its
        reason so a person can see *why* an org did not start rather than guessing.

        `executor` overrides the executor plugin path, exactly as `Orchestrator.execute` allows, so a
        run can be driven by a deterministic stub in a test.
        """
        entry = self.portfolio.org(org_ref)
        if not entry.enabled:
            raise FleetError(
                f"org {entry.name!r} is disabled in the portfolio; enable it before running it")
        with self._lock:
            running_now = sum(1 for rt in self._runtimes.values() if rt.running)
            already = self._runtimes.get(entry.id)
            if already is not None and already.running:
                raise FleetError(f"org {entry.name!r} already has a run in flight")
            if running_now >= self._max_concurrent:
                raise FleetError(
                    f"the fleet is at its concurrency ceiling ({self._max_concurrent}); "
                    "wait for a run to finish or raise it")
        self._check_org_budget(entry)

        runtime = self._runtime_for(entry.id)
        if not runtime.loaded:
            raise FleetError(f"could not load org {entry.name!r}: {runtime.load_error}")

        orch = runtime.orchestrator
        run = orch.adopt(manifest, slug=entry.slug) if manifest else orch.prepare(goal)
        handle = OrgRunHandle(org_id=entry.id, run_id=str(getattr(run, "run_id", "")))
        with self._lock:
            runtime.handle = handle

        def _work() -> None:
            try:
                orch.approve(run)
                orch.execute(run, executor=executor or None)
            except Exception as exc:  # noqa: BLE001 - a run failure is recorded, not raised here
                handle.error = str(exc)
                self._emit("fleet.org.failed", {"org_id": entry.id, "error": str(exc)})
            finally:
                handle.finished = True
                self._emit("fleet.org.finished", {"org_id": entry.id, "run_id": handle.run_id})

        if background:
            handle.thread = threading.Thread(
                target=_work, name=f"agentorg-org-{entry.slug}", daemon=True)
            handle.thread.start()
        else:
            _work()
        self._emit("fleet.org.started", {"org_id": entry.id, "run_id": handle.run_id,
                                         "goal": goal or manifest})
        return handle

    def _check_org_budget(self, entry: Any) -> None:
        """Refuse a run when the org has spent its daily allowance.

        The per-org ceiling is what keeps one runaway org from consuming the principal's whole budget.
        `0` on the entry falls back to the fleet default; both `0` means the global budget in the
        config is the only ceiling.
        """
        ceiling = float(entry.daily_budget_usd or self.org_daily_budget_usd or 0.0)
        if ceiling <= 0:
            return
        runtime = self._runtimes.get(entry.id)
        spent = self._org_spend(runtime.orchestrator) if (runtime and runtime.loaded) else None
        if spent is not None and spent >= ceiling:
            raise FleetError(
                f"org {entry.name!r} has spent ${spent:.2f} of its ${ceiling:.2f} daily budget; "
                "raise it (portfolio update) or wait for the next day")

    def stop_org(self, org_ref: str) -> dict[str, Any]:
        """Ask one org's run to stop, at its next node boundary.

        Cooperative, like `Orchestrator.pause`/`abort`: a kill mid-node would waste the tokens already
        spent and could corrupt state, so the stop takes effect where it is safe.
        """
        entry = self.portfolio.org(org_ref)
        runtime = self._runtimes.get(entry.id)
        if runtime is None or not runtime.loaded:
            raise FleetError(f"org {entry.name!r} is not loaded, so it has nothing to stop")
        try:
            runtime.orchestrator.pause()
        except Exception as exc:  # noqa: BLE001
            raise FleetError(f"could not stop org {entry.name!r}: {exc}") from exc
        self._emit("fleet.org.stopping", {"org_id": entry.id})
        return runtime.handle.as_dict() if runtime.handle else {"org_id": entry.id, "running": False}

    def forget_org(self, org_ref: str) -> None:
        """Drop a loaded runtime, so the next use rebuilds it from disk.

        For when an org's roster was edited outside the fleet (the CLI, the app) and the in-memory
        orchestrator is stale. It does not touch the register or the org's folder.
        """
        entry = self.portfolio.org(org_ref)
        with self._lock:
            self._runtimes.pop(entry.id, None)

    def wait(self, org_ref: str | None = None, *, timeout: float | None = None) -> bool:
        """Block until an org's run finishes (all of them when `org_ref` is None).

        Returns True when everything waited for finished, False on timeout. A bounded wait rather
        than an unbounded join, so a caller can report progress instead of hanging on a wedged run.
        """
        with self._lock:
            handles = [rt.handle for rt in self._runtimes.values() if rt.handle is not None]
        if org_ref is not None:
            entry = self.portfolio.org(org_ref)
            handles = [h for h in handles if h.org_id == entry.id]
        deadline = (time.time() + timeout) if timeout is not None else None
        for handle in handles:
            remaining = None if deadline is None else max(0.0, deadline - time.time())
            if handle.thread is not None:
                handle.thread.join(timeout=remaining)
            if handle.running:
                return False
        return True

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:  # noqa: BLE001 - an observer must not break the fleet
            pass

    # ── the orgs, for a caller that wants the register ───────────────────────

    def orgs(self) -> Iterable[Any]:
        return list(self.portfolio.orgs)

    def runtime(self, org_ref: str) -> OrgRuntime | None:
        entry = self.portfolio.org(org_ref)
        with self._lock:
            return self._runtimes.get(entry.id)
