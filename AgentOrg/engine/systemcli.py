#!/usr/bin/env python3
"""systemcli.py — the machine's capabilities, driven from a terminal by the person who owns it.

WHY THIS EXISTS
---------------
The engine learned to act on the Mac one scoped grant at a time (`sysctl_tools`), and a console can
describe what those grants mean (`syscap`). A person sitting at a terminal could do neither: there was
no command to take a screenshot, no command to set the volume, and no command that would even say what
the machine tooling would permit. The ask was explicit — *a person can use every system capability the
agents can* — and the only correct way to answer it is to drive the **same** `SystemTools` the agents
drive, through the **same** gate, so there is exactly one implementation of "set the volume".

DESIGN
------
- **One implementation, two callers.** Every command here goes through `ToolRegistry.call`, which is
  the one entry point an agent's tool call passes through. Not the `SystemTools` methods directly: the
  registry is where the capability gate lives (`SYSTEM_TOOL_CAPABILITY` + `_granted_scoped`), so
  routing around it would give the CLI a permission model of its own. A second "set the volume" is the
  bug this design exists to prevent, and a second *gate* is the same bug one layer up.
- **The CLI adds what a model's refusal cannot: a next move.** The tool layer's refusals are written
  for a model — "do not retry this call, record an open question instead". A person needs the
  opposite: which switch to flip, and the exact command that flips it. So every command asks the two
  gates *first*, through public API only (`syscap.granted_in`, `CONSENT_REQUIRED`, `consent_gate`),
  purely to choose its wording and its exit code — and then makes the real call, which enforces the
  same gates again. The pre-check decides nothing; it explains.
- **Exit codes separate a refusal from a failure.** 0 ran and worked; 1 ran and failed; 2 a usage
  error; and **3 the engine would not allow it** — a capability the holder does not have, an approval
  nobody gave, an allowlist. A script that retries a failure must not retry a refusal, so the two
  cannot share a code. A command given *nothing to do* is a usage error as well, even when it answers
  the question it was asked: `system enable` with no switch prints the state, writes nothing and exits
  2, so a script cannot mistake a question for a change. `EXIT_USAGE` is the honest code there
  because the invocation was incomplete — the same reason `system power` and `system notify` refuse
  with it — and where `enabled` is already on, exiting 0 would make "asked a question" and "flipped a
  switch" indistinguishable to the only reader that matters.
- **Every report ends with a next move, in both output modes.** `next` is one line naming the command
  that applies to the state *just reported*, never a fixed suggestion: on a full-access machine the
  consent gate has stepped aside, so pointing at it would send a person to a switch that no longer
  decides anything. The field carries the whole line, prose included, so `--json` callers — the app
  and `serve` — can show the same sentence the terminal shows rather than inventing their own; a
  caller that wants only the command takes the text before `NEXT_SEP`.
- **Everything a person reads about the mode branches on the mode.** An empty allowlist means "none
  allowed" in the scoped mode and "no list is kept" under `allow_full_access` (`sysctl_tools` skips
  the check entirely in the latter), so a phrase that does not branch on it is wrong on exactly the
  machines where it matters most — the ones that are wide open while the CLI says a grant reaches
  nothing. `allowlist_phrase` is that sentence's one home; `doctor` reads it too.
- **The holder is named, never implied.** Everything the machine permits is keyed to an agent id —
  the per-agent consent especially — so the CLI says whose authority it is acting under, prints it,
  and lets `--agent` act as someone else. That is also how a person finds out what *another* agent may
  do, which is the question the per-agent grants exist to answer.
- **`--json` matches the rest of the CLI**: `json.dumps(payload, indent=2, sort_keys=True,
  default=str)` and nothing else on stdout (see `cli._emit`). Diagnostics go to stderr, so
  `system state --json | jq` works — the same contract `tests/test_phase38_cli_parity.py` holds every
  other command to.
- **The refusal wording comes from the tool layer, verbatim.** What is printed is the tool's own text.
  A CLI that re-worded it would be a second description of the same permission, which is the drift
  `syscap` exists to prevent.

Wiring into `engine/cli.py` — one line inside `build_parser`, just before the final `return parser`::

    from .systemcli import install as _install_system; _install_system(sub, common)

Usage:
    python3 -m engine.systemcli list
    python3 -m engine.systemcli state --json
    python3 -m engine.systemcli volume --set 30
    python3 -m engine.systemcli consent grant --tool set_volume
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "EXIT_REFUSED", "Holder", "Console", "COMMANDS", "TOOLS_BY_COMMAND", "NEXT_SEP",
    "allowlist_phrase", "build_parser", "install", "main",
]


#: Exit codes. The first three are `cli`'s own, spelled here so this module can be read and run on
#: its own; `EXIT_REFUSED` is the one this module adds, and it is what makes a permission decision
#: scriptable — `engine.cli system state --json` exiting 3 tells a caller to stop and ask, where
#: exiting 1 would read as "try again".
EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3

#: What separates a `next` command from the reason it is the next one. Spelled once because the same
#: string goes into the `--json` `next` field and into the text line: a caller that wants only the
#: command splits on it, and a person and the app read one sentence rather than two paraphrases.
NEXT_SEP = "   — "


# ── whose authority the console is acting under ──────────────────────────────


@dataclass(frozen=True)
class Holder:
    """The principal a command acts as: an agent id, a name, and the grants in force.

    Not an `AgentSpec`, and deliberately: the holder is sometimes a *view* of an agent rather than the
    agent itself (see `_console`), and copying a roster spec here would let a command mutate the
    roster by accident. `ToolRegistry` reads exactly two attributes off whatever it is handed —`.id`
    and `.capabilities` — so this carries those two plus the sentence that says where they came from,
    which is what a person reading `system list` needs to trust the rest of the output.
    """

    id: str
    name: str
    capabilities: tuple[str, ...]
    why: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "grants": list(self.capabilities),
                "why": self.why}


#: The Owner's id in every roster, and the id `_holder` falls back to. Imported rather than written
#: out because it is the key the mailbox, the ledger and the human-handoff path are all stored under —
#: a second spelling would put the console's approvals at a gate the run never reads.
def _owner_id() -> str:
    from .org.roster import OWNER_ID

    return OWNER_ID


# ── shared helpers, matching `cli`'s own ─────────────────────────────────────


def _warn(message: str) -> None:
    """Write a diagnostic to stderr, keeping stdout for the answer."""
    print(message, file=sys.stderr)


def _emit(payload: dict[str, Any], *, as_json: bool, human: str) -> None:
    """Print either machine or human output, never both.

    The JSON form is spelled out rather than imported from `cli` so this module has no import-time
    dependency on the file that installs it — `cli` imports this one, and a cycle through a helper
    would break the moment `cli` is mid-edit. The format is `cli._emit`'s, character for character.
    """
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(human)


# ── what each subcommand can reach ───────────────────────────────────────────

#: Subcommand -> the catalogue tools it can drive. Declared as data so a test can prove two things
#: without hand-copying a list: every name here is a real catalogue entry, and the union of every
#: command's tools is *every* catalogue entry — so a tool added to `sysctl_tools` cannot become
#: unreachable from the terminal without a test going red.
TOOLS_BY_COMMAND: dict[str, tuple[str, ...]] = {
    "list": (),
    "state": ("system_state",),
    "clipboard": ("read_clipboard", "write_clipboard"),
    "screenshot": ("take_screenshot",),
    "volume": ("get_volume", "set_volume", "set_mute"),
    "open": ("open_app",),
    "automation": ("run_automation",),
    "notify": ("say_message", "post_notification"),
    "search": ("spotlight_search",),
    "power": ("keep_awake", "sleep_now"),
    "network": ("network_status",),
    "shortcuts": ("run_shortcut",),
    "softwareupdate": ("list_os_updates", "install_os_updates"),
    "call": (),
}

#: The parser's command names, derived from the table above rather than restated. The keys are the
#: subcommands a person types; `consent` is added by `install` and reaches no tool (it writes the
#: ledger, which is a decision about the machine rather than an action on it).
COMMANDS: tuple[str, ...] = tuple(sorted(TOOLS_BY_COMMAND)) + ("consent",)


# ── the console: config, roster, registry, ledger ────────────────────────────


@dataclass
class Console:
    """Everything one command needs, resolved once.

    Built per command rather than cached, because the whole point of the command is to act on the
    machine *now*: a cached registry would hold a stale roster and a stale consent decision, and the
    consent decision is the one thing here that changes between two invocations by design.
    """

    config: Any
    config_path: Path | None
    project: Path
    holder: Holder
    registry: Any
    as_json: bool = False
    #: The outcome of the last `invoke`, so a command that needs to *act on* what a tool produced
    #: reads it from the tool's own record rather than guessing at it. Only `screenshot` uses this
    #: today; keeping it on the console means no command has to re-derive a fact the tool reported.
    last_result: Any = None
    #: The tool the last `invoke` called, for `report` to name in its document.
    last_tool: str = ""
    #: Where the last call's files ended up. Starts as the tool's own `paths` and is *replaced* by a
    #: command that moves the file, so the printed document names a file that exists.
    last_paths: list[str] = field(default_factory=list)

    #: `.agent_state/` — where the ledger the consent gate reads actually is. Spelled from the
    #: resolved project root, so it is byte-identical to the directory `ToolRegistry` hands to
    #: `SystemTools`; a second spelling would put an approval somewhere nothing reads.
    @property
    def state_dir(self) -> Path:
        from .state import ENGINE_STATE_DIRNAME

        return self.project / ENGINE_STATE_DIRNAME

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "ledger.jsonl"

    # ── consent, read through public API only ───────────────────────────────

    def consent_state(self, tool: str) -> str:
        """`"approved"`, `"requested"`, `"none"`, or `"not-required"` for this holder.

        Reads the ledger directly with `Ledger.current` and the module's own public `consent_gate`,
        which is the *same* reading `SystemTools._has_consent` performs — so a CLI that says "already
        approved" cannot be contradicted by the call that follows.
        """
        from .sysctl_tools import CONSENT_REQUIRED, consent_gate, request_gate

        if tool not in CONSENT_REQUIRED:
            return "not-required"
        ledger = self._ledger()
        if ledger is None:
            return "none"
        try:
            decision = ledger.current(consent_gate(tool, self.holder.id))
            if decision is not None and str(decision.choice) == "approved":
                return "approved"
            if ledger.current(request_gate(tool, self.holder.id)) is not None:
                return "requested"
        except Exception:  # noqa: BLE001 - an unreadable ledger means "not approved", never a crash
            return "none"
        return "none"

    def _ledger(self) -> Any:
        """The decision ledger, or None when there is not one to read.

        `Ledger` tolerates a missing file (a `record` creates it), so this does not require one: a
        fresh project has no ledger and the first `consent grant` is what makes one.
        """
        try:
            from .org.ledger import Ledger

            return Ledger(path=self.ledger_path)
        except Exception:  # noqa: BLE001
            return None

    def consent_line(self, tool: str) -> str:
        """The one command that changes this tool's consent state, ready to paste."""
        return f"engine.cli system consent grant --tool {tool} --agent {self.holder.id}"

    # ── the call ────────────────────────────────────────────────────────────

    def invoke(self, tool: str, arguments: dict[str, Any] | None = None, *,
               defer_output: bool = False) -> int:
        """Run one catalogue tool and report it the way a person reads. Returns an exit code.

        The order is the design: the two gates are asked first so the CLI can name the switch, then
        `registry.call` enforces them for real. A pre-check that *decided* would be a second gate; one
        that only *explains* costs nothing, because the call refuses on its own terms either way.

        `defer_output` exists for exactly one caller: `screenshot --path` *changes the answer* after the
        call (the tool's path becomes the file's, via a move), so it needs to report once at the end
        rather than printing now and printing again. It leaves `last_result` populated and writes
        nothing; the caller then calls `report` with the same exit code.
        """
        from .sysctl_tools import CATALOGUE

        entry = next((e for e in CATALOGUE if e.name == tool), None)
        if entry is None:
            _warn(f"no system tool named {tool!r}; `system call` lists what exists")
            return EXIT_USAGE

        blocked = self._blocked(entry, arguments or {})
        if blocked is not None:
            return blocked

        result = self.registry.call(tool, dict(arguments or {}))
        self.last_result = result
        self.last_tool = tool
        self.last_paths = list(result.paths)
        code = self._code_for(tool, result)
        if not defer_output:
            self.report(code)
        return code

    def _code_for(self, tool: str, result: Any) -> int:
        """The exit code for one call, so `invoke` and a deferred `report` cannot disagree.

        Whether this was a *refusal* rather than a failure decides it, and the tool module's own
        convention is the test: every refusal it writes begins `"<tool> refused:"`, while every failure
        begins `"<tool> failed:"`. Read off the text rather than off a flag the registry does not carry
        — a second signal would be a second thing to keep in step.
        """
        if result.ok:
            return EXIT_OK
        return EXIT_REFUSED if f"{tool} refused:" in result.text else EXIT_CHECK_FAILED

    def report(self, code: int) -> None:
        """Print the last call's result, using `self.last_paths` for where the files actually are.

        `last_paths` starts as the tool's own report and is *replaced* when a caller moves what the tool
        wrote, so the document a `--json` caller parses names the file that exists rather than the one
        that did not survive the move.
        """
        result = self.last_result
        tool = self.last_tool
        if result is None:
            return
        paths = list(self.last_paths or result.paths)
        refused = code == EXIT_REFUSED
        payload = {
            **result.as_dict(),
            "tool": tool,
            "grant": self.registry.SYSTEM_TOOL_CAPABILITY.get(tool, ""),
            "holder": self.holder.as_dict(),
            "refused": refused,
            "paths": paths,
            "ledger": str(self.ledger_path),
        }
        if self.as_json:
            _emit(payload, as_json=True, human="")
            return
        print(result.text)
        if result.ok and paths and paths != list(result.paths):
            print(f"moved to {paths[0]}")
        if not result.ok:
            # The tool's own words are the answer; what the CLI adds is who this was said to and where
            # the record lives, because that is the part a person needs in order to act.
            _warn(f"({tool} as {self.holder.name} <{self.holder.id}> — "
                  f"decisions for this holder are recorded in {self.ledger_path})")

    def _blocked(self, entry: Any, arguments: dict[str, Any]) -> int | None:
        """A person-facing refusal, or None when the call should go ahead.

        Three things can stop a command before it reaches the tool, and each one has a different next
        move for the person reading it: the section could be off, the grant could be missing, or the
        approval could be. Everything the *handler* refuses afterwards (an allowlist, a bound, a
        missing binary) is reported by the handler itself.

        The reason is written to **stderr** in both output modes, because stdout is the answer and a
        script reading it must not find a sentence where it expected a document. `--json` additionally
        writes the refusal as a document to stdout, so the caller that most needs the reason can read
        it as data instead of scraping a message off a diagnostic stream.
        """
        section = getattr(self.config, "system", None)
        if not bool(getattr(section, "enabled", False)):
            return self._refused(
                entry,
                "the machine tools are off, so nothing here is even offered — the engine cannot act "
                "on this Mac until an operator says so",
                required=f"system.enabled = true in {self.config_path or '(the config)'}",
                # The command that flips the switch, not just the command that describes it. The
                # remedy used to be `doctor` alone, which was the worst kind of dead end: `doctor`
                # could not read `[system]` at all, so the tool this refusal sent a person to said
                # "all checks passed" and named nothing. `doctor` reads the posture now (its `system
                # access` check), so it is worth keeping as the second half — the whole picture is
                # larger than the one switch that fixes this refusal.
                remedy="engine.cli system enable --on   (then engine.cli doctor for the whole "
                       "machine posture)")
        grant = str(getattr(entry, "capability", "") or "")
        if not self.reaches(grant):
            return self._refused(
                entry,
                f"it reaches {grant}, and this holder does not have that grant",
                required=grant,
                remedy=f"engine.cli hire <name> --skill <skill> --capability {grant}   (or act as a "
                       f"holder that has it, with --agent)")
        if self.consent_state(entry.name) == "none" and not self.full_access:
            return self._refused(
                entry,
                "it changes something on this Mac, and no approval covers it. Approvals are per "
                "holder and per tool, and are given once",
                required=f"a live 'approved' decision at gate {self._gate(entry.name)!r} in "
                         f"{self.ledger_path}",
                remedy=self.consent_line(entry.name))
        return None

    def _refused(self, entry: Any, why: str, *, required: str, remedy: str) -> int:
        """Report a refusal in the tool layer's own three-line shape, and return its exit code.

        The shape is deliberate (`required` / `held` / a next move) because that is the shape every
        refusal a *model* reads has, and the console is the same engine answering a different reader.
        A person who has seen one refusal here has seen them all.
        """
        held = ", ".join(self.holder.capabilities) or "(none)"
        lines = [
            f"{entry.name} refused: {why}",
            f"  required: {required}",
            f"  held    : {held}",
            f"  acting as: {self.holder.name} <{self.holder.id}> — {self.holder.why}",
            f"  do this : {remedy}",
        ]
        _warn("\n".join(lines))
        if self.as_json:
            _emit({"ok": False, "refused": True, "tool": entry.name,
                   "grant": str(getattr(entry, "capability", "") or ""),
                   "holder": self.holder.as_dict(), "required": required, "held": held,
                   "reason": why, "remedy": remedy, "ledger": str(self.ledger_path)},
                  as_json=True, human="")
        return EXIT_REFUSED

    def _gate(self, tool: str) -> str:
        from .sysctl_tools import consent_gate

        return consent_gate(tool, self.holder.id)

    @property
    def full_access(self) -> bool:
        """`system.allow_full_access` — the mode where the allowlists and the ask-once gate step
        aside. Read from the config, never from a flag, so no argument can widen it."""
        return bool(getattr(getattr(self.config, "system", None), "allow_full_access", False))

    def reaches(self, grant: str) -> bool:
        """Whether this holder's grants reach one capability.

        Through `syscap.granted_in`, which implements the same equality-with-`*` rule the registry
        enforces and is pinned against it by `tests/test_phase40_capability_surface.py`. Written here
        as a call rather than a comparison so the wildcard rule has one home.
        """
        from .syscap import granted_in

        return grant in {c.grant for c in granted_in(self.holder.capabilities)}


