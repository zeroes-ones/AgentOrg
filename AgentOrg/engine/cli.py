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
from .config import ConfigError, load, scan_for_leaks
from .library import LibraryError, resolve
from .org import Binder, HiringDesk, PolicyResolver, Router, default_company
from .org.binding import BindingError
from .org.router import RouteContext, RouterError
from .orchestrator import Orchestrator, OrchestratorError, RunPhase
from .planner import PlanError, Planner, emit_safe_yaml
from .state import Workspace
from .providers.registry import build_providers
from .resources import derive_ceiling, detect
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
        library = resolve(getattr(args, "library", None))
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check every precondition and report what was verified.

    This is the first command to run when something is wrong, and the reason it reports each check
    individually is that "it does not work" is not a diagnosis.
    """
    checks: list[dict[str, Any]] = []
    failures = 0

    # 1. Configuration.
    try:
        config = load(getattr(args, "config", None))
        checks.append({
            "check": "configuration",
            "ok": True,
            "detail": f"{config.path} with {len(config.providers)} providers",
            "warnings": list(config.raw.get("_warnings") or []),
        })
    except ConfigError as exc:
        checks.append({"check": "configuration", "ok": False, "detail": str(exc)})
        # A failure is a diagnostic, so it goes to stderr with the rest of them. stdout stays
        # reserved for the answer, which is what makes `--json | tool` reliable.
        _warn(f"FAIL configuration\n  {exc}")
        if args.json:
            print(json.dumps({"checks": checks, "failures": 1}, indent=2, sort_keys=True))
        return EXIT_CHECK_FAILED

    # 2. Library, pinned and hash-verified.
    try:
        library = resolve(getattr(args, "library", None))
        checks.append({
            "check": "skills library",
            "ok": True,
            "detail": f"{library.files.root} at commit {str(library.commit or '')[:12]}",
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
    catalog = ModelCatalog(config, providers)
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
    _, source, _, _ = _load_stack(args)
    try:
        plan = Planner(source).plan(args.goal, slug=args.slug, max_iterations=args.max_iterations)
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
        plan = Planner(source).plan(args.goal, slug=args.slug or "org-check")
        binder = Binder(org)
        payload["plan"] = {"name": plan.manifest["name"], "nodes": plan.node_ids(),
                           "validated": plan.validation.valid}
        payload["staffing_gaps"] = binder.staffing_gaps(plan.manifest)
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
        print(f"Plan: {payload['plan']['name']}  (validated: {payload['plan']['validated']})")
        gaps = payload.get("staffing_gaps") or []
        if gaps:
            print()
            print("Staffing gaps — the plan needs capabilities the roster does not staff:")
            for gap in gaps:
                print(f"  {gap['node_id']:20s} {gap['skill']:22s} {gap['reason']}")
            print()
            print("  Hire an agent with these skills, or amend the plan, before running.")
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
        library = resolve(getattr(args, "library", None))
    except LibraryError as exc:
        _warn(f"library error: {exc}")
        raise SystemExit(EXIT_CHECK_FAILED) from exc
    workspace = _resolve_workspace(args, slug)
    workspace.ensure()
    bus = EventBus(run_id=f"cli_{slug}", trace_path=workspace.trace_path)
    # The roster the Owner hired is the roster the run uses. Without this, `hire` would write a file
    # nothing reads — the agents would exist on disk and never appear in a run, which is the exact
    # "capability with no effect" failure this project refuses elsewhere.
    org = _roster_for(config, library, getattr(args, "root", None))
    return Orchestrator(config=config, library=library, workspace=workspace, bus=bus,
                        org=org), workspace


def _resolve_workspace(args: argparse.Namespace, slug: str):
    """Resolve the workspace a command is aimed at, from ``--project`` / ``--root`` / ``--slug``.

    One resolver, because the ordering is a decision that must not be forgotten by one command out of
    eleven. Naming a folder (``--project``) is more specific than naming a directory of folders
    (``--root``), so it wins when both are given.
    """
    from .state import Workspace

    project = getattr(args, "project", None)
    if project:
        try:
            return Workspace.attach(project)
        except Exception as exc:  # noqa: BLE001 - a bad path is a usage error, said plainly
            _warn(f"cannot attach that project: {exc}")
            raise SystemExit(EXIT_CHECK_FAILED) from exc
    return Workspace.for_project(slug, root=getattr(args, "root", None))


def _slug_for(args: argparse.Namespace) -> str:
    """The project slug a command operates on.

    ``--slug`` is no longer required, because ``--project`` names the project itself: an attached
    folder supplies its own name, so demanding a second, redundant identifier would be a flag that
    exists only to be ignored.
    """
    explicit = getattr(args, "slug", None)
    if explicit:
        return explicit
    project = getattr(args, "project", None)
    if project:
        from .state import Workspace

        try:
            return Workspace.attach(project).slug
        except Exception as exc:  # noqa: BLE001 - same failure the resolver reports
            _warn(f"cannot attach that project: {exc}")
            raise SystemExit(EXIT_CHECK_FAILED) from exc
    _warn("give either --slug <name> or --project <folder>")
    raise SystemExit(EXIT_USAGE)


def _roster_for(config: Any, library: Any, root: Any) -> Any:
    """Load the effective roster, falling back to the built-in company.

    A roster problem must never block a run: the built-ins are a complete, runnable org, so a broken
    user roster is reported and then ignored rather than turning every command into an error.
    """
    from .catalog import ModelCatalog
    from .people import HireError, People

    try:
        people = People(library=library, config=config, catalog=ModelCatalog(config, {}),
                     project=root or None)
        return people.load(project=root or None)
    except HireError as exc:
        _warn(f"warning: ignoring the user roster: {exc}")
        return None


def cmd_run(args: argparse.Namespace) -> int:
    """Run a goal or an existing manifest, and report where it ended up.

    With `--goal` the graph is planned and shown before execution; with `--manifest` an existing graph
    is adopted. Either way the run stops at a gate rather than proceeding past one.
    """
    slug = args.slug or (Path(args.manifest).stem if args.manifest else _slug_from_goal(args.goal))
    orch, workspace = _orchestrator(args, slug)

    try:
        if args.manifest:
            run = orch.adopt(args.manifest, goal=args.goal or "", slug=slug)
        else:
            run = orch.prepare(args.goal, slug=slug, max_iterations=args.max_iterations)
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
        print()
        print("(dry run: nothing executed)")
        if args.json:
            print(json.dumps(run.as_dict(), indent=2, sort_keys=True, default=str))
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
                print(f"    {name:20s} {str(record.get('status')):14s} {record.get('verdict')}")
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
    print(f"  goal      : {status['goal']}")
    print(f"  workspace : {workspace.path}")
    print(f"  running   : {status['running']}")
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
            print(f"    {name:20s} {str(record.get('status')):14s} {record.get('verdict')}")
    return EXIT_OK


def cmd_decide(args: argparse.Namespace) -> int:
    """Resolve a gate: approve and continue, or reject and park.

    A rejection records the note, because a rejection the agents cannot read is one they will
    re-attempt identically.
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
    orch.decide(bool(args.approve), run=run, note=args.note or "")
    if args.json:
        print(json.dumps(run.as_dict(), indent=2, sort_keys=True, default=str))
    else:
        verdict = "approved" if args.approve else "rejected"
        print(f"{verdict} {run.gate.gate_id if run.gate else ''} -> phase {run.phase.value}")
        if args.note:
            print(f"  note: {args.note}")
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
            goal = orch.goal_set(objective, by="cli", armed=not args.no_arm)
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
    """Where `skills new` writes. `--root` aims it at a project, `--global` at the user library."""
    from . import usercfg

    if getattr(args, "root", None):
        return usercfg.project_root(args.root)
    return None


