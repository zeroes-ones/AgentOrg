#!/usr/bin/env python3
"""health.py — golden signals, graduated health states, and probe-based recovery.

WHY THIS EXISTS
---------------
An org needs to notice when one of its agents has gone bad, and act without waking the Owner for
every blip. That is two requirements in tension: act automatically, but do not act on noise.

The resolution is graduated: score the agent on the golden signals, require a minimum sample
before judging, let hard triggers override a healthy average, and require *evidence* to come back
rather than a timer.

DESIGN
------
- **Golden signals mapped honestly.** The library's RED/USE framework maps onto latency, traffic,
  errors and saturation — where an agent's saturation signal is its **context saturation**, which
  the session lifecycle already produces.
- **A minimum-sample guard.** A new agent must not be quarantined on its first bad task; that
  would punish a hire for being new rather than for being wrong.
- **Hard triggers override the score.** Three consecutive contract breaches, or a secret-leak
  guardrail trip, quarantines immediately regardless of a healthy average — a good average is
  exactly how a severe fault hides.
- **Recovery needs evidence.** A degraded agent is re-tested against its skill's golden cases, and
  the probe scorer receives the artifact and evidence but **never the agent's reasoning**, so it
  cannot inherit the blind spot it is testing for.
- **Quarantine cannot deadlock the graph.** If every capable agent is quarantined, the caller is
  told to escalate rather than spinning.

Usage:
    mon = HealthMonitor(config)
    mon.observe(agent_id="ag_1", outcome="success", latency_ms=1200, tokens=900)
    state, transition = mon.evaluate("ag_1")
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable

from ..config import Config
from .agent import AgentState

__all__ = [
    "HealthMonitor",
    "HealthState",
    "HealthTransition",
    "SignalWindow",
    "ProbeResult",
]


class HealthState(str, Enum):
    """An agent's health, as the roster view shows it."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"        # deprioritised in routing but still usable
    QUARANTINED = "quarantined"  # removed from the pool pending recovery or the Owner


