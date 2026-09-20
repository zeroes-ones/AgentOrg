#!/usr/bin/env python3
"""onboarding.py — one definition of "ready to run", owned by the engine.

WHY THIS EXISTS
---------------
First-run setup was the app's, and only the app's. `Setup.swift` computed a `SetupGate` from six
inputs and the macOS wizard walked a person through it; the terminal had nothing at all, so a person
at a prompt was told to read `USAGE.md`. That is backwards — the CLI is the complete surface and the
window is a convenience over it — and it is the same class of drift `doctor_checks` was extracted to
prevent: two surfaces answering "is this ready?" separately will answer it differently, and the
terminal is the one that cannot show a button when they do.

So the *decision* lives here. The app and the CLI both read the answer from this module, and the
words travel with it: every blocking gate carries a `why` in plain language and a `next_step`, the
single command that resolves it — because a terminal has no buttons, and "not ready" without the one
thing to type is the scavenger hunt this replaces.

THE VOCABULARY IS THE APP'S, DELIBERATELY
-----------------------------------------
:class:`GateKind` carries the same six states, with the same raw strings, as the app's
`SetupGate.Kind`, and :func:`gate` takes the same six inputs as `SetupReadiness.gate` and evaluates
them in the same order. Neither is an accident: the app has a `switch` over those kinds and a rail
that ticks steps by their ids, so a renamed state here would break a surface this module cannot see.
The order is by *dependency*, not difficulty — there is no point offering a project choice while no
model resolves, because the project question can be answered at any time and the model one cannot.

WHAT IS *NOT* HERE
------------------
Nothing re-derives engine policy. The default pair comes from `Config.default_pair`, the providers
from `build_providers`, the window from the engine's own resolution (the catalogue first, the
declared table second — the same one `defaults` and `hire` use), and the posture from the
`[goal]` block as the file records it. A second implementation of any of those would disagree with
the engine, and a gate that disagrees with the engine is worse than no gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "GateKind", "SetupGate", "SetupReport", "Step", "STEP_ORDER",
    "gate", "readiness", "inspect", "discover_workspace", "remember_workspace", "remember_path",
    "journey", "journey_payload", "render_journey", "first_run_hint",
]

#: How the engine names itself in guidance. The same form `USAGE.md` and the app's own resolutions
#: use, so a line copied out of a terminal is a line that runs.
CLI = "python3 -m engine.cli"

#: The documented first move on a machine with no configuration at all. Also the engine's own
#: instruction in `ConfigError("no configuration found")`, quoted here so the CLI and the app say the
#: same thing rather than one of them inventing a path.
COPY_EXAMPLE = f"cp credentials.example.json credentials.json"

#: The two commands that can satisfy the model step, named where a person can find them again. The
#: placeholders are deliberate: the engine has nothing to substitute into them on a fresh machine, and
#: a real-looking endpoint invented here would be worse than an obvious blank.
ADD_ENDPOINT = (f"{CLI} providers add PROVIDER --kind openai "
                f"--base-url https://host/v1 --key-env PROVIDER_API_KEY")
PICK_MODEL = f"{CLI} defaults set --provider PROVIDER --model MODEL"


class GateKind(str, Enum):
    """The first thing standing between the person and a run.

    The raw values are the app's own `SetupGate.Kind` strings, so a payload from this module decodes
    without a translation table — and so `switch gate.kind` in Swift keeps compiling.
    """

    #: Nothing can be asked because the engine cannot start from its configuration. The reason is the
    #: engine's own failure text when it has one, so the step shows the real cause.
    ENGINE_UNAVAILABLE = "engineUnavailable"
    #: No default provider and model pair resolves, so no agent can bind.
    NEEDS_MODEL = "needsModel"
    #: A default resolves but no context window is known for it — an agent cannot be bound to a model
    #: whose window is unknown, and hiring refuses exactly this.
    NEEDS_WINDOW = "needsWindow"
    #: The person has not confirmed which folder the agents should work in.
    NEEDS_PROJECT = "needsProject"
    #: The person has not chosen how much the goals they create may decide alone.
    NEEDS_AUTONOMY = "needsAutonomy"
    #: A run is possible.
    READY = "ready"


#: Every state, for a test that asserts each one is reachable and for a caller that wants the list.
GateKind.ALL = tuple(GateKind)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class SetupGate:
    """One evaluation of "can a run happen yet", with what is missing and what to type.

    Frozen because it is a *value*: a caller renders it, a payload carries it, and nothing legitimately
    mutates it half-way through a wizard step.

    Attributes
    ----------
    kind:
        Which of the six states this is.
    why:
        What is missing and why it matters, in the engine's own words where the engine has them.
    next_step:
        The single command that resolves this gate at a terminal. Empty only when the gate is ready —
        a resolution that is a placeholder is better than none, but an invented one is worse.
    provider / model:
        The pair the gate is about, when it is about one, so a caller can name it without re-resolving.
    """

    kind: GateKind
    why: str = ""
    next_step: str = ""
    provider: str = ""
    model: str = ""

    # ── the five blocking cases, named as the app names them ────────────────
    @classmethod
    def engine_unavailable(cls, reason: str = "", *, next_step: str = "") -> "SetupGate":
        return cls(GateKind.ENGINE_UNAVAILABLE,
                   why=reason or "The engine has not started yet, so there is nothing to ask it.",
                   next_step=next_step or COPY_EXAMPLE)

    @classmethod
    def needs_model(cls, why: str, *, next_step: str = ADD_ENDPOINT) -> "SetupGate":
        return cls(GateKind.NEEDS_MODEL, why=why, next_step=next_step)

    @classmethod
    def needs_window(cls, why: str, *, provider: str, model: str,
                     next_step: str = "") -> "SetupGate":
        command = next_step or (f"{CLI} defaults set --provider {provider} --model {model} "
                                f"--context-window 32768")
        return cls(GateKind.NEEDS_WINDOW, why=why, next_step=command,
                   provider=provider, model=model)

    @classmethod
    def needs_project(cls, why: str, *, next_step: str = "") -> "SetupGate":
        # The command *is* the answer to this step: naming a folder the engine can work in confirms it
        # and initialises `<folder>/.agent_state/` (see `cli.cmd_onboard`). It is `onboard` rather than
        # `run` so the person sees the step move, rather than a run starting as a side effect of
        # answering a setup question. The default folder is the one the person is standing in, which
        # is the answer to "which folder" in the commonest case — a path typed from memory is a path
        # that does not exist, and `Workspace.attach` refuses one for good reason.
        return cls(GateKind.NEEDS_PROJECT, why=why,
                   next_step=next_step or f"{CLI} onboard --project .")

    @classmethod
    def needs_autonomy(cls, why: str, *, next_step: str = "") -> "SetupGate":
        return cls(GateKind.NEEDS_AUTONOMY, why=why,
                   next_step=next_step or f"{CLI} defaults autonomy --posture unattended")

    # ── derived, so a caller never switches on the message ──────────────────

    @property
    def is_blocking(self) -> bool:
        return self.kind is not GateKind.READY

    @property
    def step(self) -> int:
        """The wizard step number, one-based and 3 when ready — the app's own numbering."""
        if self.kind in (GateKind.ENGINE_UNAVAILABLE, GateKind.NEEDS_MODEL, GateKind.NEEDS_WINDOW):
            return 1
        if self.kind is GateKind.NEEDS_PROJECT:
            return 2
        return 3

    @property
    def step_id(self) -> str | None:
        """Which journey step this gate sits on, by that step's own id.

        The id rather than the number, because the journey's *order* is a list and the number is
        derived from it — adding a step renumbers everything, and a hardcoded `== 2` in a view would
        then point at the wrong row. `engineUnavailable` and `ready` have no id: neither is a step a
        person answers, one because nothing can be asked yet and the other because everything is done.
        """
        if self.kind in (GateKind.NEEDS_MODEL, GateKind.NEEDS_WINDOW):
            return "model"
        if self.kind is GateKind.NEEDS_PROJECT:
            return "project"
        if self.kind is GateKind.NEEDS_AUTONOMY:
            return "autonomy"
        return None

    @property
    def title(self) -> str:
        """A short heading, shared with the app so one gate is never two differently-named things."""
        if self.kind is GateKind.ENGINE_UNAVAILABLE:
            return "Start the engine"
        if self.kind in (GateKind.NEEDS_MODEL, GateKind.NEEDS_WINDOW):
            # `needsModel` and `needsWindow` share a heading on purpose: they are one step of the
            # journey and the second is the first one's follow-up question. A person should not be
            # told they are on a different step because the window is missing.
            return "Choose a model"
        if self.kind is GateKind.NEEDS_PROJECT:
            return "Choose a project"
        if self.kind is GateKind.NEEDS_AUTONOMY:
            return "Choose how much it decides alone"
        return "Ready to run"

    @property
    def detail(self) -> str:
        """The sentence under the heading: what is missing and why it matters."""
        if self.kind is GateKind.READY:
            return ("A model resolves, a project is chosen, and a new goal will use the autonomy "
                    "you picked.")
        return self.why

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape. The app reads these keys by name, so they are a contract, not a dump."""
        return {
            "kind": self.kind.value,
            "title": self.title,
            "detail": self.detail,
            "why": self.why,
            "next_step": self.next_step,
            "step": self.step,
            "step_id": self.step_id,
            "blocking": self.is_blocking,
            "provider": self.provider,
            "model": self.model,
        }


# ── the pure evaluation ──────────────────────────────────────────────────────


def gate(engine_is_running: bool, engine_failure: str | None = None,
         defaults: dict[str, Any] | None = None, providers: Iterable[Any] = (),
         project_confirmed: bool = False, posture_chosen: bool = False, *,
         engine_next_step: str = "") -> SetupGate:
    """The first unmet precondition, or `ready`.

    The same six inputs and the same order as the app's `SetupReadiness.gate`, because the two must
    agree about which single thing to show a person:

    1. **Is the engine up?** Nothing can be asked before this, so it short-circuits every other
       question. The failure text is the engine's own, so the step sends the person to the real cause.
    2. **Does a model resolve?** "No provider at all" and "a provider but no default names it" read
       differently on purpose: they need different next actions, and collapsing them would send half
       the readers to the wrong control.
    3. **Is its window known?** An agent binds to a window, so an unknown one is a real gate.
    4. **Is a project confirmed?**
    5. **Is the posture chosen?**

    ``engine_next_step`` is the one place this deviates from the app's signature, and it is a
    parameter rather than a policy: only the caller knows whether the engine refused to start because
    there is no configuration file or because the one there is unusable, and those two have different
    fixes. The app passes nothing and gets the documented copy-the-example command.
    """
    return _evaluate(
        engine_is_running=engine_is_running, engine_failure=engine_failure, defaults=defaults,
        providers=providers, project_confirmed=project_confirmed, posture_chosen=posture_chosen,
        engine_next_step=engine_next_step, check_window=True)


def _evaluate(*, engine_is_running: bool, engine_failure: str | None,
              defaults: dict[str, Any] | None, providers: Iterable[Any],
              project_confirmed: bool, posture_chosen: bool,
              engine_next_step: str = "", no_provider_reason: str = "",
              workspace_path: str = "", check_window: bool = True) -> SetupGate:
    """The body of :func:`gate`, with the knobs a gatherer needs and a caller must not set.

    `check_window=False` skips the window question entirely. It is for a caller that *could not look*
    — a session's greeting must not open a socket before the prompt appears — and it is a distinct
    answer from "the window is missing": reporting an unasked question as blocked would nag about a
    model the catalogue may well resolve, which is the "report contradicts the behaviour" failure.
    The default is `True` so a caller that simply forgot to look still blocks on the safe side.
    """
    if not engine_is_running:
        return SetupGate.engine_unavailable(engine_failure or "", next_step=engine_next_step)

    block = defaults or {}
    provider = str(block.get("provider") or "")
    model = str(block.get("model") or "")
    reason = str(block.get("reason") or "")
    providers = list(providers or ())

    if not providers:
        why = ("No model can be reached because no provider is configured yet. Add an endpoint — an "
               "OpenAI-compatible host, Anthropic, or a local Ollama — and test it to fetch its "
               "models.")
        if no_provider_reason:
            # The engine's own refusal, which names the provider and what it was missing. Better than
            # a general sentence: the difference between "re-paste the key" and "add an endpoint" is
            # the difference between a fixed config and a wasted afternoon.
            why = f"{no_provider_reason.rstrip('.')}."
        return SetupGate.needs_model(why, next_step=ADD_ENDPOINT)

    if not provider or not model:
        return SetupGate.needs_model(
            reason or ("A provider is configured, but no default model is set, so no agent can be "
                       "hired onto one. Pick the model everyone should use."),
            next_step=_pick_model_command(provider, block.get("candidate")))

    window = block.get("context_window")
    if check_window and (window is None or int(window) <= 0):
        source = str(block.get("window_source") or "")
        note = f" (the engine looked: {source})" if source else ""
        command = ""
        if provider and model:
            # The window override is written for *this* pair, so the command names the pair and a
            # window. Left as nothing, the gate's own default does the same with a generic number.
            command = (f"{CLI} defaults set --provider {provider} --model {model} "
                       f"--context-window 32768")
        return SetupGate.needs_window(
            f"{provider}/{model} has no known context window{note}, and an agent cannot be bound to "
            "a model whose window is unknown. Test the provider so its model list is read, or choose "
            "a model the provider reports.",
            provider=provider, model=model, next_step=command)

    if not project_confirmed:
        # The command names the folder to confirm. With a workspace already in view it names *that*
        # one, so running the printed line answers the step rather than asking the person to retype a
        # path the engine already resolved.
        command = ""
        if workspace_path:
            command = f'{CLI} onboard --project "{workspace_path}"'
        return SetupGate.needs_project(
            "Choose the folder the agents should work in. A folder of your own is edited directly; a "
            "managed project is one the engine owns.", next_step=command)
    if not posture_chosen:
        return SetupGate.needs_autonomy(
            "Say how much a goal you set here may decide on its own. Unattended lets the engine "
            "release the gates it is authorised to release; Supervised waits for you at every one.")
    # The pair travels on the ready gate too, so a caller that reports "ready" can also say on what
    # without resolving the default a second time.
    return SetupGate(GateKind.READY, provider=provider, model=model)


# ── the engine's own answers, gathered ───────────────────────────────────────


def _pick_model_command(provider: str, candidate: str = "") -> str:
    """The command that names a default model, as concretely as the configuration allows.

    Three cases, in falling order of usefulness:

    - **A provider and a model the provider could offer** — a command a person can run as printed,
      replacing the placeholder with a name that is real for their endpoint.
    - **A provider but no candidate model** — the provider is real and only the model is a
      placeholder, because inventing a model id would produce a command that fails with "unknown
      model" and teaches the wrong lesson. The person looks at `models` for the list.
    - **No provider yet** — the endpoint comes first; a default naming a provider that does not exist
      is refused by `defaults set`, so leading with it would be leading with a command that must fail.
    """
    if not provider:
        return ADD_ENDPOINT
    return f"{CLI} defaults set --provider {provider} --model {candidate or 'MODEL'}"


def default_window(config: Any, providers: Any, catalog: Any = None) -> tuple[int | None, Any, str]:
    """The context window the engine will bind the default model with, and where it came from.

    Resolved exactly as `defaults` and a *hire* resolve it: an explicit `defaults.context_window`
    wins (it exists for the model the provider cannot report), then the live catalogue's probed value,
    then the declared table. Reading only the declared table would report `window: UNKNOWN` for a
    model the engine probes and then bind it happily — the report contradicting the behaviour, which
    is the failure this ordering exists to prevent.

    With no ``catalog`` the CLI's own resolver is used rather than a second copy of the rule, so
    `onboard` prints the number `defaults` prints. A caller that already holds a shared catalogue
    (the console, whose poll must not build a new one) passes it and gets the same rule against that.
    """
    provider, model, _ = config.default_pair()
    if not provider or not model:
        return None, None, ""
    spec = config.default_model_spec()
    if spec.context_window and spec.model_id == model:
        return int(spec.context_window), spec.max_output, spec.source
    if catalog is None:
        from .cli import _resolve_default_window

        return _resolve_default_window(config, providers)
    try:
        entry = catalog.resolve(provider, model)
        if entry is not None and entry.window_known:
            return int(entry.context_window), entry.max_output, entry.source
    except Exception:  # noqa: BLE001 - a probe failure degrades to "unknown", never raises
        pass
    return None, None, ""


def posture_recorded(config: Any) -> bool:
    """Whether the file records a posture, as opposed to the engine having a default for it.

    The engine's own default is `unattended`, and the journey must still tell "the person has not
    answered" from "the person answered Unattended" — the first is a step it has to show and the
    second is not. So this reads the *document*, not the validated config, which fills the default in.
    """
    goal = getattr(config, "raw", None)
    if not isinstance(goal, dict):
        return False
    block = goal.get("goal")
    if not isinstance(block, dict):
        return False
    return bool(str(block.get("default_posture") or "").strip())


def readiness(config: Any = None, providers: Any = None, workspace: Any = None, *,
              config_error: str = "", config_path: Any = None, build_failure: str = "",
              probe: bool = True, check_window: bool | None = None,
              catalog: Any = None) -> SetupGate:
    """Evaluate the gate from the engine's own state — the one gatherer both surfaces call.

    Every input is something the engine already reports; nothing here guesses at a file or
    re-derives a policy. `config=None` means the engine could not load its configuration, and the
    reason travels into the gate because the engine's own failure text is the useful part.

    Two separate questions, deliberately not one flag:

    - **`probe`** — may this call consult a provider over the network? The catalogue's probed window
      is what makes a model bindable that the declared table has never heard of, so a caller that
      skips it is answering from the configuration alone.
    - **`check_window`** — should an unknown window block the gate? Defaults to `True`, because the
      safe direction is to block: an agent cannot bind to a model whose window is unknown, and a gate
      that said "ready" and then had hiring refuse is the exact failure this module exists to prevent.
      A CLI that skipped the lookup still blocks, and the step's own `--context-window` command is the
      fix that needs no probe at all. Only a caller that genuinely *cannot* know — the session's
      greeting, which must not open a socket before the prompt appears — passes `False`, and it is
      then the `onboard` command that answers the question exactly.
    """
    if config is None:
        return SetupGate.engine_unavailable(config_error, next_step=_engine_recovery(config_path))
    if check_window is None:
        check_window = True

    block: dict[str, Any] = {}
    try:
        provider, model, reason = config.default_pair()
        window, _, source = default_window(config, providers, catalog) if probe else (None, None, "")
        if not probe:
            spec = config.default_model_spec()
            if spec.context_window and spec.model_id == model:
                window, source = int(spec.context_window), spec.source
        block = {"provider": provider, "model": model, "reason": reason,
                 "context_window": window, "window_source": source,
                 "candidate": _candidate_model(config, provider, model)}
    except Exception as exc:  # noqa: BLE001 - a broken config must degrade to a gate, not a traceback
        return SetupGate.engine_unavailable(
            f"the engine could not resolve its default model: {exc}",
            next_step=_engine_recovery(config_path))

    try:
        built = list(providers or ())
    except TypeError:
        built = []

    return _evaluate(
        engine_is_running=True, engine_failure=None, defaults=block, providers=built,
        project_confirmed=bool(workspace is not None and _exists(workspace)),
        posture_chosen=posture_recorded(config),
        no_provider_reason=build_failure,
        workspace_path=str(workspace.path) if workspace is not None else "",
        check_window=check_window)


def _exists(workspace: Any) -> bool:
    """Whether a workspace has been initialised. An unreadable one is not a chosen one."""
    try:
        return bool(workspace.exists())
    except Exception:  # noqa: BLE001
        return False


def _candidate_model(config: Any, provider: str, model: str) -> str:
    """A model id this provider can actually offer, for a command a person can run as printed.

    Deliberately *not* an invention. The candidates are the ones the engine already attributes to the
    provider — its declared alias table, the catalogue entries probed against it, and the curated
    table's models for it — in the same attribution `Config.default_models_for` uses, so the command
    names something the provider is already known to serve. An empty answer keeps the placeholder,
    which is honest; a guessed id would produce a command that fails with "unknown model".

    Nothing here resolves a *window*, unlike `default_pair`'s own fallback: this is guidance for the
    step that is blocked *because* no model resolves, so requiring a known window would withhold the
    suggestion exactly when it is needed.
    """
    if not provider:
        return ""
    for candidate in sorted(config.default_models_for(provider)):
        if candidate and (not model or candidate != model):
            return candidate
    return ""


def _engine_recovery(config_path: Any) -> str:
    """The one command that gives the engine a configuration to start from.

    Named with the file when the caller told the engine which one to use, because a copy to the wrong
    path is a command that appears to work and changes nothing — and named bare otherwise, matching
    the instruction `README.md`, `USAGE.md` and the engine's own `ConfigError` all give.
    """
    if config_path:
        example = Path(__file__).resolve().parent.parent / "credentials.example.json"
        return f"cp {example} {config_path}"
    return COPY_EXAMPLE


def discover_workspace() -> Any:
    """The project a bare `onboard` should be standing in, or None.

    Discovery in a fixed order, so the answer is the same twice:

    1. **The folder the person confirmed last time**, recorded in the engine's own user root by
       :func:`remember_workspace`. This is the CLI's whole answer to a question the engine otherwise
       forgets: `onboard --project X` initialises `X/.agent_state/`, and without a record the *next*
       `onboard` would ask the same question again — guidance that does not stick is guidance that
       gets ignored.
    2. **The current directory, when it is already an engine project** (`./.agentorg` or
       `./.agent_state`), so a person standing in the folder does not have to name it.
    3. **A managed project under the default `projects/` root** when exactly one is initialised.

    More than one candidate under the managed root is *not* guessed at: naming the wrong project is
    worse than asking, and the step's own command is how a person answers it. A caller that named
    `--project`/`--slug`/`--root` never reaches here.
    """
    from .state import Workspace

    remembered = _remembered_path()
    if remembered is not None:
        workspace = _attached_or_none(remembered)
        if workspace is not None:
            return workspace

    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / ".agent_state").is_dir() or (candidate / ".agentorg").is_dir():
            workspace = _attached_or_none(candidate)
            if workspace is not None:
                return workspace
            break

    projects = Path(__file__).resolve().parent.parent / "projects"
    try:
        initialised = [Workspace.for_project(slug, root=projects)
                       for slug in Workspace.list_projects(projects)]
        initialised = [workspace for workspace in initialised if workspace.exists()]
    except Exception:  # noqa: BLE001
        return None
    return initialised[0] if len(initialised) == 1 else None


#: Where the engine records which folder the person confirmed. Under the *user* root rather than in
#: the project, because the whole point is to find the project — and beside the roster and custom
#: skills `usercfg` already keeps there, so it is personal state in a directory that is personal.
REMEMBERED_FILE = "onboard.json"


def remember_path() -> Path:
    """The file that remembers the confirmed project, in the engine's own user root."""
    from . import usercfg

    return usercfg.global_root() / REMEMBERED_FILE


