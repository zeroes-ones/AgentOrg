#!/usr/bin/env python3
"""cli.py — the command-line surface, so every documented command actually runs.

WHY THIS EXISTS
---------------
Documentation that describes commands nobody can run is worse than no documentation. This module
is the real entry point for the parts of the engine that are built: diagnosing the environment,
inspecting skills and models, planning a workflow, and reviewing the organisation.

It is also the debugging tool. `doctor` answers "why will this not start" in one command, and
every subcommand reports what it checked rather than only a verdict — because the reason a check
failed is the useful part.

DESIGN
------
- **Exit codes are meaningful**: 0 success, 1 a check failed, 2 a usage error. That makes the CLI
  usable in a script and in CI.
- **`--json` on every read command**, so the same information can be consumed by a tool.
- **Nothing destructive without confirmation.** Read-only commands dominate; the ones that write
  say what they will write first.
- **stdout is for the answer, stderr for diagnostics**, matching the engine's protocol discipline.

Usage:
    python3 -m engine.cli doctor
    python3 -m engine.cli skills list
    python3 -m engine.cli skills show code-reviewer
    python3 -m engine.cli models
    python3 -m engine.cli plan --goal "build a booking API" --out plan.yaml
    python3 -m engine.cli org
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .bus import EventBus
from .catalog import ModelCatalog
from .completion import SUPPORTED_SHELLS
from .config import SUPPORTED_KINDS, ConfigError, GoalConfig, SystemConfig, load, scan_for_leaks
from .library import LibraryError, resolve
from .org import Binder, HiringDesk, PolicyResolver, Router, default_company
from .org.binding import BindingError
from .org.router import RouteContext, RouterError
from .orchestrator import Orchestrator, OrchestratorError, RunPhase
from .planner import PlanError, Planner, emit_safe_yaml
from .state import Workspace
from .providers.registry import build_providers
from .resources import derive_ceiling, detect
from .schedules import DEFAULT_POSTURE, DEFAULT_TICK_S, MAX_TICK_S, MIN_TICK_S
from .skills import FilesystemSkillSource, SkillError
from .skills.bundle import Tier

#: Exit codes, documented so a script can branch on them.
EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_USAGE = 2


# ── shared helpers ───────────────────────────────────────────────────────────


def _emit(payload: dict[str, Any], *, as_json: bool, human: str) -> None:
    """Print either machine or human output, never both."""
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(human)


def _warn(message: str) -> None:
    """Write a diagnostic to stderr, keeping stdout for the answer."""
    print(message, file=sys.stderr)


def _load_stack(args: argparse.Namespace) -> tuple[Any, FilesystemSkillSource, dict[str, Any], list[str]]:
    """Load config, library, providers and skills — the shared prologue.

    Raises SystemExit with EXIT_CHECK_FAILED on a fatal problem, so every subcommand gets the same
    behaviour: a failure names what is wrong rather than surfacing a traceback.
    """
    try:
        config = load(getattr(args, "config", None))
    except ConfigError as exc:
        _warn(f"configuration error: {exc}")
        raise SystemExit(EXIT_CHECK_FAILED) from exc

    warnings = list(config.raw.get("_warnings") or [])
    for warning in warnings:
        _warn(f"warning: {warning}")

    try:
        library = resolve(getattr(args, "library", None),
                          pin_path=getattr(args, "library_pin", None))
    except LibraryError as exc:
        _warn(f"library error: {exc}")
        raise SystemExit(EXIT_CHECK_FAILED) from exc

    providers, skipped = build_providers(config)
    for entry in skipped:
        _warn(f"provider skipped: {entry}")

    source = _skill_source_for(library, getattr(args, "root", None))
    return config, source, providers, skipped


def _skill_source_for(library: Any, root: Any = None) -> Any:
    """The skill source: the pinned library with the Owner's own skills layered over it.

    Layered rather than replaced, so a custom skill is additive and the pinned corpus keeps its
    guarantees. A project that has authored nothing behaves exactly as before.
    """
    from .skills.overlay import OverlaySkillSource

    return OverlaySkillSource(FilesystemSkillSource(library), project=root)


# ── doctor ───────────────────────────────────────────────────────────────────


def cmd_completion(args: argparse.Namespace) -> int:
    """Print a shell completion script for this command tree.

    The script is the *only* thing on stdout — a banner or a progress line would corrupt
    `engine.cli completion bash > file`, which is the single way this command is meant to be used.
    A refusal (an unsupported shell) goes to stderr and exits as a usage error.
    """
    from .completion import script_for

    try:
        # `build_parser` is called here rather than taking the namespace's own parser, because the
        # script must describe the tree as it exists at this moment; a cached script is exactly the
        # staleness this module exists to avoid.
        script = script_for(str(args.shell), build_parser(),
                            program=getattr(args, "program", "engine.cli"))
    except ValueError as exc:
        _warn(str(exc))
        return EXIT_USAGE
    print(script, end="")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check every precondition and report what was verified.

    This is the first command to run when something is wrong, and the reason it reports each check
    individually is that "it does not work" is not a diagnosis.

    A thin renderer over :func:`doctor_checks`, so the chat's `/doctor` and this command cannot
    disagree about what a healthy engine is.
    """
    try:
        checks = doctor_checks(getattr(args, "config", None), getattr(args, "library", None),
                               getattr(args, "library_pin", None))
    except ConfigError as exc:
        # A failure is a diagnostic, so it goes to stderr with the rest of them. stdout stays
        # reserved for the answer, which is what makes `--json | tool` reliable.
        _warn(f"FAIL configuration\n  {exc}")
        if args.json:
            print(json.dumps({"checks": [{"check": "configuration", "ok": False, "detail": str(exc)}],
                              "failures": 1}, indent=2, sort_keys=True))
        return EXIT_CHECK_FAILED

    # A configuration that will not load stops the run of checks at the first one, and it is reported
    # the old way: as a diagnostic on stderr, because nothing else can be checked without it. Detected
    # from the shape `doctor_checks` returns rather than from a second exception, so the chat's
    # `/doctor` sees the same failed check as a value instead of an abort.
    if len(checks) == 1 and checks[0]["check"] == "configuration" and not checks[0]["ok"]:
        _warn(f"FAIL configuration\n  {checks[0]['detail']}")
        if args.json:
            print(json.dumps({"checks": checks, "failures": 1}, indent=2, sort_keys=True))
        return EXIT_CHECK_FAILED

    failures = sum(1 for c in checks if not c["ok"])
    if args.json:
        print(json.dumps({"checks": checks, "failures": failures}, indent=2, sort_keys=True))
    else:
        width = max(len(c["check"]) for c in checks)
        for check in checks:
            mark = "OK  " if check["ok"] else "FAIL"
            print(f"{mark} {check['check']:<{width}}  {check['detail']}")
            for warning in check.get("warnings") or []:
                print(f"     warning: {warning}")
            for entry in check.get("skipped") or []:
                print(f"     skipped: {entry}")
        print()
        print("doctor: all checks passed" if not failures
              else f"doctor: {failures} check(s) failed")
    return EXIT_OK if not failures else EXIT_CHECK_FAILED


def doctor_checks(config_path: Any = None, library_path: Any = None,
                  library_pin: Any = None) -> list[dict[str, Any]]:
    """Every precondition, as a list of `{check, ok, detail, …}` — the one implementation of `doctor`.

    Extracted from `cmd_doctor` for a concrete reason: the chat's `/doctor` must run the *same*
    checks. Two copies of "what a healthy engine is" is a second thing to keep correct, and the first
    time they drifted the session would bless an environment the command refuses.

    `config_path` is honoured as the loader honours an explicit path — exclusively, with no fallback —
    so a check run against one file cannot silently report on another. A missing configuration is
    returned as a single failed check rather than raised: the caller decides whether that is fatal
    (the CLI command exits 1) or merely the first thing to report (the chat keeps its session).
    """
    checks: list[dict[str, Any]] = []

    # 1. Configuration.
    try:
        config = load(config_path)
        checks.append({
            "check": "configuration",
            "ok": True,
            "detail": f"{config.path} with {len(config.providers)} providers",
            "warnings": list(config.raw.get("_warnings") or []),
        })
    except ConfigError as exc:
        checks.append({"check": "configuration", "ok": False, "detail": str(exc)})
        return checks

    # 2. Library. The detail carries the two integrity facts separately rather than one word
    # "verified", because a checkout with no pin has checked its runner's capabilities and nothing
    # else — and an operator reading "verified" would not know that.
    try:
        library = resolve(library_path, pin_path=library_pin)
        checks.append({
            "check": "skills library",
            "ok": True,
            "detail": (f"{library.files.root} at commit {str(library.commit or '')[:12]} — "
                       f"{library.verification_summary()}"),
            **library.verification_report(),
        })
    except LibraryError as exc:
        checks.append({"check": "skills library", "ok": False, "detail": str(exc)})
        library = None

    # 3. Skills actually parse into enforceable bundles.
    if library is not None:
        source = FilesystemSkillSource(library)
        names = source.names()
        try:
            bundles = 0
            for name in names:
                source.load(name)
                bundles += 1
            checks.append({"check": "skill bundles", "ok": True,
                           "detail": f"{bundles} skills parsed with criteria and checklists"})
        except SkillError as exc:
            checks.append({"check": "skill bundles", "ok": False, "detail": str(exc)})

    # 4. Providers.
    providers, skipped = build_providers(config)
    checks.append({
        "check": "providers",
        "ok": bool(providers),
        "detail": f"built {sorted(providers)}" + (f"; skipped {len(skipped)}" if skipped else ""),
        "skipped": skipped,
    })

    # 5. Machine capacity and the derived ceiling.
    caps = detect()
    derivation = derive_ceiling(caps, local_models_in_use=0)
    checks.append({
        "check": "machine",
        "ok": True,
        "detail": (
            f"{caps.cpu_count} cpus, {caps.memory_gb} GB, "
            f"ceiling {derivation['ceiling']} ({derivation['reason']})"
        ),
    })

    # 6. Model metadata: an agent can only bind to a model with a known context window.
    known_windows = len(config.known_models)
    checks.append({
        "check": "model catalog",
        "ok": known_windows > 0,
        "detail": (
            f"{known_windows} models with declared windows; "
            "models without a known window cannot be bound to an agent"
        ),
    })

    # 7. Secret hygiene.
    try:
        state_dir = Path.cwd() / ".agent_state"
        leaks = scan_for_leaks(state_dir) if state_dir.exists() else []
        checks.append({
            "check": "secret hygiene",
            "ok": not leaks,
            "detail": (f"{len(leaks)} key-shaped strings found under {state_dir}" if leaks
                       else "no key material found in the run-state directory"),
        })
    except Exception as exc:  # noqa: BLE001 - a scan failure must not crash doctor
        checks.append({"check": "secret hygiene", "ok": False, "detail": str(exc)})

    # 8. The machine posture. The one section a person reaches for `doctor` *about* was the one
    # section it could not see: this command never read `[system]`, while `systemcli._blocked`
    # refuses by naming `engine.cli doctor` as the remedy. A full-access machine therefore printed
    # "all checks passed" and the tool a person trusts most said nothing about the switch that
    # decides whether the engine can act on their Mac at all.
    checks.append(_system_access(config))

    return checks


def _system_access(config: Any) -> dict[str, Any]:
    """The `[system]` block as a check: whether the machine tools are offered, and to how much.

    **The check is `ok` in every mode, and that is deliberate.** A wide-open machine is the
    operator's own decision rather than a fault, and a `doctor` that failed here would exit 1 on a
    correctly configured machine — which is how a check teaches its reader to ignore it. What a
    permissive posture gets instead is a *warning*, the channel this file already has for a fact
    worth noticing that is not a failure, and a detail line that leads with the mode in capitals so
    the `OK` never reads as "all clear".

    The allowlist wording comes from `systemcli.allowlist_phrase` — the same function `system list`
    prints — because the failure this exists to stop is two surfaces describing one mode two ways:
    an empty allowlist read as "nothing allowed" while `allow_full_access` had already stepped the
    allowlist check aside. The import is deferred because `cli` is what wires `systemcli` into the
    parser, and a module-scope import would make that a cycle.
    """
    from .systemcli import allowlist_phrase

    section = getattr(config, "system", None)
    enabled = bool(getattr(section, "enabled", False))
    full = bool(getattr(section, "allow_full_access", False))
    if not enabled:
        return {
            "check": "system access",
            "ok": True,
            "detail": "OFF — no machine tool is offered to any agent, whatever capability it holds. "
                      "Turn them on with: engine.cli system enable --on",
        }
    if full:
        return {
            "check": "system access",
            "ok": True,
            "detail": "ON with FULL ACCESS — the allowlists and the per-action approval are lifted",
            # The sentence a person needs and could not get anywhere else: what this mode means for
            # *them*, including the part the allowlist lines used to deny.
            "warnings": [
                "full access means any application may be launched, any AppleScript may run and any "
                "Shortcut may be pulled, whatever the allowlists say — and no approval is asked for. "
                "Any agent holding a system:* grant can drive this Mac. Turn it off with: "
                "engine.cli system enable --no-full-access",
            ],
        }
    lists = " | ".join(
        f"{key}: {allowlist_phrase(list(getattr(section, key, None) or []), full_access=False)}"
        for key in ("allow_apps", "allow_automation", "allow_shortcuts"))
    return {"check": "system access", "ok": True, "detail": f"ON, scoped — {lists}"}


# ── onboard ──────────────────────────────────────────────────────────────────


def cmd_onboard(args: argparse.Namespace) -> int:
    """Walk the first-run path: what is done, what is next, and the one command for it.

    **Why a command of its own rather than another line in `doctor`.** They answer different
    questions, and the difference is not cosmetic:

    - `doctor` asks *"is this environment healthy?"* and reports its independent checks — skills
      parse, the machine has capacity, no key material leaked into the run state. Several of them can
      fail at once and none of them is ordered against another, because the point is a complete
      picture of an environment that is already set up.
    - `onboard` asks *"can a run happen yet, and what is the single next thing?"* It reports **one**
      gate, because a list of five problems is not guidance — the whole failure this module exists to
      fix is that a person at a fresh prompt was handed documentation instead of the next move.

    Folding the second into the first would mean either an unordered list (which is the failure) or a
    doctor that suppresses its own checks (which loses the diagnosis). They share every input and the
    same loaded config; they are two readings of it.

    Exit code follows the gate: **0 when a run is possible, 1 when it is not**, so a bootstrap script
    can branch on readiness rather than parsing prose.

    **`--project` is the project step's resolution, not a filter.** Naming a folder the engine can
    work in *is* the answer to "which folder should the agents work in", so the command confirms it —
    creating `<folder>/.agent_state/` exactly as a run does, and nothing else (see
    `Workspace.ensure`). Without that, the guidance would name a command that leaves the gate exactly
    where it was, which is worse than no guidance: it teaches the person that running the command does
    not help. Naming an *existing engine-owned project* with `--slug`/`--root` counts the same way.
    """
    from .onboarding import GateKind, journey_payload, render_journey

    try:
        report = inspect_setup(args, confirm_project=True)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a first-run command must never traceback
        _warn(f"cannot evaluate setup: {type(exc).__name__}: {exc}")
        return EXIT_CHECK_FAILED

    for warning in report.warnings:
        _warn(f"warning: {warning}")
    # The engine's own reason for the first step, on the diagnostic stream. `inspect` degrades a
    # failure into a gate rather than raising, and the *why* is the useful part — a step that says
    # "the engine cannot start" without the reason sends the person to the wrong place, and this
    # module's whole argument is that the engine's own words beat a sentence invented here.
    if report.gate.kind is GateKind.ENGINE_UNAVAILABLE:
        _warn(report.gate.why)

    if args.json:
        payload = journey_payload(report.gate)
        payload["gate"] = report.gate.as_dict()
        payload["config_path"] = report.config_path
        payload["workspace"] = report.workspace
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        if report.confirmed:
            print(f"project   : {report.confirmed}  (this is the folder the agents will work in)")
            print()
        for line in render_journey(report.gate):
            print(line)
    return EXIT_OK if not report.gate.is_blocking else EXIT_CHECK_FAILED


def inspect_setup(args: argparse.Namespace, *, confirm_project: bool = False) -> Any:
    """Gather the engine's own state and evaluate the gate — the one path `onboard` and the chat share.

    Both callers go through `onboarding.inspect`, so the command and the session cannot disagree
    about what needs doing; the only difference is that the command probes (it is a deliberate request
    and has time for the network) while the session's greeting does not.

    `confirm_project=True` is the `onboard` command's own act: a folder named on the command line is
    the answer to the project step, so it is initialised rather than merely inspected. The session
    passes nothing, because opening a chat must not create a workspace the person never named.
    """
    from .onboarding import inspect, remember_workspace

    workspace = None
    if getattr(args, "project", None):
        from .state import Workspace

        try:
            workspace = Workspace.attach(args.project)
        except Exception as exc:  # noqa: BLE001 - a bad path is the user's to hear about, plainly
            _warn(f"cannot attach that project: {exc}")
            raise SystemExit(EXIT_CHECK_FAILED) from exc
    elif getattr(args, "root", None) or getattr(args, "slug", None):
        workspace = _resolve_workspace(args, getattr(args, "slug", None) or "onboard")

    confirmed = ""
    if confirm_project and workspace is not None:
        # `ensure()` and nothing more: an attached folder gets `.agent_state/` and no `docs/` or
        # `src/`, so confirming a folder never edits the person's tree beyond the state directory the
        # engine owns everywhere.
        workspace.ensure()
        # And remember it, so the next bare `onboard` finds the step behind it. Without this the
        # command would ask the same question on every run — guidance that does not stick is guidance
        # a person learns to ignore.
        remember_workspace(workspace)
        confirmed = str(workspace.path)
    report = inspect(getattr(args, "config", None), workspace,
                     probe=not getattr(args, "no_probe", False))
    report.confirmed = confirmed
    report.workspace = str(workspace.path) if workspace is not None else ""
    return report


# ── skills ───────────────────────────────────────────────────────────────────


def cmd_skills_list(args: argparse.Namespace) -> int:
    """List skills, with the counts that show whether a bundle is enforceable."""
    _, source, _, _ = _load_stack(args)
    entries: list[dict[str, Any]] = []
    for name in source.names():
        try:
            bundle = source.load(name)
        except SkillError as exc:
            entries.append({"name": name, "error": str(exc)})
            continue
        entries.append({
            "name": bundle.name,
            "version": bundle.version,
            "criteria": len(bundle.contract.criteria),
            "checklist": len(bundle.checklist),
            "inputs": list(bundle.contract.inputs),
            "outputs": list(bundle.contract.outputs),
            "evidence_required": bundle.contract.evidence_required,
            "escalate_to": list(bundle.contract.escalate_to),
            "content_hash": bundle.content_hash[:12],
        })
    if args.json:
        print(json.dumps({"count": len(entries), "skills": entries}, indent=2, sort_keys=True))
        return EXIT_OK
    print(f"{len(entries)} skills in {source.library_root}")
    print(f"{'name':34s} {'ver':8s} {'crit':>4s} {'chk':>4s} {'out':>4s}  outputs")
    for entry in entries:
        if "error" in entry:
            print(f"{entry['name']:34s} ERROR  {entry['error'][:40]}")
            continue
        print(f"{entry['name']:34s} {entry['version']:8s} {entry['criteria']:4d} "
              f"{entry['checklist']:4d} {len(entry['outputs']):4d}  {', '.join(entry['outputs'])}")
    return EXIT_OK