def _project_root(args: argparse.Namespace) -> Path:
    """The folder whose `.agent_state/` the approvals live in, and the root the tools are confined to.

    The current directory by default, because a person typing `system state` in a terminal means "this
    machine", not "this project" — and the machine tools take no path at all, so the only thing the
    folder decides is where the ledger goes. `--project` points it at a project instead, which is what
    you want when the approvals should be the *same ones* an agent's run there reads.

    `--org` is honoured too, because `cli`'s own common parent offers it on every command: a person who
    typed `system state --org my-org` meant *that* org's approvals, and silently using the shell's
    directory instead would act under a different holder's ledger than the flag named. It resolves
    through the shared `portfolio.workspace_for`, so a managed org reads `projects/<slug>` and one
    command and the next agree about which folder an org is.
    """
    project = getattr(args, "project", None)
    if project:
        return Path(project).expanduser().resolve()
    ref = getattr(args, "org", None)
    if ref:
        from .portfolio import Portfolio, PortfolioError, workspace_for
        from .state import StateError

        try:
            portfolio = Portfolio.load()
        except PortfolioError as exc:
            raise LookupError(f"cannot read the portfolio: {exc}") from exc
        if portfolio is None:
            raise LookupError("no portfolio exists, so no org can be named; create one with "
                              "`engine.cli portfolio add <name> --path <folder>`")
        try:
            return workspace_for(portfolio.org(ref)).path.resolve()
        except (PortfolioError, StateError) as exc:
            raise LookupError(str(exc)) from exc
    root = getattr(args, "root", None)
    if root:
        from . import usercfg

        return usercfg.project_root(root).parent.resolve()
    return Path.cwd().resolve()


