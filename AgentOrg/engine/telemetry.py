#!/usr/bin/env python3
"""telemetry.py — OTel-shaped spans, with the library's stable naming contract.

WHY THIS EXISTS
---------------
Run state is already a trace; the library is explicit that it is "one exporter away from
Langfuse/Phoenix-class visibility". This module is that exporter, and it exists because a claim of
"observability" that cannot be pointed at is not one.

The naming is a **contract, not a convention**: `session.<workflow>` and
`workflow.<workflow>.node.<id>` are the library's own stable span names, so a span pipeline
configured against its exporter works against ours without re-instrumentation. Inventing new names
would make the two incompatible for no gain.

DESIGN
------
- **The vocabulary is the library's.** `escalation_rate`, `cost_per_success_usd`, `guardrail_blocks`,
  `cost_unreported_runs` — so `skill-sli-report.py` remains a valid external cross-check rather than a
  competing definition.
- **An unmeasured run is never free.** `usage_reported` and `cost_measured` distinguish "spent nothing"
  from "not measured", because conflating them is how a cost dashboard lies.
- **Sampling is 100% on escalations, guardrail trips and health transitions**, per the library's
  policy — those are the events worth having every one of.
- **Skill content hashes travel on every span**, so "which prompt produced this output?" is
  answerable.
- **Spans are JSONL**, so an ordinary tool can read them and a span pipeline can ingest them.

Usage:
    exporter = SpanExporter(path=state_dir / "telemetry" / "spans.jsonl")
    exporter.session_span(run_id="run_1", workflow="booking", outcome="complete", ...)
    exporter.node_span(...)
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "SamplingPolicy",
    "Span",
    "SpanExporter",
    "TelemetryError",
    "naming",
]

#: The library's stable span-name patterns. These are a contract: a pipeline built against its
#: exporter consumes ours unchanged.
_SESSION_NAME = "session.{workflow}"
_NODE_NAME = "workflow.{workflow}.node.{node}"
#: Our own spans, named in the same shape so one naming rule covers everything.
_ROTATION_NAME = "agent.{agent}.session.{index}"
_DELEGATION_NAME = "agent.{agent}.delegation.{index}"
_HEALTH_NAME = "agent.{agent}.health"


class TelemetryError(RuntimeError):
    """Raised when a span cannot be written."""


def naming() -> dict[str, str]:
    """The span-name patterns, so a consumer can assert them rather than guess."""
    return {
        "session": _SESSION_NAME,
        "node": _NODE_NAME,
        "rotation": _ROTATION_NAME,
        "delegation": _DELEGATION_NAME,
        "health": _HEALTH_NAME,
    }


@dataclass(frozen=True)
class SamplingPolicy:
    """Which spans are always recorded, and what fraction otherwise.

    The default is 100% everywhere, which is right for a desktop application where a run is a few
    hundred spans rather than millions of requests. The knobs exist so a long soak run can be dialled
    down without losing the events that matter.
    """

    default: float = 1.0
    on_escalation: float = 1.0
    on_guardrail: float = 1.0
    on_health_change: float = 1.0

    def rate_for(self, *, escalated: bool = False, guardrail: bool = False,
                 health_change: bool = False) -> float:
        """The sampling rate for one span, taking the highest applicable rate.

        A span that both escalated and tripped a guardrail is sampled at the more generous of the two
        — never dropped because one policy happened to be lower.
        """
        rates = [self.default]
        if escalated:
            rates.append(self.on_escalation)
        if guardrail:
            rates.append(self.on_guardrail)
        if health_change:
            rates.append(self.on_health_change)
        return max(rates)


@dataclass
class Span:
    """One OTel-shaped span.

    Attributes are stable keys, mirroring the library's exporter: `status`, `verdict`, `evidence`,
    `iterations`, plus cost and token figures where they exist.
    """

    name: str
    trace_id: str
    span_id: str
    run_id: str = ""
    parent_span_id: str = ""
    kind: str = "internal"           # session | node | rotation | delegation | health
    status: str = ""
    verdict: str = ""
    started_at: str = ""
    duration_ms: float = 0.0
    iterations: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    # Cost and usage. `usage_reported` and `cost_measured` are the honesty flags.
    tokens_prompt: int | None = None
    tokens_completion: int | None = None
    cost_usd: float | None = None
    usage_reported: bool = False
    cost_measured: bool = False
    skill_hashes: dict[str, str] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """The wire form. OTel-shaped: `trace_id`, `span_id`, `name`, `kind`, `attributes`."""
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "kind": self.kind,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status,
            "verdict": self.verdict,
            "iterations": self.iterations,
            "attributes": self.attributes,
            "tokens": {
                "prompt": self.tokens_prompt,
                "completion": self.tokens_completion,
                "total": (
                    (self.tokens_prompt or 0) + (self.tokens_completion or 0)
                    if self.usage_reported else None
                ),
            },
            "cost": {
                "usd": round(self.cost_usd, 6) if self.cost_measured else None,
                "measured": self.cost_measured,
            },
            "usage_reported": self.usage_reported,
            "skill_hashes": self.skill_hashes,
            "events": self.events,
        }


class SpanExporter:
    """Writes spans to a JSONL file, with sampling and a bounded in-memory tail.

    Parameters
    ----------
    path:
        Where spans are written. `None` disables export entirely, which is what a caller wanting a
        telemetry-free run passes.
    sampling:
        The policy deciding which spans are recorded.
    tail_size:
        How many recent spans are kept in memory for the UI.
    """

    def __init__(self, path: os.PathLike | str | None = None, *,
                 sampling: SamplingPolicy | None = None, run_id: str = "",
                 tail_size: int = 2000) -> None:
        self.path = Path(path) if path else None
        self.sampling = sampling or SamplingPolicy()
        self.run_id = run_id
        self._lock = threading.RLock()
        self._tail: list[Span] = []
        self._tail_size = max(1, tail_size)
        self._counters = {"emitted": 0, "dropped": 0}
        self._fh: Any = None
        self._span_counter = 0
        self._trace_id = _hash(run_id or f"trace-{time.time()}")

    # ── identity ────────────────────────────────────────────────────────────

    def _next_span_id(self) -> str:
        """A short, unique span id, monotonically increasing within the run."""
        with self._lock:
            self._span_counter += 1
            return _hash(f"{self._trace_id}:{self._span_counter}")[:16]

    # ── writing ─────────────────────────────────────────────────────────────

    def _should_record(self, *, escalated: bool = False, guardrail: bool = False,
                       health_change: bool = False) -> bool:
        """Apply the sampling policy deterministically.

        The decision is derived from a counter rather than a random draw, so a run's spans are
        reproducible — a flaky sampling decision would make a trace impossible to compare with another.
        """
        rate = self.sampling.rate_for(escalated=escalated, guardrail=guardrail,
                                      health_change=health_change)
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        with self._lock:
            index = self._counters["emitted"] + self._counters["dropped"]
        # A deterministic stride: record every 1/rate-th span.
        return (index % max(1, int(round(1.0 / rate)))) == 0

    def emit(self, span: Span, *, escalated: bool = False, guardrail: bool = False,
             health_change: bool = False) -> Span | None:
        """Record a span, subject to sampling. Returns it, or None when dropped.

        Dropping is counted rather than silent, so a consumer can tell a quiet run from a sampled one.
        """
        if not self._should_record(escalated=escalated, guardrail=guardrail,
                                   health_change=health_change):
            with self._lock:
                self._counters["dropped"] += 1
            return None
        if not span.span_id:
            span.span_id = self._next_span_id()
        if not span.trace_id:
            span.trace_id = self._trace_id
        if not span.run_id:
            span.run_id = self.run_id
        if not span.started_at:
            span.started_at = _iso_now()

        with self._lock:
            self._counters["emitted"] += 1
            self._tail.append(span)
            if len(self._tail) > self._tail_size:
                self._tail.pop(0)
            self._write(span)
        return span

    def _write(self, span: Span) -> None:
        """Append one span as JSONL, tolerating a write failure without killing the run."""
        if self.path is None:
            return
        try:
            if self._fh is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self.path, "a", encoding="utf-8")
            self._fh.write(json.dumps(span.as_dict(), separators=(",", ":"), sort_keys=True) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError as exc:
            # A telemetry failure must never stop the work it was observing.
            raise TelemetryError(f"cannot write span to {self.path}: {exc}") from exc

    def close(self) -> None:
        """Flush and close the file handle. Idempotent."""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                finally:
                    self._fh = None

    # ── span constructors ───────────────────────────────────────────────────

    def session_span(self, *, workflow: str, outcome: str, **kwargs: Any) -> Span | None:
        """The `session.<workflow>` span: one per run, rolling up everything beneath it."""
        payload = {
            "workflow": workflow,
            "outcome": outcome,
            "complete": outcome == "complete",
            "escalated": outcome == "escalated",
            **kwargs.pop("attributes", {}),
        }
        span = Span(name=_SESSION_NAME.format(workflow=workflow), kind="session",
                    trace_id=self._trace_id, span_id="", status=outcome,
                    attributes=payload, **kwargs)
        return self.emit(span, escalated=span.attributes.get("escalated", False),
                         guardrail=span.attributes.get("guardrail_blocks", 0) > 0)

    def node_span(self, *, workflow: str, node: str, status: str = "", verdict: str = "",
                  parent_span_id: str = "", iterations: int = 0,
                  duration_ms: float = 0.0, skill_hash: str = "",
                  tokens_prompt: int | None = None, tokens_completion: int | None = None,
                  cost_usd: float | None = None, evidence: Iterable[str] = (),
                  attributes: dict[str, Any] | None = None) -> Span | None:
        """The `workflow.<workflow>.node.<id>` span: one per executed node.

        The skill hash travels here, so a span answers "which prompt produced this output?" without
        needing a second lookup.
        """
        usage_reported = tokens_prompt is not None or tokens_completion is not None
        cost_measured = cost_usd is not None
        span = Span(
            name=_NODE_NAME.format(workflow=workflow, node=node), kind="node",
            trace_id=self._trace_id, span_id="", parent_span_id=parent_span_id,
            status=status, verdict=verdict, iterations=iterations,
            duration_ms=duration_ms,
            tokens_prompt=tokens_prompt, tokens_completion=tokens_completion,
            cost_usd=cost_usd, usage_reported=usage_reported, cost_measured=cost_measured,
            skill_hashes={node: skill_hash} if skill_hash else {},
            attributes={"workflow": workflow, "node": node,
                        "evidence": list(evidence), **(attributes or {})},
        )
        return self.emit(span, escalated=status == "escalated",
                         guardrail=bool((attributes or {}).get("guardrail_block")))

    def rotation_span(self, *, agent: str, index: int, trigger: str, reason: str,
                      saturation: float, attention_weight: float,
                      constraints_carried: int, parent_span_id: str = "") -> Span | None:
        """A session rotation. Named in the library's shape so one rule covers every span."""
        return self.emit(Span(
            name=_ROTATION_NAME.format(agent=agent, index=index), kind="rotation",
            trace_id=self._trace_id, span_id="", parent_span_id=parent_span_id,
            status="rotated", attributes={
                "agent_id": agent, "session_index": index, "trigger": trigger, "reason": reason,
                "saturation": round(saturation, 4),
                "attention_weight": round(attention_weight, 4),
                "constraints_carried": constraints_carried,
            },
        ))

    def delegation_span(self, *, agent: str, index: int, skill: str, kind: str, tier: str,
                        approved: bool, parent_span_id: str = "") -> Span | None:
        """A delegation. `tier` records which approval authority decided it."""
        return self.emit(Span(
            name=_DELEGATION_NAME.format(agent=agent, index=index), kind="delegation",
            trace_id=self._trace_id, span_id="", parent_span_id=parent_span_id,
            status="approved" if approved else "gated", attributes={
                "agent_id": agent, "skill": skill, "delegation_kind": kind,
                "tier": tier, "approved": approved,
            },
        ))

    def health_span(self, *, agent: str, previous: str, current: str, reason: str,
                    score: float, hard_trigger: str = "") -> Span | None:
        """A health transition. Always sampled: a quarantine is worth having every one of."""
        return self.emit(Span(
            name=_HEALTH_NAME.format(agent=agent), kind="health",
            trace_id=self._trace_id, span_id="",
            status=current, attributes={
                "agent_id": agent, "previous": previous, "current": current,
                "reason": reason, "score": round(score, 4), "hard_trigger": hard_trigger,
            },
        ), health_change=True)

    # ── SLI rollup ──────────────────────────────────────────────────────────

    def sli_rollup(self) -> dict[str, Any]:
        """Organisation-level SLIs, in the library's own vocabulary.

        Using its exact metric names is what keeps `skill-sli-report.py` a valid cross-check: if the
        two disagreed, one of them would be wrong and there would be no way to tell which.
        """
        with self._lock:
            spans = list(self._tail)
        sessions = [s for s in spans if s.kind == "session"]
        nodes = [s for s in spans if s.kind == "node"]
        runs = len(sessions)
        escalated = sum(1 for s in sessions if s.attributes.get("escalated"))
        guardrail_blocks = sum(1 for s in nodes if s.attributes.get("guardrail_block"))
        # Only *node* spans are expected to report usage. A rotation, delegation or health span has no
        # token usage by nature, so counting them as "unreported" would inflate the figure and make the
        # cost warning meaningless. An unmeasured node, by contrast, must never be read as free.
        unreported = sum(1 for s in nodes if not s.usage_reported)
        measured_costs = [s.cost_usd for s in nodes if s.cost_measured and s.cost_usd is not None]
        successes = len(sessions) - escalated
        return {
            "runs": runs,
            "complete": successes,
            "escalated": escalated,
            "escalation_rate": round(escalated / runs, 4) if runs else 0.0,
            "nodes": len(nodes),
            "guardrail_blocks": guardrail_blocks,
            "cost_usd": round(sum(measured_costs), 6) if measured_costs else None,
            "cost_measured_spans": len(measured_costs),
            "cost_unreported_spans": unreported,
            "cost_per_success_usd": (
                round(sum(measured_costs) / successes, 6)
                if measured_costs and successes else None
            ),
            "dropped_by_sampling": self._counters["dropped"],
            "emitted": self._counters["emitted"],
        }

    def summary(self) -> str:
        """A readable rollup, for the CLI and the completion view."""
        rollup = self.sli_rollup()
        lines = [
            f"runs               {rollup['runs']}",
            f"complete           {rollup['complete']}",
            f"escalated          {rollup['escalated']}  "
            f"({rollup['escalation_rate']:.0%} escalation rate)",
            f"nodes              {rollup['nodes']}",
            f"guardrail blocks   {rollup['guardrail_blocks']}",
        ]
        if rollup["cost_usd"] is None:
            lines.append("cost               unknown (no span reported usage)")
        else:
            lines.append(f"cost               ${rollup['cost_usd']:.4f} "
                         f"over {rollup['cost_measured_spans']} measured span(s)")
            if rollup["cost_unreported_spans"]:
                lines.append(f"                   {rollup['cost_unreported_spans']} span(s) "
                             "did NOT report usage — their cost is unknown, not zero")
        if rollup["cost_per_success_usd"] is not None:
            lines.append(f"cost per success   ${rollup['cost_per_success_usd']:.4f}")
        if rollup["dropped_by_sampling"]:
            lines.append(f"sampled out        {rollup['dropped_by_sampling']} span(s)")
        return "\n".join(lines)

    def tail(self, *, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Recent spans as dicts, optionally filtered by kind. For the UI."""
        with self._lock:
            spans = list(self._tail)
        if kind:
            spans = [s for s in spans if s.kind == kind]
        return [s.as_dict() for s in spans[-limit:]]

    def stats(self) -> dict[str, Any]:
        """Exporter state, for the resources view."""
        with self._lock:
            return {
                "path": str(self.path) if self.path else None,
                "enabled": self.path is not None,
                "trace_id": self._trace_id,
                "emitted": self._counters["emitted"],
                "dropped": self._counters["dropped"],
                "tail": len(self._tail),
                "sampling": {
                    "default": self.sampling.default,
                    "on_escalation": self.sampling.on_escalation,
                    "on_guardrail": self.sampling.on_guardrail,
                    "on_health_change": self.sampling.on_health_change,
                },
            }


def load_spans(path: os.PathLike | str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read exported spans back, tolerating a torn final line."""
    target = Path(path)
    if not target.is_file():
        return []
    out: list[dict[str, Any]] = []
    with open(target, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                out.append(data)
    return out[-limit:] if limit else out


def _hash(text: str) -> str:
    """A hex digest, for span and trace ids."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