def _remembered_path() -> Path | None:
    try:
        target = remember_path()
        if not target.is_file():
            return None
        import json as _json

        data = _json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable record is no record, never a failure
        return None
    raw = str((data or {}).get("project") or "").strip()
    return Path(raw).expanduser() if raw else None


def remember_workspace(workspace: Any) -> None:
    """Record a confirmed folder, so the next `onboard` sees the step behind it.

    Best-effort: a machine where the user root cannot be written must still be able to onboard, so a
    failure here is swallowed rather than raised. The gate is evaluated from the *filesystem* either
    way — this only saves the person from naming the same folder twice.
    """
    try:
        import json as _json

        target = remember_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_json.dumps({"project": str(workspace.path)}, indent=2),
                          encoding="utf-8")
    except Exception:  # noqa: BLE001 - remembering is a convenience, not a precondition
        pass


def _attached_or_none(path: Path) -> Any:
    from .state import Workspace

    try:
        workspace = Workspace.attach(path)
    except Exception:  # noqa: BLE001
        return None
    return workspace if workspace.exists() else None


#: The steps of the journey, in dependency order, with the ids the app's rail ticks by. Read off one
#: list so "the order" and "the steps" cannot be two answers.
STEP_ORDER: tuple[str, ...] = ("engine", "model", "project", "autonomy")


