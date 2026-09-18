#!/usr/bin/env python3
"""portfolio.py — the person, and the several orgs they run.

WHY THIS EXISTS
---------------
The engine assumes one org per workspace. That is correct for a project and wrong for a *person*: in
the real world one principal runs several organisations at once — a CEO who is also a founder, a CTO
of a second company, the chair of a foundation — each with its own agents, missions, goals, budget and
risk. Today that person's only option is to keep several folders and remember which is which, because
nothing models *the person* or *the set of orgs*.

This module is that missing top:

```
Principal   the human — one identity across every org            portfolio.json
  └ OrgEntry  a named org the principal runs         → workspace + roster + missions + goals + runs
```

Note what is **not** here. A Portfolio does not run anything, plan anything, or spend anything. It is a
register: who the principal is, which orgs they run, where each org lives, and what state each is in.
Execution stays in `Orchestrator` (one per org, through `engine/fleet.py`), and the identity of an
agent stays inside its own org. Keeping the register inert is what stops it becoming a second control
plane.

DESIGN
------
- **The principal is one identity, the orgs are many.** `Principal.id` is stable and shared; every
  `Org` records `principal_id`, so "the CEO of Tesla" and "the CTO of SpaceX" are the same human
  without the agents, budgets or mailboxes of the two orgs ever touching. That is the *shared
  principal, independent agents* split the design chose.
- **An org entry points at a workspace; it does not copy one.** `OrgEntry.path` is the org's folder,
  and its roster, missions, goals and runs live inside it exactly as they do today. The portfolio
  stores a pointer, never a duplicate — a second copy of a roster is a second thing to keep true.
- **Identity is explicit and stable.** `Org.id` is generated once and never re-derived from the name,
  because a rename must not orphan a roster, a mission or a spend ledger.
- **Durable and versioned, exactly like a Goal.** `portfolio.json` is written atomically
  (temp + `os.replace` + fsync), schema-checked, and a portfolio read from disk comes back with no
  org *running* — a register that re-armed work on load would be an unattended spend with a nicer
  name.
- **A missing org folder is reported, not fatal.** A portfolio is a manifest of where things are; a
  moved folder should say so on inspection, not refuse to open.

Usage:
    portfolio = Portfolio.load() or Portfolio.new()
    portfolio.ensure_principal("Sandeep")
    entry = portfolio.add_org(name="Tesla", slug="tesla", path=Path("~/work/tesla"))
    portfolio.orgs()                      # ordered
    portfolio.rollup()                    # cross-org summary for the console
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "PortfolioError", "Principal", "OrgEntry", "Portfolio",
    "PORTFOLIO_FILENAME", "PORTFOLIO_VERSION", "DEFAULT_PRINCIPAL_ID",
]

#: The register, in the global root (``~/.agentorg/portfolio.json``): the person spans projects, so
#: the portfolio is not project-local the way a roster is.
PORTFOLIO_FILENAME = "portfolio.json"
#: Bumped when the document's shape changes incompatibly.
PORTFOLIO_VERSION = "1.0.0"

#: The principal's stable id, fixed rather than generated so a hire or a decision recorded in one org
#: is attributable to the same human in the next. Mirrors `roster.OWNER_ID`'s reasoning one level up.
DEFAULT_PRINCIPAL_ID = "pr_owner"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class PortfolioError(RuntimeError):
    """A portfolio that cannot be read, written or transitioned, named so the reason is actionable."""


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def _slugify(text: str, *, limit: int = 48) -> str:
    """A stable folder-safe slug: lowercase, alphanumeric, hyphens."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:limit].strip("-")
    if not slug:
        slug = "org"
    if not slug[0].isalnum():
        slug = "o" + slug
    return slug


@dataclass
class Principal:
    """The human who runs every org in the portfolio.

    Deliberately thin: an identity and a name. A principal has no model, no budget and no skills —
    those belong to the agents inside an org. What the principal *has* is terminal authority, which
    every org records by pointing at this id.
    """

    id: str = DEFAULT_PRINCIPAL_ID
    name: str = "Owner"
    created_at: str = ""
    #: Free-form, for anything a person wants to remember about themselves.
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "created_at": self.created_at,
                "notes": self.notes}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Principal":
        return cls(
            id=str(data.get("id") or DEFAULT_PRINCIPAL_ID),
            name=str(data.get("name") or "Owner"),
            created_at=str(data.get("created_at") or ""),
            notes=str(data.get("notes") or ""),
        )


