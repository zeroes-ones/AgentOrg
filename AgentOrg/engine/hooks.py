#!/usr/bin/env python3
"""hooks.py — lifecycle hooks, and the notification an unattended run owes you.

WHY THIS EXISTS
---------------
Two gaps, both about a run you leave alone.

The first is *"when a run finishes, do X"*. Every X — ring a bell, post to a webhook, copy the
artifact somewhere, append a line to a team log — would otherwise become a feature decided here and
shipped here, one per want. A lifecycle hook inverts that: the person names the event and the
command, and the engine runs it. The reference agents ship twenty lifecycle events; this engine had
none, and the zero was the gap.

The second is the failure mode a supervised run does not have: it stops, and nobody is looking. A run
in front of you fails loudly. An unattended one parks at a gate, or dies on a schema refusal, and
waits to be noticed — usually until the bill arrives. A notification closes that gap, and it is
deliberately a *short* list of events: a channel that fires for everything is one you learn to ignore,
which is worse than no channel at all.

WHAT A HOOK IS NOT
------------------
**A hook is told what happened; it cannot change the run.** Nothing it prints is parsed, its exit code
decides nothing, and every way it can fail — non-zero, a hang, a binary that does not exist, a config
entry that is not a command — is recorded as a failed hook and swallowed. That is the property the
module is built around, because a hook is an observation, not a second control plane: an engine that
let a `git push` in someone's `hooks.events` decide whether a run continues would be an engine whose
termination nobody can reason about.

DESIGN
------
- **Dispatch is synchronous, and therefore bounded rather than free.** The alternative — a worker
  thread with a queue — cannot deliver the event this feature exists for. Nothing in the engine ever
  calls `EventBus.close()` (`cli.py`, `serve.py`, `fleet.py` and the generated executor all build a
  bus and walk away), so an asynchronous dispatcher would drop whatever is still queued when the
  process exits — and the event that arrives at a process exit is precisely `run.end`. So each command
  runs inline, bounded by `timeout_s`. A person who attaches a hook to a high-frequency event
  (`llm.response`, `node.enter`) is choosing to pay that bound per event; the timeout is what makes
  the choice survivable. **The cost, stated rather than hidden:** `EventBus._deliver` calls
  subscribers while holding the bus lock, so a slow hook blocks other threads' `emit` for up to
  `timeout_s × matched commands`. That is why the default `events` map is empty and the default
  `notify.on` list is five events, and why a hook belongs on a lifecycle event rather than on a token.
- **The command is never interpolated with the payload.** The event arrives on stdin and in the
  environment; the command text stays exactly what the person wrote. A payload carrying `$(…)` can
  therefore never become a command, which is the difference between "a webhook URL with a weird slug
  in it" and remote code execution. A shell *does* expand `$AGENTORG_EVENT` inside the command, since
  the child inherits the variables — that is the shell's own quoting, and it is how a desktop
  notification names the event without any templating from us.
- **Failure is a record, not an exception.** A returned `HookOutcome` carries the status, the exit
  code, the reason and the captured output; the same line is appended to `.agent_state/hooks.jsonl` so
  "did my hook run last night" is answerable after an unattended run.
- **Output is bounded, and captured to a file rather than a pipe.** A hook that prints without end
  would otherwise either fill a pipe and block until its timeout, or fill this process's memory. A
  temporary file bounds both, and the cap is applied when it is read.
- **Redaction happens on the way into the log.** A hook's stdout is arbitrary text and may quote a
  key; a log that once held one is a leak regardless of what a reader later does with it.

Usage:
    from engine.hooks import HookRunner, Notifier, Lifecycle

    # config.hooks.events = {"run.end": "say done", "goal.*": ["ring", "log it"], "*": "tick"}
    runner = HookRunner(cfg, run_id="run_1", state_dir=workspace.state_dir)
    runner.dispatch(event)

    # An unattended run that tells you, in `credentials.json`:
    #   "notify": {"on": ["run.end", "goal.blocked", "human.gate"],
    #              "command": "notify-send AgentOrg \\"$AGENTORG_EVENT\\"",
    #              "url": "https://example.invalid/hooks/agentorg"}
    # The command is a normal shell string: it inherits the child's environment, so `$AGENTORG_EVENT`
    # and the rest expand there. Read the whole event from stdin when a static line is not enough.

    # Or, wiring it at the one place every event passes:
    bus = EventBus(run_id="run_1", trace_path=workspace.trace_path,
                   lifecycle=config, lifecycle_slug=workspace.display_name)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import redact

__all__ = [
    "HooksError",
    "HookOutcome",
    "NotifyOutcome",
    "HookRunner",
    "Notifier",
    "Lifecycle",
    "matches",
    "read_hook_log",
    "HOOK_LOG_NAME",
    "DISABLED_ENV",
    "MAX_OUTPUT_CHARS",
    "MAX_ENV_PAYLOAD",
    "ENV_EVENT",
    "ENV_RUN_ID",
    "ENV_SLUG",
    "ENV_PAYLOAD",
    "ENV_PAYLOAD_TRUNCATED",
]

#: Beside `trace.jsonl` and `ledger.jsonl`: one append-only question — "what did my hooks do".
HOOK_LOG_NAME = "hooks.jsonl"

#: The kill switch, for a process that must not run a workspace's configured hooks: the test suite,
#: or a diagnostic command run in a repository whose `credentials.json` belongs to someone else.
DISABLED_ENV = "AGENTORG_NO_HOOKS"

#: How the child learns what happened. The event goes to stdin as one JSON document; these carry the
#: parts a shell command can use without parsing.
ENV_EVENT = "AGENTORG_EVENT"
ENV_RUN_ID = "AGENTORG_RUN_ID"
ENV_SLUG = "AGENTORG_SLUG"
ENV_PAYLOAD = "AGENTORG_PAYLOAD"
ENV_PAYLOAD_TRUNCATED = "AGENTORG_PAYLOAD_TRUNCATED"

#: How much of a child's stdout/stderr is kept. A hook that dumps a build log must not turn the
#: diagnostics into the largest file in the workspace.
MAX_OUTPUT_CHARS = 4000

#: `AGENTORG_PAYLOAD` is capped well below `ARG_MAX` (~1 MB on macOS, and the inherited environment
#: consumes part of it): a large `llm.response` payload in an environment variable would make `Popen`
#: fail with "argument list too long", turning an observation into a failure. stdin always carries the
#: whole event, so nothing is lost — only the copy that has to fit in an environment.
MAX_ENV_PAYLOAD = 60_000

#: How many attempts are kept in memory for inspection. Bounded because an unattended run can fire
#: hooks hundreds of thousands of times, and the log on disk is the durable record.
OUTCOME_TAIL = 200


class HooksError(RuntimeError):
    """Raised when a runner cannot be built at all — a missing config, not a failing hook.

    A failing hook never raises: it comes back as a `HookOutcome` saying what went wrong.
    """


# ── what happened ────────────────────────────────────────────────────────────


@dataclass
class HookOutcome:
    """One command's attempt at one event."""

    event: str
    command: str
    ok: bool
    #: `ok`, `failed`, `timeout`, `missing`, or `refused` for a config entry that is not a command.
    status: str
    exit_code: int | None = None
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    reason: str = ""

    def as_dict(self, *, run_id: str = "", slug: str = "") -> dict[str, Any]:
        """The durable record: what ran, what happened, and enough to act on it."""
        return {
            "kind": "hook", "event": self.event, "command": self.command,
            "ok": self.ok, "status": self.status, "exit_code": self.exit_code,
            "duration_ms": self.duration_ms, "reason": self.reason,
            "stdout": self.stdout, "stderr": self.stderr,
            "run_id": run_id, "slug": slug, "ts": _iso_now(),
        }

    def line(self) -> str:
        """One line for a person reading a terminal or a log tail."""
        detail = f"exit {self.exit_code}" if self.exit_code is not None else self.status
        tail = f": {self.reason}" if self.reason else ""
        return f"hook {self.event}: {detail} ({self.duration_ms} ms){tail}"