def cmd_propose(args: argparse.Namespace) -> int:
    """Find the engine's own defects and write proposals for the ones worth fixing.

    Nothing is applied. The whole point of this command is that it *stops*: it measures, drafts,
    validates against the behavioural suite, and leaves a readable proposal for the Owner to accept or
    delete. A loop that applied its own changes would be unguarded, because the suite that judges it is
    code it could rewrite.
    """
    from .improver import Improver, is_safety_surface

    slug = _slug_for(args)
    orch, workspace = _orchestrator(args, slug)

    # `--dry-run` lists what it found without drafting or writing anything, which is the honest way to
    # see the loop working before it produces files.
    improver = Improver(workspace=workspace, memory=getattr(orch, "memory", None))

    if args.list:
        listing = _proposals_for(improver)
        if args.json:
            print(json.dumps(listing, indent=2, sort_keys=True, default=str))
            return EXIT_OK
        print(f"{listing['count']} proposal(s) in {listing['directory']}")
        for entry in listing["proposals"]:
            finding = entry.get("finding") or {}
            print(f"  {entry['proposal_id']:12s} {finding.get('kind', '?'):20s} "
                  f"{(entry.get('validation') or {}).get('improved')}")
        if listing["refused_count"]:
            print(f"\n  {listing['refused_count']} refused (the boundary working):")
            for entry in listing["refused"][-5:]:
                print(f"    {entry.get('kind', '?'):20s} {str(entry.get('reason'))[:80]}")
        print("\n  Nothing here has been applied. Read a proposal, then apply it yourself.")
        return EXIT_OK

    findings = improver.detect()
    if args.dry_run:
        if args.json:
            print(json.dumps([f.as_dict() for f in findings], indent=2, sort_keys=True, default=str))
            return EXIT_OK
        if not findings:
            print("no findings: nothing the evidence supports as a defect")
            return EXIT_OK
        print(f"{len(findings)} finding(s), none drafted (dry run):")
        for finding in findings:
            refused = " [SAFETY SURFACE]" if is_safety_surface(finding.path) else ""
            print(f"  [{finding.severity:8s}] {finding.summary()[:78]}{refused}")
        return EXIT_OK

    considered = improver.run_once()
    if args.json:
        print(json.dumps({
            "considered": [{"id": p.proposal_id, "kind": p.finding.kind, "state": p.state,
                            "improved": p.validation.improved,
                            "regressions": p.validation.regressions,
                            "refusal": p.refusal} for p in considered],
            "applies_changes": False,
        }, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    promoted = [p for p in considered if p.state == "promoted"]
    print(f"{len(considered)} finding(s) considered, {len(promoted)} promoted")
    for proposal in considered:
        mark = {"promoted": "->", "rejected": "x ", "refused": "! "}.get(proposal.state, "? ")
        print(f"  {mark} {proposal.proposal_id:12s} {proposal.finding.kind:20s} {proposal.state}")
        if proposal.refusal:
            print(f"      {proposal.refusal[:100]}")
        elif proposal.validation.improved:
            print(f"      improves: {', '.join(proposal.validation.improved)}")
    if promoted:
        print()
        print(f"  Written to {improver.proposals_dir()}")
        print("  Nothing has been applied. Read one, then apply it yourself if you agree.")
    return EXIT_OK


def _proposals_for(improver: Any) -> dict[str, Any]:
    """Read the proposals directory the way the CLI and the console both need it."""
    directory = improver.proposals_dir()
    proposals: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                proposals.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    proposals.sort(key=lambda p: str(p.get("at") or ""), reverse=True)
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
            "applies_changes": False}


def cmd_pool(args: argparse.Namespace) -> int:
    """The task pool: work agents pull, rather than work pushed at them.

    `pool list` shows the queue; `pool add` puts work in it; `pool claim` is what a worker does. The
    point of the pool is that capability decides who does the work, so `add` takes `--skill`.
    """
    from .org import default_company
    from .people import HireError, People
    from .pool import PoolError, TaskPool

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
            task = pool.create(args.description, priority=args.priority or 50,
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
            print(json.dumps({"summary": summary,
                              "tasks": [t.as_dict() for t in tasks]},
                             indent=2, sort_keys=True, default=str))
            return EXIT_OK
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
                        catalog=ModelCatalog(config, providers), project=args.root)
        try:
            org = people.load(project=args.root)
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
                    project=args.root)
    try:
        org = people.load(project=args.root)
        spec = people.hire(
            HireRequest(name=args.name, skill=args.skill,
                        provider=args.provider or "", model=args.model or "",
                        context_window=args.context_window, level=args.level or "senior",
                        role=args.role or "worker", team=args.team or "",
                        title=args.title or "", max_concurrency=args.concurrency or 1),
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
    if getattr(args, "root", None):
        return usercfg.project_root(args.root)
    return None


def cmd_agents(args: argparse.Namespace) -> int:
    """List the effective roster: the built-ins plus every hire, from both roots."""
    from .catalog import ModelCatalog
    from .people import HireError, People

    config, source, providers, _ = _load_stack(args)
    catalog = ModelCatalog(config, providers)
    people = People(library=source.library, config=config, catalog=catalog,
                    project=args.root)
    try:
        org = people.load(project=args.root)
    except HireError as exc:
        _warn(f"cannot load the roster: {exc}")
        return EXIT_CHECK_FAILED
    roster = [a for a in org.roster_view() if a.get("kind") != "human"]
    if args.json:
        print(json.dumps({"agents": roster, "loaded_from": people.loaded_from},
                         indent=2, sort_keys=True, default=str))
        return EXIT_OK
    print(f"{len(roster)} agent(s)")
    for entry in roster:
        print(f"  {entry['name']:10s} {entry.get('title', ''):20s} "
              f"{entry.get('provider')}/{entry.get('model')}  [{', '.join(entry.get('skills') or [])}]")
    if people.loaded_from:
        print()
        for path in people.loaded_from:
            print(f"  roster: {path}")
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
                     project=args.root).load(project=args.root)
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
                     project=args.root).load(project=args.root)
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
    """
    from .serve import serve

    config, source, providers, _ = _load_stack(args)
    if not providers:
        _warn("no provider could be built, so there is nothing to run")
        return EXIT_CHECK_FAILED
    return serve(config=config, library=source.library, project=args.slug or "console",
                 root=getattr(args, "root", None), project_dir=getattr(args, "project", None))


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
            bus = EventBus(run_id=f"chat_{args.slug or 'chat'}", trace_path=workspace.trace_path)
            orch = Orchestrator(config=config, library=source.library, workspace=workspace, bus=bus)
            org = orch.org
        except Exception as exc:  # noqa: BLE001 - the chat must open even if the org cannot
            _warn(f"no org available for this chat: {exc}")

    session = ChatSession(config=config, gateway=gateway, org=org, orchestrator=orch,
                          workspace=workspace, skills=source, catalog=catalog)
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
            "start with:  python3 -m engine.cli doctor"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", parents=[common],
                            help="check every precondition and say what failed")
    doctor.set_defaults(func=cmd_doctor)

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
    run.add_argument("--slug", help="project name (default: derived from the goal or filename)")
    run.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    run.add_argument("--max-iterations", type=int, default=3, dest="max_iterations",
                     help="cap on the review-fix loop (default: 3)")
    run.add_argument("--executor", help="override the executor plugin path (testing)")
    run.add_argument("--dry-run", action="store_true", dest="dry_run",
                     help="plan and bind, but do not execute")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", parents=[common],
                            help="show where a run is, without changing anything")
    status.add_argument("--slug", help="project name (optional with --project, which names it)")
    status.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    status.set_defaults(func=cmd_status)

    decide = sub.add_parser("decide", parents=[common],
                            help="resolve a gate: approve and continue, or reject and park")
    decide.add_argument("--slug", help="project name (optional with --project, which names it)")
    decide.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    decide.add_argument("--approve", action="store_true", help="approve and continue")
    decide.add_argument("--reject", action="store_true", help="reject and park")
    decide.add_argument("--note", help="why; a rejection records it so agents do not retry blindly")
    decide.set_defaults(func=cmd_decide)

    instruct = sub.add_parser("instruct", parents=[common],
                              help="push guidance or a constraint into a run")
    instruct.add_argument("text", help="the guidance")
    instruct.add_argument("--slug", help="project name (optional with --project, which names it)")
    instruct.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    instruct.add_argument("--constraint", action="store_true",
                          help="make it non-negotiable, so it survives compaction and rotation")
    instruct.set_defaults(func=cmd_instruct)

    goal = sub.add_parser("goal", parents=[common],
                          help="set, inspect, pause, resume or clear the durable goal (the loop "
                               "that continues past a model finishing)")
    goal_sub = goal.add_subparsers(dest="goal_command", required=True)

    goal_set = goal_sub.add_parser("set", parents=[common],
                                   help="set the objective and arm the loop")
    goal_set.add_argument("objective", nargs="+", help="what should be achieved")
    goal_set.add_argument("--no-arm", action="store_true", dest="no_arm",
                          help="record the objective but do not start working on it")
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

    hire = sub.add_parser("hire", parents=[common],
                          help="hire an agent into the roster")
    hire.add_argument("name", help="the agent's name (must be unique)")
    hire.add_argument("--skill", required=True, help="the skill it is bound to (e.g. security-reviewer)")
    hire.add_argument("--provider", help="provider id (default: the configured default)")
    hire.add_argument("--model", help="model id (default: the configured default)")
    hire.add_argument("--context-window", type=int, help="override the probed context window")
    hire.add_argument("--level", help="junior | practitioner | senior | staff | principal")
    hire.add_argument("--role", help="worker | reviewer (default: inferred from the skill)")
    hire.add_argument("--title", help="a human title for the roster (default: derived from the skill)")
    hire.add_argument("--team", help="the team it joins")
    hire.add_argument("--concurrency", type=int, help="how many tasks it may run at once")
    hire.add_argument("--root", help="project root to find .agentorg in (default: walk up)")
    hire.add_argument("--roster-root", help="write the roster here (default: the project root)")
    hire.set_defaults(func=cmd_hire)

    agents = sub.add_parser("agents", parents=[common],
                            help="list the effective roster: built-ins plus every hire")
    agents.add_argument("--root", help="project root to find .agentorg in (default: walk up)")
    agents.set_defaults(func=cmd_agents)

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

    pool = sub.add_parser("pool", parents=[common],
                          help="the task pool: work agents pull, capability-routed")
    pool_sub = pool.add_subparsers(dest="pool_command", required=True)
    pool_list = pool_sub.add_parser("list", parents=[common],
                                    help="show the pool and its counts")
    pool_list.add_argument("--slug", help="project name (optional with --project, which names it)")
    pool_list.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    pool_list.add_argument("--state", help="only tasks in this state (pool|claimed|done|...)")
    pool_list.set_defaults(func=cmd_pool)
    pool_add = pool_sub.add_parser("add", parents=[common], help="add claimable work")
    pool_add.add_argument("description", help="what needs doing")
    pool_add.add_argument("--slug", help="project name (optional with --project, which names it)")
    pool_add.add_argument("--root", help="projects root (default: AgentOrg/projects)")
    pool_add.add_argument("--skill", action="append",
                          help="a skill the claimer must hold (repeatable)")
    pool_add.add_argument("--capability", action="append",
                          help="a capability the claimer must hold (repeatable)")
    pool_add.add_argument("--priority", type=int, help="0-100, higher first (default 50)")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns an exit code; never raises for an expected failure."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # Suppressed flags may be absent entirely; normalise so every command can read them the same way.
    for name, default in (("json", False), ("config", None), ("library", None)):
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


if __name__ == "__main__":
    raise SystemExit(main())
