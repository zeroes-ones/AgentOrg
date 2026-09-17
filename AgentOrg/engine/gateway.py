#!/usr/bin/env python3
"""gateway.py — the model-agnostic entry point: routing, budget, cost, one call surface.

WHY THIS EXISTS
---------------
This is the named deliverable of the original brief and the only way the rest of the
engine talks to a model. It exists to concentrate four cross-cutting concerns that would
otherwise be re-implemented (and re-implemented inconsistently) at every call site:

1. **Routing.** Resolve `provider_id` + model, expanding aliases, and fail with a clear
   message when the provider is not configured or was skipped for a missing key.
2. **Budget.** A run's cost ceiling is a hard stop, not advice. Before spending, the
   gateway checks the remaining budget; on breach it raises rather than overspending.
3. **Cost correctness.** Every figure is labelled `measured`, `estimated` or `unknown`,
   and estimates are reconciled against provider-reported usage. A dashboard that reads
   "unmeasured" as "free" lies, so the distinction is carried in the type itself.
4. **Accounting.** Tokens and cost are attributed per agent, per model and per run, and
   recorded so the SLI rollup and the health signals can use them.

DESIGN
------
- **One `complete()` and one `stream()`.** Callers never see adapters.
- **Budget is checked before the call and charged after it.** Checking first prevents the
  spend; charging after is the only time the real number exists.
- **An unknown cost is never zero.** `Cost.known` is False when the provider did not
  report usage and no price is configured, and the total propagates that partial
  knowledge rather than manufacturing certainty.
- **A context-length failure is surfaced as a signal, not just an error**, because the
  correct response is to compact or rotate, not to retry.
- **Events are emitted for request and response**, so the UI terminal and the cost view
  are driven by the same stream as everything else.

Usage:
    gateway = Gateway(config, providers, catalog, bus=bus, estimator=TokenEstimator())
    response = gateway.complete(request, provider_id="ollama", agent_id="ag_1", node_id="fixer")
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from .catalog import ModelCatalog
from .config import Config, ConfigError
from .providers.base import (
    ChatRequest,
    ChatResponse,
    Chunk,
    ErrorKind,
    GatewayError,
    Provider,
    Usage,
)
from .protocol import EventType
from .tokens import TokenEstimator

__all__ = ["Cost", "BudgetExceeded", "Gateway", "CostLedger"]

# Per-1k-token prices for models the curated table can price. Local models are genuinely
# free and are handled by locality, not listed here.
#
# Shape: (prompt, completion, cached_prompt). The third is what a *cache hit* is billed at, and it
# is the whole point of cache engineering — DeepSeek bills cached input at roughly a tenth of the
# miss rate, and the others discount similarly. `None` means the provider publishes no separate
# cached rate, in which case a hit is billed as a normal prompt token rather than silently at zero:
# under-billing a provider we cannot price would make the saving look larger than it is.
_PRICE_TABLE: dict[str, tuple[float, float, float | None]] = {
    "gpt-4o-2024-11-20": (0.0025, 0.010, 0.00125),
    "gpt-4o-mini": (0.00015, 0.0006, 0.000075),
    "claude-sonnet-4-20250514": (0.003, 0.015, 0.0003),
    "claude-haiku-4-20250514": (0.0008, 0.004, 0.00008),
    "deepseek-chat": (0.00014, 0.00028, 0.000014),
}


class BudgetExceeded(RuntimeError):
    """Raised when a call would breach the run's cost ceiling.

    Deliberately a distinct exception rather than a `GatewayError`: a budget stop is a
    policy decision by the Owner, not a provider failure, and the two need different
    handling in the orchestrator (park the run versus back off and retry).
    """

    def __init__(self, spent_usd: float, limit_usd: float, *, scope: str = "run") -> None:
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        self.scope = scope
        super().__init__(
            f"{scope} budget exhausted: ${spent_usd:.4f} spent of ${limit_usd:.4f}. "
            "Park the run and raise the ceiling, or reduce scope."
        )

    def to_dict(self) -> dict[str, Any]:
        return {"spent_usd": round(self.spent_usd, 6), "limit_usd": self.limit_usd,
                "scope": self.scope, "reason": str(self)}


@dataclass
class Cost:
    """The cost of one call, with explicit knowledge state.

    ``source`` is the honesty mechanism: `measured` when the provider reported usage and
    we have a price, `estimated` when usage was reported but we hold an estimate, and
    `unknown` when the provider reported nothing. `unknown` must never be rendered as
    `$0.00` — that is the cost illusion the design forbids.
    """

    usd: float = 0.0
    source: str = "unknown"      # "measured" | "estimated" | "unknown" | "free"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str = ""
    provider_id: str = ""
    # ── caching ──
    # Tokens served from the prefix cache, and what the same call would have cost had they missed.
    # Both are needed: the hit count explains the bill, and the counterfactual is the only way to
    # state the *saving* rather than assert it.
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    cache_write_tokens: int | None = None
    #: What this call would have cost with no cache at all. None when there is nothing to compare.
    uncached_usd: float | None = None

    @property
    def known(self) -> bool:
        """True when the cost is a real number (including a genuine zero for local)."""
        return self.source in ("measured", "estimated", "free")

    @property
    def cache_saving_usd(self) -> float | None:
        """What caching saved on this call, or None when it cannot be known.

        None rather than 0.0 when nothing was reported: a saving of "unknown" and a saving of
        "nothing" are different facts, and rendering the first as the second would make an
        uncached run look like a cache that was simply not helping.
        """
        if self.uncached_usd is None or not self.known:
            return None
        return max(0.0, self.uncached_usd - self.usd)

    def as_dict(self) -> dict[str, Any]:
        return {
            "usd": round(self.usd, 6),
            "source": self.source,
            "known": self.known,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "model": self.model,
            "provider_id": self.provider_id,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "uncached_usd": (round(self.uncached_usd, 6)
                             if self.uncached_usd is not None else None),
            "cache_saving_usd": (round(self.cache_saving_usd, 6)
                                 if self.cache_saving_usd is not None else None),
        }


@dataclass
class CostLedger:
    """Running totals for a run, per agent and per model.

    The ledger is the source of truth for the budget check and for the economics view, and
    it counts *unmeasured* calls separately so "we spent nothing" and "we have no idea"
    are never confused.
    """

    by_agent_usd: dict[str, float] = field(default_factory=dict)
    by_model_usd: dict[str, float] = field(default_factory=dict)
    by_agent_tokens: dict[str, int] = field(default_factory=dict)
    total_usd: float = 0.0
    total_tokens: int = 0
    calls: int = 0
    unmeasured_calls: int = 0
    unknown_cost_calls: int = 0
    #: Cache accounting, accumulated across the run. `None` means no provider reported one, which is
    #: deliberately distinct from zero.
    cache_saving_usd: float | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None

    def charge(self, cost: Cost, *, agent_id: str | None = None) -> None:
        """Record a call's cost and tokens."""
        self.calls += 1
        tokens = (cost.prompt_tokens or 0) + (cost.completion_tokens or 0)
        if not cost.known:
            self.unknown_cost_calls += 1
        if cost.prompt_tokens is None or cost.completion_tokens is None:
            self.unmeasured_calls += 1
        self.total_usd += cost.usd if cost.known else 0.0
        self.total_tokens += tokens
        # Accumulate the cache picture. The *saving* is only added when it could actually be computed,
        # so a run with an unreported cache reports `None` saved rather than a confident $0.00 — the
        # same rule that keeps an unreported cost from rendering as free.
        saving = cost.cache_saving_usd
        if saving is not None:
            self.cache_saving_usd = (self.cache_saving_usd or 0.0) + saving
        if cost.cache_hit_tokens is not None:
            self.cache_hit_tokens = (self.cache_hit_tokens or 0) + cost.cache_hit_tokens
        if cost.cache_miss_tokens is not None:
            self.cache_miss_tokens = (self.cache_miss_tokens or 0) + cost.cache_miss_tokens
        if agent_id:
            self.by_agent_usd[agent_id] = self.by_agent_usd.get(agent_id, 0.0) + (cost.usd if cost.known else 0.0)
            self.by_agent_tokens[agent_id] = self.by_agent_tokens.get(agent_id, 0) + tokens
        if cost.model:
            self.by_model_usd[cost.model] = self.by_model_usd.get(cost.model, 0.0) + (cost.usd if cost.known else 0.0)

    def snapshot(self) -> dict[str, Any]:
        """Serialisable state for the `cost.update`/`budget.burn` events.

        `cost_complete` is False whenever any call was unmeasured — the SLI vocabulary
        calls these `cost_unreported_runs`, and they must be visible, not averaged away.
        """
        return {
            "total_usd": round(self.total_usd, 6),
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "unmeasured_calls": self.unmeasured_calls,
            "unknown_cost_calls": self.unknown_cost_calls,
            "cost_complete": self.unknown_cost_calls == 0,
            "by_agent_usd": {k: round(v, 6) for k, v in sorted(self.by_agent_usd.items())},
            "by_model_usd": {k: round(v, 6) for k, v in sorted(self.by_model_usd.items())},
            "by_agent_tokens": dict(sorted(self.by_agent_tokens.items())),
            "cache_saving_usd": (round(self.cache_saving_usd, 6)
                                 if self.cache_saving_usd is not None else None),
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
        }