def inspect(config_path: Any = None, workspace: Any = None, *, probe: bool = True,
            catalog: Any = None, discover: bool = True) -> "SetupReport":
    """Load, build and evaluate — tolerantly, so a fresh machine gets a step rather than an error.

    Every failure here is a *gate*, never an exception: a missing credentials file, a file that will
    not parse, and a configuration whose providers all refuse to build are three of the states this
    command exists to describe, and a command that crashes on the state it exists to explain has no
    reason to exist. Nothing is written.

    `discover=True` finds the project a bare call should be standing in (see
    :func:`discover_workspace`), because the engine has no other way to remember that question once a
    run has initialised the folder. The *session* passes `discover=False`: its workspace is resolved
    by the commands that act on it, and a greeting must not pick a project on the person's behalf.
    """
    from .config import ConfigError, load
    from .providers.registry import build_providers

    if workspace is None and discover:
        workspace = discover_workspace()

    warnings: list[str] = []
    config = None
    failure = ""
    try:
        config = load(config_path)
        warnings.extend(config.raw.get("_warnings") or [])
    except ConfigError as exc:
        failure = str(exc)

    providers: dict[str, Any] = {}
    build_failure = ""
    if config is not None:
        try:
            providers, skipped = build_providers(config)
            warnings.extend(f"provider skipped: {entry}" for entry in skipped)
        except ConfigError as exc:
            build_failure = str(exc)
        except Exception as exc:  # noqa: BLE001 - a provider that explodes is a gate, not a crash
            build_failure = f"{type(exc).__name__}: {exc}"

    resolved = readiness(config, providers, workspace, config_error=failure,
                         config_path=config_path, build_failure=build_failure,
                         probe=probe, catalog=catalog)
    return SetupReport(gate=resolved, config=config, providers=providers, warnings=warnings,
                       config_path=str(config.path) if config is not None and config.path else "",
                       workspace=str(workspace.path) if workspace is not None else "")


