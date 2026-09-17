#!/usr/bin/env python3
"""catalog.py — ask each provider what models it actually has.

WHY THIS EXISTS
---------------
The Owner picks a model when naming an agent, so the app must present real options
rather than a hardcoded list that rots the moment a provider ships a new model or
retires an old one. It must also answer a second, sharper question: **what is this
model's context window?** The entire session-projection and rotation design depends on
that number being real.

DESIGN
------
- **`None` always means unknown, never a default.** A guessed context window silently
  corrupts the pre-flight projection and causes real overflows at the worst moment. So a
  model whose window we could not determine is reported as unknown, and binding an agent
  to it is refused with that reason.
- **Every descriptor carries `source`: probed | declared | assumed.** The picker shows
  which, so the Owner can see what is verified versus what came from the config file.
  Ollama can be *probed* accurately via `/api/show`; most cloud `/v1/models` endpoints
  report only ids, so those fall back to the curated table.
- **Discovery never blocks the app.** A local provider is often not running. A failed
  probe is recorded as a status, not raised, and the offline table still yields a usable
  list so the Owner can build an org before wiring keys.
- **Results are cached with a TTL and can be refreshed deliberately.** Re-probing on
  every keystroke in the picker would hammer a local server.
- **A TTL-expired entry is refreshed lazily on read**, so the common path (open the
  picker) is fast and the stale path (a model was pulled an hour ago) still corrects.

Usage:
    catalog = ModelCatalog(config, providers)
    entries = catalog.list_models(refresh=False)
    spec = catalog.resolve("ollama", "qwen2.5-coder:7b")
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config, ModelSpec
from .providers.base import ErrorKind, GatewayError, Provider

__all__ = ["ModelEntry", "ModelCatalog"]


@dataclass
class ModelEntry:
    """One model as the Owner sees it.

    `context_window=None` is a first-class value meaning *not determined*. The UI renders
    it distinctly and binding refuses it, because the alternative — assuming a number —
    is how a run overflows in production.
    """

    provider_id: str
    model_id: str
    display_name: str = ""
    context_window: int | None = None
    max_output: int | None = None
    locality: str = "cloud"
    quantization: str | None = None
    loaded: bool | None = None
    size_bytes: int | None = None
    parameter_size: str | None = None
    # Provenance of the *metadata*, not the model: how we came to know these limits.
    source: str = "assumed"      # "probed" | "declared" | "assumed"
    available: bool | None = None  # probed reachability of the provider

    @property
    def window_known(self) -> bool:
        """True when the context window is known well enough to bind an agent to."""
        return isinstance(self.context_window, int) and self.context_window > 0

    def as_dict(self) -> dict[str, Any]:
        """Wire form for `model.catalog.refreshed`."""
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "display_name": self.display_name or self.model_id,
            "context_window": self.context_window,
            "max_output": self.max_output,
            "locality": self.locality,
            "quantization": self.quantization,
            "loaded": self.loaded,
            "size_bytes": self.size_bytes,
            "parameter_size": self.parameter_size,
            "source": self.source,
            "available": self.available,
            "window_known": self.window_known,
        }


@dataclass
class _CacheEntry:
    """Cached discovery results for one provider."""

    entries: list[ModelEntry]
    fetched_at: float
    status: str
    error: str | None = None


class ModelCatalog:
    """Discovers, caches and resolves the models each provider offers.

    Parameters
    ----------
    config:
        Supplies the curated `models.known` table and the catalog settings (TTL,
        whether to probe capabilities, whether to allow the offline fallback).
    providers:
        Live adapters to probe. Absent providers simply contribute their configured
        models, so a provider that cannot be built is still represented.
    sleep / now:
        Injected clock, so TTL behaviour is testable without waiting.
    """

    def __init__(self, config: Config, providers: dict[str, Provider] | None = None, *,
                 now: Any = time.time) -> None:
        self.config = config
        self.providers = dict(providers or {})
        self._now = now
        self._cache: dict[str, _CacheEntry] = {}
        self.ttl_s = float(config.catalog.get("refresh_ttl_s", 900) or 900)
        self.offline_fallback = bool(config.catalog.get("offline_fallback", True))
        self.probe_capabilities = bool(config.catalog.get("probe_capabilities", False))
        #: Ask a host that serves the OpenAI model list whether it also serves Ollama's `/api/show`,
        #: which reports a real context length. On by default because the alternative is worse: every
        #: model from such a host lands with an unknown window and an agent cannot be bound to it.
        self.probe_native_windows = bool(config.catalog.get("probe_native_windows", True))

    # ── discovery ───────────────────────────────────────────────────────────

    def refresh(self, provider_id: str | None = None, *, force: bool = False) -> list[ModelEntry]:
        """Discover models, using the cache when it is still fresh.

        Returns the entries for one provider, or for every provider when `provider_id` is
        None. A probe failure never raises: it is recorded as a status and the offline
        table fills the gap, because the app must open on a machine where the local
        provider is not running.
        """
        targets = [provider_id] if provider_id else sorted(
            set(self.config.providers) | set(self.providers)
        )
        collected: list[ModelEntry] = []
        for pid in targets:
            collected.extend(self._refresh_one(pid, force=force))
        return collected

    def _refresh_one(self, provider_id: str, *, force: bool) -> list[ModelEntry]:
        cached = self._cache.get(provider_id)
        if cached is not None and not force and (self._now() - cached.fetched_at) < self.ttl_s:
            return cached.entries

        provider = self.providers.get(provider_id)
        entries: list[ModelEntry] = []
        status = "offline"
        error: str | None = None

        if provider is not None:
            try:
                entries = self._probe(provider_id, provider)
                status = "probed"
            except GatewayError as exc:
                status = "down" if exc.kind is not ErrorKind.AUTH else "misconfigured"
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - discovery must never crash the app
                status = "error"
                error = f"{type(exc).__name__}: {exc}"

        if not entries and self.offline_fallback:
            # No live list: fall back to the curated table plus any aliases the config
            # declares, so the picker is still usable and clearly marked 'declared'.
            entries = self._configured_models(provider_id)
            # `configured` already says the list came from configuration, so it must not also take the
            # `+configured` suffix — that produced the nonsensical `configured+configured`, a label a
            # person learns to ignore and then misses the state behind. Any *other* status
            # (down / misconfigured / error) genuinely has two facts: the probe failed, and the list
            # came from config, so the suffix is added to those.
            status = "configured" if status == "offline" else f"{status}+configured"

        self._cache[provider_id] = _CacheEntry(
            entries=entries, fetched_at=self._now(), status=status, error=error
        )
        return entries

    def _probe(self, provider_id: str, provider: Provider) -> list[ModelEntry]:
        """Ask one provider for its model list, enriching where it can.

        Dispatch uses the provider's declared `kind` rather than its class name, so a
        subclass or a test proxy is still probed by its real protocol. Ollama is the one
        provider that can be enriched cheaply and accurately (`/api/show` reports the real
        context length), so it gets a second call per model.
        """
        kind = getattr(provider, "kind", "") or type(provider).__name__.replace("Provider", "").lower()
        if kind in ("ollama",):
            return self._probe_ollama(provider_id, provider)
        if kind in ("openai", "anthropic"):
            return self._probe_openai_style(provider_id, provider)
        # An unrecognised provider kind contributes nothing from the wire; the offline
        # table fills in, which is better than guessing at an unknown protocol.
        return []

    def _probe_openai_style(self, provider_id: str, provider: Provider) -> list[ModelEntry]:
        """Probe a `/v1/models` endpoint, enriching real windows where the host can report them.

        The response shape is `{"data": [{"id": "..."}]}`, which carries no capability metadata — which
        is why the curated table exists as a fallback. But some hosts *can* report a real window even
        though their listing does not: Ollama's cloud (`https://ollama.com/v1`) serves the OpenAI list
        **and** the native `/api/show`, which reports the true context length. Without asking, every
        model from such a host lands with an unknown window, and hiring an agent on one is refused —
        a working endpoint described as unbindable.

        So the `/api/show` enrichment runs here too, guarded: it is tried only for hosts that look like
        Ollama (the probe is cheap and the failure is caught), and a host that does not implement it
        simply keeps the unenriched entry. Nothing is invented — an unread window stays unknown.
        """
        base = getattr(provider, "base_url", "").rstrip("/")
        endpoint = f"{base}/models"
        headers = provider._headers() if hasattr(provider, "_headers") else {}  # type: ignore[attr-defined]
        transport = provider.transport  # type: ignore[attr-defined]
        response = transport.get_json(endpoint, headers, provider_id=provider_id)
        data = response.json()
        raw_models = data.get("data")
        if not isinstance(raw_models, list):
            raise GatewayError(
                ErrorKind.BAD_RESPONSE,
                "model list response had no 'data' array",
                provider_id=provider_id,
            )
        enrich_windows = bool(getattr(self, "probe_native_windows", True)) and _looks_like_ollama(base)
        entries: list[ModelEntry] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            entry = self._enrich(provider_id, model_id, {
                "display_name": item.get("name") or model_id,
                "size_bytes": item.get("size") if isinstance(item.get("size"), int) else None,
            })
            if enrich_windows:
                # `/api/show` lives at the host root, not under the API prefix: the base may be
                # `https://ollama.com/v1`, but the native endpoint is `https://ollama.com/api/show`.
                origin = _origin_of(base)
                probed = self._probe_ollama_show(transport, origin, headers, provider_id, model_id)
                if probed is not None and probed[0] is not None:
                    entry.context_window = probed[0]
                    entry.source = probed[1]
            entries.append(entry)
        return entries

    def _probe_ollama(self, provider_id: str, provider: Provider) -> list[ModelEntry]:
        """Probe `/api/tags`, then `/api/show` for each model's real limits.

        `/api/show` returns the model's `context_length` in its `model_info` and the
        parameter size in `details`, which is what makes local models *probed* rather
        than assumed — and the local concurrency cap depends on knowing the real size.
        """
        base = getattr(provider, "base_url", "").rstrip("/")
        headers = provider._headers() if hasattr(provider, "_headers") else {}  # type: ignore[attr-defined]
        transport = provider.transport  # type: ignore[attr-defined]

        response = transport.get_json(f"{base}/api/tags", headers, provider_id=provider_id)
        data = response.json()
        raw_models = data.get("models")
        if not isinstance(raw_models, list):
            raise GatewayError(
                ErrorKind.BAD_RESPONSE,
                "ollama /api/tags had no 'models' array",
                provider_id=provider_id,
            )

        entries: list[ModelEntry] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("name") or "").strip()
            if not model_id:
                continue
            details = item.get("details") or {}
            entry = self._enrich(provider_id, model_id, {
                "display_name": model_id,
                "size_bytes": item.get("size") if isinstance(item.get("size"), int) else None,
                "quantization": details.get("quantization_level"),
                "parameter_size": details.get("parameter_size"),
                "locality": "local",
            })
            # A second call per model gives the real window. It is cheap locally and
            # worth it: an assumed window is the failure we are trying to avoid.
            probed = self._probe_ollama_show(transport, base, headers, provider_id, model_id)
            if probed is not None:
                window, source = probed
                if window is not None:
                    entry.context_window = window
                    entry.source = source
            entries.append(entry)
        return entries

    @staticmethod
    def _probe_ollama_show(transport: Any, base: str, headers: dict[str, str],
                           provider_id: str, model_id: str) -> tuple[int | None, str] | None:
        """Read one model's real context length from `/api/show`.

        Returns ``(window, source)`` or None when the call failed. A failure here must not
        fail the whole discovery — one unreadable model should not remove every model.
        """
        import json as _json

        try:
            body = _json.dumps({"model": model_id}).encode("utf-8")
            response = transport.post_json(f"{base}/api/show", headers,
                                           {"model": model_id}, provider_id=provider_id)
            data = response.json()
        except (GatewayError, TypeError):
            return None
        info = data.get("model_info") or {}
        # Ollama keys the context length by architecture, e.g. "qwen2.context_length",
        # so the suffix is what we match on rather than a fixed key.
        window: int | None = None
        for key, value in info.items():
            if str(key).endswith(".context_length") and isinstance(value, int) and value > 0:
                window = int(value)
                break
        if window is None and isinstance(data.get("context_length"), int):
            window = int(data["context_length"])
        return (window, "probed")

    # ── enrichment ──────────────────────────────────────────────────────────

    def _enrich(self, provider_id: str, model_id: str, overrides: dict[str, Any]) -> ModelEntry:
        """Attach what we know about a model, marking where each fact came from.

        Order: a probed value from the provider wins; otherwise the curated config table;
        otherwise the limits stay None and the source is `assumed`.
        """
        spec = self.config.known_models.get(model_id)
        provider_spec = self.config.providers.get(provider_id)
        entry = ModelEntry(
            provider_id=provider_id,
            model_id=model_id,
            display_name=overrides.get("display_name") or model_id,
            context_window=None,
            max_output=None,
            locality=overrides.get("locality") or (spec.locality if spec else "cloud"),
            quantization=overrides.get("quantization"),
            size_bytes=overrides.get("size_bytes"),
            parameter_size=overrides.get("parameter_size"),
            source="assumed",
        )
        if spec is not None and spec.context_window is not None:
            entry.context_window = spec.context_window
            entry.max_output = spec.max_output
            entry.source = spec.source if spec.source != "assumed" else "declared"
            entry.locality = spec.locality
        # A cloud endpoint that declares nothing still benefits from the provider's
        # locality classification (a self-hosted gateway on a private IP is local).
        if provider_spec is not None and _is_local_url(provider_spec.base_url):
            entry.locality = "local"
        return entry

    def _configured_models(self, provider_id: str) -> list[ModelEntry]:
        """Models implied by configuration: this provider's aliases plus the curated models that
        belong to it.

        Scoping matters. An earlier version offered every curated cloud model under every cloud
        provider, so the picker would present `gpt-4o` on Anthropic — a model that provider cannot
        serve. A curated model belongs to a provider when the provider declares it as an alias, when
        the model's id or its prefix names that provider, or when the provider is local and the model
        is local.
        """
        entries: list[ModelEntry] = []
        seen: set[str] = set()
        provider_spec = self.config.providers.get(provider_id)
        aliases = provider_spec.model_aliases if provider_spec else {}
        is_local = bool(provider_spec and _is_local_url(provider_spec.base_url))

        # The provider's own declared aliases first: these are unambiguous.
        for alias, resolved in sorted(aliases.items()):
            if resolved in seen:
                continue
            seen.add(resolved)
            entries.append(self._enrich(provider_id, resolved,
                                        {"display_name": f"{alias} → {resolved}"}))

        # Then curated models that plausibly belong to this provider.
        for model_id, spec in sorted(self.config.known_models.items()):
            if model_id in seen:
                continue
            if spec.locality != ("local" if is_local else "cloud"):
                continue
            if not self._belongs_to(model_id, provider_id, spec):
                continue
            seen.add(model_id)
            entries.append(self._enrich(provider_id, model_id, {}))
        return entries

    @staticmethod
    def _belongs_to(model_id: str, provider_id: str, spec: ModelSpec) -> bool:
        """Whether a curated model is plausibly served by a given provider.

        The heuristic is deliberately conservative: a model is offered only when its id or its
        family prefix names the provider. Offering more would be listing models that provider cannot
        serve, which is worse than offering fewer — a picker that suggests an invalid model wastes
        the Owner's time and then fails at call time.
        """
        lowered = model_id.lower()
        provider = provider_id.lower()
        # Direct naming, e.g. "claude-..." on anthropic, "gpt-..." on openai, "deepseek-..." on
        # deepseek.
        if provider in lowered:
            return True
        # A local provider may serve any local model: the Owner hosts them itself.
        if spec.locality == "local":
            return True
        # A shared, vendor-agnostic id is served wherever the alias declares it, which is handled
        # by the alias path above; a curated entry with no provider affinity is not offered blindly.
        return False

    # ── public surface ──────────────────────────────────────────────────────

    def list_models(self, *, provider_id: str | None = None, refresh: bool = False,
                    locality: str | None = None, only_known_windows: bool = False) -> list[ModelEntry]:
        """List models, optionally filtered — the picker's data source.

        `only_known_windows=True` is what the "bind an agent" path uses: it hides models
        that cannot be safely bound rather than letting the Owner pick one and hit a
        refusal later.
        """
        entries = self.refresh(provider_id, force=refresh)
        if locality:
            entries = [e for e in entries if e.locality == locality]
        if only_known_windows:
            entries = [e for e in entries if e.window_known]
        return sorted(entries, key=lambda e: (e.locality, e.provider_id, e.model_id))

    def resolve(self, provider_id: str, model: str) -> ModelEntry | None:
        """Find one model, expanding an alias first.

        Returns None when the model is not in any list — the caller decides whether that
        is fatal (binding to an unknown model) or merely informative.
        """
        resolved = self.config.alias(provider_id, model)
        for entry in self.refresh(provider_id):
            if entry.model_id == resolved or entry.model_id == model:
                return entry
        # Not in the list (provider down, or a model the Owner typed): still describe it
        # from configuration so binding can report a window problem precisely.
        spec = self.config.known_models.get(resolved)
        if spec is None and not provider_id:
            return None
        return self._enrich(provider_id, resolved, {})

    def model_spec(self, provider_id: str, model: str) -> ModelSpec:
        """Return the limits for a model, never inventing a window."""
        entry = self.resolve(provider_id, model)
        if entry is None:
            return ModelSpec(model_id=model, context_window=None, source="assumed")
        return ModelSpec(
            model_id=entry.model_id,
            context_window=entry.context_window,
            max_output=entry.max_output,
            locality=entry.locality,
            provider_id=entry.provider_id,
            source=entry.source,
        )

    def status(self) -> dict[str, Any]:
        """Per-provider discovery status, for the resources view and diagnostics.

        `status` values are deliberately distinct: `probed` (live list), `down`
        (unreachable), `misconfigured` (bad key), `configured` (offline table only).
        """
        return {
            pid: {
                "status": entry.status,
                "count": len(entry.entries),
                "age_s": round(self._now() - entry.fetched_at, 1),
                "error": entry.error,
            }
            for pid, entry in sorted(self._cache.items())
        }

    def invalidate(self, provider_id: str | None = None) -> None:
        """Drop cached discovery, forcing a real probe on the next read."""
        if provider_id is None:
            self._cache.clear()
        else:
            self._cache.pop(provider_id, None)

    def local_models(self) -> list[ModelEntry]:
        """Every known local model — used to size the scheduler's memory accounting."""
        return [e for e in self.list_models() if e.locality == "local"]