def _holder(args: argparse.Namespace, config: Any, project: Path) -> Holder:
    """Who this command acts as.

    `--agent` acts under a roster agent's grants, which is how a person asks "may *it* do this?" and
    gets the engine's own answer. Without it the console acts as the Owner, holding `system:*`: the
    person at the keyboard is the authority the whole consent machinery exists to reach, and a console
    that refused to touch the machine until some *other* agent had been hired for the privilege would
    answer the ask with a setup step. What it does not do is skip a scope — every allowlist and every
    approval still applies, so nothing is widened by being typed by hand.
    """
    ref = str(getattr(args, "agent", "") or "").strip()
    if not ref:
        name = "Owner"
        try:
            org = _roster(config, project)
            owner = org.owner() if org is not None else None
            if owner is not None:
                name = owner.name
        except Exception:  # noqa: BLE001 - a broken roster must not block the person's own console
            pass
        return Holder(
            id=_owner_id(), name=name, capabilities=("system:*",),
            why="the console's own holder — a command you type is the grant; --agent acts as someone "
                "else instead")

    org = _roster(config, project)
    if org is None:
        raise LookupError(f"no roster could be loaded for {project}, so --agent {ref!r} cannot be "
                          "resolved")
    spec = next((a for a in org.agents.values() if a.id == ref), None)
    if spec is None:
        spec = next((a for a in org.agents.values() if a.name.lower() == ref.lower()), None)
    if spec is None:
        raise LookupError(f"no agent {ref!r} in the roster; `engine.cli agents` lists every id and name")
    return Holder(id=spec.id, name=spec.name, capabilities=tuple(spec.capabilities),
                  why=f"the grants on {spec.name}'s roster entry")


def _roster(config: Any, project: Path) -> Any:
    """The effective roster for this project, or None when it cannot be built.

    Every failure is a `None` rather than an exception: the roster is needed only for *naming* the
    holder and for `--agent`, and a person whose roster file is broken should still be able to read
    their own battery level.
    """
    try:
        from .catalog import ModelCatalog
        from .people import People
        from .providers.registry import build_providers

        providers, _ = build_providers(config)
        people = People(library=None, config=config,
                        catalog=ModelCatalog(config, providers), project=project)
        return people.load(project=project)
    except Exception:  # noqa: BLE001
        return None


def _load_config(args: argparse.Namespace) -> tuple[Any, str]:
    """The config and the reason it could not be read, one of which is always empty."""
    from .config import ConfigError, load

    try:
        return load(getattr(args, "config", None)), ""
    except ConfigError as exc:
        return None, str(exc)


def _console(args: argparse.Namespace, *, tolerate_broken_config: bool = False
             ) -> tuple[Console | None, int]:
    """Build the console. Returns `(console, EXIT_OK)` or `(None, code)` with the reason on stderr."""
    from .config import SystemConfig

    config, problem = _load_config(args)
    if config is None:
        if not tolerate_broken_config:
            _warn(f"configuration error: {problem}")
            return None, EXIT_CHECK_FAILED
        # `list` is a *description* command: a person asking what a grant means should get an answer
        # even when the engine is unhappy about something else, which is the same rule `serve`'s
        # `_cmd_system` follows. The default section describes every capability with everything off.
        _warn(f"warning: {problem} — describing the defaults instead of your config")
        config = type("Config", (), {"system": SystemConfig()})()

    project = _project_root(args)
    try:
        holder = _holder(args, config, project)
        registry = _registry_for(config, project, holder)
    except LookupError as exc:
        _warn(str(exc))
        return None, EXIT_CHECK_FAILED

    return Console(
        config=config,
        config_path=getattr(config, "path", None),
        project=project,
        holder=holder,
        registry=registry,
        as_json=bool(getattr(args, "json", False)),
    ), EXIT_OK


def _registry_for(config: Any, project: Path, holder: Holder) -> Any:
    """The registry every command in this module goes through.

    **Why the registry and not `SystemTools` directly.** `ToolRegistry.call` is where the capability
    gate lives — `SYSTEM_TOOL_CAPABILITY` plus `_granted_scoped` — so the CLI reaching the tool object
    would bypass the one check that decides whether this holder may act at all. The CLI and an agent's
    tool call therefore arrive at `SystemTools` by the identical path, which is what makes "there is one
    implementation of set_volume" true rather than aspirational.

    Split out as a function so a test can substitute the class and prove the command used it. `sandbox`
    is deliberately not passed: this module reaches the machine through the system tools, and offering a
    shell it never uses would put a capability in the registry that no command here can be asked for.
    """
    from .tools import ToolRegistry

    return ToolRegistry(workspace_root=project, agent=holder,
                        system=getattr(config, "system", None))


# ── list ─────────────────────────────────────────────────────────────────────


def cmd_list(args: argparse.Namespace) -> int:
    """What exists, what each capability reaches, and whether this holder has it.

    Renders `syscap.describe()` — the same function `serve._cmd_system` returns to the app — so the
    terminal and the console cannot say different words about the same grant. A capability with no
    tool behind it is marked, because a switch that buys nothing is worse than an absent one.

    **The mark on each row is one holder's, and the row says whose.** "granted" answered for the
    acting holder — the Owner by default, who holds `system:*` — so every row read `granted` on a
    machine whose roster held six of the twelve: two surfaces, one word, two meanings. So the marks
    are labelled with the holder they are about, `--holders` answers the other question (every
    roster holder, per grant), and the counts are named for what they count: `holds` is the
    capability count, `approvals` is the ledger. Two different numbers under one word is the
    confusion this block exists to end.
    """
    console, code = _console(args, tolerate_broken_config=True)
    if console is None:
        return code

    from .syscap import console_payload, summary

    section = getattr(console.config, "system", None)
    payload = console_payload(section)
    payload["holder"] = console.holder.as_dict()
    payload["config"] = str(console.config_path or "")
    payload["ledger"] = str(console.ledger_path)
    payload["consent"] = {tool: console.consent_state(tool) for tool in _consent_tools()}
    # The holder's own counts, *replacing* the description's abstract one. `console_payload` reports
    # the capability set (no holder, nothing granted), because that is the document a description
    # reader compares against; a command answering for a named holder adds the answer to "and how much
    # of this do I have". Replacing rather than adding a second key keeps one number meaning one
    # thing — two counts in one document is how a reader picks the wrong one.
    counts = summary(console.holder.capabilities)
    payload["granted"] = counts["granted"]
    payload["unmet"] = counts["unmet"]
    payload["granted_count"] = counts["granted_count"]
    payload["total"] = counts["total"]
    payload["summary"] = counts["text"]
    # The other reading of the same rows, always in the document: `list` is one call, and a person
    # asking "who can do this" should not have to know that a flag exists to ask it.
    payload["holders"] = holders_by_grant(console.config, console.project)
    payload["next"] = system_next(section)

    if console.as_json:
        _emit(payload, as_json=True, human="")
        return EXIT_OK

    entries = payload["capabilities"]
    built = [e for e in entries if e["available"]]
    print(f"system capabilities — {len(entries)} declared, {len(built)} with a tool behind them")
    print()
    print(f"the mark on each row is {console.holder.name} <{console.holder.id}>'s own grant — the "
          f"acting holder; `--holders` lists every roster holder for each grant")
    print()
    for entry in entries:
        held = console.reaches(entry["grant"])
        mark = "granted    " if held else "not granted"
        print(f"  {mark}  {entry['grant']:24s} {entry['title']}")
        print(f"           reaches : {entry['reaches']}")
        print(f"           changes : {entry['changes'] or 'nothing — this one only reads'}")
        if entry["caution"]:
            print(f"           caution : {entry['caution']}")
        if not entry["available"]:
            print("           NOT BUILT YET — no tool backs this grant, so switching it on buys "
                  "nothing today")
        if args.holders:
            holders = payload["holders"].get(entry["grant"]) or []
            print(f"           holders : {_holder_line(holders)}")
    print()
    print("settings")
    # `yes`/`no` rather than the Python bools, because `system enable` prints these two switches the
    # same words — one switch spelled two ways is how a person comparing two commands concludes they
    # disagree about it.
    print(f"  enabled          : {'yes' if payload['enabled'] else 'no'}"
          + ("" if payload["enabled"] else "   (system.enabled — off means no machine tool is even "
                                            "offered)"))
    print(f"  full access      : {'yes' if payload['full_access'] else 'no'}"
          + ("   (no allowlists and no consent prompt)" if payload["full_access"] else ""))
    # The mode is passed to every allowlist phrase, because it is the difference between "none
    # allowed" and "no list is kept" — see `allowlist_phrase`.
    print(f"  allow_apps       : "
          f"{allowlist_phrase(payload['allow_apps'], full_access=payload['full_access'])}")
    print(f"  allow_automation : "
          f"{allowlist_phrase(payload['allow_automation'], full_access=payload['full_access'])}")
    print(f"  allow_shortcuts  : "
          f"{allowlist_phrase(payload['allow_shortcuts'], full_access=payload['full_access'])}")
    print()
    print("holder")
    print(f"  acting as        : {console.holder.name} <{console.holder.id}>")
    print(f"  grants           : {', '.join(console.holder.capabilities)}")
    print(f"  why              : {console.holder.why}")
    # `holds` counts capabilities; `approvals` counts ledger decisions. They were one word, and they
    # are 12 and 0 on the same machine, which is exactly how a person reads the wrong one.
    print(f"  holds            : {payload['summary']}")
    print("  approvals        : "
          + _approval_line(payload["consent"], full_access=payload["full_access"]))
    print(f"  ledger           : {console.ledger_path}")
    if payload["unavailable"]:
        print(f"  built            : declared but not built: {', '.join(payload['unavailable'])}")
    print()
    print(f"next   : {payload['next']}")
    return EXIT_OK


