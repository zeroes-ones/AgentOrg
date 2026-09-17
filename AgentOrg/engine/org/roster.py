#!/usr/bin/env python3
"""roster.py — the organisation: teams, agents, reporting lines, persistence.

WHY THIS EXISTS
---------------
The Owner builds an org: names agents, binds each to skills and a model, groups them into
teams, and decides who reports to whom. That roster is the state that makes "the same skill
exists many times under different names" real, and it must survive restarts and be editable
without disturbing a run in flight.

DESIGN
------
- **Persistence is atomic and schema-versioned.** The roster is written temp-then-replace, so
  a crash cannot leave a half-written org that would lose agents.
- **Renaming preserves identity.** Agents are keyed by id, so a rename changes only the
  display name and every trace, mailbox and health record stays valid.
- **Termination is explicit, and retiring an agent with active reports is refused.** A
  specialist whose helpers are still working must be retired after them, not before.
- **The Owner is a real agent in the roster**, because that is what lets a human handoff use
  the same contract, ledger and audit path as an automated one.
- **A default company is provided** so the first-run experience has something to run before
  the Owner has made any choices.

Usage:
    org = default_company(provider="ollama", model="qwen2.5-coder:7b", context_window=32768)
    org.hire(AgentSpec(...))
    org.save(path)
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .agent import AgentError, AgentKind, AgentLevel, AgentRuntime, AgentSpec, AgentState, Budget
from .mailbox import Mailbox

__all__ = ["Org", "OrgError", "RoleTemplate", "Team", "default_company", "OWNER_ID"]

#: The Owner's stable id. Fixed rather than generated so a human action recorded in one run
#: is attributable in the next.
OWNER_ID = "ag_owner"


class OrgError(RuntimeError):
    """Raised on an invalid roster operation."""


@dataclass
class Team:
    """A group of agents with a lead."""

    name: str
    lead: str | None = None
    members: list[str] = field(default_factory=list)
    purpose: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "lead": self.lead,
                "members": list(self.members), "purpose": self.purpose}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Team":
        return cls(
            name=str(data.get("name") or ""),
            lead=data.get("lead"),
            members=[str(m) for m in (data.get("members") or [])],
            purpose=str(data.get("purpose") or ""),
        )


@dataclass
class RoleTemplate:
    """A reusable role definition used to seed a default company.

    A template is a *specification*, not an agent: instantiating one yields a concrete
    :class:`AgentSpec` with a fresh id, so two Developers hired from the same template are
    genuinely distinct employees.
    """

    title: str
    skills: list[str]
    level: AgentLevel = AgentLevel.PRACTITIONER
    team: str = ""
    role: str = "worker"
    capabilities: list[str] = field(default_factory=list)
    #: The capability scope this role is granted by default. Empty means "derive from the role":
    #: a reviewer gets read-only, everyone else gets read plus write under `src/`. Stating it here is
    #: what makes the built-in company *usable* — before this, its agents held no capabilities at all,
    #: so the first tool call any of them made was refused.
    granted: list[str] = field(default_factory=list)
    max_concurrency: int = 1
    purpose: str = ""

    def instantiate(self, *, name: str, provider: str, model: str, context_window: int,
                    max_output: int | None = None, agent_id: str | None = None,
                    budget: Budget | None = None, tags: list[str] | None = None) -> AgentSpec:
        """Create a concrete agent from this template."""
        from .agent import new_agent_id

        return AgentSpec(
            id=agent_id or new_agent_id(),
            name=name,
            title=self.title,
            skills=list(self.skills),
            provider=provider,
            model=model,
            context_window=context_window,
            max_output=max_output,
            level=self.level,
            team=self.team,
            role=self.role,
            capabilities=self.effective_capabilities(),
            max_concurrency=self.max_concurrency,
            budget=budget or Budget(),
            tags=list(tags or []),
            origin="template",
        )

    def effective_capabilities(self) -> list[str]:
        """The capabilities this role carries.

        Derived from the role when the template states none, because the alternative — no grants at
        all — makes every tool call refused, which looks like a broken tool rather than a missing
        permission. The derivation is least-privilege: a **reviewer reads and never writes**, so the
        independence guarantee survives contact with a filesystem, and a worker writes under `src/`
        only.

        An explicit `capabilities` list always wins, so a template that needs something unusual can
        state it rather than being overridden by the default.
        """
        if self.capabilities:
            return list(self.capabilities)
        if self.granted:
            return list(self.granted)
        if self.role == "reviewer":
            # Read-only, deliberately. A verifier that can edit the artifact it judges is not a
            # verifier, and that is a structural guarantee here rather than a convention.
            return ["read:*"]
        return ["read:*", "write:src/**"]


#: The default company, in the order the pipeline runs. Kept small on purpose: an org seeded
#: with thirty agents is harder to reason about than one seeded with the seven a build needs,
#: and the Owner can hire more.
DEFAULT_TEMPLATES: tuple[RoleTemplate, ...] = (
    RoleTemplate(title="Product Manager", skills=["product-manager"], team="Product",
                 level=AgentLevel.SENIOR, role="worker",
                 purpose="Turns raw intake into a PRD with acceptance criteria."),
    RoleTemplate(title="System Architect", skills=["system-architect"], team="Platform",
                 level=AgentLevel.STAFF, role="worker",
                 purpose="Turns the PRD into an architecture with documented trade-offs."),
    RoleTemplate(title="API Designer", skills=["api-designer"], team="Platform",
                 level=AgentLevel.SENIOR, role="worker",
                 purpose="Turns the architecture into a reviewable API contract."),
    RoleTemplate(title="Backend Developer", skills=["backend-developer"], team="Platform",
                 level=AgentLevel.SENIOR, role="worker",
                 purpose="Implements the change against the PRD and API spec."),
    RoleTemplate(title="Code Reviewer", skills=["code-reviewer"], team="Quality",
                 level=AgentLevel.STAFF, role="reviewer",
                 purpose="Reviews the change against the PRD; owes a severity-graded verdict."),
    RoleTemplate(title="QA Engineer", skills=["qa-engineer"], team="Quality",
                 level=AgentLevel.SENIOR, role="reviewer",
                 purpose="Executes acceptance criteria and reports suite results."),
    RoleTemplate(title="Security Reviewer", skills=["security-reviewer"], team="Security",
                 level=AgentLevel.STAFF, role="reviewer",
                 purpose="Reviews the change for vulnerabilities and data-flow exposure."),
)


@dataclass
class Org:
    """The roster: agents, teams and reporting lines.

    Parameters
    ----------
    agents:
        Live specs keyed by agent id.
    runtimes:
        Live state keyed by agent id. Separate from specs so the roster file does not change
        on every heartbeat.
    """

    name: str = "AgentOrg"
    agents: dict[str, AgentSpec] = field(default_factory=dict)
    runtimes: dict[str, AgentRuntime] = field(default_factory=dict)
    teams: dict[str, Team] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    org_version: str = "1.0.0"
    _locks: dict[str, threading.RLock] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._guard = threading.RLock()

    # ── hiring and lifecycle ────────────────────────────────────────────────

    def hire(self, spec: AgentSpec, *, team: str | None = None) -> AgentSpec:
        """Add an agent to the roster.

        Raises
        ------
        OrgError
            When the id is already taken, or when a name collides with an existing agent.
            Name collisions are refused because two agents called "Alice" would make the
            roster and every log line ambiguous to the Owner.
        """
        with self._guard:
            if spec.id in self.agents:
                raise OrgError(f"agent id {spec.id!r} is already in the roster")
            clash = [a for a in self.agents.values() if a.name.lower() == spec.name.lower()]
            if clash:
                raise OrgError(
                    f"an agent named {spec.name!r} already exists (id {clash[0].id}). "
                    "Names must be unique so the roster and logs are unambiguous."
                )
            self.agents[spec.id] = spec
            self.runtimes[spec.id] = AgentRuntime(agent_id=spec.id)
            if team:
                self.assign_team(spec.id, team)
            elif spec.team:
                self.assign_team(spec.id, spec.team)
            return spec

    def hire_from(self, template: RoleTemplate, *, name: str, provider: str, model: str,
                  context_window: int, max_output: int | None = None,
                  budget: Budget | None = None) -> AgentSpec:
        """Instantiate a template into a live agent."""
        return self.hire(template.instantiate(
            name=name, provider=provider, model=model,
            context_window=context_window, max_output=max_output, budget=budget,
        ))

    def terminate(self, agent_id: str, *, reason: str = "") -> AgentSpec:
        """Remove an agent from the routing pool.

        The spec is returned so a helper's lineage can be recorded before it is dropped. Two
        safeguards apply:

        - The Owner cannot be terminated, because that would leave the org with no terminal
          authority.
        - An agent with a report that is *currently working* cannot be terminated, because the
          work that report holds would be orphaned mid-flight. Idle reports are re-parented to
          the Owner rather than orphaned, so their reporting line stays valid.
        """
        with self._guard:
            spec = self.get(agent_id)
            if spec.id == OWNER_ID:
                raise OrgError("the Owner cannot be terminated; it holds terminal authority")

            reports = self.direct_reports(agent_id)
            working = [r for r in reports
                       if self.runtimes.get(r.id, AgentRuntime(r.id)).busy()]
            if working:
                names = ", ".join(f"{r.name} ({r.id})" for r in working)
                raise OrgError(
                    f"cannot terminate {spec.name!r} while it has reports that are actively "
                    f"working: {names}. Terminate or release them first, or the work they hold "
                    "is orphaned mid-flight."
                )

            runtime = self.runtimes.get(agent_id)
            if runtime is not None:
                runtime.state = AgentState.TERMINATED
            del self.agents[agent_id]
            self.runtimes.pop(agent_id, None)

            # Re-parent idle reports so no agent is left pointing at a departed manager. Their
            # own reports_to would otherwise name an agent that is no longer in the roster.
            for report in reports:
                if report.id in self.agents:
                    report.reports_to = OWNER_ID if OWNER_ID in self.agents else None

            for team in self.teams.values():
                if agent_id in team.members:
                    team.members.remove(agent_id)
                if team.lead == agent_id:
                    team.lead = None
            return spec

    def rename(self, agent_id: str, new_name: str) -> AgentSpec:
        """Rename an agent, keeping its id and therefore all its history."""
        with self._guard:
            spec = self.get(agent_id)
            if not (new_name or "").strip():
                raise OrgError("an agent name cannot be empty")
            clash = [a for a in self.agents.values()
                     if a.id != agent_id and a.name.lower() == new_name.lower()]
            if clash:
                raise OrgError(f"an agent named {new_name!r} already exists (id {clash[0].id})")
            spec.name = new_name.strip()
            return spec

    def reassign(self, agent_id: str, *, team: str | None = None,
                 reports_to: str | None = None, skills: list[str] | None = None) -> AgentSpec:
        """Change an agent's team, reporting line or skills.

        Only the fields passed are changed, so a caller can adjust one thing without
        accidentally clearing the others.
        """
        with self._guard:
            spec = self.get(agent_id)
            if team is not None:
                self.assign_team(agent_id, team)
            if reports_to is not None:
                if reports_to and reports_to not in self.agents:
                    raise OrgError(f"reports_to names an unknown agent {reports_to!r}")
                spec.reports_to = reports_to or None
            if skills is not None:
                if not skills:
                    raise OrgError(
                        f"cannot strip every skill from {spec.name!r}; an agent with no "
                        "capability cannot be routed work"
                    )
                spec.skills = list(skills)
            return spec

    # ── lookup ──────────────────────────────────────────────────────────────

    def get(self, agent_id: str) -> AgentSpec:
        """Return a spec by id.

        Raises
        ------
        OrgError
            With the available ids, because "agent not found" without them is a puzzle.
        """
        try:
            return self.agents[agent_id]
        except KeyError:
            raise OrgError(
                f"unknown agent {agent_id!r}; roster has: "
                + (", ".join(sorted(self.agents)) or "(empty)")
            ) from None

    def by_name(self, name: str) -> AgentSpec | None:
        """Find an agent by its display name, case-insensitively."""
        lowered = (name or "").strip().lower()
        for spec in self.agents.values():
            if spec.name.lower() == lowered:
                return spec
        return None

    def runtime(self, agent_id: str) -> AgentRuntime:
        """Live state for an agent, created on first access."""
        with self._guard:
            runtime = self.runtimes.get(agent_id)
            if runtime is None:
                runtime = AgentRuntime(agent_id=agent_id)
                self.runtimes[agent_id] = runtime
            return runtime

    def lock_for(self, agent_id: str) -> threading.RLock:
        """A per-agent lock, so a caller can serialise work for one agent.

        This is the mechanism behind single-flight: the scheduler holds this while an agent is
        working, so it cannot be handed a second task concurrently.
        """
        with self._guard:
            lock = self._locks.get(agent_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[agent_id] = lock
            return lock

    def owner(self) -> AgentSpec | None:
        """The human Owner, if one is in the roster."""
        return self.agents.get(OWNER_ID)

    # ── selection ───────────────────────────────────────────────────────────

    def candidates_for(self, skill: str, *, available_only: bool = True,
                       min_level: AgentLevel | None = None) -> list[AgentSpec]:
        """Every agent holding a skill, best-first.

        Ordered by capability level, then by current load, then by name. Preferring the most
        capable agent and then the least busy one is what makes routing predictable rather
        than arbitrary, and the name tiebreak keeps the order stable across runs.
        """
        out: list[AgentSpec] = []
        for spec in self.agents.values():
            if not spec.has_skill(skill):
                continue
            if min_level is not None and int(spec.level) < int(min_level):
                continue
            runtime = self.runtimes.get(spec.id)
            if available_only and runtime is not None:
                if runtime.state in (AgentState.QUARANTINED, AgentState.TERMINATED):
                    continue
            out.append(spec)

        def sort_key(spec: AgentSpec) -> tuple[int, int, str]:
            runtime = self.runtimes.get(spec.id)
            load = 1 if (runtime is not None and runtime.busy()) else 0
            return (-int(spec.level), load, spec.name)

        out.sort(key=sort_key)
        return out

    def available_for(self, skill: str) -> list[AgentSpec]:
        """Agents holding a skill that are idle right now."""
        return [spec for spec in self.candidates_for(skill)
                if self.runtime(spec.id).available()]

    def direct_reports(self, agent_id: str) -> list[AgentSpec]:
        """Agents whose `reports_to` names this agent."""
        return [spec for spec in self.agents.values() if spec.reports_to == agent_id]

    def span_of_control(self, agent_id: str) -> int:
        """How many active reports this agent currently holds.

        Counted from the roster rather than from a cached counter, so a hire or terminate is
        reflected immediately — a stale counter would let the span-of-control cap drift open.
        The delegation design caps this so one agent cannot become a bottleneck holding fifty
        reports.
        """
        return sum(
            1 for report in self.direct_reports(agent_id)
            if self.runtimes.get(report.id, AgentRuntime(report.id)).state
            not in (AgentState.TERMINATED, AgentState.QUARANTINED)
        )

    def skills_present(self) -> list[str]:
        """Every skill held by at least one agent, sorted."""
        found: set[str] = set()
        for spec in self.agents.values():
            found.update(spec.skills)
        return sorted(found)

    def agents_for_skill(self, skill: str) -> list[AgentSpec]:
        """Every agent holding a skill, regardless of state."""
        return [spec for spec in self.agents.values() if spec.has_skill(skill)]

    # ── teams ───────────────────────────────────────────────────────────────

    def assign_team(self, agent_id: str, team_name: str, *, lead: bool = False) -> Team:
        """Put an agent in a team, creating the team when it does not exist."""
        with self._guard:
            spec = self.get(agent_id)
            team = self.teams.get(team_name)
            if team is None:
                team = Team(name=team_name)
                self.teams[team_name] = team
            for existing in self.teams.values():
                if agent_id in existing.members and existing is not team:
                    existing.members.remove(agent_id)
            if agent_id not in team.members:
                team.members.append(agent_id)
            spec.team = team_name
            if lead:
                team.lead = agent_id
            return team

    def team_members(self, team_name: str) -> list[AgentSpec]:
        """Every agent in a team, in hiring order."""
        team = self.teams.get(team_name)
        if team is None:
            return []
        return [self.agents[member] for member in team.members if member in self.agents]

    # ── persistence ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialisation for the org file and the `agent.org.changed` event."""
        return {
            "org_version": self.org_version,
            "name": self.name,
            "agents": [spec.as_dict() for spec in sorted(self.agents.values(), key=lambda a: a.id)],
            "teams": [team.as_dict() for team in sorted(self.teams.values(), key=lambda t: t.name)],
            "policy": self.policy,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Org":
        """Rebuild an org from a persisted dict.

        An agent that fails to reconstruct is skipped rather than aborting the whole load: a
        roster with one broken entry should open so the Owner can fix it, not refuse to open
        at all.
        """
        org = cls(
            name=str(data.get("name") or "AgentOrg"),
            org_version=str(data.get("org_version") or "1.0.0"),
            policy=data.get("policy") if isinstance(data.get("policy"), dict) else {},
        )
        skipped: list[str] = []
        for raw in data.get("agents") or []:
            if not isinstance(raw, dict):
                continue
            try:
                spec = AgentSpec.from_dict(raw)
            except (AgentError, ValueError, TypeError) as exc:
                skipped.append(f"{raw.get('name') or raw.get('id')}: {exc}")
                continue
            org.agents[spec.id] = spec
            org.runtimes[spec.id] = AgentRuntime(agent_id=spec.id)
        for raw in data.get("teams") or []:
            if isinstance(raw, dict):
                team = Team.from_dict(raw)
                if team.name:
                    org.teams[team.name] = team
        if skipped:
            org.policy.setdefault("_load_warnings", []).extend(skipped)
        return org

    def save(self, path: os.PathLike | str) -> Path:
        """Atomically persist the roster."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise OrgError(f"failed to save the roster to {target}: {exc}") from exc
        return target

    @classmethod
    def load(cls, path: os.PathLike | str) -> "Org":
        """Load a roster, or return an empty one when the file is absent."""
        target = Path(path)
        if not target.is_file():
            return cls()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise OrgError(f"roster file {target} is corrupt: {exc.msg}") from exc
        if not isinstance(data, dict):
            raise OrgError(f"roster file {target} must be a JSON object")
        return cls.from_dict(data)

    # ── views ───────────────────────────────────────────────────────────────

    def roster_view(self) -> list[dict[str, Any]]:
        """The roster as the UI shows it: identity, state, cost and workload."""
        out: list[dict[str, Any]] = []
        for spec in sorted(self.agents.values(), key=lambda a: (a.team, -int(a.level), a.name)):
            runtime = self.runtime(spec.id)
            entry = spec.as_dict()
            entry["state"] = runtime.state.value
            entry["current_task"] = runtime.current_task
            entry["current_node"] = runtime.current_node
            entry["active_reports"] = self.span_of_control(spec.id)
            entry["stats"] = runtime.as_dict()
            out.append(entry)
        return out

    def summary(self) -> str:
        """A short human-readable roster, for the CLI and the approval prompt."""
        if not self.agents:
            return "The roster is empty. Hire an agent to begin."
        lines = [f"{self.name}: {len(self.agents)} agents across {len(self.teams)} teams"]
        for team in sorted(self.teams.values(), key=lambda t: t.name):
            lead = self.agents[team.lead].name if team.lead in self.agents else "—"
            lines.append(f"  {team.name} (lead: {lead})")
            for member in self.team_members(team.name):
                runtime = self.runtime(member.id)
                skills = ", ".join(member.skills)
                lines.append(
                    f"    {member.name:14s} {member.title:20s} {runtime.state.value:11s} "
                    f"{member.provider}/{member.model}  [{skills}]"
                )
        unteamed = [a for a in self.agents.values() if not a.team]
        if unteamed:
            lines.append("  (no team)")
            for member in sorted(unteamed, key=lambda a: a.name):
                runtime = self.runtime(member.id)
                lines.append(f"    {member.name:14s} {member.title:20s} {runtime.state.value}")
        return "\n".join(lines)


def default_company(*, provider: str, model: str, context_window: int,
                    max_output: int | None = None, reviewer_provider: str | None = None,
                    reviewer_model: str | None = None,
                    reviewer_context_window: int | None = None,
                    owner_name: str = "Owner") -> Org:
    """Seed a runnable company.

    The reviewers are bound to a *different* model than the builders when one is supplied,
    because `verification-independence-engineer` requires a verifier that differs from the
    producer by model, context lineage, or both. Making that the default means the property
    holds without the Owner having to know to configure it.

    Parameters
    ----------
    reviewer_provider, reviewer_model:
        When omitted the builders' model is used, and the independence property then rests on
        context lineage alone (the reviewer never receives the producer's reasoning), which is
        still a valid boundary but a weaker one.
    """
    org = Org(name="AgentOrg")

    # The Owner is a real agent, so a human handoff travels the same path as an automated one.
    owner = AgentSpec(
        id=OWNER_ID,
        name=owner_name,
        title="Owner",
        skills=["*"],  # the Owner may act in any role
        kind=AgentKind.HUMAN,
        role="owner",
        level=AgentLevel.PRINCIPAL,
        provider="",
        model="",
        context_window=None,  # not applicable to a human
        capabilities=["read:*", "write:*", "deploy:*", "admin:*"],
        origin="owner",
    )
    # A human agent carries no model, so the AI-specific validation is skipped for it.
    org.agents[owner.id] = owner
    org.runtimes[owner.id] = AgentRuntime(agent_id=owner.id)

    names = {
        "Product Manager": "Priya",
        "System Architect": "Arjun",
        "API Designer": "Ana",
        "Backend Developer": "Alice",
        "Code Reviewer": "Sana",
        "QA Engineer": "Quinn",
        "Security Reviewer": "Sam",
    }
    reviewer_titles = {"Code Reviewer", "QA Engineer", "Security Reviewer"}
    for template in DEFAULT_TEMPLATES:
        is_reviewer = template.title in reviewer_titles
        use_provider = reviewer_provider if (is_reviewer and reviewer_provider) else provider
        use_model = reviewer_model if (is_reviewer and reviewer_model) else model
        use_window = (reviewer_context_window if (is_reviewer and reviewer_context_window)
                      else context_window)
        spec = template.instantiate(
            name=names.get(template.title, template.title.split()[0]),
            provider=use_provider, model=use_model, context_window=use_window,
            max_output=max_output,
            budget=Budget(),
        )
        org.agents[spec.id] = spec
        org.runtimes[spec.id] = AgentRuntime(agent_id=spec.id)
        org.assign_team(spec.id, template.team, lead=(template.role == "reviewer"
                                                      and template.title == "Code Reviewer"))

    # Reporting lines: everyone reports to the Owner; developers report through the architect.
    architect = next((s for s in org.agents.values() if s.title == "System Architect"), None)
    for spec in org.agents.values():
        if spec.is_human:
            continue
        spec.reports_to = architect.id if (architect and spec.title == "Backend Developer") else OWNER_ID
    return org