@dataclass
class NotifyOutcome:
    """One channel's attempt at one notification."""

    event: str
    #: `command` or `url`.
    channel: str
    target: str
    ok: bool
    status: str
    detail: str = ""
    status_code: int | None = None
    duration_ms: int = 0

    def as_dict(self, *, run_id: str = "", slug: str = "") -> dict[str, Any]:
        return {
            "kind": "notify", "channel": self.channel, "target": self.target, "event": self.event,
            "ok": self.ok, "status": self.status, "detail": self.detail,
            "status_code": self.status_code, "duration_ms": self.duration_ms,
            "run_id": run_id, "slug": slug, "ts": _iso_now(),
        }


@dataclass
class _Ran:
    """The raw result of running one command or one POST, before it is labelled by its caller."""

    ok: bool
    status: str
    exit_code: int | None = None
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    reason: str = ""
    status_code: int | None = None


# ── the durable record ───────────────────────────────────────────────────────


class _LifecycleLog:
    """Append-only JSONL of every hook and notification attempt.

    Best-effort by construction: a workspace whose state directory is read-only loses the record, not
    the run. Failures are remembered in `error` rather than raised, so a caller can explain a missing
    log instead of asserting one that cannot exist.
    """

    def __init__(self, path: os.PathLike | str | None) -> None:
        self.path = Path(path) if path else None
        self.error = ""
        self._fh: Any = None
        self._lock = threading.RLock()

    def write(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self._lock:
            try:
                if self._fh is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._fh = open(self.path, "a", encoding="utf-8")
                # Redacted on the way in: stdout is arbitrary text and may quote a key.
                safe = {k: (redact(v) if isinstance(v, str) else v) for k, v in record.items()}
                self._fh.write(json.dumps(safe, separators=(",", ":"), default=str) + "\n")
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError as exc:
                self.error = (
                    f"the hook log at {self.path} is not writable ({exc}); this run's hooks still ran, "
                    "but nothing about them was recorded on disk."
                )

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                except OSError:
                    pass
                finally:
                    self._fh = None


def read_hook_log(path: os.PathLike | str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read `hooks.jsonl` back, tolerating a torn final line and any unparsable record."""
    target = Path(path)
    if not target.is_file():
        return []
    out: list[dict[str, Any]] = []
    with open(target, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                out.append(data)
    return out[-limit:] if limit else out


# ── event matching ───────────────────────────────────────────────────────────


def matches(pattern: str, event: str) -> bool:
    """Whether a configured pattern covers an event name.

    Exact names and `*` are the documented surface. `goal.*` is supported too, via a documented
    fallback: the obvious prefix-reading of a `*` pattern. Without it `{"goal.*": ...}` looks wired
    and silently fires nothing, which is indistinguishable from a hook that has not happened yet —
    the config-that-does-nothing failure this codebase refuses everywhere else.
    """
    if pattern == "*" or pattern == event:
        return True
    if pattern.endswith("*"):
        return event.startswith(pattern[:-1])
    return False


def _parse_events(raw: Any) -> tuple[list[tuple[str, str]], list[str]]:
    """Flatten `events` into (pattern, command) pairs, and explain what could not be read.

    An entry that is not a command is *reported*, not dropped: the alternative is a config that looks
    wired and fires nothing, which is indistinguishable from a hook that works until the night it
    matters.
    """
    entries: list[tuple[str, str]] = []
    problems: list[str] = []
    if raw is None:
        return entries, problems
    if not isinstance(raw, Mapping):
        return entries, [f"hooks.events must be an object mapping an event name to a command or a "
                         f"list of commands; got {type(raw).__name__}"]
    for name, value in raw.items():
        pattern = str(name)
        commands = value if isinstance(value, list) else [value]
        for command in commands:
            if isinstance(command, str) and command.strip():
                entries.append((pattern, command))
            else:
                problems.append(
                    f"hooks.events[{pattern!r}] is {command!r}, which is not a shell command; expected "
                    "a string or a list of strings, e.g. {\"run.end\": \"say done\"}"
                )
    return entries, problems


# ── the shared execution contract ────────────────────────────────────────────


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ms_since(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _event_parts(event: Any) -> tuple[str, str, str, str]:
    """Split what the bus handed us into ``(name, whole event JSON, payload JSON, slug)``.

    Accepts an `Event`, a wire dict, or a bare name, so a caller writing a test — or a future
    subscriber that only has the payload — does not have to build an `Event` to fire a hook.
    """
    slug = ""
    if isinstance(event, Mapping):
        document = dict(event)
        name = str(document.get("type") or "")
    elif isinstance(event, str):
        name = event
        document = {"type": name, "payload": {}}
    else:
        name = str(getattr(event, "type_value", "") or "")
        to_dict = getattr(event, "to_dict", None)
        document = to_dict() if callable(to_dict) else {"type": name, "payload": {}}
    if not name:
        name = str(document.get("type") or "unknown")
    payload = document.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    for key in ("slug", "project"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate:
            slug = candidate
            break
    return name, json.dumps(document, default=str), json.dumps(dict(payload), default=str), slug


def _child_env(event: str, run_id: str, slug: str, payload_json: str) -> dict[str, str]:
    """The environment a child runs in: everything we have, plus what it needs to know.

    Inherited rather than replaced, because a hook is a command a person would otherwise type
    themselves and it needs `PATH`, `HOME` and the rest to behave like one.
    """
    env = dict(os.environ)
    env[ENV_EVENT] = event
    env[ENV_RUN_ID] = run_id
    env[ENV_SLUG] = slug
    if len(payload_json) > MAX_ENV_PAYLOAD:
        # A preview with its size on it, rather than a truncated document: cutting JSON in half hands
        # the child something that cannot be parsed, and parsing half a payload is worse than being
        # told the environment copy is partial. Verified against a 65 KB payload: the child sees
        # `truncated: true` and the byte count, while stdin still gets the whole event.
        env[ENV_PAYLOAD_TRUNCATED] = "1"
        env[ENV_PAYLOAD] = json.dumps({
            "truncated": True, "event": event, "bytes": len(payload_json),
            "preview": payload_json[:4000]})
    else:
        env[ENV_PAYLOAD] = payload_json
    return env


def _read_capped(handle: Any) -> tuple[str, int]:
    """Read a temp file from the start, capped, and report how much was written in total."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(0)
    text = handle.read(MAX_OUTPUT_CHARS)
    if size > MAX_OUTPUT_CHARS:
        text += f"\n… [{size - MAX_OUTPUT_CHARS} more characters not shown]"
    return text, size


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child *and its descendants*.

    A shell command starts a shell, so killing the direct child leaves the real command running —
    a hook that ignores its timeout and keeps working against the repository it was told about. Its
    own process group (from `start_new_session`) is what makes one signal sufficient.
    """
    try:
        # `getpgid` can report a *recycled* pid's group if the child has already been reaped, which
        # would kill an unrelated process. `Popen.poll()` first makes the already-exited case a no-op.
        if proc.poll() is not None:
            return
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _run_command(command: str, *, stdin_text: str, env: dict[str, str],
                 timeout_s: int) -> _Ran:
    """Run one shell command under the hook contract, bounded, and never raising.

    Output goes to temporary files rather than pipes: `communicate()` on a pipe reads a runaway
    child's output into this process's memory, and a pipe nobody drains deadlocks the child. A file
    bounds both, and the cap is applied when it is read.
    """
    started = time.monotonic()
    out_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    err_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        try:
            proc = subprocess.Popen(
                command, shell=True, stdin=subprocess.PIPE, stdout=out_file, stderr=err_file,
                text=True, env=env,
                # Its own session, so the timeout takes the whole tree and nothing else.
                start_new_session=True,
            )
        except OSError as exc:
            return _Ran(False, "missing", None, _ms_since(started), "", "",
                        f"the command could not be started ({exc}); check the shell and the binary")
        timed_out = False
        try:
            proc.communicate(input=stdin_text, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if proc.stdin is not None:
            # On the timeout path `communicate` never got to close it; leaving it open leaks a pipe.
            try:
                proc.stdin.close()
            except OSError:
                pass
        stdout, _ = _read_capped(out_file)
        stderr, _ = _read_capped(err_file)
        duration = _ms_since(started)
        if timed_out:
            return _Ran(False, "timeout", None, duration, stdout, stderr,
                        f"killed after hooks.timeout_s={timeout_s}s; the command and everything it "
                        "started were killed with it. Raise the timeout, or make the command return.")
        code = proc.returncode
        if code == 0:
            return _Ran(True, "ok", 0, duration, stdout, stderr)
        if code == 127:
            # The shell's own "not found", which is worth naming: "exit 127" reads like a bug in the
            # hook, while "the binary does not exist" is a thing a person can fix in one edit.
            return _Ran(False, "missing", code, duration, stdout, stderr,
                        "the shell could not find the command (exit 127): the binary in hooks.events "
                        "does not exist or is not on PATH")
        return _Ran(False, "failed", code, duration, stdout, stderr,
                    f"exit {code}" + (f": {stderr.strip().splitlines()[-1]}" if stderr.strip() else ""))
    except Exception as exc:  # noqa: BLE001 - a hook must never break the run it observes
        return _Ran(False, "failed", None, _ms_since(started), "", "",
                    f"the hook could not be run at all: {type(exc).__name__}: {exc}")
    finally:
        out_file.close()
        err_file.close()


def _post_json(url: str, body: str, timeout_s: int) -> _Ran:
    """POST the event JSON to a URL, bounded, and never raising."""
    started = time.monotonic()
    request = urllib.request.Request(
        url, data=body.encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "AgentOrg"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return _Ran(True, "ok", None, _ms_since(started), status_code=getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        return _Ran(False, "failed", None, _ms_since(started), status_code=exc.code,
                    reason=f"the endpoint answered HTTP {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        timed_out = isinstance(getattr(exc, "reason", None), TimeoutError)
        return _Ran(False, "timeout" if timed_out else "failed", None, _ms_since(started),
                    reason=(f"no answer within notify.timeout_s={timeout_s}s" if timed_out
                            else f"the endpoint could not be reached: {exc.reason}"))
    except (TimeoutError, OSError) as exc:
        return _Ran(False, "timeout" if isinstance(exc, TimeoutError) else "failed", None,
                    _ms_since(started), reason=str(exc) or type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - the same rule as a hook: never break the run
        return _Ran(False, "failed", None, _ms_since(started),
                    reason=f"the notification could not be sent: {type(exc).__name__}: {exc}")


def _disabled() -> bool:
    """Whether the operator turned the whole lifecycle off for this process."""
    return os.environ.get(DISABLED_ENV, "").strip().lower() not in ("", "0", "false", "no", "off")


def _section(config: Any, hooks: Any, attribute: str, owner: str) -> Any:
    """Return the config section to build from, or refuse with something a caller can act on.

    `Notifier(config)` where `config.notify` is absent is refused rather than defaulted, and the
    refusal names the attribute: a notifier built from the built-in defaults would advertise a
    channel it cannot deliver, which is the silent no-op this module exists to remove.
    """
    section = hooks if hooks is not None else getattr(config, attribute, None)
    if section is None:
        raise HooksError(
            f"{owner} needs a Config (its `config.{attribute}` is read) or the section itself; got "
            f"{config!r}. There is no sensible default command, and the alternative to refusing is an "
            f"object that silently does nothing."
        )
    return section


# ── the hook runner ──────────────────────────────────────────────────────────


class HookRunner:
    """Run the commands a person attached to named events. Told what happened; changes nothing.

    Parameters
    ----------
    config:
        A loaded `Config`; `config.hooks` is read from it. Pass `hooks=` to hand the section over
        directly, which is what a test does.
    run_id / slug:
        Stamped into every child's environment. The slug is taken from the event's own payload when
        it carries one, so a hook on `run.end` names the project even though a bus only knows a run
        id.
    state_dir:
        Where `hooks.jsonl` is appended. `None` keeps the attempts in memory only, which is what a
        bus with no trace path gets.
    """

    def __init__(self, config: Any = None, *, hooks: Any = None, run_id: str = "",
                 slug: str = "", state_dir: os.PathLike | str | None = None) -> None:
        section = _section(config, hooks, "hooks", "HookRunner")
        self.run_id = run_id
        self.slug = slug
        self.timeout_s = max(1, int(getattr(section, "timeout_s", 30) or 30))
        self.enabled = bool(getattr(section, "enabled", True)) and not _disabled()
        self._entries, self._problems = _parse_events(getattr(section, "events", None))
        self._outcomes: deque[HookOutcome] = deque(maxlen=OUTCOME_TAIL)
        self._log = _LifecycleLog(Path(state_dir) / HOOK_LOG_NAME if state_dir else None)
        if self.enabled:
            for problem in self._problems:
                self._log.write({"kind": "hook", "status": "refused", "reason": problem,
                                 "event": "", "command": "", "ok": False, "run_id": run_id,
                                 "slug": slug, "ts": _iso_now()})

    # ── inspection ──────────────────────────────────────────────────────────

    def problems(self) -> list[str]:
        """Config entries that are not commands, each with what was expected instead.

        Exposed rather than swallowed: a `hooks.events` typo that runs nothing is indistinguishable
        from a hook that has not fired yet, and the difference matters at 3am.
        """
        return list(self._problems)

    def outcomes(self) -> list[HookOutcome]:
        """The most recent attempts, oldest first. The durable record is `hooks.jsonl`."""
        return list(self._outcomes)

    def log_path(self) -> Path | None:
        return self._log.path

    def commands_for(self, event: str) -> list[str]:
        """Every command that would run for an event, in config order."""
        if not self.enabled:
            return []
        return [command for pattern, command in self._entries if matches(pattern, event)]

    def configured(self) -> bool:
        """Whether this runner has anything to do — enabled, with at least one command."""
        return self.enabled and bool(self._entries)

    def close(self) -> None:
        """Release the log handle. Idempotent."""
        self._log.close()

    # ── firing ──────────────────────────────────────────────────────────────

    def dispatch(self, event: Any) -> list[HookOutcome]:
        """Run the hooks for one event. The form `EventBus.subscribe` expects. Never raises."""
        return self.run(event=event)

    def run(self, event_name: str = "", *, event: Any = None,
            slug: str = "") -> list[HookOutcome]:
        """Run every command matching an event. Returns one outcome per command.

        Sequential and inline: see the module docstring for why this is bounded rather than
        asynchronous. Each command is capped at `timeout_s`, so the worst case for one event is
        `timeout_s × matched commands` — a person who attaches a hook to `node.enter` is choosing
        that bound, and the timeout is what makes the choice survivable.
        """
        document = json.dumps({"type": event_name})
        payload_json = "{}"
        if event is not None:
            # The event is the authority on its own name, so a subscriber cannot fire hooks under a
            # label it guessed, and the whole document — not just the payload — is what stdin gets.
            event_name, document, payload_json, slug = _event_parts(event)
        commands = self.commands_for(event_name)
        if not commands:
            return []
        resolved_slug = slug or self.slug
        env = _child_env(event_name, self.run_id, resolved_slug, payload_json)
        if resolved_slug != self.slug:
            # The slug the child reported, which is what a hook that logs the project should see
            # rather than the constructor's fallback. Not in the environment a second time; this is
            # the copy the outcome carries into `hooks.jsonl`.
            self.slug = resolved_slug
        stdin_text = document + "\n"
        outcomes: list[HookOutcome] = []
        for command in commands:
            ran = _run_command(command, stdin_text=stdin_text, env=env, timeout_s=self.timeout_s)
            outcome = HookOutcome(
                event=event_name, command=command, ok=ran.ok, status=ran.status,
                exit_code=ran.exit_code, duration_ms=ran.duration_ms,
                stdout=ran.stdout, stderr=ran.stderr, reason=ran.reason)
            outcomes.append(outcome)
            self._record(outcome)
        self._outcomes.extend(outcomes)
        return outcomes

    def _record(self, outcome: HookOutcome) -> None:
        self._log.write(outcome.as_dict(run_id=self.run_id, slug=self.slug))


# ── the notifier ─────────────────────────────────────────────────────────────


class Notifier:
    """Tell a person when an unattended run needs them.

    `config.notify.on` is the whole policy: a notification fires for those events and no others.
    Deliberately not every event — a channel that pings on every node entry is one you mute, and a
    muted channel is worse than none because it looks like it is working.

    Both channels are optional and independent: `command` is a shell command under exactly the hook
    contract (event JSON on stdin, the environment variables set, bounded by `timeout_s`), and `url`
    POSTs the event JSON. A channel that cannot be delivered is recorded and swallowed.

    The blocking trade is the same as `HookRunner`'s, for the same reason, and the bound is
    `notify.timeout_s` per channel. It is the smaller cost of the two, because the default `on` list
    is short by design.
    """

    def __init__(self, config: Any = None, *, notify: Any = None, run_id: str = "",
                 slug: str = "", state_dir: os.PathLike | str | None = None) -> None:
        section = _section(config, notify, "notify", "Notifier")
        self.run_id = run_id
        self.slug = slug
        self.timeout_s = max(1, int(getattr(section, "timeout_s", 15) or 15))
        self.enabled = bool(getattr(section, "enabled", True)) and not _disabled()
        raw_on = getattr(section, "on", None)
        self.on: list[str] = [str(name) for name in raw_on] if isinstance(raw_on, list) else []
        self.command = str(getattr(section, "command", "") or "")
        self.url = str(getattr(section, "url", "") or "")
        self._outcomes: deque[NotifyOutcome] = deque(maxlen=OUTCOME_TAIL)
        self._log = _LifecycleLog(Path(state_dir) / HOOK_LOG_NAME if state_dir else None)

    # ── inspection ──────────────────────────────────────────────────────────

    def channels(self) -> list[str]:
        """Which channels are configured, so a caller can say "none" instead of failing quietly."""
        names = []
        if self.command.strip():
            names.append("command")
        if self.url.strip():
            names.append("url")
        return names if self.enabled else []

    def wanted(self, event: str) -> bool:
        """Whether this event is one the person asked to be interrupted for."""
        if not self.enabled:
            return False
        return any(matches(pattern, event) for pattern in self.on)

    def configured(self) -> bool:
        """Whether this notifier has an event to fire on *and* somewhere to send it.

        Both halves, because either one alone is the silent no-op this module refuses: an `on` list
        with no channel reads as "notifications are set up", and a channel with an empty `on` list
        never fires.
        """
        return self.enabled and bool(self.on) and bool(self.channels())

    def outcomes(self) -> list[NotifyOutcome]:
        return list(self._outcomes)

    def log_path(self) -> Path | None:
        return self._log.path

    def close(self) -> None:
        self._log.close()

    # ── firing ──────────────────────────────────────────────────────────────

    def dispatch(self, event: Any) -> list[NotifyOutcome]:
        """Notify, when the event is one that was asked for. Never raises."""
        name, document, payload_json, slug = _event_parts(event)
        if not self.wanted(name):
            return []
        env = _child_env(name, self.run_id, slug or self.slug, payload_json)
        outcomes: list[NotifyOutcome] = []
        if self.command.strip():
            ran = _run_command(self.command, stdin_text=document + "\n", env=env,
                               timeout_s=self.timeout_s)
            outcomes.append(NotifyOutcome(
                event=name, channel="command", target=self.command, ok=ran.ok, status=ran.status,
                detail=ran.reason, status_code=ran.exit_code, duration_ms=ran.duration_ms))
        if self.url.strip():
            ran = _post_json(self.url, document, self.timeout_s)
            outcomes.append(NotifyOutcome(
                event=name, channel="url", target=self.url, ok=ran.ok, status=ran.status,
                detail=ran.reason, status_code=ran.status_code, duration_ms=ran.duration_ms))
        for outcome in outcomes:
            self._log.write(outcome.as_dict(run_id=self.run_id, slug=self.slug))
        self._outcomes.extend(outcomes)
        return outcomes


# ── the one integration point ────────────────────────────────────────────────


class Lifecycle:
    """Both consumers behind one subscriber, so a bus is wired once.

    There is exactly one place in the engine where every event passes — `EventBus.emit` — so wiring
    here covers every event type without a dozen call sites knowing hooks exist. A bus that has no
    trace path is also a bus with no workspace, which is why `state_dir=None` is normal rather than a
    degraded case: the hooks still run, only their record is not persisted.

    **Either half may be absent.** A config with `hooks.events` and no `notify` channel gets a runner
    and no notifier, and vice versa; `attach` decides that and reports it through `configured()`.
    """

    def __init__(self, runner: "HookRunner | None", notifier: "Notifier | None") -> None:
        self.runner = runner
        self.notifier = notifier

    def __call__(self, event: Any) -> None:
        try:
            self.runner.dispatch(event)
        except Exception:  # noqa: BLE001 - an observation must not break the run it observes
            pass
        try:
            self.notifier.dispatch(event)
        except Exception:  # noqa: BLE001 - the same rule, and the reason both are wrapped
            pass

    @classmethod
    def attach(cls, bus: Any, config: Any = None, *, run_id: str = "", slug: str = "",
               state_dir: os.PathLike | str | None = None,
               diagnostics: Any = None) -> "Lifecycle | None":
        """Wire hooks and notifications onto a bus, or return None when there is nothing to do.

        Returns None rather than raising, and returns None when neither side is actually configured:
        a subscriber that does nothing is one more thing between an event and its handler, and that
        is the case for every existing run — the default `hooks.events` is empty and `notify` has no
        channel.

        **Each section is built and offered on its own.** A broken `hooks` block must not take the
        notifier down with it: the notification is the more valuable half for a run nobody is
        watching, and refusing it because a *hook* was malformed is precisely how an unattended run
        stops without telling anyone.

        A failure while attaching is logged to `diagnostics` when one was given and swallowed
        otherwise — a run must not fail to start because an optional observer could not be built.
        """
        runner = _build_or_log("hooks", lambda: HookRunner(
            config, run_id=run_id, slug=slug, state_dir=state_dir), diagnostics)
        notifier = _build_or_log("notify", lambda: Notifier(
            config, run_id=run_id, slug=slug, state_dir=state_dir), diagnostics)
        if runner is None and notifier is None:
            return None
        lifecycle = cls(runner, notifier)
        subscribe = getattr(bus, "subscribe", None)
        if callable(subscribe):
            subscribe(lifecycle)
        return lifecycle

    # ── firing ──────────────────────────────────────────────────────────────

    def dispatch(self, event: Any) -> None:
        """Offer an event to both observers. The form every consumer in this module shares."""
        self(event)

    def __call__(self, event: Any) -> None:
        """Offer an event to both halves. Raises nothing, deliberately.

        The bus already disables a raising subscriber — but a disabled subscriber is a *silent* one,
        and a run with no hooks and no notification is the failure this module exists to remove. So
        the failure has to stay inside here rather than reach the bus's error counter.
        """
        try:
            if self.runner is not None:
                self.runner.dispatch(event)
        except Exception:  # noqa: BLE001 - an observation must not break the run it observes
            pass
        try:
            if self.notifier is not None:
                self.notifier.dispatch(event)
        except Exception:  # noqa: BLE001 - the same rule, and the reason both are wrapped
            pass

    def close(self) -> None:
        """Release both logs. Idempotent, and safe when a side was never built."""
        for observer in (self.runner, self.notifier):
            if observer is None:
                continue
            try:
                observer.close()
            except Exception:  # noqa: BLE001 - a teardown must not raise over bookkeeping
                pass

    def log_path(self) -> Path | None:
        """Where attempts are recorded, when either side has a log."""
        return (self.runner.log_path() if self.runner else None) or (
            self.notifier.log_path() if self.notifier else None)

    def configured(self) -> list[str]:
        """Which halves are actually watching, so a caller can report "none" out loud."""
        names = []
        if self.runner is not None and self.runner.configured():
            names.append("hooks")
        if self.notifier is not None and self.notifier.configured():
            names.append("notify")
        return names


def _build_or_log(side: str, build: Callable[[], Any], diagnostics: Any) -> Any:
    """Build one observer, or return None with the reason reported rather than raised.

    Catches everything, not just `HooksError`: the promise this backs is that a run cannot fail to
    *start* because an optional observer was malformed, and a section built by hand — or by a future
    config shape — can fail in ways this module does not enumerate.
    """
    try:
        observer = build()
    except Exception as exc:  # noqa: BLE001 - an optional observer must never block a run
        if diagnostics is not None:
            try:
                diagnostics.warning(f"{side}.attach.failed",
                                    message=f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 - diagnostics is best-effort too
                pass
        return None
    return observer if observer.configured() else None