def _approval_line(consent: dict[str, str], *, full_access: bool) -> str:
    """How many state-changing tools this holder has approved, and whether it matters here.

    The count is worded as `consent list` words it, because one number under two spellings is the
    same defect as two numbers under one word. Under full access the gate has stepped aside, so the
    count is reported and immediately qualified — a person reading `0 of 10 approved` there would
    otherwise conclude that nothing may run.
    """
    total = len(consent)
    approved = sum(1 for state in consent.values() if state == "approved")
    line = f"{approved} of {total} state-changing tools approved"
    if full_access:
        return line + "   (full access is on, so no approval is required — the ask-once gate has " \
                      "stepped aside)"
    return line


def _holder_line(holders: list[dict[str, Any]]) -> str:
    """One grant's roster holders, or the honest absence of any."""
    if not holders:
        return "(nobody in the roster — only a holder with `system:*`, like the acting one, reaches it)"
    return ", ".join(f"{h['name']} <{h['id']}>" for h in holders)


def holders_by_grant(config: Any, project: Path) -> dict[str, list[dict[str, Any]]]:
    """Every roster holder whose grants reach each capability.

    **Why the roster and not the acting holder.** `system list` marks each row with the holder the
    command acts as, which answers "may *I* do this" and cannot answer "who can". On this machine
    the default holder holds `system:*`, so every row read `granted`, while the served roster held
    six of the twelve grants — one holder, `ag_ux`. Two surfaces, one word, two meanings, and the
    surface with the roster in it said nothing.

    A holder that cannot be resolved (a broken roster) yields every grant with no holders rather
    than an error: the listing is a description, and a description that refuses to render because a
    peripheral file is unreadable is worse than one that says nobody holds anything.
    """
    from .syscap import describe, granted_in

    org = _roster(config, project)
    out: dict[str, list[dict[str, Any]]] = {entry.grant: [] for entry in describe()}
    if org is None:
        return out
    for spec in org.agents.values():
        for entry in granted_in(getattr(spec, "capabilities", None) or ()):
            if entry.grant in out:
                out[entry.grant].append({"id": spec.id, "name": spec.name})
    for holders in out.values():
        holders.sort(key=lambda h: (str(h["name"]).lower(), str(h["id"])))
    return out


def _consent_tools() -> tuple[str, ...]:
    """Every tool that needs an approval, in a stable order. Read off the tool module, not restated."""
    from .sysctl_tools import CONSENT_REQUIRED

    return tuple(sorted(CONSENT_REQUIRED))


def allowlist_phrase(values: Sequence[str], *, full_access: bool) -> str:
    """How an allowlist reads — which depends on the mode the engine is actually in.

    **The mode is the whole sentence.** `allow_full_access` makes `sysctl_tools` skip the allowlist
    check outright (`open_app`, `run_automation`, `run_shortcut` all guard their empty-list refusal
    with `if not self.full_access`), so an empty list means "nothing may launch/run" in the scoped
    mode and "no list is kept" in that one. Printing the scoped reading under a `full access : yes`
    line was not merely imprecise: on this machine `allow_automation` and `allow_shortcuts` are empty
    *and* full access is on, so the CLI told a person that AppleScript reaches nothing at the moment
    it reached everything. The second half of the sentence is `sysctl_tools`' own words, because a
    paraphrase of a permission is a second description of it.

    One function, two callers: `system list`, `system enable`, `system shortcuts` and `cli.doctor` all
    print this, so the mode cannot be described one way in the listing and another in the diagnosis.
    """
    if values:
        return ", ".join(values)
    if full_access:
        return "(empty — no list is kept: full access is on, so this reaches anything it could)"
    return "(empty — none allowed)"


def system_next(section: Any) -> str:
    """The one line a person should be given after a report about `[system]`.

    **Derived, not fixed.** The hint this replaces pointed at `system consent list` unconditionally,
    which on a full-access machine is a switch that has already stepped aside — a refusal that cannot
    happen is a next move worth nothing. So the mode decides: nothing is offered until `enabled`, the
    allowlists and the approval are off the table while full access is on, and only in the scoped
    mode is the ledger the thing that decides.

    Returns the whole line, separator included, so the `--json` `next` field and the text line are
    the same string; `NEXT_SEP` is where a caller that wants only the command splits it.
    """
    if not bool(getattr(section, "enabled", False)):
        return f"engine.cli system enable --on{NEXT_SEP}the machine tools are offered to nobody until " \
               f"this is on"
    if bool(getattr(section, "allow_full_access", False)):
        return f"engine.cli system enable --no-full-access{NEXT_SEP}restore the allowlists and the " \
               f"per-action approval"
    return f"engine.cli system consent list{NEXT_SEP}every state-changing tool, and whether this " \
           f"holder has approved it"


def _mode_changes(section: Any) -> list[str]:
    """The commands that change the state `_render_switches` just reported, derived from it.

    `system enable` with no switch is the one command that answers a *question* about the machine
    access switches, and the answer used to end with `--on` on a machine that was already on — noise
    where the inverses a person staring at `full access : yes` might want belong. Each line here is
    the command for a switch whose value the report just showed, so nothing offered is irrelevant to
    what was printed; each is a whole `next`-shaped line, so it prints and serialises the same way.
    """
    enabled = bool(getattr(section, "enabled", False))
    full = bool(getattr(section, "allow_full_access", False))
    lines = [
        f"engine.cli system enable --off{NEXT_SEP}turn machine access off; every system tool stops "
        f"being offered"
        if enabled else
        f"engine.cli system enable --on{NEXT_SEP}make the machine tools offerable — this still grants "
        f"nothing on its own",
        f"engine.cli system enable --no-full-access{NEXT_SEP}restore the allowlists and the "
        f"per-action approval"
        if full else
        f"engine.cli system enable --full-access{NEXT_SEP}lift the allowlists and the per-action "
        f"approval",
    ]
    if not full:
        # Only offered in the mode where they decide something: under full access the engine skips
        # the allowlist check, so a `--allow-apps` line would be advice about a switch nobody reads.
        lines.append(f"engine.cli system enable --allow-apps Safari{NEXT_SEP}one application `open` "
                     f"may launch (repeatable; it replaces the stored list)")
        lines.append(f"engine.cli system enable --allow-shortcuts NAME{NEXT_SEP}one Shortcut "
                     f"`shortcuts` may run")
        lines.append(f"engine.cli system enable --allow-automation PREFIX{NEXT_SEP}one AppleScript "
                     f"handler `automation` may drive")
    return lines


# ── the commands that reach the machine ──────────────────────────────────────


def cmd_state(args: argparse.Namespace) -> int:
    """Battery, disk, uptime and running applications — the cheapest way to ask what this Mac is up
    to."""
    console, code = _console(args)
    return code if console is None else console.invoke("system_state")


def cmd_clipboard(args: argparse.Namespace) -> int:
    """Read the clipboard, or replace it.

    Text travels through stdin on the way *in*, because argv is readable by every process on the
    machine (`ps`) and the thing a person puts on their clipboard is often a token — the same reason
    `write_clipboard` sends it on stdin. `--set` is offered for the convenience of a sentence, and its
    exposure is stated in the help rather than hidden.
    """
    console, code = _console(args)
    if console is None:
        return code
    if args.stdin and args.set is not None:
        _warn("--stdin and --set both name the text; give one")
        return EXIT_USAGE
    if args.stdin:
        text = sys.stdin.read()
    elif args.set is not None:
        text = args.set
    else:
        return console.invoke("read_clipboard")
    return console.invoke("write_clipboard", {"text": text})


