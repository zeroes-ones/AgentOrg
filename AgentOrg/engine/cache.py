#!/usr/bin/env python3
"""cache.py — prefix-shape hashing, so a cache miss can be *explained* rather than guessed at.

WHY THIS EXISTS
---------------
Providers bill cached input at a fraction of the miss rate, and they can only reuse a prefix when the
**exact bytes** match what they saw before. A loop that reorders its prompt, rewrites a section, or
interpolates a fresh timestamp each turn silently loses that discount — and, crucially, loses it
*invisibly*: the request still succeeds, the answer is still correct, and the only symptom is a
larger bill.

This module makes the prefix visible. It hashes the parts of a request that decide cache reuse,
compares consecutive turns, and reports **which part changed**. That turns "the cache is not helping"
into "the tools hash changed, because the schema order is not stable", which is a fixable statement.

DESIGN
------
- **Hash what the provider sees, not what we hold.** Only the system prompt and the tool schemas are
  hashed here, because only those are stable enough to be worth comparing across turns. A hash over
  the whole message list would change every turn by construction and explain nothing.
- **Tool schemas are normalised before hashing.** Two identical schema sets serialised in a different
  key order are *different bytes* to the provider and therefore a guaranteed miss. Sorting makes the
  hash order-independent and, because the normalised form is what gets sent, removes the miss too.
- **A miss is never inferred.** The reasons come from comparing our own snapshots; the hit/miss token
  counts come from the provider. Neither is derived from the other, so a provider that reports
  nothing yields `None` rather than a confident 0%.
- **Reasons are a closed vocabulary.** `system` / `tools` / `session_context` / a content-rewrite
  reason. A free-text reason cannot be aggregated, and the point of this is to answer "what keeps
  changing?" across a whole run.

Usage:
    shape = capture_shape(system=prompt.system, schemas=[])
    diag = compare_shapes(prev, shape, usage=response.usage)
    if diag.prefix_changed:
        print("cache miss because:", ", ".join(diag.prefix_change_reasons))
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "PrefixShape", "CacheDiagnostics", "ToolSchemaCost",
    "capture_shape", "compare_shapes", "normalize_schemas",
    "estimate_tokens", "schema_costs", "ShapeTracker",
]


def _short_hash(value: Any) -> str:
    """A stable, short hash over any JSON-serialisable value.

    `sort_keys` is load-bearing: an un-sorted dump would make the hash depend on dict insertion
    order, which is exactly the kind of hidden instability this module exists to detect.
    """
    blob = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def estimate_tokens(text: str, *, chars_per_token: float = 4.0) -> int:
    """A rough token count from byte length.

    Deliberately crude and zero-allocation: this is for *diagnostics* — "the tool block costs about
    400 tokens" — not for accounting, which uses the provider's own counts.
    """
    if not text:
        return 0
    return int(len(text) / max(1.0, chars_per_token))


@dataclass(frozen=True)
class ToolSchemaCost:
    """One tool's contribution to the prefix, for a diagnostic display."""

    name: str
    tokens: int

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "tokens": self.tokens}


def normalize_schemas(schemas: Iterable[Any]) -> list[dict[str, Any]]:
    """Canonicalise tool schemas so an equal set hashes equally.

    Sorted by name, then description, then parameters. Without this, two calls with the same tools
    in a different registration order serialise to different bytes, miss the cache, and look
    inexplicable — the schemas are "the same" to a human and different to the provider.
    """
    out: list[dict[str, Any]] = []
    for schema in schemas or []:
        if isinstance(schema, dict):
            out.append({
                "name": str(schema.get("name") or ""),
                "description": str(schema.get("description") or ""),
                "parameters": schema.get("parameters") or {},
            })
        else:
            params = getattr(schema, "parameters", {}) or {}
            out.append({
                "name": str(getattr(schema, "name", "") or ""),
                "description": str(getattr(schema, "description", "") or ""),
                "parameters": params,
            })
    out.sort(key=lambda s: (s["name"], s["description"], json.dumps(s["parameters"], sort_keys=True)))
    return out


def schema_costs(schemas: Iterable[Any]) -> list[ToolSchemaCost]:
    """Per-tool token estimates, so a caller can see which tool is inflating the prefix."""
    costs: list[ToolSchemaCost] = []
    for schema in normalize_schemas(schemas):
        costs.append(ToolSchemaCost(name=schema["name"],
                                    tokens=estimate_tokens(json.dumps(schema, sort_keys=True))))
    return costs


@dataclass
class PrefixShape:
    """A snapshot of the parts of a request that decide provider-side cache reuse."""

    system_hash: str = ""
    tools_hash: str = ""
    prefix_hash: str = ""
    #: Bumped whenever the *content* of the log is rewritten (a compaction, a rewind). Lets a caller
    #: distinguish "the history changed" from "the prefix changed" without diffing the history.
    log_rewrite_version: int = 0
    tool_schema_tokens: int = 0
    #: A digest of any session-context block that is prepended verbatim (memory, recall). Kept
    #: separate from the system hash because it is *ours*, not the provider's, and changes for
    #: different reasons.
    session_context_digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "system_hash": self.system_hash,
            "tools_hash": self.tools_hash,
            "prefix_hash": self.prefix_hash,
            "log_rewrite_version": self.log_rewrite_version,
            "tool_schema_tokens": self.tool_schema_tokens,
            "session_context_digest": self.session_context_digest,
        }