@dataclass
class OrgEntry:
    """One org the principal runs, and where it lives.

    The entry is a *pointer plus a label*, not a copy of an org. The roster, missions, goals and runs
    live in the folder `path` names, under that folder's `.agent_state/`, exactly as they do when the
    org is run on its own.
    """

    id: str
    name: str
    slug: str
    #: The org's project folder (the workspace root). An absolute, expanded path.
    path: str = ""
    #: What this org is for, in the principal's words — shown in the portfolio view.
    charter: str = ""
    #: Optional: the org is paused at the portfolio level without touching its own state.
    enabled: bool = True
    #: A per-org daily spend ceiling, enforced by the fleet. `0` means "use the configured default".
    daily_budget_usd: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "slug": self.slug, "path": self.path,
            "charter": self.charter, "enabled": self.enabled,
            "daily_budget_usd": self.daily_budget_usd, "created_at": self.created_at,
            "updated_at": self.updated_at, "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrgEntry":
        if not str(data.get("id") or "").strip():
            raise PortfolioError("an org entry needs an id")
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data.get("id")),
            slug=str(data.get("slug") or _slugify(str(data.get("name") or data.get("id")))),
            path=str(data.get("path") or ""),
            charter=str(data.get("charter") or ""),
            enabled=bool(data.get("enabled", True)),
            daily_budget_usd=float(data.get("daily_budget_usd") or 0.0),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            tags=[str(t) for t in (data.get("tags") or [])],
        )

    @property
    def workspace_path(self) -> Path:
        """The org's folder as a `Path`, expanded. Empty when the entry names no folder."""
        return Path(self.path).expanduser() if self.path else Path()


