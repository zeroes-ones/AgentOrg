#!/usr/bin/env python3
"""slo.py — service-level objectives and error-budget burn for agents and runs.

WHY THIS EXISTS
---------------
Health answers "is this agent working well right now". An SLO answers a different question: "is
the organisation meeting the reliability we promised, and how fast is it spending the margin for
error". Without an error budget, every threshold is a guess and every alert is either noise or too
late.

The library's observability guidance is specific: alert on **burn rate**, not on a raw threshold.
"Error rate exceeds 0.1%" fires constantly and teaches people to ignore alerts; "you will exhaust
this month's error budget in one hour" is actionable.

DESIGN
------
- **Objectives are configurable per scope** (run-level and agent-level) with the library's own
  metric names, so `skill-sli-report.py` remains a valid external cross-check.
- **Burn rate, not absolute error rate.** A 1× burn spends the budget exactly over the window;
  a 14.4× burn spends it in about an hour, which is what deserves an alert.
- **Multi-window evaluation.** A short window catches a spike, a long window catches a slow burn —
  evaluating both is what avoids both alert storms and silent degradation.
- **Budget exhaustion is not an error.** It is a signal that reliability work takes priority over
  features, and it is reported as such.

Usage:
    tracker = SLOTracker(config)
    tracker.observe_run(escalated=True, outcome="escalation")
    report = tracker.evaluate(window_s=3600)
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Iterable

from ..config import Config

__all__ = ["BurnRate", "SLOReport", "SLOTracker", "Severity", "Objective"]


class Severity(str, Enum):
    """How urgent a burn is, mapped to the library's alerting bands."""

    INFO = "info"          # trend anomaly, no immediate impact
    WARNING = "warning"    # budget burning fast, page during business hours
    CRITICAL = "critical"  # budget will exhaust imminently, page now


@dataclass
class Objective:
    """One service-level objective.

    `target` is the fraction of good events required, so `0.70` means "at least 70% of runs must
    complete without escalating" — the same 0.70 the library's SLI report uses as its default
    escalation-rate gate.
    """

    name: str
    metric: str
    target: float
    scope: str = "run"
    # The window the target applies to, in seconds. A month by default, matching the library's
    # error-budget framing.
    window_s: float = 30 * 24 * 3600.0
    description: str = ""

    @property
    def error_budget(self) -> float:
        """The fraction of bad events the objective tolerates."""
        return max(0.0, 1.0 - self.target)


@dataclass(frozen=True)
class BurnRate:
    """A burn-rate reading for one window.

    `rate` is relative to the budget: 1.0 spends the whole budget exactly once over the objective's
    window, so 14.4 means a month-long budget would be consumed in roughly two days of this
    behaviour — the library's critical band.
    """

    window_s: float
    rate: float
    severity: Severity
    good: int
    total: int
    action: str = ""
    objective_window_s: float = 0.0

    @property
    def exhausted_in_s(self) -> float | None:
        """How long until the budget is spent if this rate is sustained.

        Derived from the ratio of the observation window to the objective window: a rate of R
        consumes the objective's budget in `objective_window / R` seconds. Returns None when the
        budget is not burning at all, which is different from burning slowly.
        """
        if self.rate <= 0:
            return None
        window = self.objective_window_s or self.window_s
        if window <= 0:
            return None
        return window / self.rate

    def as_dict(self) -> dict[str, Any]:
        remaining = self.exhausted_in_s
        return {
            "window_s": self.window_s,
            "rate": round(self.rate, 4),
            "severity": self.severity.value,
            "good": self.good,
            "total": self.total,
            "action": self.action,
            "exhausted_in_s": round(remaining, 1) if remaining is not None else None,
        }


@dataclass
class SLOReport:
    """The evaluation of one objective at one moment."""

    objective: Objective
    observed_good: int
    observed_total: int
    burn_rates: list[BurnRate] = field(default_factory=list)
    at: str = ""

    @property
    def attainment(self) -> float:
        """The observed good fraction."""
        return self.observed_good / self.observed_total if self.observed_total else 1.0

    @property
    def meeting(self) -> bool:
        """Whether the observed fraction meets the target."""
        return self.attainment >= self.objective.target

    @property
    def budget_remaining(self) -> float:
        """Fraction of the error budget left, floored at zero."""
        tolerated = self.objective.error_budget
        if tolerated <= 0:
            return 1.0 if self.meeting else 0.0
        consumed = max(0.0, 1.0 - self.attainment) / tolerated
        return max(0.0, 1.0 - consumed)

    @property
    def severity(self) -> Severity:
        """The worst severity across the windows evaluated, raised if the budget is spent.

        A burn-rate band alone is not enough: a rate just above 1× consumes the budget without ever
        crossing the 2× warning band, so an exhausted budget would otherwise report as
        informational. An exhausted budget that is missing its target is treated as critical,
        because there is no margin left for the next failure.
        """
        if not self.burn_rates:
            base = Severity.INFO
        else:
            order = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
            base = max((b.severity for b in self.burn_rates), key=lambda s: order[s])

        if self.meeting:
            return base
        if self.budget_remaining <= 0.0:
            return Severity.CRITICAL
        if self.budget_remaining <= 0.25 and base is Severity.INFO:
            # Most of the margin is gone; raise to a warning even if the rate looks sustainable.
            return Severity.WARNING
        return base

    def as_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective.name,
            "metric": self.objective.metric,
            "scope": self.objective.scope,
            "target": self.objective.target,
            "attainment": round(self.attainment, 4),
            "meeting": self.meeting,
            "budget_remaining": round(self.budget_remaining, 4),
            "severity": self.severity.value,
            "good": self.observed_good,
            "total": self.observed_total,
            "windows": [b.as_dict() for b in self.burn_rates],
            "at": self.at,
        }


