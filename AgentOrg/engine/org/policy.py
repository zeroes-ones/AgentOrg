#!/usr/bin/env python3
"""policy.py — autonomy levels, route classes, and layered resolution with a safety floor.

WHY THIS EXISTS
---------------
The Owner must be able to say "let the org route routine work itself, but ask me before it
escalates or resolves a conflict". That is a *per-route-class* setting, and it has to be
resolvable at several scopes because a real organisation delegates differently by team and by
individual.

Without a floor, one careless per-agent setting could silently disable every human gate —
turning the loop guards and escalation design into decoration. So the floor is enforced here,
in one place, and refused loudly.

DESIGN
------
- **Four levels, ordered**: `auto` < `notify` < `confirm` < `manual`. The order is what makes
  "this layer may only tighten, not loosen" expressible.
- **Layered resolution, most specific wins**: org → team → agent → run → task.
- **A safety floor** that no layer may cross without an explicit opt-in: `R-ESCALATE` and
  `R-CONFLICT` cannot resolve below `confirm`.
- **Every resolution reports which layer decided.** "Why did this run without asking me?" is
  answerable rather than mysterious.

Usage:
    resolver = PolicyResolver(defaults={"R-ESCALATE": "confirm"}, allow_autonomous_escalation=False)
    resolver.set("agent", "ag_7f3a", "R-REWORK", "notify")
    level, layer = resolver.resolve(RouteClass.REWORK, agent_id="ag_7f3a")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

__all__ = [
    "AUTONOMY_ORDER",
    "ROUTE_CLASSES",
    "SAFETY_FLOOR",
    "Autonomy",
    "PolicyError",
    "PolicyResolver",
    "RouteClass",
    "Resolution",
]


class PolicyError(RuntimeError):
    """Raised when a policy is invalid or a resolution would breach the safety floor."""


class Autonomy(str, Enum):
    """How much the org may do without asking.

    Ordered deliberately: the integer value is the ordering, and comparisons rely on it.
    """

    AUTO = "auto"        # act silently
    NOTIFY = "notify"    # act, then tell the Owner
    CONFIRM = "confirm"  # propose, then wait for a decision
    MANUAL = "manual"    # the Owner must initiate; the org may not act at all

    @property
    def rank(self) -> int:
        return AUTONOMY_ORDER[self]

    def permits_action(self) -> bool:
        """Whether this level lets the org proceed on its own."""
        return self in (Autonomy.AUTO, Autonomy.NOTIFY)

    def requires_human(self) -> bool:
        """Whether this level blocks on a human decision."""
        return self in (Autonomy.CONFIRM, Autonomy.MANUAL)


#: The autonomy ladder, least to most human involvement. The ordering is load-bearing: the
#: safety floor and the "may only tighten" rule both compare ranks.
AUTONOMY_ORDER: dict[Autonomy, int] = {
    Autonomy.AUTO: 0,
    Autonomy.NOTIFY: 1,
    Autonomy.CONFIRM: 2,
    Autonomy.MANUAL: 3,
}


class RouteClass(str, Enum):
    """Every class of next-step decision the org makes.

    Classifying first is what makes autonomy a policy rather than a global switch: the Owner
    can automate routine handoffs while still gating escalation.
    """

    CONTRACT = "R-CONTRACT"        # phase advance, a normal forward handoff
    REWORK = "R-REWORK"            # reviewer -> developer revision
    DELEGATE = "R-DELEGATE"        # an agent spawns a helper, peer or specialist
    ESCALATE = "R-ESCALATE"        # exhaustion, retry cap, >3 open questions
    CONFLICT = "R-CONFLICT"        # two agents contradict each other
    MATCH_FAIL = "R-MATCH-FAIL"    # the router found no confident match


#: Route classes whose floor cannot be lowered without an explicit opt-in. Escalation and
#: conflict are exactly the points where an unchecked org is most dangerous — the router being
#: confident is not evidence that it is right.
SAFETY_FLOOR: dict[RouteClass, Autonomy] = {
    RouteClass.ESCALATE: Autonomy.CONFIRM,
    RouteClass.CONFLICT: Autonomy.CONFIRM,
}

#: The default policy. Routine work is automated; anything that would normally reach a human
#: stays a confirmation.
DEFAULT_POLICY: dict[str, str] = {
    RouteClass.CONTRACT.value: Autonomy.AUTO.value,
    RouteClass.REWORK.value: Autonomy.AUTO.value,
    RouteClass.DELEGATE.value: Autonomy.AUTO.value,
    RouteClass.ESCALATE.value: Autonomy.CONFIRM.value,
    RouteClass.CONFLICT.value: Autonomy.CONFIRM.value,
    RouteClass.MATCH_FAIL.value: Autonomy.CONFIRM.value,
}

#: Scopes, least to most specific. A more specific scope wins.
SCOPE_ORDER: tuple[str, ...] = ("org", "team", "agent", "run", "task")


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving a route class: the level *and* who decided it.

    The `layer` field is what makes the routing auditable. Without it, "why did the org act
    without asking?" can only be answered by re-deriving the whole policy stack.
    """

    route_class: RouteClass
    level: Autonomy
    layer: str
    scope_id: str = ""
    floored: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "route_class": self.route_class.value,
            "level": self.level.value,
            "layer": self.layer,
            "scope_id": self.scope_id,
            "floored": self.floored,
            "note": self.note,
        }


