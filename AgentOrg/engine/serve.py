#!/usr/bin/env python3
"""serve.py — the long-running NDJSON server the native console drives.

WHY THIS EXISTS
---------------
The macOS app has always launched `engine.cli serve`. That command did not exist. So the console —
every tab, every panel — could never start the engine it was built to watch: the app would spawn a
process, the process would exit with a usage error, and the UI would sit at "idle" forever.

This module is the missing half. It is a **line-protocol server**: the app writes one JSON command per
line on stdin, the engine writes one JSON event per line on stdout, and the two never interleave
because diagnostics go to stderr.

DESIGN
------
- **stdout is the protocol, stderr is for humans.** A stray `print` on stdout corrupts the stream the
  app parses, so every diagnostic here goes to stderr. That rule is why the app can read stdout raw.
- **`stdin` owns the control channel, and the run happens off it.** A run takes minutes; if the loop
  blocked while executing, the app's `/pause` would queue behind the very work it is trying to pause.
  So commands execute on a worker thread and the read loop stays responsive.
- **Every command is acknowledged, always.** The app's `send` awaits a `command.ack` correlated by
  `cmd_id` and times out otherwise. An unacknowledged command is indistinguishable from a lost one, so
  the ack is sent for failures too — with the reason.
- **The event stream is the run's own bus.** Rather than inventing a second protocol, the run's
  `EventBus` is tapped and forwarded, so the UI sees exactly the events the engine already emits and
  the two cannot drift.
- **EOF is a clean shutdown.** The app closes stdin when it stops; that must end the server rather than
  leaving an orphan holding the project.

Usage:
    python3 -m engine.cli serve
    python3 -m engine.cli serve --slug demo --root projects
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .protocol import Ack, Command, Event, EventType

__all__ = ["Server", "ServerError", "serve"]


class ServerError(RuntimeError):
    """A server problem worth naming rather than surfacing as a traceback on stdout."""


def _log(message: str) -> None:
    """Diagnostics go to stderr. stdout belongs to the protocol and a stray line corrupts it.

    Never raises. stderr is a pipe to the app, and once the app has exited that pipe is broken — so a
    logging call would raise `BrokenPipeError` at the worst possible moment, which is precisely when we
    are trying to report that the app is gone. A diagnostic that can break the code path it is
    describing is worse than no diagnostic.
    """
    try:
        print(message, file=sys.stderr, flush=True)
    except (BrokenPipeError, ValueError, OSError):
        pass


@dataclass
class Server:
    """The NDJSON command loop.

    Parameters
    ----------
    config:
        Validated configuration.
    library:
        The pinned library handle.
    workspace:
        The project the console is pointed at.
    stdin / stdout:
        Injectable streams, so the whole loop is testable without a terminal or a subprocess.
    """

    config: Any
    library: Any
    workspace: Any
    stdin: Any = None
    stdout: Any = None
    #: Set by `start`/`resume`: the orchestrator the run commands act on.
    orchestrator: Any = None
    slug: str = "console"
    #: The portfolio the console is showing, when there is one. Loaded lazily so a single-org setup
    #: pays nothing for the multi-org layer.
    _portfolio: Any = None
    _portfolio_loaded: bool = False
    #: The fleet, built on first use, so several orgs can run at once from one server.
    _fleet: Any = None

    def __post_init__(self) -> None:
        self.stdin = self.stdin if self.stdin is not None else sys.stdin
        self.stdout = self.stdout if self.stdout is not None else sys.stdout
        self._seq = 0
        self._lock = threading.RLock()
        self._commands: "queue.Queue[Command | None]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._forwarded: set[int] = set()
        #: Commands currently being executed by the worker. `_drain` waits on this together with the
        #: queue, because an in-flight command has already left the queue and would otherwise be
        #: mistaken for nothing to wait for.
        self._in_flight: int = 0
        #: The shared discovery catalog. Built lazily on first use and dropped by `_reload_config`,
        #: so its TTL cache survives across polls instead of being thrown away each time.
        self._discovery: Any = None

    # ── the transport ───────────────────────────────────────────────────────

    def emit(self, event: Event | dict[str, Any]) -> None:
        """Write one event as a single line.

        Serialised under a lock because two threads emit: the worker handling a command and the run's
        own bus forwarding events. Interleaved writes would produce a line the app cannot parse.
        """
        with self._lock:
            self._seq += 1
            if isinstance(event, Event):
                payload = event.to_dict()
                payload.setdefault("seq", self._seq)
                if not payload.get("seq"):
                    payload["seq"] = self._seq
            else:
                payload = dict(event)
                payload.setdefault("seq", self._seq)
            payload.setdefault("ts", _now())
            try:
                line = json.dumps(payload, default=str)
            except (TypeError, ValueError) as exc:
                _log(f"cannot serialise an event: {exc}")
                return
            self.stdout.write(line + "\n")
            self.stdout.flush()

    def ack(self, cmd_id: str, *, ok: bool, detail: dict[str, Any] | None = None,
            error: str | None = None) -> None:
        """Acknowledge a command. Sent for failures too, because a silent command looks lost."""
        event = Ack(cmd_id=cmd_id, ok=ok, error=error, detail=detail or {}).to_event(0)
        self.emit(event)

    # ── the loop ────────────────────────────────────────────────────────────

    def serve_forever(self) -> int:
        """Read commands until EOF, executing each off the read loop. Returns an exit code."""
        _log(f"agentorg serve: project={self.workspace.path} pid={_pid()}")
        # The readiness handshake, first on the wire. The console cannot otherwise tell a live engine
        # from a spawned-but-doomed one: a bootstrap failure is a process that exists for a moment and
        # exits, and the app used to call that "running". This frame is emitted *after* the workspace,
        # config and providers are all resolved, so receiving it means the engine really is usable —
        # and anything that stops it before here is reported by the app as a failure, not a success.
        self.emit(Event(seq=0, type=EventType.ENGINE_READY,
                        payload={"pid": _pid(), "project": str(self.workspace.path),
                                 "slug": self.slug,
                                 "providers": sorted(getattr(self.config, "providers", {}) or {})}))
        self.emit(Event(seq=0, type=EventType.AGENT_LOG,
                        payload={"text": "engine ready", "stream": "stderr"}))
        self._worker = threading.Thread(target=self._work, name="agentorg-serve", daemon=True)
        self._worker.start()
        self._watch_parent()
        try:
            for line in self.stdin:
                if self._stop.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                command = self._parse(line)
                if command is None:
                    continue
                self._commands.put(command)
        except (KeyboardInterrupt, BrokenPipeError):
            pass
        except OSError as exc:
            _log(f"serve: stdin failed: {exc}")
        finally:
            # Drain the queue before stopping. EOF arrives as soon as the writer closes stdin, which
            # can be well before the worker has executed what was already read — stopping first would
            # drop those commands' acks and leave the app waiting on a reply that never comes.
            self._drain()
            self._shutdown()
        return 0

    def _watch_parent(self) -> None:
        """Exit if the app that started us goes away, whatever the pipe says.

        Why the pipe is not enough. The app holds the *write* end of this process's stdin, so a normal
        quit closes it and the read loop ends. But if the app is killed outright — SIGKILL, a crash, a
        force-quit — the write end can still be open in some other process that inherited it (a helper,
        a backgrounded child), so EOF never arrives and this process would become an orphan holding the
        project. Observed in practice: SIGTERM to the app left the engine running indefinitely, and the
        next app instance then started a *second* engine on the same checkpoint.

        **The expected parent is named, not sampled.** An earlier version recorded `os.getppid()` at
        startup and watched for it to change — which has a race: if the app dies during this process's
        own import, the parent is already gone and `getppid()` reads 1, so there is nothing to detect
        and the watchdog gave up. Instead the app passes its own pid in `$AGENTORG_PARENT_PID`, and this
        polls whether *that* pid is still alive. Naming it removes the race entirely.

        Falls back to change-detection when the variable is absent, which is the case for a CLI launch —
        where `getppid()` becoming 1 is legitimate (detached with `nohup`/`setsid`) and must not be
        treated as a reason to exit.
        """
        expected_raw = os.environ.get("AGENTORG_PARENT_PID", "").strip()
        expected = int(expected_raw) if expected_raw.isdigit() else 0

        if expected > 1:
            def _poll_expected() -> None:
                while not self._stop.wait(2.0):
                    if not _pid_alive(expected):
                        # Stop FIRST, then log. The log goes to stderr, which is the dead app's pipe —
                        # writing to it raises `BrokenPipeError`, and anything after that line would
                        # never run. Ordering this the other way round made the watchdog silently
                        # useless for exactly the case it exists for: the app being gone.
                        self._stop_now()
                        return

            threading.Thread(target=_poll_expected, name="agentorg-parent-watch", daemon=True).start()
            return

        # No named parent: the only signal available is a change in our own parent.
        parent = os.getppid()
        if parent <= 1:
            # Started detached (a shell that exited, `nohup`, `setsid`). There is nothing to watch, and
            # exiting would be wrong — a deliberately detached engine is meant to keep running.
            return

        def _poll_ppid() -> None:
            while not self._stop.wait(2.0):
                if os.getppid() != parent:
                    self._stop_now()   # stop before logging, for the same reason as above
                    return

        threading.Thread(target=_poll_ppid, name="agentorg-parent-watch", daemon=True).start()

    def _stop_now(self) -> None:
        """Stop immediately: unblock the read loop, then exit without waiting to be collected.

        An engine whose app is gone has no client, no acknowledgements to send, and a project it should
        release, so it exits rather than lingering. `os._exit` rather than a clean return because the
        read loop is parked on stdin, which may never see EOF — the very condition that brought us here.
        """
        self._stop.set()
        self._commands.put(None)
        try:
            os.close(0)
        except OSError:
            pass
        os._exit(0)

    def _drain(self, timeout_s: float = 10.0) -> None:
        """Wait until the worker has *finished* what it was given, bounded so a wedged command cannot
        hang exit.

        Why the queue being empty is not the same as done: the worker takes a command **off** the queue
        and then runs it, so the queue reads empty while the command is still in flight. Waiting on
        `empty()` therefore returned immediately and `_shutdown` killed the worker mid-command — the
        ack never went out, and the app sat waiting for a reply to a command that had actually
        succeeded. That is the worst shape of bug here: the work is done and the answer is lost.

        So the worker reports what it is doing, and this waits on that. A counter rather than a flag,
        because the worker is single-threaded today but the invariant should not depend on that.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._commands.empty() and self._in_flight == 0:
                return
            time.sleep(0.02)

    def _parse(self, line: str) -> Command | None:
        """Parse one command line, reporting a malformed one without dying.

        A single unparsable line must not take the server down: the app would then lose its engine
        over one bad frame. It is logged, and the loop continues.
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            _log(f"serve: ignoring a malformed command line: {exc}")
            return None
        if not isinstance(data, dict) or not data.get("type"):
            _log("serve: ignoring a command with no type")
            return None
        return Command(
            cmd_id=str(data.get("cmd_id") or f"cmd_{int(time.time() * 1000)}"),
            type=str(data["type"]),
            payload=data.get("payload") if isinstance(data.get("payload"), dict) else {},
        )

    def _work(self) -> None:
        """Execute queued commands, one at a time, off the read loop."""
        while not self._stop.is_set():
            try:
                command = self._commands.get(timeout=0.2)
            except queue.Empty:
                continue
            if command is None:
                return
            # Marked before the work, so `_drain` cannot conclude "nothing running" while this is
            # inside `handle()`. Decremented in `finally` so a crashing command cannot leave the
            # counter stuck and turn a bounded wait into a 10-second stall on every exit.
            with self._lock:
                self._in_flight += 1
            try:
                detail = self.handle(command)
                self.ack(command.cmd_id, ok=True, detail=detail or {})
            except Exception as exc:  # noqa: BLE001 - one bad command must not kill the server
                _log(f"serve: command {command.type!r} failed: {exc}")
                self.ack(command.cmd_id, ok=False, error=str(exc))
            finally:
                with self._lock:
                    self._in_flight -= 1

    def _shutdown(self) -> None:
        self._stop.set()
        self._commands.put(None)
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=2.0)
        _log("agentorg serve: stopped")

    # ── commands ────────────────────────────────────────────────────────────

    def handle(self, command: Command) -> dict[str, Any]:
        """Dispatch one command. Returns the ack's detail, or raises to report a refusal."""
        kind = command.type_value
        handler: Callable[[dict[str, Any]], dict[str, Any]] | None = getattr(
            self, f"_cmd_{kind}", None)
        if handler is None:
            raise ServerError(f"unknown command {kind!r}")
        return handler(command.payload or {})

    # -- read-only ----------------------------------------------------------

    def _cmd_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The snapshot every panel polls. Shape matters: the UI reads these keys by name.

        Every key is present in **both** branches. A field that appears only once a run has started
        is a field the panel renders as missing until then — which for the cache would read as
        "caching is broken" rather than "nothing has run yet".
        """
        if self.orchestrator is None:
            return {"phase": "idle", "running": False, "org": self._roster(),
                    "gate": None, "outcome": {}, "cost": self._cost(),
                    "cache": self._cmd_cache({}), "swarm": self._cmd_swarm({}),
                    "workspace": self._workspace_info(),
                    "goal": self._cmd_goal_status({})["goal"],
                    "mission": self._cmd_mission({})["mission"],
                    "subagents": self._cmd_subagents({}),
                    "proposals": self._cmd_proposals({}),
                    "activity": self._cmd_activity({}),
                    "flow": self._cmd_flow({}),
                    "defaults": self._cmd_defaults({}),
                    "portfolio": self._cmd_portfolio({})}
        status = self.orchestrator.status()
        # The roster is what the Org panel renders, so it travels with every snapshot rather than
        # needing its own command the UI would have to remember to send.
        status["org"] = self._roster()
        status.setdefault("running", False)
        # The cache summary travels with status too, so the Cost panel gets it from the poll it
        # already makes rather than needing a second round trip that might never be added.
        status["cache"] = self._cmd_cache({})
        # The swarm snapshot travels the same way, so a panel showing per-item fan-out progress does
        # not need a second command the UI has to remember to send.
        status["swarm"] = self._cmd_swarm({})
        # Whether the agents are pointed at the user's own folder or a managed one is the first thing
        # the Org panel has to say, so it travels here rather than waiting for a dedicated command.
        status["workspace"] = self._workspace_info()
        # The goal travels with status so the Work panel can show whether the loop will continue
        # without a second round trip that might never be added.
        status.setdefault("goal", self._cmd_goal_status({})["goal"])
        # The mission travels the same way, so the console shows the standing purpose and the active
        # step from the poll it already makes.
        status.setdefault("mission", self._cmd_mission({})["mission"])
        # The subagent tree travels the same way, so the panel shows isolated children from the poll
        # it already makes.
        status.setdefault("subagents", self._cmd_subagents({}))
        # Proposals travel with status too, so a promoted fix is visible without the console having to
        # remember a second command — and so the count can badge the sidebar.
        status.setdefault("proposals", self._cmd_proposals({}))
        # The activity timeline travels the same way, so the console's "what is happening" panel is
        # populated from the poll it already makes rather than a second round trip.
        status.setdefault("activity", self._cmd_activity({}))
        # The org board travels the same way, so the Flow panel shows who is on what from the poll it
        # already makes. It is derived from the same artifacts as `activity`, so the two cannot
        # disagree about what is happening.
        status.setdefault("flow", self._cmd_flow({}))
        # The default the org runs on travels too, so the Providers panel's Defaults editor shows the
        # effective pair from the poll it already makes rather than a second round trip.
        status.setdefault("defaults", self._cmd_defaults({}))
        # The portfolio travels too, so the console can show every org the principal runs — the
        # register, not the live picture (that is `portfolio_live`, sent when the panel is open).
        status.setdefault("portfolio", self._cmd_portfolio({}))
        return status

    def _cmd_activity(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The activity timeline: what the org is doing, why it stopped, and what is next.

        Reads the same artifacts the panels already do, folded into one ordered story. Bounded like
        the terminal buffer, so a long run yields a summary rather than a file dump.
        """
        from .activity import build_activity

        org = getattr(self.orchestrator, "org", None)
        goal_status = self._cmd_goal_status({}).get("goal")
        run_status = self.orchestrator.status() if self.orchestrator is not None else {}
        try:
            return build_activity(
                self.workspace, run_status=run_status, goal_status=goal_status, org=org,
                staffing=list(run_status.get("staffing_gaps") or []),
                limit=int(payload.get("limit") or 120))
        except Exception as exc:  # noqa: BLE001 - a snapshot must never break the poll
            _log(f"serve: activity snapshot failed: {exc}")
            return {"headline": "activity unavailable", "timeline": [], "counts": {}}

    def _cmd_flow(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The org board: which agent has which work, what crossed between them, and what came back.

        Derived from the same artifacts the other panels read, so the CLI's `flow` and the console's
        Flow panel cannot disagree. Bounded by the trace tail, like the activity report, so a long run
        yields a board rather than a file dump.
        """
        from .flow import build_flow

        org = getattr(self.orchestrator, "org", None)
        run_status = self.orchestrator.status() if self.orchestrator is not None else {}
        try:
            return build_flow(
                self.workspace, run_status=run_status, org=org,
                limit=int(payload.get("limit") or 800),
                org_id=str(getattr(org, "id", "") or ""),
                org_name=str(getattr(org, "name", "") or ""))
        except Exception as exc:  # noqa: BLE001 - a snapshot must never break the poll
            _log(f"serve: flow snapshot failed: {exc}")
            return {"flow_version": "1.0.0", "headline": "flow unavailable",
                    "rows": [], "handoffs": [], "counts": {}, "agents": []}

    def _workspace_info(self) -> dict[str, Any]:
        """Where this run is working: the attached folder, or the managed project."""
        ws = self.workspace
        return {
            "path": str(getattr(ws, "path", "")),
            "name": str(getattr(ws, "display_name", getattr(ws, "slug", ""))),
            "attached": bool(getattr(ws, "is_attached", False)),
            "state_dir": str(getattr(ws, "state_dir", "")),
        }

    def _cmd_org(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"org": self._roster()}

    def _cmd_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every model the console may bind, with its window and provenance."""
        catalog = self._catalog()
        return {
            "models": [entry.as_dict() for entry in catalog.list_models()],
            "providers": catalog.status(),
        }

    def _catalog(self) -> Any:
        """The server's discovery catalog, built once and reused.

        Reused for two reasons, both of which matter at the panel's refresh rate:

        - **The TTL cache only works if the object survives.** `ModelCatalog` caches per-provider
          discovery for `refresh_ttl_s` (900s by default). Building a new one per call discards that
          cache, so every panel load would re-probe every provider.
        - **A down provider is expensive.** Each probe spends the transport's full retry budget with
          backoff, so ten unreachable providers on a two-second poll is a UI that never settles.

        Rebuilt only when the configuration changes, which is what `_reload_config` signals by
        dropping this reference.
        """
        from .catalog import ModelCatalog
        from .providers.registry import build_providers

        existing = getattr(self, "_discovery", None)
        if existing is not None:
            return existing
        providers, _ = build_providers(self.config)
        self._discovery = ModelCatalog(self.config, providers)
        return self._discovery

    def _cmd_providers(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every configured provider, its discovery status, and which models it offers.

        Kept in one answer because the console's provider editor needs all three at once: the entry to
        edit, whether it is reachable, and the list to choose a model from. A shape that required three
        round trips would leave the panel half-rendered between them.

        **No key is ever returned.** `has_key` and `api_key_env` are what the UI shows; the value
        itself stays in the file, because a secret that crosses the socket is a secret in a log, a
        screenshot and a crash report.
        """
        from .catalog import ModelCatalog
        from .providers.registry import build_providers

        _, skipped = build_providers(self.config)
        catalog = self._catalog()

        entries: list[dict[str, Any]] = []
        # The catalog caches discovery for `refresh_ttl_s` (900s by default), which is the whole point
        # of it. Reading status before that cache is populated reported every provider as "unknown",
        # so the refresh has to happen — but through the *shared* catalog, not a fresh one per call.
        # A fresh catalog per call would re-probe every provider on every panel load, and a provider
        # that is down costs its full retry budget each time: the console would appear to hang.
        catalog.refresh()
        status = catalog.status()
        for pid, spec in sorted(self.config.providers.items()):
            models = []
            try:
                models = [e.as_dict() for e in catalog.list_models(provider_id=pid)]
            except Exception:  # noqa: BLE001 - a discovery failure must not break the list
                models = []
            entries.append({
                "id": pid,
                "kind": spec.kind,
                "base_url": spec.base_url,
                "api_key_env": spec.api_key_env or "",
                # Whether a key is *present*, never the key. `resolve_key` checks the environment
                # first, so this is true when the variable is set even if the file has no literal.
                "has_key": bool(spec.resolve_key()),
                "api_version": spec.api_version or "",
                # Derived, never stored: present only when the configured base was corrected on load,
                # so an existing config with a full endpoint explains itself in the panel.
                "base_url_note": spec.base_url_note,
                "timeout_s": spec.timeout_s,
                "max_retries": spec.max_retries,
                "concurrency": spec.concurrency,
                "model_aliases": dict(spec.model_aliases),
                # Header *names* only: a value may be a credential (see `ProviderConfig.__repr__`).
                "headers": sorted(spec.extra_headers),
                "status": (status.get(pid) or {}).get("status", "unknown"),
                "error": (status.get(pid) or {}).get("error"),
                "model_count": len(models),
                "models": models,
            })
        return {
            "providers": entries,
            "skipped": list(skipped),
            "kinds": ["openai", "anthropic", "ollama"],
            # Tell the console which file an edit will touch, so the panel can show the path rather
            # than the operator having to guess which of the candidates was loaded.
            "config_path": str(self.config.path) if self.config.path else "",
        }

    def _cmd_provider_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Test a provider entry *before* it is saved, and return the models it offers.

        The candidate is built in memory from the payload, so testing cannot half-write a
        configuration. Testing an unsaved entry is the whole point: "add a key and a URL, fetch the
        models" has to work on the values in the form, not on values already committed to disk.
        """
        from .catalog import ModelCatalog
        from .config import Config, ConfigError, ProviderConfig
        from .providers.registry import build_provider

        candidate = self._provider_spec_from_payload(payload)
        try:
            adapter = build_provider(candidate)
        except ConfigError as exc:
            # A configuration problem (bad kind, missing key) is an *answer* to "test this", not a
            # server error — it is reported with the same success flag a network failure uses.
            return {"ok": False, "reason": str(exc), "models": []}

        # A config carrying only the candidate: the probe must not accidentally discover the models
        # of a *different* provider that happens to share an id in the saved file.
        single = Config(providers={candidate.id: candidate}, known_models=dict(self.config.known_models))
        catalog = ModelCatalog(single, {candidate.id: adapter})
        try:
            models = [e.as_dict() for e in catalog.list_models(provider_id=candidate.id, refresh=True)]
        except Exception as exc:  # noqa: BLE001 - an unreachable endpoint is a result, not a crash
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "models": []}

        status = catalog.status().get(candidate.id) or {}
        status_name = str(status.get("status") or "")
        # The catalog appends `+configured` when it falls back to the offline model table, so the
        # *reachability* verdict is the base word: `down+configured` means the endpoint is down and
        # the model list came from configuration. Testing the whole string would report "down" as
        # reachable — the one answer this command exists to give.
        base_status = status_name.split("+", 1)[0]
        reachable = base_status not in ("down", "error", "")
        # A reachable endpoint with no models is a real state (a gateway that lists nothing); report
        # it as reachable so the operator sees "connected, no models" rather than a bare failure.
        return {
            "ok": reachable and bool(models),
            "reachable": reachable,
            "reason": status.get("error") or ("" if models else "the endpoint returned no models"),
            "status": status_name,
            "models": models,
            "model_count": len(models),
            # The base actually probed, and why it differs from what was typed. This is what turns
            # "it didn't work" into "your URL was the full endpoint; I used the base" — the difference
            # between a user re-checking a correct key and one who knows what happened.
            "base_url": candidate.base_url,
            "note": candidate.base_url_note,
        }

    def _cmd_provider_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add or update one provider in `credentials.json`, then reload the live config.

        The config is reloaded in place rather than left stale, so the very next `models` or `start`
        sees the new endpoint. A roster change without a reload is how "I added it and nothing used it"
        happens.
        """
        from .config import ConfigError, load, write_provider

        if not self.config.path:
            raise ServerError("this engine was started without a credentials file, so there is "
                              "nowhere to save a provider")
        spec = self._provider_spec_from_payload(payload)
        entry: dict[str, Any] = {
            "kind": spec.kind,
            "base_url": spec.base_url,
            "timeout_s": spec.timeout_s,
            "max_retries": spec.max_retries,
            "concurrency": spec.concurrency,
        }
        # Prefer the environment variable when one is named: that is the documented safe path, and a
        # literal written back over it would silently move the secret into the file.
        if spec.api_key_env:
            entry["api_key_env"] = spec.api_key_env
        elif spec.api_key:
            entry["api_key"] = spec.api_key
        if spec.api_version:
            entry["api_version"] = spec.api_version
        if spec.extra_headers:
            entry["extra_headers"] = dict(spec.extra_headers)
        if spec.model_aliases:
            entry["model_aliases"] = dict(spec.model_aliases)

        try:
            target = write_provider(self.config.path, entry, provider_id=spec.id)
        except ConfigError as exc:
            raise ServerError(str(exc)) from exc

        self._reload_config()
        self.emit(Event(seq=0, type=EventType.MODEL_CATALOG_REFRESHED,
                        payload={"provider_id": spec.id, "reason": "provider saved"}))
        return {"saved": str(target), "provider_id": spec.id,
                # The normalised base and, when it differs from what was typed, why. The console
                # shows this so the correction is *told*, not silently applied — the user pasted a
                # real endpoint and deserves to know it was understood.
                "base_url": spec.base_url,
                "note": spec.base_url_note,
                "providers": self._cmd_providers({})["providers"]}

    def _cmd_provider_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove one provider from `credentials.json`."""
        from .config import ConfigError, write_provider

        pid = str(payload.get("provider_id") or payload.get("id") or "").strip()
        if not pid:
            raise ServerError("provider_remove needs a provider_id")
        if not self.config.path:
            raise ServerError("this engine was started without a credentials file")
        try:
            write_provider(self.config.path, {}, provider_id=pid, remove=True)
        except ConfigError as exc:
            raise ServerError(str(exc)) from exc
        self._reload_config()
        self.emit(Event(seq=0, type=EventType.MODEL_CATALOG_REFRESHED,
                        payload={"provider_id": pid, "reason": "provider removed"}))
        return {"removed": pid, "providers": self._cmd_providers({})["providers"]}

    def _provider_spec_from_payload(self, payload: dict[str, Any]) -> Any:
        """Build a `ProviderConfig` from a console payload, validating what it must.

        Shared by test and add so the two cannot disagree about what a valid entry is — a provider
        that tests green and then fails to save is worse than one that fails both.
        """
        from .config import ProviderConfig

        pid = str(payload.get("provider_id") or payload.get("id") or "").strip()
        if not pid:
            raise ServerError("a provider needs an id")
        kind = str(payload.get("kind") or "openai").strip().lower()
        if kind not in ("openai", "anthropic", "ollama"):
            raise ServerError(f"unsupported provider kind {kind!r}; use openai, anthropic or ollama")
        base_url = str(payload.get("base_url") or "").strip()
        if not base_url:
            raise ServerError("a provider needs a base_url")

        raw_headers = payload.get("extra_headers") or payload.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise ServerError("extra_headers must be an object of name -> value")
        headers = {str(k).strip(): str(v) for k, v in raw_headers.items() if str(k).strip()}

        api_key_env = str(payload.get("api_key_env") or "").strip() or None
        # An explicit null/empty key means "no key" — a local endpoint — rather than "leave it
        # unchanged", which is what an absent field would mean if this merged. It does not merge.
        api_key = payload.get("api_key")
        api_key = str(api_key).strip() if isinstance(api_key, str) and api_key.strip() else None

        try:
            timeout_s = float(payload.get("timeout_s") or 120)
            max_retries = int(payload.get("max_retries") or 3)
            concurrency = int(payload.get("concurrency") or 2)
        except (TypeError, ValueError) as exc:
            raise ServerError(f"timeout, retries and concurrency must be numbers: {exc}") from exc

        return ProviderConfig(
            id=pid, kind=kind, base_url=base_url, api_key=api_key, api_key_env=api_key_env,
            api_version=str(payload.get("api_version") or "").strip() or None,
            timeout_s=timeout_s, max_retries=max_retries, concurrency=concurrency,
            extra_headers=headers)

    def _reload_config(self) -> None:
        """Re-read the configuration in place after a provider edit.

        In place, because the server and every panel read `self.config`: swapping the object would
        leave the console talking to a stale config while the file on disk said something else.

        The **same file** is re-read rather than re-discovered. `load()` with no path walks its
        candidate list, which resolves `$AGENTORG_CREDENTIALS` and then `./credentials.json` — so a
        server started with an explicit path would silently reload a *different* config, and the edit
        would appear to have been ignored.
        """
        from .config import load

        try:
            fresh = load(self.config.path, warn=False) if self.config.path else load(warn=False)
        except Exception as exc:  # noqa: BLE001 - a broken edit must not kill the server
            _log(f"serve: could not reload the configuration: {exc}")
            return
        self.config = fresh
        # Drop the cached discovery so the next read probes the new endpoint rather than serving
        # models from the provider that was just replaced. This is the one signal that the catalog
        # must be rebuilt — otherwise the TTL would keep a stale provider list for 15 minutes.
        self._discovery = None

    def _default_window(self, provider: str, model: str) -> tuple[int | None, int | None, str]:
        """The window (and max output) the system will bind the default model with, and its source.

        Resolved as a hire resolves it: an explicit `defaults.context_window` wins, then the live
        catalog's probed value, then the declared table. The console and the CLI must report the same
        number — they did not, because this handler read only the declared table.
        """
        spec = self.config.default_model_spec()
        if spec.context_window and spec.model_id == model:
            return int(spec.context_window), spec.max_output, spec.source
        try:
            from .catalog import ModelCatalog

            entry = self._catalog().resolve(provider, model)
            if entry is not None and entry.window_known:
                return int(entry.context_window), entry.max_output, entry.source
        except Exception:  # noqa: BLE001 - a probe failure degrades to "unknown"
            pass
        return None, None, ""

    def _cmd_defaults(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The default provider/model everyone uses, and how autonomous a goal is by default.

        Reports the **effective** pair, not just the file: a declared default that a removed provider
        invalidated resolves to a usable one, and the reason travels so the panel can say why. No key
        is ever returned.
        """
        config = self.config
        provider, model, reason = config.default_pair()
        # The window the system will *actually* bind with — catalog first, declared table second —
        # exactly as the CLI reports it and as `hire` resolves it. Reading only the declared table told
        # the console `window: None` for `Olla/deepseek-v4.1-flash` (a probed model the config never
        # declares) while the same run bound 1048576, so the panel and the behaviour disagreed.
        window, max_output, window_source = self._default_window(provider, model)
        return {
            "provider": provider,
            "model": model,
            "reason": reason,
            "context_window": window,
            "window_source": window_source,
            "max_output": max_output,
            "temperature": config.default.temperature,
            "declared": config.default.as_dict(),
            "reviewer": {"provider": config.default.reviewer_provider,
                         "model": config.default.reviewer_model},
            "autonomy": {
                "auto_pass_auto_gates": config.goal.auto_pass_auto_gates,
                "auto_hire_missing": config.goal.auto_hire_missing,
                "persist_auto_hires": config.goal.persist_auto_hires,
                "auto_hire_max_tier": config.goal.auto_hire_max_tier,
                "token_budget": config.goal.token_budget,
            },
            "usable": config._usable_providers(),
            "providers": sorted(config.providers),
            "config_path": str(config.path) if config.path else "",
        }

    def _cmd_defaults_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set the default provider and/or model in `credentials.json`, atomically.

        Validated against what is actually configured *before* the write: a default naming a provider
        that does not exist would resolve to a fallback and silently not be what the person chose, so
        it is refused with the list of real providers.
        """
        from .config import ConfigError, set_defaults

        if not self.config.path:
            raise ServerError("this engine was started without a credentials file")
        provider = str(payload.get("provider") or "").strip()
        model = str(payload.get("model") or "").strip()
        if provider and provider not in self.config.providers:
            raise ServerError(
                f"unknown provider {provider!r}; configured: "
                f"{', '.join(sorted(self.config.providers)) or '(none)'}")
        window = payload.get("context_window")
        try:
            set_defaults(
                self.config.path,
                provider=provider,
                model=model,
                reviewer_provider=str(payload.get("reviewer_provider") or "").strip(),
                reviewer_model=str(payload.get("reviewer_model") or "").strip(),
                context_window=int(window) if window not in (None, "") else None,
            )
        except (ConfigError, ValueError) as exc:
            raise ServerError(str(exc)) from exc
        self._reload_config()
        self.emit(Event(seq=0, type=EventType.MODEL_CATALOG_REFRESHED,
                        payload={"reason": "defaults set", "provider": provider,
                                 "model": model}))
        return self._cmd_defaults({})

    def _cmd_autonomy_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set how autonomous a goal is by default, in `credentials.json`, atomically.

        Kept separate from the model default on purpose: changing which model the org runs on must not
        silently change whether a run needs a person. A boolean passed as `None` is left untouched, so
        a panel can send only the switch that changed.
        """
        from .config import ConfigError, set_autonomy

        if not self.config.path:
            raise ServerError("this engine was started without a credentials file")
        goal: dict[str, Any] = {}
        for key, payload_key in (("auto_pass_auto_gates", "auto_pass_auto_gates"),
                                 ("auto_hire_missing", "auto_hire_missing"),
                                 ("persist_auto_hires", "persist_auto_hires")):
            value = payload.get(payload_key)
            if value is not None:
                goal[key] = bool(value)
        if payload.get("auto_hire_max_tier") is not None:
            goal["auto_hire_max_tier"] = int(payload["auto_hire_max_tier"])
        if not goal:
            raise ServerError("autonomy_set needs at least one setting")
        try:
            set_autonomy(self.config.path, goal=goal)
        except ConfigError as exc:
            raise ServerError(str(exc)) from exc
        self._reload_config()
        self.emit(Event(seq=0, type=EventType.POLICY_CHANGED,
                        payload={"reason": "autonomy set", **goal}))
        return self._cmd_defaults({})

    def _cmd_cache(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The cache picture for this run: hit rate, tokens, and what it has saved.

        Accumulated from the forwarded `llm.response` events rather than read from a gateway, because
        the gateway lives in the executing subprocess — the parent sees only what crosses the bus.
        Every figure is `None` when no provider reported a cache, because a hit rate invented from
        silence would claim caching works when nobody has looked.
        """
        totals = getattr(self, "_cache_totals", None) or {
            "turns": 0, "cache_hit_tokens": None, "cache_miss_tokens": None,
            "cache_saving_usd": None, "cache_reported": False,
        }
        hit = totals.get("cache_hit_tokens")
        miss = totals.get("cache_miss_tokens")
        total = (hit or 0) + (miss or 0)
        return {
            "turns": totals.get("turns", 0),
            "cache_reported": bool(totals.get("cache_reported")),
            "cache_hit_tokens": hit,
            "cache_miss_tokens": miss,
            "cache_hit_rate": (round((hit or 0) / total, 4)
                               if totals.get("cache_reported") and total > 0 else None),
            "cache_saving_usd": totals.get("cache_saving_usd"),
        }

    def _accumulate_cache(self, event: Any) -> None:
        """Fold one forwarded event's cache figures into the run totals."""
        payload = getattr(event, "payload", None) or {}
        cost = payload.get("cost") if isinstance(payload, dict) else None
        if not isinstance(cost, dict):
            return
        totals = getattr(self, "_cache_totals", None)
        if totals is None:
            totals = {"turns": 0, "cache_hit_tokens": None, "cache_miss_tokens": None,
                      "cache_saving_usd": None, "cache_reported": False}
            self._cache_totals = totals
        totals["turns"] += 1
        for key in ("cache_hit_tokens", "cache_miss_tokens"):
            value = cost.get(key)
            if value is not None:
                totals[key] = (totals[key] or 0) + int(value)
                totals["cache_reported"] = True
        saving = cost.get("cache_saving_usd")
        if saving is not None:
            totals["cache_saving_usd"] = (totals["cache_saving_usd"] or 0.0) + float(saving)

    def _cmd_pool(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The task pool, so a console can show queued work and who can claim it."""
        from .pool import TaskPool

        pool = TaskPool(self.workspace.pool_path)
        return {"summary": pool.summary(),
                "tasks": [t.as_dict() for t in pool.tasks.values()]}

    def _cmd_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._cmd_status(payload)

    # -- the run lifecycle --------------------------------------------------

    def _cmd_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Plan (and unless asked not to, execute) a goal.

        The `start` command the app sends covers both: it proposes a graph and, having done so,
        proceeds — the Owner's approve gate is where a run actually waits.
        """
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise ServerError("start needs a goal")
        slug = str(payload.get("slug") or self.slug)
        self.slug = slug
        orch = self._orchestrator(slug)
        # `auto_staff` is left to the goal's policy (and then the config default), so the console does
        # not have to send a second flag — arming a goal *is* the authorisation. An explicit
        # `auto_staff` in the payload overrides, which is what a "report the gap, do not fill it"
        # button would send.
        auto_staff = payload.get("auto_staff")
        run = orch.prepare(goal, slug=slug,
                           max_iterations=int(payload.get("max_iterations") or 3),
                           auto_staff=None if auto_staff is None else bool(auto_staff))
        self._forward_bus(orch)
        self.emit(Event(seq=0, type=EventType.MANIFEST_PROPOSED,
                        payload={"run_id": run.run_id, "slug": run.slug, "goal": goal,
                                 "validated": True,
                                 "nodes": [n.get("id") for n in
                                           (run.plan.manifest.get("nodes") if run.plan else [])]}))
        detail = {"run_id": run.run_id, "slug": run.slug, "phase": run.phase.value}

        if payload.get("dry_run"):
            return detail

        # Execute on this worker thread: the read loop stays free so /pause and /abort are honoured
        # while the run is in flight.
        orch.approve(run)
        self.emit(Event(seq=0, type=EventType.MANIFEST_APPROVED,
                        payload={"run_id": run.run_id, "slug": run.slug}))
        outcome = orch.execute(run)
        detail["phase"] = run.phase.value
        detail["outcome"] = (outcome.summary or {}).get("outcome") or outcome.state.value
        detail["gated"] = bool(getattr(outcome, "gated", False))
        return detail

    def _cmd_approve(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch, run = self._current_run()
        if run is None:
            raise ServerError("no run is loaded to approve")
        orch.decide(True, run=run, note=str(payload.get("note") or ""))
        return {"run_id": run.run_id, "phase": run.phase.value}

    def _cmd_reject(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch, run = self._current_run()
        if run is None:
            raise ServerError("no run is loaded to reject")
        orch.decide(False, run=run, note=str(payload.get("note") or ""))
        return {"run_id": run.run_id, "phase": run.phase.value}

    def _cmd_instruct(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch, run = self._current_run()
        if run is None:
            raise ServerError("no run is loaded to instruct")
        text = str(payload.get("text") or payload.get("instruction") or "").strip()
        if not text:
            raise ServerError("instruct needs text")
        orch.instruct(text, run=run, as_constraint=bool(payload.get("constraint")))
        return {"run_id": run.run_id, "instruction": text}

    def _cmd_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self.orchestrator
        if orch is None:
            raise ServerError("no run is loaded to resume")
        run = orch.resume()
        return {"run_id": run.run_id, "phase": run.phase.value}

    def _cmd_abort(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Stop the run, keeping its checkpoint so it can be resumed.

        A best-effort signal: the app posts it without awaiting, so it must never raise for the
        ordinary case of nothing running.
        """
        orch = self.orchestrator
        if orch is None:
            return {"aborted": False, "reason": "no run is loaded"}
        try:
            orch.abort()
            return {"aborted": True}
        except Exception as exc:  # noqa: BLE001 - aborting nothing is not an error worth raising
            return {"aborted": False, "reason": str(exc)}

    def _cmd_pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self.orchestrator
        if orch is None:
            return {"paused": False, "reason": "no run is loaded"}
        try:
            orch.pause()
            return {"paused": True}
        except Exception as exc:  # noqa: BLE001
            return {"paused": False, "reason": str(exc)}

    def _cmd_reassign(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch, run = self._current_run()
        if run is None:
            raise ServerError("no run is loaded to reassign")
        node = str(payload.get("node") or "").strip()
        agent_id = str(payload.get("agent_id") or "").strip()
        if not node or not agent_id:
            raise ServerError("reassign needs node and agent_id")
        orch.reassign(node, agent_id, run=run)
        return {"node": node, "agent_id": agent_id}

    def _cmd_takeover(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch, run = self._current_run()
        if run is None:
            raise ServerError("no run is loaded to take over")
        node = str(payload.get("node") or "").strip()
        if not node:
            raise ServerError("takeover needs a node")
        orch.takeover(node, run=run)
        return {"node": node}

    def _cmd_hire(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Hire an agent from the console, into the same roster the CLI writes."""
        people = self._people()
        org = people.load(project=self._project_dir())
        spec = people.hire(self._hire_request(payload), org=org)
        self._sync_org()
        return {"agent": spec.as_dict(), "warnings": people.warnings}

    def _cmd_agents(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every agent in the effective roster, with what the console needs to edit one.

        The built-ins are included, because they *are* the org until the Owner changes it — a panel
        that listed only hired agents would look empty on a fresh install while seven agents were
        actually running. `origin` distinguishes them, and `editable` says which can be changed.
        """
        people = self._people()
        org = people.load(project=self._project_dir())
        agents: list[dict[str, Any]] = []
        for spec in sorted(org.agents.values(), key=lambda a: (_is_hired(a), a.name)):
            entry = spec.as_dict()
            entry["editable"] = spec.id != "ag_owner"
            entry["origin"] = spec.origin
            entry["skills"] = list(spec.skills)
            entry["provider"] = spec.provider
            entry["model"] = spec.model
            entry["context_window"] = spec.context_window
            entry["level"] = spec.level.value if hasattr(spec.level, "value") else str(spec.level)
            # `hired` is the Owner's own agents; `built-in` is the default company; the Owner
            # principal is neither, so the panel can present it as authority rather than headcount.
            if spec.id == "ag_owner":
                entry["status"] = "owner"
            else:
                entry["status"] = "hired" if _is_hired(spec) else "built-in"
            agents.append(entry)
        return {
            "agents": agents,
            "count": len(agents),
            # The Owner principal is *not* a hire — it is the terminal authority the engine always
            # has. Counting it would make a fresh install report one hired agent that nobody hired.
            "hired": sum(1 for a in agents
                         if a["origin"] == "owner" and a.get("id") != "ag_owner"),
            "roster_path": str(self._roster_file()),
            "skills": self._skill_names(),
        }

    def _cmd_agent_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Change an agent's name, model, skill level or limits — keeping its id and history."""
        from .people import HireError

        agent_id = str(payload.get("agent_id") or payload.get("id") or "").strip()
        if not agent_id:
            raise ServerError("agent_update needs an agent_id")
        people = self._people()
        people.load(project=self._project_dir())
        try:
            spec = people.update_agent(
                agent_id,
                name=payload.get("name") if isinstance(payload.get("name"), str) else None,
                provider=payload.get("provider") if isinstance(payload.get("provider"), str) else None,
                model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                context_window=(int(payload["context_window"])
                                if payload.get("context_window") else None),
                level=payload.get("level") if isinstance(payload.get("level"), str) else None,
                team=payload.get("team") if isinstance(payload.get("team"), str) else None,
                title=payload.get("title") if isinstance(payload.get("title"), str) else None,
                max_concurrency=(int(payload["max_concurrency"])
                                 if payload.get("max_concurrency") else None),
                )
        except (HireError, ValueError, TypeError) as exc:
            raise ServerError(str(exc)) from exc
        self._sync_org()
        return {"agent": spec.as_dict(), "warnings": people.warnings}

    def _cmd_agent_retire(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove an agent from the roster."""
        from .people import HireError

        agent_id = str(payload.get("agent_id") or payload.get("id") or "").strip()
        if not agent_id:
            raise ServerError("agent_retire needs an agent_id")
        people = self._people()
        people.load(project=self._project_dir())
        try:
            spec = people.retire_agent(agent_id, reason=str(payload.get("reason") or ""))
        except HireError as exc:
            raise ServerError(str(exc)) from exc
        self._sync_org()
        return {"retired": spec.as_dict(),
                "agents": self._cmd_agents({})["agents"]}

    def _cmd_skills(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The skills an agent can be hired for, so the console offers real ones rather than a guess."""
        return {"skills": self._skill_names()}

    def _people(self) -> Any:
        """A roster manager over this workspace's project root.

        Built fresh per call so a hire or an edit always reads the current file — a cached manager
        would happily re-write a roster that another change had already moved on from.
        """
        from .catalog import ModelCatalog
        from .people import People
        from .providers.registry import build_providers

        providers, _ = build_providers(self.config)
        return People(library=self.library, config=self.config,
                      catalog=ModelCatalog(self.config, providers),
                      project=self._project_dir())

    def _project_dir(self) -> Any:
        """The project the console is pointed at — the directory a roster is discovered from.

        `workspace.path`, not `workspace.root`: for an attached project the root is the *parent*
        directory, so a hire would land one level above the repository and the user would never find
        it.
        """
        return getattr(self.workspace, "path", self.workspace.root)

    def _roster_file(self) -> Any:
        """The exact file a hire writes, so the console can show it rather than describe it vaguely."""
        from . import usercfg

        return usercfg.roster_path(project=self._project_dir())

    def _sync_org(self) -> None:
        """Point a loaded run's orchestrator at the refreshed roster.

        Without this a run already holding an `Org` keeps the old object, so a just-hired agent is
        absent from the run that is about to use it — the "I hired it and nothing changed" failure.
        """
        if self.orchestrator is not None:
            self.orchestrator.org = self._people().load(project=self._project_dir())

    def _hire_request(self, payload: dict[str, Any]) -> Any:
        """Turn a console payload into a validated hire request."""
        from .people import HireRequest

        return HireRequest(
            name=str(payload.get("name") or ""),
            skill=str(payload.get("skill") or ""),
            provider=str(payload.get("provider") or ""),
            model=str(payload.get("model") or ""),
            context_window=(int(payload["context_window"]) if payload.get("context_window") else None),
            level=str(payload.get("level") or "senior"),
            role=str(payload.get("role") or "worker"),
            team=str(payload.get("team") or ""),
            title=str(payload.get("title") or ""),
            purpose=str(payload.get("purpose") or ""),
            max_concurrency=int(payload.get("max_concurrency") or 1),
            as_reviewer=bool(payload.get("as_reviewer")),
        )

    def _skill_names(self) -> list[str]:
        """The skill names the library exposes, sorted. Empty when no library is attached.

        Built through a skill *source* rather than off the `Library` handle, because the names live in
        the source (the flat discovery layer plus the nested tree and the Owner's own roots) — reading
        the handle would return nothing and the hire form would offer an empty skill picker.
        """
        try:
            from .orchestrator import _skill_source

            source = _skill_source(self.library, project=self._project_dir())
            return sorted(str(name) for name in source.names())
        except Exception:  # noqa: BLE001 - a missing library yields an empty list, not a crash
            return []

    def _cmd_swarm(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The current swarm picture: any fan-out in flight, and the last vote tally.

        The console needs this as a *snapshot* command, not only as events, for the same reason the
        run status is polled: a UI that only listens can miss a transition and then show a stale
        swarm forever.
        """
        return {
            "fanout": getattr(self, "_last_fanout", None),
            "vote": getattr(self, "_last_vote", None),
            "running": bool(getattr(self, "_fanout_running", False)),
        }

    def _goal_orchestrator(self) -> Any:
        """The orchestrator a goal command acts on, created and subscribed if necessary.

        One accessor, because every goal command needs the same two things — an orchestrator that
        exists, and a bus the app is subscribed to. Doing it ad hoc per handler is how a command ends
        up mutating state silently.
        """
        orch = self.orchestrator
        if orch is None:
            orch = self._orchestrator(self.slug)
        self._forward_bus(orch)
        return orch

    def _cmd_goal_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set the objective and arm the loop, from the console.

        Accepts the per-goal autonomy flags, so the console's "human gate on/off" switch is one call:
        an unchanged objective with a changed policy replaces the policy and keeps the history, rather
        than forcing a clear-and-restart.
        """
        from .goal import GoalPolicy

        objective = str(payload.get("objective") or payload.get("goal") or "").strip()
        if not objective:
            raise ServerError("goal_set needs an objective")
        orch = self._goal_orchestrator()
        armed = not bool(payload.get("no_arm"))
        base = orch._default_goal_policy()
        policy = GoalPolicy(
            auto_approve=(base.auto_approve if payload.get("auto_approve") is None
                          else bool(payload.get("auto_approve"))),
            auto_hire=(base.auto_hire if payload.get("auto_hire") is None
                       else bool(payload.get("auto_hire"))),
            persist_hires=(base.persist_hires if payload.get("persist_hires") is None
                           else bool(payload.get("persist_hires"))),
            human_gate=bool(payload.get("human_gate", False)),
        )
        orch.goal_set(objective, armed=armed, by="app", policy=policy)
        return self._goal_detail(orch)

    def _cmd_proposals(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every proposal the improver has produced, newest first, plus what it refused.

        Both halves matter. A list that showed only promotions would hide the boundary working, and the
        refusals are how a person sees that a fix aimed at the eval gate was stopped rather than
        quietly dropped.
        """
        from .improver import PROPOSALS_DIRNAME

        directory = self.workspace.state_dir / PROPOSALS_DIRNAME
        proposals: list[dict[str, Any]] = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                data["file"] = str(path.with_suffix(".md"))
                proposals.append(data)
        proposals.sort(key=lambda p: str(p.get("at") or ""), reverse=True)

        refused: list[dict[str, Any]] = []
        rejected_path = directory / "rejected.jsonl"
        if rejected_path.is_file():
            for line in rejected_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    refused.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return {
            "proposals": proposals,
            "count": len(proposals),
            "refused": refused[-20:],
            "refused_count": len(refused),
            "directory": str(directory),
            # Stated so the console can say it plainly rather than implying otherwise.
            "applies_changes": False,
        }

    def _cmd_improve(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one improver cycle, in the background, and report what it considered.

        Runs on this worker thread, which is what makes it *background*: the read loop stays free, so
        the console keeps answering while the cycle runs. Nothing is applied — the cycle's whole output
        is proposals on disk — so there is no state left half-changed if the app is quit mid-cycle.
        """
        from .improver import Improver

        improver = Improver(
            workspace=self.workspace,
            memory=getattr(self, "_memory", None),
        )
        considered = improver.run_once()
        self.emit(Event(seq=0, type=EventType.AGENT_LOG,
                        payload={"text": f"improver: considered {len(considered)} finding(s)",
                                 "stream": "stderr"}))
        return {
            "considered": [
                {"id": p.proposal_id, "kind": p.finding.kind, "state": p.state,
                 "improved": p.validation.improved, "regressions": p.validation.regressions,
                 "refusal": p.refusal}
                for p in considered
            ],
            "count": len(considered),
            "applies_changes": False,
            **self._cmd_proposals({}),
        }

    def _cmd_goal_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The goal picture. Answered even with no orchestrator, so the panel renders on first open."""
        orch = self.orchestrator
        if orch is None:
            return {"goal": {"objective": "", "state": "cleared", "live": False, "open": False,
                             "budget_enabled": False, "token_budget": 0, "pause_reason": "",
                             "summary": "", "blocked_reason": "", "slice": {}, "spend": {},
                             "history": []}}
        return {"goal": orch.goal_status()}

    def _cmd_subagent_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Page one child's transcript from the console.

        The same byte-addressed read the agent's `read_subagent_result` tool performs, exposed so a
        person can inspect what a child actually did without the panel holding the whole transcript.
        """
        from .subagents import ChildStore, SubagentError

        child_id = str(payload.get("child_id") or "").strip()
        if not child_id:
            raise ServerError("subagent_result needs a child_id")
        run_id = ""
        orch = self.orchestrator
        if orch is not None and getattr(orch, "_run", None) is not None:
            run_id = getattr(orch._run, "run_id", "") or ""
        store = ChildStore(self.workspace, run_id=run_id or "run")
        try:
            page = store.read(child_id=child_id,
                              offset_bytes=int(payload.get("offset_bytes") or 0),
                              limit_bytes=int(payload.get("limit_bytes") or 0))
        except SubagentError as exc:
            raise ServerError(str(exc)) from exc
        return page.as_dict()

    def _cmd_subagents(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every child this run started, as reference frames, for the Work panel's tree.

        Read from disk rather than held in memory, so the tree survives a page refresh and shows what
        actually ran — the console is a window onto the engine, not a second copy of its state.
        """
        from .subagents import ChildStore

        run_id = ""
        orch = self.orchestrator
        if orch is not None and getattr(orch, "_run", None) is not None:
            run_id = getattr(orch._run, "run_id", "") or ""
        store = ChildStore(self.workspace, run_id=run_id or "run")
        children = [ref.as_dict() for ref in store.children()]
        return {
            "children": children,
            "count": len(children),
            "running": sum(1 for c in children if c.get("status") == "running"),
            "failed": sum(1 for c in children if c.get("status") == "failed"),
        }

    def _cmd_goal_pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        if orch.goal() is None:
            raise ServerError("no goal is set")
        orch.goal_pause()
        return self._goal_detail(orch)

    def _cmd_goal_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        if orch.goal() is None:
            raise ServerError("no goal is set")
        orch.goal_resume(by="app")
        return self._goal_detail(orch)

    def _cmd_goal_clear(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        if orch.goal() is None:
            raise ServerError("no goal is set")
        orch.goal_clear()
        return self._goal_detail(orch)

    def _goal_detail(self, orch: Any) -> dict[str, Any]:
        """The command acknowledgement shape, so the app can update its row without re-polling."""
        return {"goal": orch.goal_status()}

    # ── mission ─────────────────────────────────────────────────────────────

    def _cmd_mission(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The mission snapshot: the standing purpose, its objectives and progress.

        A snapshot command rather than only events, for the same reason the goal is one: a UI that
        only listens can miss a transition and show a stale mission forever.
        """
        orch = self.orchestrator
        goal = orch.mission_status() if orch is not None else None
        if goal is None:
            return {"mission": {"statement": "", "state": "empty", "objectives": [],
                                "progress": {"done": 0, "total": 0, "next": ""}}}
        return {"mission": goal}

    def _cmd_mission_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        statement = str(payload.get("statement") or payload.get("mission") or "").strip()
        if not statement:
            raise ServerError("mission_set needs a statement")
        objectives = [str(o) for o in (payload.get("objectives") or []) if str(o).strip()]
        orch.mission_set(statement, objectives=objectives, armed=bool(payload.get("arm")))
        return self._mission_detail(orch)

    def _cmd_mission_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        text = str(payload.get("objective") or "").strip()
        if not text:
            raise ServerError("mission_add needs an objective")
        at = payload.get("at")
        orch.mission_add(text, at=int(at) if at is not None else None)
        return self._mission_detail(orch)

    def _cmd_mission_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_remove(int(payload.get("index") or 0))
        return self._mission_detail(orch)

    def _cmd_mission_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Hand an objective to a goal — the one place the mission decides what gets worked."""
        orch = self._goal_orchestrator()
        index = payload.get("index")
        detail = orch.mission_start(index=int(index) if index is not None else None,
                                    armed=not bool(payload.get("no_arm")), by="app")
        return detail

    def _cmd_mission_advance(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_advance(summary=str(payload.get("summary") or ""))
        return self._mission_detail(orch)

    def _cmd_mission_mark(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_mark(int(payload.get("index") or 0), str(payload.get("state") or "done"),
                          summary=str(payload.get("summary") or ""))
        return self._mission_detail(orch)

    def _cmd_mission_arm(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_arm(by="app")
        return self._mission_detail(orch)

    def _cmd_mission_pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_pause()
        return self._mission_detail(orch)

    def _cmd_mission_clear(self, payload: dict[str, Any]) -> dict[str, Any]:
        orch = self._goal_orchestrator()
        orch.mission_clear()
        return self._mission_detail(orch)

    def _mission_detail(self, orch: Any) -> dict[str, Any]:
        return {"mission": orch.mission_status(), "goal": orch.goal_status()}

    # ── portfolio: one principal, several orgs ──────────────────────────────

    def _load_portfolio(self) -> Any:
        """The portfolio, loaded once. None when there is none (a single-org setup)."""
        if self._portfolio_loaded:
            return self._portfolio
        self._portfolio_loaded = True
        from .portfolio import Portfolio, PortfolioError

        try:
            self._portfolio = Portfolio.load()
        except PortfolioError as exc:  # noqa: BLE001 - a broken register must not kill the server
            _log(f"serve: cannot read the portfolio: {exc}")
            self._portfolio = None
        return self._portfolio

    def _fleet_for(self, portfolio: Any) -> Any:
        """The fleet for this server, built once so its org runtimes persist across commands.

        Built once rather than per command because the fleet *is* the concurrency: an org left running
        on its thread must survive the next command, so the object that holds it must too.
        """
        from .fleet import Fleet

        if self._fleet is None:
            self._fleet = Fleet(config=self.config, library=self.library, portfolio=portfolio,
                                on_event=self._fleet_event)
        return self._fleet

    def _fleet_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Forward a fleet event to the console, so org starts and finishes are visible live."""
        try:
            self.emit({"type": kind, **payload})
        except Exception:  # noqa: BLE001 - an emit failure must not break the fleet
            pass

    def _cmd_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The portfolio snapshot: the principal and every org, loaded or not.

        A snapshot command rather than only events, so the console shows the whole portfolio from the
        poll it already makes. It does **not** load every org — that is `_cmd_portfolio_live` — because
        reading ten rosters on every 2s poll would be wasteful; the register is enough to list them.
        """
        portfolio = self._load_portfolio()
        if portfolio is None:
            return {"portfolio": None, "principal": None, "active_org_id": "", "orgs": [],
                    "counts": {"orgs": 0, "enabled": 0, "missing": 0}}
        return {
            "portfolio": {"principal": portfolio.principal.as_dict(),
                          "active_org_id": portfolio.active_org_id,
                          "counts": portfolio.inspect()["counts"]},
            "principal": portfolio.principal.as_dict(),
            "active_org_id": portfolio.active_org_id,
            "orgs": portfolio.inspect()["orgs"],
        }

    def _cmd_portfolio_live(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The live cross-org view: load every org and gather its mission, spend and blockers.

        Expensive by nature — it builds an orchestrator per org — so it is a deliberate command the
        Portfolio panel sends when open, not part of the status poll.
        """
        portfolio = self._load_portfolio()
        if portfolio is None:
            return {"rollup": None, "fleet": None}
        fleet = self._fleet_for(portfolio)
        for entry in portfolio.orgs:
            try:
                fleet._runtime_for(entry.id)
            except Exception as exc:  # noqa: BLE001 - one bad org must not blank the view
                _log(f"serve: org {entry.slug} failed to load: {exc}")
        return {"rollup": fleet.rollup(), "fleet": fleet.status()}

    def _cmd_portfolio_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one org from the console, in parallel with any other org already running."""
        portfolio = self._load_portfolio()
        if portfolio is None:
            raise ServerError("no portfolio; add an org from the CLI first")
        ref = str(payload.get("org") or "").strip()
        if not ref:
            raise ServerError("portfolio_run needs an org")
        fleet = self._fleet_for(portfolio)
        try:
            handle = fleet.run_org(ref, goal=str(payload.get("goal") or ""),
                                   manifest=str(payload.get("manifest") or ""),
                                   background=bool(payload.get("background", True)))
        except Exception as exc:  # noqa: BLE001 - a refusal is a result, said plainly
            raise ServerError(str(exc)) from exc
        return {"handle": handle.as_dict(), "fleet": fleet.status()}

    def _cmd_portfolio_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        portfolio = self._load_portfolio()
        if portfolio is None:
            raise ServerError("no portfolio")
        ref = str(payload.get("org") or "").strip()
        fleet = self._fleet_for(portfolio)
        try:
            detail = fleet.stop_org(ref)
        except Exception as exc:  # noqa: BLE001
            raise ServerError(str(exc)) from exc
        return {"stopped": detail, "fleet": fleet.status()}

    def _cmd_portfolio_select(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Select the active org — the one bare console commands act on."""
        portfolio = self._load_portfolio()
        if portfolio is None:
            raise ServerError("no portfolio")
        ref = str(payload.get("org") or "").strip()
        try:
            entry = portfolio.set_active(ref)
            portfolio.save()
        except Exception as exc:  # noqa: BLE001
            raise ServerError(str(exc)) from exc
        return {"active_org_id": entry.id, "active_org": entry.as_dict()}

    def _cmd_portfolio_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Register an org from the console."""
        from .portfolio import Portfolio

        portfolio = self._load_portfolio()
        if portfolio is None:
            portfolio = Portfolio.new()
            self._portfolio = portfolio
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ServerError("portfolio_add needs a name")
        try:
            entry = portfolio.add_org(
                name=name, slug=str(payload.get("slug") or ""),
                path=str(payload.get("path") or ""), charter=str(payload.get("charter") or ""),
                daily_budget_usd=float(payload.get("daily_budget_usd") or 0.0),
                make_active=bool(payload.get("active")))
            portfolio.save()
        except Exception as exc:  # noqa: BLE001
            raise ServerError(str(exc)) from exc
        return {"org": entry.as_dict(), "portfolio": self._cmd_portfolio({})["portfolio"]}

    def _cmd_portfolio_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        portfolio = self._load_portfolio()
        if portfolio is None:
            raise ServerError("no portfolio")
        ref = str(payload.get("org") or "").strip()
        try:
            entry = portfolio.remove_org(ref)
            portfolio.save()
        except Exception as exc:  # noqa: BLE001
            raise ServerError(str(exc)) from exc
        return {"removed": entry.as_dict(), "portfolio": self._cmd_portfolio({})["portfolio"]}

    def _cmd_fanout(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Start a fan-out from the console: a template, some items, and go.


        This is the native-app surface for the *fan-out* primitive — the one the engine could already
        do and no UI could reach. The validation is the same as the executor's, because both go
        through `plan_fanout`, so a template the CLI would refuse is refused here with the same words.
        """
        from .fanout import FanoutError, plan_fanout, run_fanout

        template = str(payload.get("template") or "").strip()
        raw_items = payload.get("items") or []
        if not isinstance(raw_items, list):
            raise ServerError("fanout needs an items array")
        items = [str(i) for i in raw_items]
        skill = str(payload.get("skill") or "code-reviewer")
        try:
            plan = plan_fanout(template, items, skill=skill)
        except FanoutError as exc:
            # Refused *before* anything runs, which is the point: a bad template would otherwise
            # cost N duplicate calls.
            raise ServerError(str(exc)) from exc

        orch = self.orchestrator
        if orch is None:
            raise ServerError("start a run first; a fan-out needs a roster to draw agents from")
        agents = [a.id for a in getattr(orch, "org", None).agents.values()
                  if not a.is_human] if getattr(orch, "org", None) is not None else []
        if not agents:
            raise ServerError("the roster has no agents to fan out to")

        concurrency = int(payload.get("max_parallel")
                          or getattr(getattr(self.config, "executor", None),
                                     "fanout_max_parallel", 4))
        self._fanout_running = True
        self._last_fanout = {"count": len(plan), "succeeded": 0, "failed": 0, "running": True,
                             "items": [i.as_dict() for i in plan.items]}

        def _emit(kind: str, event: dict[str, Any]) -> None:
            # Streamed as an event as well as kept in the snapshot, so the panel updates live rather
            # than only on the next poll.
            self.emit(Event(seq=0, type="swarm.fanout", payload={"kind": kind, **event}))
            if kind in ("fanout.item_done", "fanout.item_failed"):
                self._last_fanout["items"] = [i.as_dict() for i in plan.items]
                self._last_fanout["succeeded"] = sum(1 for i in plan.items if i.ok)
                self._last_fanout["failed"] = sum(1 for i in plan.items if not i.ok)

        # A console-driven fan-out calls the model directly rather than spawning a full node run:
        # the panel is asking "review these files", not "build me a graph".
        def _one(item: Any, agent_id: str) -> tuple[str, str, int]:
            from .providers.base import ChatRequest, Message, Role

            gateway = getattr(orch, "gateway", None)
            if gateway is None:
                return "", "no gateway is available for this run", 0
            agent = orch.org.get(agent_id)
            request = ChatRequest(model=agent.model,
                                  messages=[Message.text_message(Role.USER, item.prompt)],
                                  max_tokens=2048)
            response = gateway.complete(request, provider_id=agent.provider, agent_id=agent_id,
                                        node_id=f"fanout:{item.index}")
            usage = response.usage
            tokens = (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
            return response.text, "", tokens

        try:
            run_fanout(plan, _one, agents=agents, max_parallel=concurrency, on_event=_emit)
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised as a crash
            self._fanout_running = False
            raise ServerError(f"the fan-out could not run: {exc}") from exc
        self._fanout_running = False
        summary = plan.summary()
        self._last_fanout = {**summary, "running": False,
                             "items": [i.as_dict() for i in plan.items]}
        return self._last_fanout

    def _cmd_shutdown(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A clean stop: the app asked, so the loop ends rather than being killed."""
        self._stop.set()
        return {"stopping": True}

    # ── helpers ─────────────────────────────────────────────────────────────

    def _orchestrator(self, slug: str) -> Any:
        """Build the orchestrator for a project, reusing the roster the Owner hired."""
        from .bus import EventBus
        from .catalog import ModelCatalog
        from .orchestrator import Orchestrator
        from .people import HireError, People
        from .providers.registry import build_providers
        from .state import Workspace

        # An attached workspace *is* the project: rebuilding from `root`/`slug` would silently point
        # the run at a managed directory beside the user's repository instead of at it.
        if getattr(self.workspace, "is_attached", False) and slug == self.slug:
            workspace = self.workspace
        else:
            workspace = Workspace.for_project(slug, root=self.workspace.root)
        workspace.ensure()
        bus = EventBus(run_id=f"serve_{slug}", trace_path=workspace.trace_path)
        org = None
        try:
            providers, _ = build_providers(self.config)
            org = People(library=self.library, config=self.config,
                         catalog=ModelCatalog(self.config, providers),
                         project=workspace.path).load(project=workspace.path)
        except HireError as exc:
            _log(f"serve: ignoring the user roster: {exc}")
        self.orchestrator = Orchestrator(
            config=self.config, library=self.library, workspace=workspace, bus=bus, org=org)
        self.workspace = workspace
        # Subscribe here rather than at each command that builds an orchestrator. A goal command
        # creates one lazily, so forwarding *only* on `start` meant the first `goal_set` produced
        # events nobody was listening for — the console would show a goal that never announced itself.
        self._forward_bus(self.orchestrator)
        return self.orchestrator

    def _current_run(self) -> tuple[Any, Any | None]:
        if self.orchestrator is None:
            return None, None
        try:
            return self.orchestrator, self.orchestrator.status() and self.orchestrator.load(self.slug)
        except Exception:  # noqa: BLE001 - "no run" is reported by the caller, not raised here
            return self.orchestrator, None

    def _roster(self) -> list[dict[str, Any]]:
        org = getattr(self.orchestrator, "org", None)
        if org is None:
            return []
        return org.roster_view()

    def _cost(self) -> dict[str, Any]:
        """Cost, labelled. An unmeasured figure is never rendered as zero."""
        return {"runs": 0, "nodes": 0, "cost_usd": None}

    def _forward_bus(self, orch: Any) -> None:
        """Forward the run's own events to the app.

        Tapping the existing bus rather than building a second stream is what keeps the console and
        the engine from disagreeing about what happened: there is one producer of events.
        """
        bus = getattr(orch, "bus", None)
        if bus is None or id(bus) in self._forwarded:
            return
        self._forwarded.add(id(bus))
        subscribe = getattr(bus, "subscribe", None)
        if subscribe is None:
            return

        def _on_event(event: Any) -> None:
            try:
                if getattr(event, "type_value", "") == "llm.response":
                    self._accumulate_cache(event)
                self.emit(event)
            except Exception as exc:  # noqa: BLE001 - a forward must not break the run
                _log(f"serve: could not forward an event: {exc}")

        try:
            subscribe(_on_event)
        except Exception as exc:  # noqa: BLE001 - a bus that cannot be tapped is not fatal
            _log(f"serve: could not subscribe to the run bus: {exc}")


def _is_hired(spec: Any) -> bool:
    """Whether an agent was hired by the Owner, rather than being part of the built-in company.

    `origin == "owner"` is not sufficient on its own: the built-in Owner *principal* also carries that
    origin, so a fresh install would report a hired agent nobody hired.
    """
    return (str(getattr(spec, "origin", "")) == "owner"
            and str(getattr(spec, "id", "")) != "ag_owner")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def _pid() -> int:
    import os

    return os.getpid()


def _pid_alive(pid: int) -> bool:
    """Whether a process is still running — excluding a zombie.

    `os.kill(pid, 0)` is the portable existence test, but it returns success for a **zombie**: a dead
    process not yet reaped still has a pid, and signalling it "succeeds". That is not a corner case
    here, it is the normal case — the engine's parent is the app, and when the app dies its status
    becomes `Z` until launchd reaps it. A liveness check that reports "alive" for a process that is
    already dead is worse than no check, because it is a check that cannot fail.

    So the real status is read where the platform exposes it: `/proc/<pid>/stat` on Linux, and
    `proc_pidinfo` on macOS (there is no `/proc` there, and shelling out to `ps` is slow and blocked in
    some sandboxes — which would silently degrade to "alive" and make this useless). When neither is
    available the process is assumed alive, which is the conservative direction: better to keep a
    working engine than to stop a live one.
    """
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else — still alive for our purposes
    except OSError:
        return False

    # It exists; find out whether it is *running* rather than a zombie.
    try:
        proc_stat = Path(f"/proc/{pid}/stat")
        if proc_stat.is_file():
            fields = proc_stat.read_text().rsplit(")", 1)
            if len(fields) == 2:
                return not fields[1].strip().startswith("Z")
    except OSError:
        pass

    try:
        running = _pid_alive_via_libproc(pid)
        if running is not None:
            return running
    except Exception:  # noqa: BLE001 - any failure here must not stop a working engine
        pass

    return True


def _pid_alive_via_libproc(pid: int) -> bool | None:
    """macOS: read the process status with `proc_pidinfo`, returning None when it cannot be determined.

    `PROC_PIDTBSDINFO` (3) fills a `proc_bsdinfo` whose first field is the `pbi_status`: 1 is born, 2 is
    running, 3 is sleeping, 4 is idle and **5 is a zombie**. Reading it directly is what makes a zombie
    distinguishable without a shell command.

    A return of 0 bytes is not "unknown": `errno` says why. `ESRCH` (3) means the pid is gone, which is
    exactly what the caller needs to know — an earlier version treated 0 as undeterminable and reported
    "alive", so the watchdog never fired for the case it exists to catch.
    """
    import ctypes
    import ctypes.util

    libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
    buffer = ctypes.create_string_buffer(256)
    ctypes.set_errno(0)
    written = libproc.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(3), ctypes.c_uint64(0),
                                   buffer, ctypes.c_int(256))
    if written <= 0:
        errno = ctypes.get_errno()
        if errno == 3:      # ESRCH — no such process
            return False
        return None         # genuinely undeterminable (a permission limit, say)
    status = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32)).contents.value
    return status != 5      # 5 == SZOMB


def serve(*, config: Any, library: Any, project: str = "console",
          root: Any = None, project_dir: Any = None,
          stdin: Any = None, stdout: Any = None) -> int:
    """Start the server for a project. Returns an exit code.

    ``project_dir`` attaches an existing folder (the console's *Open Project…*); ``project``/``root``
    are the managed alternative. Exactly one is meaningful, and ``project_dir`` wins because naming a
    folder is more specific than naming a directory of folders.
    """
    from .state import Workspace

    if project_dir:
        workspace = Workspace.attach(project_dir)
    else:
        workspace = Workspace.for_project(project, root=root)
    workspace.ensure()
    server = Server(config=config, library=library, workspace=workspace, slug=project,
                    stdin=stdin, stdout=stdout)
    return server.serve_forever()