@dataclass
class Portfolio:
    """The principal, and the orgs they run. A register — it runs nothing and spends nothing."""

    principal: Principal = field(default_factory=Principal)
    orgs: list[OrgEntry] = field(default_factory=list)
    #: The org the CLI/console acts on by default, so a bare command is unambiguous.
    active_org_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    version: str = PORTFOLIO_VERSION

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def new(cls, *, principal_name: str = "Owner") -> "Portfolio":
        now = _iso_now()
        return cls(
            principal=Principal(id=DEFAULT_PRINCIPAL_ID, name=principal_name or "Owner",
                                created_at=now),
            created_at=now, updated_at=now,
        )

    # ── the principal ───────────────────────────────────────────────────────

    def ensure_principal(self, name: str) -> Principal:
        """Set (or rename) the principal. A blank name is refused — an unnamed principal is
        unattributable, which is the one thing this level exists to prevent."""
        clean = (name or "").strip()
        if not clean:
            raise PortfolioError("the principal needs a name; an unnamed owner is unattributable")
        if not self.principal.created_at:
            self.principal.created_at = _iso_now()
        self.principal.name = clean
        self.updated_at = _iso_now()
        return self.principal

    # ── orgs ────────────────────────────────────────────────────────────────

    def org_ids(self) -> list[str]:
        return [entry.id for entry in self.orgs]

    def org(self, ref: str) -> OrgEntry:
        """Find an org by id, slug or name — the three ways a person refers to one.

        Case-insensitive on name/slug because a command line should not care about capitals, and
        raising a clear error naming the available orgs rather than returning None, because every
        caller that looks one up wants either the org or a reason.
        """
        wanted = (ref or "").strip().lower()
        for entry in self.orgs:
            if entry.id == ref or entry.slug.lower() == wanted or entry.name.lower() == wanted:
                return entry
        known = ", ".join(f"{e.name}({e.slug})" for e in self.orgs) or "none"
        raise PortfolioError(f"no org {ref!r} in this portfolio; known orgs: {known}")

    def has_org(self, ref: str) -> bool:
        try:
            self.org(ref)
            return True
        except PortfolioError:
            return False

    def add_org(self, *, name: str, slug: str = "", path: str | Path = "",
                charter: str = "", daily_budget_usd: float = 0.0,
                tags: Iterable[str] = (), make_active: bool = False) -> OrgEntry:
        """Register an org. Its folder must not already be another org's folder, and two orgs may not
        share a name or slug — an ambiguous register is one a person cannot act on.

        The id is generated from the slug and disambiguated, then **never re-derived**: renaming an
        org changes its label, not its identity.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            raise PortfolioError("an org needs a name")
        clean_slug = _slugify(slug or clean_name)
        for other in self.orgs:
            if other.slug == clean_slug:
                raise PortfolioError(f"an org with the slug {clean_slug!r} already exists ({other.name})")
            if other.name.lower() == clean_name.lower():
                raise PortfolioError(f"an org named {clean_name!r} already exists")

        resolved = str(Path(path).expanduser()) if path else ""
        if resolved:
            for other in self.orgs:
                if other.path and other.path == resolved:
                    raise PortfolioError(
                        f"{resolved} is already the folder for org {other.name!r}; "
                        "one folder is one org")

        now = _iso_now()
        entry = OrgEntry(
            id=self._unique_id(clean_slug), name=clean_name, slug=clean_slug, path=resolved,
            charter=(charter or "").strip(), daily_budget_usd=float(daily_budget_usd or 0.0),
            created_at=now, updated_at=now, tags=[str(t) for t in tags],
        )
        self.orgs.append(entry)
        if make_active or not self.active_org_id:
            self.active_org_id = entry.id
        self.updated_at = now
        return entry

    def update_org(self, ref: str, *, name: str = "", charter: str = "",
                   enabled: bool | None = None, daily_budget_usd: float | None = None,
                   path: str | Path | None = None, tags: Iterable[str] | None = None) -> OrgEntry:
        """Edit an org's label or policy. Identity (`id`, `slug`) is deliberately not editable."""
        entry = self.org(ref)
        if name.strip():
            clash = next((o for o in self.orgs
                          if o.id != entry.id and o.name.lower() == name.strip().lower()), None)
            if clash is not None:
                raise PortfolioError(f"an org named {name.strip()!r} already exists")
            entry.name = name.strip()
        if charter:
            entry.charter = charter.strip()
        if enabled is not None:
            entry.enabled = bool(enabled)
        if daily_budget_usd is not None:
            entry.daily_budget_usd = max(0.0, float(daily_budget_usd))
        if path is not None:
            resolved = str(Path(path).expanduser()) if path else ""
            if resolved:
                clash = next((o for o in self.orgs
                              if o.id != entry.id and o.path == resolved), None)
                if clash is not None:
                    raise PortfolioError(f"{resolved} is already the folder for org {clash.name!r}")
            entry.path = resolved
        if tags is not None:
            entry.tags = [str(t) for t in tags]
        entry.updated_at = _iso_now()
        self.updated_at = entry.updated_at
        return entry

    def remove_org(self, ref: str) -> OrgEntry:
        """Forget an org from the register. **It does not delete the folder** — the roster, missions
        and runs stay on disk; only the pointer is removed."""
        entry = self.org(ref)
        self.orgs = [o for o in self.orgs if o.id != entry.id]
        if self.active_org_id == entry.id:
            self.active_org_id = self.orgs[0].id if self.orgs else ""
        self.updated_at = _iso_now()
        return entry

    def set_active(self, ref: str) -> OrgEntry:
        entry = self.org(ref)
        self.active_org_id = entry.id
        self.updated_at = _iso_now()
        return entry

    def active_org(self) -> OrgEntry | None:
        if not self.active_org_id:
            return None
        try:
            return self.org(self.active_org_id)
        except PortfolioError:
            return None

    def _unique_id(self, base: str) -> str:
        """A stable, unique org id derived from the slug."""
        candidate = f"org_{base}"
        taken = {o.id for o in self.orgs}
        if candidate not in taken:
            return candidate
        n = 2
        while f"{candidate}_{n}" in taken:
            n += 1
        return f"{candidate}_{n}"

    # ── inspection ──────────────────────────────────────────────────────────

    def inspect(self) -> dict[str, Any]:
        """The register with each org's folder checked: does it exist, and is it a real workspace?

        Read-only, and tolerant: a moved or missing folder is reported as `missing` rather than
        raised, because a register is exactly the place to record "this one needs fixing".
        """
        orgs: list[dict[str, Any]] = []
        for entry in self.orgs:
            path = entry.workspace_path
            exists = bool(path) and path.is_dir()
            state_dir = path / ".agent_state" if exists else None
            orgs.append({
                **entry.as_dict(),
                "exists": exists,
                "has_state": bool(state_dir and state_dir.is_dir()),
                "state_dir": str(state_dir) if state_dir else "",
            })
        return {
            "principal": self.principal.as_dict(),
            "active_org_id": self.active_org_id,
            "orgs": orgs,
            "counts": {"orgs": len(self.orgs),
                       "enabled": sum(1 for o in self.orgs if o.enabled),
                       "missing": sum(1 for o in orgs if not o["exists"])},
        }

    def rollup(self, *, per_org: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
        """The cross-org summary: every org, its mission, its spend and its blockers in one place.

        `per_org` is the live picture the caller gathered (one dict per org id, from the fleet), so
        this function stays a pure fold and the portfolio module keeps needing no engine. An org with
        no live picture shows as `not loaded` rather than as zero spend, because "no figure" and
        "zero" are different facts.
        """
        by_id = {str(item.get("id")): item for item in per_org}
        rows: list[dict[str, Any]] = []
        totals = {"spend_usd": 0.0, "orgs": len(self.orgs), "running": 0,
                  "blocked": 0, "waiting": 0}
        for entry in self.orgs:
            live = by_id.get(entry.id, {})
            # `live` may be present but *not loaded*: the fleet returns a row for every registered org
            # so the console can show the whole portfolio, and a row whose org has not been loaded
            # carries no live picture. Honour the row's own `loaded` flag when it has one, so an
            # unloaded org reads as "not loaded" rather than as an org that spent nothing.
            has_live = bool(live.get("loaded", bool(live))) if live else False
            row = {
                "id": entry.id, "name": entry.name, "slug": entry.slug, "enabled": entry.enabled,
                "charter": entry.charter,
                "mission": live.get("mission", "") if has_live else "",
                "mission_state": live.get("mission_state", "") if has_live else "",
                "objective_now": live.get("objective_now", "") if has_live else "",
                "phase": live.get("phase", "") if has_live else "",
                "running": bool(live.get("running")) and has_live,
                "stop_reason": live.get("stop_reason", "") if has_live else "",
                "blocked": int(live.get("blocked_nodes") or 0) if has_live else 0,
                "waiting_host": bool(live.get("waiting_host")) and has_live,
                "spend_usd": live.get("spend_usd") if has_live else None,
                "headline": live.get("headline", "") if has_live else "",
                "next_action": live.get("next_action", "") if has_live else "",
                "loaded": has_live,
            }
            if row["running"]:
                totals["running"] += 1
            if row["blocked"]:
                totals["blocked"] += 1
            if row["waiting_host"]:
                totals["waiting"] += 1
            if isinstance(row["spend_usd"], (int, float)):
                totals["spend_usd"] += float(row["spend_usd"])
            rows.append(row)
        return {"principal": self.principal.as_dict(), "totals": totals, "orgs": rows}

    # ── serialisation ───────────────────────────────────────────────────────

    def as_dict(self) -> dict[str, Any]:
        return {
            "portfolio_version": self.version,
            "principal": self.principal.as_dict(),
            "orgs": [entry.as_dict() for entry in self.orgs],
            "active_org_id": self.active_org_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Portfolio":
        version = str(data.get("portfolio_version") or PORTFOLIO_VERSION)
        if version.split(".")[0] != PORTFOLIO_VERSION.split(".")[0]:
            raise PortfolioError(
                f"portfolio version {version} is not compatible with {PORTFOLIO_VERSION}; it was "
                "written by a different build. Fix or remove the file."
            )
        principal_raw = data.get("principal")
        principal = (Principal.from_dict(principal_raw)
                     if isinstance(principal_raw, dict) else Principal())
        orgs: list[OrgEntry] = []
        for raw in data.get("orgs") or []:
            if not isinstance(raw, dict):
                continue
            try:
                orgs.append(OrgEntry.from_dict(raw))
            except PortfolioError:
                continue  # a broken entry is skipped; the rest of the register still opens
        return cls(
            principal=principal,
            orgs=orgs,
            active_org_id=str(data.get("active_org_id") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            version=version,
        )

    # ── persistence ─────────────────────────────────────────────────────────

    @staticmethod
    def path_for(root: Any = None) -> Path:
        """Where the portfolio lives: ``<root>/portfolio.json`` (default ``~/.agentorg/``)."""
        if root is not None:
            return Path(root) / PORTFOLIO_FILENAME
        from . import usercfg

        return usercfg.global_root() / PORTFOLIO_FILENAME

    def save(self, root: Any = None) -> Path:
        """Persist atomically, temp-then-`os.replace`, so a reader never sees a torn register."""
        target = self.path_for(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = _iso_now()
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.as_dict(), fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise PortfolioError(f"failed to write portfolio {target}: {exc}") from exc
        return target

    @classmethod
    def load(cls, root: Any = None) -> "Portfolio | None":
        """Read the portfolio, or None when there is none.

        None rather than an empty portfolio keeps the decision with the caller: "no portfolio" and "a
        portfolio with no orgs" are different, and conflating them is how a fresh setup inherits a
        register nobody made.
        """
        target = cls.path_for(root)
        if not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PortfolioError(
                f"portfolio {target} is corrupt: {exc.msg} (line {exc.lineno}). Refusing to load "
                "an unreadable register."
            ) from exc
        if not isinstance(data, dict):
            raise PortfolioError(f"portfolio {target} must be a JSON object")
        return cls.from_dict(data)
