#!/usr/bin/env python3
"""config.py — load, validate and redact AgentOrg configuration.

WHY THIS EXISTS
---------------
One file decides what the whole organisation is allowed to do: which providers exist,
how many agents may run at once, when context compacts, how deep delegation may go,
and what a run may cost. Getting that file wrong is not a cosmetic problem — a missing
`context_window` causes real overflows, and a leaked key is unrecoverable. So this
module validates aggressively and treats secrets as radioactive.

DESIGN
------
- **Secrets resolve from the environment first.** ``api_key_env`` names a variable;
  the plain ``api_key`` field exists for local convenience but is discouraged and is
  never logged. Both paths funnel through :meth:`ProviderConfig.resolve_key`.
- **Nothing secret is ever printable.** ``__repr__`` on the config objects redacts
  key material, and :func:`redact` scrubs key patterns out of arbitrary text before
  it can reach the event bus, a log line or the UI.
- **Validation is structural, not advisory.** Unknown providers, a missing
  `context_window` for a bound model, an inverted context threshold ladder, or a
  non-positive concurrency limit all raise :class:`ConfigError` at load time.
- **The file is not required to be executable or pretty** — only correct.

Usage:
    from engine.config import load
    cfg = load()                       # ./credentials.json, else the example
    cfg.providers["ollama"].base_url
    cfg.context.compact_at
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "ConfigError",
    "ProviderConfig",
    "ModelSpec",
    "ContextConfig",
    "ConcurrencyConfig",
    "HealthConfig",
    "SLOConfig",
    "DelegationConfig",
    "ExecutorConfig",
    "BudgetConfig",
    "PolicyConfig",
    "TelemetryConfig",
    "GoalConfig",
    "DefaultsConfig",
    "Config",
    "load",
    "redact",
    "scan_for_leaks",
    "write_provider",
    "set_defaults",
    "set_autonomy",
]

SUPPORTED_KINDS = ("openai", "anthropic", "ollama")
AUTONOMY_LEVELS = ("auto", "notify", "confirm", "manual")
ROUTE_CLASSES = ("R-CONTRACT", "R-REWORK", "R-DELEGATE", "R-ESCALATE", "R-CONFLICT", "R-MATCH-FAIL")
# The floor that prevents a single careless per-agent setting from disabling every
# human gate. R-ESCALATE and R-CONFLICT may not resolve below "confirm" unless the
# Owner explicitly opts in via policy.allow_autonomous_escalation.
SAFETY_FLOOR = {"R-ESCALATE": "confirm", "R-CONFLICT": "confirm"}


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed or unsafe."""


# ── redaction ────────────────────────────────────────────────────────────────

# Patterns that must never survive into a log, an event or the UI. Order matters:
# the most specific shapes run first so a broad rule cannot mangle a precise one.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),                      # OpenAI-style
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),                  # Anthropic-style
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{16,}"),                 # OpenAI project keys
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),                  # GitHub tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),                            # AWS access key id
    # Ollama (and similar) style: a long hex id, a dot, then an opaque token — e.g.
    # `96490a5b…43c7.HfFnMz-…pn98d`. Added because the bare value matched nothing above, so a key
    # pasted into a provider field could reach a log or a trace unredacted. Hex-then-dot is specific
    # enough not to mangle ordinary prose, and the alternative — not catching it — is a leaked
    # credential written to disk permanently.
    re.compile(r"\b[0-9a-fA-F]{24,}\.[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]{16,}"),        # Authorization headers
    re.compile(r"(?i)\bx-api-key\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*[\"']([^\"']{8,})[\"']"),
)
_REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Scrub key-shaped substrings from arbitrary text.

    Called on every string that leaves the engine for a log, event or file. It is
    intentionally aggressive: over-redacting a log line is harmless, under-redacting
    one leaks a credential into `trace.jsonl` permanently.
    """
    if not text:
        return text
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


def _redact_value(value: Any) -> Any:
    """Recursively redact a JSON-ish value in place (returns a copy)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


# ── leak scanning ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Leak:
    """One detected secret-shaped string in a file."""

    path: str
    line: int
    pattern: str
    preview: str


def scan_for_leaks(root: os.PathLike | str, *, max_bytes: int = 4 << 20) -> list[Leak]:
    """Scan a file or a directory tree for key-shaped strings.

    Run at startup over the workspace, and again before a diagnostics bundle is packaged. A hit means
    something wrote a secret where it should not have — the run-state directory, a trace file, a
    session transcript. Returning findings rather than raising lets the caller decide the severity, but
    the CLI treats a non-empty result as fatal.

    Accepts a file as well as a directory. That matters: `rglob` on a *file* path yields nothing, so an
    earlier version silently returned no findings when handed a file — which is precisely how a leak
    check passes a secret through. A check that cannot fail is worse than no check.
    """
    base = Path(root)
    findings: list[Leak] = []
    if not base.exists():
        return findings

    if base.is_file():
        candidates = [base]
    else:
        skip_dirs = {".git", "__pycache__", ".pytest_cache", "node_modules", ".build"}
        candidates = [p for p in base.rglob("*")
                      if p.is_file() and not skip_dirs.intersection(p.parts)]

    for path in candidates:
        try:
            if path.stat().st_size > max_bytes:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern in _SECRET_PATTERNS:
                match = pattern.search(line)
                if match:
                    raw = match.group(0)
                    findings.append(Leak(
                        path=str(path),
                        line=lineno,
                        pattern=pattern.pattern,
                        # Never store the secret itself — only enough to locate it.
                        preview=raw[:6] + "…" + f"({len(raw)} chars)",
                    ))
                    break
    return findings


# ── provider ─────────────────────────────────────────────────────────────────


#: Path suffixes that are an *operation*, not a base. Stripped when someone pastes a full endpoint.
#:
#: This is the difference between "your key is wrong" and "we probed a URL that does not exist". A
#: base URL is the part *before* the operation, and each adapter appends its own operation:
#: OpenAI-compatible appends `chat/completions` and `models`; Anthropic appends `v1/messages`; Ollama
#: appends `api/chat` and `api/tags`. So what counts as "the operation" is per-kind — for OpenAI the
#: `/v1` is part of the base and must survive, while for Anthropic it is part of the operation and must
#: go. Stripping the same suffix from both would turn a correct Anthropic URL into `/v1/v1/messages`.
_OPERATION_SUFFIXES: dict[str, tuple[str, ...]] = {
    "openai": ("chat/completions", "completions", "embeddings", "responses", "models"),
    "anthropic": ("v1/messages", "messages"),
    "ollama": ("api/chat", "api/generate", "api/tags", "api/embeddings", "api/show", "api/ps"),
}