@dataclass
class SetupReport:
    """What `inspect` found: the gate, what it loaded, and what it could not.

    `workspace` and `confirmed` are the same path when the caller named a project — the first is the
    folder the gate was evaluated against, the second is the folder this run *initialised* (see
    `cli.inspect_setup`), and they are separate fields so a session, which confirms nothing, can still
    report which folder was in view.
    """

    gate: SetupGate
    config: Any = None
    providers: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    config_path: str = ""
    workspace: str = ""
    confirmed: str = ""


# ── the journey ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Step:
    """One step of the path: what it is for, whether it is done, and what it unlocks.

    The keys are the ones the app's `SetupJourneyReport` decodes by name — `id`, `title`, `purpose`,
    `unlocks`, `satisfied`, `resolves` — because the app renders *these words* rather than its own
    copy, which is what makes the terminal and the window say one thing.
    """

    id: str
    title: str
    purpose: str
    unlocks: str
    satisfied: bool
    resolves: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "purpose": self.purpose,
            "unlocks": self.unlocks,
            "satisfied": self.satisfied,
            "resolves": self.resolves,
        }


#: The steps themselves: the one place the sequence and its words are written.
#:
#: `resolves` is the *typical* command for the step; :func:`journey` overrides the current step's with
#: the gate's own `next_step`, which is the situational one (it names the real provider, model and
#: path). Keeping a static fallback means a satisfied step still says what it took to satisfy it.
_STEPS: tuple[tuple[str, str, str, str, str], ...] = (
    ("engine",
     "The engine is running",
     "The engine is the part that actually talks to a model and does the work. Nothing else can "
     "happen until it can start from a configuration.",
     "Everything else — every step below is answered by the engine, so this one is first and "
     "unskippable.",
     COPY_EXAMPLE),
    ("model",
     "A model answers",
     "A provider is one API you can already reach — an OpenAI-compatible host, Anthropic, or a "
     "local Ollama — and the model is the specific one the agents use. Pick an endpoint you already "
     "hold a key for; a local Ollama needs none, which is the one choice that works with no account "
     "anywhere.",
     "Hiring agents. An agent is bound to a model, so with no model resolved the roster is empty "
     "and no work can start. You know it worked when the engine reaches the endpoint and reads its "
     "model list — press Test and a green result means it did. When it fails, the reason is the "
     "engine's own: a missing key names the variable to set, and a URL that was the full endpoint "
     "rather than the base is corrected and you are told.",
     PICK_MODEL),
    ("project",
     "A project to work in",
     "The folder the agents read and edit. It is either one of your own — edited directly — or a "
     "folder the engine creates and keeps to itself.",
     "Anything that touches a file, and every record of what happened: the roster, the run history "
     "and the cache all live inside the project.",
     f"{CLI} onboard --project ."),
    ("autonomy",
     "How much it decides alone",
     "Unattended lets a goal release the gates the engine is authorised to release and finish "
     "without you. Supervised waits for you at every gate.",
     "Setting a goal. From here the org plans the work, hires what it needs, runs it, and reports "
     "back — with a record you can read afterwards either way.",
     f"{CLI} defaults autonomy --posture unattended"),
)