def cmd_screenshot(args: argparse.Namespace) -> int:
    """Capture the screen, optionally saving the result where you name.

    *Where* a screenshot may be written is the tool's decision (`system.screenshot_dir`, inside the
    project by default) and is left alone. `--path` is the person naming a location for their own file
    afterwards, which is a shell-level decision rather than a capability — so the CLI takes the
    screenshot through the tool and moves the file, rather than teaching the tool a second way to
    choose a destination. An existing file is never overwritten without `--force`: a screenshot that
    silently replaced something would be the one destructive act here.
    """
    console, code = _console(args)
    if console is None:
        return code

    # The destination is judged *before* the capture, not after. A capture costs the person a shutter's
    # worth of their screen and the screen-recording permission prompt on a fresh machine, so refusing
    # afterwards would spend all of that to deliver a message about an argument — and would leave a file
    # behind that nobody asked for.
    target: Path | None = None
    if args.path:
        target = Path(args.path).expanduser()
        if target.is_dir():
            _warn(f"--path names a directory ({target}); name the file to write")
            return EXIT_USAGE
        if target.exists() and not args.force:
            _warn(f"{target} already exists; pass --force to replace it")
            return EXIT_USAGE

    result = console.invoke("take_screenshot", defer_output=True)
    if result != EXIT_OK or target is None:
        # With no `--path` the answer is already final, so it is reported straight away; a failed
        # capture is reported the same way, through the one reporting path so the two modes cannot
        # diverge.
        console.report(result)
        return result

    import shutil

    # Read the path off the tool's *own* result (`ToolResult.paths`), never from a timestamp: a guessed
    # name would move the wrong file, or none, and reporting success either way is worse than failing.
    written = list(console.last_paths)
    if not written or not Path(written[0]).is_file():
        _warn(f"the screenshot was taken, but the tool reported no usable path for it, so --path was "
              f"not applied. Look in {console.state_dir / 'screenshots'}")
        console.report(result)
        return EXIT_CHECK_FAILED
    shot = Path(written[0])
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(shot), str(target))
    except OSError as exc:
        _warn(f"the screenshot was taken to {shot} but could not be moved to {target}: {exc}")
        console.report(result)
        return EXIT_CHECK_FAILED
    # The move *changes the answer*, so it is recorded before anything is printed: `report` reads
    # `last_paths`, and under `--json` that is the only document the caller parses. Printing the tool's
    # path and then a line of prose would be the one way this command could break `| jq` — so there is
    # exactly one report, at the end, naming the file that exists.
    console.last_paths = [str(target)]
    console.report(EXIT_OK)
    return EXIT_OK


def cmd_volume(args: argparse.Namespace) -> int:
    """Read the volume, or set it, or mute/unmute it."""
    console, code = _console(args)
    if console is None:
        return code
    chosen = [name for name, given in (("--set", args.set is not None), ("--mute", args.mute),
                                       ("--unmute", args.unmute)) if given]
    if len(chosen) > 1:
        _warn(f"{' and '.join(chosen)} cannot be combined; give one, or none to read the volume")
        return EXIT_USAGE
    if args.set is not None:
        return console.invoke("set_volume", {"level": args.set})
    if args.mute or args.unmute:
        return console.invoke("set_mute", {"muted": bool(args.mute)})
    return console.invoke("get_volume")


def cmd_open(args: argparse.Namespace) -> int:
    """Launch an application, if `system.allow_apps` names it."""
    console, code = _console(args)
    return code if console is None else console.invoke("open_app", {"name": args.app})


