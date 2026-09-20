#!/usr/bin/env python3
"""sandbox.py — a *confined* shell, which is the only kind this engine will run.

WHY THIS EXISTS
---------------
`engine/tools.py` shipped without a command tool on purpose: an unconstrained shell inside a real
repository is a much larger risk than a file write. But "never" was never quite right either. An
unattended agent that cannot run the test suite it just wrote is limited to *guessing* whether the
code works — and an agent that guesses about its own output is exactly the confident-wrong-output
failure the rest of this engine is built to prevent. So the refusal in `tools.py` named its own
remedy: this deserved *its own decision*, not a side door.

This module is that decision, and the decision is **confinement**. A command runs inside a Seatbelt
profile that grants the minimum a toolchain needs, and everything the profile does not name is
refused by the kernel rather than by this code. The distinction matters: a Python-level check of an
argv string is a suggestion the child can ignore — the child is a *different program*, and it will
spawn its own children, open its own files, and connect its own sockets. A policy enforced at the
syscall boundary is the only kind that survives that.

Three properties this module exists to keep:

1. **The confinement is real, and it is the default.** DENY is the first rule. Read reaches the
   workspace and the system paths a toolchain needs; write reaches the workspace and nothing else;
   network is off unless the operator turned it on. This is not a denylist of dangerous commands —
   `rm -rf /` is simply a command that fails, and so is a Python one-liner that does the same thing.
2. **A refusal never leaves you unconfined.** If no confinement backend exists on this platform, the
   runner returns a refusal. It does not fall back to `subprocess` without a profile. A sandbox that
   silently degrades when its backend is absent is *worse than no sandbox*, because the caller
   believes a guarantee that is not being kept.
3. **argv, never a shell string.** Accepting `"make test && git push"` would move the whole decision
   back to string parsing. A command is a list; composition is the caller's problem, not a policy a
   regex can be talked around.

DESIGN
------
- **The profile is a plain allowlist**, built once per run and passed with `-p`. It carries no state
  and no interpolation of the command, so what confines `ls` also confines anything the command
  spawns.
- **`TMPDIR` is pinned to the one temp directory the profile grants.** `tempfile` consults `$TMPDIR`
  first, and every compiler, test runner and toolchain leans on it, so the child has to be told which
  path it may actually write. The operator's own private temp directory is used rather than one inside
  the workspace — see `SandboxedCommand.tmpdir` for why the tidier-looking workspace-local choice makes
  `doctor`'s secret-hygiene scan report leaks the engine never produced.
- **The engine's own state is excluded from writes**, the same exclusion `tools.py` makes for files.
  A command that can rewrite `run_state.json` or delete `effects.jsonl` can fabricate a resume and
  defeat idempotency, so the write grant is carved around it. Reads of the state directory stay
  allowed: a test run that inspects its own checkpoint is legitimate, and the state directory is
  inside the tree the command was pointed at.
- **Truncation is reported, not hidden.** A capped result says so in the text. The alternative — a
  silently clipped test log — produces a model that reasons about output it never saw.
- **A timeout is a kill, and it says which.** A killed command is not a failing command; the result
  distinguishes "exited 1" from "was stopped at the ceiling", because the model's next move differs.

KNOWN GAP — NESTED CONFINEMENT IS REFUSED BY THE KERNEL
-------------------------------------------------------
`(deny default)` does not include `sandbox*`, and there is no SBPL operation of that name to add, so a
confined child cannot itself call `sandbox-exec`: the kernel answers `sandbox_apply: Operation not
permitted` and the grandchild exits 71. That matters because `SandboxedCommand(...).run(run_tests)`
*is* a nested case — the outer profile applies to the test process's children, and a test that wants
its own sandbox cannot have one.

The practical consequence, measured rather than assumed: a command whose only job is to run a
*self-contained* tool (a compiler, a linter, `run_tests.py` over tests that spawn nothing) works
inside the profile. A command that runs the *engine's own* suite does not, because parts of that suite
spawn processes and the sandbox makes them fail rather than simply denying them. So `run_command` is
useful for the job it was built for — let the agent run the tests it just wrote — and is not a way to
re-run this repository's suite from inside a run. Closing it would mean either running a command
*without* the outer profile (the thing this module refuses to do) or accepting recursive Seatbelt use,
which the platform does not offer. The preflight and the profile keep their guarantees; this is a
bound on what they can be pointed at, and it is stated here rather than discovered as a mystery
failure.

Usage:
    runner = SandboxedCommand(workspace_root=Path("~/code/app"), config=cfg.sandbox)
    result = runner.run([sys.executable, "run_tests.py"])
    result.exit_code, result.stdout, result.truncated
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .state import ENGINE_STATE_DIRNAME

__all__ = [
    "SandboxError", "CommandResult", "SandboxProfile", "SandboxedCommand",
    "SANDBOX_EXEC", "detect_backend", "backend_available",
]

#: Where Seatbelt lives. Checked rather than assumed: `sandbox-exec` is deprecated, is absent on
#: Linux and Windows entirely, and is not on every macOS install — and a path we did not verify is
#: how a runner ends up unconfined while claiming otherwise.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: System paths granted for reading. These are the paths the *toolchain* needs, not the paths an
#: attacker would want: they hold the interpreter, the dylibs, the compiler and the SDK, and none of
#: them carries per-user data. `/System` and `/usr` are on the read-only system volume.
SYSTEM_READ_PATHS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library/Frameworks",
    "/Library/Developer",
    "/private/var/folders",
    "/private/etc",
    "/dev",
)

#: The environment keys `tempfile` consults for the temp directory, in its own order. The runner pins
#: all three to the one path the profile grants, so a toolchain picking any of them lands somewhere it
#: is actually allowed to write.
TMPDIR_ENV_KEYS: tuple[str, ...] = ("TMPDIR", "TEMP", "TMP")

#: Seatbelt operation classes that are named explicitly rather than folded into a blanket `allow`.
#: Each is here because a *toolchain* needs it and a hostile command gains little from it:
#: `sysctl-read` is what `platform.machine()` and `os.cpu_count()` read (without it, `import ssl`
#: and every `sysctl`-probing tool fails), `signal` lets a child reap what it spawned, and
#: `ipc-posix*` is what `multiprocessing` uses for its semaphores.
BASE_OPERATIONS: tuple[str, ...] = (
    "process*",
    "sysctl-read",
    "signal",
    "ipc-posix*",
)

#: The child's `HOME`, left alone rather than moved into the workspace.
#:
#: An earlier version pointed `HOME` at a directory inside the workspace, reasoning that a tool
#: resolving `~` should not reach the operator's home. It turned out to be the worst kind of fix:
#: `Path.home()` consults `$HOME` *before* the password database, so the engine's own
#: `library.default_search_paths()` — which looks for the Skills checkout under `~/Documents` — began
#: resolving `~` to the workspace. Nothing errored. The library simply "was not found", which is a
#: misleading failure produced by the sandbox itself and would have been blamed on a missing checkout.
#:
#: Leaving `HOME` alone is also the more honest posture: the profile does not grant the real home
#: directory for reading or writing, so a tool that reaches for it fails visibly at the syscall
#: boundary — a true report about the confinement — rather than being quietly relocated.
CHILD_HOME_RELPATH: str | None = None


class SandboxError(RuntimeError):
    """A sandbox that could not be *configured*: an impossible grant, or a shell string.

    Deliberately narrower than it looks. Anything the *command* does — a non-zero exit, a write the
    profile refuses, a timeout — is a `CommandResult`, not an exception; this is only for a request
    that cannot be turned into a profile at all, and `SandboxedCommand.run` catches it and returns it
    as a refusal so a tool call never dies on it.
    """


@dataclass(frozen=True)
class CommandResult:
    """What a confined command produced.

    `exit_code` is `None` when the command never ran to an exit — it timed out or could not be
    started. `None` is deliberately not `1`: "was killed at the ceiling" and "failed" call for
    different next moves, and collapsing them would hide the ceiling from the model. `timed_out`
    and `refused` carry the specific reason.
    """

    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    #: True when either stream hit `max_output_bytes` and the text is incomplete. The model must know,
    #: or it will reason about a test log as though it had seen all of it.
    truncated: bool = False
    #: True when the wall-clock ceiling stopped the command rather than the command finishing.
    timed_out: bool = False
    #: True when a preflight refused to run at all — no backend, or no command. Nothing was executed.
    refused: bool = False
    #: Why, in words, when `refused` or `timed_out`. Always set on a non-success.
    reason: str = ""
    #: The profile the command ran under, for the trace. Kept on the result so a later reader can see
    #: *what* the confinement actually was rather than trusting that there was one.
    profile: str = ""
    #: Absolute paths of the directories this run used for the child's scratch space, so a caller can
    #: see where a command's temp files went.
    scratch: list[str] = field(default_factory=list)

    @property
    def combined(self) -> str:
        """stdout and stderr as one block, the way a terminal would have shown them."""
        parts = [self.stdout]
        if self.stderr:
            parts.append(self.stderr if not self.stdout else f"\n[stderr]\n{self.stderr}")
        return "".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "exit_code": self.exit_code, "truncated": self.truncated,
            "timed_out": self.timed_out, "refused": self.refused, "reason": self.reason,
            "stdout_bytes": len(self.stdout.encode("utf-8")),
            "stderr_bytes": len(self.stderr.encode("utf-8")),
        }


def detect_backend(platform_name: str | None = None) -> str | None:
    """Which confinement backend is usable here, or None when there is none.

    Returns the backend's name rather than a boolean so a refusal can say what it looked for — an
    operator on Linux needs to know that "no sandbox" is a fact about the platform, not a
    misconfiguration of this engine.
    """
    name = platform_name if platform_name is not None else sys.platform
    if name == "darwin" and os.path.isfile(SANDBOX_EXEC) and os.access(SANDBOX_EXEC, os.X_OK):
        return "seatbelt"
    return None


def backend_available(platform_name: str | None = None) -> bool:
    """Whether a confined command can run at all. See :func:`detect_backend`."""
    return detect_backend(platform_name) is not None


def _sbpl_subpath(text: str) -> str:
    """One path as an SBPL `(subpath …)` filter.

    The filter is not decoration. A bare string in a rule is a *literal* match on that exact path:
    `(allow file-read* "/usr")` grants `/usr` itself and nothing inside it, so `python3` cannot read
    its own stdlib and the command dies with a parser error from a profile that looked right. A
    `subpath` filter grants the path and everything under it, which is what "grant the toolchain
    directories" actually means.

    Seatbelt's language has no escape for a double quote inside a string literal, which makes a path
    containing one impossible to express rather than merely awkward to write. Refusing is the right
    answer: the alternative is emitting a profile that does not parse, and a profile that does not
    parse is a command that does not run at all — or, far worse, one that runs under a profile whose
    meaning nobody checked.
    """
    if '"' in text or "\n" in text or "\x00" in text:
        raise SandboxError(
            f"cannot express the path {text!r} in a Seatbelt profile: it contains a quote or a "
            "control character, which SBPL string literals cannot escape. Move the project or the "
            "entry out of that path.")
    return f'(subpath "{text}")'


@dataclass(frozen=True)
class SandboxProfile:
    """One Seatbelt profile, and the environment a command runs under with it.

    Built from a workspace root and a `SandboxConfig`. Held as data rather than a string because the
    profile is also the evidence: a caller can inspect `read_paths` / `write_paths` to see what was
    granted instead of re-parsing SBPL.
    """

    root: Path
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    allow_network: bool
    #: The command's `TMPDIR`, granted explicitly in addition to whatever covers the workspace.
    #:
    #: Granted as its own rule because `allow_write` may point outside the workspace while the temp
    #: directory must stay writable regardless. The `.agent_state` write carve-out is deliberately not
    #: applied here: it names a directory inside the workspace, and this is not one.
    tmpdir: Path

    #: Ancestors read for *metadata* only — existence, size, timestamps — never for their contents.
    #:
    #: This is what turns the read grant into something a toolchain can navigate with. `find`, `git`
    #: and every recursive walk starts by stating every ancestor of the tree it is about to enter, and a
    #: policy that denies those stats makes a tool fail on a directory it was *allowed* to read. The
    #: sharpest case is `~`: the child's `HOME` is inside the workspace, and without a metadata grant on
    #: `/` and `/Users` a `~`-expansion quietly resolves to the wrong path rather than erroring.
    #:
    #: Metadata is the one leak this file is least comfortable with and it is worth naming exactly: with
    #: it a command can learn that a path outside the workspace *exists*. It cannot open it, list it or
    #: read a byte of it, and the tests assert the home directory's contents stay unreachable.
    METADATA_GRANT_PREFIXES: tuple[str, ...] = ("/", "/Users", "/private", "/var", "/etc", "/tmp",
                                                "/opt", "/Applications")

    def text(self) -> str:
        """The SBPL source.

        Four things here are load-bearing and each was arrived at by testing, not by reading:

        1. **`(literal "/")` must be in the read grant.** With it omitted, *every* confined command dies
           with SIGABRT before `main` — `/bin/echo` included, and nothing is written to stderr, so it
           reads like a bug in the runtime rather than a policy. Something in dyld's or libsystem's
           start-up stats or opens the volume root, and the grant is what lets the process start at all.
           The alternative that also works, `(subpath "/")`, grants read over the whole filesystem and
           would make `~/.ssh/id_rsa` readable. `literal` grants that one directory entry and nothing
           beneath it, so the home directory stays unreachable while the process can still run.

        2. **The metadata grant is what keeps `HOME` honest.** `SandboxedCommand` points the child's
           `HOME` into the workspace, and a resolver like `~`-expansion has to `stat` the way there.
           Without a metadata grant on `/` and `/Users`, `Path.home()` — and therefore `library.py`'s
           search for the Skills checkout — silently resolves to *this repository* rather than to the
           real home. Nothing errors; the tool simply looks in the wrong place, which is the kind of
           failure that costs an afternoon. The grant is metadata-only on purpose: with it a command can
           learn that a path exists, and cannot open, list or read it. The tests assert the home
           directory's contents stay unreachable.

        3. **An earlier `deny` wins over a later `allow`, and a later `deny` wins over an earlier
           `allow`.** The engine-state carve-out is therefore emitted *last*, after every grant. That
           ordering is what makes it hold regardless of which order the caller's `allow_write` entries
           arrived in: a caller who names a path inside `.agent_state/` still does not get to write the
           checkpoints. This is the opposite of the "last rule wins" intuition `iptables` teaches, and
           getting it backwards is silent — the profile parses, and the deny simply stops applying.

        4. **`subpath` filters, not bare strings.** `(allow file-read* "/usr")` grants `/usr` itself and
           nothing under it, so `python3` cannot read its own stdlib and the profile *parses fine* while
           the command fails with an `illegal argument` from the parser instead. Every path below goes
           through :func:`_sbpl_subpath`.
        """
        lines = [
            "(version 1)",
            ";; Deny first. Everything below this line is a named exception, not a default.",
            "(deny default)",
        ]
        lines += [f"(allow {op})" for op in BASE_OPERATIONS]

        # Metadata first, and deliberately broader than the content grant below: a tool has to be able
        # to *see* an ancestor directory before it can reach the granted path inside it. This grants
        # `stat` and nothing else — no listing, no opening, no reading.
        metas = [_sbpl_subpath(p) for p in self.METADATA_GRANT_PREFIXES]
        lines.append(f"(allow file-read-metadata {' '.join(metas)})")

        reads = [_sbpl_subpath(str(self.root))]
        reads += [_sbpl_subpath(p) for p in self.read_paths]
        lines.append(f"(allow file-read* (literal \"/\") {' '.join(reads)})")

        writes = [_sbpl_subpath(str(self.root))]
        # The temp directory is named in its own right, not just by the workspace grant: it lives
        # outside the workspace, and `allow_write` may point outside too. If it happens to sit *inside*
        # a granted tree the extra rule is harmless, because `allow` is idempotent.
        writes.append(_sbpl_subpath(str(self.tmpdir)))
        writes += [_sbpl_subpath(p) for p in self.write_paths]
        lines.append(f"(allow file-write* {' '.join(writes)})")

        # Last, so it wins: `deny` beats any *earlier* allow over a path it covers. Placing it here
        # rather than beside the write grant above is what keeps the carve-out effective no matter
        # which order the caller's `allow_write` entries arrived in.
        state = self.root / ENGINE_STATE_DIRNAME
        if state != self.root:
            lines.append(f"(deny file-write* {_sbpl_subpath(str(state))})")

        if self.allow_network:
            # `network*` rather than `network-outbound` alone: a name lookup goes through the system's
            # DNS responder over a unix socket and Mach, so `network-outbound` alone grants the
            # connection but breaks the resolution — a failure that reads like a broken build.
            lines += ["(allow network*)", "(allow mach*)"]
        lines.append("")
        return "\n".join(lines)

    def environment(self) -> dict[str, str]:
        """The child's environment: `TMPDIR` pinned to the one path the profile grants for it.

        Pinned rather than passed through, because the profile grants *one* temp directory and the
        child must be told which: inheriting a `$TMPDIR` the profile does not carry would hand the
        child a path it is then denied, which fails as a `PermissionError` from inside a toolchain.
        `HOME` and everything else are passed through untouched — see :data:`CHILD_HOME_RELPATH` for
        why relocating `HOME` is the wrong move.
        """
        return dict.fromkeys(TMPDIR_ENV_KEYS, str(self.tmpdir))


class SandboxedCommand:
    """Run argv lists under a Seatbelt profile, bounded in time and output.

    Parameters
    ----------
    workspace_root:
        The project root. The command's `cwd` is pinned here, and this is the only tree it may write.
    config:
        The `SandboxConfig` section. Read only — this module never writes configuration.
    allow_write:
        Extra absolute paths to grant, overriding `config.allow_write` when supplied. An operator
        escape hatch that is explicit at the call site rather than a property of the engine.
    """

    def __init__(self, *, workspace_root: Path | str, config: Any = None,
                 allow_write: Iterable[str] | None = None) -> None:
        self.root = Path(workspace_root).resolve()
        self.config = config
        self.max_seconds = int(getattr(config, "max_seconds", 120) or 120)
        self.max_output_bytes = int(getattr(config, "max_output_bytes", 100_000) or 100_000)
        self.allow_network = bool(getattr(config, "allow_network", False))
        raw_extra = allow_write if allow_write is not None else (
            getattr(config, "allow_write", None) or [])
        self.extra_write_paths = self._clean_paths(raw_extra)
        self.backend = detect_backend()

    def _clean_paths(self, raw: Iterable[Any]) -> tuple[str, ...]:
        """Normalise the extra write roots, refusing one that would give away more than it says.

        `allow_write: ["/"]` is a configuration that *looks* like an entry and is really "no sandbox
        at all". Refusing it is the whole point of this module: an operator who wants that should turn
        the tool off, where the honesty is at least visible in `specs()`.
        """
        paths: list[str] = []
        for item in raw:
            text = str(item or "").strip()
            if not text:
                continue
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                raise SandboxError(
                    f"sandbox allow_write entry {text!r} is not an absolute path. A relative entry "
                    "would be resolved against something other than the workspace, which is how a "
                    "narrow-looking grant turns into a wide one.")
            resolved = candidate.resolve() if candidate.exists() else candidate
            if str(resolved).rstrip("/") in ("", "/"):
                raise SandboxError(
                    f"sandbox allow_write entry {text!r} is the filesystem root, which grants every "
                    "write on the machine. Turn sandbox.enabled off instead — that is honest about "
                    "what is happening, and it shows up in the tool list.")
            paths.append(str(resolved).rstrip("/") or str(resolved))
        return tuple(paths)

    # ── the profile ─────────────────────────────────────────────────────────

    def profile(self) -> SandboxProfile:
        """Build the profile for this workspace.

        Read paths are fixed constants plus the workspace; nothing about the *command* reaches this
        method, which is what makes the profile non-negotiable from the child's side. There is no
        argument a command can pass that widens it.
        """
        return SandboxProfile(
            root=self.root,
            read_paths=SYSTEM_READ_PATHS,
            write_paths=self.extra_write_paths,
            allow_network=self.allow_network,
            tmpdir=self.tmpdir(),
        )

    def tmpdir(self) -> Path:
        """The temp directory the command will be told to use, resolved once.

        `tempfile` consults `$TMPDIR` first, so this is exactly where a confined `tempfile.mkdtemp()`
        will land — and the profile has to grant that path, or every toolchain that stages through temp
        fails. Resolved here rather than at rule-emission time so the grant and the environment cannot
        disagree about `/var` versus `/private/var`, which on macOS are the same directory.

        The operator's own temp directory is used rather than one inside the workspace, and the reason
        is worth stating because the alternative looks tidier: a workspace-local scratch area puts the
        fixtures a test suite writes — including a deliberately leaky one — inside `.agent_state/`,
        which `config.scan_for_leaks` walks in its entirety. `doctor` then reports leaks the engine
        never produced. A sandbox that makes the engine's own integrity check lie is worse than a
        sandbox whose temp grant is the operator's already-private directory.
        """
        for key in TMPDIR_ENV_KEYS:
            value = os.environ.get(key)
            if value:
                return Path(value).resolve()
        return Path(tempfile.gettempdir()).resolve()

    def preflight(self) -> str | None:
        """Whether a command can be confined here, or the reason it cannot.

        Returns None when confinement is available. Called before every run rather than once at
        construction, because the answer is a property of the machine at the moment of the call.
        """
        if self.backend is None:
            return (
                "no confinement backend is available on this platform "
                f"(platform={sys.platform!r}, looked for {SANDBOX_EXEC}). This engine will not run a "
                "command without one: an unconfined shell is the risk the run_command tool was "
                "withheld for, and running it anyway while reporting success would be worse than "
                "refusing. On macOS install the Xcode command line tools for sandbox-exec; on Linux "
                "no Seatbelt equivalent is wired up here.")
        if not self.root.is_dir():
            return f"the workspace root {self.root} is not a directory"
        return None

    @staticmethod
    def _clean_argv(argv: Sequence[Any]) -> list[str]:
        """Coerce the command to a plain string list, refusing a shell string.

        A bare string is the case worth refusing explicitly. `run_command("make test")` *looks* like
        it works and would need a shell to work, and the moment a shell is involved the caller is
        back to parsing rather than confining: `;`, `&&`, `$(…)` and backticks all become live. There
        is one honest reading of a command line and the caller has to write it.
        """
        if isinstance(argv, (str, bytes)):
            raise SandboxError(
                f"run_command takes an argv list, not a shell string (got {argv!r}). A string would "
                "have to be handed to a shell, and a shell is exactly what the confinement cannot "
                "follow through. Write it as a list, e.g. "
                "[\"python3\", \"-m\", \"pytest\", \"-q\"].")
        parts = [str(p) for p in argv]
        if not parts:
            raise SandboxError("an empty command was given; nothing to run")
        if not parts[0].strip():
            raise SandboxError("the command's first element is blank; nothing to run")
        return parts

    # ── running ─────────────────────────────────────────────────────────────

    def run(self, argv: Sequence[Any], *, timeout_s: int | None = None,
            cwd: Path | str | None = None) -> CommandResult:
        """Run `argv` confined. Never raises for a failure of the *command*; see :class:`CommandResult`.

        The preflight runs first and returns a refusal rather than an exception, for the same reason
        `tools.py` returns refusals: a model that is told *why* it cannot run something can adapt,
        and an exception ends the node and loses the work it had already done.
        """
        try:
            parts = self._clean_argv(argv)
        except SandboxError as exc:
            return CommandResult(False, None, "", str(exc), refused=True, reason=str(exc))

        unavailable = self.preflight()
        if unavailable is not None:
            return CommandResult(False, None, "", unavailable, refused=True, reason=unavailable)

        try:
            profile = self.profile()
        except SandboxError as exc:
            return CommandResult(False, None, "", str(exc), refused=True, reason=str(exc))

        try:
            workdir = self._resolve_cwd(cwd)
        except SandboxError as exc:
            return CommandResult(False, None, "", str(exc), refused=True, reason=str(exc))

        try:
            profile.tmpdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            reason = (f"cannot prepare the sandbox temp directory {profile.tmpdir}: {exc}. A command "
                      "with no writable temp directory fails in ways that read like a bug in the "
                      "command rather than in the sandbox.")
            return CommandResult(False, None, "", reason, refused=True, reason=reason)

        environment = dict(os.environ)
        environment.update(profile.environment())

        ceiling = int(timeout_s if timeout_s is not None else self.max_seconds)
        command = [SANDBOX_EXEC, "-p", profile.text(), "--", *parts]
        try:
            process = subprocess.Popen(
                command, cwd=str(workdir), env=environment, shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # Its own process group, so a command that spawns compilers can be stopped as a unit.
                # Without `start_new_session` the child shares this process's group, and the kill on
                # timeout below would deliver the signal to the engine's own process group — the
                # sandbox would take the host down with it, which is a far worse failure than a
                # command that overruns.
                start_new_session=True,
            )
        except OSError as exc:
            return CommandResult(False, None, "", "", refused=True,
                                 reason=f"cannot start the sandboxed command: {exc}")

        timed_out = False
        try:
            raw_out, raw_err = process.communicate(timeout=ceiling)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(process)
            try:
                raw_out, raw_err = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                # The grace window expired too. Say so rather than reporting an output we do not
                # have: the child is still out there and its pipes were closed under it.
                self._kill(process)
                raw_out, raw_err = b"", b""

        out, out_cut = self._render(raw_out)
        err, err_cut = self._render(raw_err)
        notes: list[str] = []
        if timed_out:
            notes.append(
                f"[stopped at the {ceiling}s ceiling — the command had not finished. It may have "
                "been killed mid-write; raise sandbox.max_seconds if it genuinely needs longer.]")
        if out_cut or err_cut:
            notes.append(
                f"[output truncated at {self.max_output_bytes} bytes per stream; what is shown is "
                "the beginning. Narrow the command's output rather than assuming this is all of it.]")
        if notes:
            err = (err + "\n" if err else "") + "\n".join(notes)

        exit_code = None if timed_out else process.returncode
        return CommandResult(
            ok=(exit_code == 0),
            exit_code=exit_code,
            stdout=out,
            stderr=err,
            truncated=bool(out_cut or err_cut),
            timed_out=timed_out,
            reason=("the command exceeded its wall-clock ceiling" if timed_out else ""),
            profile=profile.text(),
            scratch=[str(profile.tmpdir)],
        )

    def _resolve_cwd(self, cwd: Path | str | None) -> Path:
        """The command's working directory, which never leaves the workspace.

        Pinned by default, and a caller-supplied `cwd` is still confined to the tree: a command whose
        `cwd` is outside the workspace is a command running somewhere the profile does not grant it,
        which would fail confusingly rather than being refused clearly.
        """
        if cwd is None:
            return self.root
        candidate = Path(cwd)
        resolved = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise SandboxError(
                f"the command's working directory {resolved} is outside the workspace {self.root}. "
                "A toolchain that runs here cannot read its own files, so the failure would look like "
                "a broken build rather than a mispinned directory.") from None
        return resolved

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        """Ask the child to stop, and its whole group with it.

        A `make` that is killed alone leaves its compiler children running, still holding the pipes —
        which is how a timeout turns into a hang that the ceiling was supposed to prevent. Killing the
        process group is what makes the ceiling mean something for a command that spawns.
        """
        try:
            os.killpg(os.getpgid(process.pid), 15)
        except (OSError, AttributeError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                pass

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(process.pid), 9)
        except (OSError, AttributeError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass

    def _render(self, raw: bytes) -> tuple[str, bool]:
        """Decode one captured stream, cutting it at the cap and saying whether it cut.

        Cut on a byte boundary rather than a line boundary on purpose: a command that emits one very
        long line (a minified bundle, a base64 blob) would otherwise pass the cap intact, and the cap
        is the bound that keeps a runaway command from filling memory before it is ever truncated.
        """
        cut = len(raw) > self.max_output_bytes
        return raw[:self.max_output_bytes].decode("utf-8", errors="replace"), cut