def normalize_base_url(raw: str, kind: str = "") -> tuple[str, str]:
    """Reduce a pasted endpoint to the base a provider can actually append to.

    People are handed a *full* endpoint — `https://ollama.com/v1/chat/completions` is what the Ollama
    Cloud docs show — and type that into the base-URL field. Appending `/models` to it then probes
    `.../chat/completions/models`, which 404s, and the failure is reported as the provider being
    unreachable: a correct key and a correct URL, described as a broken configuration.

    So the operation is stripped here, once, and the caller is told what changed. Returns
    `(base_url, note)` where `note` is empty when nothing was adjusted.
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return url, ""
    suffixes = _OPERATION_SUFFIXES.get((kind or "").strip().lower(), ())
    for suffix in sorted(suffixes, key=len, reverse=True):
        if url.lower().endswith("/" + suffix):
            base = url[: -(len(suffix) + 1)].rstrip("/")
            # Never reduce to nothing: a bare `https://host` with "/models" is a real base.
            if base and "://" in base:
                return base, (f"used {base} as the base URL and dropped the operation "
                              f"'/{suffix}' — a base is the part before the call")
    return url, ""


@dataclass
class ProviderConfig:
    """One LLM provider endpoint."""

    id: str
    kind: str
    base_url: str
    api_key: str | None = None
    api_key_env: str | None = None
    api_version: str | None = None
    timeout_s: float = 120.0
    max_retries: int = 3
    concurrency: int = 2
    model_aliases: dict[str, str] = field(default_factory=dict)
    #: Extra request headers, sent as-is on every call to this provider.
    #:
    #: Endpoints other than the well-known ones routinely need something in a header — a gateway's
    #: routing key, an organisation id, an `X-Api-Key` instead of a Bearer token. Without this, "any
    #: OpenAI-compatible endpoint is a config entry, not new code" stopped being true the moment the
    #: endpoint wanted a header, and the fix was usually a proxy. Values are redacted in `__repr__`.
    extra_headers: dict[str, str] = field(default_factory=dict)
    #: Set when `base_url` was corrected on the way in, naming what changed.
    #:
    #: A `base_url` is the part *before* the operation — `https://ollama.com/v1`, not
    #: `https://ollama.com/v1/chat/completions`. People paste the full endpoint they were given, and
    #: the old behaviour then appended `/models` to it and probed a path that does not exist, so a
    #: perfectly good key looked broken. Normalising fixes the call; this note is how the user learns
    #: their input was understood rather than silently rewritten.
    base_url_note: str = ""

    def __post_init__(self) -> None:
        self.base_url, self.base_url_note = normalize_base_url(self.base_url, self.kind)

    def resolve_key(self) -> str | None:
        """Resolve the API key, preferring the environment over the file.

        Environment-first means the file can be shared or even committed by accident
        without exposing the credential. Local providers legitimately have no key.
        """
        if self.api_key_env:
            value = os.environ.get(self.api_key_env)
            if value:
                return value.strip()
        if self.api_key:
            return self.api_key.strip()
        return None

    def __repr__(self) -> str:  # pragma: no cover - trivial, but security-relevant
        return (
            f"ProviderConfig(id={self.id!r}, kind={self.kind!r}, "
            f"base_url={self.base_url!r}, api_key=<redacted>, "
            f"api_key_env={self.api_key_env!r}, "
            # Header *names* are useful in a log; their values may be credentials, so only the
            # names are shown — the same instinct as redacting the key itself.
            f"extra_headers={sorted(self.extra_headers)!r})"
        )


@dataclass(frozen=True)
class ModelSpec:
    """A model's measured or declared limits.

    ``context_window`` of ``None`` is meaningful: it means *unknown*, and an agent is
    refused at binding time rather than silently assuming a default. Assuming a window
    is how a run overflows in production.
    """

    model_id: str
    context_window: int | None = None
    max_output: int | None = None
    locality: str = "cloud"
    provider_id: str | None = None
    source: str = "declared"


# ── sub-configs ──────────────────────────────────────────────────────────────


def _check_ladder(compact: float, evict: float, overflow: float) -> None:
    if not (0.0 < compact < evict < overflow <= 1.0):
        raise ConfigError(
            "context thresholds must satisfy 0 < compact_at < evict_at < overflow_at <= 1; "
            f"got compact_at={compact}, evict_at={evict}, overflow_at={overflow}"
        )


@dataclass
class ContextConfig:
    compact_at: float = 0.70
    evict_at: float = 0.85
    overflow_at: float = 0.95
    attention_floor: float = 0.30
    attention_lambda: float = 0.10
    max_rotations_per_node: int = 4
    rotate_on_phase_change: bool = True
    output_reserve_frac: float = 0.15
    output_reserve_min: int = 1024
    output_reserve_max: int = 4096
    verbatim_markers: tuple[str, ...] = ("NEVER", "MUST NOT", "SECURITY", "AUTH", "COMPLIANCE")

    def __post_init__(self) -> None:
        _check_ladder(self.compact_at, self.evict_at, self.overflow_at)
        if not (0.0 < self.attention_floor <= 1.0):
            raise ConfigError(f"context.attention_floor must be in (0, 1]; got {self.attention_floor}")
        if self.attention_lambda <= 0:
            raise ConfigError(f"context.attention_lambda must be > 0; got {self.attention_lambda}")
        if self.max_rotations_per_node < 1:
            raise ConfigError("context.max_rotations_per_node must be >= 1")
        if self.output_reserve_min <= 0 or self.output_reserve_max < self.output_reserve_min:
            raise ConfigError("context output reserve bounds are inverted or non-positive")

    def output_reserve(self, context_window: int) -> int:
        """Tokens reserved for the model's reply, so we never fill the window.

        Bounded on both sides: a floor keeps tiny local models usable, a ceiling stops
        a large window from reserving an absurd amount.
        """
        return max(
            self.output_reserve_min,
            min(self.output_reserve_max, int(context_window * self.output_reserve_frac)),
        )


@dataclass
class ConcurrencyConfig:
    global_ceiling: int | None = None
    cpu_headroom: int = 1
    queue_max_depth: int = 64
    heartbeat_s: float = 30.0
    grace_s: float = 5.0
    per_provider_limits: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for key, value in self.per_provider_limits.items():
            if value < 1:
                raise ConfigError(f"concurrency.per_provider_limits[{key!r}] must be >= 1; got {value}")
        if self.queue_max_depth < 1:
            raise ConfigError("concurrency.queue_max_depth must be >= 1")
        if self.heartbeat_s <= 0 or self.grace_s < 0:
            raise ConfigError("concurrency.heartbeat_s must be > 0 and grace_s >= 0")


@dataclass
class ExecutorConfig:
    """Node-execution knobs.

    These were read via `getattr(config, "executor", None)` before the section existed, so every
    lookup silently fell through to the default: the knobs were documented as configurable and were
    not. Declaring them here is what makes the documentation true.
    """

    #: Whether a reply that omitted its machine-readable trailer gets one focused repair turn.
    #: On by default, because the alternative is parking a node whose work succeeded.
    trailer_repair: bool = True
    #: How many agents a `swarm` node may run. Capped because voting costs N times the tokens and
    #: N times the latency; the cap is reported whenever it truncates a larger binding.
    swarm_max_voters: int = 3
    #: How many fan-out subagents run in one batch. Bounded for the same reason, and because a
    #: 128-item job launched at once would exhaust a local provider's concurrency in a single burst.
    fanout_max_parallel: int = 4
    #: Whether a node declaring `tools: true` may actually use them. On, because the tool loop is the
    #: point of the feature; a deployment that wants a strict pipeline can turn it off here.
    tools_enabled: bool = True
    #: Forces every tool-using node read-only regardless of its capabilities. Set when a run is
    #: inspecting a project rather than changing it, so "nothing is written" is a property of the run.
    tools_read_only: bool = False
    #: The model-call bound for one tool-using node.
    max_tool_steps: int = 12
    #: Whether a tool-using node may dispatch isolated subagents (`task`/`fleet`). Off by default: a
    #: fan-out of tool-using children multiplies cost, so it is opt-in rather than incidental.
    subagents_enabled: bool = False
    #: How many subagents run at once when they are enabled.
    subagent_max_parallel: int = 4
    #: Whether `fleet` (the parallel shape) is offered. The single `task` shape is the conservative
    #: default; N-at-once is the throughput shape and is turned on deliberately.
    subagent_fleet_enabled: bool = False
    #: The ceiling on **one model reply** in tokens, when the bound model does not declare its own.
    #:
    #: This matters more than it looks, and it is the single most consequential number in the run.
    #: It was hardcoded at 4096, so a capable model writing a long artifact — a PRD, a design doc, a
    #: large diff — was cut off mid-sentence *before* it could emit its machine-readable trailer. The
    #: node then failed its own completion contract with "declared criteria not covered", and the real
    #: cause (truncation) was visible only as `finish_reason: length` in the trace. A real PRD run on a
    #: 1M-token model hit this repeatedly and it read as the model refusing to comply.
    #:
    #: Measured: the same run needed >16384 tokens and was still truncating. A model with a 1M-token
    #: window exists precisely to hold large artifacts, so the ceiling is generous and scales with the
    #: window — see `ExecutorContext._resolve_max_output`, which never asks for more than half the
    #: window (so the prompt still fits). Lower it to trade artifact completeness for cheaper calls.
    max_output_tokens: int = 32768

    def __post_init__(self) -> None:
        if self.swarm_max_voters < 1:
            raise ConfigError(
                f"executor.swarm_max_voters must be >= 1; got {self.swarm_max_voters}"
            )
        if self.fanout_max_parallel < 1:
            raise ConfigError(
                f"executor.fanout_max_parallel must be >= 1; got {self.fanout_max_parallel}"
            )
        if self.max_tool_steps < 1:
            raise ConfigError(
                f"executor.max_tool_steps must be >= 1; got {self.max_tool_steps}"
            )
        if self.subagent_max_parallel < 1:
            raise ConfigError(
                f"executor.subagent_max_parallel must be >= 1; got {self.subagent_max_parallel}"
            )


@dataclass
class GoalConfig:
    """The `[goal]` section: how a long-running objective is bounded, and how autonomous it is.

    A Goal has **no ceiling by default** (``token_budget = 0``), matching a coding agent you leave
    running: it continues until the agent reports completion, a genuine blocker, or you stop it. That
    is a deliberate weakening of the engine's "no unbounded bill" rule, so the cumulative spend is
    always tracked and shown, and a positive budget is both resumably enforceable and the recommended
    setting for anything unattended.

    **Autonomy.** The default is *autonomous*: arming a goal is standing authorisation to pass every
    gate that is mechanically decidable (the agent gate, a policy `confirm` route class) and to staff a
    gap by spawning a subagent on the default model. The person asked for a tool they can leave, so
    "a human is needed unless I chose one" is the intended polarity. Two per-goal switches restore the
    pause: `human_gate` parks at every gate, and setting `auto_approve`/`auto_hire` false narrows it.
    """

    #: Tokens a Goal may spend per *slice* before pausing. `0` means no ceiling (the default). A
    #: positive value pauses the goal with reason `budget_spend` and `goal resume` grants a fresh one.
    token_budget: int = 0
    #: How many times the same consecutive tool call is repeated before the run is reminded. Reminders,
    #: not stops: the calls still execute, because a legitimate retry after a transient failure is real.
    repeat_call_reminders: tuple[int, ...] = (3, 5, 8)
    #: Whether arming a goal is standing authorisation to pass auto-approvable gates.
    #:
    #: On by default. A **terminal** gate (release, close, spend — the manifest's `kind: human`) is
    #: never auto-approved: only the Owner holds that authority. This switch covers the gates the *org*
    #: can decide itself, and the per-goal `human_gate` / `auto_approve` override wins over it.
    auto_pass_auto_gates: bool = True
    #: Whether a plan's staffing gap is closed automatically instead of parking the run.
    #:
    #: On by default. A node whose skill no agent holds would otherwise stop the run three nodes in,
    #: far from the cause; spawning a subagent on the default model keeps the work moving, which is
    #: what "if the person does not exist, create one" asks for.
    auto_hire_missing: bool = True
    #: Whether an auto-created helper is *persisted* to the roster, or stays ephemeral.
    #:
    #: Off by default: an ephemeral subagent does the work and leaves no roster entry to clean up. A
    #: goal can opt in per-objective (`--persist-hires`) when the capability should be reused, and then
    #: the created employee appears in `agents` and the Org/People panels like any other hire.
    persist_auto_hires: bool = False
    #: The highest delegation tier an auto-hire may reach without asking. `0` means the safest tier
    #: only: a cheap, reversible helper. Anything above it parks rather than silently spending.
    auto_hire_max_tier: int = 0

    def __post_init__(self) -> None:
        if self.token_budget < 0:
            raise ConfigError(f"goal.token_budget must be >= 0 (0 means no ceiling); got "
                              f"{self.token_budget}")
        reminders = tuple(int(n) for n in (self.repeat_call_reminders or ()))
        if any(n < 1 for n in reminders):
            raise ConfigError("goal.repeat_call_reminders must all be >= 1")
        self.repeat_call_reminders = reminders or (3, 5, 8)
        if int(self.auto_hire_max_tier) < 0 or int(self.auto_hire_max_tier) > 2:
            raise ConfigError(
                "goal.auto_hire_max_tier must be between 0 (safest only) and 2; got "
                f"{self.auto_hire_max_tier}"
            )


@dataclass
class DefaultsConfig:
    """The `[defaults]` section: the provider and model everyone uses unless told otherwise.

    This is the one answer to "which model do my people run on". Before it, three separate places
    each picked their own fallback — `hire` defaulted to `ollama`, `default_company` to
    `qwen2.5-coder:7b`, the planner to whatever was first in the catalog — so a person could hire onto
    one model and watch the built-in company run on another. Resolving it **once**, here, is what makes
    "the default unless I specify" actually hold everywhere.

    A provider/model pair is only a *preference*: :meth:`Config.default_pair` validates it against what
    is actually configured and reachable, and degrades with a stated reason rather than binding an
    agent to a model that no longer exists.
    """

    provider: str = ""
    model: str = ""
    #: The model reviewers run on, so a verifier differs from its producer by construction.
    #: Empty means "find a distinct one, or fall back with a warning" — never a silent same-model
    #: verdict presented as independent.
    reviewer_provider: str = ""
    reviewer_model: str = ""
    temperature: float = 0.2
    #: A context window to use when the catalogue cannot report one. Without it a default model with an
    #: unknown window cannot be bound at all, which is the most common first-run failure.
    context_window: int | None = None

    def __post_init__(self) -> None:
        if not (0.0 <= float(self.temperature) <= 2.0):
            raise ConfigError(
                f"defaults.temperature must be between 0 and 2; got {self.temperature}"
            )
        if self.context_window is not None and int(self.context_window) < 1024:
            raise ConfigError(
                "defaults.context_window must be at least 1024 when set; got "
                f"{self.context_window}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model,
                "reviewer_provider": self.reviewer_provider,
                "reviewer_model": self.reviewer_model,
                "temperature": self.temperature,
                "context_window": self.context_window}


@dataclass
class HealthConfig:
    window_tasks: int = 20
    min_samples: int = 5
    healthy_at: float = 0.80
    degraded_at: float = 0.50
    hard_trigger_contract_breaches: int = 3
    quarantine_policy: str = "automatic"
    probe_enabled: bool = True
    retirement_idle_runs: int = 20

    def __post_init__(self) -> None:
        if not (0.0 <= self.degraded_at < self.healthy_at <= 1.0):
            raise ConfigError(
                "health thresholds must satisfy 0 <= degraded_at < healthy_at <= 1; "
                f"got degraded_at={self.degraded_at}, healthy_at={self.healthy_at}"
            )
        if self.min_samples < 1:
            raise ConfigError("health.min_samples must be >= 1 (a first failure must not quarantine)")
        if self.window_tasks < self.min_samples:
            raise ConfigError("health.window_tasks must be >= min_samples")


@dataclass
class SLOConfig:
    run: dict[str, float] = field(default_factory=dict)
    agent: dict[str, float] = field(default_factory=dict)


@dataclass
class DelegationConfig:
    max_depth: int = 3
    span_of_control: int = 5
    allow_ephemeral: bool = True
    budget_share_max: float = 0.50
    anti_sprawl_window_runs: int = 5
    anti_sprawl_growth_threshold: float = 0.20
    approval_tiers: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_depth < 1:
            raise ConfigError("delegation.max_depth must be >= 1")
        if self.span_of_control < 1:
            raise ConfigError("delegation.span_of_control must be >= 1")
        if not (0.0 < self.budget_share_max <= 1.0):
            raise ConfigError(
                f"delegation.budget_share_max must be in (0, 1]; got {self.budget_share_max}"
            )

    def elevated_markers(self) -> tuple[str, ...]:
        """Capability prefixes that force an Owner gate regardless of budget."""
        raw = self.approval_tiers.get("elevated_capability_markers")
        if isinstance(raw, list) and raw:
            return tuple(str(m) for m in raw)
        return ("write:", "deploy:", "exec:", "admin:")


@dataclass
class BudgetConfig:
    run_max_usd: float = 25.0
    run_max_tokens: int = 4_000_000
    org_max_usd_daily: float = 100.0

    def __post_init__(self) -> None:
        if self.run_max_usd <= 0:
            raise ConfigError("budget.run_max_usd must be > 0")
        if self.run_max_tokens <= 0:
            raise ConfigError("budget.run_max_tokens must be > 0")
        if self.org_max_usd_daily <= 0:
            raise ConfigError("budget.org_max_usd_daily must be > 0")


@dataclass
class RouterConfig:
    threshold: float = 0.62
    margin: float = 0.15

    def __post_init__(self) -> None:
        if not (0.0 <= self.threshold <= 1.0):
            raise ConfigError(f"policy.router.threshold must be in [0, 1]; got {self.threshold}")
        if not (0.0 <= self.margin <= 1.0):
            raise ConfigError(f"policy.router.margin must be in [0, 1]; got {self.margin}")


@dataclass
class PolicyConfig:
    default_autonomy: dict[str, str] = field(default_factory=dict)
    allow_autonomous_escalation: bool = False
    router: RouterConfig = field(default_factory=RouterConfig)

    def __post_init__(self) -> None:
        # `_build_simple` cannot know this field is a nested dataclass, so a raw dict
        # arrives here. Coerce it, otherwise RouterConfig's own range validation never
        # runs and an out-of-range threshold slips through unvalidated.
        if isinstance(self.router, dict):
            valid = {f.name for f in fields(RouterConfig)}
            self.router = RouterConfig(**{k: v for k, v in self.router.items() if k in valid})
        for route_class, level in self.default_autonomy.items():
            if route_class not in ROUTE_CLASSES:
                raise ConfigError(
                    f"policy.default_autonomy has unknown route class {route_class!r}; "
                    f"valid: {', '.join(ROUTE_CLASSES)}"
                )
            if level not in AUTONOMY_LEVELS:
                raise ConfigError(
                    f"policy.default_autonomy[{route_class!r}] = {level!r} is not one of "
                    f"{', '.join(AUTONOMY_LEVELS)}"
                )
        self.enforce_floor(self.allow_autonomous_escalation)

    def enforce_floor(self, allow_autonomous_escalation: bool) -> None:
        """Refuse a configuration that would disable the human gates wholesale.

        The safety floor is the difference between "autonomous organisation" and
        "unmonitored organisation". A single careless per-agent setting must not be
        able to remove every human gate, so this is validated at the org level too.
        """
        if allow_autonomous_escalation:
            return
        order = {level: i for i, level in enumerate(AUTONOMY_LEVELS)}
        for route_class, floor in SAFETY_FLOOR.items():
            level = self.default_autonomy.get(route_class)
            if level is not None and order[level] < order[floor]:
                raise ConfigError(
                    f"policy.default_autonomy[{route_class!r}] = {level!r} is below the safety "
                    f"floor {floor!r}.\n"
                    "  R-ESCALATE and R-CONFLICT may not resolve below 'confirm' unless "
                    "policy.allow_autonomous_escalation is explicitly set to true."
                )


@dataclass
class TelemetryConfig:
    spans_path: str | None = None
    sample_default: float = 1.0
    sample_on_escalation: float = 1.0
    export_enabled: bool = True


# ── top-level config ─────────────────────────────────────────────────────────


@dataclass
class Config:
    """The validated, redaction-safe view of `credentials.json`."""

    path: Path | None = None
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    known_models: dict[str, ModelSpec] = field(default_factory=dict)
    catalog: dict[str, Any] = field(default_factory=dict)
    #: The raw `[defaults]` document, kept for compatibility with the loader's warnings machinery.
    defaults: dict[str, Any] = field(default_factory=dict)
    #: The validated view of `[defaults]`, and the single source of truth for the default pair.
    default: DefaultsConfig = field(default_factory=DefaultsConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    slo: SLOConfig = field(default_factory=SLOConfig)
    delegation: DelegationConfig = field(default_factory=DelegationConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    goal: GoalConfig = field(default_factory=GoalConfig)
    schemas: dict[str, str] = field(default_factory=dict)
    idempotency: dict[str, Any] = field(default_factory=dict)
    library: dict[str, Any] = field(default_factory=dict)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # ── lookup helpers ──────────────────────────────────────────────────────

    def provider(self, provider_id: str) -> ProviderConfig:
        try:
            return self.providers[provider_id]
        except KeyError:
            raise ConfigError(
                f"unknown provider {provider_id!r}; configured: "
                f"{', '.join(sorted(self.providers)) or '(none)'}"
            ) from None

    def model_spec(self, model_id: str) -> ModelSpec:
        """Return what we know about a model, never inventing a window.

        Falls back to an `assumed`-sourced spec with unknown limits when the catalog
        has no entry, so callers can surface "unknown" rather than a fabricated number.
        """
        spec = self.known_models.get(model_id)
        if spec is not None:
            return spec
        return ModelSpec(model_id=model_id, context_window=None, max_output=None, source="assumed")

    def alias(self, provider_id: str, model: str) -> str:
        """Expand a friendly alias to the provider's real model id.

        A provider that is not in the configuration — one registered at runtime, such as a
        test double, or a provider built from an environment override — has no aliases to
        consult. That is a normal case rather than an error, so the model passes through
        unchanged instead of raising.
        """
        spec = self.providers.get(provider_id)
        if spec is None:
            return model
        return spec.model_aliases.get(model, model)

    # ── the default pair ────────────────────────────────────────────────────
    #
    # One resolution, used by every caller that needs "the model my people run on". Before this each
    # of `hire`, `default_company` and the planner picked its own fallback, which is how a person
    # could hire onto one model and watch the built-in company run on another.

    def _usable_providers(self) -> list[str]:
        """Configured provider ids that look reachable, most-preferred first.

        A provider with no key and no local host is *configured but unusable*; choosing it as the
        default would make every hire fail with a key error. Local kinds (ollama) need no key, so
        they stay in. The ordering is deterministic so two runs agree.
        """
        usable: list[str] = []
        for pid in sorted(self.providers):
            spec = self.providers[pid]
            if spec.kind == "ollama" or spec.resolve_key():
                usable.append(pid)
        return usable

    def default_pair(self) -> tuple[str, str, str]:
        """The effective default ``(provider, model, reason)`` for the whole organisation.

        Resolution order, and the reason is always returned so a caller can say *why* the answer
        differs from the file:

        1. ``defaults.provider`` + ``defaults.model`` when both are configured and usable.
        2. the configured provider with a declared model that has a known window.
        3. the first usable provider with any model, else the first configured provider at all.

        The model is expanded through the provider's alias table, so ``claude-sonnet`` becomes the
        provider's real id at the one place that decides. An empty model is a legitimate result — it
        means *nothing is configured yet* — and callers say so rather than binding a placeholder.
        """
        configured = self.providers
        declared = self.default
        if declared.provider and declared.provider in configured:
            model = self.alias(declared.provider, declared.model) if declared.model else ""
            if model:
                return declared.provider, model, "configured default"
            # A default naming a provider but no model: use that provider's first known model.
            for model_id, spec in sorted(self.known_models.items()):
                if spec.context_window:
                    return declared.provider, model_id, "configured provider, first known model"

        for pid in self._usable_providers():
            for model_id, spec in sorted(self.known_models.items()):
                if spec.context_window and pid in self.default_models_for(pid):
                    return pid, model_id, "first usable provider with a known model"

        usable = self._usable_providers()
        if usable:
            return usable[0], "", "first usable provider, no model declared"
        if configured:
            first = sorted(configured)[0]
            return first, "", "first configured provider (no usable one)"
        return "", "", "no providers are configured"

    def default_models_for(self, provider_id: str) -> set[str]:
        """Model ids that plausibly belong to a provider.

        The catalogue records `provider_id` on a probed entry; a declared model (the `known` table) has
        none. So a declared model is attributable to any provider whose alias table names it, and to
        every provider when nothing narrows it down — the alternative is refusing a perfectly good
        declared default because it was never probed.
        """
        out: set[str] = set()
        for entry in (self.catalog.get("models") or []):
            if isinstance(entry, dict) and str(entry.get("provider_id") or "") == provider_id:
                out.add(str(entry.get("model_id") or ""))
        spec = self.providers.get(provider_id)
        if spec is not None:
            out.update(spec.model_aliases.values())
        if not out:
            out.update(self.known_models.keys())
        return {m for m in out if m}

    def default_model_spec(self) -> ModelSpec:
        """What we know about the default model, with the configured override applied.

        ``defaults.context_window`` exists because a *declared but unprobed* model is the most common
        first-run failure: the window is unknown, so no agent can be bound, and the person sees a
        refusal instead of an agent. Naming the window in the config resolves it without a probe.
        """
        _, model, _ = self.default_pair()
        spec = self.model_spec(model)
        if spec.context_window is None and self.default.context_window:
            return ModelSpec(model_id=model, context_window=int(self.default.context_window),
                             max_output=spec.max_output, locality=spec.locality,
                             provider_id=spec.provider_id, source="config")
        return spec

    def public_dict(self) -> dict[str, Any]:
        """A redacted, JSON-serialisable view safe to emit as an event."""
        return {
            "path": str(self.path) if self.path else None,
            "providers": {
                pid: {
                    "kind": p.kind,
                    "base_url": p.base_url,
                    "api_key_env": p.api_key_env,
                    "has_key": bool(p.resolve_key()),
                    "concurrency": p.concurrency,
                }
                for pid, p in self.providers.items()
            },
            "defaults": _redact_value(self.defaults),
            "budget": {
                "run_max_usd": self.budget.run_max_usd,
                "run_max_tokens": self.budget.run_max_tokens,
                "org_max_usd_daily": self.budget.org_max_usd_daily,
            },
            "policy": {
                "default_autonomy": dict(self.policy.default_autonomy),
                "allow_autonomous_escalation": self.policy.allow_autonomous_escalation,
            },
        }


# ── loading ──────────────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"config {path} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must be a JSON object at the top level")
    return data


def _check_permissions(path: Path) -> list[str]:
    """Warn when a config holding a literal key is group/world readable.

    Only warns when a literal `api_key` is present; an env-referenced config holds no
    secret, so a lax mode on it is noise rather than a finding.
    """
    warnings: list[str] = []
    try:
        mode = path.stat().st_mode
    except OSError:
        return warnings
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        try:
            has_literal = bool(json.loads(path.read_text(encoding="utf-8")).get("providers"))
        except (OSError, ValueError):
            has_literal = False
        if has_literal:
            warnings.append(
                f"{path} is group/world readable (mode {oct(mode & 0o777)}). "
                "Run: chmod 600 " + str(path)
            )
    return warnings


def _remove_provider_references(document: dict[str, Any], provider_id: str) -> None:
    """Drop every reference to a provider that has just been removed.

    Two places name a provider: `concurrency.per_provider_limits[pid]` and `defaults.provider`. Leaving
    either behind produced a config the loader had to repair — and before the loader learned to repair,
    it refused to load at all, which made removing a provider from the console a way to brick the whole
    engine. Both are pruned here so the state is never written in the first place.

    Removing the default is deliberately *not* replaced with a guess at another provider: a default
    chosen silently by this function would be a routing decision the user did not make. The absence is
    resolved by the loader (`defaults.provider` is dropped, a configured provider is used), and the
    console shows the remaining providers so a new default is one click away.
    """
    concurrency = document.get("concurrency")
    if isinstance(concurrency, dict):
        limits = concurrency.get("per_provider_limits")
        if isinstance(limits, dict):
            limits.pop(provider_id, None)
    defaults = document.get("defaults")
    if isinstance(defaults, dict) and str(defaults.get("provider") or "") == provider_id:
        defaults.pop("provider", None)


def write_provider(path: os.PathLike | str, entry: dict[str, Any], *,
                   provider_id: str | None = None,
                   remove: bool = False) -> Path:
    """Add, replace or remove one provider entry in a credentials document, atomically.

    Why this exists as a function rather than inline in the console handler: writing a config is the
    one operation where a bug destroys something the user cannot re-derive. Three rules are therefore
    enforced in one place, and none of them are negotiable:

    - **Merge, never replace.** Only the named provider key changes. A naive `json.dump` of the new
      provider would drop every other provider, the model windows and the whole policy block — so the
      user's existing configuration would disappear the first time they added an endpoint.
    - **Atomic, then `0600`.** Temp file, `os.replace`, and the mode set *before* the secret is
      written. A file that is briefly world-readable is a leaked key, and a torn write is a config
      the user cannot load.
    - **Refuse to invent a path.** The target must be an existing file, so a typo cannot create a
      second credentials file somewhere the engine will never read.

    `entry` is stored under `providers[provider_id]`; pass `remove=True` with an id to delete one.
    """
    target, document = _read_document(path)
    pid = str(provider_id or entry.get("id") or "").strip()
    if not pid:
        raise ConfigError("a provider entry needs an id")

    providers = document.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        document["providers"] = providers

    if remove:
        providers.pop(pid, None)
        # Prune the references to the provider that has just gone, in the same write, so the console
        # cannot leave a config the loader must repair. A dangling `per_provider_limits` entry or a
        # `defaults.provider` naming the removed provider is exactly what made "remove a provider"
        # brick the engine (the loader refuses-or-prunes it), so the fix belongs at the writer too:
        # the state is never created, rather than cleaned up afterwards.
        _remove_provider_references(document, pid)
    else:
        payload = dict(entry)
        payload.pop("id", None)
        payload["kind"] = str(payload.get("kind") or "openai").strip().lower()
        providers[pid] = payload

    tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
    _write_document_atomic(target, document, tmp)
    return target


def _write_document_atomic(target: Path, document: dict[str, Any], tmp: Path) -> None:
    """Write a JSON document with mode `0600`, fsynced, then renamed into place.

    Split out so `write_provider`, `set_defaults` and `set_autonomy` share exactly one
    implementation of the rule that matters: **the mode is set before the secret is written**, and a
    torn write never replaces a good file.
    """
    try:
        # Mode first, contents second: the file is never on disk with a secret in it and a lax mode.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigError(f"failed to write {target}: {exc}") from exc


def _read_document(path: os.PathLike | str) -> tuple[Path, dict[str, Any]]:
    """Read a credentials document, refusing to invent one at a path that does not exist."""
    target = Path(path).expanduser()
    if not target.is_file():
        raise ConfigError(
            f"no credentials file at {target}; refusing to create one. Copy "
            "credentials.example.json to credentials.json first, or edit the existing file."
        )
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{target} is not a JSON object")
    return target, document


def set_defaults(path: os.PathLike | str, *, provider: str = "", model: str = "",
                 reviewer_provider: str = "", reviewer_model: str = "",
                 context_window: int | None = None,
                 clear: Iterable[str] = ()) -> Path:
    """Merge a change into `defaults`, atomically, leaving everything else untouched.

    This is what makes "a default provider and model that everyone uses unless I specify" a real,
    one-command setting rather than a hand edit. Three rules, the same ones `write_provider` follows:

    - **Merge, never replace.** Only the named keys change; providers, windows and the policy block
      survive. A person setting a default must never lose their keys.
    - **Atomic, then `0600`.** A partial defaults block is a config that loads to something nobody
      chose.
    - **An empty string does not silently overwrite.** Pass `clear=("model",)` to remove a key
      deliberately; omitting `model` leaves the existing value alone.
    """
    target, document = _read_document(path)
    defaults = document.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
        document["defaults"] = defaults

    if provider:
        defaults["provider"] = str(provider).strip()
    if model:
        defaults["model"] = str(model).strip()
    if context_window is not None:
        defaults["context_window"] = int(context_window)
    # The reviewer pair is written in the readable nested form the loader also accepts.
    reviewer = defaults.get("reviewer")
    if reviewer_provider or reviewer_model:
        if not isinstance(reviewer, dict):
            reviewer = {}
            defaults["reviewer"] = reviewer
        if reviewer_provider:
            reviewer["provider"] = str(reviewer_provider).strip()
        if reviewer_model:
            reviewer["model"] = str(reviewer_model).strip()
        if not reviewer:
            defaults.pop("reviewer", None)

    for key in clear:
        defaults.pop(str(key), None)
    if not defaults:
        document.pop("defaults", None)

    tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
    _write_document_atomic(target, document, tmp)
    return target


def set_autonomy(path: os.PathLike | str, *, goal: dict[str, Any] | None = None) -> Path:
    """Merge a change into the `[goal]` autonomy block, atomically.

    Kept separate from `set_defaults` so the *model* decision and the *authority* decision are two
    deliberate writes. A person changing which model their people run on should not accidentally
    change whether a run needs them.
    """
    target, document = _read_document(path)
    if not goal:
        return target
    section = document.get("goal")
    if not isinstance(section, dict):
        section = {}
        document["goal"] = section
    valid = {f.name for f in fields(GoalConfig)}
    for key, value in goal.items():
        if str(key) in valid and value is not None:
            section[str(key)] = value
    tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
    _write_document_atomic(target, document, tmp)
    return target


def _build_providers(raw: dict[str, Any]) -> dict[str, ProviderConfig]:
    providers: dict[str, ProviderConfig] = {}
    if not isinstance(raw, dict):
        raise ConfigError("config 'providers' must be an object")
    for pid, spec in raw.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"provider {pid!r} must be an object")
        kind = str(spec.get("kind", "")).strip().lower()
        if kind not in SUPPORTED_KINDS:
            raise ConfigError(
                f"provider {pid!r} has unsupported kind {kind!r}; "
                f"supported: {', '.join(SUPPORTED_KINDS)}"
            )
        base_url = spec.get("base_url")
        if not base_url:
            raise ConfigError(f"provider {pid!r} is missing base_url")
        aliases = spec.get("model_aliases") or {}
        if not isinstance(aliases, dict):
            raise ConfigError(f"provider {pid!r} model_aliases must be an object")
        headers = spec.get("extra_headers") or {}
        if not isinstance(headers, dict):
            raise ConfigError(f"provider {pid!r} extra_headers must be an object")
        try:
            providers[pid] = ProviderConfig(
                id=pid,
                kind=kind,
                base_url=str(base_url).rstrip("/"),
                api_key=spec.get("api_key"),
                api_key_env=spec.get("api_key_env"),
                api_version=spec.get("api_version"),
                timeout_s=float(spec.get("timeout_s", 120)),
                max_retries=int(spec.get("max_retries", 3)),
                concurrency=int(spec.get("concurrency", 2)),
                model_aliases={str(k): str(v) for k, v in aliases.items()},
                extra_headers={str(k): str(v) for k, v in headers.items()},
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"provider {pid!r} has an invalid field: {exc}") from exc
        if providers[pid].timeout_s <= 0:
            raise ConfigError(f"provider {pid!r} timeout_s must be > 0")
        if providers[pid].max_retries < 0:
            raise ConfigError(f"provider {pid!r} max_retries must be >= 0")
        if providers[pid].concurrency < 1:
            raise ConfigError(f"provider {pid!r} concurrency must be >= 1")
    if not providers:
        raise ConfigError("config defines no providers; at least one is required")
    return providers


def _build_models(raw: dict[str, Any]) -> tuple[dict[str, ModelSpec], dict[str, Any]]:
    """Build the known-model table and return the catalog settings alongside it."""
    section = raw.get("models") or {}
    if not isinstance(section, dict):
        raise ConfigError("config 'models' must be an object")
    catalog = section.get("catalog") or {}
    known_raw = section.get("known") or {}
    if not isinstance(known_raw, dict):
        raise ConfigError("config 'models.known' must be an object")
    known: dict[str, ModelSpec] = {}
    for model_id, spec in known_raw.items():
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ConfigError(f"models.known[{model_id!r}] must be an object")
        window = spec.get("context_window")
        if window is not None:
            try:
                window = int(window)
            except (TypeError, ValueError):
                raise ConfigError(
                    f"models.known[{model_id!r}].context_window must be an integer or null"
                ) from None
            if window <= 0:
                raise ConfigError(f"models.known[{model_id!r}].context_window must be > 0")
        max_out = spec.get("max_output")
        if max_out is not None:
            try:
                max_out = int(max_out)
            except (TypeError, ValueError):
                raise ConfigError(
                    f"models.known[{model_id!r}].max_output must be an integer or null"
                ) from None
        locality = str(spec.get("locality", "cloud"))
        if locality not in ("local", "cloud"):
            raise ConfigError(
                f"models.known[{model_id!r}].locality must be 'local' or 'cloud'; got {locality!r}"
            )
        known[model_id] = ModelSpec(
            model_id=model_id,
            context_window=window,
            max_output=max_out,
            locality=locality,
            provider_id=spec.get("provider_id"),
            source=str(spec.get("source", "declared")),
        )
    return known, catalog


def _build_simple(cls, raw_section: Any, name: str):
    """Instantiate a dataclass sub-config from a mapping, ignoring unknown keys."""
    if raw_section is None:
        return cls()
    if not isinstance(raw_section, dict):
        raise ConfigError(f"config {name!r} must be an object")
    valid = {f.name for f in fields(cls)}
    kwargs = {}
    for key, value in raw_section.items():
        if key in valid:
            kwargs[key] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"config {name!r} has an invalid value: {exc}") from exc


def _build_defaults(raw_section: Any) -> DefaultsConfig:
    """Instantiate :class:`DefaultsConfig`, tolerating the legacy and the nested shapes.

    `defaults` was a plain dict before this change, and the example file still writes the flat form
    (`{"provider": …, "model": …}`). Both are read, so an existing credentials.json keeps working —
    the nested form exists only so a caller can hand it a validated object.
    """
    if raw_section is None:
        return DefaultsConfig()
    if isinstance(raw_section, DefaultsConfig):
        return raw_section
    if not isinstance(raw_section, dict):
        raise ConfigError("config 'defaults' must be an object")
    # A `reviewer` sub-object is the readable form; the flat keys are kept alongside it.
    nested = raw_section.get("reviewer")
    reviewer = nested if isinstance(nested, dict) else {}
    known = {f.name for f in fields(DefaultsConfig)}
    kwargs: dict[str, Any] = {k: v for k, v in raw_section.items() if k in known}
    if reviewer:
        for key, target in (("provider", "reviewer_provider"), ("model", "reviewer_model")):
            value = reviewer.get(key)
            if value and not kwargs.get(target):
                kwargs[target] = str(value)
    if "context_window" in kwargs and kwargs["context_window"] in ("", None):
        kwargs.pop("context_window")
    try:
        return DefaultsConfig(**kwargs)
    except (TypeError, ConfigError) as exc:
        # A bad defaults block must not brick the engine — every other setting would still be usable.
        # It is reported as a warning and the safe defaults apply, which is the same degradation the
        # loader already applies to a stale `defaults.provider`.
        raise ConfigError(f"config 'defaults' has an invalid value: {exc}") from exc


def _defaults_or_warn(raw_section: Any, warnings: list[str]) -> DefaultsConfig:
    """Build the defaults view, degrading to the safe defaults with a stated reason.

    A *typo* in one optional block must not take down `doctor`, which exists to explain the problem —
    the same reasoning that makes a stale `defaults.provider` degrade rather than refuse.
    """
    try:
        return _build_defaults(raw_section)
    except ConfigError as exc:
        warnings.append(f"{exc}; using the built-in defaults. Fix the file or the Defaults editor.")
        return DefaultsConfig()


def load(path: os.PathLike | str | None = None, *, warn: bool = True) -> Config:
    """Load and validate configuration.

    Resolution order: explicit ``path``, ``$AGENTORG_CREDENTIALS``, ``./credentials.json``,
    then ``credentials.example.json`` next to it. Falling back to the example lets the
    app open on a fresh machine so the Owner can build an org before wiring keys — the
    first-run experience depends on that.

    Returns a :class:`Config`. Accumulated permission warnings are attached to
    ``cfg.raw["_warnings"]`` so the CLI can surface them without this function
    printing anything itself.
    """
    candidates: list[Path] = []
    explicit = False
    if path is not None:
        candidates.append(Path(path).expanduser())
        explicit = True
    else:
        env_path = os.environ.get("AGENTORG_CREDENTIALS")
        if env_path:
            # An explicit pointer is honoured exclusively: silently falling back to a
            # different config when the named one is missing would mean running with
            # providers and budgets the operator did not intend.
            candidates.append(Path(env_path).expanduser())
            explicit = True
        else:
            here = Path(__file__).resolve().parent.parent
            candidates.append(here / "credentials.json")
            candidates.append(Path.cwd() / "credentials.json")
            candidates.append(here / "credentials.example.json")

    chosen: Path | None = None
    for candidate in candidates:
        if candidate.is_file():
            chosen = candidate
            break
    if chosen is None:
        hint = (
            "Copy credentials.example.json to credentials.json and fill it in."
            if not explicit
            else "The path was given explicitly, so no fallback was attempted."
        )
        raise ConfigError(
            "no configuration found. Looked for:\n  "
            + "\n  ".join(str(c) for c in candidates)
            + "\n" + hint
        )

    raw = _read_json(chosen)
    warnings = _check_permissions(chosen) if warn else []

    providers = _build_providers(raw.get("providers") or {})
    known_models, catalog = _build_models(raw)

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError("config 'defaults' must be an object")
    default_provider = defaults.get("provider")
    if default_provider and default_provider not in providers:
        # A default naming a provider that is no longer configured is a *stale pointer*, not a typo to
        # refuse over. Refusing here bricked the whole engine — every command, including `doctor`, which
        # exists to diagnose it — the moment a provider was removed from the console. So it degrades:
        # the default is dropped, the reason is recorded as a warning, and the caller falls back to
        # whatever provider is configured. A call that *names* the missing provider still fails
        # loudly at the gateway (`unknown provider … available: …`), so nothing is silently misrouted.
        warnings.append(
            f"defaults.provider {default_provider!r} is not a configured provider "
            f"({', '.join(sorted(providers)) or 'none'}); ignoring it and using a configured default. "
            f"Fix the file or pick a provider in the console."
        )
        defaults = dict(defaults)
        defaults.pop("provider", None)
        default_provider = None

    concurrency = _build_simple(ConcurrencyConfig, raw.get("concurrency"), "concurrency")
    # A per-provider limit naming an unknown provider is a *dangling reference* left by a provider
    # removal, not a reason to refuse the whole config. Pruning it here is the fix that cannot brick
    # the engine: `write_provider(remove=True)` already avoids leaving it behind, and this recovers the
    # files it created before that fix. The prune is reported so it is never silent.
    unknown_limits = set(concurrency.per_provider_limits) - set(providers)
    if unknown_limits:
        warnings.append(
            "concurrency.per_provider_limits named providers that are not configured "
            f"({', '.join(sorted(unknown_limits))}); those entries were pruned. "
            "This is left behind when a provider is removed."
        )
        for stale in sorted(unknown_limits):
            concurrency.per_provider_limits.pop(stale, None)
    # A configured provider with no explicit limit inherits its own concurrency field.
    for pid, prov in providers.items():
        concurrency.per_provider_limits.setdefault(pid, prov.concurrency)

    policy = _build_simple(PolicyConfig, raw.get("policy"), "policy")
    if not policy.default_autonomy:
        policy.default_autonomy = {
            "R-CONTRACT": "auto",
            "R-REWORK": "auto",
            "R-DELEGATE": "auto",
            "R-ESCALATE": "confirm",
            "R-CONFLICT": "confirm",
            "R-MATCH-FAIL": "confirm",
        }
        policy.enforce_floor(policy.allow_autonomous_escalation)

    schemas = raw.get("schemas") or {}
    if not isinstance(schemas, dict):
        raise ConfigError("config 'schemas' must be an object")

    cfg = Config(
        path=chosen,
        providers=providers,
        known_models=known_models,
        catalog=catalog if isinstance(catalog, dict) else {},
        defaults=defaults,
        default=_defaults_or_warn(defaults, warnings),
        context=_build_simple(ContextConfig, raw.get("context"), "context"),
        concurrency=concurrency,
        health=_build_simple(HealthConfig, raw.get("health"), "health"),
        slo=_build_simple(SLOConfig, raw.get("slo"), "slo"),
        delegation=_build_simple(DelegationConfig, raw.get("delegation"), "delegation"),
        executor=_build_simple(ExecutorConfig, raw.get("executor"), "executor"),
        budget=_build_simple(BudgetConfig, raw.get("budget"), "budget"),
        policy=policy,
        goal=_build_simple(GoalConfig, raw.get("goal"), "goal"),
        schemas={str(k): str(v) for k, v in schemas.items()},
        idempotency=raw.get("idempotency") or {},
        library=raw.get("library") or {},
        telemetry=_build_simple(TelemetryConfig, raw.get("telemetry"), "telemetry"),
        raw=raw,
    )
    cfg.raw["_warnings"] = warnings
    return cfg