def cmd_skills_show(args: argparse.Namespace) -> int:
    """Show one skill's contract, checklist and research gate.

    This is the command to run when asking "what will this agent actually be held to".
    """
    _, source, _, _ = _load_stack(args)
    try:
        bundle = source.load(args.name)
    except SkillError as exc:
        _warn(f"skill error: {exc}")
        return EXIT_CHECK_FAILED

    payload = {
        "name": bundle.name, "version": bundle.version, "hash": bundle.content_hash,
        "token_budget": bundle.token_budget, "tags": list(bundle.tags),
        "contract": bundle.contract.as_dict(),
        "checklist": [i.as_dict() for i in bundle.checklist],
        "research_steps": list(bundle.research_steps),
        "anti_rationalization": list(bundle.anti_rationalization),
        "consumes_from": list(bundle.consumes_from), "feeds_into": list(bundle.feeds_into),
        "section_tiers": {tier.name: len(bundle.sections_for_tier(tier))
                          for tier in (Tier.ROUTE, Tier.CORE, Tier.DETAIL)},
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print(f"# {bundle.name} v{bundle.version}  ({bundle.content_hash[:12]})")
    print(f"budget: {bundle.token_budget} tokens   tags: {', '.join(bundle.tags)}")
    print()
    contract = bundle.contract
    print("## Contract")
    print(f"  inputs   : {', '.join(contract.inputs) or '(none)'}")
    print(f"  outputs  : {', '.join(contract.outputs) or '(none)'}")
    print(f"  evidence : {'required' if contract.evidence_required else 'optional'}")
    print(f"  escalate : {', '.join(contract.escalate_to) or '(none)'}"
          f"{'   [criteria from the Verification section]' if contract.criteria_from_fallback else ''}")
    print()
    print(f"## Completion criteria ({len(contract.criteria)})")
    for index, criterion in enumerate(contract.criteria, 1):
        print(f"  {index}. {criterion}")
    if bundle.checklist:
        print()
        print(f"## Checklist ({len(bundle.checklist)}) — the prompt requires every id")
        for item in bundle.checklist:
            print(f"  [{item.id}] {item.text[:96]}")
    if bundle.research_steps:
        print()
        print(f"## Research gate ({len(bundle.research_steps)})")
        for step in bundle.research_steps:
            print(f"  {step[:96]}")
    if bundle.anti_rationalization:
        print()
        print(f"## Rationalizations this role forbids ({len(bundle.anti_rationalization)})")
        for rule in bundle.anti_rationalization:
            print(f"  {rule[:96]}")
    return EXIT_OK


def cmd_skills_pin(args: argparse.Namespace) -> int:
    """Record the library's content pin, so later runs can prove it was not modified.

    This is the caller `build_manifest`/`write_manifest` never had: without it, the manifest
    machinery was a promise with no way to make or keep it, and no run could ever compare content
    against anything. It resolves with `verify=False` deliberately — the point is to *create* the
    baseline, and a tree that cannot be checked against the not-yet-written pin would make the first
    run on a fresh checkout impossible.
    """
    from .library import default_pin_path

    target = Path(args.out).expanduser() if args.out else default_pin_path()
    try:
        library = resolve(getattr(args, "library", None), verify=False)
    except LibraryError as exc:
        _warn(f"library error: {exc}")
        return EXIT_CHECK_FAILED
    try:
        written = library.write_manifest(target)
    except OSError as exc:
        _warn(f"cannot write the pin: {exc}")
        return EXIT_CHECK_FAILED

    payload = {"pin": str(written), "root": str(library.files.root), "commit": library.commit,
               "files": len(library.manifest)}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK
    print(f"recorded {payload['files']} file hashes from {payload['root']}")
    print(f"  commit: {library.commit or '(not a git checkout)'}")
    print(f"  pin:    {written}")
    print("  every later run compares against it, and a mismatch refuses to start;"
          " re-run this command deliberately after reviewing a library change")
    return EXIT_OK


def cmd_skills_graph(args: argparse.Namespace) -> int:
    """Read the library's own dependency graph — what depends on what.

    Every skill declares `chain: consumes_from/feeds_into`. This is the command that makes that graph
    usable: one skill's neighbourhood, a transitive closure, or a review of a *set* of skills as one
    plan (do they hang together, and what do several of them need that the set leaves out).
    """
    from .skills.graph import SkillGraph

    _, source, _, _ = _load_stack(args)
    graph = SkillGraph(source)
    limit = max(1, int(args.limit or 15))

    # --review: review a set of skills as one plan.
    if args.review:
        review = graph.plan_review(args.review, min_consensus=int(args.min_consensus or 2))
        if args.json:
            print(json.dumps(review, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        print(f"Plan review: {len(review['skills'])} skill(s)")
        coherence = review.get("coherence") or {}
        print()
        print("Coherence — how many of the plan's own skills the library relates to each:")
        for name, count in sorted(coherence.items(), key=lambda kv: (kv[1], kv[0])):
            flag = "  <-- unrelated to the rest" if count == 0 and len(review["skills"]) > 1 else ""
            print(f"  {name:34s} {count}{flag}")
        consensus = review.get("consensus_missing") or []
        if consensus:
            print()
            print(f"Consensus prerequisites — declared by >= {review['min_consensus']} of the plan's")
            print("skills but not in the plan (the gap a person usually misses):")
            for entry in consensus[:limit]:
                print(f"  {entry['skill']:34s} demanded by {entry['demanded_by']}")
        else:
            print()
            print("No consensus prerequisite is missing from this set.")
        return EXIT_OK

    # A transitive closure of one skill.
    if args.upstream or args.downstream:
        name = args.upstream or args.downstream
        direction = "upstream" if args.upstream else "downstream"
        names = graph.closure([name], direction=direction)
        payload = {"skill": name, "direction": direction, "count": len(names),
                   "skills": names}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        print(f"{name}: {len(names)} skill(s) transitively {direction}")
        for entry in names[:limit]:
            print(f"  {entry}")
        if len(names) > limit:
            print(f"  …and {len(names) - limit} more")
        return EXIT_OK

    # One skill's neighbourhood.
    if args.skill:
        payload = graph.explain(args.skill, limit=limit)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        print(f"{args.skill}")
        print(f"  upstream   ({payload['upstream_count']}): "
              f"{', '.join(payload['upstream'][:limit]) or '(none)'}")
        print(f"  downstream ({payload['downstream_count']}): "
              f"{', '.join(payload['downstream'][:limit]) or '(none)'}")
        return EXIT_OK

    # Default: the whole-graph summary.
    stats = graph.stats()
    if args.json:
        print(json.dumps(stats.as_dict(), indent=2, sort_keys=True, default=str))
        return EXIT_OK
    print("The library's chain: dependency graph")
    print(f"  skills        : {stats.skills}")
    print(f"  edges         : {stats.edges}  (consumes_from + feeds_into)")
    print(f"  with edges    : {stats.with_edges}")
    print(f"  orphans       : {stats.orphans}")
    print(f"  mutually tied : {stats.cycles}  (a producer and its reviewer reference each other)")
    print()
    print("Use --skill NAME for one skill, --upstream/--downstream NAME for a closure, or")
    print("--review A B C to review a set of skills as one plan.")
    return EXIT_OK


# ── models ───────────────────────────────────────────────────────────────────


def cmd_models(args: argparse.Namespace) -> int:
    """List models with their provenance, and flag the ones that cannot be bound.

    The `source` column is the point: `probed` means the provider reported it, `declared` means the
    config table, `assumed` means we do not know the context window — and an agent cannot be bound
    to an assumed one.
    """
    config, _, providers, _ = _load_stack(args)
    catalog = ModelCatalog(config, providers)
    entries = catalog.list_models(refresh=args.refresh)
    payload = {
        "models": [e.as_dict() for e in entries],
        "provider_status": catalog.status(),
        "bindable": sum(1 for e in entries if e.window_known),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print(f"{len(entries)} models ({payload['bindable']} bindable)")
    print(f"{'provider':12s} {'model':32s} {'window':>9s} {'out':>7s} {'loc':6s} source")
    for entry in entries:
        window = str(entry.context_window) if entry.window_known else "UNKNOWN"
        out = str(entry.max_output) if entry.max_output else "-"
        print(f"{entry.provider_id:12s} {entry.model_id:32s} {window:>9s} {out:>7s} "
              f"{entry.locality:6s} {entry.source}")
    print()
    print("provider discovery:")
    for provider_id, status in sorted(catalog.status().items()):
        detail = f"  {provider_id}: {status['status']} ({status['count']} models)"
        if status.get("error"):
            detail += f"  error: {status['error'][:60]}"
        print(detail)
    unknown = [e for e in entries if not e.window_known]
    if unknown:
        print()
        print(f"note: {len(unknown)} model(s) have an unknown context window and cannot be bound")
        print("      to an agent. Probe the provider, or declare context_window in the config.")
    return EXIT_OK


# ── plan ─────────────────────────────────────────────────────────────────────


def cmd_plan(args: argparse.Namespace) -> int:
    """Turn a goal into a validated workflow manifest.

    Writes the manifest only when asked, and always prints the summary first — the Owner approves a
    graph, so showing it is the point.
    """
    config, source, _, _ = _load_stack(args)
    # Pass the roster when one can be loaded, so the plan can say which capabilities nobody holds
    # *before* it is approved. A roster problem must not block planning — the planner runs without one.
    try:
        org = _roster_for(config, source.library, _project_root_for(args))
    except Exception:  # noqa: BLE001 - planning must work with no roster
        org = None
    try:
        plan = Planner(source, org=org).plan(args.goal, slug=args.slug,
                                             max_iterations=args.max_iterations)
    except PlanError as exc:
        _warn(f"planning failed: {exc}")
        return EXIT_CHECK_FAILED

    if args.json:
        print(json.dumps(plan.as_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(plan.summary())

    if args.out:
        target = Path(args.out)
        target.write_text(emit_safe_yaml(plan.manifest), encoding="utf-8")
        _warn(f"wrote {target}")

    if not plan.validation.valid:
        _warn(f"validation errors: {plan.validation.errors}")
        return EXIT_CHECK_FAILED
    return EXIT_OK


# ── org ──────────────────────────────────────────────────────────────────────


def cmd_org(args: argparse.Namespace) -> int:
    """Show a default company, its bindings against a plan, and its policy matrix.

    With `--goal` the roster is checked against what that goal's plan actually needs, which is where
    a staffing gap becomes visible before a run rather than during one.
    """
    config, source, providers, _ = _load_stack(args)
    catalog = ModelCatalog(config, providers)
    default_provider = config.defaults.get("provider") or (sorted(providers)[0] if providers else "")
    default_model = config.defaults.get("model") or ""

    if not default_provider or not default_model:
        _warn(
            "no default model configured: set defaults.provider and defaults.model in the config."
        )
        return EXIT_CHECK_FAILED

    # The context window must be real: an agent cannot be bound without one.
    default_entry = catalog.resolve(default_provider, default_model)
    context_window = default_entry.context_window if default_entry else None
    if not context_window:
        _warn(
            f"the default model {default_model!r} has an unknown context window, so no agent can "
            "be bound to it. Probe the provider (`python3 -m engine.cli models --refresh`) or "
            "declare context_window in the config."
        )
        return EXIT_CHECK_FAILED

    # Reviewers on a different model, so independence holds structurally rather than by request.
    # Preferring a model that actually differs matters: two providers serving the same model name
    # is not model independence, and the design promises the stronger boundary where the roster
    # allows it. Reachability is preferred within that: a reviewer on a live different model beats
    # one on a live identical model, and both beat one on a dead endpoint. The search therefore
    # tries reachable providers first and only falls back to an unreachable one when it is the sole
    # source of a distinct model — and says so rather than pretending the boundary is strong.
    # Refresh every provider first, because `status()` reads the per-provider cache and an unvisited
    # provider would otherwise look reachable by accident.
    catalog.refresh()

    def _is_reachable(pid: str) -> bool:
        status = str((catalog.status().get(pid) or {}).get("status", ""))
        return "down" not in status and "misconfigured" not in status

    reachable = {pid for pid in providers if _is_reachable(pid)}
    unreachable = {pid for pid in providers if not _is_reachable(pid)}

    reviewer_provider = ""
    reviewer_model = ""
    reviewer_window = None
    for pool in (reachable, unreachable):
        for candidate_provider in sorted(pool):
            if candidate_provider == default_provider:
                continue
            for entry in catalog.list_models(provider_id=candidate_provider, only_known_windows=True):
                if entry.model_id != default_model:
                    reviewer_provider, reviewer_model = candidate_provider, entry.model_id
                    reviewer_window = entry.context_window
                    break
            if reviewer_model:
                break
        if reviewer_model:
            if candidate_provider in unreachable:
                _warn(
                    f"reviewers are on a different model via {candidate_provider!r}, which is not "
                    "reachable right now; the independence boundary holds, but a run would need that "
                    "provider up."
                )
            break

    if not reviewer_model:
        # No genuinely different model is available: fall back to a different provider if one
        # exists, and say plainly that the boundary is weaker than it could be.
        for candidate_provider in sorted(reachable | unreachable):
            if candidate_provider == default_provider:
                continue
            aliases = config.providers[candidate_provider].model_aliases
            candidate_model = next(iter(aliases.values()), "")
            if candidate_model:
                entry = catalog.resolve(candidate_provider, candidate_model)
                if entry is not None and entry.window_known:
                    reviewer_provider, reviewer_model = candidate_provider, candidate_model
                    reviewer_window = entry.context_window
                    _warn(
                        f"no reviewer model distinct from {default_model!r} is available, so "
                        "reviewers share the builders' model; independence rests on context "
                        "lineage alone."
                    )
                    break

    org = default_company(
        provider=default_provider, model=default_model, context_window=context_window,
        reviewer_provider=reviewer_provider or None,
        reviewer_model=reviewer_model or None,
        reviewer_context_window=reviewer_window,
    )

    payload: dict[str, Any] = {
        "roster": org.roster_view(),
        "policy": PolicyResolver().effective(),
        "skills_present": org.skills_present(),
    }

    if args.goal:
        plan = Planner(source, org=org).plan(args.goal, slug=args.slug or "org-check")
        binder = Binder(org)
        payload["plan"] = {"name": plan.manifest["name"], "nodes": plan.node_ids(),
                           "validated": plan.validation.valid, "shape": plan.shape}
        payload["staffing_gaps"] = binder.staffing_gaps(plan.manifest)
        # The planner's own gaps carry the hire that closes each, so the Owner can act on them.
        payload["hire_suggestions"] = [dict(gap) for gap in plan.staffing]
        bindings = binder.plan_bindings(plan.manifest, skip_unstaffed=True)
        payload["bindings"] = {nid: b.as_dict() for nid, b in bindings.items()}
        payload["router_preview"] = Router(org).route(RouteContext(
            node_id="preview", skill=plan.nodes[0]["skill"] if plan.nodes else "",
        )).as_dict() if plan.nodes else {}

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print(org.summary())
    print()
    print("Policy (effective autonomy by route class):")
    print(PolicyResolver().summary())
    if args.goal:
        print()
        print(f"Plan: {payload['plan']['name']}  (validated: {payload['plan']['validated']}, "
              f"shape: {payload['plan'].get('shape', 'software')})")
        gaps = payload.get("staffing_gaps") or []
        if gaps:
            print()
            print("Staffing gaps — the plan needs capabilities the roster does not staff:")
            for gap in gaps:
                print(f"  {gap['node_id']:20s} {gap['skill']:22s} {gap['reason']}")
            print()
            for suggestion in payload.get("hire_suggestions") or []:
                print(f"  close {suggestion['skill']:26s} with: {suggestion['hire']}")
        print()
        print("Bindings:")
        for node_id, binding in (payload.get("bindings") or {}).items():
            names = [org.agents[a].name for a in binding["agents"] if a in org.agents]
            print(f"  {node_id:20s} -> {', '.join(names)}  [{binding['policy']}]")
    return EXIT_OK


# ── hiring ───────────────────────────────────────────────────────────────────


def cmd_delegation(args: argparse.Namespace) -> int:
    """Show the delegation rules and what a hire costs.

    Prints the six invariants and the tier thresholds, because these are the rules an agent (or a
    person reading a rejection) needs in order to understand a refusal.
    """
    config, _, _, _ = _load_stack(args)
    desk = HiringDesk(
        Org_free(), max_depth=int(config.delegation.max_depth),
        span_of_control=int(config.delegation.span_of_control),
        allow_ephemeral=bool(config.delegation.allow_ephemeral),
        budget_share_max=float(config.delegation.budget_share_max),
        approval_tiers=dict(config.delegation.approval_tiers or {}),
    )
    stats = desk.stats()
    payload = {"max_depth": stats["max_depth"], "span_of_control": stats["span_of_control"],
               "budget_share_max": stats["budget_share_max"],
               "allow_ephemeral": stats["allow_ephemeral"],
               "invariants": stats["invariants"],
               "approval_tiers": config.delegation.approval_tiers}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print("Delegation invariants (enforced in code, not requested in a prompt):")
    for key, text in sorted(stats["invariants"].items()):
        print(f"  {key}  {text}")
    print()
    print("Budget:")
    print(f"  a child may take at most {stats['budget_share_max']:.0%} of the parent's remainder")
    print(f"  run ceiling: ${config.budget.run_max_usd:.2f} / {config.budget.run_max_tokens} tokens")
    print()
    print("Approval tiers:")
    tiers = config.delegation.approval_tiers or {}
    for key in sorted(tiers):
        print(f"  {key}: {tiers[key]}")
    print("  T0/T1 auto-approve; T2/T3 reach the Owner with the full requisition.")
    print()
    print(f"Ephemeral helpers: {'allowed' if stats['allow_ephemeral'] else 'disabled'}")
    print(f"Max depth: {stats['max_depth']}   Span of control: {stats['span_of_control']}")
    return EXIT_OK


def _orchestrator(args: argparse.Namespace, slug: str):
    """Build an orchestrator over a project workspace.

    Kept in one place so every run command resolves the workspace identically — a command that used a
    different root would silently operate on a different project.
    """
    config, source, _, _ = _load_stack(args)
    try:
        library = resolve(getattr(args, "library", None),
                          pin_path=getattr(args, "library_pin", None))
    except LibraryError as exc:
        _warn(f"library error: {exc}")
        raise SystemExit(EXIT_CHECK_FAILED) from exc
    workspace = _resolve_workspace(args, slug)
    workspace.ensure()
    bus = EventBus(run_id=f"cli_{slug}", trace_path=workspace.trace_path,
                   lifecycle=config, lifecycle_slug=workspace.display_name)
    # The roster the Owner hired is the roster the run uses. Without this, `hire` would write a file
    # nothing reads — the agents would exist on disk and never appear in a run, which is the exact
    # "capability with no effect" failure this project refuses elsewhere.
    org = _roster_for(config, library, _project_root_for(args), args=args)
    return Orchestrator(config=config, library=library, workspace=workspace, bus=bus,
                        org=org), workspace


def _resolve_workspace(args: argparse.Namespace, slug: str):
    """Resolve the workspace a command is aimed at, from ``--org`` / ``--project`` / ``--root`` /
    ``--slug``.

    One resolver, because the ordering is a decision that must not be forgotten by one command out of
    twenty. Precedence is by specificity: an **org** names the project itself, a **folder** names the
    project itself, and a **root** names a directory *of* projects — so `--org` and `--project` both
    win over `--root`, and `--org` wins over `--project` only because it is the more explicit of the
    two ways to name the same thing.
    """
    from .state import Workspace

    org_path = _org_workspace_path(args)
    if org_path is not None:
        try:
            return Workspace.attach(org_path)
        except Exception as exc:  # noqa: BLE001 - a bad org folder is a usage error, said plainly
            _warn(f"cannot attach the org's folder: {exc}")
            raise SystemExit(EXIT_CHECK_FAILED) from exc
    project = getattr(args, "project", None)
    if project:
        try:
            return Workspace.attach(project)
        except Exception as exc:  # noqa: BLE001 - a bad path is a usage error, said plainly
            _warn(f"cannot attach that project: {exc}")
            raise SystemExit(EXIT_CHECK_FAILED) from exc
    return Workspace.for_project(slug, root=getattr(args, "root", None))


def _org_entry(args: argparse.Namespace) -> Any:
    """The portfolio org entry named by ``--org``, or None when the flag is absent.

    Raises SystemExit with a clear reason when the flag names an org the portfolio does not have, or
    when no portfolio exists at all — because "you asked for an org and there is no register" is a
    setup problem a person needs stated, not a silent fallback to the current folder.
    """
    from .portfolio import Portfolio, PortfolioError

    ref = getattr(args, "org", None)
    if not ref:
        return None
    try:
        portfolio = Portfolio.load()
    except PortfolioError as exc:
        _warn(f"cannot read the portfolio: {exc}")
        raise SystemExit(EXIT_CHECK_FAILED) from exc
    if portfolio is None:
        _warn("no portfolio exists; create one with `engine.cli portfolio add <name> --path <folder>`")
        raise SystemExit(EXIT_CHECK_FAILED)
    try:
        return portfolio.org(ref)
    except PortfolioError as exc:
        _warn(str(exc))
        raise SystemExit(EXIT_CHECK_FAILED) from exc


def _org_workspace_path(args: argparse.Namespace) -> Path | None:
    """The folder an ``--org`` names, or None.

    None means one of two real things: no ``--org`` was given at all, or the org named has no folder
    of its own and runs as a managed project under ``projects/<slug>``. The managed fallback is
    deliberately **not** applied here — it belongs in `_project_root_for`, where a root can be
    resolved; this function answers only "what folder did the entry name".
    """
    entry = _org_entry(args)
    if entry is None:
        return None
    return entry.workspace_path


def _slug_for(args: argparse.Namespace) -> str:
    """The project slug a command operates on.

    ``--slug`` is no longer required: ``--org`` names a registered org and ``--project`` names a
    folder, and either supplies its own name, so demanding a second, redundant identifier would be a
    flag that exists only to be ignored.
    """
    explicit = getattr(args, "slug", None)
    if explicit:
        return explicit
    entry = _org_entry(args)
    if entry is not None:
        org_path = entry.workspace_path
        if org_path is None:
            # A path-less org is a managed project keyed on its own slug, so return that. Before, this
            # fell through to the usage error below, reading `--org ideas` as "you named no project".
            return entry.slug
        from .state import Workspace

        return Workspace.attach(org_path).slug
    project = getattr(args, "project", None)
    if project:
        from .state import Workspace

        try:
            return Workspace.attach(project).slug
        except Exception as exc:  # noqa: BLE001 - same failure the resolver reports
            _warn(f"cannot attach that project: {exc}")
            raise SystemExit(EXIT_CHECK_FAILED) from exc
    _warn("give one of --slug <name>, --project <folder>, or --org <org>")
    raise SystemExit(EXIT_USAGE)


def _project_root_for(args: argparse.Namespace) -> Path | None:
    """The project root a command is aimed at, from `--org`, `--project` or `--root`.

    `--org` selects a registered org; `--project` names the project folder itself; `--root` names a
    directory *of* projects. All three mean "the roster and skills for this work live here", so all
    three must be honoured — otherwise a command run with `--project Ideas` silently ignored
    `Ideas/.agentorg/roster.json` and fell back to the built-in company, which is exactly the "the CEO
    I hired was not used" failure.

    An org resolves through the shared `workspace_for`, so a *managed* org — one with no folder of
    its own — reads its roster and skills from `projects/<slug>` rather than from the current
    directory. This is where the managed fallback belongs, because `entry.workspace_path` alone is
    `None` for a path-less org.
    """
    from . import usercfg

    entry = _org_entry(args)
    if entry is not None:
        from .portfolio import workspace_for
        from .state import StateError

        try:
            return workspace_for(entry, root=getattr(args, "root", None)).path
        except StateError:
            # A named folder that has gone: return the path it would use rather than raising, so a
            # roster load still falls back to the built-ins as it did before.
            return entry.workspace_path
    if getattr(args, "project", None):
        return Path(args.project)
    if getattr(args, "root", None):
        return usercfg.project_root(args.root)
    return None


def _roster_for(config: Any, library: Any, root: Any, *, args: Any = None) -> Any:
    """Load the effective roster, falling back to the built-in company.

    A roster problem must never block a run: the built-ins are a complete, runnable org, so a broken
    user roster is reported and then ignored rather than turning every command into an error.

    When the command named an ``--org``, the org's identity (id + principal) is applied to the roster,
    so the run records which org it belongs to rather than looking like an anonymous default company.
    """
    from .catalog import ModelCatalog
    from .people import HireError, People

    org_id = ""
    principal_id = ""
    org_name = ""
    if args is not None:
        entry = _org_entry(args)
        if entry is not None:
            org_id, org_name = entry.id, entry.name
            principal_id = _principal_id()
            if root is None:
                # A path-less org must read its managed project, never the process's working
                # directory — which for the console is the engine's own source tree and holds a
                # roster that is not this org's. Through the shared resolver, tolerantly: a named
                # folder that has gone falls back to its path rather than raising, because this
                # function's whole contract is that a roster problem never blocks a run.
                from .portfolio import workspace_for
                from .state import StateError

                try:
                    root = workspace_for(entry, root=getattr(args, "root", None)).path
                except StateError:
                    root = entry.workspace_path

    try:
        people = People(library=library, config=config, catalog=ModelCatalog(config, {}),
                     project=root or None)
        return people.load(project=root or None, org_id=org_id, principal_id=principal_id,
                           name=org_name)
    except HireError as exc:
        _warn(f"warning: ignoring the user roster: {exc}")
        return None


def _principal_id() -> str:
    """The principal's id from the portfolio, or empty when there is none.

    Tolerant on purpose: a command that named an org has a portfolio by definition (the org came from
    it), but the roster load must not fail if the register becomes unreadable mid-command.
    """
    from .portfolio import DEFAULT_PRINCIPAL_ID, Portfolio

    try:
        portfolio = Portfolio.load()
    except Exception:  # noqa: BLE001 - a broken register must not block a roster load
        return DEFAULT_PRINCIPAL_ID
    return portfolio.principal.id if portfolio is not None else DEFAULT_PRINCIPAL_ID


def _posture_policy(orch: Any, posture: str) -> Any:
    """The goal policy for a named posture, keeping every other setting the default already chose.

    Built by overriding the configured default rather than constructing from nothing, so
    `run --posture supervised` narrows the authority to "ask me" without also silently resetting
    auto-hire or persist-hires to something the config did not say.
    """
    from .goal import GoalPolicy, Posture

    base = orch._default_goal_policy()
    resolved = Posture(str(posture).strip().lower())
    return GoalPolicy(
        auto_approve=base.auto_approve if resolved is Posture.UNATTENDED else False,
        auto_hire=base.auto_hire,
        persist_hires=base.persist_hires,
        posture=resolved,
    )


def _console_for(args: argparse.Namespace, *, workspace: Any = None,
                 orchestrator: Any = None, slug: str | None = None,
                 needs_workspace: bool = True) -> Any:
    """A `serve.Server` over a workspace, for the operations the console already implements.

    Built rather than re-implemented. Adding a provider is a validation, a normalisation, a merge-write
    and a reload; a child's transcript is a byte-addressed page; a proposal list has a shape the panel
    reads by name. `serve` owns every one of those rules, and a second copy here would be a second
    definition — the two would drift apart and the CLI would accept what the app refuses.

    `needs_workspace=False` leaves the workspace unset, for a handler that only reads the configuration
    (`providers list`). Resolving a workspace there would *create* a project directory for a command
    that never looks in it, which is state a person did not ask for.

    Its events are swallowed: `Server.emit` writes the protocol to stdout, and stdout here belongs to
    the answer. That is what keeps `--json` parseable.
    """
    from .serve import Server

    if workspace is None and needs_workspace:
        workspace = _resolve_workspace(args, slug or _slug_for(args))
        workspace.ensure()
    config, _, _, _ = _load_stack(args)
    return Server(config=config,
                  library=resolve(getattr(args, "library", None),
                                  pin_path=getattr(args, "library_pin", None)),
                  workspace=workspace, orchestrator=orchestrator,
                  slug=slug or getattr(workspace, "slug", "console"),
                  stdout=_NullStream())


class _NullStream:
    """A write sink that discards everything, for a `Server` whose protocol stream nobody reads."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


def _approve_prepared_plan(args: argparse.Namespace) -> int:
    """Approve the plan already prepared on disk and execute it — the CLI's half of `approve_plan`.

    The **same engine calls the console's command makes** (`Orchestrator.approve` then
    `Orchestrator.execute`), applied to the run a previous `run --dry-run` parked rather than to a graph
    planned in this process. That run's checkpoint is the plan: `orch.load(slug)` reads it, so the
    terminal continues the *same* run instead of re-adopting the manifest through `run --manifest`,
    which would start a second run and leave the parked one's checkpoint behind. A plan prepared by the
    console is the same file in the same place, so either surface finishes it.
    """
    slug = _slug_for(args)
    orch, _ = _orchestrator(args, slug)
    run = orch.load(slug)
    if run is None:
        _warn(f"no prepared plan for {slug!r}; `engine.cli run --goal '…' --dry-run` parks one")
        return EXIT_CHECK_FAILED
    if run.phase != RunPhase.AWAITING_APPROVAL:
        _warn(f"the run is {run.phase.value}, not awaiting approval, so there is nothing to approve")
        return EXIT_CHECK_FAILED
    try:
        orch.approve(run)
    except OrchestratorError as exc:
        _warn(f"cannot approve this plan: {exc}")
        return EXIT_CHECK_FAILED
    if not args.json:
        print(f"Approved {run.run_id}. Executing...")
    outcome = orch.execute(run, executor=args.executor)
    if args.json:
        print(json.dumps({"run": run.as_dict(), "outcome": outcome.as_dict()},
                         indent=2, sort_keys=True, default=str))
    else:
        summary = outcome.summary or {}
        print(f"  outcome   : {summary.get('outcome') or outcome.state.value}")
        print(f"  phase     : {run.phase.value}")
        if run.stop_reason:
            # The single most important line: *why* the run is not done.
            print(f"  stopped   : {run.stop_reason}")
        if run.gate:
            print()
            print(f"  GATE: {run.gate.gate_id} — {run.gate.reason[:70]}")
            print(f"    decide with: engine.cli decide --slug {slug} --approve")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    """Run a goal or an existing manifest, and report where it ended up.

    With `--goal` the graph is planned and shown before execution; with `--manifest` an existing graph
    is adopted; with `--approve-plan` the graph a previous `run --dry-run` left parked is approved and
    executed. Either way the run stops at a gate rather than proceeding past one.
    """
    # The terminal's half of the console's `approve_plan`: a plan parked on disk is a decision waiting
    # for a person, and every CLI command is a new process — so the run that composed it is gone and
    # this one must load it rather than plan it again. `--goal`/`--manifest` name a graph to build,
    # which is the opposite of "act on the one already parked", so the two together is a usage error
    # rather than one silently winning.
    if getattr(args, "approve_plan", False):
        if args.goal or args.manifest:
            _warn("--approve-plan acts on the plan already prepared on disk; drop --goal/--manifest")
            return EXIT_USAGE
        return _approve_prepared_plan(args)

    slug = args.slug or (Path(args.manifest).stem if args.manifest else _slug_from_goal(args.goal))
    orch, workspace = _orchestrator(args, slug)

    # `--posture` is a run-level convenience for the one thing a person most often wants to state on
    # the command line: *should this finish without me?* It applies the posture to the goal this run
    # belongs to, so `run --goal "…" --posture supervised` parks and `--posture unattended` does not.
    # Without a goal on disk there is nothing to carry a posture, so it is recorded only when one
    # exists — and a caller wanting full control uses `goal set --posture …` first.
    if getattr(args, "posture", None) and orch.goal() is not None:
        try:
            existing = orch.goal()
            orch.goal_set(existing.objective, armed=False, by="cli",
                          policy=_posture_policy(orch, args.posture))
        except Exception as exc:  # noqa: BLE001 - a posture that cannot be applied is reported
            _warn(f"cannot apply --posture {args.posture}: {exc}")
            return EXIT_CHECK_FAILED

    try:
        if args.manifest:
            run = orch.adopt(args.manifest, goal=args.goal or "", slug=slug)
        else:
            # An explicit `run` continues an armed goal the same command set in an earlier process.
            # `honour_armed_goal` is passed only when the objective *matches* the one on disk, so
            # running an unrelated goal does not take over an objective someone else armed.
            same_objective = bool(
                orch.goal() is not None
                and orch.goal().objective.strip() == (args.goal or "").strip())
            run = orch.prepare(args.goal, slug=slug, max_iterations=args.max_iterations,
                               honour_armed_goal=same_objective)
    except OrchestratorError as exc:
        _warn(f"cannot start this run: {exc}")
        return EXIT_CHECK_FAILED

    if not args.json:
        print(f"Run {run.run_id}  ({run.phase.value})")
        print(f"  workspace : {workspace.path}")
        print(f"  manifest  : {run.manifest_path.name}")
        if run.plan:
            print(f"  plan      : {run.plan.summary().splitlines()[0]}")
        if run.staffing_gaps:
            print()
            print("Staffing gaps — the plan needs capabilities the roster does not staff:")
            for gap in run.staffing_gaps:
                print(f"  {gap['node_id']:20s} {gap['skill']:22s} {gap['reason']}")
            print("  Hire an agent with these skills, or amend the plan, before running.")

    if args.dry_run:
        # `--json` means *only* JSON on stdout. Printing the human "(dry run…)" line first made the
        # output unparsable, so a script doing `run --dry-run --json | jq` got a syntax error on line
        # one — the "nothing works" symptom on a command whose whole purpose is to be safe to run.
        if args.json:
            print(json.dumps(run.as_dict(), indent=2, sort_keys=True, default=str))
        else:
            print()
            print("(dry run: nothing executed)")
        return EXIT_OK

    orch.approve(run)
    if not args.json:
        print()
        print("Approved. Executing...")

    outcome = orch.execute(run, executor=args.executor)
    if args.json:
        print(json.dumps({"run": run.as_dict(), "outcome": outcome.as_dict()},
                         indent=2, sort_keys=True, default=str))
    else:
        print()
        summary = outcome.summary or {}
        steps = summary.get("steps_used") or (summary.get("budget") or {}).get("steps_used")
        print(f"  outcome   : {summary.get('outcome') or outcome.state.value}")
        print(f"  steps     : {steps}")
        print(f"  phase     : {run.phase.value}")
        if run.stop_reason:
            # The single most important line: *why* the run is not done.
            print(f"  stopped   : {run.stop_reason}")
        if run.gate:
            print()
            print(f"  GATE: {run.gate.gate_id} — {run.gate.reason[:70]}")
            print(f"    requires: {', '.join(run.gate.requires) or '(none)'}")
            print(f"    present : {', '.join(run.gate.present) or '(none)'}")
            print()
            print("  Decide with:  engine.cli decide --slug", slug, "--approve|--reject --note ...")
        nodes = run.outcome.get("nodes") or {}
        if nodes:
            print()
            print("  nodes:")
            for name, record in sorted(nodes.items()):
                line = f"    {name:20s} {str(record.get('status')):14s} {record.get('verdict')}"
                detail = str(record.get("summary") or "").strip()
                if detail:
                    line += f"  — {detail[:90]}"
                print(line)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """Show where a run is, without changing anything."""
    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    run = orch.load(slug) if workspace.exists() else None
    if run is None:
        _warn(f"no run found for {slug!r} in {workspace.path}")
        return EXIT_CHECK_FAILED
    status = orch.status()
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print(f"Run {status['run_id']}  ({status['phase']})")
    # The run's goal text lives on the checkpoint; `status['goal']` is the *goal-status* document the
    # app renders (state, live, spend). Printing the document as the objective is what made this line
    # read as a dict, so the objective is taken from the right field.
    goal_doc = status.get("goal") if isinstance(status.get("goal"), dict) else {}
    objective = str(goal_doc.get("objective") or status.get("run_goal") or "")
    if objective:
        print(f"  objective : {objective[:100]}")
        if goal_doc:
            print(f"  goal      : {goal_doc.get('state')}"
                  + (f"  ({goal_doc.get('pause_reason')})" if goal_doc.get("pause_reason") else "")
                  + f"   continues: {'yes' if goal_doc.get('live') else 'no'}")
    print(f"  workspace : {workspace.path}")
    print(f"  running   : {status['running']}")
    if status.get("stop_reason"):
        # Why the run is where it is. Without this a blocked run showed only "blocked".
        print(f"  stopped   : {status['stop_reason']}")
    if status.get("gate"):
        gate = status["gate"]
        print(f"  gate      : {gate['gate_id']} ({gate['kind']}) — {gate['reason'][:70]}")
    if status.get("staffing_gaps"):
        print(f"  gaps      : {[g['skill'] for g in status['staffing_gaps']]}")
    if status.get("instructions"):
        print(f"  instructed: {status['instructions']}")
    if status.get("constraints"):
        print(f"  constraints: {status['constraints']}")
    cost = status.get("cost") or {}
    print(f"  cost      : {cost.get('runs')} run(s), {cost.get('nodes')} node(s), "
          f"${cost.get('cost_usd') if cost.get('cost_usd') is not None else 'unknown'}")
    nodes = (status.get("outcome") or {}).get("nodes") or {}
    if nodes:
        print()
        print("  nodes:")
        for name, record in sorted(nodes.items()):
            line = f"    {name:20s} {str(record.get('status')):14s} {record.get('verdict')}"
            detail = str(record.get("summary") or "").strip()
            if detail:
                # The node's own words for why it is not done — the most actionable line available.
                line += f"  — {detail[:90]}"
            print(line)
    return EXIT_OK


def cmd_session(args: argparse.Namespace) -> int:
    """List, inspect, archive and branch the sessions a workspace and a root know about.

    `list` answers "what have I run here, and what did it cost" over a whole projects root; `show`
    is one session in full. The two write operations are deliberately asymmetric: `export` produces a
    new archive (nothing on disk is at risk), while `fork` copies a session into a *new* slug and
    refuses to touch the source at all — a branch that could damage the thing it branched from would
    be worse than no branch.
    """
    from .session_exchange import (
        SessionError,
        export_session,
        fork_session,
        list_sessions,
        session_detail,
        sessions_under,
        verify_export,
    )

    action = args.session_command

    if action == "list":
        root = getattr(args, "root", None)
        if not root and getattr(args, "project", None):
            # A single attached folder is a session of one, which is the honest answer rather than a
            # refusal: the command was aimed at a project, and a project is a session.
            sessions = [_resolve_workspace(args, _slug_for(args))]
        else:
            sessions = sessions_under(root or Path(__file__).resolve().parent.parent / "projects")
        rows = list_sessions(sessions)
        if args.json:
            print(json.dumps({"count": len(rows), "root": str(root or ""), "sessions": rows},
                             indent=2, sort_keys=True, default=str))
            return EXIT_OK
        if not rows:
            print("no sessions found")
            print(f"  looked in : {root or 'AgentOrg/projects'}")
            return EXIT_OK
        print(f"{len(rows)} session(s)")
        for row in rows:
            spend = row.get("spend") or {}
            cost = (f"${spend['cost_usd']:.4f}" if spend.get("measured")
                    else "unmeasured")
            label = row["objective"] or row["run_id"] or "(no objective)"
            print(f"  {row['slug']:28s} {str(row['phase']):14s} {cost:12s} {row['updated']}")
            print(f"    {label[:96]}")
            if row.get("error"):
                _warn(f"    {row['slug']}: {row['error']}")
        return EXIT_OK

    if action in ("show", "export", "fork"):
        slug = args.slug or _slug_for(args)
        workspace = _resolve_workspace(args, slug)

        if action == "show":
            detail = session_detail(workspace)
            if args.json:
                print(json.dumps(detail, indent=2, sort_keys=True, default=str))
                return EXIT_OK
            print(f"session   : {detail['slug']}"
                  + ("  (attached)" if detail.get("attached") else ""))
            print(f"  where     : {detail['path']}")
            print(f"  run       : {detail['run_id'] or '(none)'}   phase: {detail['phase']}")
            if detail["objective"]:
                print(f"  objective : {detail['objective'][:100]}")
            if detail["goal_state"]:
                print(f"  goal      : {detail['goal_state']}   posture: "
                      f"{detail['posture'] or '(unset)'}   continues: "
                      f"{'yes' if detail['live'] else 'no'}")
            spend = detail.get("spend") or {}
            cost = f"${spend['cost_usd']:.4f}" if spend.get("measured") else "unmeasured"
            print(f"  spend     : {cost} over {spend.get('tokens') if spend.get('measured') else '?'}"
                  f" token(s)")
            print(f"  trace     : {detail['trace_events']} event(s)   "
                  f"ledger: {len(detail.get('ledger') or [])} entr(ies)   "
                  f"handoffs: {len(detail.get('handoffs') or [])}")
            if detail.get("refused"):
                for entry in detail["refused"]:
                    _warn(f"  handoff {entry['handoff_id']} refused: {entry['reason']}")
            if detail.get("load_error"):
                _warn(f"  {detail['load_error']}")
            rows = detail.get("rows") or []
            if rows:
                print()
                print("  nodes:")
                for row in rows:
                    line = (f"    {str(row.get('node_id')):20s} {str(row.get('status')):14s} "
                            f"{str(row.get('agent_name') or row.get('skill') or ''):18s}")
                    summary = str(row.get("summary") or "").strip()
                    if summary:
                        line += f"  — {summary[:70]}"
                    print(line)
            return EXIT_OK

        if action == "fork":
            try:
                report = fork_session(workspace, args.to)
            except SessionError as exc:
                _warn(str(exc))
                return EXIT_CHECK_FAILED
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True, default=str))
                return EXIT_OK
            print(f"forked {report['from_slug']} -> {report['slug']}")
            print(f"  from      : {report['from']}")
            print(f"  to        : {report['to']}")
            print(f"  copied    : {report['files']} file(s), {report['bytes']} byte(s)")
            for note in report["notes"]:
                print(f"  note      : {note}")
            print(f"  the original is untouched; continue the branch with --slug {report['slug']}")
            return EXIT_OK

        destination = args.out or f"{slug}-session.zip"
        try:
            report = export_session(workspace, destination)
        except SessionError as exc:
            _warn(str(exc))
            return EXIT_CHECK_FAILED
        if args.verify:
            try:
                verify_export(report["path"])
            except SessionError as exc:
                _warn(f"the archive did not verify: {exc}")
                return EXIT_CHECK_FAILED
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        manifest = report["manifest"]
        print(f"exported {manifest['slug']} -> {report['path']}")
        print(f"  bytes     : {report['bytes']}")
        print(f"  files     : {manifest['counts']['files']} "
              f"({manifest['counts']['handoffs']} handoff(s), {manifest['counts']['nodes']} node(s))")
        print(f"  schemas   : "
              + (", ".join(f"{k}={v}" for k, v in sorted(manifest['schema_versions'].items()))
                 or "(none)"))
        if manifest["absent"]:
            print(f"  absent    : {', '.join(manifest['absent'])}")
        if args.verify:
            print("  verified  : every member matches the manifest")
        return EXIT_OK

    _warn(f"unknown session action {action!r}")
    return EXIT_USAGE


def cmd_schedules(args: argparse.Namespace) -> int:
    """Add, list, remove, enable and watch the objectives that fire on a clock.

    The watcher is the only part that spends, and it is a **foreground** process: a schedule file
    arms nothing on its own, exactly as a goal file arms nothing on its own. What makes the watch
    safe is the rule it enforces rather than the interval — a fire that ends paused, blocked, gated
    or failed disables its entry, so a failing objective cannot become an unattended spend loop.
    """
    from .schedules import (
        DEFAULT_TICK_S,
        MAX_TICK_S,
        MIN_TICK_S,
        PARKED_OUTCOMES,
        ScheduleError,
        ScheduleStore,
        watch,
    )

    action = args.schedules_command
    slug = getattr(args, "slug", None) or ""
    if action in ("list", "add", "remove", "enable", "watch"):
        slug = slug or _slug_for(args)
    workspace = _resolve_workspace(args, slug)
    workspace.ensure()
    store = ScheduleStore(workspace)
    if store.load_error:
        _warn(store.load_error)

    if action == "list":
        view = store.view()
        if args.json:
            print(json.dumps(view, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        counts = view["counts"]
        print(f"{counts['total']} schedule(s): {counts['enabled']} armed, "
              f"{counts['disabled']} disabled, {counts['due']} due")
        print(f"  workspace : {workspace.path}")
        if view["load_error"]:
            _warn(f"  {view['load_error']}")
        for entry in view["entries"]:
            every = f"every {entry['interval_s']}s" if entry["interval_s"] else "once"
            print(f"  {entry['id']:16s} {'armed ' if entry['enabled'] else 'paused'} {every:12s} "
                  f"{entry['next_due_at'] or '-':24s} {entry['slug']}")
            print(f"    {entry['objective'][:96]}")
            if entry["last_outcome"]:
                print(f"    last    : {entry['last_outcome']}"
                      + (f" — {entry['last_detail'][:70]}" if entry["last_detail"] else ""))
            if entry["disabled_reason"]:
                print(f"    paused  : {entry['disabled_reason'][:90]}")
        return EXIT_OK

    if action == "add":
        objective = " ".join(args.objective).strip()
        try:
            entry = store.add(objective=objective, slug=slug, posture=args.posture,
                              every=args.every, at=args.at, due_now=args.due_now,
                              enabled=not args.disabled)
        except ScheduleError as exc:
            _warn(str(exc))
            return EXIT_USAGE if "say when it should fire" in str(exc) else EXIT_CHECK_FAILED
        if args.json:
            print(json.dumps(entry.as_dict(), indent=2, sort_keys=True, default=str))
            return EXIT_OK
        every = f"every {entry.interval_s}s" if entry.interval_s else "once"
        print(f"added {entry.id}: {objective}")
        print(f"  fires     : {every}, next {entry.next_due_at}")
        print(f"  posture   : {entry.posture}")
        print(f"  workspace : {workspace.path}")
        print("  arm it    : engine.cli schedules watch --slug " + entry.slug)
        return EXIT_OK

    if action in ("remove", "enable"):
        try:
            if action == "remove":
                entry = store.remove(args.ref)
            else:
                entry = store.enable(args.ref)
        except ScheduleError as exc:
            _warn(str(exc))
            return EXIT_CHECK_FAILED
        if args.json:
            print(json.dumps(entry.as_dict(), indent=2, sort_keys=True, default=str))
            return EXIT_OK
        if action == "remove":
            print(f"removed {entry.id}: {entry.objective[:80]}")
        else:
            print(f"armed {entry.id}: next due {entry.next_due_at}")
        return EXIT_OK

    if action == "watch":
        def resolve(target: str):
            """The orchestrator a fire lands in — the same resolution every other command uses.

            Deliberately *not* an error path of its own: a slug that cannot be resolved disables its
            entry inside `watch`, with the reason recorded, because a watcher that exited on the first
            bad entry would stop the good ones that were still armed.
            """
            return _orchestrator(args, target)

        if args.json:
            # A watcher's whole point is a stream of results, so `--json` emits one JSON object per
            # *fire* and one final summary, each on its own line. NDJSON is the only shape that keeps
            # stdout a single machine-readable stream for a process that runs until interrupted.
            def emit(record: dict[str, Any]) -> None:
                for fire in record.get("fired") or []:
                    print(json.dumps({"type": "fire", **fire}, sort_keys=True, default=str),
                          flush=True)
                for entry in record.get("disabled") or []:
                    print(json.dumps({"type": "disabled", **entry}, sort_keys=True, default=str),
                          flush=True)
        else:
            def emit(record: dict[str, Any]) -> None:
                for fire in record.get("fired") or []:
                    print(f"  fired   : {fire['slug']} -> {fire['outcome']}"
                          + (f"  ({fire['detail'][:70]})" if fire["detail"] else ""))
                for entry in record.get("disabled") or []:
                    _warn(f"  PAUSED  : {entry['slug']} — {entry['reason'][:160]}")
                for entry in record.get("removed") or []:
                    # A fire that beat a removal: the run happened, but the entry was deleted beside
                    # the watcher while it ran. Left removed (never re-armed), and named here so the
                    # deletion is visible in the human-readable stream and not only under `--json`.
                    _warn(f"  REMOVED : {entry['slug']} — {entry['detail'][:160]}")

        if not args.json:
            print(f"watching {len(store.entries)} entry(ies) every {args.interval}s; "
                  "Ctrl-C to stop")
            armed = [entry for entry in store.entries if entry.enabled]
            if not armed:
                print("  nothing is armed; `schedules enable <id>` arms an entry")
            for entry in armed:
                print(f"  {entry.id:16s} next {entry.next_due_at or '-'}  {entry.objective[:64]}")

        try:
            report = watch(store, resolve=resolve, tick_s=args.interval, ticks=args.ticks,
                           max_fires=args.max_fires, on_tick=emit, executor=args.executor)
        except ScheduleError as exc:
            _warn(str(exc))
            return EXIT_USAGE
        except KeyboardInterrupt:
            _warn("stopped")
            if args.json:
                print(json.dumps({"type": "summary", "ticks": 0, "fired": [], "disabled": [],
                                  "stopped": "interrupted"}, sort_keys=True))
            return EXIT_OK

        summary = {"type": "summary", **report} if args.json else report
        if args.json:
            print(json.dumps(summary, sort_keys=True, default=str))
            return EXIT_OK
        print(f"stopped  : {report['stopped']}")
        print(f"  ticks   : {report['ticks']}   fires: {len(report['fired'])}")
        for fire in report["fired"]:
            print(f"    {fire['slug']:24s} {fire['outcome']:10s} {fire['run_id']}")
        if report["disabled"]:
            print()
            print("  paused for you — a schedule never re-arms a goal a previous fire left parked:")
            for entry in report["disabled"]:
                print(f"    {entry['slug']}: {entry['reason'][:110]}")
        if report["removed"]:
            # The other thing a fire can end in: the entry was deleted while its run was in flight, so
            # the run's outcome is real but the schedule is gone. `--json` always carried this; the
            # summary said nothing, which left a removal-invisible list of fires with no explanations.
            print()
            print("  removed while their run was in flight — left removed, not re-armed:")
            for entry in report["removed"]:
                print(f"    {entry['slug']}: {entry['detail'][:110]}")
        return EXIT_OK

    _warn(f"unknown schedules action {action!r}")
    return EXIT_USAGE


def cmd_flow(args: argparse.Namespace) -> int:
    """The org board: which agent is working on what, what moved between them, and what came back.

    `activity` is a *story* (what happened, in order). `status` is a *snapshot* (where is the run).
    This is a *board*: one row per unit of work with its owner, its information flow — in from whom,
    out to whom — and its progress. It answers the question a person running an org actually asks
    when several things are in flight at once.
    """
    from .flow import build_flow, clip

    slug = _slug_for(args)
    config, source, _, _ = _load_stack(args)
    workspace = _resolve_workspace(args, slug)
    org = None
    try:
        org = _roster_for(config, source.library, _project_root_for(args))
    except Exception:  # noqa: BLE001 - the board must render with no roster
        org = None
    org_id = str(getattr(org, "id", "") or "")
    org_name = str(getattr(org, "name", "") or "")

    board = build_flow(workspace, org=org, limit=max(1, int(getattr(args, "limit", None) or 800)),
                       org_id=org_id, org_name=org_name)

    if args.json:
        print(json.dumps(board, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    tone_mark = {"good": "✓", "warn": "!", "bad": "✗", "info": "·", "muted": "·"}
    print(board["headline"])
    print(f"  run       : {board['run_id'] or '(none)'}   phase: {board['phase']}")
    if board.get("goal"):
        print(f"  objective : {str(board['goal'])[:100]}")
    counts = board.get("counts") or {}
    print(f"  work      : {counts.get('done', 0)} done, {counts.get('working', 0)} working, "
          f"{counts.get('waiting', 0)} waiting, {counts.get('stuck', 0)} stuck "
          f"({counts.get('gates', 0)} gate(s))")
    if not board["rows"]:
        print()
        print("  no work is assigned yet — plan a goal to populate the board")
        return EXIT_OK

    print()
    print(f"  {'node':20s} {'agent':14s} {'status':13s} {'in from':14s} {'out to':14s} verdict")
    for row in board["rows"]:
        mark = tone_mark.get(str(row.get("tone")), "·")
        print(f"  {mark}{row['node_id']:19s} {(row['agent_name'] or '—'):14s} "
              f"{row['status']:13s} {(row['received_from'] or '—'):14s} "
              f"{(row['sent_to'] or '—'):14s} {row['verdict'] or ''}".rstrip())
        if row.get("blocked_by"):
            # Clipped on a word boundary, so the reason reads as shortened rather than as a typo —
            # the headline above carries the full sentence for whoever wants all of it.
            print(f"      ↳ {clip(row['blocked_by'], 100)}")

    handoffs = board.get("handoffs") or []
    if handoffs:
        print()
        print("Handoffs (information crossing an agent boundary):")
        for handoff in handoffs:
            mark = tone_mark.get(str(handoff.get("tone")), "·")
            route = f"{handoff['from_agent'] or handoff['from_node'] or '?'} → " \
                    f"{handoff['to_agent'] or handoff['to_node'] or '?'}"
            line = f"  {mark} {handoff['state']:10s} {route}"
            if handoff.get("summary"):
                line += f"  — {str(handoff['summary'])[:60]}"
            print(line)
    return EXIT_OK


def cmd_activity(args: argparse.Namespace) -> int:
    """What is happening: one timeline of the goal, run, nodes, swarms and why it stopped.

    The command exists because "I do not know what is happening in the app" is the actual complaint a
    person has about an autonomous org. `status` answers "where is the run"; this answers "what has it
    been doing, why is it where it is, and what should I do next".
    """
    from .activity import build_activity

    slug = _slug_for(args)
    config, source, _, _ = _load_stack(args)
    workspace = _resolve_workspace(args, slug)
    org = None
    try:
        org = _roster_for(config, source.library, _project_root_for(args))
    except Exception:  # noqa: BLE001 - activity must render with no roster
        org = None

    report = build_activity(workspace, org=org, limit=max(1, int(args.limit or 40)))

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    tone_mark = {"good": "✓", "warn": "!", "bad": "✗", "info": "·"}
    print(f"{report['headline']}")
    print(f"  workspace : {report['workspace']}")
    if report.get("objective"):
        print(f"  objective : {report['objective'][:100]}")
    print(f"  phase     : {report['phase']}   running: {report['running']}"
          + (f"   continues: {report['going']['continues']}" if report.get("going") else ""))
    if report.get("stop_reason"):
        print(f"  stopped   : {report['stop_reason']}")
    counts = report.get("counts") or {}
    print(f"  nodes     : {counts.get('done', 0)} done, {counts.get('in_flight', 0)} in flight, "
          f"{counts.get('blocked', 0)} blocked, {counts.get('pending', 0)} pending")
    if counts.get("swarms") or counts.get("subagents"):
        print(f"  swarms    : {counts.get('swarms', 0)} event(s); "
              f"subagents: {counts.get('subagents', 0)} "
              f"({counts.get('subagents_running', 0)} running)")
    if report.get("staffing_gaps"):
        print(f"  gaps      : {[g['skill'] for g in report['staffing_gaps']]}")

    timeline = report.get("timeline") or []
    if timeline:
        print()
        print("Timeline (newest last):")
        for entry in timeline:
            mark = tone_mark.get(str(entry.get("tone")), "·")
            at = str(entry.get("at") or "(current)")
            when = at[11:19] if len(at) >= 19 and at.startswith("20") else at
            line = f"  {mark} {when:>9s}  {entry.get('kind', ''):9s} {entry.get('title', '')}"
            if entry.get("detail"):
                line += f"  — {str(entry['detail'])[:70]}"
            print(line)

    action = report.get("next_action") or {}
    if action.get("kind") and action.get("kind") != "none":
        print()
        print(f"Next: {action.get('label')}")
        if action.get("detail"):
            print(f"  {action['detail']}")
        if action.get("command"):
            print(f"  $ {action['command']}")
    return EXIT_OK


def cmd_attention(args: argparse.Namespace) -> int:
    """Every workspace that needs you, with the command that resolves each one.

    `status` answers "where is *this* run" and `activity` answers "what is happening *here*" — both
    scoped to a project you have to name first, which is the whole complaint this exists for: a run
    parked at a gate in a project nobody had registered was unreachable, because every surface was
    scoped to one workspace and nothing enumerated the rest. This enumerates them, in the engine's own
    words: what each is waiting for and the exact command that resolves it.

    Nothing here *decides* anything. A gate is listed and never answered, a plan is named and never
    approved — a run's state is the person's data, and this command only reads it.

    `--root` aims it at a directory of projects; without it, the engine's own ``projects/``.
    """
    from .attention import build_attention
    from .flow import clip
    from .portfolio import Portfolio, PortfolioError

    try:
        portfolio = Portfolio.load()
    except PortfolioError as exc:
        # A register that will not load costs the "is it already an org" half and nothing else: the
        # list of what needs a person is still the answer to the question asked.
        _warn(f"cannot read the portfolio: {exc}")
        portfolio = None

    report = build_attention(getattr(args, "root", None), portfolio=portfolio)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    rows = report["workspaces"]
    print(f"{report['count']} workspace(s) need you")
    print(f"  looked in : {report['root']}")
    if not rows:
        print()
        print("nothing is waiting: no run here is at a gate, stopped, or parked awaiting a plan")
        return EXIT_OK
    for row in rows:
        action = row.get("next_action") or {}
        print()
        print(f"  {row['name']}  ({row['phase']})")
        if row.get("headline"):
            # The engine's own sentence for what this workspace is doing, clipped on a word boundary.
            print(f"    {clip(row['headline'], 150)}")
        if row.get("objective"):
            print(f"    on        : {clip(row['objective'], 100)}")
        # The same shape `activity` closes with, so the step and its command read the same way on both
        # commands — one workspace or twenty.
        print(f"    Next: {row['waiting_for']}")
        if action.get("detail"):
            print(f"      {clip(str(action['detail']), 200)}")
        if action.get("command"):
            print(f"      $ {action['command']}")
        org = row.get("org") or {}
        if org.get("registered"):
            print(f"      in your portfolio as {org.get('slug')}")
        elif org.get("adopt_command"):
            # The one step that makes an unreachable project reachable *in the app*: `serve` is bound to
            # one workspace, and `portfolio_add` is the one write that is not (`engine.attention`).
            print(f"      make it usable in the app: {org['adopt_command']}")
    return EXIT_OK


def cmd_decide(args: argparse.Namespace) -> int:
    """Resolve a gate: approve and continue, or reject and park.

    A rejection records the note, because a rejection the agents cannot read is one they will
    re-attempt identically.

    **Approve continues the run.** The command was documented as "approve and continue" and only ever
    cleared the gate — so the run sat at `ready` with nothing driving it, and the operator had to know
    to re-run the manifest by hand. That is the difference between resolving a gate and resuming the
    work behind it, and the CLI had no path for the second. `--no-continue` keeps the old behaviour for
    a caller that wants to inspect before spending.
    """
    if not args.approve and not args.reject:
        _warn("choose --approve or --reject")
        return EXIT_USAGE
    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    run = orch.load(slug)
    if run is None:
        _warn(f"no run found for {slug!r}")
        return EXIT_CHECK_FAILED
    if run.gate is None:
        _warn(f"the run is {run.phase.value}, which is not waiting on a gate")
        return EXIT_CHECK_FAILED
    gate_id = run.gate.gate_id
    orch.decide(bool(args.approve), run=run, note=args.note or "")
    if args.json:
        print(json.dumps(run.as_dict(), indent=2, sort_keys=True, default=str))
    else:
        verdict = "approved" if args.approve else "rejected"
        print(f"{verdict} {gate_id} -> phase {run.phase.value}")
        if args.note:
            print(f"  note: {args.note}")

    # An approval means "carry on", so carry on. A rejection parks the run, which is the answer the
    # operator asked for and needs no continuation.
    if args.approve and not getattr(args, "no_continue", False):
        if not args.json:
            print()
            print("Continuing the run past the gate...")
        try:
            outcome = orch.execute(run, executor=args.executor)
        except OrchestratorError as exc:
            _warn(f"cannot continue: {exc}")
            return EXIT_CHECK_FAILED
        summary = outcome.summary or {}
        if not args.json:
            print(f"  outcome   : {summary.get('outcome') or outcome.state.value}")
            print(f"  phase     : {run.phase.value}")
            if run.stop_reason:
                print(f"  stopped   : {run.stop_reason}")
            if run.gate:
                print(f"  GATE: {run.gate.gate_id} — {run.gate.reason[:70]}")
                print(f"    decide with: engine.cli decide --slug {slug} --approve")
        else:
            print(json.dumps({"run": run.as_dict(), "outcome": outcome.as_dict()},
                             indent=2, sort_keys=True, default=str))
    return EXIT_OK


def cmd_abort(args: argparse.Namespace) -> int:
    """Stop a run for good, keeping its checkpoint.

    **This is not `decide`, and the difference is the whole point.** `decide` answers the gate a run is
    waiting on and lets it carry on; `abort` ends the run where it stands. A person reaching for "make
    it stop" while money is being spent needs that stated, because approving a gate on the way to
    stopping pays for work nobody asked for.
    """
    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    run = orch.load(slug)
    if run is None:
        _warn(f"no run found for {slug!r} in {workspace.path}; "
              f"`engine.cli status --slug {slug}` shows what is actually there")
        return EXIT_CHECK_FAILED
    if run.phase.terminal:
        _warn(f"the run is already {run.phase.value}, so there is nothing left to stop. "
              f"`engine.cli run --slug {slug} --manifest <file>` starts a fresh one")
        return EXIT_CHECK_FAILED

    previous = run.phase.value
    orch.abort(run)
    if args.json:
        print(json.dumps({**run.as_dict(), "aborted": True, "previous_phase": previous,
                          "checkpoint": str(workspace.checkpoint_path)},
                         indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print(f"aborted {run.run_id}  ({previous} -> {run.phase.value})")
    print(f"  workspace : {workspace.path}")
    print("  the checkpoint is kept, so what already finished is still there to read")
    print(f"  read it   : engine.cli activity --slug {slug}")
    print("  not a gate: `engine.cli decide` resolves a gate and lets the run carry on")
    return EXIT_OK


def cmd_pause(args: argparse.Namespace) -> int:
    """Park a run at its next node boundary, keeping its checkpoint.

    **This is not `abort`, and the difference is what a person is asking for.** `pause` parks a run
    so it can be continued; `abort` ends it. Both keep the checkpoint, so a person who reached for
    the wrong one still has their work — but only one of them can be continued, and saying which is
    half of what this command is for.

    The console has sent `pause` to a live engine since it had a Pause button, and the terminal had
    no way to ask for the same state — while `USAGE.md` told the reader that aborting leaves "a
    resume possible". This is that missing half: the **same engine operation** `serve._cmd_pause`
    reaches, applied to the run this process loaded from disk rather than one it is executing. A run
    another process is running cannot be signalled from here, so what is written is the phase a later
    `status`, `resume` or `flow` reads — which is the state a live pause leaves behind, not a
    lookalike.
    """
    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    run = orch.load(slug)
    if run is None:
        _warn(f"no run found for {slug!r} in {workspace.path}; "
              f"`engine.cli status --slug {slug}` shows what is actually there")
        return EXIT_CHECK_FAILED
    if run.phase.terminal:
        _warn(f"the run is already {run.phase.value}, so there is nothing to park. "
              f"`engine.cli run --slug {slug} --manifest <file>` starts a fresh one")
        return EXIT_CHECK_FAILED

    previous = run.phase.value
    already = run.phase is RunPhase.PAUSED
    # A settled or already-paused run is left alone by the engine operation, so the "already" case is
    # answered here rather than asked for: repeating a pause is not a refusal, it is the state the
    # person wanted.
    if not already:
        orch.pause(run)
    if args.json:
        print(json.dumps({**run.as_dict(), "paused": True, "already_paused": already,
                          "previous_phase": previous,
                          "checkpoint": str(workspace.checkpoint_path)},
                         indent=2, sort_keys=True, default=str))
        return EXIT_OK

    if already:
        print(f"{run.run_id} is already paused  ({previous})")
    else:
        print(f"paused {run.run_id}  ({previous} -> {run.phase.value})")
    print(f"  workspace : {workspace.path}")
    print("  the checkpoint is kept, and no node is abandoned: the run stops at a boundary")
    print(f"  continue  : engine.cli resume --slug {slug}")
    print(f"  stop      : engine.cli abort --slug {slug}   (ends it; pause only parks it)")
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    """Continue a parked run from its checkpoint, and carry the work on.

    **The console could always do this and the terminal could not.** `resume` is one of the two
    commands the app's Pause/Resume pair sends, and a run parked at a *gate* by `decide --reject`, or
    parked by `pause`, or left mid-graph by a crash, had no way to be continued from a shell. The
    console's half is `serve._cmd_resume`; this is the same `Orchestrator.resume` on the checkpoint
    this process loaded.

    It continues *executing*, rather than only clearing the pause, for the reason `decide --approve`
    does: a run set to `ready` with nothing driving it is the "documented as continue, and nothing
    continued" defect this file has already had once. `--no-execute` keeps the older, inspect-first
    behaviour.

    The honest limit: a checkpoint that says `running` cannot be told apart from a crashed one by a
    second process — there is no lock in the workspace to ask — so a resume against a graph another
    process is still executing would be a second executor of the same nodes. `decide --approve` has
    the same shape and the same limit; a warning on stderr names it when the checkpoint says
    `running`, so the one case where it matters is not silent.
    """
    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    run = orch.load(slug)
    if run is None:
        _warn(f"no run found for {slug!r} in {workspace.path}; "
              f"`engine.cli status --slug {slug}` shows what is actually there")
        return EXIT_CHECK_FAILED
    if run.phase.terminal:
        _warn(f"the run is {run.phase.value}, so there is nothing to continue. "
              f"`engine.cli run --slug {slug} --manifest <file>` starts a fresh one")
        return EXIT_CHECK_FAILED
    if run.phase is RunPhase.AWAITING_APPROVAL:
        _warn(f"the run is awaiting approval, not paused; "
              f"`engine.cli run --approve-plan --slug {slug}` approves and executes it")
        return EXIT_CHECK_FAILED
    if run.phase is RunPhase.RUNNING:
        _warn("the checkpoint says this run is still running. If another process is executing it, "
              "stop that one first — two executors of one graph write to the same checkpoint")

    previous = run.phase.value
    orch.resume(run)
    if getattr(args, "no_execute", False):
        if args.json:
            print(json.dumps({**run.as_dict(), "resumed": True, "previous_phase": previous,
                              "executed": False, "checkpoint": str(workspace.checkpoint_path)},
                             indent=2, sort_keys=True, default=str))
            return EXIT_OK
        print(f"resumed {run.run_id}  ({previous} -> {run.phase.value})")
        print("  --no-execute: the gate is cleared and the run is ready; nothing was driven")
        print(f"  carry on  : engine.cli resume --slug {slug}")
        return EXIT_OK

    try:
        outcome = orch.execute(run, executor=args.executor)
    except OrchestratorError as exc:
        _warn(f"cannot continue: {exc}")
        return EXIT_CHECK_FAILED
    summary = outcome.summary or {}
    if args.json:
        print(json.dumps({"run": run.as_dict(), "outcome": outcome.as_dict(),
                          "resumed": True, "previous_phase": previous},
                         indent=2, sort_keys=True, default=str))
        return EXIT_OK
    print(f"resumed {run.run_id}  ({previous} -> {run.phase.value})")
    print(f"  outcome   : {summary.get('outcome') or outcome.state.value}")
    if run.stop_reason:
        print(f"  stopped   : {run.stop_reason}")
    if run.gate:
        print()
        print(f"  GATE: {run.gate.gate_id} — {run.gate.reason[:70]}")
        print(f"    decide with: engine.cli decide --slug {slug} --approve")
    return EXIT_OK


def cmd_reassign(args: argparse.Namespace) -> int:
    """Pin a node to a different agent — the manual form of the router's job.

    The router's refusals still hold: an agent without the node's skill, or one that produced the
    artifact a review node would judge, is turned down here exactly as it is turned down there. The
    Owner overrides a *choice*, never an invariant.
    """
    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    run = orch.load(slug)
    if run is None:
        _warn(f"no run found for {slug!r} in {workspace.path}; "
              f"`engine.cli run --slug {slug} --manifest <file>` starts one")
        return EXIT_CHECK_FAILED
    if run.phase.terminal:
        _warn(f"the run is {run.phase.value}, so no node of it will run again and the pin would "
              f"change nothing. `engine.cli run --slug {slug} --manifest <file>` runs the graph again")
        return EXIT_CHECK_FAILED

    spec = _agent_for(orch, args.agent)
    before = run.bindings.get(args.node)
    try:
        orch.reassign(args.node, spec.id, run=run)
    except OrchestratorError as exc:
        _warn(f"cannot reassign {args.node!r}: {exc}")
        return EXIT_CHECK_FAILED

    binding = run.bindings.get(args.node)
    payload = {"run_id": run.run_id, "phase": run.phase.value, "node": args.node,
               "agent_id": spec.id, "agent": spec.name,
               "binding": _binding_dict(binding), "previous": _binding_dict(before)}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print(f"{args.node} pinned to {spec.name} ({spec.id})")
    print(f"  was       : {_binding_line(before)}")
    print(f"  now       : {_binding_line(binding)}")
    print(f"  run       : {run.run_id}  ({run.phase.value})")
    print("  the decision is recorded on the run, so `flow` shows who was actually assigned")
    return EXIT_OK


def cmd_takeover(args: argparse.Namespace) -> int:
    """Take a node over as the Owner, so a human produces that node's artifact.

    The artifact then records a human producer, which is what an audit trail needs. Nothing executes
    here: this records *who* will do the work, and the run picks it up on its next pass.
    """
    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    run = orch.load(slug)
    if run is None:
        _warn(f"no run found for {slug!r} in {workspace.path}; "
              f"`engine.cli run --slug {slug} --manifest <file>` starts one")
        return EXIT_CHECK_FAILED
    if run.phase.terminal:
        _warn(f"the run is {run.phase.value}, so no node of it will run again and the takeover would "
              f"change nothing. `engine.cli run --slug {slug} --manifest <file>` runs the graph again")
        return EXIT_CHECK_FAILED

    # The orchestrator records the takeover without checking the node exists, so the check belongs
    # here — a takeover of a node no run has is a line in a log that changes nothing.
    nodes = [str(n.get("id")) for n in orch._nodes_of(run)]
    if nodes and args.node not in nodes:
        _warn(f"node {args.node!r} is not in this run's plan; nodes: {', '.join(nodes)}")
        return EXIT_CHECK_FAILED

    try:
        orch.takeover(args.node, run=run)
    except OrchestratorError as exc:
        _warn(f"cannot take over {args.node!r}: {exc}")
        return EXIT_CHECK_FAILED

    decision = run.decisions[-1] if run.decisions else {}
    payload = {"run_id": run.run_id, "phase": run.phase.value, "node": args.node,
               "by": str(decision.get("by") or ""), "decision": decision}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print(f"{args.node} taken over by the Owner ({payload['by']})")
    print("  the artifact this node produces will record a human producer")
    print(f"  run       : {run.run_id}  ({run.phase.value})")
    print(f"  not the same as `engine.cli reassign --slug {slug} {args.node} --agent <id>`: that "
          "hands the node to an agent, this does it yourself")
    return EXIT_OK


def _bytes_label(total: int) -> str:
    """A byte count a person can read, for the one line that reports what a discard freed."""
    if total >= 1_048_576:
        return f"{total / 1_048_576:.1f} MB"
    if total >= 1024:
        return f"{total / 1024:.1f} KB"
    return f"{total} B"


def cmd_discard(args: argparse.Namespace) -> int:
    """Clear a *settled* run's checkpoints, so the board stops reporting work nobody can act on.

    **This is not `abort`, and the difference is the whole point.** `abort` stops a run that is still
    going and keeps its checkpoint so the finished work can be read. `discard` acts on a run that has
    already ended — most often one parked at a gate or blocked — and *moves* its two checkpoints out
    of the way, because those files are the only thing making `flow` and `status` report a node with
    no way to resolve it. `abort`, `decide`, `reassign` and `takeover` all act on a run in flight, so
    on a settled run every one of them refuses with "no run found"; this is the verb that was missing.

    The record of what happened — the trace, the handoffs, the ledger, the goal and the cache — is
    **kept**, so discarding is not erasing; `--include-record` is the explicit, separately named way
    to move those too. Nothing is deleted either way: the checkpoints are moved into
    `.agent_state/discarded/<stamp>/`, and the reply says exactly where. A run that is *live* is
    refused by the engine operation, which is told the answer by the process running it.
    """
    from .state import StateError
    from .systemcli import NEXT_SEP

    slug = _slug_for(args)
    workspace = _resolve_workspace(args, slug)
    try:
        # `live=False` deliberately: this process owns no run thread, so it has no run in flight. A
        # server that *does* passes its own `_run_is_live()` — the engine operation refuses on that,
        # so a live checkpoint is never moved out from under the writer.
        report = workspace.discard_run(include_record=bool(getattr(args, "include_record", False)))
    except StateError as exc:
        _warn(str(exc))
        return EXIT_CHECK_FAILED

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    if not report["discarded"]:
        # Already clean is the *answer*, not a failure: the desired state holds. The reason is still
        # stated, so "nothing to do" cannot be confused with a command that did not run.
        print(f"nothing to discard for {slug}")
        print(f"  workspace : {report['workspace']}")
        print(f"  reason    : {report['reason']}")
        print(f"Next: engine.cli flow --slug {slug}{NEXT_SEP}the board already reports no run")
        return EXIT_OK

    moved = report["moved"]
    print(f"discarded {len(moved)} checkpoint(s) for {slug}")
    print(f"  workspace : {report['workspace']}")
    print(f"  backup    : {report['backup_dir']}")
    for entry in moved:
        print(f"    moved   : {entry['name']} ({_bytes_label(entry['bytes'])})")
    print(f"  freed     : {_bytes_label(report['freed_bytes'])}")
    if report["kept"]:
        print(f"  kept      : {', '.join(report['kept'])} — the record of what happened")
    else:
        print("  kept      : the roster, schedules, proposals and sessions — none of the record was "
              "on disk")
    print(f"Next: engine.cli flow --slug {slug}{NEXT_SEP}the board no longer reports the discarded run")
    return EXIT_OK


def _agent_for(orch: Any, ref: str) -> Any:
    """The roster agent a person named, by id or by name.

    A name is what `agents` shows; an id is what the engine stores on a binding. Resolving both in one
    place means a mistyped reference is refused with the names that exist, rather than pinning a node
    to nothing that quietly looks pinned.
    """
    agents = list(getattr(getattr(orch, "org", None), "agents", {}).values())
    spec = next((a for a in agents if a.id == ref), None)
    if spec is None:
        spec = next((a for a in agents if a.name.lower() == ref.lower()), None)
    if spec is None:
        _warn(f"no agent {ref!r} in the roster; `engine.cli agents` lists every id and name")
        raise SystemExit(EXIT_CHECK_FAILED)
    return spec


def _binding_dict(binding: Any) -> Any:
    """A binding as data, whether it is a `Binding` object or the pinned dict `reassign` writes."""
    return binding.as_dict() if hasattr(binding, "as_dict") else binding


def _binding_line(binding: Any) -> str:
    """A binding as one readable line — including "there was none", which is the router's own choice."""
    if not binding:
        return "(the router's choice, not pinned)"
    if isinstance(binding, dict):
        agents = [str(a) for a in (binding.get("agents") or [])]
        if not agents and binding.get("pinned_id"):
            agents = [str(binding["pinned_id"])]
        return f"{', '.join(agents) or '(unknown)'}  [{binding.get('policy') or 'auto'}]"
    return str(binding)


def cmd_subagents(args: argparse.Namespace) -> int:
    """The children a run started, and one child's transcript read a page at a time.

    A child is a session rather than a node: its reads never accumulate in the parent's window, so what
    the parent saw of it is a preview. This is how a person reads what it actually did, and the page is
    the same byte-addressed one the agent's own `read_subagent_result` tool returns.

    Both actions go through the console's handler, so the CLI cannot describe a child differently from
    the panel that renders it.
    """
    from .serve import ServerError

    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    # `load` also puts the run on the orchestrator, which is what scopes a child store to the run that
    # produced it. Without it the console would look under the placeholder run id `run` and report
    # "no children" for a run whose children are right there on disk.
    orch.load(slug)
    console = _console_for(args, workspace=workspace, orchestrator=orch)
    action = args.subagents_command

    if action == "result":
        try:
            page = console._cmd_subagent_result({
                "child_id": args.child_id,
                "offset_bytes": args.offset or 0,
                "limit_bytes": args.limit or 0,
            })
        except ServerError as exc:
            _warn(f"cannot read that child: {exc}; "
                  f"`engine.cli subagents list --slug {slug}` lists the children of this run")
            return EXIT_CHECK_FAILED
        if args.json:
            print(json.dumps(page, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        end = page["offset_bytes"] + page["returned_bytes"]
        print(f"{page['child_id']}  bytes {page['offset_bytes']}..{end} of {page['total_bytes']}")
        print()
        print(page["text"], end="" if page["text"].endswith("\n") else "\n")
        print()
        if page["more"]:
            print(f"  more remains: --offset {page['next_offset_bytes']}")
        else:
            print("  that is the whole transcript")
        return EXIT_OK

    if action != "list":  # pragma: no cover - argparse guards this
        _warn(f"unknown subagents action {action!r}")
        return EXIT_USAGE

    payload = console._cmd_subagents({})
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print(f"{payload['count']} child(ren) for this run"
          + (f"  ({payload['running']} running, {payload['failed']} failed)"
             if payload["count"] else ""))
    for child in payload["children"]:
        task = str(child.get("task") or "")[:52]
        print(f"  {str(child.get('child_id')):14s} {str(child.get('status')):10s} "
              f"{(child.get('skill') or '-'):24s} {task}")
    if not payload["count"]:
        print("  (none — a child appears once a node dispatches one; `run` and `fanout` start them)")
        print(f"  a child's work is a session: `engine.cli subagents result <child_id> --slug {slug}`")
    return EXIT_OK


def cmd_goal(args: argparse.Namespace) -> int:
    """Set, inspect, pause, resume or clear the durable goal for a workspace.

    The goal is what makes a run continue past a model final. Its vocabulary matches the reference
    agent (`set`/`status`/`pause`/`resume`/`clear`), and — importantly — `resume` is the *only* way an
    objective starts spending again after a reload: a goal restored from disk comes back disarmed.
    """
    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    action = args.goal_command

    try:
        if action == "set":
            objective = " ".join(args.objective).strip()
            if not objective:
                _warn("a goal needs an objective: engine.cli goal set \"what to achieve\"")
                return EXIT_USAGE
            from .goal import GoalPolicy, Posture

            # Build the per-goal autonomy from the flags, falling back to the configured default for
            # anything not named — so `goal set "…"` keeps the configured posture and only an explicit
            # flag narrows it. `--posture` is the direct form; `--human-gate` is the legacy alias it
            # supersedes, and the two cannot disagree because the posture is resolved first.
            base = orch._default_goal_policy()
            if getattr(args, "posture", None):
                posture = Posture(args.posture)
            elif getattr(args, "human_gate", False):
                posture = Posture.SUPERVISED
            else:
                posture = base.posture
            gate_choice = None if posture is Posture.SUPERVISED else args.auto_approve
            policy = GoalPolicy(
                auto_approve=base.auto_approve if gate_choice is None else bool(gate_choice),
                auto_hire=base.auto_hire if args.auto_hire is None else bool(args.auto_hire),
                persist_hires=(base.persist_hires if args.persist_hires is None
                               else bool(args.persist_hires)),
                posture=posture,
            )
            goal = orch.goal_set(objective, by="cli", armed=not args.no_arm, policy=policy)
        elif action == "status":
            goal = orch.goal()
        elif action == "pause":
            goal = orch.goal_pause()
        elif action == "resume":
            goal = orch.goal_resume(by="cli")
        elif action == "clear":
            goal = orch.goal_clear()
        else:  # pragma: no cover - argparse guards this
            _warn(f"unknown goal action {action!r}")
            return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - a goal error is a result, said plainly
        _warn(str(exc))
        return EXIT_CHECK_FAILED

    if action == "status" and goal is None:
        if args.json:
            print(json.dumps(orch.goal_status(), indent=2, sort_keys=True, default=str))
        else:
            print(f"no goal set for {workspace.display_name}")
            print(f"  workspace : {workspace.path}"
                  + ("  (attached)" if workspace.is_attached else ""))
            print('  set one   : engine.cli goal set "what to achieve"')
        return EXIT_OK

    status = orch.goal_status()
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print(f"goal      : {status['objective']}")
    print(f"  state     : {status['state']}"
          + (f"  ({status['pause_reason']})" if status.get("pause_reason") else ""))
    print(f"  continue  : {'yes' if status['live'] else 'no'}")
    # Autonomy is a property of the goal, and a person reading `goal status` needs to know whether the
    # run will stop at a gate or pass it — the whole reason the pause happened.
    policy = status.get("policy") or {}
    if policy:
        print(f"  posture   : {status.get('posture') or policy.get('posture') or 'unattended'}"
              + ("  (every gate waits for you)"
                 if (status.get("posture") or policy.get("posture")) == "supervised" else
                 "  (the goal answers its own gates)"))
        print(f"  autonomy  : gates={'auto' if status.get('decides_gates') else 'human'}  "
              f"gaps={'auto' if status.get('staffs_gaps') else 'report'}  "
              f"hires={'persist' if policy.get('persist_hires') else 'ephemeral'}")
    print(f"  budget    : " + (f"{status['token_budget']} tokens/slice"
                              if status.get("budget_enabled") else "none (continues until done)"))
    spend = status.get("spend") or {}
    print(f"  spent     : {spend.get('rounds', 0)} round(s), {spend.get('tokens', 0)} token(s), "
          f"${spend.get('cost_usd', 0.0):.4f}")
    if status.get("summary"):
        print(f"  summary   : {status['summary']}")
    if status.get("blocked_reason"):
        print(f"  blocked   : {status['blocked_reason']}")
    if status.get("pause_reason") == "restored":
        print("  note      : restored from disk and disarmed; `goal resume` continues it")
    return EXIT_OK


def cmd_mission(args: argparse.Namespace) -> int:
    """Set, inspect and drive the standing purpose above the goal.

    A mission is the *why*: an ordered set of objectives worked one at a time. It does not spend — the
    `mission start` action hands the active objective to a Goal, and the Goal is what arms spending —
    so the spend decision stays in exactly one place.
    """
    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    action = args.mission_command

    try:
        detail: dict[str, Any] = {}
        if action == "set":
            statement = " ".join(args.statement).strip()
            if not statement:
                _warn("a mission needs a statement: engine.cli mission set \"what it is for\"")
                return EXIT_USAGE
            mission = orch.mission_set(statement, objectives=args.objective or [],
                                       armed=bool(args.arm))
        elif action == "status":
            mission = orch.mission()
        elif action == "add":
            mission = orch.mission_add(" ".join(args.objective).strip(), at=args.at)
        elif action == "remove":
            mission = orch.mission_remove(int(args.index))
        elif action == "arm":
            mission = orch.mission_arm()
        elif action == "pause":
            mission = orch.mission_pause()
        elif action == "advance":
            mission = orch.mission_advance(summary=args.summary or "")
        elif action == "mark":
            mission = orch.mission_mark(int(args.index), args.state, summary=args.summary or "")
        elif action == "clear":
            mission = orch.mission_clear()
        elif action == "start":
            detail = orch.mission_start(index=args.index, armed=not args.no_arm)
            mission = orch.mission()
        else:  # pragma: no cover - argparse guards this
            _warn(f"unknown mission action {action!r}")
            return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - a mission error is a result, said plainly
        _warn(str(exc))
        return EXIT_CHECK_FAILED

    status = orch.mission_status()

    if action == "status" and mission is None:
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True, default=str))
        else:
            print(f"no mission set for {workspace.display_name}")
            print(f"  workspace : {workspace.path}")
            print('  set one   : engine.cli mission set "what it is for" '
                  '--objective "first step"')
        return EXIT_OK

    if args.json:
        payload = {"mission": status}
        if detail:
            payload.update(detail)
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    if not status.get("statement"):
        print(f"mission   : (none — cleared)")
        return EXIT_OK

    state = str(status.get("state", "empty"))
    print(f"mission   : {status['statement']}")
    print(f"  state     : {state}"
          + (f"  ({status['pause_reason']})" if status.get("pause_reason") else ""))
    progress = status.get("progress") or {}
    print(f"  progress  : {progress.get('done', 0)} of {progress.get('total', 0)} objective(s)"
          + (f"  ({progress['fraction'] * 100:.0f}%)" if progress.get("total") else ""))
    now = status.get("now")
    if now:
        print(f"  now       : #{status['progress'].get('active_index')} {now['text']}")
    elif progress.get("next"):
        print(f"  next      : {progress['next']}")
    objectives = status.get("objectives") or []
    if objectives:
        print()
        print("  objectives:")
        for index, objective in enumerate(objectives):
            mark = {"done": "x", "active": ">", "blocked": "!", "skipped": "-"}.get(
                str(objective.get("state")), " ")
            line = f"    [{mark}] #{index:<2d} {objective.get('text', '')}"
            if objective.get("summary"):
                line += f"   — {str(objective['summary'])[:60]}"
            print(line)
    if detail.get("goal"):
        goal_state = detail["goal"].get("state")
        live = detail["goal"].get("live")
        print()
        if live:
            print(f"  goal      : {goal_state}  (the mission's objective is now being worked)")
        else:
            print(f"  goal      : {goal_state}  — recording, not spending. Run `mission start`"
                  f" without --no-arm to begin.")
    return EXIT_OK


def cmd_instruct(args: argparse.Namespace) -> int:
    """Push guidance or a constraint into a run.

    With `--constraint` the text becomes non-negotiable, which the AR-04 machinery then preserves
    verbatim across every compaction and rotation for the rest of the run.
    """
    slug = _slug_for(args)
    orch, _ = _orchestrator(args, slug)
    run = orch.load(slug)
    if run is None:
        _warn(f"no run found for {slug!r}")
        return EXIT_CHECK_FAILED
    orch.instruct(args.text, run=run, as_constraint=bool(args.constraint))
    kind = "constraint" if args.constraint else "instruction"
    print(f"added {kind} to {run.slug}: {args.text}")
    if args.constraint:
        print("  (non-negotiable: preserved verbatim across compaction and rotation)")
    return EXIT_OK


def cmd_skills_new(args: argparse.Namespace) -> int:
    """Author a new skill.

    Writes a complete, enforceable skill into the Owner's own root — never the library, which is
    commit-pinned and hash-verified. The generated file is plain markdown meant to be edited.
    """
    from .authoring import AuthoringError, SkillTemplate, slugify, write_skill

    criteria = list(args.criterion or [])
    checklist = list(args.check or [])
    if not criteria:
        _warn("a skill needs at least one --criterion; a skill with no criteria cannot gate a node")
        return EXIT_USAGE
    if not checklist:
        # One checklist item per criterion is a sane default: the contract stays enforceable without
        # the author having to name each item twice.
        checklist = [f"{c.rstrip('.')} is satisfied with evidence" for c in criteria]

    template = SkillTemplate(
        name=slugify(args.name), purpose=args.purpose or "", description=args.purpose or "",
        tags=list(args.tag or []),
        inputs=list(args.input or []), outputs=list(args.output or []),
        author=args.author or "Owner",
    )
    try:
        path = write_skill(template, criteria=criteria, checklist=checklist,
                           root=_skill_root_for(args), global_=bool(args.global_),
                           overwrite=bool(args.force))
    except AuthoringError as exc:
        _warn(f"cannot write the skill: {exc}")
        return EXIT_CHECK_FAILED

    if args.json:
        print(json.dumps({"path": str(path), "name": template.name}, indent=2))
        return EXIT_OK
    print(f"wrote {path}")
    print(f"  name      : {template.name}")
    print(f"  criteria  : {len(criteria)}")
    print(f"  checklist : {len(checklist)} ids")
    print("Edit the file, then use it: `engine.cli plan --goal ...` or `engine.cli hire <name> "
          f"--skill {template.name}`")
    return EXIT_OK


def _skill_root_for(args: argparse.Namespace) -> Path | None:
    """Where `skills new` writes. `--project`/`--root` aim it at a project, `--global` at the user
    library.

    Consults the same resolver the roster does, so a skill authored with `--project X` lands in
    `X/.agentorg/skills` where the planner for that project will find it — not silently in the
    process's own project.
    """
    project = _project_root_for(args)
    if project is not None:
        from . import usercfg

        return usercfg.project_root(project)
    return None


def cmd_propose(args: argparse.Namespace) -> int:
    """Find the engine's own defects and write proposals for the ones worth fixing.

    Nothing is applied. The whole point of this command is that it *stops*: it measures, drafts,
    validates against the behavioural suite, and leaves a readable proposal for the Owner to accept or
    delete. A loop that applied its own changes would be unguarded, because the suite that judges it is
    code it could rewrite.

    Kept as its own name because it is the one most people already have in their fingers; `improve`
    and `proposals` are the console's vocabulary for the same two operations, and all of them share
    one implementation rather than rendering the directory three ways.
    """
    from .improver import Improver

    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    improver = Improver(workspace=workspace, memory=getattr(orch, "memory", None))

    # `--list` shows what is already there; `--dry-run` shows the findings without drafting anything;
    # a bare `propose` runs the cycle. Each is one of the renderers `improve`/`proposals` also use, so
    # the two vocabularies cannot describe one directory differently.
    if args.list:
        return _render_proposals(args, improver)
    if args.dry_run:
        return _render_findings(args, improver)
    return _render_cycle(args, improver)


def _render_proposals(args: argparse.Namespace, improver: Any) -> int:
    """List the proposals directory, plus what the boundary refused. Applies nothing."""
    listing = _proposals_for(improver)
    if args.json:
        print(json.dumps(listing, indent=2, sort_keys=True, default=str))
        return EXIT_OK
    print(f"{listing['count']} proposal(s) in {listing['directory']}")
    for entry in listing["proposals"]:
        _print_proposal(entry)
    if listing["refused_count"]:
        print()
        print(f"  {listing['refused_count']} refused (the boundary working):")
        for entry in listing["refused"][-5:]:
            print(f"    {str(entry.get('kind')):22s} {str(entry.get('reason'))[:80]}")
    if not listing["count"]:
        print("  (none — nothing has been measured as a defect yet)")
    print()
    print("  Nothing here has been applied, and nothing here will be. Read one, apply it yourself.")
    return EXIT_OK


def _render_findings(args: argparse.Namespace, improver: Any) -> int:
    """What the detectors measured, without drafting or writing anything."""
    from .improver import is_safety_surface

    findings = improver.detect()
    if args.json:
        print(json.dumps({"findings": [{**f.as_dict(), "safety_surface": is_safety_surface(f.path)}
                                       for f in findings],
                          "count": len(findings), "applies_changes": False},
                         indent=2, sort_keys=True, default=str))
        return EXIT_OK
    if not findings:
        print("no findings: nothing the evidence supports as a defect")
        return EXIT_OK
    print(f"{len(findings)} finding(s), none drafted (dry run):")
    for finding in findings:
        refused = " [SAFETY SURFACE]" if is_safety_surface(finding.path) else ""
        print(f"  [{finding.severity:8s}] {finding.summary()[:78]}{refused}")
    print()
    print("  Nothing has been applied. Without --dry-run a cycle drafts, validates and stops.")
    return EXIT_OK


def _render_cycle(args: argparse.Namespace, improver: Any) -> int:
    """One improver cycle: detect, draft, validate, promote — and apply nothing, ever."""
    considered = improver.run_once()
    listing = _proposals_for(improver)
    payload = {
        "considered": _considered_dicts(considered),
        "count": len(considered),
        "applies_changes": False,
        **listing,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    promoted = [p for p in considered if p.state == "promoted"]
    print(f"{len(considered)} finding(s) considered, {len(promoted)} promoted")
    for proposal in considered:
        mark = {"promoted": "->", "rejected": "x ", "refused": "! "}.get(proposal.state, "? ")
        print(f"  {mark} {proposal.proposal_id:12s} {proposal.finding.kind:22s} {proposal.state}")
        if proposal.refusal:
            print(f"      {proposal.refusal[:100]}")
        elif proposal.validation.improved:
            print(f"      improves: {', '.join(proposal.validation.improved)}")
    print()
    if promoted:
        print(f"  Written to {listing['directory']}")
    print("  Nothing has been applied. Read one, then apply it yourself if you agree.")
    return EXIT_OK


def _considered_dicts(considered: list[Any]) -> list[dict[str, Any]]:
    """The fields the console reads off a cycle's proposals, so both surfaces expose the same ones."""
    return [{"id": p.proposal_id, "kind": p.finding.kind, "state": p.state,
             "improved": p.validation.improved, "regressions": p.validation.regressions,
             "refusal": p.refusal} for p in considered]


def _print_proposal(entry: dict[str, Any]) -> None:
    """One proposal as a human reads it: what was found, where, and the file to open."""
    finding = entry.get("finding") or {}
    print(f"  {str(entry.get('proposal_id')):12s} {str(finding.get('kind')):22s} "
          f"{str(finding.get('path') or '')[:56]}")
    if finding.get("summary"):
        print(f"      {str(finding['summary'])[:88]}")
    if entry.get("file"):
        print(f"      read: {entry['file']}")


def _proposals_for(improver: Any) -> dict[str, Any]:
    """Read the proposals directory the way the CLI and the console both need it.

    Each entry carries the `.md` path beside the JSON, which is the file a person actually reads — the
    console does the same, so the two surfaces point at the same document.

    Each entry also carries its `lifecycle` (state, whether `apply` would proceed, and why not), and
    that is computed by `proposals.ProposalStore` rather than restated here: the panel offers an Apply
    button on `can_apply`, and a second copy of the rule would show up as a button that refuses.
    """
    from .proposals import annotate, ProposalStore

    directory = improver.proposals_dir()
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
    annotate(proposals, ProposalStore(workspace=improver.workspace))
    refused: list[dict[str, Any]] = []
    rejected = directory / "rejected.jsonl"
    if rejected.is_file():
        for line in rejected.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    refused.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return {"proposals": proposals, "count": len(proposals), "refused": refused[-20:],
            "refused_count": len(refused), "directory": str(directory),
            # Still stated even now that `apply` exists, because it is the *listing* that applies
            # nothing. The field means "this read changed no files", and a listing that stopped saying
            # so would leave the guarantee people relied on unstated. The per-entry `lifecycle` says
            # what each proposal *could* do; this says what reading the list did.
            "applies_changes": False}


def cmd_pool(args: argparse.Namespace) -> int:
    """The task pool: work agents pull, rather than work pushed at them.

    `pool list` shows the queue; `pool add` puts work in it; `pool claim` is what a worker does. The
    point of the pool is that capability decides who does the work, so `add` takes `--skill`.
    """
    from .org import default_company
    from .people import HireError, People
    from .pool import DEFAULT_PRIORITY, PoolError, TaskPool

    config, source, providers, _ = _load_stack(args)
    workspace = _resolve_workspace(args, _slug_for(args))
    workspace.ensure()
    pool = TaskPool(workspace.pool_path)

    if args.pool_command == "add":
        schema = None
        if args.schema:
            try:
                schema = json.loads(args.schema)
            except json.JSONDecodeError as exc:
                _warn(f"--schema is not valid JSON: {exc}")
                return EXIT_USAGE
        try:
            task = pool.create(args.description,
                               priority=DEFAULT_PRIORITY if args.priority is None else args.priority,
                               required_skills=args.skill or [],
                               required_capabilities=args.capability or [],
                               output_schema=schema, tags=args.tag or [],
                               parent_id=args.parent)
        except PoolError as exc:
            _warn(f"cannot add that task: {exc}")
            return EXIT_CHECK_FAILED
        if args.json:
            print(json.dumps(task.as_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(f"added {task.id}: {task.description}")
            if task.required_skills:
                print(f"  needs   : {', '.join(task.required_skills)}")
            if task.required_capabilities:
                print(f"  rights  : {', '.join(task.required_capabilities)}")
            if task.output_schema:
                print("  output  : a JSON schema is required for completion")
        return EXIT_OK

    if args.pool_command == "list":
        summary = pool.summary()
        tasks = sorted(pool.tasks.values(), key=lambda t: (-t.priority, t.created_at))
        if args.state:
            tasks = [t for t in tasks if t.state == args.state]
        if args.json:
            print(json.dumps({"summary": summary, "state": args.state, "shown": len(tasks),
                              "tasks": [t.as_dict() for t in tasks]},
                             indent=2, sort_keys=True, default=str))
            return EXIT_OK
        # The headline is the *filtered* set when a filter was asked for. It printed the whole pool's
        # total above a filtered list, so `pool list --state done` on a pool with nothing done read
        # "3 task(s)" and then "(none)" — a count that contradicts the list under it is worse than no
        # count, and it was reachable only now that `--state` refuses a value the pool does not have.
        if args.state:
            print(f"{len(tasks)} of {summary['total']} task(s) in {args.state!r}")
        else:
            print(f"{summary['total']} task(s): "
                  + ", ".join(f"{k}={v}" for k, v in sorted(summary['by_state'].items())))
        for task in tasks:
            who = task.claimed_by or task.offered_to or ""
            print(f"  {task.id:16s} {task.state:8s} p{task.priority:<3d} "
                  f"{(','.join(task.required_skills) or '-'):22s} {who:12s} {task.description[:44]}")
        if not tasks:
            print("  (none)")
        return EXIT_OK

    if args.pool_command == "claim":
        # A claim needs an agent, so the roster is loaded and the named agent resolved.
        people = People(library=source.library, config=config,
                        catalog=ModelCatalog(config, providers), project=_project_root_for(args))
        try:
            org = people.load(project=_project_root_for(args))
        except HireError as exc:
            _warn(f"cannot load the roster: {exc}")
            return EXIT_CHECK_FAILED
        agent = next((a for a in org.agents.values()
                      if a.name.lower() == (args.agent or "").lower() and not a.is_human), None)
        if agent is None:
            _warn(f"no agent named {args.agent!r}; try `engine.cli agents`")
            return EXIT_CHECK_FAILED
        try:
            task = pool.claim(agent, task_id=args.task)
        except PoolError as exc:
            _warn(f"cannot claim: {exc}")
            return EXIT_CHECK_FAILED
        if task is None:
            print(f"nothing is claimable by {agent.name} right now")
            return EXIT_OK
        print(f"{agent.name} claimed {task.id}: {task.description}")
        if task.output_schema:
            print("  this task requires JSON output matching its schema")
        return EXIT_OK

    _warn(f"unknown pool command {args.pool_command!r}")
    return EXIT_USAGE


def _resolve_default_window(config: Any, providers: dict[str, Any]) -> tuple[int | None, int | None, str]:
    """The window (and max output) the system will bind the default model with, and its provenance.

    Resolved exactly as a hire resolves it: an explicit `defaults.context_window` wins (it exists for
    the model the provider cannot report), then the live catalog's probed value, then the declared
    table. `defaults` used to read only the declared table, so a default on a probed model reported
    `window: UNKNOWN` and warned that no agent could be bound — while the very same model hired fine,
    because `hire` *does* consult the catalog. The report has to match the behaviour.
    """
    provider, model, _ = config.default_pair()
    if not provider or not model:
        return None, None, ""
    spec = config.default_model_spec()
    if spec.context_window and spec.model_id == model:
        return int(spec.context_window), spec.max_output, spec.source
    try:
        from .catalog import ModelCatalog

        entry = ModelCatalog(config, providers).resolve(provider, model)
        if entry is not None and entry.window_known:
            return int(entry.context_window), entry.max_output, entry.source
    except Exception:  # noqa: BLE001 - a probe failure falls through to "unknown"
        pass
    return None, None, ""


def cmd_defaults(args: argparse.Namespace) -> int:
    """Show or set the default provider/model everyone uses unless told otherwise.

    This is the one surface for "which model do my people run on". It resolves and reports the
    *effective* pair — not just what the file says — so a person can see when their declared default
    has been superseded because the provider is unreachable or the model unknown, and why.

    Three actions, deliberately separate: `show` writes nothing, `set` changes the model decision, and
    `autonomy` changes the authority decision. Changing which model the org runs on must not silently
    change whether a run needs you.
    """
    from .config import ConfigError, set_autonomy, set_defaults

    action = getattr(args, "defaults_command", "show")
    config, _, providers, _ = _load_stack(args)

    if action == "set":
        path = config.path
        if path is None:
            _warn("no credentials file was loaded, so there is nowhere to write the default")
            return EXIT_CHECK_FAILED
        # Validate *before* the write: a default naming a provider that is not configured would resolve
        # to a fallback and silently not be what the person chose. The serve handler already refused
        # this; the CLI wrote the unknown name straight to the file, so `defaults set --provider nope`
        # left a config pointing at a provider that does not exist.
        if args.provider and args.provider not in config.providers:
            _warn(
                f"unknown provider {args.provider!r}; configured: "
                f"{', '.join(sorted(config.providers)) or '(none)'}"
            )
            return EXIT_CHECK_FAILED
        if args.reviewer_provider and args.reviewer_provider not in config.providers:
            _warn(
                f"unknown reviewer provider {args.reviewer_provider!r}; configured: "
                f"{', '.join(sorted(config.providers)) or '(none)'}"
            )
            return EXIT_CHECK_FAILED
        try:
            set_defaults(
                path,
                provider=args.provider or "",
                model=args.model or "",
                reviewer_provider=args.reviewer_provider or "",
                reviewer_model=args.reviewer_model or "",
                context_window=args.context_window,
            )
        except ConfigError as exc:
            _warn(f"cannot set the default: {exc}")
            return EXIT_CHECK_FAILED
        # Re-read, so the report is the file's truth rather than the argument's echo.
        config, _, providers, _ = _load_stack(args)

    if action == "autonomy":
        path = config.path
        if path is None:
            _warn("no credentials file was loaded, so there is nowhere to write the setting")
            return EXIT_CHECK_FAILED
        goal: dict[str, Any] = {}
        if args.auto_gates is not None:
            goal["auto_pass_auto_gates"] = bool(args.auto_gates)
        if args.auto_hire is not None:
            goal["auto_hire_missing"] = bool(args.auto_hire)
        if args.persist_hires is not None:
            goal["persist_auto_hires"] = bool(args.persist_hires)
        if args.max_tier is not None:
            goal["auto_hire_max_tier"] = int(args.max_tier)
        if getattr(args, "posture", None):
            goal["default_posture"] = str(args.posture)
        if not goal:
            _warn("nothing to change: pass --posture, --auto-gates/--no-auto-gates, --auto-hire, "
                  "--persist-hires or --max-tier")
            return EXIT_USAGE
        try:
            set_autonomy(path, goal=goal)
        except ConfigError as exc:
            _warn(f"cannot set autonomy: {exc}")
            return EXIT_CHECK_FAILED
        config, _, providers, _ = _load_stack(args)

    provider, model, reason = config.default_pair()
    spec = config.default_model_spec()
    # Report the window the system will *actually* bind with — the catalog's probed value first, the
    # declared table second — not just the declared table. Reading only the config printed
    # `window: UNKNOWN` for `Olla/deepseek-v4.1-flash` (a probed model the config never declares) and
    # warned that no agent could be bound, while `hire` on that same model worked fine. A report that
    # contradicts the behaviour is worse than no report.
    resolved_window, max_output, window_source = _resolve_default_window(config, providers)
    report = {
        "provider": provider,
        "model": model,
        "reason": reason,
        "context_window": resolved_window,
        "window_known": resolved_window is not None,
        "window_source": window_source,
        "max_output": max_output,
        "temperature": config.default.temperature,
        "reviewer": {
            "provider": config.default.reviewer_provider,
            "model": config.default.reviewer_model,
        },
        "declared": config.default.as_dict(),
        "autonomy": {
            "auto_pass_auto_gates": config.goal.auto_pass_auto_gates,
            "auto_hire_missing": config.goal.auto_hire_missing,
            "persist_auto_hires": config.goal.persist_auto_hires,
            "auto_hire_max_tier": config.goal.auto_hire_max_tier,
            "token_budget": config.goal.token_budget,
        },
        "config_path": str(config.path) if config.path else "",
        "providers": sorted(providers),
        "usable": config._usable_providers(),
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(f"default   : {provider}/{model or '(none)'}")
        print(f"  why       : {reason}")
        window = str(resolved_window) if resolved_window else "UNKNOWN"
        print(f"  window    : {window}"
              + (f"  ({window_source})" if resolved_window and window_source else ""))
        print(f"  reviewer  : {config.default.reviewer_provider or '(derived)'}/"
              f"{config.default.reviewer_model or '(derived)'}")
        print(f"  usable    : {', '.join(report['usable']) or '(none)'}")
        print(f"  configured: {', '.join(report['providers']) or '(none)'}")
        print(f"  autonomy  : gates={'auto' if config.goal.auto_pass_auto_gates else 'human'}  "
              f"gaps={'auto' if config.goal.auto_hire_missing else 'report'}  "
              f"hires={'persist' if config.goal.persist_auto_hires else 'ephemeral'}")
        print()
        print("Change it with:  engine.cli defaults set --provider P --model M")
        print("                 engine.cli defaults autonomy --no-auto-gates   # every goal waits")
        if not resolved_window:
            print()
            print("note: this model has no known context window, so no agent can be bound to it.")
            print("      Probe the provider (`models --refresh`) or pass --context-window.")
    return EXIT_OK


def cmd_hire(args: argparse.Namespace) -> int:
    """Hire an agent into the roster.

    Agents are the one thing the product models but could not create: every run used the same seven
    built-in names. This is the missing half — the roster becomes the Owner's to grow.
    """
    from .catalog import ModelCatalog
    from .people import HireError, HireRequest, People

    config, source, providers, _ = _load_stack(args)
    catalog = ModelCatalog(config, providers)
    people = People(library=source.library, config=config, catalog=catalog,
                    project=_project_root_for(args))
    try:
        org = people.load(project=_project_root_for(args))
        spec = people.hire(
            HireRequest(name=args.name, skill=args.skill,
                        provider=args.provider or "", model=args.model or "",
                        context_window=args.context_window, level=args.level or "senior",
                        role=args.role or "worker", team=args.team or "",
                        title=args.title or "", max_concurrency=args.concurrency or 1,
                        capabilities=list(getattr(args, "capabilities", None) or [])),
            org=org, roster_root=_roster_root_for(args),
        )
    except HireError as exc:
        _warn(f"cannot hire: {exc}")
        return EXIT_CHECK_FAILED
    for warning in people.warnings:
        _warn(f"warning: {warning}")
    if args.json:
        print(json.dumps(spec.as_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(f"hired {spec.name} ({spec.title})  id={spec.id}")
        print(f"  skill  : {', '.join(spec.skills)}")
        print(f"  model  : {spec.provider}/{spec.model}  (window {spec.context_window})")
        print(f"  level  : {spec.level.label}   role: {spec.role}")
        if spec.team:
            print(f"  team   : {spec.team}")
    return EXIT_OK


def _roster_root_for(args: argparse.Namespace) -> Path | None:
    """Where a hire is persisted.

    `--roster-root` wins, then `--root` (so the roster lands in the project the command was aimed at
    rather than wherever the shell happened to be), then the discovered project root. Getting this
    wrong writes an agent's definition to a directory nobody looks in, which makes the hire vanish.
    """
    from . import usercfg

    if getattr(args, "roster_root", None):
        return Path(args.roster_root)
    project = _project_root_for(args)
    if project is not None:
        # `usercfg.project_root` finds the `.agentorg` (or the repo marker) under the target, so
        # `--project Ideas` and `--root <dir>` both write `…/.agentorg/roster.json` rather than a
        # loose `roster.json` beside the code, which no reader looks for.
        return usercfg.project_root(project)
    return None


def cmd_agents(args: argparse.Namespace) -> int:
    """List the effective roster: the built-ins plus every hire, from both roots."""
    from .catalog import ModelCatalog
    from .people import HireError, People

    config, source, providers, _ = _load_stack(args)
    catalog = ModelCatalog(config, providers)
    people = People(library=source.library, config=config, catalog=catalog,
                    project=_project_root_for(args))
    try:
        org = people.load(project=_project_root_for(args))
    except HireError as exc:
        _warn(f"cannot load the roster: {exc}")
        return EXIT_CHECK_FAILED
    roster = [a for a in org.roster_view() if a.get("kind") != "human"]
    # The anti-sprawl report, which existed on the desk and was reachable from nowhere — so a roster
    # could grow a delegate that burns tokens without finishing work and no command would say so.
    # Computed here because this is the only command that already holds the real roster; the desk's
    # own `anti_sprawl` reads live runtimes, which an empty roster cannot supply.
    sprawl = _sprawl_report(org, config)
    if args.json:
        print(json.dumps({"agents": roster, "sprawl": sprawl,
                          "loaded_from": people.loaded_from},
                         indent=2, sort_keys=True, default=str))
        return EXIT_OK
    print(f"{len(roster)} agent(s)")
    for entry in roster:
        print(f"  {entry['name']:10s} {entry.get('title', ''):20s} "
              f"{entry.get('provider')}/{entry.get('model')}  [{', '.join(entry.get('skills') or [])}]")
    if sprawl["suspects"]:
        print()
        print("Sprawl suspected — tokens per completed task is above the configured threshold:")
        for name in sprawl["suspects"]:
            print(f"  {name}")
        print("  These agents spend more per finished task than the design expects. Retire one, or")
        print("  check whether it is being delegated work its skill does not cover.")
    if people.loaded_from:
        print()
        for path in people.loaded_from:
            print(f"  roster: {path}")
    return EXIT_OK


def _people_for(args: argparse.Namespace) -> Any:
    """A roster manager aimed at the project this command named, with a catalog to resolve windows.

    Built the same way `cmd_hire` and `cmd_agents` build it, so an edit validates against the models
    the roster can actually see rather than against a bare config table.
    """
    from .catalog import ModelCatalog
    from .people import People

    config, source, providers, _ = _load_stack(args)
    return People(library=source.library, config=config,
                  catalog=ModelCatalog(config, providers), project=_project_root_for(args))


def _agent_id_for(people: Any, ref: str) -> str:
    """Resolve an agent named by id or by name to its id.

    The console edits by id because it listed the roster first. A person at a terminal is likelier to
    have the *name* in front of them, so both resolve here — and a miss is refused with the ids, since
    an edit aimed at nothing that appears to succeed is the worse outcome.
    """
    org = people.org
    if org is None:
        org = people.load(project=people.project)
    agents = list(org.agents.values())
    spec = next((a for a in agents if a.id == ref), None)
    if spec is None:
        spec = next((a for a in agents if a.name.lower() == ref.lower()), None)
    if spec is None:
        _warn(f"no agent {ref!r} in the roster; `engine.cli agents` lists every id and name")
        raise SystemExit(EXIT_CHECK_FAILED)
    return spec.id


def cmd_agent_update(args: argparse.Namespace) -> int:
    """Change an agent's name, model, level or limits — keeping its id and its history.

    Editing rather than firing-and-rehiring: the id is what the mailbox, the session history and the
    health record are keyed on, so a rehire would look like a brand-new employee with no past. Only the
    fields named are touched.

    It edits the roster the *console* edits, through the same `People` object, so an agent changed here
    is the agent the app then shows.
    """
    from .people import HireError

    people = _people_for(args)
    try:
        org = people.load(project=_project_root_for(args))
    except HireError as exc:
        _warn(f"cannot load the roster: {exc}")
        return EXIT_CHECK_FAILED
    agent_id = _agent_id_for(people, args.agent)

    changes = {"name": args.name, "provider": args.provider, "model": args.model,
               "context_window": args.context_window, "level": args.level, "team": args.team,
               "title": args.title, "max_concurrency": args.concurrency}
    if all(value is None for value in changes.values()):
        _warn("nothing to change: name at least one of --name, --provider, --model, --level, "
              "--title, --team or --concurrency")
        return EXIT_USAGE

    try:
        spec = people.update_agent(agent_id, org=org, roster_root=_roster_root_for(args), **changes)
    except (HireError, ValueError, TypeError) as exc:
        _warn(f"cannot update {agent_id}: {exc}")
        return EXIT_CHECK_FAILED
    for warning in people.warnings:
        _warn(f"warning: {warning}")

    payload = {"agent": spec.as_dict(), "changed": [k for k, v in changes.items() if v is not None],
               "roster": str(_roster_root_for(args) or "") or None}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print(f"updated {spec.name} ({spec.id})")
    print(f"  changed : {', '.join(payload['changed'])}")
    print(f"  model   : {spec.provider}/{spec.model}  (window {spec.context_window})")
    print(f"  level   : {spec.level.label}   title: {spec.title}   team: {spec.team or '(none)'}")
    print("  its id and its history are unchanged, so the run it belongs to keeps its bindings")
    return EXIT_OK


def cmd_agent_retire(args: argparse.Namespace) -> int:
    """Remove an agent from the roster, keeping what it produced.

    The org's own refusals hold: the Owner cannot be retired, and an agent whose reports are actively
    working cannot be either — the work those reports hold would be orphaned mid-flight.
    """
    from .people import HireError

    people = _people_for(args)
    try:
        org = people.load(project=_project_root_for(args))
    except HireError as exc:
        _warn(f"cannot load the roster: {exc}")
        return EXIT_CHECK_FAILED
    agent_id = _agent_id_for(people, args.agent)

    try:
        spec = people.retire_agent(agent_id, reason=args.reason or "", org=org,
                                   roster_root=_roster_root_for(args))
    except HireError as exc:
        _warn(f"cannot retire {agent_id}: {exc}")
        return EXIT_CHECK_FAILED

    remaining = [a for a in people.org.roster_view() if a.get("kind") != "human"]
    payload = {"retired": spec.as_dict(), "reason": args.reason or "",
               "agents": remaining, "count": len(remaining)}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    print(f"retired {spec.name} ({spec.id})"
          + (f" — {args.reason}" if args.reason else ""))
    print(f"  {len(remaining)} agent(s) remain; what it produced is kept")
    if not remaining:
        print("  the roster is now empty of workers — `engine.cli hire` adds one back")
    return EXIT_OK


def cmd_providers(args: argparse.Namespace) -> int:
    """List, add, test or remove provider endpoints.

    One command with four actions rather than four nouns, because they are edits to one file and a
    person doing one of them almost always does another next. It is the console's own handler in every
    case, so a provider the CLI accepts is a provider the panel can edit.

    **No key is ever printed.** `has_key` says whether a key resolves; the value stays in the file.
    """
    from .serve import ServerError

    action = args.providers_command
    # No workspace is resolved: these four actions read and write `credentials.json` and nothing else,
    # and resolving one would *create* a project directory for a command that never looks in it.
    console = _console_for(args, needs_workspace=False)

    if action == "list":
        payload = console._cmd_providers({})
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        print(f"{len(payload['providers'])} provider(s) from {payload['config_path'] or '(no file)'}")
        for entry in payload["providers"]:
            models = entry.get("model_count") or 0
            line = (f"  {entry['id']:12s} {entry['kind']:10s} {entry['status']:18s} "
                    f"{'key' if entry['has_key'] else 'no key':6s} {models:3d} model(s)  "
                    f"{entry['base_url']}")
            print(line)
            if entry.get("base_url_note"):
                print(f"      note: {entry['base_url_note'][:100]}")
            if entry.get("error"):
                print(f"      error: {str(entry['error'])[:100]}")
        if payload["skipped"]:
            print()
            print("  skipped (could not be built):")
            for entry in payload["skipped"]:
                print(f"    {str(entry)[:110]}")
        if not payload["providers"]:
            print("  (none — add one with: engine.cli providers add <id> "
                  f"--kind {SUPPORTED_KINDS[0]} "
                  "--base-url https://host/v1 --key-env NAME)")
        return EXIT_OK

    # `remove` names nothing but the id, so the candidate payload is built only where it is used —
    # reading flags a subparser never defines is the `AttributeError: no attribute 'kind'` failure.
    payload: dict[str, Any] = {
        "provider_id": args.provider_id or "",
        "kind": getattr(args, "kind", None) or SUPPORTED_KINDS[0],
        "base_url": getattr(args, "base_url", None) or "",
        "api_key": getattr(args, "key", None),
        "api_key_env": getattr(args, "key_env", None),
        "api_version": getattr(args, "api_version", None),
        "timeout_s": getattr(args, "timeout_s", None),
        "max_retries": getattr(args, "max_retries", None),
        "concurrency": getattr(args, "concurrency", None),
    }

    if action == "test":
        result = console._cmd_provider_test(payload)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        if result.get("ok"):
            print(f"{payload['provider_id']} reachable — {result.get('model_count')} model(s)")
        else:
            print(f"{payload['provider_id']} not usable: {result.get('reason') or 'no reason given'}")
        if result.get("status"):
            print(f"  status    : {result['status']}")
        if result.get("note"):
            print(f"  note      : {result['note']}")
        for model in (result.get("models") or [])[:12]:
            window = model.get("context_window") if model.get("window_known") else "UNKNOWN"
            print(f"    {model.get('model_id'):36s} window {window}")
        return EXIT_OK

    if action == "add":
        try:
            result = console._cmd_provider_add(payload)
        except ServerError as exc:
            _warn(f"cannot save that provider: {exc}")
            return EXIT_CHECK_FAILED
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        print(f"saved {result['provider_id']} to {result['saved']}")
        if result.get("base_url_note"):
            print(f"  note: {result['base_url_note']}")
        print("  the live engine has already re-read the file, so `models` sees it now")
        print(f"  test it: engine.cli providers test {result['provider_id']} --base-url "
              f"{result['base_url']}")
        return EXIT_OK

    if action == "remove":
        try:
            result = console._cmd_provider_remove({"provider_id": args.provider_id})
        except ServerError as exc:
            # Includes the refusal to remove the *last* provider: the engine will not write a document
            # with no providers, because no launch could read it (`config._build_providers`).
            _warn(f"cannot remove {args.provider_id!r}: {exc}")
            return EXIT_CHECK_FAILED
        # The ids are read back from the *file*, not from the console's own provider list: that list is
        # a live probe of the endpoints, and "what is left in the file" should not depend on a probe
        # answering. Both are true statements, but only one of them is the answer to this command.
        remaining = [str(pid) for pid in _provider_ids_from(console.config.path)]
        payload = {"removed": result["removed"], "remaining": remaining}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        print(f"removed {result['removed']}")
        print(f"  remaining : {', '.join(remaining) or '(none)'}")
        print("  a default, a concurrency limit or the reviewer default that named it was pruned in "
              "the same write")
        # The roster agents still bound to it travel in the engine's reply (`agents`), but this command
        # resolves no project — it edits `credentials.json` and nothing else — so there is no roster to
        # read here. The app has a workspace and shows the names; a CLI that guessed from the current
        # directory would be reporting on a project nobody named.
        return EXIT_OK

    _warn(f"unknown providers action {action!r}")  # pragma: no cover - argparse guards this
    return EXIT_USAGE


def _provider_ids_from(path: Any) -> list[str]:
    """The provider ids a credentials document actually holds, read from the file.

    The file rather than `Config.providers`, because the answer must be what a *write* produced. A
    document the loader refuses has no `Config` to read, so a reload can leave the in-memory object
    stale on exactly the document this command is reporting about.
    """
    if not path:
        return []
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    providers = document.get("providers")
    return sorted(providers) if isinstance(providers, dict) else []


def cmd_improve(args: argparse.Namespace) -> int:
    """Run one cycle of the self-improvement loop. **Nothing is applied.**

    The loop is propose-only by construction (`engine/improver.py`): it measures a defect, drafts a fix,
    proves it against the eval baseline and *stops*, leaving a file for you to read and apply yourself.
    The surfaces that judge the loop — the eval gate, the guardrail, the budget config — are refused
    outright, because an improver that can rewrite its own gate can make anything pass.

    `--list` shows what is already there; `--dry-run` shows the findings without drafting anything.
    """
    from .improver import Improver

    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)
    improver = Improver(workspace=workspace, memory=getattr(orch, "memory", None))

    if args.list:
        return _render_proposals(args, improver)
    if args.dry_run:
        return _render_findings(args, improver)
    code = _render_cycle(args, improver)
    if not args.json:
        print(f"  List them  : engine.cli proposals --slug {slug}")
    return code


def cmd_proposals(args: argparse.Namespace) -> int:
    """What the self-improvement loop has proposed — and the lifecycle a person drives.

    The listing half is read-only and says so. The subcommands are the part that was missing: a
    detection nobody can act on is a report, not a loop, and the person's complaint was exactly that
    the self-checks "don't have to move forward on fixes".

    What still does not happen here, by construction:

    - `accept` and `reject` are bookkeeping and cannot touch the tree.
    - `apply` is the one transition that edits anything, so it requires a **demonstrated improvement**
      *and* a proposal nothing has settled yet — a rejected or already-applied one is refused before
      the evidence is even read. It runs the project's own test suite before and after, and **reverts
      on regression** (or on a run it could not read, which is not a pass). A proposal whose patch is a
      description rather than a diff is refused with that said plainly rather than quietly "applied" as
      a no-op.
    - Everything aimed at the machinery that judges this loop is still refused, and
      `engine/proposals.py` — the applier itself — is on that list.
    """
    from .improver import Improver

    slug = _slug_for(args)
    workspace = _resolve_workspace(args, slug)
    workspace.ensure()
    action = getattr(args, "proposals_command", None)

    if action in ("accept", "reject", "apply", "undo"):
        return _proposal_lifecycle(args, workspace, action)

    if getattr(args, "show", None):
        return _show_proposal(args, workspace, args.show)

    code = _render_proposals(args, Improver(workspace=workspace))
    if not args.json:
        print("  Move one  : engine.cli proposals accept <id> | reject <id> --reason '...'")
        print("              engine.cli proposals apply <id>   (tests run before and after)")
        print(f"  Draft one : engine.cli improve --slug {slug}")
    return code


def _proposal_store(workspace: Any) -> Any:
    from .proposals import ProposalStore

    return ProposalStore(workspace=workspace)


def _show_proposal(args: argparse.Namespace, workspace: Any, proposal_id: str) -> int:
    """One proposal in full: what it found, why, the evidence, and the patch."""
    from .proposals import ProposalLifecycleError

    store = _proposal_store(workspace)
    try:
        proposal = store.load(proposal_id)
    except ProposalLifecycleError as exc:
        _warn(str(exc))
        return EXIT_CHECK_FAILED
    payload = {
        **proposal.as_dict(),
        "file": str(store.directory / f"{proposal.proposal_id}-{proposal.finding.kind}.md"),
        "can_apply": store.can_apply(proposal),
        "why_not": store.why_not(proposal),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK
    print(proposal.render())
    return EXIT_OK


def _proposal_lifecycle(args: argparse.Namespace, workspace: Any, action: str) -> int:
    """One lifecycle step, with the same words and the same exit codes as every other command."""
    from .proposals import ProposalLifecycleError

    store = _proposal_store(workspace)
    proposal_id = args.proposal_id
    try:
        if action == "accept":
            proposal = store.accept(proposal_id)
            payload = {"proposal_id": proposal.proposal_id, "state": proposal.state,
                       "applied": False,
                       "next": f"engine.cli proposals apply {proposal_id}"}
        elif action == "reject":
            proposal = store.reject(proposal_id, reason=args.reason or "")
            payload = {"proposal_id": proposal.proposal_id, "state": proposal.state,
                       "reason": proposal.refusal, "applied": False}
        elif action == "undo":
            outcome = store.undo(proposal_id)
            payload = {**outcome.as_dict(), "applied": False}
            if outcome.refused:
                _warn(outcome.refused)
                return EXIT_CHECK_FAILED
        else:
            outcome = store.apply(proposal_id, force=bool(getattr(args, "force", False)))
            payload = outcome.as_dict()
            if outcome.refused:
                # A refusal is *data*, so `--json` gets the whole outcome before the code is returned:
                # a caller that only saw exit 1 would not know whether the tree was touched, and the
                # answer here is always "no", which is worth being able to assert.
                if args.json:
                    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
                _warn(outcome.refused)
                return EXIT_CHECK_FAILED
    except ProposalLifecycleError as exc:
        _warn(str(exc))
        return EXIT_CHECK_FAILED

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK
    _print_lifecycle(action, payload)
    return EXIT_OK


def _print_lifecycle(action: str, payload: dict[str, Any]) -> None:
    """The human half. Every line about what was touched is derived from the outcome, not assumed."""
    if action == "accept":
        print(f"accepted   : {payload['proposal_id']}  (state {payload['state']})")
        print("  applied  : nothing — accepting is a decision, not an edit")
        print(f"  next     : {payload['next']}")
        return
    if action == "reject":
        print(f"rejected   : {payload['proposal_id']}  (state {payload['state']})")
        print(f"  reason   : {payload['reason']}")
        print("  recorded : the same finding is not re-drafted every cycle")
        return
    if action == "undo":
        print(f"undone     : {payload['proposal_id']}")
        for path in payload.get("files") or []:
            print(f"  restored : {path}")
        if payload.get("detail"):
            print(f"  {payload['detail']}")
        return
    # apply
    if payload.get("reverted"):
        print(f"REVERTED   : {payload['proposal_id']} — the tree is back as it was")
        print(f"  {payload['detail']}")
        return
    print(f"applied    : {payload['proposal_id']}")
    for path in payload.get("files") or []:
        print(f"  changed  : {path}")
    before = payload.get("tests_before") or {}
    after = payload.get("tests_after") or {}
    if before or after:
        print(f"  tests    : {before.get('passed', '?')} -> {after.get('passed', '?')} passed, "
              f"{after.get('failed', '?')} failed")
    if payload.get("backup_dir"):
        print(f"  undo     : engine.cli proposals undo {payload['proposal_id']}")
        print(f"  copy of the originals: {payload['backup_dir']}")


def _sprawl_report(org: Any, config: Any) -> dict[str, Any]:
    """The anti-sprawl metric over the real roster, from the configured window and threshold.

    Degrades to an empty report rather than failing: a roster with no completed tasks has nothing to
    measure, and that is not an error.
    """
    from .org import HiringDesk

    try:
        desk = HiringDesk(
            org, max_depth=int(config.delegation.max_depth),
            span_of_control=int(config.delegation.span_of_control),
            allow_ephemeral=bool(config.delegation.allow_ephemeral),
            budget_share_max=float(config.delegation.budget_share_max),
            approval_tiers=dict(config.delegation.approval_tiers or {}),
        )
        return desk.anti_sprawl(
            window_runs=int(getattr(config.delegation, "anti_sprawl_window_runs", 5)),
            growth_threshold=float(
                getattr(config.delegation, "anti_sprawl_growth_threshold", 0.20)),
        )
    except Exception as exc:  # noqa: BLE001 - a metric that cannot be computed is not a failure
        return {"agents": {}, "suspects": [], "error": str(exc)}


def cmd_portfolio(args: argparse.Namespace) -> int:
    """The principal and the several orgs they run.

    In the real world one person runs several organisations at once — a CEO who is also a founder, a
    CTO of a second company, the chair of a foundation — each with its own agents, missions, goals and
    budget. This is that top level: a register of the person and their orgs, plus the one command that
    starts work in an org *in parallel* with any other org already running.

    The portfolio itself runs nothing and spends nothing; that stays with each org's goal.
    """
    from .fleet import Fleet, FleetError
    from .portfolio import Portfolio, PortfolioError

    action = args.portfolio_command

    try:
        portfolio = Portfolio.load()
    except PortfolioError as exc:
        _warn(f"cannot read the portfolio: {exc}")
        return EXIT_CHECK_FAILED

    # ── init ────────────────────────────────────────────────────────────────
    if action == "init":
        if portfolio is None:
            portfolio = Portfolio.new()
        portfolio.ensure_principal(args.name)
        path = portfolio.save()
        if args.json:
            print(json.dumps({"principal": portfolio.principal.as_dict(), "path": str(path)},
                             indent=2, sort_keys=True, default=str))
        else:
            print(f"principal : {portfolio.principal.name}  ({portfolio.principal.id})")
            print(f"  register: {path}")
            if not portfolio.orgs:
                print('  next    : engine.cli portfolio add "My Company" --path ~/code/my-company')
        return EXIT_OK

    if portfolio is None:
        _warn("no portfolio yet; start with `engine.cli portfolio init <your name>`")
        return EXIT_CHECK_FAILED

    # ── add / update / remove / use ──────────────────────────────────────────
    if action == "add":
        try:
            entry = portfolio.add_org(name=args.name, slug=args.slug or "", path=args.path or "",
                                      charter=args.charter or "",
                                      daily_budget_usd=args.daily_budget_usd or 0.0,
                                      make_active=bool(args.active))
        except PortfolioError as exc:
            _warn(f"cannot add the org: {exc}")
            return EXIT_CHECK_FAILED
        portfolio.save()
        if args.json:
            print(json.dumps(entry.as_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(f"registered org : {entry.name}  ({entry.slug}, {entry.id})")
            if entry.path:
                print(f"  folder       : {entry.path}")
            else:
                print("  folder       : (managed, under projects/)")
            print(f"  run it       : engine.cli portfolio run {entry.slug} \"what to achieve\"")
        return EXIT_OK

    if action == "update":
        enabled = True if args.enable else (False if args.disable else None)
        try:
            entry = portfolio.update_org(args.org, name=args.name or "",
                                         charter=args.charter or "", enabled=enabled,
                                         daily_budget_usd=args.daily_budget_usd)
        except PortfolioError as exc:
            _warn(f"cannot update the org: {exc}")
            return EXIT_CHECK_FAILED
        portfolio.save()
        if args.json:
            print(json.dumps(entry.as_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(f"updated org : {entry.name}  ({entry.slug})")
            print(f"  enabled   : {entry.enabled}")
            print(f"  budget    : "
                  + (f"${entry.daily_budget_usd:.2f}/day" if entry.daily_budget_usd else "none"))
        return EXIT_OK

    if action == "remove":
        return _portfolio_remove(args, portfolio)

    if action == "use":
        try:
            entry = portfolio.set_active(args.org)
        except PortfolioError as exc:
            _warn(f"cannot select the org: {exc}")
            return EXIT_CHECK_FAILED
        portfolio.save()
        print(f"active org : {entry.name}  ({entry.slug})")
        return EXIT_OK

    # ── the commands that read or run ───────────────────────────────────────
    if action == "show":
        try:
            entry = portfolio.org(args.org)
        except PortfolioError as exc:
            _warn(str(exc))
            return EXIT_CHECK_FAILED
        inspected = {row["id"]: row for row in portfolio.inspect()["orgs"]}[entry.id]
        config, source, _, _ = _load_stack(args)
        roster: list[dict[str, Any]] = []
        if inspected["exists"]:
            # Through the shared resolver: `entry.workspace_path` is `None` for a managed org, and
            # handing a None root to `_roster_for` read the *current directory's* roster — the engine's
            # own source tree — instead of the org's managed project. `exists` above is computed from
            # this same resolver, so the folder read here is the folder the check just approved.
            from .portfolio import workspace_for

            org = _roster_for(config, source.library,
                              workspace_for(entry, root=getattr(args, "root", None)).path)
            if org is not None:
                roster = [a for a in org.roster_view() if a.get("kind") != "human"]
        if args.json:
            print(json.dumps({**inspected, "roster": roster}, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        print(f"{entry.name}  ({entry.slug}, {entry.id})")
        print(f"  charter   : {entry.charter or '(none)'}")
        print(f"  folder    : {entry.path or '(managed, under projects/)'}")
        print(f"  exists    : {inspected['exists']}   state: {inspected['has_state']}")
        print(f"  enabled   : {entry.enabled}"
              + (f"   budget: ${entry.daily_budget_usd:.2f}/day" if entry.daily_budget_usd else ""))
        if roster:
            print(f"  agents    : {len(roster)}")
            for spec in roster[:12]:
                print(f"    {spec['name']:12s} {spec.get('title', ''):20s} "
                      f"[{', '.join(spec.get('skills') or [])}]")
        main = portfolio.active_org()
        if main is not None and main.id == entry.id:
            print("  (this is the active org)")
        return EXIT_OK

    if action in ("status",):
        return _portfolio_status(args, portfolio)

    if action in ("run", "stop"):
        return _portfolio_run(args, portfolio)

    _warn(f"unknown portfolio action {action!r}")
    return EXIT_USAGE


def _portfolio_status(args: argparse.Namespace, portfolio: Any) -> int:
    """Print the whole portfolio: every org, its mission, its spend and its blockers in one place."""
    from .fleet import Fleet
    from .portfolio import Portfolio

    live = bool(getattr(args, "live", False))
    payload: dict[str, Any]
    if live:
        config, source, _, _ = _load_stack(args)
        fleet = Fleet(config=config, library=source.library, portfolio=portfolio)
        if not portfolio.orgs:
            payload = {"rollup": portfolio.rollup(), "fleet": fleet.status()}
        else:
            # Loading every org is the *point* of `--live`: it is the "what is actually happening
            # across all my companies" view, so each org is loaded and its live picture gathered.
            for entry in portfolio.orgs:
                try:
                    fleet._runtime_for(entry.id)
                except Exception:  # noqa: BLE001 - one bad org must not blank the whole view
                    continue
            payload = {"rollup": fleet.rollup(), "fleet": fleet.status()}
    else:
        payload = {"rollup": portfolio.rollup(), "register": portfolio.inspect()}

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    rollup = payload["rollup"]
    principal = rollup["principal"]
    totals = rollup["totals"]
    print(f"principal : {principal['name']}  ({principal['id']})")
    print(f"  orgs      : {totals['orgs']}"
          + (f"   running: {totals['running']}   blocked: {totals['blocked']}"
             f"   waiting: {totals['waiting']}"))
    if live and payload.get("fleet"):
        print(f"  ceiling   : {payload['fleet'].get('max_concurrent_orgs')} org(s) at once")
    print(f"  spend     : ${totals['spend_usd']:.4f} across loaded orgs")
    print()
    if not rollup["orgs"]:
        print('  no orgs yet. Add one: engine.cli portfolio add "My Company" --path ~/code/my-company')
        return EXIT_OK
    print("Orgs:")
    for row in rollup["orgs"]:
        active = " (active)" if portfolio.active_org() and portfolio.active_org().id == row["id"] else ""
        state = row["headline"] or ("not loaded" if not row["loaded"] else "idle")
        if not row["enabled"]:
            state = "disabled"
        spend = f"${row['spend_usd']:.4f}" if isinstance(row["spend_usd"], (int, float)) else "—"
        print(f"  {row['name']:16s} {row['slug']:12s} {state:34s} {spend}{active}")
        if row.get("objective_now"):
            print(f"    on: {row['objective_now'][:88]}")
        if row.get("mission"):
            print(f"    mission: {row['mission'][:88]}")
    return EXIT_OK


def _portfolio_run(args: argparse.Namespace, portfolio: Any) -> int:
    """Start (or stop) work in one org.

    A one-shot CLI process runs in the **foreground** by default: a background thread in a process
    about to exit is a lie, since the process teardown kills it. Genuine cross-org concurrency belongs
    to the long-lived `serve` daemon (and the fleet it holds), which outlives its runs — so
    `--background` is offered but says what it is for.
    """
    from .fleet import Fleet, FleetError

    config, source, _, _ = _load_stack(args)
    # The ceiling is generous here because a one-shot command starts one org; the fleet still bounds
    # it, and the daemon's own ceiling is the one that matters for real concurrency.
    fleet = Fleet(config=config, library=source.library, portfolio=portfolio,
                  max_concurrent_orgs=max(2, len(portfolio.orgs)))

    if args.portfolio_command == "stop":
        try:
            fleet.stop_org(args.org)
        except FleetError as exc:
            _warn(str(exc))
            return EXIT_CHECK_FAILED
        print(f"{args.org}: stop requested (it will pause at its next node boundary)")
        return EXIT_OK

    goal = (args.goal or "").strip()
    if not goal and not args.manifest:
        _warn('give a goal: engine.cli portfolio run <org> "what to achieve"')
        return EXIT_USAGE
    background = bool(getattr(args, "background", False))
    try:
        handle = fleet.run_org(args.org, goal=goal, manifest=args.manifest or "",
                               background=background, executor=args.executor or "")
    except FleetError as exc:
        _warn(str(exc))
        return EXIT_CHECK_FAILED

    if background:
        if args.json:
            print(json.dumps(handle.as_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(f"{args.org}: run started  ({handle.run_id})")
            print("  note: this process will exit before it finishes; watch it with "
                  "`engine.cli status --org " + args.org + "`")
        return EXIT_OK

    fleet.wait(timeout=None)
    if args.json:
        print(json.dumps(handle.as_dict(), indent=2, sort_keys=True, default=str))
        return EXIT_OK
    print(f"{args.org}: run finished" + (f" — {handle.error}" if handle.error else ""))
    # Show where it ended up, so a person does not have to run `status` next.
    runtime = fleet.runtime(args.org)
    if runtime is not None and runtime.loaded:
        live = fleet._org_live(runtime)
        print(f"  phase     : {live.get('phase')}")
        if live.get("stop_reason"):
            print(f"  stopped   : {live['stop_reason']}")
        if live.get("objective_now"):
            print(f"  on        : {live['objective_now'][:88]}")
    return EXIT_OK


def _portfolio_remove(args: argparse.Namespace, portfolio: Any) -> int:
    """Forget an org — and say what that leaves behind in the **engine's** words, not the CLI's.

    The sentence a person reads before something that sounds destructive has to be the engine's account
    of the consequence. This printed its own ("its folder and state are untouched"), which happened to
    be true and was still a second promise: the console asks `portfolio_removal` *before* it shows its
    confirmation, precisely so the sentence agreed to is the one the engine computed — including
    `can_delete_folder`, which is false today and is the fact that decides whether the panel offers a
    delete switch at all. Two copies of "what removing does" is the same defect as two copies of a
    vocabulary, one step further into the destructive direction.

    `--preview` is the console's own first half: the account, and nothing removed. A refusal names the
    next move, which matters most here — a typo in the org reference must not read as "removed".
    """
    from .portfolio import PortfolioError
    from .serve import ServerError

    console = _console_for(args, needs_workspace=False)
    try:
        account = console._cmd_portfolio_removal({"org": args.org})
    except ServerError as exc:
        _warn(f"cannot inspect {args.org!r}: {exc}")
        return EXIT_CHECK_FAILED

    if args.json and getattr(args, "preview", False):
        print(json.dumps(account, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    if getattr(args, "preview", False):
        org = account["org"]
        sized = account.get("folder_bytes")
        print(f"would forget : {org['name']}  ({org['slug']}, {org['id']})")
        print(f"  folder      : {account['folder'] or '(managed, under projects/)'}")
        on_disk = _bytes_label(int(sized)) if sized is not None else "(nothing to size)"
        print(f"  on disk     : {on_disk}")
        if account.get("active"):
            print("  note        : this is the active org, so bare commands would have no default")
        print(f"  folder kept : {account['folder_kept']}")
        print(f"  delete      : {account['can_delete_folder']} — {account['can_delete_folder_why']}")
        print("  nothing was removed: drop --preview to actually forget it")
        return EXIT_OK

    try:
        entry = portfolio.remove_org(args.org)
    except PortfolioError as exc:
        _warn(f"cannot remove the org: {exc}")
        return EXIT_CHECK_FAILED
    portfolio.save()

    if args.json:
        print(json.dumps({**account, "removed": True}, indent=2, sort_keys=True, default=str))
        return EXIT_OK
    print(f"forgot org : {entry.name}")
    print(f"  folder     : {account['folder'] or '(managed, under projects/)'}")
    print(f"  folder kept: {account['folder_kept']} — {account['can_delete_folder_why']}")
    return EXIT_OK


def cmd_fanout(args: argparse.Namespace) -> int:
    """Split a job across agents from the command line.

    The CLI half of the fan-out primitive. `serve` exposes the same thing for the app; having it here
    too matters because the CLI is the debugging surface — a fan-out the app can run and the CLI
    cannot is one nobody can reproduce.

    Two ways in, and the second is the one this command existed without:

    - `--item` names the work directly, when you already know it.
    - `--goal` **derives** it: a lead agent explores the project, decides whether a swarm is even
      warranted, and produces the item list. That is the step that turns "improve this project" into
      a fan-out, and typing the items by hand cannot do it.
    """
    from .fanout import FanoutError, plan_fanout, run_fanout

    config, source, providers, _ = _load_stack(args)
    items = list(args.item or [])
    template = args.template

    if args.goal:
        template, items, refused = _decompose_goal(args, config, source, providers)
        if refused is not None:
            return refused
    elif not (template and items):
        # Neither path supplied: name both, because "template is required" would be misleading when
        # the correct fix is to add --item or --goal.
        _warn("give either a template with at least two --item flags, or a --goal to derive them")
        return EXIT_USAGE

    try:
        plan = plan_fanout(template, items, skill=args.skill or "code-reviewer")
    except FanoutError as exc:
        _warn(f"cannot fan out: {exc}")
        return EXIT_CHECK_FAILED

    if not providers:
        _warn("no provider could be built, so there is nothing to run")
        return EXIT_CHECK_FAILED

    # `--dry-run` shows the expanded prompts without spending anything, which is the first thing
    # anyone wants when a template is doing something unexpected.
    if args.dry_run:
        print(f"{len(plan)} item(s) from `{args.template}`")
        for item in plan.items:
            print(f"  [{item.index}] {item.prompt}")
        return EXIT_OK

    from .catalog import ModelCatalog
    from .gateway import Gateway
    from .people import HireError, People
    from .providers.base import ChatRequest, Message, Role
    from .tokens import TokenEstimator

    catalog = ModelCatalog(config, providers)
    gateway = Gateway(config, providers, catalog, estimator=TokenEstimator())
    try:
        org = People(library=source.library, config=config, catalog=catalog,
                     project=_project_root_for(args)).load(project=_project_root_for(args))
    except HireError as exc:
        _warn(f"cannot load the roster: {exc}")
        return EXIT_CHECK_FAILED
    agents = [a for a in org.agents.values() if not a.is_human and a.has_skill(plan.skill)]
    if not agents:
        _warn(f"no agent holds {plan.skill!r}, so there is nobody to fan out to")
        return EXIT_CHECK_FAILED

    def _one(item: Any, agent_id: str) -> tuple[str, str, int]:
        agent = org.get(agent_id)
        request = ChatRequest(model=agent.model,
                              messages=[Message.text_message(Role.USER, item.prompt)],
                              max_tokens=2048)
        response = gateway.complete(request, provider_id=agent.provider, agent_id=agent_id,
                                    node_id=f"fanout:{item.index}")
        usage = response.usage
        return response.text, "", (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)

    def _on_event(kind: str, event: dict[str, Any]) -> None:
        # Progress to stderr: stdout stays the answer, which is the CLI's own discipline.
        if kind in ("fanout.backpressure", "fanout.recovered", "fanout.finished"):
            _warn(f"{kind}: {event}")

    # `--parallel` wins; otherwise the configured bound, which is the same knob the executor reads.
    parallel = args.parallel or int(
        getattr(getattr(config, "executor", None), "fanout_max_parallel", 4) or 4)
    run_fanout(plan, _one, agents=[a.id for a in agents],
               max_parallel=parallel, on_event=_on_event)
    summary = plan.summary()
    if args.json:
        print(json.dumps({"summary": summary,
                          "items": [i.as_dict() for i in plan.items]},
                         indent=2, sort_keys=True, default=str))
        return EXIT_OK
    print(f"fan-out of {summary['count']}: {summary['succeeded']} succeeded"
          + (f", {summary['failed']} failed" if summary["failed"] else ""))
    for item in plan.items:
        mark = "ok  " if item.ok else "FAIL"
        print(f"  {mark} [{item.index}] {item.item}  ({item.agent_id})")
        if not item.ok:
            print(f"        {item.error[:120]}")
    return EXIT_OK


def _decompose_goal(args: argparse.Namespace, config: Any, source: Any,
                    providers: Any) -> tuple[str, list[str], int | None]:
    """Run a lead agent over a goal to derive the fan-out items.

    Returns `(template, items, exit_code)`. `exit_code` is non-None when the decomposition decided
    *not* to swarm or could not be produced, which the caller returns directly.

    This is the bridge the whole feature was missing: without it, `fanout` required the items to be
    typed, so a goal like "harden the auth flow" — where deciding the work *is* the work — could not
    produce a swarm at all.
    """
    from .agentloop import AgentLoop  # noqa: F401 - imported for the seam's type
    from .catalog import ModelCatalog
    from .decompose import DecomposeError, Decomposer
    from .gateway import Gateway
    from .people import HireError, People
    from .providers.base import ChatRequest
    from .tokens import TokenEstimator
    from .tools import ToolRegistry

    if not providers:
        _warn("no provider could be built, so a goal cannot be decomposed")
        return "", [], EXIT_CHECK_FAILED

    catalog = ModelCatalog(config, providers)
    gateway = Gateway(config, providers, catalog, estimator=TokenEstimator())
    try:
        org = People(library=source.library, config=config, catalog=catalog,
                     project=_project_root_for(args)).load(project=_project_root_for(args))
    except HireError as exc:
        _warn(f"cannot load the roster: {exc}")
        return "", [], EXIT_CHECK_FAILED

    # The lead is the most senior worker available: deciding the work is the judgement call, and the
    # capability gate applies to it exactly as it does to a worker — a lead that could read files its
    # workers cannot would plan work nobody is allowed to do.
    skill = args.skill or "code-reviewer"
    candidates = [a for a in org.agents.values() if not a.is_human and a.has_skill(skill)]
    if not candidates:
        _warn(f"no agent holds {skill!r}, so there is no lead to decompose the goal")
        return "", [], EXIT_CHECK_FAILED
    lead = max(candidates, key=lambda a: (int(a.level), a.name))

    project = _project_root(args)
    tools = ToolRegistry(workspace_root=project, agent=lead, read_only=True)

    def _complete(request: ChatRequest) -> Any:
        request.model = lead.model
        return gateway.complete(request, provider_id=lead.provider, agent_id=lead.id,
                                node_id="decompose")

    decomposer = Decomposer(complete=_complete, tools=tools,
                            max_items=args.max_items or 12,
                            on_event=lambda kind, payload: _warn(f"{kind}: {payload}")
                            if kind in ("decompose.direct", "decompose.planned") else None)
    try:
        decision = decomposer.decompose(args.goal, authored=args.procedure or "")
    except DecomposeError as exc:
        _warn(f"cannot decompose that goal: {exc}")
        return "", [], EXIT_CHECK_FAILED

    if not decision.swarm:
        # Declining is a real outcome, not a failure — and reporting it plainly is the point.
        print(f"no swarm: {decision.reason}")
        print("Run it directly instead, or name the items yourself with --item.")
        return "", [], EXIT_OK

    print(f"swarm of {len(decision.items)} on {decision.skill!r} "
          f"({decision.steps} lead step(s), looked at {len(decision.explored)} path(s))")
    print(f"  because: {decision.reason}")
    for item in decision.items:
        print(f"  - {item}")
    return decision.prompt_template, decision.items, None


def _project_root(args: argparse.Namespace) -> Path:
    """The project a command is aimed at, as a real directory."""
    from .state import Workspace

    slug = getattr(args, "slug", None) or "demo"
    workspace = _resolve_workspace(args, slug)
    return workspace.path


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the NDJSON server the native console drives.

    The console launches this at startup. Without it the app had nothing to talk to — it spawned a
    process that exited with a usage error and then waited forever, which is why the UI has always
    shown an idle engine.

    **The bootstrap is a protocol participant too.** Everything this function does *before* the server
    loop — loading the config, the library and the providers — can fail, and when it does the process
    exits with a diagnostic on *stderr*. The app reads stdout, so it never saw the reason and could
    only report "the engine exited with status 1". So a fatal bootstrap failure is also written to
    **stdout as a typed `error` frame**, which is the one channel the app is guaranteed to read. The
    frame is emitted before the process exits, so the console can show *why* rather than that it died.
    """
    try:
        config, source, providers, _ = _load_stack(args)
    except SystemExit as exc:
        # The shared prologue refuses with a diagnostic on stderr and raises `SystemExit from exc`, so
        # the real reason is on the `__cause__`. Carrying it into the frame is the difference between
        # "the engine died" and "concurrency.per_provider_limits names unknown providers: anthropic".
        reason = str(exc.__cause__) if exc.__cause__ is not None else ""
        if reason:
            message = f"the engine could not start: {reason}"
        else:
            message = ("the engine could not start because its configuration is not usable. "
                       "Run `engine.cli doctor` to see every failing check.")
        _emit_bootstrap_error(message)
        raise exc
    except Exception as exc:  # noqa: BLE001 - any bootstrap failure must reach the console
        _emit_bootstrap_error(f"the engine could not start: {exc}")
        raise SystemExit(EXIT_CHECK_FAILED) from exc

    if not providers:
        message = ("no provider could be built from the configuration, so there is nothing to run. "
                   "Add a provider with a key in the console's Providers tab, or set the environment "
                   "variable it names.")
        _warn(message)
        _emit_bootstrap_error(message)
        return EXIT_CHECK_FAILED
    from .serve import serve

    return serve(config=config, library=source.library, project=args.slug or "console",
                 root=getattr(args, "root", None), project_dir=getattr(args, "project", None))


def _emit_bootstrap_error(message: str) -> None:
    """Write one typed `error` frame to stdout for a failure that happens before the server loop.

    stdout is normally reserved for the answer, and for `serve` the answer *is* the protocol stream —
    so a startup failure belongs on it. This is the only place a non-`serve`-loop function writes a
    frame, and it is deliberate: a console that cannot tell why the engine died is a console that shows
    a healthy engine doing nothing, which is exactly the bug this exists to fix.

    Never raises: the process is on its way out, and a failure to *report* a failure must not replace
    the real reason with a traceback.
    """
    try:
        from .protocol import Event, EventType

        frame = Event(seq=0, type=EventType.ERROR,
                      payload={"message": message, "retryable": False, "fatal": True,
                               "phase": "bootstrap"})
        sys.stdout.write(json.dumps(frame.to_dict(), default=str) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - reporting the failure must not mask it
        pass


def cmd_chat(args: argparse.Namespace) -> int:
    """Talk to a model, or to the org, from one loop.

    The conversational front door: plain text goes to a model; `/run` hands a goal to the org and the
    gate commands resolve it without leaving the chat. Every call goes through the Gateway, so the
    provider is a switch and not a code path.
    """
    from .chat import ChatError, ChatSession
    from .catalog import ModelCatalog
    from .gateway import Gateway
    from .tokens import TokenEstimator

    config, source, providers, _ = _load_stack(args)
    if not providers:
        _warn("no provider could be built, so there is nothing to talk to")
        return EXIT_CHECK_FAILED

    catalog = ModelCatalog(config, providers)
    gateway = Gateway(config, providers, catalog, estimator=TokenEstimator())

    # The org and the orchestrator are optional: without them chat is direct-only, which is still the
    # useful half. With them, `/run` executes for real.
    orch = None
    workspace = None
    org = None
    if not args.no_org:
        try:
            workspace = _resolve_workspace(args, args.slug or "chat")
            workspace.ensure()
            # `lifecycle=` so the session gets the same hooks and notifications a `run` does. Without
            # it a configured `run.end` hook fired for `engine.cli run` and silently did not for the
            # session — which is the front door now, so the feature would have appeared broken for
            # the workflow it exists to serve. Found by configuring a hook and watching it not fire.
            bus = EventBus(run_id=f"chat_{args.slug or 'chat'}", trace_path=workspace.trace_path,
                           lifecycle=config, lifecycle_slug=workspace.display_name)
            orch = Orchestrator(config=config, library=source.library, workspace=workspace, bus=bus)
            org = orch.org
        except Exception as exc:  # noqa: BLE001 - the chat must open even if the org cannot
            _warn(f"no org available for this chat: {exc}")

    # `source.library` rather than `source`, because `/setup` and `/cache` drive the console handlers
    # and those take the pinned library handle, not the overlay.
    session = ChatSession(config=config, gateway=gateway, org=org, orchestrator=orch,
                          workspace=workspace, skills=source, catalog=catalog,
                          library=source.library)
    if args.agent and org is not None:
        session._cmd_agent(args.agent)
    if args.model:
        session._cmd_model(args.model)
    if args.message:
        # A one-shot: answer and exit, so `chat -m "…"` is scriptable.
        session._say(args.message)
        return EXIT_OK
    return session.run()


def _slug_from_goal(goal: str) -> str:
    """Derive a project slug from a goal, matching the planner's own rule."""
    import re

    if not goal:
        return "run"
    slug = re.sub(r"[^a-z0-9]+", "-", goal.strip().lower()).strip("-")[:48].strip("-")
    return slug or "run"


def Org_free():
    """An empty roster, for commands that only need the delegation configuration."""
    from .org import Org

    return Org()


# ── parser ───────────────────────────────────────────────────────────────────


def capability_help() -> str:
    """The `hire --capability` help, with the machine grants read from the config that declares them.

    **Why this is not a sentence with six names in it.** It was, and it named half of them: the list
    said `system:state, system:clipboard, system:screenshot, system:media, system:open,
    system:automation`, while `SystemConfig.CAPABILITIES` declared twelve. A person reading `--help` to
    find out what they could grant was told about six grants and no others — and the six missing ones
    included `system:softwareupdate`, the grant whose own prose calls it the heaviest here. The same
    6-of-12 staleness had already been fixed in the app's hire form; this is the other surface.

    The project grants stay written out: `read:`/`write:`/`exec:` are the sandbox's vocabulary, declared
    in no config module, and there are three of them. The `system:` half is derived, so a thirteenth
    grant is documented the moment it is declared rather than the next time someone remembers this line.
    """
    grants = ", ".join(SystemConfig.CAPABILITIES)
    return ("what this agent may reach, repeatable: read:*, write:src/**, exec:*, "
            f"{grants}. Given, it REPLACES the skill's default least-privilege set rather than adding "
            "to it, so revoking is expressible.")


# ── vocabularies the engine owns ─────────────────────────────────────────────
#
# One rule, applied six times: a flag whose values are a *closed set* the engine already declares
# reads that set rather than repeating it. `capability_help` above is the precedent and the reason —
# a help line with half a list in it is worse than no line, because it looks authoritative.
#
# The failure these prevent is not cosmetic. `--level` documented five levels while `people.LEVELS`
# resolved six (`mid` is an alias people type); `--kind` named three provider kinds in a `choices=`
# list beside `config.SUPPORTED_KINDS`; `--posture` spelled `unattended`/`supervised` out four times
# against `goal.Posture`; `mission mark` recited the five objective states and `pool list --state`
# recited four of six task states, the latter accepting any typo as an empty list. Every one of them
# is a second copy of a table the engine enforces, and a second copy is a copy that can disagree.
#
# Imported inside the functions, like `capability_help`'s own config read: `build_parser` is called by
# `doctor` and by shell completion, and those must not pay for importing the goal loop, the mission or
# the pool to describe a flag.


def posture_choices() -> list[str]:
    """The run postures, from `goal.Posture` — the enum that decides what each one means."""
    from .goal import Posture

    return [posture.value for posture in Posture]


def postures_phrase(default: str) -> str:
    """The postures as a sentence, with `default` marked as such.

    The default is the *caller's* — a flag with no default of its own hands the goal's own posture
    (`GoalPolicy`'s) and a scheduled entry its own — so it is passed rather than guessed here.
    """
    return ", ".join(f"{value} (default)" if value == default else value
                     for value in posture_choices())


def provider_kind_choices() -> list[str]:
    """The provider kinds a *write* is accepted for, from `config.SUPPORTED_KINDS`.

    That tuple and not `providers.registry.SUPPORTED_KINDS`: the registry's list carries `fake`, which
    exists so a test can inject a transport, and offering it here would invite a provider entry
    nothing can talk to.
    """
    return list(SUPPORTED_KINDS)


def level_choices() -> list[str]:
    """The skill levels, from `people.LEVELS` — the table `hire` resolves `--level` through."""
    from .people import LEVELS

    return sorted(LEVELS)


def hire_role_choices() -> list[str]:
    """The roles a hire may name, from `people.HIRE_ROLES` — the tuple `hire()` validates against."""
    from .people import HIRE_ROLES

    return list(HIRE_ROLES)


def mission_state_choices() -> list[str]:
    """The objective states, from `mission.ObjectiveState` — the enum `mission mark` writes."""
    from .mission import ObjectiveState

    return [state.value for state in ObjectiveState]


def pool_state_choices() -> list[str]:
    """The pooled-task states, from `pool.TaskState` — the class the pool itself sets them from."""
    from .pool import TaskState

    return list(TaskState.all())


def pool_priority_help() -> str:
    """The priority range and default, from the constants `pool.create` clamps with."""
    from .pool import DEFAULT_PRIORITY, MAX_PRIORITY, MIN_PRIORITY

    return (f"{MIN_PRIORITY}-{MAX_PRIORITY}, higher first (default {DEFAULT_PRIORITY})")


def build_parser() -> argparse.ArgumentParser:
    """The argument surface, with help text that explains rather than restates.

    The three global flags are accepted *before or after* the subcommand, because a user typing
    `engine.cli doctor --json` is expressing the same intent as `engine.cli --json doctor` and being
    told off for the word order is a poor experience.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="path to credentials.json (default: auto-discover)")
    common.add_argument("--library", default=argparse.SUPPRESS,
                        help="path to the Skills library root (default: auto-discover)")
    # A run compares the library against a recorded pin when one exists. This flag names that pin;
    # without it, `$AGENTORG_LIBRARY_PIN` and then `<repo>/.library-pin.json` are tried. The flag
    # exists because an unpinned checkout is the normal first state — a pin is opt-in, but once
    # recorded it is enforced, and the enforcement has to be reachable from a real command line.
    common.add_argument("--library-pin", dest="library_pin", default=argparse.SUPPRESS,
                        help="path to a recorded library pin (default: $AGENTORG_LIBRARY_PIN, "
                             "then <engine repo>/.library-pin.json)")
    # `default=argparse.SUPPRESS` is load-bearing. With a normal default, the subparser's own copy of
    # this flag would set `False` and silently overwrite a `--json` given before the subcommand,
    # which is the classic argparse subparser-default trap.
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    # Attach an existing project folder instead of a managed `projects/<slug>` workspace. Offered on
    # the shared parent so every subcommand that reaches the filesystem can be aimed at your real
    # repository; a command that does not resolve a workspace simply ignores it.
    common.add_argument("--project", default=argparse.SUPPRESS,
                        help="attach an existing project folder (state goes in <folder>/.agent_state/)")
    # `--org` selects which org in the portfolio a command acts on, so one principal can run several
    # orgs from one CLI. It resolves the org's folder the same way `--project` names a folder, which
    # is what keeps every existing command org-scoped without a second set of commands.
    common.add_argument("--org", default=argparse.SUPPRESS,
                        help="act on this org from the portfolio (by name, slug or id)")

    parser = argparse.ArgumentParser(
        prog="engine.cli",
        parents=[common],
        description=(
            "AgentOrg engine — diagnose the environment, inspect the skill library, plan a "
            "workflow, and review the organisation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes:\n"
            "  0  success\n"
            "  1  a check failed or the command could not complete\n"
            "  2  usage error\n\n"
            "start with:  python3 -m engine.cli          (opens the session)\n"
            "or:          python3 -m engine.cli doctor   (checks the environment)"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", parents=[common],
                            help="check every precondition and say what failed")
    doctor.set_defaults(func=cmd_doctor)

    # Both reference agents ship this (`reasonix completion bash|zsh|fish`), and a forty-subcommand CLI
    # whose only discovery path is `--help` is one people use two commands of. The script is generated
    # from the parser rather than written by hand, so a renamed verb cannot keep being offered.
    completion = sub.add_parser(
        "completion", parents=[common],
        help="print a shell completion script for this command tree",
        description=("Print a completion script for bash, zsh or fish. Generated from the argument "
                     "parser itself, so it cannot offer a command that no longer exists."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("install:\n"
                "  bash : python3 -m engine.cli completion bash > /etc/bash_completion.d/engine.cli\n"
                "  zsh  : python3 -m engine.cli completion zsh  > \"${fpath[1]}/_engine.cli\"\n"
                "  fish : python3 -m engine.cli completion fish > ~/.config/fish/completions/engine.cli.fish"))
    completion.add_argument("shell", choices=list(SUPPORTED_SHELLS),
                            help="which shell to emit for")
    completion.set_defaults(func=cmd_completion)

    onboard = sub.add_parser(
        "onboard", parents=[common],
        help="the first-run path: every step, which are done, and the one command for the next")
    onboard.add_argument("--slug", help="a managed project to treat as the chosen one")
    onboard.add_argument("--root", help="projects root, so a managed project can be found")
    # The escape hatch for a script or a CI check that must not touch the network. It removes the
    # *lookup*, never the requirement: a model whose window is unknown still blocks, because an agent
    # cannot be bound to one — and the step's own `--context-window` command is the fix that needs no
    # probe. Silently passing a model the engine would refuse to hire onto is the one direction this
    # flag must not take.
    onboard.add_argument("--no-probe", action="store_true", dest="no_probe",
                         help="do not contact providers; decide from the configuration alone "
                              "(a model with no known window still blocks)")
    onboard.set_defaults(func=cmd_onboard)

    skills = sub.add_parser("skills", parents=[common], help="inspect the skill library")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    skills_list = skills_sub.add_parser("list", parents=[common],
                                        help="list skills with their contract counts")
    skills_list.add_argument("--filter", help="only names containing this text")
    skills_list.add_argument("--root", help="project root, so your own skills are listed too")
    skills_list.set_defaults(func=cmd_skills_list)
    skills_show = skills_sub.add_parser("show", parents=[common],
                                        help="show one skill's contract and checklist")
    skills_show.add_argument("name", help="skill name, e.g. code-reviewer")
    skills_show.add_argument("--root", help="project root, so your own skills are found too")
    skills_show.set_defaults(func=cmd_skills_show)
    skills_pin = skills_sub.add_parser(
        "pin", parents=[common],
        help="record the library's content pin, which every later run is checked against")
    skills_pin.add_argument("--out", help="where to write the pin (default: $AGENTORG_LIBRARY_PIN, "
                                         "then <engine repo>/.library-pin.json)")
    skills_pin.set_defaults(func=cmd_skills_pin)

    skills_graph = skills_sub.add_parser(
        "graph", parents=[common],
        help="the library's chain: dependency graph (what depends on what)")
    skills_graph.add_argument("--skill", help="show one skill's upstream/downstream")
    skills_graph.add_argument("--upstream", help="everything this skill transitively depends on")
    skills_graph.add_argument("--downstream", help="everything that consumes this skill")
    skills_graph.add_argument("--review", nargs="+", metavar="SKILL",
                              help="review a set of skills as one plan: coherence + consensus gaps")
    skills_graph.add_argument("--min-consensus", type=int, default=2, dest="min_consensus",
                              help="consensus threshold for the --review gaps (default: 2)")
    skills_graph.add_argument("--limit", type=int, default=15, help="rows to show (default: 15)")
    skills_graph.set_defaults(func=cmd_skills_graph)

    skills_new = skills_sub.add_parser("new", parents=[common],
                                       help="author a new skill into your own root")
    skills_new.add_argument("name", help="the skill name (slugged automatically)")
    skills_new.add_argument("--purpose", help="what it is for; becomes the description")
    skills_new.add_argument("--criterion", action="append",
                            help="a completion criterion (repeatable; at least one required)")
    skills_new.add_argument("--check", action="append",
                            help="a checklist item (repeatable; defaults to one per criterion)")
    skills_new.add_argument("--tag", action="append", help="a tag (repeatable)")
    skills_new.add_argument("--input", action="append", help="an input artifact type (repeatable)")
    skills_new.add_argument("--output", action="append", help="an output artifact type (repeatable)")
    skills_new.add_argument("--author", help="the author name recorded in the frontmatter")
    skills_new.add_argument("--global", dest="global_", action="store_true",
                            help="write to ~/.agentorg instead of the project")
    skills_new.add_argument("--root", help="project root to write into (default: walk up)")
    skills_new.add_argument("--force", action="store_true", help="replace an existing skill")
    skills_new.set_defaults(func=cmd_skills_new)

    models = sub.add_parser("models", parents=[common],
                            help="list models, their windows and their provenance")
    models.add_argument("--refresh", action="store_true",
                        help="re-probe providers, bypassing the cache")
    models.set_defaults(func=cmd_models)

    plan = sub.add_parser("plan", parents=[common],
                          help="turn a goal into a validated workflow manifest")
    plan.add_argument("--goal", required=True, help="what you want built")
    plan.add_argument("--slug", help="manifest name (default: derived from the goal)")
    plan.add_argument("--max-iterations", type=int, default=3, dest="max_iterations",
                      help="cap on the review-fix loop (default: 3)")
    plan.add_argument("--out", help="write the manifest as Safe YAML to this path")
    plan.set_defaults(func=cmd_plan)

    org = sub.add_parser("org", parents=[common], help="show the roster, policy and bindings")
    org.add_argument("--goal", help="also check the roster against this goal's plan")
    org.add_argument("--slug", help="manifest name used with --goal")
    org.set_defaults(func=cmd_org)

    delegation = sub.add_parser("delegation", parents=[common],
                                help="show the hiring rules and their thresholds")
    delegation.set_defaults(func=cmd_delegation)

    run = sub.add_parser("run", parents=[common],
                         help="plan and execute a goal, or execute an existing manifest")
    run.add_argument("--goal", help="what you want built (plans the graph)")
    run.add_argument("--manifest", help="an existing manifest to adopt and run")
    run.add_argument("--approve-plan", action="store_true", dest="approve_plan",
                     help="approve the plan `run --dry-run` left parked and execute it; "
                          "name it with --slug")
    run.add_argument("--slug", help="project name (default: derived from the goal or filename)")
    run.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    run.add_argument("--max-iterations", type=int, default=3, dest="max_iterations",
                     help="cap on the review-fix loop (default: 3)")
    run.add_argument("--executor", help="override the executor plugin path (testing)")
    run.add_argument("--posture", choices=posture_choices(),
                     help=f"how far this run's goal may go on its own: "
                          f"{postures_phrase(GoalConfig().default_posture)} — unattended lets it "
                          "answer its own gates and finish alone; supervised parks at every gate")
    run.add_argument("--dry-run", action="store_true", dest="dry_run",
                     help="plan and bind, but do not execute")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", parents=[common],
                            help="show where a run is, without changing anything")
    status.add_argument("--slug", help="project name (optional with --project, which names it)")
    status.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    status.set_defaults(func=cmd_status)

    activity = sub.add_parser(
        "activity", parents=[common],
        help="what is happening: one timeline of the goal, run, nodes, swarms and why it stopped")
    activity.add_argument("--slug", help="project name (optional with --project, which names it)")
    activity.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    activity.add_argument("--limit", type=int, default=40,
                          help="how many timeline entries to show (default: 40)")
    activity.set_defaults(func=cmd_activity)

    attention = sub.add_parser(
        "attention", parents=[common],
        help="every workspace that needs you, and the command that resolves each one")
    attention.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    attention.set_defaults(func=cmd_attention)

    flow = sub.add_parser(
        "flow", parents=[common],
        help="the org board: which agent has which work, what crossed between them, and what came back")
    flow.add_argument("--slug", help="project name (optional with --project, which names it)")
    flow.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    flow.add_argument("--limit", type=int, default=800,
                      help="how much of the trace to read (default: 800 lines)")
    flow.set_defaults(func=cmd_flow)

    decide = sub.add_parser("decide", parents=[common],
                            help="resolve a gate: approve and continue, or reject and park")
    decide.add_argument("--slug", help="project name (optional with --project, which names it)")
    decide.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    decide.add_argument("--approve", action="store_true", help="approve and continue")
    decide.add_argument("--reject", action="store_true", help="reject and park")
    decide.add_argument("--note", help="why; a rejection records it so agents do not retry blindly")
    decide.add_argument("--no-continue", action="store_true", dest="no_continue",
                        help="clear the gate but do not resume the run (inspect before spending)")
    decide.add_argument("--executor", help="override the executor plugin path (testing)")
    decide.set_defaults(func=cmd_decide)

    instruct = sub.add_parser("instruct", parents=[common],
                              help="push guidance or a constraint into a run")
    instruct.add_argument("text", help="the guidance")
    instruct.add_argument("--slug", help="project name (optional with --project, which names it)")
    instruct.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    instruct.add_argument("--constraint", action="store_true",
                          help="make it non-negotiable, so it survives compaction and rotation")
    instruct.set_defaults(func=cmd_instruct)

    abort = sub.add_parser(
        "abort", parents=[common],
        help="STOP the run for good, keeping its checkpoint — not `decide`, which answers a gate and "
             "lets the run carry on spending")
    abort.add_argument("--slug", help="project name (optional with --project, which names it)")
    abort.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    abort.set_defaults(func=cmd_abort)

    pause = sub.add_parser(
        "pause", parents=[common],
        help="park the run at its next node boundary, keeping its checkpoint — `resume` continues "
             "it, `abort` ends it")
    pause.add_argument("--slug", help="project name (optional with --project, which names it)")
    pause.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser(
        "resume", parents=[common],
        help="continue a parked run from its checkpoint, and carry the work on")
    resume.add_argument("--slug", help="project name (optional with --project, which names it)")
    resume.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    resume.add_argument("--no-execute", action="store_true", dest="no_execute",
                        help="clear the pause and stop there, so the run is ready to inspect before "
                             "anything is spent")
    resume.add_argument("--executor", help="override the executor plugin path (testing)")
    resume.set_defaults(func=cmd_resume)

    reassign = sub.add_parser(
        "reassign", parents=[common],
        help="pin a node to a different agent instead of letting the router choose")
    reassign.add_argument("node", help="the node id in this run's plan")
    reassign.add_argument("--agent", required=True,
                          help="the agent to pin it to, by name or id (`agents` lists both)")
    reassign.add_argument("--slug", help="project name (optional with --project, which names it)")
    reassign.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    reassign.set_defaults(func=cmd_reassign)

    takeover = sub.add_parser(
        "takeover", parents=[common],
        help="take a node over yourself, so its artifact records a human producer")
    takeover.add_argument("node", help="the node id in this run's plan")
    takeover.add_argument("--slug", help="project name (optional with --project, which names it)")
    takeover.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    takeover.set_defaults(func=cmd_takeover)

    discard = sub.add_parser(
        "discard", parents=[common],
        help="clear a *settled* run so the board stops reporting it — moves the two checkpoints to "
             ".agent_state/discarded/, never deletes, and is not `abort` (which stops a live run)")
    discard.add_argument("--slug", help="project name (optional with --project, which names it)")
    discard.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    discard.add_argument("--include-record", action="store_true", dest="include_record",
                         help="also move the trace, handoffs, ledger, goal and cache — the record of "
                              "the run. Off by default, because that record is what is kept")
    discard.set_defaults(func=cmd_discard)

    subagents = sub.add_parser(
        "subagents", parents=[common],
        help="the children a run started, and one child's transcript read a page at a time")
    subagents_sub = subagents.add_subparsers(dest="subagents_command", required=True)

    subagents_list = subagents_sub.add_parser(
        "list", parents=[common],
        help="every child this run started, as the bounded frame its parent saw")
    subagents_list.add_argument("--slug", help="project name (optional with --project)")
    subagents_list.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    subagents_list.set_defaults(func=cmd_subagents)

    subagents_result = subagents_sub.add_parser(
        "result", parents=[common],
        help="read one child's transcript by byte range — the same page the agent's own tool returns")
    subagents_result.add_argument("child_id", help="the child id from `subagents list`")
    subagents_result.add_argument("--offset", type=int,
                                  help="byte offset to read from (default: 0)")
    subagents_result.add_argument("--limit", type=int,
                                  help="bytes to read (clamped by the engine, not unbounded)")
    subagents_result.add_argument("--slug", help="project name (optional with --project)")
    subagents_result.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    subagents_result.set_defaults(func=cmd_subagents)

    goal = sub.add_parser("goal", parents=[common],
                          help="set, inspect, pause, resume or clear the durable goal (the loop "
                               "that continues past a model finishing)")
    goal_sub = goal.add_subparsers(dest="goal_command", required=True)

    goal_set = goal_sub.add_parser("set", parents=[common],
                                   help="set the objective and arm the loop")
    goal_set.add_argument("objective", nargs="+", help="what should be achieved")
    goal_set.add_argument("--no-arm", action="store_true", dest="no_arm",
                          help="record the objective but do not start working on it")
    goal_set.add_argument("--posture", choices=posture_choices(),
                          help=f"how far this goal may go on its own: "
                               f"{postures_phrase(GoalConfig().default_posture)} — unattended lets "
                               "it answer its own gates and can finish alone; supervised waits for "
                               "you at every one")
    goal_set.add_argument("--human-gate", action="store_true", dest="human_gate",
                          help="stop at every gate: you decide, the org does not "
                               "(same as --posture supervised)")
    goal_set.add_argument("--auto-approve", action="store_true", default=None, dest="auto_approve",
                          help="pass gates the org can decide (the default)")
    goal_set.add_argument("--no-auto-approve", action="store_false", default=None,
                          dest="auto_approve", help="wait at every gate")
    goal_set.add_argument("--auto-hire", action="store_true", default=None, dest="auto_hire",
                          help="create a helper on the default model when a skill is missing")
    goal_set.add_argument("--no-auto-hire", action="store_false", default=None, dest="auto_hire",
                          help="report the staffing gap instead of filling it")
    goal_set.add_argument("--persist-hires", action="store_true", default=None,
                          dest="persist_hires",
                          help="write auto-created helpers to the roster so they persist")
    goal_set.add_argument("--slug", help="project name (optional with --project)")
    goal_set.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    goal_set.set_defaults(func=cmd_goal)

    goal_status = goal_sub.add_parser("status", parents=[common],
                                      help="show the goal, its state and what it has spent")
    goal_status.add_argument("--slug", help="project name (optional with --project)")
    goal_status.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    goal_status.set_defaults(func=cmd_goal)

    goal_pause = goal_sub.add_parser("pause", parents=[common],
                                     help="stop the loop, keeping the objective")
    goal_pause.add_argument("--slug", help="project name (optional with --project)")
    goal_pause.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    goal_pause.set_defaults(func=cmd_goal)

    goal_resume = goal_sub.add_parser("resume", parents=[common],
                                      help="continue, granting a fresh budget slice")
    goal_resume.add_argument("--slug", help="project name (optional with --project)")
    goal_resume.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    goal_resume.set_defaults(func=cmd_goal)

    goal_clear = goal_sub.add_parser("clear", parents=[common],
                                     help="forget the objective, keeping the spend history")
    goal_clear.add_argument("--slug", help="project name (optional with --project)")
    goal_clear.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    goal_clear.set_defaults(func=cmd_goal)

    mission = sub.add_parser(
        "mission", parents=[common],
        help="the standing purpose above the goal: an ordered set of objectives worked one at a time")
    mission_sub = mission.add_subparsers(dest="mission_command", required=True)

    mission_set = mission_sub.add_parser("set", parents=[common],
                                         help="state the mission, with optional objectives")
    mission_set.add_argument("statement", nargs="+", help="the standing purpose, in words")
    mission_set.add_argument("--objective", action="append", default=[],
                             help="an objective, in order (repeatable)")
    mission_set.add_argument("--arm", action="store_true",
                             help="start working it (does not spend; `mission start` does)")
    mission_set.add_argument("--slug", help="project name (optional with --project)")
    mission_set.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    mission_set.set_defaults(func=cmd_mission)

    mission_status = mission_sub.add_parser("status", parents=[common],
                                            help="show the mission, its objectives and progress")
    mission_status.add_argument("--slug", help="project name (optional with --project)")
    mission_status.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    mission_status.set_defaults(func=cmd_mission)

    mission_add = mission_sub.add_parser("add", parents=[common],
                                         help="add an objective (appended, or at --at)")
    mission_add.add_argument("objective", nargs="+", help="the objective")
    mission_add.add_argument("--at", type=int, help="insert at this position")
    mission_add.add_argument("--slug", help="project name (optional with --project)")
    mission_add.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    mission_add.set_defaults(func=cmd_mission)

    mission_remove = mission_sub.add_parser("remove", parents=[common],
                                            help="remove an objective by position")
    mission_remove.add_argument("index", type=int, help="the objective's position (#0-based)")
    mission_remove.add_argument("--slug", help="project name (optional with --project)")
    mission_remove.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    mission_remove.set_defaults(func=cmd_mission)

    mission_arm = mission_sub.add_parser("arm", parents=[common],
                                         help="start working the mission (does not spend)")
    mission_arm.add_argument("--slug", help="project name (optional with --project)")
    mission_arm.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    mission_arm.set_defaults(func=cmd_mission)

    mission_pause = mission_sub.add_parser("pause", parents=[common],
                                           help="stop working the mission, keeping its objectives")
    mission_pause.add_argument("--slug", help="project name (optional with --project)")
    mission_pause.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    mission_pause.set_defaults(func=cmd_mission)

    mission_start = mission_sub.add_parser(
        "start", parents=[common],
        help="hand the active objective to a goal (and arm it), so the mission does real work")
    mission_start.add_argument("--index", type=int, help="which objective to start")
    mission_start.add_argument("--no-arm", action="store_true", dest="no_arm",
                               help="set the goal but do not start spending")
    mission_start.add_argument("--slug", help="project name (optional with --project)")
    mission_start.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    mission_start.set_defaults(func=cmd_mission)

    mission_advance = mission_sub.add_parser("advance", parents=[common],
                                             help="finish the active objective and move to the next")
    mission_advance.add_argument("--summary", help="what the finished objective produced")
    mission_advance.add_argument("--slug", help="project name (optional with --project)")
    mission_advance.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    mission_advance.set_defaults(func=cmd_mission)

    mission_mark = mission_sub.add_parser("mark", parents=[common],
                                          help="set an objective's state "
                                               f"({'|'.join(mission_state_choices())})")
    mission_mark.add_argument("index", type=int, help="the objective's position (#0-based)")
    mission_mark.add_argument("state", choices=mission_state_choices(),
                              help=f"one of: {', '.join(mission_state_choices())}")
    mission_mark.add_argument("--summary", help="what it produced, or why it is blocked")
    mission_mark.add_argument("--slug", help="project name (optional with --project)")
    mission_mark.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    mission_mark.set_defaults(func=cmd_mission)

    mission_clear = mission_sub.add_parser("clear", parents=[common],
                                           help="forget the mission, keeping its history")
    mission_clear.add_argument("--slug", help="project name (optional with --project)")
    mission_clear.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    mission_clear.set_defaults(func=cmd_mission)

    chat = sub.add_parser("chat", parents=[common],
                          help="talk to a model, or to the org, from one conversational loop")
    chat.add_argument("-m", "--message", help="send one message and exit (scriptable one-shot)")
    chat.add_argument("--agent", help="answer as a named agent from the roster")
    chat.add_argument("--model", help="start on a specific provider/model (e.g. ollama/qwen2.5-coder:14b)")
    chat.add_argument("--slug", help="project name for /run (default: chat)")
    chat.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    chat.add_argument("--no-org", action="store_true",
                      help="direct model chat only; no roster and no /run")
    chat.set_defaults(func=cmd_chat)

    serve_cmd = sub.add_parser("serve", parents=[common],
                               help="run the NDJSON server the native console drives")
    serve_cmd.add_argument("--slug", help="project name (default: console)")
    serve_cmd.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    serve_cmd.set_defaults(func=cmd_serve)

    fanout = sub.add_parser("fanout", parents=[common],
                            help="split a job across agents, from items or from a goal")
    fanout.add_argument("template", nargs="?",
                        help="the prompt template, containing {{item}} (omit when using --goal)")
    fanout.add_argument("--item", action="append",
                        help="an item to expand the template over (repeatable, at least two)")
    fanout.add_argument("--goal",
                        help="let a lead agent derive the items: it explores the project, decides "
                             "whether a swarm is warranted, and produces the list")
    fanout.add_argument("--procedure", help="shared context for --goal (an SOP or house rules)")
    fanout.add_argument("--max-items", type=int, help="ceiling on a decomposed swarm (default 12)")
    fanout.add_argument("--skill", help="which skill's holders should do the work")
    fanout.add_argument("--slug", help="project name, so --goal explores the right folder")
    fanout.add_argument("--parallel", type=int, help="max concurrent subagents")
    fanout.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    fanout.add_argument("--dry-run", action="store_true", help="show the expanded prompts only")
    fanout.set_defaults(func=cmd_fanout)

    defaults_cmd = sub.add_parser(
        "defaults", parents=[common],
        help="the provider and model everyone uses unless you say otherwise — and how autonomous "
             "a goal is by default")
    # `required=True` so a bare `defaults` shows the answer rather than crashing on a missing `func`.
    # It used to be optional, so plain `engine.cli defaults` raised `AttributeError: no attribute
    # 'func'` — the exact "nothing works" symptom, on the very command that says which model everyone
    # runs on. `show` is the default action, which is what a person typing `defaults` means.
    defaults_sub = defaults_cmd.add_subparsers(dest="defaults_command")
    defaults_cmd.set_defaults(func=cmd_defaults)

    defaults_show = defaults_sub.add_parser("show", parents=[common],
                                            help="show the effective default and why")
    defaults_show.set_defaults(func=cmd_defaults)

    defaults_set = defaults_sub.add_parser("set", parents=[common],
                                           help="set the default provider and/or model")
    defaults_set.add_argument("--provider", help="the provider id everyone uses by default")
    defaults_set.add_argument("--model", help="the model id everyone uses by default")
    defaults_set.add_argument("--reviewer-provider",
                              help="the provider reviewers run on (keeps them independent)")
    defaults_set.add_argument("--reviewer-model", help="the model reviewers run on")
    defaults_set.add_argument("--context-window", type=int,
                              help="declare the window when the provider cannot report it")
    defaults_set.set_defaults(func=cmd_defaults)

    defaults_autonomy = defaults_sub.add_parser(
        "autonomy", parents=[common],
        help="how autonomous a new goal is: whether it passes gates and staffs its own gaps")
    defaults_autonomy.add_argument("--auto-gates", action="store_true", default=None,
                                   dest="auto_gates",
                                   help="a goal passes gates the org can decide (the default)")
    defaults_autonomy.add_argument("--no-auto-gates", action="store_false", default=None,
                                   dest="auto_gates",
                                   help="every goal waits for you at a gate")
    defaults_autonomy.add_argument("--auto-hire", action="store_true", default=None,
                                   dest="auto_hire",
                                   help="a goal closes a staffing gap with a helper (the default)")
    defaults_autonomy.add_argument("--no-auto-hire", action="store_false", default=None,
                                   dest="auto_hire",
                                   help="a goal parks and reports the gap instead")
    defaults_autonomy.add_argument("--persist-hires", action="store_true", default=None,
                                   dest="persist_hires",
                                   help="write auto-created helpers to the roster")
    defaults_autonomy.add_argument("--ephemeral-hires", action="store_false", default=None,
                                   dest="persist_hires",
                                   help="auto-created helpers are temporary (the default)")
    defaults_autonomy.add_argument("--max-tier", type=int, default=None, dest="max_tier",
                                   help="highest delegation tier an auto-hire may reach (0-2)")
    defaults_autonomy.add_argument("--posture", choices=posture_choices(),
                                   help=f"the posture a new goal inherits: "
                                        f"{postures_phrase(GoalConfig().default_posture)} — "
                                        "unattended lets it answer its own gates and finish alone; "
                                        "supervised waits for you at every one")
    defaults_autonomy.set_defaults(func=cmd_defaults)

    hire = sub.add_parser("hire", parents=[common],
                          help="hire an agent into the roster")
    hire.add_argument("name", help="the agent's name (must be unique)")
    hire.add_argument("--skill", required=True, help="the skill it is bound to (e.g. security-reviewer)")
    hire.add_argument("--provider", help="provider id (default: the configured default)")
    hire.add_argument("--model", help="model id (default: the configured default)")
    hire.add_argument("--context-window", type=int, help="override the probed context window")
    hire.add_argument("--level", choices=level_choices(),
                      help=f"one of: {', '.join(level_choices())} "
                           "(default: inferred from the skill)")
    hire.add_argument("--role", choices=hire_role_choices(),
                      help=f"one of: {', '.join(hire_role_choices())} "
                           "(default: inferred from the skill)")
    hire.add_argument("--title", help="a human title for the roster (default: derived from the skill)")
    hire.add_argument("--team", help="the team it joins")
    hire.add_argument("--concurrency", type=int, help="how many tasks it may run at once")
    hire.add_argument("--capability", action="append", dest="capabilities", metavar="GRANT",
                      help=capability_help())
    hire.add_argument("--root", help="project root to find .agentorg in (default: walk up)")
    hire.add_argument("--roster-root", help="write the roster here (default: the project root)")
    hire.set_defaults(func=cmd_hire)

    agents = sub.add_parser("agents", parents=[common],
                            help="list the effective roster: built-ins plus every hire")
    agents.add_argument("--root", help="project root to find .agentorg in (default: walk up)")
    agents.set_defaults(func=cmd_agents)

    agent = sub.add_parser("agent", parents=[common],
                           help="change the roster the console edits: update or retire one agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)

    agent_update = agent_sub.add_parser(
        "update", parents=[common],
        help="change an agent's name, model, level or limits, keeping its id and its history")
    agent_update.add_argument("agent", help="the agent's name or id (`agents` lists both)")
    agent_update.add_argument("--name", help="a new name (must be unique in the roster)")
    agent_update.add_argument("--provider", help="move it to this provider")
    agent_update.add_argument("--model", help="move it to this model")
    agent_update.add_argument("--context-window", type=int, dest="context_window",
                              help="an explicit window, when the provider cannot report one")
    agent_update.add_argument("--level", choices=level_choices(),
                              help=f"one of: {', '.join(level_choices())}")
    agent_update.add_argument("--title", help="a human title for the roster")
    agent_update.add_argument("--team", help="the team it joins (empty moves it to none)")
    agent_update.add_argument("--concurrency", type=int,
                              help="how many tasks it may run at once")
    agent_update.add_argument("--root", help="project root to find .agentorg in (default: walk up)")
    agent_update.add_argument("--roster-root", help="write the roster here (default: the project root)")
    agent_update.set_defaults(func=cmd_agent_update)

    agent_retire = agent_sub.add_parser(
        "retire", parents=[common],
        help="remove an agent from the roster, keeping what it produced")
    agent_retire.add_argument("agent", help="the agent's name or id (`agents` lists both)")
    agent_retire.add_argument("--reason", help="why; recorded with the termination")
    agent_retire.add_argument("--root", help="project root to find .agentorg in (default: walk up)")
    agent_retire.add_argument("--roster-root", help="write the roster here (default: the project root)")
    agent_retire.set_defaults(func=cmd_agent_retire)

    providers = sub.add_parser(
        "providers", parents=[common],
        help="list, add, test or remove provider endpoints — the same edits the console makes")
    providers_sub = providers.add_subparsers(dest="providers_command", required=True)

    providers_list = providers_sub.add_parser(
        "list", parents=[common],
        help="every configured provider, whether it is reachable, and how many models it offers")
    providers_list.add_argument("--slug", help="project name, used only to locate the workspace")
    providers_list.set_defaults(func=cmd_providers)

    providers_add = providers_sub.add_parser(
        "add", parents=[common],
        help="add or replace one provider in credentials.json, then re-read it in place")
    providers_add.add_argument("provider_id", help="the provider id, e.g. groq")
    providers_add.add_argument("--kind", choices=provider_kind_choices(),
                               help=f"the protocol it speaks "
                                    f"(default: {SUPPORTED_KINDS[0]})")
    providers_add.add_argument("--base-url", required=True, dest="base_url",
                               help="the API base, e.g. https://api.groq.com/openai/v1 — a base is "
                                    "the part before the operation, so a pasted full endpoint is "
                                    "reduced to one and you are told")
    providers_add.add_argument("--key-env", dest="key_env",
                               help="the environment variable holding the key (preferred: it stays "
                                    "out of the file)")
    providers_add.add_argument("--key", help="the key itself, when there is no variable to use")
    providers_add.add_argument("--api-version", dest="api_version",
                               help="the API version header, for endpoints that need one")
    providers_add.add_argument("--timeout-s", type=float, dest="timeout_s",
                               help="request timeout in seconds (default: 120)")
    providers_add.add_argument("--max-retries", type=int, dest="max_retries",
                               help="retries before a call fails (default: 3)")
    providers_add.add_argument("--concurrency", type=int,
                               help="how many requests this endpoint may serve at once")
    providers_add.add_argument("--slug", help="project name, used only to locate the workspace")
    providers_add.set_defaults(func=cmd_providers)

    providers_test = providers_sub.add_parser(
        "test", parents=[common],
        help="probe an entry before it is saved, and list the models it offers")
    providers_test.add_argument("provider_id", help="the provider id to test")
    providers_test.add_argument("--kind", choices=provider_kind_choices(),
                                help=f"the protocol it speaks "
                                     f"(default: {SUPPORTED_KINDS[0]})")
    providers_test.add_argument("--base-url", required=True, dest="base_url",
                                help="the API base to probe")
    providers_test.add_argument("--key-env", dest="key_env", help="the environment variable holding it")
    providers_test.add_argument("--key", help="the key itself")
    providers_test.add_argument("--api-version", dest="api_version", help="the API version header")
    providers_test.add_argument("--timeout-s", type=float, dest="timeout_s", help="request timeout")
    providers_test.add_argument("--max-retries", type=int, dest="max_retries", help="retries")
    providers_test.add_argument("--concurrency", type=int, help="concurrent requests")
    providers_test.add_argument("--slug", help="project name, used only to locate the workspace")
    providers_test.set_defaults(func=cmd_providers)

    providers_remove = providers_sub.add_parser(
        "remove", parents=[common],
        help="remove one provider, pruning the defaults and limits that named it")
    providers_remove.add_argument("provider_id", help="the provider id to remove")
    providers_remove.add_argument("--slug", help="project name, used only to locate the workspace")
    providers_remove.set_defaults(func=cmd_providers)

    portfolio = sub.add_parser(
        "portfolio", parents=[common],
        help="the principal and the several orgs they run (one person, many organisations)")
    portfolio_sub = portfolio.add_subparsers(dest="portfolio_command", required=True)

    portfolio_set = portfolio_sub.add_parser("init", parents=[common],
                                             help="set the principal (the person)")
    portfolio_set.add_argument("name", help="the principal's name")
    portfolio_set.set_defaults(func=cmd_portfolio)

    portfolio_status = portfolio_sub.add_parser("status", parents=[common],
                                                help="the portfolio: every org and what it is doing")
    portfolio_status.add_argument("--live", action="store_true",
                                  help="load every org to show live mission, spend and blockers")
    portfolio_status.set_defaults(func=cmd_portfolio)

    portfolio_add = portfolio_sub.add_parser("add", parents=[common],
                                             help="register an org the principal runs")
    portfolio_add.add_argument("name", help="the org's name, e.g. Tesla")
    portfolio_add.add_argument("--slug", help="a stable slug (default: from the name)")
    portfolio_add.add_argument("--path", help="the org's project folder")
    portfolio_add.add_argument("--charter", help="what this org is for, in your words")
    portfolio_add.add_argument("--daily-budget-usd", type=float, default=0.0,
                               dest="daily_budget_usd", help="a per-org daily spend ceiling")
    portfolio_add.add_argument("--active", action="store_true",
                               help="make this the default org for bare commands")
    portfolio_add.set_defaults(func=cmd_portfolio)

    portfolio_use = portfolio_sub.add_parser("use", parents=[common],
                                             help="select the default org")
    portfolio_use.add_argument("org", help="org name, slug or id")
    portfolio_use.set_defaults(func=cmd_portfolio)

    portfolio_update = portfolio_sub.add_parser("update", parents=[common],
                                                help="edit an org's label, charter, budget or enabled")
    portfolio_update.add_argument("org", help="org name, slug or id")
    portfolio_update.add_argument("--name", help="a new display name")
    portfolio_update.add_argument("--charter", help="what this org is for")
    portfolio_update.add_argument("--daily-budget-usd", type=float, dest="daily_budget_usd",
                                  help="a per-org daily spend ceiling")
    portfolio_update.add_argument("--enable", action="store_true", help="include this org in runs")
    portfolio_update.add_argument("--disable", action="store_true", help="exclude it from runs")
    portfolio_update.set_defaults(func=cmd_portfolio)

    portfolio_remove = portfolio_sub.add_parser("remove", parents=[common],
                                                help="forget an org (its folder is left alone)")
    portfolio_remove.add_argument("org", help="org name, slug or id")
    portfolio_remove.add_argument("--preview", action="store_true",
                                  help="the engine's account of what forgetting it takes away and "
                                       "what it leaves behind, and remove nothing")
    portfolio_remove.set_defaults(func=cmd_portfolio)

    portfolio_show = portfolio_sub.add_parser("show", parents=[common],
                                              help="one org: its folder, roster and live state")
    portfolio_show.add_argument("org", help="org name, slug or id")
    portfolio_show.set_defaults(func=cmd_portfolio)

    portfolio_run = portfolio_sub.add_parser(
        "run", parents=[common],
        help="start work in an org")
    portfolio_run.add_argument("org", help="org name, slug or id")
    portfolio_run.add_argument("goal", nargs="?", help="what the org should achieve")
    portfolio_run.add_argument("--manifest", help="an existing manifest to adopt instead of a goal")
    portfolio_run.add_argument("--background", action="store_true",
                               help="return immediately (only useful inside the serve daemon, "
                                    "which outlives the run; a one-shot CLI process does not)")
    portfolio_run.add_argument("--executor", help="override the executor plugin path (testing)")
    portfolio_run.set_defaults(func=cmd_portfolio)

    portfolio_stop = portfolio_sub.add_parser("stop", parents=[common],
                                              help="ask one org's run to stop at its next boundary")
    portfolio_stop.add_argument("org", help="org name, slug or id")
    portfolio_stop.set_defaults(func=cmd_portfolio)

    propose = sub.add_parser(
        "propose", parents=[common],
        help="find the engine's own defects and write proposals (never applies anything)")
    propose.add_argument("--list", action="store_true",
                         help="show proposals already written, and what was refused")
    propose.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="show the findings without drafting or writing anything")
    propose.add_argument("--slug", help="project name (optional with --project)")
    propose.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    propose.set_defaults(func=cmd_propose)

    improve = sub.add_parser(
        "improve", parents=[common],
        help="run one cycle of the self-improvement loop — it drafts, proves and STOPS: nothing is "
             "applied, by any code path")
    improve.add_argument("--list", action="store_true",
                         help="show the proposals already written, and what was refused")
    improve.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="measure and list the findings without drafting anything")
    improve.add_argument("--slug", help="project name (optional with --project)")
    improve.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    improve.set_defaults(func=cmd_improve)

    proposals = sub.add_parser(
        "proposals", parents=[common],
        help="what the self-improvement loop has proposed — the loop never applies anything, and "
             "`accept`/`reject` move one forward while `apply` is a separate, re-verified step")
    proposals.add_argument("--slug", help="project name (optional with --project)")
    proposals.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    proposals.add_argument("--show", metavar="ID",
                           help="read one proposal in full: its rationale, evidence and patch")
    proposals.set_defaults(func=cmd_proposals)
    # Not `required`: a bare `proposals` is the listing, which is the commonest thing anyone types.
    proposals_sub = proposals.add_subparsers(dest="proposals_command")

    # The lifecycle, as subcommands of the listing. Deliberately *not* separate top-level nouns:
    # `decide` already owns approve/reject for gates, and a second `accept` at the top level would be
    # a second word for one idea. Under `proposals` the object is unambiguous.
    #
    # Each verb carries `--slug`/`--root` too, like every other verb in this parser (`pool list`,
    # `status`, `decide`, …): `proposals accept <id> --slug s` is the order a person types, and a flag
    # that is only legal *before* the verb is a usage error the reader cannot act on — which is what
    # this surface shipped, so its own tests could not reach a proposal at all.
    #
    # `default=argparse.SUPPRESS` is load-bearing here for the reason `common`'s own flags document:
    # with a normal default, the verb's copy of `--slug` would set `None` and silently overwrite one
    # given before the verb, breaking the order that used to work. Suppressed, the flag is only
    # written when it is actually passed, so both orders resolve to the same slug.
    proposals_accept = proposals_sub.add_parser(
        "accept", parents=[common],
        help="agree with a proposal — records the decision and applies NOTHING")
    proposals_accept.add_argument("proposal_id", help="the proposal id, e.g. prop_0001")
    proposals_accept.add_argument("--slug", default=argparse.SUPPRESS,
                                  help="project name (optional with --project)")
    proposals_accept.add_argument("--root", default=argparse.SUPPRESS,
                                  help="projects root (default: AgentOrg/projects)")
    proposals_accept.set_defaults(func=cmd_proposals)

    proposals_reject = proposals_sub.add_parser(
        "reject", parents=[common],
        help="decline a proposal; the reason is recorded so the finding is not re-drafted")
    proposals_reject.add_argument("proposal_id", help="the proposal id, e.g. prop_0001")
    proposals_reject.add_argument("--reason", default="", help="why, in your words")
    proposals_reject.add_argument("--slug", default=argparse.SUPPRESS,
                                  help="project name (optional with --project)")
    proposals_reject.add_argument("--root", default=argparse.SUPPRESS,
                                  help="projects root (default: AgentOrg/projects)")
    proposals_reject.set_defaults(func=cmd_proposals)

    # "a proposal nothing has settled yet", not "an accepted one": `promoted` — what the improver's
    # own writer leaves a validated proposal in — is applyable, because requiring an explicit `accept`
    # first would hide the button on every proposal the loop just produced. What `apply` refuses is a
    # *settled* proposal (rejected or already applied), an unvalidated one, and one with no patch.
    proposals_apply = proposals_sub.add_parser(
        "apply", parents=[common],
        help="apply a proposal the loop has validated — runs the test suite before and after and "
             "reverts on regression")
    proposals_apply.add_argument("proposal_id", help="the proposal id, e.g. prop_0001")
    proposals_apply.add_argument("--force", action="store_true",
                                 help="apply even without a demonstrated improvement (still reverts "
                                      "on a regression)")
    proposals_apply.add_argument("--slug", default=argparse.SUPPRESS,
                                 help="project name (optional with --project)")
    proposals_apply.add_argument("--root", default=argparse.SUPPRESS,
                                 help="projects root (default: AgentOrg/projects)")
    proposals_apply.set_defaults(func=cmd_proposals)

    proposals_undo = proposals_sub.add_parser(
        "undo", parents=[common],
        help="put an applied proposal's files back, from the copy taken before it was applied")
    proposals_undo.add_argument("proposal_id", help="the proposal id, e.g. prop_0001")
    proposals_undo.add_argument("--slug", default=argparse.SUPPRESS,
                                help="project name (optional with --project)")
    proposals_undo.add_argument("--root", default=argparse.SUPPRESS,
                                help="projects root (default: AgentOrg/projects)")
    proposals_undo.set_defaults(func=cmd_proposals)

    pool = sub.add_parser("pool", parents=[common],
                          help="the task pool: work agents pull, capability-routed")
    pool_sub = pool.add_subparsers(dest="pool_command", required=True)
    pool_list = pool_sub.add_parser("list", parents=[common],
                                    help="show the pool and its counts")
    pool_list.add_argument("--slug", help="project name (optional with --project, which names it)")
    pool_list.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    pool_list.add_argument("--state", choices=pool_state_choices(),
                           help=f"only tasks in this state "
                                f"({'|'.join(pool_state_choices())})")
    pool_list.set_defaults(func=cmd_pool)
    pool_add = pool_sub.add_parser("add", parents=[common], help="add claimable work")
    pool_add.add_argument("description", help="what needs doing")
    pool_add.add_argument("--slug", help="project name (optional with --project, which names it)")
    pool_add.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    pool_add.add_argument("--skill", action="append",
                          help="a skill the claimer must hold (repeatable)")
    pool_add.add_argument("--capability", action="append",
                          help="a capability the claimer must hold (repeatable)")
    pool_add.add_argument("--priority", type=int, help=pool_priority_help())
    pool_add.add_argument("--tag", action="append", help="a tag (repeatable)")
    pool_add.add_argument("--parent", help="the task this was decomposed from")
    pool_add.add_argument("--schema", help="a JSON Schema the completion's output must satisfy")
    pool_add.set_defaults(func=cmd_pool)
    pool_claim = pool_sub.add_parser("claim", parents=[common],
                                     help="have an agent take the best eligible task")
    pool_claim.add_argument("--slug", help="project name (optional with --project, which names it)")
    pool_claim.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    pool_claim.add_argument("--agent", required=True, help="which agent is claiming")
    pool_claim.add_argument("--task", help="a specific task id instead of the best eligible one")
    pool_claim.set_defaults(func=cmd_pool)

    session = sub.add_parser(
        "session", parents=[common],
        help="the sessions this root knows: list them, inspect one, export it, or branch it")
    session_sub = session.add_subparsers(dest="session_command", required=True)

    session_list = session_sub.add_parser(
        "list", parents=[common], help="every session under a projects root, newest first")
    session_list.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    session_list.set_defaults(func=cmd_session)

    session_show = session_sub.add_parser(
        "show", parents=[common],
        help="one session in full: the goal, the run, the nodes, the handoffs and the spend")
    session_show.add_argument("--slug", help="project name (optional with --project)")
    session_show.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    session_show.set_defaults(func=cmd_session)

    session_export = session_sub.add_parser(
        "export", parents=[common],
        help="write a self-contained ZIP of one session, with a manifest naming what is inside")
    session_export.add_argument("--slug", help="project name (optional with --project)")
    session_export.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    session_export.add_argument("--out", help="where to write it (default: <slug>-session.zip)")
    session_export.add_argument("--verify", action="store_true",
                                help="re-read the archive and check every member against its hash")
    session_export.set_defaults(func=cmd_session)

    session_fork = session_sub.add_parser(
        "fork", parents=[common],
        help="branch a session into a NEW slug; the original is left byte-identical")
    session_fork.add_argument("--to", required=True,
                              help="the new slug to branch into (refused if it already exists)")
    session_fork.add_argument("--slug", help="the session to branch (optional with --project)")
    session_fork.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    session_fork.set_defaults(func=cmd_session)

    schedules = sub.add_parser(
        "schedules", parents=[common],
        help="objectives that fire on a clock — and the rule that stops a failing one firing for ever")
    schedules_sub = schedules.add_subparsers(dest="schedules_command", required=True)

    schedules_list = schedules_sub.add_parser("list", parents=[common],
                                              help="the schedule, what is due and what is paused")
    schedules_list.add_argument("--slug", help="project name (optional with --project)")
    schedules_list.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    schedules_list.set_defaults(func=cmd_schedules)

    schedules_add = schedules_sub.add_parser(
        "add", parents=[common], help="schedule an objective to fire once, or on an interval")
    schedules_add.add_argument("objective", nargs="+", help="what the scheduled goal should achieve")
    schedules_add.add_argument("--every", help="repeat: 30s, 15m, 6h, 2d (a bare number is minutes)")
    schedules_add.add_argument("--at", help="fire once at a UTC ISO instant (2026-09-18T07:30:00)")
    schedules_add.add_argument("--due-now", action="store_true", dest="due_now",
                               help="fire at the watcher's next tick")
    schedules_add.add_argument("--posture", choices=posture_choices(), default=DEFAULT_POSTURE,
                               help=f"how far the scheduled goal may go on its own: "
                                    f"{postures_phrase(DEFAULT_POSTURE)} — unattended lets it answer "
                                    "its own gates and finish alone; supervised parks at every gate")
    schedules_add.add_argument("--disabled", action="store_true",
                               help="record it without arming it")
    schedules_add.add_argument("--slug", help="project name (optional with --project)")
    schedules_add.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    schedules_add.set_defaults(func=cmd_schedules)

    schedules_remove = schedules_sub.add_parser("remove", parents=[common],
                                                help="forget a scheduled entry")
    schedules_remove.add_argument("ref", help="the entry's id, or its slug")
    schedules_remove.add_argument("--slug", help="project name (optional with --project)")
    schedules_remove.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    schedules_remove.set_defaults(func=cmd_schedules)

    schedules_enable = schedules_sub.add_parser(
        "enable", parents=[common],
        help="arm an entry again (the deliberate act that undoes a no-re-arm refusal)")
    schedules_enable.add_argument("ref", help="the entry's id, or its slug")
    schedules_enable.add_argument("--slug", help="project name (optional with --project)")
    schedules_enable.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    schedules_enable.set_defaults(func=cmd_schedules)

    schedules_watch = schedules_sub.add_parser(
        "watch", parents=[common],
        help="start what is due, in the foreground, until interrupted")
    schedules_watch.add_argument("--slug", help="project name (optional with --project)")
    schedules_watch.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    schedules_watch.add_argument("--interval", type=int, default=DEFAULT_TICK_S, dest="interval",
                                 help=f"seconds between ticks, {MIN_TICK_S}-{MAX_TICK_S} "
                                      f"(default: {DEFAULT_TICK_S})")
    schedules_watch.add_argument("--ticks", type=int,
                                 help="stop after this many ticks (default: run until interrupted)")
    schedules_watch.add_argument("--max-fires", type=int, dest="max_fires",
                                 help="stop after this many runs in total")
    schedules_watch.add_argument("--executor", help="override the executor plugin path (testing)")
    schedules_watch.set_defaults(func=cmd_schedules)

    # The machine-facing commands live in their own module and take this parser's own `common` parent,
    # so a global flag added above reaches them without an edit here. Kept out of this file because
    # `cli.py` is already the largest module in the engine and every system command is a leaf that
    # delegates to `sysctl_tools` — there is no orchestration for this file to own.
    from .systemcli import install as _install_system
    _install_system(sub, common)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns an exit code; never raises for an expected failure.

    **A bare invocation opens the session.** `python3 -m engine.cli` with no argument enters the chat
    loop instead of printing a usage error, because a person who typed the program's name wants to
    use the program, not to be handed a list of 25 commands to choose from. This is a *front door*,
    not a second implementation: the arguments are those of `chat`, and every existing subcommand
    resolves through the same parser it always did, byte-for-byte.

    The injection is done by prepending the subcommand token rather than by making `command` optional,
    because `sub.add_subparsers(dest="command", required=True)` is what makes `engine.cli teleport`
    fail with a usage error — and relaxing it would let a mistyped command fall through to opening an
    interactive session, which is a worse outcome than an error message.

    **The verb index is a front door too, and it was the one door with no handle.** `--help` starts
    with a dash, so `_names_a_subcommand` answers False and the token was prepended with `chat`: the
    command everyone types to find out what exists printed *chat's* options, and `-h` did the same.
    `engine.cli help` — the other spelling of the same request — had no subcommand by that name and
    was reported as an unrecognised argument. The only honest way to discover the verbs was
    `engine.cli completion bash`, which is not a thing a person guesses. So a *leading* `-h`,
    `--help` or `help` is answered with the top-level help, and `help <verb> [<sub>]` with that
    verb's own help. The rule is deliberately about the first token: `chat --help` still describes
    chat, and a bare invocation still opens the session.
    """
    resolved = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if resolved and resolved[0] in ("-h", "--help"):
        parser.print_help()
        return EXIT_OK
    if resolved[:1] == ["help"]:
        if len(resolved) == 1:
            parser.print_help()
            return EXIT_OK
        # Translating rather than looking the parser up by hand: argparse already knows how to
        # describe a leaf, and a second traversal here would be a second description of the tree.
        resolved = [*resolved[1:], "--help"]
    if not _names_a_subcommand(parser, resolved):
        resolved = ["chat", *resolved]
    args = parser.parse_args(resolved)
    # Suppressed flags may be absent entirely; normalise so every command can read them the same way.
    for name, default in (("json", False), ("config", None), ("library", None), ("library_pin", None)):
        if not hasattr(args, name):
            setattr(args, name, default)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _warn("interrupted")
        return EXIT_CHECK_FAILED
    except BrokenPipeError:
        # A closed pipe (e.g. `| head`) is not an error worth a traceback.
        return EXIT_OK


#: The parser's own table answers "did the user name a subcommand?", rather than a second list that
#: would go stale the first time a command was added.
def _names_a_subcommand(parser: argparse.ArgumentParser, argv: list[str]) -> bool:
    """Whether an argument list names a subcommand the parser knows.

    Scanned rather than pattern-matched, because the global flags accept *values*: `--config chat`
    names a file called `chat`, and treating that value as the subcommand would open a session
    against the wrong config. Flags are recognised by their leading dashes, `--` ends the scan
    explicitly, and the first bare word decides.

    A bare word that is **not** a known command answers False — which means it is treated as no
    subcommand and the token is prepended with `chat`. argparse then reports it as an unrecognised
    positional, so `engine.cli teleport` still fails with a usage error rather than opening a session.
    """
    known = _subcommand_names(parser)
    takes_value = _value_taking_flags(parser)
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return False
        if token.startswith("-"):
            # A flag's *value* is a bare word too: `--config chat` names a file called `chat`, and
            # treating that as the subcommand would open a session against the wrong configuration.
            # `--flag=value` carries its own value, so only a separate token is skipped.
            if token in takes_value:
                index += 2
                continue
            index += 1
            continue
        return token in known
    return False


def _value_taking_flags(parser: argparse.ArgumentParser) -> set[str]:
    """The parser's own option strings that consume a following value."""
    flags: set[str] = set()
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public accessor for this
        if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction,  # noqa: SLF001
                               argparse._CountAction, argparse._HelpAction)):  # noqa: SLF001
            continue
        if getattr(action, "nargs", None) == 0:
            continue
        flags.update(action.option_strings)
    return flags


def _subcommand_names(parser: argparse.ArgumentParser) -> set[str]:
    """The parser's own subcommand names, read off the action rather than duplicated as a list."""
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public accessor for this
        choices = getattr(action, "choices", None)
        if action.dest == "command" and isinstance(choices, dict):
            return set(choices)
    return set()


if __name__ == "__main__":
    raise SystemExit(main())