def capture_shape(*, system: str = "", schemas: Iterable[Any] = (),
                  session_context: str = "", log_rewrite_version: int = 0) -> PrefixShape:
    """Snapshot the current prefix state."""
    normalized = normalize_schemas(schemas)
    tools_json = json.dumps(normalized, sort_keys=True, default=str)
    return PrefixShape(
        system_hash=_short_hash(system or ""),
        tools_hash=_short_hash(tools_json),
        prefix_hash=_short_hash({"system": system or "", "tools": tools_json}),
        log_rewrite_version=int(log_rewrite_version),
        tool_schema_tokens=estimate_tokens(tools_json),
        session_context_digest=_short_hash(session_context or ""),
    )


@dataclass
class CacheDiagnostics:
    """What changed between two shapes, and what the provider said about reuse."""

    prefix_hash: str = ""
    prefix_changed: bool = False
    prefix_change_reasons: list[str] = field(default_factory=list)
    system_hash: str = ""
    tools_hash: str = ""
    log_rewrite_version: int = 0
    tool_schema_tokens: int = 0
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None

    @property
    def cache_hit_rate(self) -> float | None:
        """Hit tokens over eligible input, or None when the provider reported nothing."""
        if self.cache_hit_tokens is None and self.cache_miss_tokens is None:
            return None
        hit = self.cache_hit_tokens or 0
        miss = self.cache_miss_tokens or 0
        total = hit + miss
        return None if total <= 0 else hit / total

    def as_dict(self) -> dict[str, Any]:
        rate = self.cache_hit_rate
        return {
            "prefix_hash": self.prefix_hash,
            "prefix_changed": self.prefix_changed,
            "prefix_change_reasons": list(self.prefix_change_reasons),
            "system_hash": self.system_hash,
            "tools_hash": self.tools_hash,
            "log_rewrite_version": self.log_rewrite_version,
            "tool_schema_tokens": self.tool_schema_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "cache_hit_rate": round(rate, 4) if rate is not None else None,
        }


def compare_shapes(previous: PrefixShape | None, current: PrefixShape, *,
                   usage: Any = None,
                   content_rewrite_reasons: Iterable[str] = (),
                   ) -> CacheDiagnostics:
    """Describe what changed between two snapshots, using the provider's own counts for the rate.

    `content_rewrite_reasons` are the provider-visible rewrites the caller knows about (a compaction,
    a rewind). They are folded in as reasons because a rewrite *is* a prefix change — but they are
    supplied by the caller rather than guessed here, since only the caller knows whether a rewrite
    reached the provider or touched local-only metadata.
    """
    reasons: list[str] = []
    if previous is not None:
        if previous.system_hash and previous.system_hash != current.system_hash:
            reasons.append("system")
        if previous.tools_hash and previous.tools_hash != current.tools_hash:
            reasons.append("tools")
        if previous.session_context_digest != current.session_context_digest:
            reasons.append("session_context")
        if previous.log_rewrite_version != current.log_rewrite_version:
            # A rewrite-version bump with no named reason is a *local-only* change — a receipt, a
            # preview, an edited message that never reaches the provider. Reporting it as a cache
            # change would send someone hunting a miss that did not happen.
            reasons.extend(r for r in content_rewrite_reasons if r)
    reasons.extend(r for r in content_rewrite_reasons if r and r not in reasons)

    hit = getattr(usage, "cache_hit_tokens", None) if usage is not None else None
    miss = getattr(usage, "cache_miss_tokens", None) if usage is not None else None

    return CacheDiagnostics(
        prefix_hash=current.prefix_hash,
        prefix_changed=bool(reasons),
        prefix_change_reasons=reasons,
        system_hash=current.system_hash,
        tools_hash=current.tools_hash,
        log_rewrite_version=current.log_rewrite_version,
        tool_schema_tokens=current.tool_schema_tokens,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
    )


class ShapeTracker:
    """Remembers the last shape and reports each turn's diagnosis.

    Kept as an object rather than a function because the comparison is *stateful by definition*: a
    shape only means something next to the one before it, and threading that through every call site
    is how the comparison gets forgotten.
    """

    def __init__(self) -> None:
        self.previous: PrefixShape | None = None
        self.turns: int = 0
        self._hits: list[int] = []
        self._misses: list[int] = []

    def observe(self, *, system: str = "", schemas: Iterable[Any] = (),
                session_context: str = "", log_rewrite_version: int = 0,
                usage: Any = None, content_rewrite_reasons: Iterable[str] = (),
                ) -> CacheDiagnostics:
        current = capture_shape(system=system, schemas=schemas,
                                session_context=session_context,
                                log_rewrite_version=log_rewrite_version)
        diagnostics = compare_shapes(self.previous, current, usage=usage,
                                     content_rewrite_reasons=content_rewrite_reasons)
        self.previous = current
        self.turns += 1
        if diagnostics.cache_hit_tokens is not None:
            self._hits.append(diagnostics.cache_hit_tokens)
        if diagnostics.cache_miss_tokens is not None:
            self._misses.append(diagnostics.cache_miss_tokens)
        return diagnostics

    def summary(self) -> dict[str, Any]:
        """Aggregate the run. An unreported cache yields None rather than a confident zero."""
        hits = sum(self._hits)
        misses = sum(self._misses)
        reported = bool(self._hits or self._misses)
        total = hits + misses
        return {
            "turns": self.turns,
            "cache_reported": reported,
            "cache_hit_tokens": hits if reported else None,
            "cache_miss_tokens": misses if reported else None,
            "cache_hit_rate": (round(hits / total, 4) if reported and total > 0 else None),
        }