@dataclass
class PolicyResolver:
    """Resolves a route class to an autonomy level across layered scopes.

    Parameters
    ----------
    defaults:
        The org-level policy, keyed by route-class value.
    allow_autonomous_escalation:
        The explicit opt-in that permits the floor to be lowered. Off by default, because the
        floor exists precisely to stop a single setting from removing every human gate.
    """

    defaults: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_POLICY))
    allow_autonomous_escalation: bool = False
    # scope -> scope_id -> route_class -> level
    overrides: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Normalise and validate the defaults once, so a bad level is caught at construction
        # rather than at the first routing decision.
        cleaned: dict[str, str] = {}
        for route_class, level in (self.defaults or {}).items():
            key = _route_key(route_class)
            cleaned[key] = _autonomy(level, context=f"defaults[{route_class!r}]").value
        self.defaults = cleaned
        self._validate_floor(cleaned, layer="org")

    # ── configuration ───────────────────────────────────────────────────────

    def set(self, scope: str, scope_id: str, route_class: str | RouteClass,
            level: str | Autonomy) -> None:
        """Set an autonomy level at a scope.

        Raises
        ------
        PolicyError
            On an unknown scope, an unknown route class, an invalid level, or a value that
            breaches the safety floor.
        """
        if scope not in SCOPE_ORDER:
            raise PolicyError(
                f"unknown policy scope {scope!r}; valid scopes: {', '.join(SCOPE_ORDER)}"
            )
        key = _route_key(route_class)
        resolved = _autonomy(level, context=f"{scope}:{scope_id} {key}")
        if scope == "org":
            self.defaults[key] = resolved.value
            self._validate_floor(self.defaults, layer="org")
            return
        bucket = self.overrides.setdefault(scope, {}).setdefault(str(scope_id), {})
        previous = bucket.get(key)
        bucket[key] = resolved.value
        try:
            self._validate_floor(bucket, layer=f"{scope}:{scope_id}")
        except PolicyError:
            # Restore the previous value so a rejected change is not half-applied.
            if previous is None:
                bucket.pop(key, None)
            else:
                bucket[key] = previous
            raise

    def set_many(self, scope: str, scope_id: str, values: dict[str, str]) -> None:
        """Set several route classes at once, all-or-nothing.

        Atomic because a partial application could leave a policy that is neither the old one
        nor the intended one, which is worse than refusing the change.
        """
        backup = {k: v for k, v in (self.overrides.get(scope, {}).get(str(scope_id), {})).items()}
        try:
            for route_class, level in values.items():
                self.set(scope, scope_id, route_class, level)
        except PolicyError:
            if scope == "org":
                # Re-apply the org defaults from the backup path is not attempted: org values
                # are already validated individually, so a failure here means one entry was
                # invalid and the earlier ones stand — reported to the caller as-is.
                raise
            bucket = self.overrides.setdefault(scope, {}).setdefault(str(scope_id), {})
            bucket.clear()
            bucket.update(backup)
            raise

    def clear(self, scope: str, scope_id: str = "", route_class: str | RouteClass | None = None) -> None:
        """Remove overrides, all of them or one route class."""
        if scope == "org":
            if route_class is None:
                self.defaults = dict(DEFAULT_POLICY)
            else:
                self.defaults.pop(_route_key(route_class), None)
            return
        if route_class is None:
            self.overrides.get(scope, {}).pop(str(scope_id), None)
            return
        self.overrides.get(scope, {}).get(str(scope_id), {}).pop(_route_key(route_class), None)

    # ── resolution ──────────────────────────────────────────────────────────

    def resolve(self, route_class: str | RouteClass, *, agent_id: str = "", team: str = "",
                run_id: str = "", task_id: str = "") -> Resolution:
        """Resolve a route class to its effective autonomy.

        Most specific scope wins. The safety floor is then applied, so a resolution that reaches
        below it is raised to the floor and the fact is recorded rather than silently altered.
        """
        key = _route_key(route_class)
        klass = RouteClass(key) if key in {k.value for k in RouteClass} else None

        # Walk scopes from most to least specific so the first hit wins.
        candidates: list[tuple[str, str, dict[str, str]]] = []
        if task_id:
            candidates.append(("task", task_id, self.overrides.get("task", {}).get(task_id, {})))
        if run_id:
            candidates.append(("run", run_id, self.overrides.get("run", {}).get(run_id, {})))
        if agent_id:
            candidates.append(("agent", agent_id, self.overrides.get("agent", {}).get(agent_id, {})))
        if team:
            candidates.append(("team", team, self.overrides.get("team", {}).get(team, {})))
        candidates.append(("org", "", self.defaults))

        chosen_level = Autonomy(DEFAULT_POLICY.get(key, Autonomy.AUTO.value))
        layer, scope_id, found = "builtin", "", False
        for scope, sid, bucket in candidates:
            raw = bucket.get(key)
            if raw is None:
                continue
            chosen_level = Autonomy(raw)
            layer, scope_id, found = scope, sid, True
            break

        floored = False
        note = ""
        if klass is not None and klass in SAFETY_FLOOR and not self.allow_autonomous_escalation:
            floor = SAFETY_FLOOR[klass]
            if chosen_level.rank < floor.rank:
                note = (
                    f"{chosen_level.value} at layer {layer!r} is below the safety floor "
                    f"{floor.value!r}; raised. Set allow_autonomous_escalation to opt out "
                    "deliberately."
                )
                chosen_level = floor
                floored = True
            elif chosen_level is floor:
                note = f"held at the safety floor ({floor.value})"

        if not found:
            note = note or "no policy set for this route class; using the built-in default"
            # Even the built-in default is floored, so the invariant holds for an unconfigured
            # route class.
            if klass is not None and klass in SAFETY_FLOOR and not self.allow_autonomous_escalation:
                floor = SAFETY_FLOOR[klass]
                if chosen_level.rank < floor.rank:
                    chosen_level = floor
                    floored = True

        if not note:
            note = f"set at layer {layer!r}"
        return Resolution(route_class=klass or RouteClass.CONTRACT, level=chosen_level,
                          layer=layer, scope_id=scope_id, floored=floored, note=note)

    def effective(self, *, agent_id: str = "", team: str = "", run_id: str = "",
                  task_id: str = "") -> dict[str, Any]:
        """Resolve every route class at once — the UI's policy matrix."""
        out: dict[str, Any] = {}
        for klass in RouteClass:
            resolution = self.resolve(klass, agent_id=agent_id, team=team,
                                      run_id=run_id, task_id=task_id)
            out[klass.value] = resolution.as_dict()
        return out

    def may_act(self, route_class: str | RouteClass, **scopes: str) -> bool:
        """Whether the org may proceed without asking for this route class."""
        return self.resolve(route_class, **scopes).level.permits_action()

    def needs_approval(self, route_class: str | RouteClass, **scopes: str) -> bool:
        """Whether this route class blocks on a human decision."""
        return self.resolve(route_class, **scopes).level.requires_human()

    # ── validation ──────────────────────────────────────────────────────────

    def _validate_floor(self, bucket: dict[str, str], *, layer: str) -> None:
        """Refuse a bucket that would put a floored route class below its floor.

        Validation happens at *set* time rather than only at resolve time, so the Owner learns
        immediately that a setting was rejected instead of discovering at run time that it had
        no effect.
        """
        if self.allow_autonomous_escalation:
            return
        for klass, floor in SAFETY_FLOOR.items():
            raw = bucket.get(klass.value)
            if raw is None:
                continue
            level = Autonomy(raw)
            if level.rank < floor.rank:
                raise PolicyError(
                    f"{layer}: policy for {klass.value} is {level.value!r}, below the safety "
                    f"floor {floor.value!r}.\n"
                    "  This floor stops a single setting from disabling every human gate.\n"
                    "  To opt out deliberately, set allow_autonomous_escalation = true."
                )

    # ── serialisation ───────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialisable policy, for the org file and the `policy.changed` event."""
        return {
            "defaults": dict(self.defaults),
            "allow_autonomous_escalation": self.allow_autonomous_escalation,
            "overrides": {scope: {sid: dict(vals) for sid, vals in ids.items()}
                          for scope, ids in self.overrides.items()},
            "safety_floor": {k.value: v.value for k, v in SAFETY_FLOOR.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyResolver":
        """Rebuild from a persisted dict."""
        resolver = cls(
            defaults=data.get("defaults") or dict(DEFAULT_POLICY),
            allow_autonomous_escalation=bool(data.get("allow_autonomous_escalation", False)),
        )
        raw_overrides = data.get("overrides") or {}
        if isinstance(raw_overrides, dict):
            for scope, ids in raw_overrides.items():
                if not isinstance(ids, dict):
                    continue
                for scope_id, values in ids.items():
                    if isinstance(values, dict):
                        for route_class, level in values.items():
                            try:
                                resolver.set(scope, str(scope_id), route_class, level)
                            except PolicyError:
                                # A persisted override that no longer validates is dropped
                                # rather than preventing the org from loading.
                                continue
        return resolver

    def summary(self) -> str:
        """A readable policy matrix, for the CLI and the Owner console."""
        lines = ["Route class          level     layer"]
        for klass in RouteClass:
            resolution = self.resolve(klass)
            floor_mark = "  [floored]" if resolution.floored else ""
            lines.append(f"{klass.value:20s} {resolution.level.value:9s} {resolution.layer}{floor_mark}")
        if self.allow_autonomous_escalation:
            lines.append("")
            lines.append("WARNING: autonomous escalation is enabled; the safety floor is not enforced.")
        return "\n".join(lines)


def _route_key(route_class: str | RouteClass) -> str:
    """Normalise a route class to its canonical string, rejecting an unknown one.

    Accepts either the enum or a string in either form (`R-ESCALATE` or `escalate`) so a config
    file and code can both be readable.
    """
    if isinstance(route_class, RouteClass):
        return route_class.value
    text = str(route_class).strip()
    for klass in RouteClass:
        if text == klass.value or text.lower() == klass.name.lower():
            return klass.value
    raise PolicyError(
        f"unknown route class {route_class!r}; valid: "
        + ", ".join(k.value for k in RouteClass)
    )


def _autonomy(level: str | Autonomy, *, context: str) -> Autonomy:
    """Normalise an autonomy level, rejecting an invalid one."""
    if isinstance(level, Autonomy):
        return level
    text = str(level).strip().lower()
    try:
        return Autonomy(text)
    except ValueError:
        raise PolicyError(
            f"{context}: unknown autonomy level {level!r}; valid: "
            + ", ".join(a.value for a in Autonomy)
        ) from None