def cmd_automation(args: argparse.Namespace) -> int:
    """Run an AppleScript or JXA snippet, if its handler is in `system.allow_automation`.

    `--file` exists because a script written in a shell argument is a quoting problem, not a command:
    AppleScript is multi-line by nature.
    """
    console, code = _console(args)
    if console is None:
        return code
    if bool(args.script) == bool(args.file):
        _warn("give exactly one of --script <source> or --file <path>")
        return EXIT_USAGE
    script = args.script
    if args.file:
        try:
            script = Path(args.file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            _warn(f"cannot read the script from {args.file}: {exc}")
            return EXIT_CHECK_FAILED
    return console.invoke("run_automation", {"script": script, "language": args.language})


def cmd_notify(args: argparse.Namespace) -> int:
    """Speak a short message, or post a banner. Speaking asks the Owner first; a banner does not."""
    console, code = _console(args)
    if console is None:
        return code
    if args.say and (args.message or args.title):
        _warn("--say speaks aloud and --message posts a banner; give one")
        return EXIT_USAGE
    if args.say:
        payload: dict[str, Any] = {"text": args.say}
        if args.voice:
            payload["voice"] = args.voice
        return console.invoke("say_message", payload)
    if args.message:
        return console.invoke("post_notification",
                              {"message": args.message, "title": args.title or "AgentOrg",
                               "sound": bool(args.sound)})
    _warn("give --say <text> to speak aloud, or --message <text> to post a banner")
    return EXIT_USAGE


def cmd_search(args: argparse.Namespace) -> int:
    """Search the Spotlight index, which sees filenames beyond the project folder."""
    console, code = _console(args)
    if console is None:
        return code
    payload: dict[str, Any] = {"query": args.query}
    if args.name_only:
        payload["name_only"] = True
    if args.within:
        payload["within"] = args.within
    if args.limit is not None:
        payload["limit"] = args.limit
    return console.invoke("spotlight_search", payload)


def cmd_power(args: argparse.Namespace) -> int:
    """Hold sleep off for a while, or put the Mac to sleep now."""
    console, code = _console(args)
    if console is None:
        return code
    if args.sleep and args.awake is not None:
        _warn("--sleep and --awake are opposite requests; give one")
        return EXIT_USAGE
    if args.sleep:
        if args.display:
            _warn("--display only means something with --awake")
            return EXIT_USAGE
        return console.invoke("sleep_now")
    if args.awake is None:
        _warn("give --awake [SECONDS] to hold sleep off, or --sleep to sleep now")
        return EXIT_USAGE
    from .sysctl_tools import DEFAULT_AWAKE_SECONDS

    payload: dict[str, Any] = {"seconds": args.awake or DEFAULT_AWAKE_SECONDS}
    if args.display:
        payload["display"] = True
    return console.invoke("keep_awake", payload)


def cmd_network(args: argparse.Namespace) -> int:
    """Report the active interface, and — unless `--quick` — measure throughput.

    The measurement is the tool's default, so the CLI does not change it: `--quick` is how a person
    says "just tell me whether I am online", which is the question that costs milliseconds.
    """
    console, code = _console(args)
    return code if console is None else console.invoke("network_status",
                                                       {"measure": not args.quick})


def cmd_shortcuts(args: argparse.Namespace) -> int:
    """Run one of your own Shortcuts, or show which ones are allowed.

    Without `--run` this is a *description*, not a tool call: the run command takes a name and the
    catalogue has no "list my shortcuts" tool, so the honest answer is the configured allowlist plus
    whether this holder may pull the lever at all.

    `--json` is honoured in that mode, because every command in this CLI answers to it — a command that
    silently printed prose when a caller asked for a document would break `| jq` on a *description*
    command, which is exactly the surface a script is most likely to read.
    """
    console, code = _console(args)
    if console is None:
        return code
    if args.run:
        payload: dict[str, Any] = {"name": args.run}
        if args.input is not None:
            payload["input"] = args.input
        return console.invoke("run_shortcut", payload)

    allowed = list(getattr(getattr(console.config, "system", None), "allow_shortcuts", None) or [])
    state = console.consent_state("run_shortcut")
    held = console.reaches("system:shortcuts")
    full = console.full_access
    described = {
        "tool": "run_shortcut",
        "grant": "system:shortcuts",
        "held": held,
        "holder": console.holder.as_dict(),
        "allowed": allowed,
        "approval": state,
        "full_access": full,
        "remedy": None if state in ("approved", "not-required")
                  else console.consent_line("run_shortcut"),
        "usage": 'engine.cli system shortcuts --run "<name>" [--input TEXT]',
    }
    # The next move, in the mode this machine is in: under full access the approval below is not a
    # gate at all, so pointing at `consent grant` would be a step that decides nothing.
    if full:
        described["next"] = (f'engine.cli system shortcuts --run "<name>"{NEXT_SEP}full access is '
                             f'on, so neither the allowlist nor the approval is consulted')
    elif described["remedy"]:
        described["next"] = f"{described['remedy']}{NEXT_SEP}it will not ask again"
    else:
        described["next"] = described["usage"]
    if console.as_json:
        _emit(described, as_json=True, human="")
        return EXIT_OK

    print("run_shortcut — pulls a lever you already built; it cannot write a new one")
    print(f"  allowed  : {allowlist_phrase(allowed, full_access=full)}")
    print(f"  grant    : system:shortcuts — "
          f"{'held' if held else 'NOT held'} by "
          f"{console.holder.name} <{console.holder.id}>")
    print(f"  approval : {state}"
          + (f"  — grant it with: {described['remedy']}" if described["remedy"] else "")
          + ("   (not consulted: full access is on)" if full else ""))
    print()
    print(f"next   : {described['next']}")
    return EXIT_OK


def cmd_softwareupdate(args: argparse.Namespace) -> int:
    """List the available macOS updates, or install them.

    The default is the *listing*: installing changes the operating system and can reboot the Mac, so
    it cannot be what a bare `system softwareupdate` does. `--install` with no labels installs the
    recommended set, which is what the tool does when it is given no labels.
    """
    console, code = _console(args)
    if console is None:
        return code
    if args.install is None:
        return console.invoke("list_os_updates", {"no_scan": not args.scan})
    if args.list_only:
        _warn("--list and --install are opposite requests; give one")
        return EXIT_USAGE
    payload: dict[str, Any] = {"labels": list(args.install), "restart": bool(args.restart)}
    return console.invoke("install_os_updates", payload)


def cmd_call(args: argparse.Namespace) -> int:
    """Call any tool in the catalogue by name, with `--arg key=value`.

    The escape hatch, and the reason the CLI cannot fall behind the tool layer: a capability added to
    `sysctl_tools` is reachable from a terminal the same day, through the identical gate. Values are
    decoded as JSON where they look like it and passed as text otherwise, so `--arg level=30` gives an
    integer and `--arg name=Safari` a string; `--args-json` takes a whole object for the nested cases
    (`--arg labels='["a","b"]'` also works).
    """
    console, code = _console(args)
    if console is None:
        return code
    if args.args_json:
        try:
            payload = json.loads(args.args_json)
        except json.JSONDecodeError as exc:
            _warn(f"--args-json is not valid JSON: {exc}")
            return EXIT_USAGE
        if not isinstance(payload, dict):
            _warn("--args-json must be a JSON object of the tool's parameters")
            return EXIT_USAGE
    else:
        payload = {}
    for raw in args.arg or []:
        if "=" not in raw:
            _warn(f"--arg expects key=value, got {raw!r}")
            return EXIT_USAGE
        key, _, value = raw.partition("=")
        payload[key.strip()] = _decode_arg(value)
    return console.invoke(args.tool, payload)


def _decode_arg(value: str) -> Any:
    """One `--arg` value, decoded the way a person writing JSON on a command line expects.

    JSON first, text second: `--arg muted=true` must send the boolean the tool validates, and
    `--arg name=Safari` must send the string. `json.loads` alone would turn `Safari` into a
    `JSONDecodeError`; text alone would turn `true` into the string `"true"`, which `set_mute` refuses
    as "muted must be true or false" — a refusal that reads like a bug in the engine.
    """
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


# ── consent ──────────────────────────────────────────────────────────────────


def cmd_consent(args: argparse.Namespace) -> int:
    """Show, give or withdraw a standing approval for one tool, for one holder.

    `by` is the **person**, taken from the portfolio's principal — never the agent id, because
    `grant_consent` refuses a `by` that names an agent by design: an approval an agent can give itself
    is not an approval. That guard is the reason this command exists rather than the engine simply
    treating a mutating call as approved once it has been typed at a terminal.
    """
    console, code = _console(args)
    if console is None:
        return code
    if args.consent_command == "list":
        return _consent_list(console)
    if args.consent_command == "grant":
        return _consent_grant(console, args)
    if args.consent_command == "revoke":
        return _consent_revoke(console, args)
    _warn("give list, grant or revoke")
    return EXIT_USAGE


def _require_consent_tool(name: str) -> str | None:
    """The tool name, or None with the reason already on stderr.

    A read is not a decision, so `--tool system_state` is refused *here*, with the set that would
    work — rather than by `grant_consent`'s exception, whose wording is addressed to a caller rather
    than to a person.
    """
    tools = _consent_tools()
    if name in tools:
        return name
    if name in {e for e in _all_tool_names()}:
        _warn(f"{name!r} reads and changes nothing, so there is nothing to approve. Only these ask "
              f"first:\n  {', '.join(tools)}")
        return None
    _warn(f"no tool named {name!r}. The tools that need an approval are:\n  {', '.join(tools)}")
    return None


def _all_tool_names() -> tuple[str, ...]:
    from .sysctl_tools import CATALOGUE

    return tuple(e.name for e in CATALOGUE)


def _consent_list(console: Console) -> int:
    """Every tool that asks first, and where this holder stands with each one."""
    states = {tool: console.consent_state(tool) for tool in _consent_tools()}
    unapproved = [tool for tool in _consent_tools() if states[tool] != "approved"]
    if console.full_access:
        # The ledger's own reading is not the gate in this mode, and a `next` pointing into it would
        # be a step that decides nothing.
        step = (f"engine.cli system enable --no-full-access{NEXT_SEP}no approval is asked for while "
                f"full access is on, so this ledger decides nothing until it is off")
    elif unapproved:
        step = (f"{console.consent_line(unapproved[0])}{NEXT_SEP}the first of {len(unapproved)} this "
                f"holder has not approved")
    else:
        step = f"engine.cli system state{NEXT_SEP}a read needs no approval"
    payload: dict[str, Any] = {
        "holder": console.holder.as_dict(),
        "ledger": str(console.ledger_path),
        "full_access": console.full_access,
        "approvals": states,
        "next": step,
    }
    if console.as_json:
        _emit(payload, as_json=True, human="")
        return EXIT_OK

    print(f"approvals for {console.holder.name} <{console.holder.id}>")
    print(f"  ledger : {console.ledger_path}"
          + ("   (does not exist yet)" if not console.ledger_path.exists() else ""))
    if console.full_access:
        print("  NOTE   : system.allow_full_access is on, so no approval is required for anything "
              "— the ask-once gate has stepped aside")
    print()
    approved = 0
    for tool in _consent_tools():
        state = console.consent_state(tool)
        approved += 1 if state == "approved" else 0
        print(f"  {state:13s} {tool}")
        if state != "approved":
            print(f"                grant with: {console.consent_line(tool)}")
    print()
    print(f"{approved} of {len(_consent_tools())} state-changing tools approved for this holder. "
          "An approval is per holder and per tool, and is given once — the next call does not ask "
          "again.")
    print()
    print(f"next   : {step}")
    return EXIT_OK


def _consent_grant(console: Console, args: argparse.Namespace) -> int:
    """Record the person's approval in the ledger the tool layer reads."""
    from .sysctl_tools import ConsentError, grant_consent

    tool = _require_consent_tool(str(getattr(args, "tool", "") or "").strip())
    if tool is None:
        return EXIT_USAGE
    person = _person()
    try:
        path = grant_consent(console.state_dir, tool=tool, agent_id=console.holder.id, by=person,
                             note=args.note or "")
    except ConsentError as exc:
        _warn(str(exc))
        return EXIT_CHECK_FAILED
    except OSError as exc:
        _warn(f"cannot write the ledger at {console.ledger_path}: {exc}")
        return EXIT_CHECK_FAILED

    step = f"engine.cli system {_command_for(tool)}{NEXT_SEP}it will not ask again"
    payload = {"tool": tool, "holder": console.holder.as_dict(), "by": person,
               "gate": console._gate(tool), "ledger": str(path), "state": "approved",
               "next": step}
    if console.as_json:
        _emit(payload, as_json=True, human="")
    else:
        print(f"approved: {tool} for {console.holder.name} <{console.holder.id}>")
        print(f"  by     : {person}")
        print(f"  gate   : {console._gate(tool)}")
        print(f"  ledger : {path}")
        print(f"  next   : {step}")
    return EXIT_OK


def _consent_revoke(console: Console, args: argparse.Namespace) -> int:
    """Supersede a standing approval, so the next call is refused again."""
    from .sysctl_tools import ConsentError, revoke_consent

    tool = _require_consent_tool(str(getattr(args, "tool", "") or "").strip())
    if tool is None:
        return EXIT_USAGE
    person = _person()
    try:
        path = revoke_consent(console.state_dir, tool=tool, agent_id=console.holder.id, by=person,
                              note=args.note or "")
    except ConsentError as exc:
        # The ledger is append-only, and "there was nothing to withdraw" is the fact a person needs —
        # not a traceback. `ConsentError` is the module's own refusal, so it is reported as a refusal.
        _warn(f"{exc}\n  state: {console.consent_state(tool)}")
        return EXIT_REFUSED
    except OSError as exc:
        _warn(f"cannot write the ledger at {console.ledger_path}: {exc}")
        return EXIT_CHECK_FAILED

    payload = {"tool": tool, "holder": console.holder.as_dict(), "by": person,
               "gate": console._gate(tool), "ledger": str(path), "state": "revoked"}
    if console.as_json:
        _emit(payload, as_json=True, human="")
    else:
        print(f"withdrawn: {tool} for {console.holder.name} <{console.holder.id}>")
        print(f"  by     : {person}")
        print(f"  ledger : {path}   (the approval is superseded, not deleted — the history says who "
              "gave it and when it was taken back)")
    return EXIT_OK


def _command_for(tool: str) -> str:
    """The subcommand a person would type to reach this tool, for a "next" line.

    Derived from `TOOLS_BY_COMMAND` rather than written into each message, so a renamed subcommand
    cannot leave a stale instruction behind.
    """
    for command, tools in TOOLS_BY_COMMAND.items():
        if tool in tools:
            return f"{command} ..." if command != "state" else "state"
    return f"call {tool}"


def _person() -> str:
    """Who is giving the approval. Never an agent id, because `grant_consent` refuses one.

    The portfolio's principal when there is one — that is the identity the engine already records on
    an org's decisions — then the login name, and finally a literal that names no agent. The
    `ag_` guard is checked here as well as in `grant_consent` so a login called `ag_something` cannot
    produce an approval attributed to an agent.
    """
    try:
        from .portfolio import DEFAULT_PRINCIPAL_ID, Portfolio

        portfolio = Portfolio.load()
        candidate = portfolio.principal.id if portfolio is not None else DEFAULT_PRINCIPAL_ID
    except Exception:  # noqa: BLE001 - a broken register must not stop a person approving something
        candidate = ""
    if candidate and not str(candidate).startswith("ag_"):
        return str(candidate)

    import getpass

    try:
        login = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name is not a reason to refuse an approval
        login = ""
    if login and not login.startswith("ag_"):
        return login
    return "owner"


# ── the parser ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """The `system` command tree, standalone.

    Split from `install` so the commands are runnable and testable without `cli.py`: the module that
    owns this code must not be the only place it can be exercised, or a change here is only visible
    through a file another agent is editing. `install` adds the identical tree to `cli`'s own parser.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("--config", default=argparse.SUPPRESS, help="path to credentials.json")
    common.add_argument("--project", default=argparse.SUPPRESS,
                        help="the folder whose .agent_state holds the approvals")

    parser = argparse.ArgumentParser(
        prog="engine.systemcli",
        parents=[common],
        description=(
            "Act on this machine with the same tools the agents use, one scoped grant at a time."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes:\n"
            "  0  the command ran and worked\n"
            "  1  the command ran and failed, or the setup is broken\n"
            f"  {EXIT_USAGE}  usage error\n"
            f"  {EXIT_REFUSED}  REFUSED — the engine would not allow it (no grant, no approval, or "
            "an allowlist)\n\n"
            "start with:  python3 -m engine.systemcli list\n"
            "then:        python3 -m engine.systemcli consent list"
        ),
    )
    install(parser.add_subparsers(dest="system_command", required=True),
            _common_parent(parser, common))
    return parser


def _common_parent(parser: argparse.ArgumentParser, common: argparse.ArgumentParser
                   ) -> argparse.ArgumentParser:
    """The parser the leaves take as a parent, so `install` can be handed either surface's own.

    `cli.build_parser` owns a `common` parent with more flags on it (`--library`, `--org`) and this
    module must not assume which. Returning the one it was given keeps every leaf inheriting exactly
    what the host parser offers, so `system state --org <org>` works where `cli` accepts `--org` and
    is a usage error where it does not.
    """
    return common


def install(sub: Any, common: Any) -> Any:
    """Add the `system` command tree to an existing subparsers action. Returns the `system` parser.

    Written to take the host's own `common` parent rather than defining a second one, so a global flag
    added to `cli` reaches every command here without a change in this file.
    """
    # The automation language is the engine's own vocabulary (`sysctl_tools`), not a second copy: the
    # flag's choices and the tool's refusal have to name the same languages or `system automation
    # --language` would accept a source the tool then rejects.
    from .sysctl_tools import SCRIPT_LANGUAGES

    leaf = argparse.ArgumentParser(add_help=False)
    # `SUPPRESS` so an omitted `--agent` leaves no attribute at all: the flag means "act as someone
    # else", and a normal default would set `None` over a value the group parser had already read.
    leaf.add_argument("--agent", default=argparse.SUPPRESS,
                      help="act as this agent from the roster (name or id), using ITS grants and ITS "
                           "approvals — default: the Owner, holding every capability")

    system = sub.add_parser(
        "system", parents=[common],
        help="act on this Mac: state, clipboard, screenshots, volume, apps, scripts and the rest",
        description=(
            "Every capability the engine may use on this machine, driven from the terminal through "
            "the same tools and the same gate the agents go through. `system list` says what each "
            "grant reaches and what it changes; `system consent list` says what is approved."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"a refusal exits {EXIT_REFUSED}; a command that ran and failed exits {EXIT_CHECK_FAILED}"
            f"\na command given nothing to do exits {EXIT_USAGE} — `system enable` with no switch "
            f"prints the state and writes nothing"
        ))
    inner = system.add_subparsers(dest="system_command", required=True)

    listing = inner.add_parser("list", parents=[common, leaf],
                               help="every capability, what it reaches, what it changes, and whether "
                                    "you have it")
    listing.add_argument("--holders", action="store_true",
                         help="also list every roster holder for each grant — the answer to \"who "
                              "can do this\", which the per-row marks (the acting holder's) cannot "
                              "give. Always present in --json as `holders`.")
    listing.set_defaults(func=cmd_list)

    state = inner.add_parser("state", parents=[common, leaf],
                             help="battery, disk space, uptime and running applications")
    state.set_defaults(func=cmd_state)

    clipboard = inner.add_parser("clipboard", parents=[common, leaf],
                                 help="read the clipboard, or replace it")
    clipboard.add_argument("--set", metavar="TEXT",
                           help="replace the clipboard with TEXT (visible to `ps`; prefer --stdin "
                                "for anything secret)")
    clipboard.add_argument("--stdin", action="store_true",
                           help="replace the clipboard with everything on standard input")
    clipboard.set_defaults(func=cmd_clipboard)

    screenshot = inner.add_parser("screenshot", parents=[common, leaf],
                                  help="capture the screen into the project")
    screenshot.add_argument("--path", metavar="FILE",
                            help="also move the capture to this file (an existing file is never "
                                 "replaced without --force)")
    screenshot.add_argument("--force", action="store_true",
                            help="allow --path to replace an existing file")
    screenshot.set_defaults(func=cmd_screenshot)

    volume = inner.add_parser("volume", parents=[common, leaf],
                              help="read the volume, set it, or mute/unmute")
    volume.add_argument("--set", type=int, metavar="N", help="output volume, 0-100")
    volume.add_argument("--mute", action="store_true", help="mute the output")
    volume.add_argument("--unmute", action="store_true", help="unmute the output")
    volume.set_defaults(func=cmd_volume)

    open_app = inner.add_parser("open", parents=[common, leaf],
                                help="launch an application named in system.allow_apps")
    open_app.add_argument("app", help="the application's name, e.g. Safari")
    open_app.set_defaults(func=cmd_open)

    automation = inner.add_parser("automation", parents=[common, leaf],
                                  help="run an AppleScript/JXA handler named in "
                                       "system.allow_automation")
    automation.add_argument("--script", help="the AppleScript/JXA source")
    automation.add_argument("--file", help="read the source from this file instead")
    automation.add_argument("--language", choices=list(SCRIPT_LANGUAGES),
                            default=SCRIPT_LANGUAGES[0],
                            help="which language the source is in")
    automation.set_defaults(func=cmd_automation)

    notify = inner.add_parser("notify", parents=[common, leaf],
                              help="speak a message aloud, or post a notification banner")
    notify.add_argument("--say", metavar="TEXT", help="speak this aloud (audible to the room)")
    notify.add_argument("--voice", help="which voice to speak with")
    notify.add_argument("--message", metavar="TEXT", help="post this as a notification banner")
    notify.add_argument("--title", help="the banner's heading (default: AgentOrg)")
    notify.add_argument("--sound", action="store_true", help="play the notification sound")
    notify.set_defaults(func=cmd_notify)

    search = inner.add_parser("search", parents=[common, leaf],
                              help="search the Spotlight index — filenames beyond this project")
    search.add_argument("query", help="a Spotlight query, e.g. \"invoice\"")
    search.add_argument("--name-only", action="store_true", dest="name_only",
                        help="match the file name only, rather than a metadata query")
    search.add_argument("--within", metavar="DIR", help="limit the search to this directory")
    search.add_argument("--limit", type=int, help="how many paths to return (default 50)")
    search.set_defaults(func=cmd_search)

    power = inner.add_parser("power", parents=[common, leaf],
                             help="hold sleep off for a while, or sleep now")
    power.add_argument("--awake", nargs="?", type=int, const=0, metavar="SECONDS",
                       help="hold sleep off (default: one hour, up to 8 hours)")
    power.add_argument("--display", action="store_true", help="also keep the display on")
    power.add_argument("--sleep", action="store_true",
                       help="put the Mac to sleep now — it interrupts whatever is running")
    power.set_defaults(func=cmd_power)

    network = inner.add_parser("network", parents=[common, leaf],
                               help="report the active interface, and measure throughput")
    network.add_argument("--quick", action="store_true",
                         help="skip the throughput measurement, which moves real data and takes "
                              "about ten seconds")
    network.set_defaults(func=cmd_network)

    shortcuts = inner.add_parser("shortcuts", parents=[common, leaf],
                                 help="run one of your own Shortcuts, or show which are allowed")
    shortcuts.add_argument("--run", metavar="NAME", help="the Shortcut's name, exactly as it appears")
    shortcuts.add_argument("--input", help="text to pass as the Shortcut's input")
    shortcuts.set_defaults(func=cmd_shortcuts)

    softwareupdate = inner.add_parser("softwareupdate", parents=[common, leaf],
                                      help="list the available macOS updates, or install them")
    softwareupdate.add_argument("--list", action="store_true", dest="list_only",
                                help="list what is available (the default)")
    softwareupdate.add_argument("--scan", action="store_true",
                                help="re-scan for updates instead of reporting the last scan")
    softwareupdate.add_argument("--install", nargs="*", metavar="LABEL",
                                help="install updates: named labels, or the recommended set when "
                                     "none are given. Changes the OS and may reboot the Mac.")
    softwareupdate.add_argument("--restart", action="store_true",
                                help="allow a reboot after installing if one is required")
    softwareupdate.set_defaults(func=cmd_softwareupdate)

    call = inner.add_parser("call", parents=[common, leaf],
                            help="call any tool in the catalogue by name — the escape hatch")
    call.add_argument("tool", help="the tool name, e.g. get_volume")
    call.add_argument("--arg", action="append", metavar="KEY=VALUE",
                      help="one parameter (repeatable); the value is decoded as JSON where it looks "
                           "like JSON")
    call.add_argument("--args-json", dest="args_json", metavar="JSON",
                      help="the whole parameter object as JSON")
    call.set_defaults(func=cmd_call)

    consent = inner.add_parser("consent", parents=[common, leaf],
                               help="see, give or withdraw a standing approval for one tool")
    consent_inner = consent.add_subparsers(dest="consent_command", required=True)
    consent_list = consent_inner.add_parser("list", parents=[common, leaf],
                                            help="where this holder stands with each approval")
    consent_list.set_defaults(func=cmd_consent)
    consent_grant = consent_inner.add_parser("grant", parents=[common, leaf],
                                             help="approve one state-changing tool, once, for one "
                                                  "holder")
    consent_grant.add_argument("--tool", required=True, help="the tool being approved")
    consent_grant.add_argument("--note", help="why, recorded in the ledger with the approval")
    consent_grant.set_defaults(func=cmd_consent)
    consent_revoke = consent_inner.add_parser("revoke", parents=[common, leaf],
                                              help="withdraw an approval, so the next call is refused "
                                                   "again")
    consent_revoke.add_argument("--tool", required=True, help="the tool being withdrawn")
    consent_revoke.add_argument("--note", help="why it was withdrawn")
    consent_revoke.set_defaults(func=cmd_consent)

    enable = inner.add_parser(
        "enable", parents=[common],
        help="write the machine-access switches into credentials.json — what actually turns this on",
        description=(
            "`enabled` makes the machine tools *offerable* and grants nothing by itself; an agent "
            "still needs a `system:*` capability. The allowlists are what make a grant a scope rather "
            "than a boolean. Setting `system.enabled` without touching the allowlists is therefore the "
            "normal, least-privilege move.\n\n"
            "With no switch at all this writes nothing: it prints the current state and the commands "
            "that change it, and exits 2 (a usage error) so a script cannot read \"asked a question\" "
            "as \"wrote a switch\"."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    enable.add_argument("--on", action="store_true", dest="switch_on",
                        help="turn machine access on")
    enable.add_argument("--off", action="store_true", dest="switch_off",
                        help="turn it off; every system tool stops being offered")
    enable.add_argument("--full-access", action="store_true", dest="full_on",
                        help="lift the two allowlists and the per-action approval — permission to "
                             "act, never permission to stop being recorded")
    enable.add_argument("--no-full-access", action="store_true", dest="full_off",
                        help="restore the allowlists and the per-action approval")
    enable.add_argument("--allow-apps", action="append", metavar="NAME",
                        help="an application `open` may launch (repeatable; replaces the stored list)")
    enable.add_argument("--allow-automation", action="append", metavar="APP",
                        help="an AppleScript target prefix `automation` may drive (repeatable)")
    enable.add_argument("--allow-shortcuts", action="append", metavar="NAME",
                        help="a Shortcut `shortcuts` may run (repeatable)")
    enable.set_defaults(func=cmd_enable)
    return system


def cmd_enable(args: argparse.Namespace) -> int:
    """Write the `[system]` block — the switch a person needs and that nothing else provided.

    Separate from `consent grant` on purpose, because the two answer different questions and only this
    one was missing. `system.enabled` says *may the engine act on this Mac at all*; a consent entry
    says *may this one state-changing call proceed*. Without a writer for the first, every capability
    refused with "the machine tools are off" no matter what was approved — the person had granted
    permission and no command could record it.

    Called with no switch it is a *question*, not a write: it reports the state and exits `EXIT_USAGE`
    (see the comment on that branch). Every hint it prints is derived from the state it just reported,
    because the fixed hint it used to print said "to turn it on" on a machine that was already on.
    """
    from .config import ConfigError, load, set_system
    from .syscap import console_payload

    console, code = _console(args, tolerate_broken_config=True)
    if console is None:
        return code
    path = getattr(console.config, "path", None)
    if not path:
        _warn("this engine was started without a credentials file, so there is nowhere to record "
              "this. Pass --config, or run with one of the usual discovery locations.")
        return EXIT_CHECK_FAILED

    update: dict[str, Any] = {}
    if getattr(args, "switch_on", False) or getattr(args, "switch_off", False):
        # Two flags rather than one boolean so "leave it alone" is expressible: `--full-access` alone
        # must not flip `enabled`, and a person fixing one switch should not silently change the other.
        update["enabled"] = bool(args.switch_on) and not args.switch_off
    if getattr(args, "full_on", False) or getattr(args, "full_off", False):
        update["allow_full_access"] = bool(args.full_on) and not args.full_off
    for flag, key in (("allow_apps", "allow_apps"),
                      ("allow_automation", "allow_automation"),
                      ("allow_shortcuts", "allow_shortcuts")):
        values = getattr(args, flag, None)
        if values is not None:
            update[key] = values
    if not update:
        # Nothing to write means the person asked a question rather than made a change — answer it with
        # the current state and the commands that change it, instead of writing a no-op.
        #
        # It exits `EXIT_USAGE`, and that is the honest code: the invocation was incomplete, exactly as
        # `system power` with no `--awake/--sleep` and `system notify` with neither `--say` nor
        # `--message` are, and a script must be able to tell "asked a question" from "wrote a switch".
        # Exiting 0 here made `system enable --on && echo flipped` print `flipped` on a machine where
        # nothing was flipped; the `--json` payload's missing `wrote` key was the only signal, and a
        # missing key is not a signal a shell can read.
        section = _system_section(console)
        step = system_next(section)
        payload = console_payload(section)
        payload["next"] = step
        payload["file"] = str(path)
        # The commands that change what was just reported, minus the one already given as `next` — a
        # block that repeats the line above it reads as two different suggestions. Guarded rather than
        # indexed blind: this list is derived, and a derived list is exactly the one that ends up
        # empty the day someone changes `system_next`.
        others = [line for line in _mode_changes(section)
                  if not step.startswith(line.split(NEXT_SEP)[0])]
        block = ""
        if others:
            block = "\n".join([f"  {'also':20} : {others[0]}"]
                              + [f"  {'':20}   {line}" for line in others[1:]]) + "\n"
        _warn("nothing was written: no switch or allowlist was given, so there was nothing to change. "
              "The current state is on stdout; this exits 2 because the command did nothing.")
        _emit(payload, as_json=console.as_json,
              human="no switch or allowlist was given, so nothing was written. Current state:\n"
                    + _render_switches(section) + f"\n  {'file':20} : {path}\n"
                    + f"  {'next':20} : {step}\n" + block)
        return EXIT_USAGE

    try:
        set_system(path, system=update)
    except ConfigError as exc:
        _warn(f"{exc}")
        return EXIT_CHECK_FAILED

    # Re-read so the report is the state on disk rather than the state that was intended: a write that
    # the loader then refuses must not be reported as a success.
    fresh = load(path, warn=False)
    section = getattr(fresh, "system", None)
    step = system_next(section)
    payload = console_payload(section)
    payload["wrote"] = update
    payload["file"] = str(path)
    payload["next"] = step
    if console.as_json:
        _emit({"wrote": update, "file": str(path), "next": step, "system": payload},
              as_json=True, human="")
        return EXIT_OK

    print(f"written: {path}")
    for key in sorted(update):
        value = update[key]
        shown = ", ".join(value) if isinstance(value, list) else ("yes" if value else "no")
        print(f"  {key:20} = {shown or '(none)'}")
    print(_render_switches(section))
    if not bool(getattr(section, "enabled", False)):
        print("  note                 : machine access is still off, so every system tool stays "
              "unoffered")
    # State-derived, like every other closing line here: a person who has just written a switch is
    # told what that state makes possible, not what the old state needed.
    print(f"  {'next':20} : {step}")
    return EXIT_OK


def _system_section(console: Console) -> Any:
    """This run's `[system]` block, or `None` when the config has no such section yet."""
    return getattr(console.config, "system", None)


def _render_switches(section: Any) -> str:
    """The two switches and the three allowlists, in one shape shared by `enable` and its no-op path.

    Shared because the "nothing to change" answer and the post-write report must say the same things —
    a person comparing an intended change against the result would otherwise be reading two formats.
    The allowlist lines go through `allowlist_phrase` for the same reason: the mode decides what an
    empty list means, and this was the place that said "this grant reaches nothing" under a `full
    access : yes` line — the strongest form of the one lie a person could act on.
    """
    enabled = bool(getattr(section, "enabled", False))
    full = bool(getattr(section, "allow_full_access", False))
    lines = [f"  {'enabled':20} : {'yes' if enabled else 'no'}",
             f"  {'full access':20} : {'yes' if full else 'no'}"]
    for key, label in (("allow_apps", "allow_apps"),
                       ("allow_automation", "allow_automation"),
                       ("allow_shortcuts", "allow_shortcuts")):
        values = list(getattr(section, key, None) or [])
        lines.append(f"  {label:20} : {allowlist_phrase(values, full_access=full)}")
    return "\n".join(lines)


# ── entry point ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Run one `system` command. `python3 -m engine.systemcli list` — the word `system` is optional.

    A front door for the same tree `install` puts into `cli`, so this module is testable and usable
    without the file that wires it in. The leading `system` is **prepended** when the first token names
    one of this tree's own commands, which is what makes `engine.systemcli system state` (the shape
    `install` produces, and the shape `cli` passes through) and `engine.systemcli state` (the shape a
    person types at the module directly) both work.

    The command names are read off the *whole* tree rather than off the top level: this parser's top
    level answers `{"system"}`, so scanning only it would never match `list` and every shorthand would
    fall through to a usage error — a front door that only opens one way.
    """
    resolved = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if resolved and resolved[0] in _command_names(parser) and resolved[0] != "system":
        resolved = ["system", *resolved]
    args = parser.parse_args(resolved)
    for name, default in (("json", False), ("config", None)):
        if not hasattr(args, name):
            setattr(args, name, default)
    return int(args.func(args))


def _command_names(parser: argparse.ArgumentParser) -> set[str]:
    """Every subcommand name anywhere in the tree, read off argparse's own actions.

    Recursive because a two-level tree (`system consent grant`) has its names at two levels, and a
    scan that stopped at the first would recognise `list` but not `consent` — an inconsistency a
    person would meet as "some shorthands work and some do not".
    """
    names: set[str] = set()
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public accessor for this
        choices = getattr(action, "choices", None)
        if not isinstance(choices, dict):
            continue
        names.update(str(name) for name in choices)
        for parser_or_action in choices.values():
            if isinstance(parser_or_action, argparse.ArgumentParser):
                names |= _command_names(parser_or_action)
    return names


if __name__ == "__main__":
    raise SystemExit(main())
