#!/usr/bin/env python3
"""registry.py — build provider adapters from configuration.

WHY THIS EXISTS
---------------
Something has to turn a `credentials.json` provider entry into a live adapter, and that
mapping is the one place where config shape meets code shape. Keeping it here means
adding a provider kind is a two-line change in one file rather than a hunt through the
codebase.

DESIGN
------
- **Unknown kind is a hard error naming the supported kinds.** A typo in `kind` must not
  silently fall back to a default adapter that then fails at request time with a
  confusing protocol error.
- **A cloud provider with no resolvable key is a load error, not a runtime surprise.**
  Failing at startup, with the variable name, is far easier to fix than a 401 three
  nodes into a run.
- **Adapters are constructed without network I/O.** Discovery is a separate, explicit
  step (`catalog.py`), so building the registry never blocks on a provider being up.
- **Local providers are marked local** so the scheduler's memory accounting is correct
  from the first construction, not inferred later.

Usage:
    providers, skipped = build_providers(config)
    providers["ollama"].complete(request)
"""

from __future__ import annotations

from typing import Any

from ..config import Config, ConfigError, ProviderConfig
from .anthropic import AnthropicProvider
from .base import Provider
from .fake import FakeProvider
from .ollama import OllamaProvider
from .openai import OpenAICompatibleProvider

__all__ = ["build_provider", "build_providers", "SUPPORTED_KINDS"]

SUPPORTED_KINDS = ("openai", "anthropic", "ollama", "fake")


def build_provider(spec: ProviderConfig, *, transport: Any = None) -> Provider:
    """Construct one adapter from its configuration entry.

    Parameters
    ----------
    spec:
        The provider entry from `credentials.json`.
    transport:
        Optional shared :class:`HttpTransport`, so tests can inject a fake one and all
        providers share a retry ledger.

    Raises
    ------
    ConfigError
        On an unsupported kind, or a cloud provider whose key cannot be resolved.
    """
    kind = spec.kind.lower()
    if kind not in SUPPORTED_KINDS:
        raise ConfigError(
            f"provider {spec.id!r} has unsupported kind {kind!r}; "
            f"supported: {', '.join(SUPPORTED_KINDS)}"
        )

    api_key = spec.resolve_key() if kind != "fake" else "fake"
    # A local endpoint legitimately needs no key. A cloud one without a key is a
    # configuration error we can name precisely.
    if kind in ("openai", "anthropic") and not api_key and _looks_cloud(spec.base_url):
        raise ConfigError(
            f"provider {spec.id!r} ({kind}) has no API key. "
            + (f"Set the ${spec.api_key_env} environment variable." if spec.api_key_env
               else "Set api_key_env (preferred) or api_key in credentials.json.")
        )

    common: dict[str, Any] = {
        "provider_id": spec.id,
        "base_url": spec.base_url,
        "api_key": api_key,
        "timeout_s": spec.timeout_s,
        "max_retries": spec.max_retries,
    }
    # Custom headers are a property of the *endpoint*, not of a provider kind, so they go to every
    # adapter rather than only the OpenAI-compatible one — otherwise an Ollama behind a gateway or a
    # proxied Anthropic endpoint would silently drop them and fail with a confusing 401.
    if spec.extra_headers:
        common["extra_headers"] = dict(spec.extra_headers)
    if transport is not None:
        common["transport"] = transport

    if kind == "fake":
        return FakeProvider(provider_id=spec.id)
    if kind == "openai":
        return OpenAICompatibleProvider(**common, locality=_locality(spec.base_url))
    if kind == "anthropic":
        return AnthropicProvider(**common, api_version=spec.api_version or "2023-06-01")
    # Ollama is local by definition, so it needs no locality argument.
    return OllamaProvider(**common)


def build_providers(config: Config, *, transport: Any = None,
                    strict: bool = False) -> tuple[dict[str, Provider], list[str]]:
    """Construct every configured provider, reporting which ones failed.

    Returns
    -------
    tuple
        ``(providers, skipped)`` where `skipped` names each provider that could not be
        built and why. The caller can surface that — silently dropping a provider the
        Owner configured would leave them wondering why an agent has no model.

    Raises
    ------
    ConfigError
        When no provider can be built (an org with no providers can do nothing), or when
        `strict=True` and any provider failed.
    """
    providers: dict[str, Provider] = {}
    skipped: list[str] = []
    for provider_id, spec in config.providers.items():
        try:
            providers[provider_id] = build_provider(spec, transport=transport)
        except ConfigError as exc:
            skipped.append(f"{provider_id}: {exc}")
    if not providers:
        raise ConfigError(
            "no provider could be constructed:\n  " + "\n  ".join(skipped)
            + "\nCheck the 'providers' block in credentials.json."
        )
    if strict and skipped:
        raise ConfigError(
            "some providers could not be constructed (strict mode):\n  "
            + "\n  ".join(skipped)
        )
    return providers, skipped


def _looks_cloud(base_url: str) -> bool:
    """True when the URL is not a loopback/localhost address.

    Used to decide whether a missing key is an error or expected. A private-network host
    is treated as local, because a self-hosted gateway may legitimately be keyless.
    """
    lowered = base_url.lower()
    local_markers = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal")
    if any(marker in lowered for marker in local_markers):
        return False
    private_prefixes = ("http://10.", "http://192.168.", "http://172.16.", "http://172.17.",
                        "http://172.18.", "http://172.19.", "http://172.2", "http://172.30.",
                        "http://172.31.")
    if lowered.startswith(private_prefixes):
        return False
    return lowered.startswith("http")


def _locality(base_url: str) -> str:
    """Classify an endpoint as local or cloud for the scheduler's memory accounting."""
    return "cloud" if _looks_cloud(base_url) else "local"
