#!/usr/bin/env python3
"""tokens.py — estimate prompt size, then correct the estimate against reality.

WHY THIS EXISTS
---------------
The session design makes decisions *before* a call: is there room for this prompt? Is
this session at 70% or at 92%? Should we compact, rotate, or refuse? All of those need a
token count before any provider has reported one.

A heuristic alone drifts: `chars/4` is fine for English prose and badly wrong for code,
JSON and non-Latin scripts. So this module estimates cheaply, then **calibrates** from
the `usage` the provider actually returns. Over a session the error shrinks instead of
accumulating, which is what makes the rotation thresholds trustworthy.

DESIGN
------
- **Per-model calibration, not global.** A coder model and a prose model tokenise
  differently; blending them would make both worse.
- **Calibration is a bounded running ratio.** A single anomalous call (a provider
  reporting a cached-prompt count) must not swing the estimate, so the ratio moves with
  a damped update and is clamped to a sane range.
- **Estimates are labelled as estimates.** The returned number carries its source so a
  caller reporting cost can say `estimated` rather than implying precision it lacks.
- **A conservative bias when unsure.** Over-estimating prompt size triggers a compaction
  slightly early, which is safe. Under-estimating causes a real context overflow, which
  is not.

Usage:
    est = TokenEstimator()
    n = est.estimate(request)                    # before the call
    est.observe("ollama", "qwen2.5-coder:7b", n, response.usage)   # calibrate after
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .providers.base import ChatRequest, Usage

__all__ = ["TokenEstimator", "Estimate", "DEFAULT_CHARS_PER_TOKEN"]

# Baseline ratio for English prose. Code and JSON are denser (more tokens per char), so
# the calibrated value moves *down* as those are observed.
DEFAULT_CHARS_PER_TOKEN = 4.0
# Bounds on the calibrated ratio. Below ~2.0 a "token" is barely more than a character
# (dense code/JSON); above ~5.0 we would be under-counting real prompts dangerously.
_MIN_RATIO = 2.0
_MAX_RATIO = 5.0
# How strongly a new observation moves the ratio. Low enough that one odd call cannot
# destabilise the estimate, high enough to track a genuinely different workload.
_ADAPT_RATE = 0.25


@dataclass(frozen=True)
class Estimate:
    """A token estimate and its provenance."""

    tokens: int
    source: str          # "measured" | "calibrated" | "heuristic"
    ratio: float         # characters per token used

    def as_dict(self) -> dict[str, Any]:
        return {"tokens": self.tokens, "source": self.source, "ratio": round(self.ratio, 3)}

    @property
    def calibrated(self) -> bool:
        """True when this estimate used an observed ratio rather than the baseline."""
        return self.source != "heuristic"


@dataclass
class _ModelCalibration:
    """Running calibration state for one (provider, model) pair."""

    ratio: float = DEFAULT_CHARS_PER_TOKEN
    observations: int = 0
    abs_error_tokens: float = 0.0
    last_estimated: int = 0
    last_actual: int = 0


@dataclass
class TokenEstimator:
    """Estimates prompt tokens and calibrates against reported usage.

    Parameters
    ----------
    default_ratio:
        Characters per token before any observation. Overridable per estimator so a
        caller that knows its workload (say, mostly JSON) can start closer to the truth.
    """

    default_ratio: float = DEFAULT_CHARS_PER_TOKEN
    _models: dict[str, _ModelCalibration] = field(default_factory=dict)

    # ── estimating ──────────────────────────────────────────────────────────

    def ratio_for(self, provider_id: str, model: str) -> float:
        """Current characters-per-token for a model, or the baseline if unobserved."""
        cal = self._models.get(self._key(provider_id, model))
        return cal.ratio if cal is not None else self.default_ratio

    def estimate(self, request: ChatRequest, *, provider_id: str = "") -> Estimate:
        """Estimate a request's prompt tokens.

        Counts the system prompt, every message's text and tool activity, the tool
        schemas, and a small per-message overhead. Tool schemas matter more than they
        look: a full checklist tool definition can be several hundred tokens.
        """
        ratio = self.ratio_for(provider_id, request.model)
        chars = self.count_chars(request)
        overhead = (len(request.messages) + (1 if request.system else 0)) * 4
        tokens = int(chars / max(_MIN_RATIO, ratio)) + overhead
        return Estimate(
            tokens=max(1, tokens),
            source="calibrated" if self._models.get(self._key(provider_id, request.model)) else "heuristic",
            ratio=ratio,
        )

    def estimate_text(self, text: str, *, provider_id: str = "", model: str = "") -> Estimate:
        """Estimate a bare string, for the projection's component parts."""
        ratio = self.ratio_for(provider_id, model)
        return Estimate(tokens=max(1, int(len(text) / max(_MIN_RATIO, ratio))),
                        source="calibrated" if self._models.get(self._key(provider_id, model)) else "heuristic",
                        ratio=ratio)

    def estimate_messages(self, messages: list[Any], *, provider_id: str = "", model: str = "") -> Estimate:
        """Estimate a message list, for the projection's history component."""
        chars = sum(len(m.text or "") for m in messages)
        return Estimate(tokens=max(1, int(chars / max(_MIN_RATIO, self.ratio_for(provider_id, model)))),
                        source="calibrated" if self._models.get(self._key(provider_id, model)) else "heuristic",
                        ratio=self.ratio_for(provider_id, model))

    @staticmethod
    def count_chars(request: ChatRequest) -> int:
        """Total characters that will be sent, including tool schemas.

        Public because calibration needs the *real* character count, not one recovered
        from a previous estimate — deriving characters from `estimate × ratio` would make
        the update circular and pin the ratio at its bound.
        """
        chars = len(request.system or "")
        for message in request.messages:
            chars += len(message.text)
            for call in message.tool_calls:
                chars += len(call.arguments_json) + len(call.name)
        for tool in request.tools:
            chars += len(json.dumps(tool.as_dict(), separators=(",", ":")))
        return chars

    # ── calibrating ─────────────────────────────────────────────────────────

    def observe(self, provider_id: str, model: str, *, chars: int, usage: Usage,
                estimated_tokens: int | None = None) -> None:
        """Update the calibration from a real usage report and the real character count.

        Only a *measured* usage is useful: a provider that reported nothing gives no
        ground truth, and treating its silence as zero would drive the ratio to nonsense.

        The observed ratio is ``chars / actual_tokens`` using the true character count, so
        the update converges on the model's real tokenisation instead of chasing itself.
        """
        if not usage.measured or not usage.prompt_tokens:
            return
        actual = int(usage.prompt_tokens)
        chars = int(chars)
        if chars <= 0:
            return

        key = self._key(provider_id, model)
        cal = self._models.get(key)
        if cal is None:
            cal = _ModelCalibration(ratio=self.default_ratio)
            self._models[key] = cal

        observed_ratio = max(_MIN_RATIO, min(_MAX_RATIO, chars / actual))
        if cal.observations == 0:
            cal.ratio = observed_ratio
        else:
            cal.ratio = (1 - _ADAPT_RATE) * cal.ratio + _ADAPT_RATE * observed_ratio
        cal.ratio = max(_MIN_RATIO, min(_MAX_RATIO, cal.ratio))
        cal.observations += 1
        if estimated_tokens is not None:
            cal.abs_error_tokens = abs(int(estimated_tokens) - actual)
            cal.last_estimated = int(estimated_tokens)
        cal.last_actual = actual

    # ── reporting ───────────────────────────────────────────────────────────

    def calibration_report(self) -> dict[str, Any]:
        """Per-model calibration state, for diagnostics and the resources view."""
        report: dict[str, Any] = {}
        for key, cal in sorted(self._models.items()):
            error_pct = (
                round(100.0 * cal.abs_error_tokens / cal.last_actual, 1)
                if cal.last_actual else None
            )
            report[key] = {
                "ratio": round(cal.ratio, 3),
                "observations": cal.observations,
                "last_error_tokens": round(cal.abs_error_tokens, 1),
                "last_error_pct": error_pct,
            }
        return report

    def reset(self, provider_id: str | None = None, model: str | None = None) -> None:
        """Clear calibration, all of it or one model's."""
        if provider_id is None:
            self._models.clear()
            return
        if model is None:
            for key in [k for k in self._models if k.startswith(f"{provider_id}:")]:
                self._models.pop(key, None)
            return
        self._models.pop(self._key(provider_id, model), None)

    @staticmethod
    def _key(provider_id: str, model: str) -> str:
        """Per-model calibration key.

        Includes the provider because the same model name can be served by a local
        runtime and a gateway with different tokenisers.
        """
        return f"{provider_id or 'default'}:{model}"