def _origin_of(url: str) -> str:
    """The scheme and host of a URL, dropping any path.

    Needed because a provider's *base* carries an API prefix (`https://ollama.com/v1`) while a native
    endpoint sits at the root (`https://ollama.com/api/show`). Building the native URL by appending to
    the base would produce `/v1/api/show`, which does not exist.
    """
    text = (url or "").strip()
    for scheme in ("https://", "http://"):
        if text.lower().startswith(scheme):
            rest = text[len(scheme):]
            host = rest.split("/", 1)[0]
            return f"{scheme}{host}"
    return text


def _looks_like_ollama(url: str) -> bool:
    """True when a base URL is likely an Ollama host, so the native `/api/show` probe is worth trying.

    Deliberately narrow, because this runs once per model: a host that is not Ollama would cost one
    wasted round trip per model on every refresh. Two shapes qualify:

    - the URL names Ollama (`https://ollama.com/v1`, the cloud endpoint that serves an OpenAI-compatible
      list *and* a native `/api/show`);
    - the URL is local, which is where a self-hosted Ollama lives even when it was configured through
      the OpenAI-compatible adapter.

    A well-known cloud host that is neither — OpenAI, Groq, Anthropic — is skipped, and its models keep
    an unknown window unless `models.known` declares one. That is the honest outcome: we do not invent
    a window, and we do not spend N requests learning nothing.
    """
    lowered = (url or "").lower()
    return "ollama" in lowered or _is_local_url(url)


def _is_local_url(url: str) -> bool:
    """True when a base URL points at this machine or a private network."""
    lowered = (url or "").lower()
    if any(m in lowered for m in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal")):
        return True
    return lowered.startswith((
        "http://10.", "http://192.168.", "http://172.16.", "http://172.17.", "http://172.18.",
        "http://172.19.", "http://172.2", "http://172.30.", "http://172.31.",
    ))