def _is_past(step_id: str, current: str | None, kind: GateKind) -> bool:
    """Whether a step is behind the person — the one place "is this done" is decided.

    `engineUnavailable` means nothing is, not even the first step: the question *after* it cannot be
    asked until it is answered. `ready` means everything is. Otherwise a step is done when it comes
    before the current one, which is why the comparison is by position in :data:`STEP_ORDER` and not
    by a number written into a caller.
    """
    if kind is GateKind.ENGINE_UNAVAILABLE:
        return False
    if kind is GateKind.READY:
        return True
    if current is None:
        return False
    try:
        return STEP_ORDER.index(step_id) < STEP_ORDER.index(current)
    except ValueError:
        return False


def journey(gate_state: SetupGate) -> list[Step]:
    """Every step of the path, in order, with its state and what it unlocks.

    Derived **from the gate** rather than re-deciding readiness: the gate stays the one authority on
    what is missing, and each step's `satisfied` is the single comparison `_is_past`. A checklist that
    says a step is finished while the gate still blocks on it is worse than no checklist — so a
    duplicated predicate here is not a convenience, it is a second answer waiting to disagree.

    The current step's `resolves` is replaced with the gate's own `next_step`: the static command names
    placeholders, and the gate names the real provider, model and folder.
    """
    current = gate_state.step_id
    steps: list[Step] = []
    for step_id, title, purpose, unlocks, resolves in _STEPS:
        satisfied = _is_past(step_id, current, gate_state.kind)
        if step_id == current and gate_state.next_step:
            resolves = gate_state.next_step
        steps.append(Step(id=step_id, title=title, purpose=purpose, unlocks=unlocks,
                          satisfied=satisfied, resolves=resolves))
    return steps