@dataclass
class _Observation:
    """One recorded event, with the timestamp its windows are measured against."""

    at: float
    good: bool
    metric: str
    scope: str = "run"


#: Default objectives, using the library's SLI vocabulary so its external report stays a valid
#: cross-check. Targets match the library's own default gates.
DEFAULT_OBJECTIVES: tuple[Objective, ...] = (
    Objective(name="run_escalation", metric="escalation_rate", target=0.70, scope="run",
              description="at least 70% of runs complete without escalating"),
    Objective(name="agent_success", metric="success_rate", target=0.85, scope="agent",
              description="at least 85% of agent tasks succeed"),
    Objective(name="guardrail_cleanliness", metric="guardrail_rate", target=0.99, scope="run",
              description="no more than 1% of tasks trip a guardrail"),
)


@dataclass
class SLOTracker:
    """Tracks observations against objectives and computes burn rates.

    Parameters
    ----------
    config:
        Supplies the SLO block, which may override the default targets.
    objectives:
        Overrides the defaults entirely when supplied.
    retention_s:
        How long observations are kept. Bounded because an unbounded history is a memory leak in a
        long-running org.
    """

    config: Config | None = None
    objectives: tuple[Objective, ...] | None = None
    retention_s: float = 30 * 24 * 3600.0
    now: Any = time.time

    def __post_init__(self) -> None:
        self._observations: Deque[_Observation] = deque()
        self._alerts: list[dict[str, Any]] = []
        if self.objectives is None:
            self.objectives = self._from_config()
        self._last_alert: dict[str, float] = {}

    def _from_config(self) -> tuple[Objective, ...]:
        """Build objectives from the config's slo block, falling back to the defaults.

        A configured target replaces the matching default rather than replacing the whole set, so
        a config that tunes one objective keeps the others.
        """
        configured = getattr(self.config, "slo", None) if self.config else None
        run_targets = dict(getattr(configured, "run", {}) or {}) if configured else {}
        agent_targets = dict(getattr(configured, "agent", {}) or {}) if configured else {}

        out: list[Objective] = []
        for objective in DEFAULT_OBJECTIVES:
            target = objective.target
            if objective.scope == "run":
                if objective.metric == "escalation_rate":
                    # The config expresses an *escalation rate* target, which is the complement of
                    # the good fraction, so it is inverted here rather than compared directly.
                    configured_rate = float(run_targets.get("escalation_rate_target", 0.0) or 0.0)
                    if configured_rate:
                        target = max(0.0, 1.0 - configured_rate)
            elif objective.scope == "agent" and objective.metric == "success_rate":
                configured_rate = float(agent_targets.get("success_rate_target", 0.0) or 0.0)
                if configured_rate:
                    target = configured_rate
            out.append(Objective(
                name=objective.name, metric=objective.metric, target=target,
                scope=objective.scope, window_s=objective.window_s,
                description=objective.description,
            ))
        return tuple(out)

    # ── observing ───────────────────────────────────────────────────────────

    def observe(self, *, metric: str, good: bool, scope: str = "run") -> None:
        """Record one event against a metric.

        `good` is the event's contribution: a completed run is good for the escalation metric, a
        successful task is good for the success metric.
        """
        self._observations.append(_Observation(at=self.now(), good=bool(good),
                                               metric=metric, scope=scope))
        self._trim()

    def observe_run(self, *, outcome: str, escalated: bool = False) -> None:
        """Record a run outcome against the escalation objective."""
        self.observe(metric="escalation_rate", good=not escalated and outcome == "complete",
                     scope="run")

    def observe_agent_task(self, *, outcome: str, guardrail_block: bool = False) -> None:
        """Record an agent task against the success and guardrail objectives."""
        self.observe(metric="success_rate", good=outcome == "success", scope="agent")
        self.observe(metric="guardrail_rate", good=not guardrail_block, scope="run")

    def _trim(self) -> None:
        """Drop observations older than the retention window."""
        cutoff = self.now() - self.retention_s
        while self._observations and self._observations[0].at < cutoff:
            self._observations.popleft()

    # ── evaluation ──────────────────────────────────────────────────────────

    def evaluate(self, objective: Objective | None = None, *,
                 window_s: float | None = None) -> SLOReport | list[SLOReport]:
        """Evaluate one objective or all of them.

        Burn rate is computed per window: a short window catches a spike, the objective's full
        window catches a slow burn. Both are reported so a caller can distinguish the two.
        """
        if objective is None:
            return [self.evaluate(o) for o in self.objectives or ()]
        now = self.now()
        retention = max(window_s or objective.window_s, 60.0)

        relevant = [o for o in self._observations
                    if o.metric == objective.metric and o.at >= now - retention]
        good = sum(1 for o in relevant if o.good)
        total = len(relevant)

        # Two windows: a short one for spikes and a long one for drift. The 1/24 ratio mirrors the
        # library's multi-window burn-rate guidance (short window to page, long window to confirm).
        short_window = min(objective.window_s, max(600.0, objective.window_s / 24.0))
        rates = [
            self._burn_rate(objective, good, total, short_window, label="short"),
            self._burn_rate(objective, good, total, max(retention, objective.window_s), label="long"),
        ]
        report = SLOReport(objective=objective, observed_good=good, observed_total=total,
                           burn_rates=rates, at=_iso_now())
        self._maybe_alert(report)
        return report

    def _burn_rate(self, objective: Objective, good: int, total: int, window_s: float,
                   *, label: str) -> BurnRate:
        """Compute the burn rate for one window and band it.

        The rate is `bad_fraction / budget_fraction`, and the bands follow the library's alerting
        guidance: 2× warns, 14.4× is critical because a month's budget would go in about an hour.
        """
        if total == 0:
            return BurnRate(window_s=window_s, rate=0.0, severity=Severity.INFO, good=0, total=0,
                            action="no observations in this window")
        bad_fraction = 1.0 - (good / total)
        budget = objective.error_budget
        rate = bad_fraction / budget if budget > 0 else (0.0 if bad_fraction == 0 else float("inf"))

        if rate >= 14.4:
            severity = Severity.CRITICAL
            action = (
                "page now: at this rate the error budget is exhausted within about an hour "
                "(14.4x burn)"
            )
        elif rate >= 2.0:
            severity = Severity.WARNING
            action = "investigate during business hours: burn is above the sustainable rate (2x)"
        else:
            severity = Severity.INFO
            action = "within budget"
        return BurnRate(window_s=window_s, rate=rate, severity=severity,
                        good=good, total=total, action=f"[{label}] {action}",
                        objective_window_s=objective.window_s)

    def _maybe_alert(self, report: SLOReport) -> None:
        """Record an alert, rate-limited per objective.

        Rate limiting matters here: the same condition re-evaluated every minute would otherwise
        produce an alert per evaluation, which is how an alerting system teaches people to ignore
        it.
        """
        if report.severity is Severity.INFO:
            return
        last = self._last_alert.get(report.objective.name, 0.0)
        if self.now() - last < 60.0:
            return
        self._last_alert[report.objective.name] = self.now()
        self._alerts.append({
            "objective": report.objective.name,
            "severity": report.severity.value,
            "attainment": round(report.attainment, 4),
            "budget_remaining": round(report.budget_remaining, 4),
            "at": _iso_now(),
        })

    # ── reporting ───────────────────────────────────────────────────────────

    def summary(self) -> str:
        """A readable SLO table, for the CLI and the owner console."""
        reports = self.evaluate()
        if isinstance(reports, SLOReport):
            reports = [reports]
        if not reports:
            return "No SLOs are configured."
        lines = ["objective              target  observed  budget  severity"]
        for report in reports:
            marker = "✗" if not report.meeting else "✓"
            lines.append(
                f"{report.objective.name:22s} {report.objective.target:6.2f}  "
                f"{report.attainment:8.2f}  {report.budget_remaining:6.2f}  "
                f"{report.severity.value} {marker}"
            )
        return "\n".join(lines)

    def report(self) -> dict[str, Any]:
        """All reports plus the alert feed, for the economics and health dashboards."""
        reports = self.evaluate()
        if isinstance(reports, SLOReport):
            reports = [reports]
        return {
            "objectives": [r.as_dict() for r in reports],
            "alerts": list(self._alerts[-50:]),
            "observations": len(self._observations),
        }

    def alerts(self) -> list[dict[str, Any]]:
        """Every recorded alert."""
        return list(self._alerts)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
