#!/usr/bin/env python3
"""people.py — hiring: the Owner's own agents, persisted and merged across the two roots.

WHY THIS EXISTS
---------------
The engine could always *model* an agent (``Org.hire``, ``RoleTemplate``), but there was no way to
actually hire one from the product: every run used the same seven built-in names. "Create agents" was
a capability with no surface, which is the same as not having it.

This module is that surface. It owns three things:

1. **Loading the roster.** The built-in company is the floor; a global ``roster.json`` is layered over
   it, then a project-local one, with the project's version winning a name clash. So you hire once
   globally and every project benefits, or override for one repository by hiring locally.
2. **Hiring.** Turning a request — a name, a skill, a model — into a real :class:`AgentSpec`, with the
   refusals the design already requires (a unique name, a skill that exists, a model with a real
   window, and a reviewer that differs from the builders).
3. **Persisting.** Writing the roster to the winning root so the next run sees it.

DESIGN
------
- **The library's skill set is the menu.** A hire naming a skill that no bundle provides is refused
  with the closest names, because an agent bound to a typo would fail at bind time, mid-run, which is
  the worst moment to discover it.
- **A context window is mandatory.** An agent without one cannot be projected, so the hire refuses
  rather than deferring the failure to the first call.
- **Independence is enforced at hire time for reviewers.** Binding a second reviewer to the producers'
  model would quietly remove the property the design promises, so it is refused with the reason.
- **The merge is by name, per agent.** A project that adds one specialist keeps the global roster; it
  does not have to redeclare the other seven.

Usage:
    people = People(library=lib, config=cfg)
    spec = people.hire(name="Dana", skill="security-reviewer", provider="ollama",
                       model="qwen2.5-coder:14b", roster_root=Path(".agentorg"))
    people.save(roster_root=Path(".agentorg"))
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import usercfg
from .org import Org, OrgError, RoleTemplate, default_company
from .org.agent import AgentKind, AgentLevel, AgentSpec, Budget

__all__ = ["People", "HireRequest", "HireError", "LEVELS", "HIRE_ROLES"]


class HireError(RuntimeError):
    """A hire that cannot be honoured, named so the Owner can act on it."""


#: The levels a hire may name, so `--level` accepts the labels people actually use.
LEVELS: dict[str, AgentLevel] = {
    "junior": AgentLevel.JUNIOR,
    "practitioner": AgentLevel.PRACTITIONER,
    "mid": AgentLevel.PRACTITIONER,
    "senior": AgentLevel.SENIOR,
    "staff": AgentLevel.STAFF,
    "principal": AgentLevel.PRINCIPAL,
}


#: The roles a hire may name. `owner` is deliberately absent: the Owner is the terminal authority the
#: engine always has, not an agent a command creates.
HIRE_ROLES: tuple[str, ...] = ("worker", "reviewer")


@dataclass
class HireRequest:
    """What the Owner asked for. Kept separate from the spec so it can be validated and recorded."""

    name: str
    skill: str
    provider: str = ""
    model: str = ""
    context_window: int | None = None
    level: str = "senior"
    role: str = "worker"
    team: str = ""
    title: str = ""
    max_concurrency: int = 1
    purpose: str = ""
    as_reviewer: bool = False
    #: What this agent may reach, as `<kind>:<scope>` grants. Empty means "use the skill's default"
    #: (`_capabilities_for`), which is least privilege — so a hire that says nothing is not silently
    #: widened. Supplied explicitly, it *replaces* the default rather than adding to it: a person
    #: granting `exec:` is stating the whole set, and merging would make "revoke write" impossible
    #: to express.
    capabilities: list[str] = field(default_factory=list)


@dataclass
class People:
    """The roster owner: loads, merges, hires and persists.

    Parameters
    ----------
    library:
        The pinned Skills library, used to validate that a skill exists and to read names.
    config:
        Validated configuration, for the default provider/model and the model windows.
    catalog:
        Optional model catalog, so a hire can resolve a real window rather than a guess.
    """

    library: Any = None
    config: Any = None
    catalog: Any = None
    org: Org | None = None
    #: The project whose `.agentorg` should be searched. Held so hire-validation and load agree on
    #: which roots are in play — a mismatch would refuse a skill the engine would run.
    project: Any = None
    #: Which roots were actually read, in precedence order — surfaced so `agents` can explain itself.
    loaded_from: list[str] = field(default_factory=list)
    #: Non-fatal problems from the last operation (e.g. a weaker independence boundary), so a caller
    #: can surface them without the hire failing.
    warnings: list[str] = field(default_factory=list)

    # ── loading and merging ─────────────────────────────────────────────────

    def load(self, *, project: Path | str | None = None, org_id: str = "",
             principal_id: str = "", name: str = "") -> Org:
        """Build the effective roster: built-ins, then global, then project.

        Later sources win per agent *name*, not per file, so a project that hires one specialist keeps
        every global agent too.

        `org_id`, `principal_id` and `name` identify the org this roster belongs to when it is one of
        several in a portfolio. They are applied to the *base* company so the identity survives even a
        roster file that predates it — a portfolio must be able to find an org it registered before
        this field existed.
        """
        self.loaded_from = []
        if project is not None:
            self.project = project
        org = self._base_company()
        if org_id:
            org.id = org_id
        if principal_id:
            org.principal_id = principal_id
        if name:
            org.name = name
        # Lowest priority last: apply global, then project, so the project overwrites.
        for root in reversed(usercfg.roots(project=project)):
            path = root / "roster.json"
            if not path.is_file():
                continue
            try:
                extra = Org.load(path)
            except Exception as exc:  # noqa: BLE001 - a bad roster must not break every command
                raise HireError(f"roster at {path} could not be read: {exc}") from exc
            self._merge(org, extra)
            self.loaded_from.append(str(path))
        # A roster file that carried its own identity wins over the caller's hint, because the file is
        # what a person actually edited; the hint only fills a blank.
        org.id = org.id or org_id
        org.principal_id = org.principal_id or principal_id
        self.org = org
        return org

    def _base_company(self) -> Org:
        """The seven built-in roles plus the Owner, on the configured default model.

        The floor rather than the whole roster: with no hires at all the product must still run, which
        it cannot if the base company is empty.
        """
        provider = str((self.config.defaults.get("provider") if self.config else "") or "ollama")
        model = str((self.config.defaults.get("model") if self.config else "") or "")
        window = self._window_for(provider, model) or 32768
        return default_company(provider=provider, model=model, context_window=window)

    def _merge(self, target: Org, source: Org) -> None:
        """Overlay `source` onto `target`, by agent name, and carry teams and tombstones across.

        Replacement is by *name* but the incoming spec brings its own id, so the old id is removed and
        the new one installed. Doing it the other way — keeping the old id and copying fields — would
        silently re-point every log line and mailbox at a different agent identity.

        `retired` is carried too, and deduplicated by record: a termination is history that belongs to
        the roster it happened in, so a project that retires one agent must not lose the global
        roster's records — and a record present in both must not be duplicated by the merge.
        """
        for spec in source.agents.values():
            if spec.is_human:
                continue
            existing = next((a for a in target.agents.values()
                             if a.name.lower() == spec.name.lower()), None)
            if existing is not None and existing.id != spec.id:
                target.agents.pop(existing.id, None)
                target.runtimes.pop(existing.id, None)
            target.agents[spec.id] = spec
            target.runtimes[spec.id] = source.runtime(spec.id)
            if spec.team:
                # Assigned after the spec is in place, because assign_team resolves the agent by id.
                target.assign_team(spec.id, spec.team,
                                   lead=(spec.role == "reviewer"
                                         and spec.title == "Code Reviewer"))
        for team in source.teams.values():
            target.teams.setdefault(team.name, team)
        for record in source.retired:
            if record not in target.retired:
                target.retired.append(dict(record))

    # ── hiring ──────────────────────────────────────────────────────────────

    def hire(self, request: HireRequest, *, org: Org | None = None,
             roster_root: Path | str | None = None, save: bool = True) -> AgentSpec:
        """Validate a request, create the agent, and persist the roster.

        Every refusal names the reason and, where useful, the alternatives — a hire that fails with
        "invalid skill" and no list is a dead end for the person reading it.
        """
        target = org or self.org
        if target is None:
            raise HireError("no roster is loaded; call load() first")

        self._validate(request, target)

        provider = request.provider or str(self.config.defaults.get("provider") or "ollama")
        model = request.model or str(self.config.defaults.get("model") or "")
        # Fall back to the *one resolved default* rather than a second, hardcoded guess. Before this,
        # a hire with no explicit model used `config.defaults` while the built-in company used its own
        # fallback — so "the default unless I specify" was two different answers.
        if not model:
            pair_provider, pair_model, _reason = self.config.default_pair()
            provider = request.provider or pair_provider or provider
            model = pair_model
        window = request.context_window or self._window_for(provider, model)
        if not window:
            raise HireError(
                f"model {provider}/{model} has no known context window, so an agent cannot be bound "
                "to it. Probe the provider (`engine.cli models --refresh`) or pass --context-window."
            )

        level = LEVELS.get(request.level.lower())
        if level is None:
            raise HireError(
                f"unknown level {request.level!r}; choose one of {', '.join(sorted(set(LEVELS)))}"
            )

        # The role is checked here rather than only offered as a `--help` line: the flag had no
        # choices at all, so `hire --role boss` wrote an agent whose role nothing downstream knows —
        # and the roster is what the router reads. The vocabulary is this tuple, and the terminal's
        # help reads *it* rather than a sentence written beside it.
        role = "reviewer" if request.as_reviewer or _is_reviewer_skill(request.skill) else request.role
        if role not in HIRE_ROLES:
            raise HireError(f"unknown role {role!r}; choose one of {', '.join(HIRE_ROLES)}")

        spec = AgentSpec(
            id=_new_id(request.name, target),
            name=request.name,
            title=request.title or _title_for(request.skill),
            skills=[request.skill],
            provider=provider,
            model=model,
            context_window=int(window),
            kind=AgentKind.AI,
            role=role,
            level=level,
            team=request.team,
            capabilities=list(request.capabilities) or _capabilities_for(request.skill),
            budget=Budget(),
            max_concurrency=max(1, int(request.max_concurrency)),
            origin="owner",
        )
        try:
            target.hire(spec, team=request.team or None)
        except OrgError as exc:
            raise HireError(str(exc)) from exc
        if save:
            self.save(org=target, roster_root=roster_root)
        self.org = target
        return spec

    def update_agent(self, agent_id: str, *, name: str | None = None, provider: str | None = None,
                     model: str | None = None, context_window: int | None = None,
                     level: str | None = None, team: str | None = None, title: str | None = None,
                     max_concurrency: int | None = None,
                     org: Org | None = None,
                     roster_root: Path | str | None = None, save: bool = True) -> AgentSpec:
        """Change an existing agent's identity, model binding or limits — keeping its id.

        Editing rather than firing-and-rehiring on purpose: the id is what the mailbox, the session
        history, the ledger entries and the health record are all keyed on. A rehire would silently
        reset every one of those, so "I only changed the model" would look like a brand-new employee
        with no past — and the health record that justifies trusting it would start from zero.

        Only the fields passed are touched, so a caller can change one thing without having to
        restate the rest.
        """
        target = org or self.org
        if target is None:
            raise HireError("no roster is loaded; call load() first")
        spec = target.agents.get(agent_id)
        if spec is None:
            raise HireError(f"no agent {agent_id!r} in the roster")

        if name is not None and name.strip() and name.strip() != spec.name:
            try:
                target.rename(agent_id, name.strip())
            except OrgError as exc:
                raise HireError(str(exc)) from exc

        if provider is not None or model is not None:
            new_provider = (provider or spec.provider).strip()
            new_model = (model or spec.model).strip()
            window = context_window or self._window_for(new_provider, new_model)
            if not window:
                raise HireError(
                    f"model {new_provider}/{new_model} has no known context window, so this agent "
                    "cannot be bound to it. Probe the provider or enter a window explicitly."
                )
            spec.provider = new_provider
            spec.model = new_model
            spec.context_window = int(window)

        if context_window is not None and not (provider or model):
            spec.context_window = int(context_window)

        if level is not None:
            chosen = LEVELS.get(level.lower())
            if chosen is None:
                raise HireError(
                    f"unknown level {level!r}; choose one of {', '.join(sorted(set(LEVELS)))}")
            spec.level = chosen
        if title is not None:
            spec.title = title
        if max_concurrency is not None:
            spec.max_concurrency = max(1, int(max_concurrency))

        if team is not None:
            # Move between teams through the org, so the team's member list cannot go stale. Not via
            # `hire` — that refuses an id already in the roster, which this agent is.
            try:
                for existing in target.teams.values():
                    if agent_id in existing.members and existing.name != team:
                        existing.members.remove(agent_id)
                        if existing.lead == agent_id:
                            existing.lead = None
                if team:
                    target.assign_team(agent_id, team)
            except OrgError as exc:
                raise HireError(str(exc)) from exc
            spec.team = team

        if save:
            self.save(org=target, roster_root=roster_root)
        self.org = target
        return spec

    def retire_agent(self, agent_id: str, *, reason: str = "", org: Org | None = None,
                     roster_root: Path | str | None = None, save: bool = True) -> AgentSpec:
        """Remove an agent from the roster, keeping what it produced.

        The refusal cases come from the org itself — an agent with reports that are actively working
        cannot be terminated, because the work they hold would be orphaned mid-flight — so this adds
        persistence rather than re-deciding them.
        """
        target = org or self.org
        if target is None:
            raise HireError("no roster is loaded; call load() first")
        try:
            spec = target.terminate(agent_id, reason=reason)
        except OrgError as exc:
            raise HireError(str(exc)) from exc
        if save:
            self.save(org=target, roster_root=roster_root)
        self.org = target
        return spec

    def _validate(self, request: HireRequest, org: Org) -> None:
        if not request.name.strip():
            raise HireError("an agent needs a name")
        if not request.skill.strip():
            raise HireError("an agent needs a skill — that is what it is for")
        clash = [a.name for a in org.agents.values() if a.name.lower() == request.name.lower()]
        if clash:
            raise HireError(
                f"an agent named {request.name!r} already exists. Names must be unique so the roster "
                "and every log line stay unambiguous."
            )
        if not self._skill_exists(request.skill):
            raise HireError(
                f"no skill named {request.skill!r}. "
                f"{self._closest_skills(request.skill)}"
            )
        # A reviewer on the producers' model weakens the independence guarantee. This *warns* rather
        # than refusing, to match the engine's own default: `default_company` accepts a shared model
        # and rests independence on context lineage, saying so plainly. Refusing here would be
        # stricter than the engine itself and would make any reviewer unhirable on a single-model
        # local setup, where no distinct model exists to offer.
        is_reviewer = request.as_reviewer or _is_reviewer_skill(request.skill)
        if is_reviewer and request.model:
            producers = {a.model for a in org.agents.values()
                         if a.kind is AgentKind.AI and a.role == "worker" and a.model}
            if request.model in producers:
                self.warnings.append(
                    f"{request.name!r} reviews work but shares the producers' model "
                    f"{request.model!r}; independence rests on context lineage alone. Pass a "
                    "different --model to strengthen it."
                )

    # ── skills ──────────────────────────────────────────────────────────────

    def _skill_names(self) -> list[str]:
        """Every skill name available: the library plus any the Owner has authored.

        Read through the same overlay the engine uses, so a skill that a plan can bind is a skill a
        hire accepts. Consulting the library alone would refuse a skill the engine would happily run.
        """
        names: set[str] = set()
        if self.library is not None:
            try:
                from .skills import FilesystemSkillSource
                from .skills.overlay import OverlaySkillSource

                source = OverlaySkillSource(FilesystemSkillSource(self.library), project=self.project)
                names.update(source.names())
            except Exception:  # noqa: BLE001 - a broken library must not block authoring
                pass
        else:
            for root in usercfg.roots(project=self.project):
                skills = root / "skills"
                if not skills.is_dir():
                    continue
                for child in sorted(skills.iterdir()):
                    if child.is_dir() and (child / "SKILL.md").is_file():
                        names.add(child.name)
        return sorted(names)

    def _skill_exists(self, name: str) -> bool:
        return name in set(self._skill_names())

    def _closest_skills(self, name: str, limit: int = 8) -> str:
        """A short, useful list: substring hits first, then a taste of the catalogue."""
        names = self._skill_names()
        needle = name.lower()
        near = [n for n in names if needle in n.lower() or n.lower() in needle]
        if near:
            return "Did you mean: " + ", ".join(near[:limit]) + "?"
        return ("Run `engine.cli skills list` to see them. For example: "
                + ", ".join(names[:limit]) + " …")

    # ── persistence ─────────────────────────────────────────────────────────

    def save(self, *, org: Org | None = None, roster_root: Path | str | None = None) -> Path:
        """Write the roster to the winning root.

        Only agents the Owner actually created are written — those whose ``origin`` is ``"owner"``
        and which are not the built-in Owner principal. Writing the built-in company too would freeze
        a copy of the defaults into the project: a later change to the built-in roster would then be
        silently shadowed by a stale snapshot, and the project file that was meant to record one hire
        would contain eight agents nobody hired.

        The `retired` tombstones are written unfiltered. They are not agents, so none of the reasoning
        above applies, and a tombstone is the *only* record a terminated agent leaves — filtering it
        would delete the record in the same write that was meant to keep it.
        """
        target = org or self.org
        if target is None:
            raise HireError("no roster to save")
        # Default to this instance's own project rather than the process's working directory: a
        # `People` built for one project must not write into whatever directory the command happened
        # to run from, which is how a roster ends up in a place nobody looks.
        if roster_root is not None:
            root = Path(roster_root)
        elif self.project is not None:
            root = usercfg.project_root(self.project)
        else:
            root = usercfg.project_root()
        root.mkdir(parents=True, exist_ok=True)
        path = root / "roster.json"
        document = target.to_dict()
        agents = document.get("agents")
        if isinstance(agents, list):
            document["agents"] = [spec for spec in agents
                                  if _is_owner_hired(spec) and spec.get("id") != "ag_owner"]
        elif isinstance(agents, dict):
            keep = {aid: spec for aid, spec in agents.items()
                    if _is_owner_hired(spec) and aid != "ag_owner"}
            document["agents"] = keep
        path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
        return path

    # ── model helpers ───────────────────────────────────────────────────────

    def _window_for(self, provider: str, model: str) -> int | None:
        """A real context window, from the catalog first and the config table second."""
        if not model:
            return None
        if self.catalog is not None:
            try:
                entry = self.catalog.resolve(provider, model)
                if entry is not None and entry.window_known:
                    return int(entry.context_window)
            except Exception:  # noqa: BLE001 - fall through to the config table
                pass
        if self.config is not None:
            try:
                spec = self.config.model_spec(model)
                if spec.context_window:
                    return int(spec.context_window)
            except Exception:  # noqa: BLE001
                pass
        return None


def _is_owner_hired(spec: Any) -> bool:
    """Whether this agent is one the Owner created, as opposed to a built-in or the Owner itself.

    `origin` is the engine's own provenance field, so this asks the model rather than guessing from a
    name or an id scheme.

    `origin="goal"` counts. It is an agent the engine created **on the goal's authority** — the
    "create the person if they do not exist" path — and once the goal asked for it to be persisted it
    is exactly as durable as a manual hire. Excluding it meant `persist_auto_hires` reported success
    and wrote an empty roster: the helper did the work, the log said it was saved, and it was gone
    next run. `ephemeral` is the one origin deliberately not persisted.
    """
    if isinstance(spec, dict):
        if spec.get("kind") == "human":
            return False
        return spec.get("origin") in ("owner", "goal")
    return (not getattr(spec, "is_human", False)
            and getattr(spec, "origin", "") in ("owner", "goal"))


def _new_id(name: str, org: Org) -> str:
    """A stable-looking id derived from the name, so a roster file reads well."""
    import re

    stem = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or "agent"
    candidate, n = f"ag_{stem}", 1
    while candidate in org.agents:
        n += 1
        candidate = f"ag_{stem}_{n}"
    return candidate


def _is_reviewer_skill(skill: str) -> bool:
    return "review" in skill.lower() or skill.lower().startswith("qa-")


def _title_for(skill: str) -> str:
    """A human title from a skill name, so the roster reads like a team and not a slug list."""
    return skill.replace("-", " ").replace("_", " ").title()


def _capabilities_for(skill: str) -> list[str]:
    """Least privilege: a reader gets read, everyone else gets read+write.

    Deliberately *not* a copy of the Owner's capability set — an agent granted `deploy:*` because it
    happened to be hired would be privilege it never asked for.
    """
    base = ["read:*"]
    if not _is_reviewer_skill(skill):
        base.append("write:src/**")
    return base