def journey_payload(gate_state: SetupGate) -> dict[str, Any]:
    """The journey as the app decodes it: a `steps` list and one `summary` sentence.

    Returned as an object rather than a bare array because the app accepts both and the object is the
    one that can grow — a schema version or a second sentence would otherwise have to become a second
    field somewhere the app is not looking.
    """
    steps = journey(gate_state)
    done = sum(1 for step in steps if step.satisfied)
    if not gate_state.is_blocking:
        summary = f"All {len(steps)} steps are done — a run is possible."
    else:
        summary = f"{done} of {len(steps)} step(s) done — next: {gate_state.title}."
    return {"summary": summary, "steps": [step.as_dict() for step in steps]}


def render_journey(gate_state: SetupGate) -> list[str]:
    """The journey as lines a terminal can print — the same lines the session's `/onboard` shows.

    One renderer, so the command and the session cannot describe the same path differently. A
    satisfied step is a title and nothing more: its purpose is no longer something to weigh, and four
    expanded steps is a wall of text in front of the one question that matters.
    """
    steps = journey(gate_state)
    current = gate_state.step_id
    lines = [gate_state.detail, ""]
    for number, step in enumerate(steps, start=1):
        if step.satisfied:
            lines.append(f"  {number}. ✓ {step.title}  (done)")
            continue
        mark = "→" if step.id == current else " "
        lines.append(f"  {number}. {mark} {step.title}")
        lines.append(f"       for    : {step.purpose}")
        lines.append(f"       unlocks: {step.unlocks}")
        lines.append(f"       next   : {step.resolves}")
    done = sum(1 for step in steps if step.satisfied)
    lines.append("")
    lines.append(f"{done} of {len(steps)} step(s) done.")
    if gate_state.is_blocking:
        lines.append(f"Next: {gate_state.next_step}")
        lines.append(f"Do it, then run `{CLI} onboard` again to see the step behind you.")
    return lines


def first_run_hint(gate_state: SetupGate) -> str:
    """One line for a session's opening: what is missing and the single command that fixes it.

    Deliberately one line, because it is printed before the person has asked anything and a paragraph
    at a prompt is a paragraph nobody reads. Empty when the gate is ready — a session that is fine
    must not greet a person with a warning about setup.
    """
    if not gate_state.is_blocking:
        return ""
    return f"Setup is not finished — {gate_state.title.lower()}: {gate_state.next_step}"