class Gateway:
    """Routes completions to providers and accounts for their cost.

    Parameters
    ----------
    config:
        Validated configuration; supplies budget ceilings and alias expansion.
    providers:
        Live adapters keyed by provider id.
    catalog:
        Model metadata, used for locality and for pricing.
    bus:
        Optional event bus. When present, `llm.request`/`llm.response` and budget events
        are emitted for the UI and the trace.
    estimator:
        Token estimator, calibrated from the usage these calls return.
    """

    def __init__(self, config: Config, providers: dict[str, Provider],
                 catalog: ModelCatalog | None = None, *, bus: Any = None,
                 estimator: TokenEstimator | None = None,
                 ledger: CostLedger | None = None,
                 run_id: str | None = None) -> None:
        self.config = config
        self.providers = dict(providers)
        self.catalog = catalog
        self.bus = bus
        self.estimator = estimator or TokenEstimator()
        self.ledger = ledger or CostLedger()
        self.run_id = run_id
        self.run_max_usd = float(config.budget.run_max_usd)
        self.run_max_tokens = int(config.budget.run_max_tokens)
        self._skipped: list[str] = []

    def note_skipped(self, skipped: list[str]) -> None:
        """Record providers the registry could not build, so callers can surface them."""
        self._skipped = list(skipped)

    # ── resolution ──────────────────────────────────────────────────────────

    def provider_for(self, provider_id: str | None) -> Provider:
        """Resolve a provider, defaulting to the configured default.

        Raises
        ------
        GatewayError
            With kind `NOT_FOUND` when the provider is absent, naming what is available
            and — importantly — whether the provider was skipped for a missing key, which
            is the actual fix in the common case.
        """
        pid = provider_id or self.config.defaults.get("provider")
        if not pid:
            raise GatewayError(
                ErrorKind.NOT_FOUND,
                "no provider specified and no default configured. "
                "Set defaults.provider in credentials.json.",
            )
        provider = self.providers.get(pid)
        if provider is None:
            if any(entry.startswith(f"{pid}:") for entry in self._skipped):
                detail = next(e for e in self._skipped if e.startswith(f"{pid}:"))
                raise GatewayError(
                    ErrorKind.AUTH,
                    f"provider {pid!r} is configured but could not be constructed: {detail}",
                    provider_id=pid,
                )
            raise GatewayError(
                ErrorKind.NOT_FOUND,
                f"unknown provider {pid!r}; available: "
                + (", ".join(sorted(self.providers)) or "(none)"),
                provider_id=pid,
            )
        return provider

    def resolve_model(self, provider_id: str | None, model: str | None) -> tuple[str, str]:
        """Return the concrete `(provider_id, model_id)` after alias expansion."""
        pid = provider_id or self.config.defaults.get("provider") or ""
        resolved = model or self.config.defaults.get("model")
        if not resolved:
            raise GatewayError(
                ErrorKind.NOT_FOUND,
                "no model specified and no default configured. "
                "Set defaults.model in credentials.json.",
            )
        return pid, self.config.alias(pid, resolved)

    # ── budget ──────────────────────────────────────────────────────────────

    def check_budget(self) -> None:
        """Raise :class:`BudgetExceeded` when the run ceiling is already reached.

        Called before every spend. Failing here is what makes the ceiling real: a
        post-hoc check would report the overspend after it happened.
        """
        if self.ledger.total_usd >= self.run_max_usd:
            self._emit(EventType.COST_CEILING, {
                "scope": "run", "spent_usd": round(self.ledger.total_usd, 6),
                "limit_usd": self.run_max_usd, "calls": self.ledger.calls,
            })
            raise BudgetExceeded(self.ledger.total_usd, self.run_max_usd)
        if self.ledger.total_tokens >= self.run_max_tokens:
            self._emit(EventType.COST_CEILING, {
                "scope": "run_tokens", "spent_tokens": self.ledger.total_tokens,
                "limit_tokens": self.run_max_tokens,
            })
            raise BudgetExceeded(float(self.ledger.total_tokens), float(self.run_max_tokens),
                                 scope="run_tokens")

    # ── cost ────────────────────────────────────────────────────────────────

    def compute_cost(self, provider_id: str, model: str, usage: Usage) -> Cost:
        """Turn a usage report into a cost with an explicit knowledge state.

        Order of truth: a provider-reported cost wins; else a price table entry applied to
        measured tokens; else a local model's genuine zero; else `unknown`.
        """
        cost = Cost(
            model=model, provider_id=provider_id,
            prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
        )
        if usage.reported_cost_usd is not None:
            cost.usd = float(usage.reported_cost_usd)
            cost.source = "measured"
            return cost

        locality = self._locality(provider_id, model)
        # Locality is checked before the price table: a local model genuinely costs no
        # API money, and that is a *known* zero. Consulting the price table first would
        # make a local model look unpriced and therefore unknown, which would understate
        # nothing but would wrongly flag every local run as a cost-reporting gap.
        if locality == "local":
            cost.usd = 0.0
            cost.source = "free"
            return cost

        prices = _PRICE_TABLE.get(model)
        if prices is not None:
            if not usage.measured:
                # A priced model that reported nothing: we genuinely do not know.
                cost.source = "unknown"
                return cost
            prompt_price, completion_price, cached_price = prices
            prompt_tokens = usage.prompt_tokens or 0
            completion_tokens = usage.completion_tokens or 0
            # Bill the cached share at the cached rate and the rest at the full rate. When the
            # provider reports no cache, every prompt token is a miss — which is the same arithmetic
            # as before caching existed, so an unreported run is not penalised.
            hit = usage.cache_hit_tokens or 0
            miss = usage.cache_miss_tokens
            if miss is None:
                miss = max(0, prompt_tokens - hit)
            hit_rate = cached_price if cached_price is not None else prompt_price
            cost.usd = (hit / 1000.0) * hit_rate + (miss / 1000.0) * prompt_price + \
                       (completion_tokens / 1000.0) * completion_price
            # The counterfactual, so the saving can be stated rather than asserted.
            cost.uncached_usd = (prompt_tokens / 1000.0) * prompt_price + \
                                (completion_tokens / 1000.0) * completion_price
            cost.cache_hit_tokens = usage.cache_hit_tokens
            cost.cache_miss_tokens = usage.cache_miss_tokens
            cost.cache_write_tokens = usage.cache_write_tokens
            cost.source = "estimated"
            return cost

        cost.source = "unknown"
        return cost

    def _locality(self, provider_id: str, model: str) -> str:
        """Local or cloud.

        Resolution order: the live catalog, then the adapter (which knows its own
        endpoint), then the curated config table. The config fallback matters because a
        gateway can be constructed without a catalog — the cost layer must still be able
        to tell a genuinely-free local model from an unpriced cloud one.
        """
        if self.catalog is not None:
            entry = self.catalog.resolve(provider_id, model)
            if entry is not None:
                return entry.locality
        provider = self.providers.get(provider_id)
        if provider is not None:
            return provider.capabilities().locality
        spec = self.config.known_models.get(model)
        if spec is not None:
            return spec.locality
        provider_spec = self.config.providers.get(provider_id)
        if provider_spec is not None:
            return "local" if not provider_spec.base_url.lower().startswith("https://") else "cloud"
        return "cloud"

    def reconcile(self, request: ChatRequest, estimated_tokens: int, usage: Usage, *,
                  provider_id: str, model: str, agent_id: str | None = None) -> None:
        """Compare our estimate against the provider's report, and calibrate.

        Uses the request's *real* character count so the calibration is grounded in fact.
        Also emits `cost.reconciled` when the estimate was materially wrong, so drift in
        the estimator is visible rather than silently absorbed into rotation thresholds.
        """
        if not usage.measured or not usage.prompt_tokens:
            return
        chars = self.estimator.count_chars(request)
        self.estimator.observe(provider_id, model, chars=chars, usage=usage,
                               estimated_tokens=estimated_tokens)
        actual = int(usage.prompt_tokens or 0)
        error_pct = abs(estimated_tokens - actual) / max(1, actual) * 100.0
        if error_pct > 25.0:
            self._emit(EventType.COST_RECONCILED, {
                "provider_id": provider_id, "model": model, "agent_id": agent_id,
                "estimated_tokens": estimated_tokens, "actual_tokens": actual,
                "error_pct": round(error_pct, 1),
                "calibrated_ratio": round(self.estimator.ratio_for(provider_id, model), 3),
            })

    # ── calls ───────────────────────────────────────────────────────────────

    def complete(self, request: ChatRequest, *, provider_id: str | None = None,
                 agent_id: str | None = None, node_id: str | None = None,
                 session_id: str | None = None) -> ChatResponse:
        """Perform a completion, enforcing budget and recording cost.

        Raises
        ------
        BudgetExceeded
            Before spending, when the ceiling is reached.
        GatewayError
            On any provider failure, classified for the retry policy.
        """
        self.check_budget()
        pid, model = self.resolve_model(provider_id, request.model)
        provider = self.provider_for(pid)
        effective = _for_model(request, model)

        estimate = self.estimator.estimate(effective, provider_id=pid)
        self._emit(EventType.LLM_REQUEST, {
            "provider_id": pid, "model": model, "agent_id": agent_id,
            "estimated_prompt_tokens": estimate.tokens, "estimate_source": estimate.source,
            "stream": False, "message_count": len(request.messages),
            "tool_count": len(request.tools),
        }, agent_id=agent_id, node_id=node_id, session_id=session_id)

        started = time.time()
        try:
            response = provider.complete(effective)
        except GatewayError as exc:
            self._emit_error(exc, pid, model, agent_id, node_id)
            raise
        response.latency_ms = (time.time() - started) * 1000.0
        response.provider_id = pid
        if not response.model:
            response.model = model

        cost = self.compute_cost(pid, model, response.usage)
        self.ledger.charge(cost, agent_id=agent_id)
        self.reconcile(effective, estimate.tokens, response.usage, provider_id=pid,
                       model=model, agent_id=agent_id)

        # Observe the prefix shape, so a cache miss can be attributed rather than guessed at. This
        # is per-gateway state: two runs do not share a prefix, and comparing across them would
        # report a change that never happened.
        diagnostics = self._observe_cache_shape(effective, response.usage)

        payload = response.as_dict()
        payload.update({"agent_id": agent_id, "estimated_prompt_tokens": estimate.tokens,
                        "cost": cost.as_dict(), "budget": self.ledger.snapshot(),
                        "cache": diagnostics.as_dict()})
        self._emit(EventType.LLM_RESPONSE, payload, agent_id=agent_id, node_id=node_id,
                   session_id=session_id)
        return response

    def _observe_cache_shape(self, request: ChatRequest, usage: Any) -> Any:
        """Track the prefix shape for this call and report what changed.

        The system prompt is hashed from the request rather than from a captured reference, because
        the request is what the provider sees — and the provider's cache is byte-based, so anything
        else would be describing a prompt nobody sent.
        """
        from .cache import ShapeTracker

        tracker = getattr(self, "_shape_tracker", None)
        if tracker is None:
            tracker = ShapeTracker()
            self._shape_tracker = tracker
        system = ""
        for message in request.messages:
            if str(getattr(message, "role", "")) == "system" or getattr(message.role, "value", "") == "system":
                system = message.text
                break
        if not system and getattr(request, "system", None):
            system = request.system or ""
        return tracker.observe(system=system, schemas=list(getattr(request, "tools", []) or []),
                               usage=usage)

    def cache_summary(self) -> dict[str, Any]:
        """This gateway's aggregate cache picture, or an all-None summary when nothing reported."""
        tracker = getattr(self, "_shape_tracker", None)
        if tracker is None:
            return {"turns": 0, "cache_reported": False, "cache_hit_tokens": None,
                    "cache_miss_tokens": None, "cache_hit_rate": None}
        return tracker.summary()

    def stream(self, request: ChatRequest, *, provider_id: str | None = None,
               agent_id: str | None = None, node_id: str | None = None,
               session_id: str | None = None) -> Iterator[Chunk]:
        """Stream a completion, returning text deltas and accounting on the terminal chunk.

        Cost is charged when the terminal chunk arrives, because that is when usage
        exists. A stream that dies before the terminal chunk charges what was reported (if
        anything) and leaves the ledger honestly short rather than guessing.
        """
        self.check_budget()
        pid, model = self.resolve_model(provider_id, request.model)
        provider = self.provider_for(pid)
        effective = _for_model(request, model)
        estimate = self.estimator.estimate(effective, provider_id=pid)

        self._emit(EventType.LLM_REQUEST, {
            "provider_id": pid, "model": model, "agent_id": agent_id,
            "estimated_prompt_tokens": estimate.tokens, "stream": True,
        }, agent_id=agent_id, node_id=node_id, session_id=session_id)

        started = time.time()
        for chunk in provider.stream(effective):
            if chunk.usage is not None and chunk.finish_reason is not None:
                cost = self.compute_cost(pid, model, chunk.usage)
                self.ledger.charge(cost, agent_id=agent_id)
                self.reconcile(effective, estimate.tokens, chunk.usage, provider_id=pid,
                               model=model, agent_id=agent_id)
                self._emit(EventType.LLM_RESPONSE, {
                    "provider_id": pid, "model": model, "agent_id": agent_id,
                    "stream": True, "finish_reason": chunk.finish_reason.value,
                    "usage": chunk.usage.as_dict(), "cost": cost.as_dict(),
                    "latency_ms": (time.time() - started) * 1000.0,
                    "budget": self.ledger.snapshot(),
                }, agent_id=agent_id, node_id=node_id, session_id=session_id)
            yield chunk

    # ── introspection ───────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        """Per-provider health plus the budget picture. Never raises."""
        return {
            "providers": {
                pid: provider.health() for pid, provider in sorted(self.providers.items())
            },
            "skipped_providers": list(self._skipped),
            "budget": {
                "limit_usd": self.run_max_usd,
                "limit_tokens": self.run_max_tokens,
                "ledger": self.ledger.snapshot(),
            },
            "calibration": self.estimator.calibration_report(),
        }

    def cost_snapshot(self) -> dict[str, Any]:
        """The ledger plus the calibration state — the economics view's data source."""
        return {"ledger": self.ledger.snapshot(), "calibration": self.estimator.calibration_report()}

    # ── events ──────────────────────────────────────────────────────────────

    def _emit(self, event_type: Any, payload: dict[str, Any], **correlation: Any) -> None:
        if self.bus is None:
            return
        self.bus.emit(event_type, payload=payload, **correlation)

    def _emit_error(self, exc: GatewayError, provider_id: str, model: str,
                    agent_id: str | None, node_id: str | None) -> None:
        """Emit a classified error, marking whether a retry is worthwhile.

        The `retryable` flag is what lets the orchestrator distinguish "back off" from
        "compact and rotate" without re-deriving the taxonomy.
        """
        if self.bus is None:
            return
        payload = exc.to_dict()
        payload.update({"provider_id": provider_id, "model": model, "agent_id": agent_id,
                        "needs_compaction": exc.kind is ErrorKind.CONTEXT_LENGTH})
        self.bus.emit(EventType.ERROR, payload=payload, agent_id=agent_id, node_id=node_id)


def _for_model(request: ChatRequest, model: str) -> ChatRequest:
    """Return a copy of the request bound to the resolved model id.

    Copying rather than mutating: the orchestrator reuses a request object across a retry
    and a rework loop, and a mutated model would silently send the alias instead of the
    resolved id.
    """
    return ChatRequest(
        model=model,
        messages=request.messages,
        system=request.system,
        tools=request.tools,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        stream=request.stream,
        json_mode=request.json_mode,
        stop=request.stop,
        extra=request.extra,
    )