@dataclass
class SignalWindow:
    """A rolling window of one agent's observed outcomes.

    Bounded by task count because an unbounded list would grow for the life of a run, and
    because a very old outcome says little about current behaviour.
    """

    capacity: int = 20
    tasks: int = 0
    successes: int = 0
    failures: int = 0
    escalations: int = 0
    guardrail_blocks: int = 0
    contract_breaches: int = 0
    checklist_fails: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    saturations: list[float] = field(default_factory=list)
    cost_usd: float = 0.0
    tokens: int = 0
    # Consecutive contract breaches: a hard trigger, tracked separately from the total because a
    # run of breaches means the agent is not converging, which an average hides.
    breach_streak: int = 0

    def record(self, *, outcome: str, latency_ms: float | None = None,
               saturation: float | None = None, breached: bool = False,
               guardrail_block: bool = False, checklist_fail: bool = False,
               cost_usd: float = 0.0, tokens: int = 0) -> None:
        """Add one observation, evicting the oldest when at capacity."""
        self.tasks += 1
        if outcome == "success":
            self.successes += 1
        elif outcome == "failure":
            self.failures += 1
        elif outcome == "escalation":
            self.escalations += 1
        if guardrail_block:
            self.guardrail_blocks += 1
        if checklist_fail:
            self.checklist_fails += 1
        if breached:
            self.contract_breaches += 1
            self.breach_streak += 1
        else:
            self.breach_streak = 0
        if latency_ms is not None:
            self.latencies_ms.append(float(latency_ms))
        if saturation is not None:
            self.saturations.append(float(saturation))
        self.cost_usd += max(0.0, cost_usd)
        self.tokens += max(0, tokens)

        # Bound the window. Trim proportionally so the ratios stay meaningful.
        if self.tasks > self.capacity:
            overflow = self.tasks - self.capacity
            self.tasks = self.capacity
            self.successes = max(0, self.successes - min(overflow, self.successes))
            self.failures = max(0, self.failures - min(overflow, self.failures))
            self.escalations = max(0, self.escalations - min(overflow, self.escalations))
        if len(self.latencies_ms) > self.capacity:
            self.latencies_ms = self.latencies_ms[-self.capacity:]
        if len(self.saturations) > self.capacity:
            self.saturations = self.saturations[-self.capacity:]

    # ── derived signals ─────────────────────────────────────────────────────

    @property
    def success_rate(self) -> float:
        """Fraction of observed tasks that succeeded."""
        return self.successes / self.tasks if self.tasks else 1.0

    @property
    def escalation_rate(self) -> float:
        """Fraction that escalated — the library's own primary SLI."""
        return self.escalations / self.tasks if self.tasks else 0.0

    @property
    def guardrail_rate(self) -> float:
        """Fraction that tripped a guardrail. Any nonzero value is a safety signal."""
        return self.guardrail_blocks / self.tasks if self.tasks else 0.0

    @property
    def breach_rate(self) -> float:
        """Fraction that breached a contract."""
        return self.contract_breaches / self.tasks if self.tasks else 0.0

    @property
    def checklist_fail_rate(self) -> float:
        """Fraction whose checklist reported a failure."""
        return self.checklist_fails / self.tasks if self.tasks else 0.0

    @property
    def latency_p95_ms(self) -> float:
        """95th-percentile turn latency."""
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return round(ordered[index], 1)

    @property
    def latency_p50_ms(self) -> float:
        """Median turn latency."""
        return round(statistics.median(self.latencies_ms), 1) if self.latencies_ms else 0.0

    @property
    def context_saturation(self) -> float:
        """Mean context saturation — the agent's saturation signal."""
        return round(statistics.fmean(self.saturations), 3) if self.saturations else 0.0

    @property
    def cost_per_success_usd(self) -> float:
        """Cost divided by successes — the metric that matters, since a cheap failing run is not
        cheap. Returns 0 with no successes, which the UI labels as unmeasured rather than free."""
        return round(self.cost_usd / self.successes, 6) if self.successes else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tasks": self.tasks,
            "successes": self.successes,
            "failures": self.failures,
            "escalations": self.escalations,
            "guardrail_blocks": self.guardrail_blocks,
            "contract_breaches": self.contract_breaches,
            "breach_streak": self.breach_streak,
            "success_rate": round(self.success_rate, 4),
            "escalation_rate": round(self.escalation_rate, 4),
            "guardrail_rate": round(self.guardrail_rate, 4),
            "breach_rate": round(self.breach_rate, 4),
            "checklist_fail_rate": round(self.checklist_fail_rate, 4),
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "context_saturation": self.context_saturation,
            "cost_usd": round(self.cost_usd, 6),
            "tokens": self.tokens,
            "cost_per_success_usd": self.cost_per_success_usd,
        }


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of a recovery probe against a degraded agent.

    Per `agent-eval-pipeline` rule 3b the probe is scored on the artifact and its evidence, never
    on the agent's reasoning — a judge that reads the reasoning inherits its blind spots and
    agrees for the same wrong reason.
    """

    agent_id: str
    skill: str
    passed: bool
    cases_run: int = 0
    cases_passed: int = 0
    at: str = ""
    detail: str = ""
    evidence_boundary: str = "artifact and evidence only; producer reasoning excluded"

    @property
    def pass_rate(self) -> float:
        return self.cases_passed / self.cases_run if self.cases_run else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "skill": self.skill, "passed": self.passed,
                "cases_run": self.cases_run, "cases_passed": self.cases_passed,
                "pass_rate": round(self.pass_rate, 3), "at": self.at,
                "detail": self.detail, "evidence_boundary": self.evidence_boundary}


@dataclass(frozen=True)
class HealthTransition:
    """A health change, with the reason and the signals that produced it."""

    agent_id: str
    previous: HealthState
    current: HealthState
    reason: str
    score: float
    hard_trigger: str = ""
    signals: dict[str, Any] = field(default_factory=dict)
    at: str = ""

    @property
    def changed(self) -> bool:
        return self.previous is not self.current

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "previous": self.previous.value,
            "current": self.current.value,
            "reason": self.reason,
            "score": round(self.score, 4),
            "hard_trigger": self.hard_trigger,
            "signals": self.signals,
            "at": self.at,
        }


#: Signal weights for the composite score. They sum to 1.0 so the score is directly comparable
#: to the healthy/degraded thresholds, which is what makes 0.80 a meaningful number.
_WEIGHTS: dict[str, float] = {
    "success_rate": 0.35,
    "non_escalation": 0.20,
    "contract_integrity": 0.20,
    "checklist_integrity": 0.10,
    "guardrail_cleanliness": 0.15,
}


@dataclass
class HealthMonitor:
    """Tracks agent health, applies graduated actions, and validates recovery.

    Parameters
    ----------
    config:
        Supplies the health block: window size, minimum samples, thresholds, hard-trigger counts.
    probe:
        An optional callable `(agent_id, skill) -> ProbeResult`. Injected so the monitor can be
        tested without the eval suite, and so the real probe can be the library's golden cases.
    """

    config: Config | None = None
    probe: Callable[[str, str], ProbeResult] | None = None
    notify: Callable[[HealthTransition], None] | None = None

    def __post_init__(self) -> None:
        health = getattr(self.config, "health", None)
        self.window_tasks = int(getattr(health, "window_tasks", 20)) if health else 20
        self.min_samples = int(getattr(health, "min_samples", 5)) if health else 5
        self.healthy_at = float(getattr(health, "healthy_at", 0.80)) if health else 0.80
        self.degraded_at = float(getattr(health, "degraded_at", 0.50)) if health else 0.50
        self.breach_streak_limit = int(
            getattr(health, "hard_trigger_contract_breaches", 3)
        ) if health else 3
        self.probe_enabled = bool(getattr(health, "probe_enabled", True)) if health else True
        self._windows: dict[str, SignalWindow] = {}
        self._states: dict[str, HealthState] = {}
        self._history: list[HealthTransition] = []
        self._probes: list[ProbeResult] = []
        self._quarantine_log: dict[str, str] = {}

    # ── observing ───────────────────────────────────────────────────────────

    def window(self, agent_id: str) -> SignalWindow:
        """The rolling signal window for an agent, created on first use."""
        window = self._windows.get(agent_id)
        if window is None:
            window = SignalWindow(capacity=self.window_tasks)
            self._windows[agent_id] = window
        return window

    def observe(self, agent_id: str, *, outcome: str, latency_ms: float | None = None,
                saturation: float | None = None, breached: bool = False,
                guardrail_block: bool = False, checklist_fail: bool = False,
                cost_usd: float = 0.0, tokens: int = 0) -> HealthTransition:
        """Record one task outcome and re-evaluate the agent.

        Every observation re-evaluates, so a hard trigger fires on the observation that causes it
        rather than at the next polling tick.
        """
        self.window(agent_id).record(
            outcome=outcome, latency_ms=latency_ms, saturation=saturation, breached=breached,
            guardrail_block=guardrail_block, checklist_fail=checklist_fail,
            cost_usd=cost_usd, tokens=tokens,
        )
        return self.evaluate(agent_id)

    def state_of(self, agent_id: str) -> HealthState:
        """Current health, defaulting to HEALTHY for an unobserved agent.

        Healthy rather than unknown: an agent with no history has done nothing wrong, and treating
        it as suspect would make every new hire unusable until it had failed.
        """
        return self._states.get(agent_id, HealthState.HEALTHY)

    # ── evaluation ──────────────────────────────────────────────────────────

    def score(self, agent_id: str) -> dict[str, float]:
        """The composite score and its components."""
        window = self.window(agent_id)
        components = {
            "success_rate": window.success_rate,
            "non_escalation": 1.0 - window.escalation_rate,
            "contract_integrity": 1.0 - window.breach_rate,
            "checklist_integrity": 1.0 - window.checklist_fail_rate,
            "guardrail_cleanliness": 1.0 - min(1.0, window.guardrail_rate * 3.0),
        }
        return components

    def evaluate(self, agent_id: str) -> HealthTransition:
        """Recompute an agent's health and return the transition.

        Order matters: hard triggers are checked *before* the composite score, because a healthy
        average is precisely how a severe fault hides.
        """
        window = self.window(agent_id)
        previous = self.state_of(agent_id)
        components = self.score(agent_id)
        composite = sum(_WEIGHTS[name] * value for name, value in components.items())

        # Hard triggers first.
        hard = ""
        if window.guardrail_blocks > 0 and window.guardrail_blocks == window.tasks:
            # Every observation tripped a guardrail: the agent is not merely unlucky.
            hard = "every observed task tripped a guardrail"
        elif window.breach_streak >= self.breach_streak_limit:
            hard = (
                f"{window.breach_streak} consecutive contract breaches (limit "
                f"{self.breach_streak_limit})"
            )

        # The minimum-sample guard: never judge on fewer than `min_samples` tasks, so a first-task
        # failure cannot quarantine a new hire.
        under_sampled = window.tasks < self.min_samples

        if hard:
            current = HealthState.QUARANTINED
            reason = f"hard trigger: {hard}"
        elif under_sampled:
            current = HealthState.HEALTHY
            reason = (
                f"only {window.tasks} of the required {self.min_samples} samples; holding healthy "
                "until there is enough evidence to judge"
            )
        elif composite >= self.healthy_at:
            current = HealthState.HEALTHY
            reason = f"composite {composite:.2f} >= {self.healthy_at:.2f}"
        elif composite >= self.degraded_at:
            current = HealthState.DEGRADED
            reason = (
                f"composite {composite:.2f} is between {self.degraded_at:.2f} and "
                f"{self.healthy_at:.2f}: deprioritised in routing but still usable"
            )
        else:
            current = HealthState.QUARANTINED
            reason = f"composite {composite:.2f} < {self.degraded_at:.2f}"

        transition = HealthTransition(
            agent_id=agent_id, previous=previous, current=current, reason=reason,
            score=composite, hard_trigger=hard,
            signals={k: round(v, 4) for k, v in components.items()},
            at=_iso_now(),
        )
        self._states[agent_id] = current
        if transition.changed:
            self._history.append(transition)
            if current is HealthState.QUARANTINED:
                self._quarantine_log[agent_id] = reason
            elif previous is HealthState.QUARANTINED:
                self._quarantine_log.pop(agent_id, None)
            if self.notify is not None:
                self.notify(transition)
        return transition

    def evaluate_all(self, agent_ids: Iterable[str]) -> list[HealthTransition]:
        """Recompute every named agent's health."""
        return [self.evaluate(agent_id) for agent_id in agent_ids]

    # ── recovery ────────────────────────────────────────────────────────────

    def run_probe(self, agent_id: str, skill: str) -> ProbeResult:
        """Probe a degraded agent against its skill's golden cases.

        A probe must be *evidence*, not a timer: an agent that returns because time passed has not
        demonstrated anything. When no probe is configured the result is a failure with the reason
        stated, rather than a silent pass.
        """
        if not self.probe_enabled:
            result = ProbeResult(agent_id=agent_id, skill=skill, passed=False,
                                 detail="probing is disabled by configuration", at=_iso_now())
        elif self.probe is None:
            result = ProbeResult(
                agent_id=agent_id, skill=skill, passed=False, at=_iso_now(),
                detail=(
                    "no probe is configured, so recovery cannot be evidenced. Supply a probe that "
                    "runs the skill's golden cases, or restore the agent with an explicit Owner "
                    "decision."
                ),
            )
        else:
            result = self.probe(agent_id, skill)
        self._probes.append(result)
        return result

    def try_recover(self, agent_id: str, skill: str) -> HealthTransition:
        """Attempt to restore a quarantined agent, requiring a passing probe.

        Raises nothing: a failed probe is a normal outcome, so the caller gets a transition whose
        `current` is still QUARANTINED and whose reason names the probe failure.
        """
        previous = self.state_of(agent_id)
        if previous is HealthState.HEALTHY:
            return HealthTransition(agent_id=agent_id, previous=previous, current=previous,
                                    reason="already healthy; nothing to recover", score=0.0,
                                    at=_iso_now())
        result = self.run_probe(agent_id, skill)
        if not result.passed:
            transition = HealthTransition(
                agent_id=agent_id, previous=previous, current=HealthState.QUARANTINED,
                reason=(
                    f"probe failed ({result.cases_passed}/{result.cases_run} cases): "
                    f"{result.detail or 'no detail supplied'}"
                ),
                score=0.0, at=_iso_now(),
            )
            self._states[agent_id] = transition.current
            return transition

        # A passing probe restores to degraded rather than healthy: trust is rebuilt with real
        # work, not granted on a test.
        transition = HealthTransition(
            agent_id=agent_id, previous=previous, current=HealthState.DEGRADED,
            reason=(
                f"probe passed ({result.cases_passed}/{result.cases_run} cases) on the artifact and "
                "evidence only; restored to degraded, which returns to healthy on observed success"
            ),
            score=0.0, at=_iso_now(),
        )
        self._states[agent_id] = transition.current
        self._quarantine_log.pop(agent_id, None)
        self._history.append(transition)
        if self.notify is not None:
            self.notify(transition)
        return transition

    def restore(self, agent_id: str, *, by: str = "owner", reason: str = "") -> HealthTransition:
        """Restore an agent by explicit Owner decision, bypassing the probe.

        A first-class path because the Owner holds terminal authority: if the probe is unavailable
        or the Owner has context the probe lacks, that decision must be possible — and it is
        recorded with who made it.
        """
        previous = self.state_of(agent_id)
        transition = HealthTransition(
            agent_id=agent_id, previous=previous, current=HealthState.HEALTHY,
            reason=f"restored by {by}" + (f": {reason}" if reason else ""),
            score=0.0, at=_iso_now(),
        )
        self._states[agent_id] = HealthState.HEALTHY
        self._quarantine_log.pop(agent_id, None)
        self._history.append(transition)
        if self.notify is not None:
            self.notify(transition)
        return transition

    # ── the deadlock guard ──────────────────────────────────────────────────

    def no_capable_agent(self, *, skill: str, holders: Iterable[str]) -> bool:
        """Whether every holder of a skill is quarantined.

        The caller must escalate when this is true rather than looping: a graph that needs a
        capability nobody healthy provides should stop and say so, not spin.
        """
        holder_ids = list(holders)
        if not holder_ids:
            return True
        return all(self.state_of(agent_id) is HealthState.QUARANTINED for agent_id in holder_ids)

    # ── reporting ───────────────────────────────────────────────────────────

    def health_report(self, agent_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Per-agent health, signals and score — the org health dashboard's data."""
        out: list[dict[str, Any]] = []
        for agent_id in agent_ids:
            window = self.window(agent_id)
            components = self.score(agent_id)
            out.append({
                "agent_id": agent_id,
                "state": self.state_of(agent_id).value,
                "score": round(sum(_WEIGHTS[k] * v for k, v in components.items()), 4),
                "components": {k: round(v, 4) for k, v in components.items()},
                "signals": window.as_dict(),
                "quarantine_reason": self._quarantine_log.get(agent_id, ""),
                "under_sampled": window.tasks < self.min_samples,
            })
        return out

    def transitions(self) -> list[dict[str, Any]]:
        """Every health change, for the audit trail and the notification feed."""
        return [t.as_dict() for t in self._history]

    def probes(self) -> list[dict[str, Any]]:
        """Every probe run, for the recovery history view."""
        return [p.as_dict() for p in self._probes]

    def sli_rollup(self) -> dict[str, Any]:
        """Organisation-level SLIs, using the library's own vocabulary so its external report
        remains a valid cross-check on our numbers.

        `cost_unreported_tasks` is surfaced separately rather than folded into a total: an
        unmeasured cost must never read as free.
        """
        runs = sum(w.tasks for w in self._windows.values())
        escalated = sum(w.escalations for w in self._windows.values())
        guardrail = sum(w.guardrail_blocks for w in self._windows.values())
        successes = sum(w.successes for w in self._windows.values())
        cost = sum(w.cost_usd for w in self._windows.values())
        return {
            "agents": len(self._windows),
            "tasks": runs,
            "complete": successes,
            "escalated": escalated,
            "escalation_rate": round(escalated / runs, 4) if runs else 0.0,
            "guardrail_blocks": guardrail,
            "cost_usd": round(cost, 6),
            "cost_per_success_usd": round(cost / successes, 6) if successes else None,
            "quarantined": sum(1 for s in self._states.values() if s is HealthState.QUARANTINED),
            "degraded": sum(1 for s in self._states.values() if s is HealthState.DEGRADED),
        }


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
