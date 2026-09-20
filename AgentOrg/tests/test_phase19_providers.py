#!/usr/bin/env python3
"""Phase 19 tests — configuring providers, and the one operation that can destroy a config.

Adding a provider from a UI means *writing the credentials file*. That is the only operation in this
project where a bug destroys something the user cannot re-derive, so these tests are mostly about the
ways it can go wrong:

1. **Merge, never replace.** A naive write drops every other provider, the model windows and the whole
   policy block — the first time the user adds an endpoint.
2. **Never briefly world-readable.** A file that is written before its mode is set is a leaked key.
3. **Never invent a path.** A typo must not create a second credentials file the engine never reads.
4. **Never return a secret.** The console shows whether a key is *present*; the value stays in the file.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import ConfigError, ProviderConfig, load, write_provider
from engine.providers.registry import build_provider


# ── extra headers ────────────────────────────────────────────────────────────


def test_a_provider_carries_custom_headers_by_default_empty():
    assert ProviderConfig(id="x", kind="openai", base_url="https://x/v1").extra_headers == {}


def test_custom_headers_reach_every_adapter_kind():
    """A gateway header is a property of the endpoint, not of the protocol dialect.

    Sending it only to the OpenAI-compatible adapter would mean an Ollama behind a gateway, or a
    proxied Anthropic endpoint, silently dropped it — and then failed with a confusing 401.
    """
    cases = (
        ("openai", "https://gw.example.com/v1", "k" * 20),
        ("anthropic", "https://api.anthropic.com", "sk-ant-" + "k" * 20),
        ("ollama", "https://gw.example.com", None),
    )
    for kind, base, key in cases:
        spec = ProviderConfig(id=kind, kind=kind, base_url=base, api_key=key,
                              extra_headers={"X-Gateway": "token-123"})
        assert build_provider(spec)._headers().get("X-Gateway") == "token-123", kind


def test_custom_headers_cannot_clobber_the_protocol_headers():
    """`anthropic-version` and the auth header are set by the adapter, not by the user."""
    spec = ProviderConfig(id="a", kind="anthropic", base_url="https://api.anthropic.com",
                          api_key="sk-ant-" + "k" * 20,
                          extra_headers={"anthropic-version": "evil", "x-api-key": "evil"})
    headers = build_provider(spec)._headers()
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["x-api-key"] == "sk-ant-" + "k" * 20


def test_a_header_value_never_appears_in_a_repr():
    """A log line, a screenshot or a crash report must not carry a credential."""
    spec = ProviderConfig(id="gw", kind="openai", base_url="https://gw/v1",
                          extra_headers={"X-Api-Key": "super-secret-value"})
    assert "super-secret-value" not in repr(spec)
    # The *name* is still shown, because which headers are set is genuinely useful.
    assert "X-Api-Key" in repr(spec)


def test_headers_load_from_the_config_document(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "providers": {"gw": {"kind": "openai", "base_url": "https://gw/v1",
                             "extra_headers": {"X-Tenant": "acme"}}},
        "defaults": {"provider": "gw", "model": "m"},
    }))
    config = load(path, warn=False)
    assert config.providers["gw"].extra_headers == {"X-Tenant": "acme"}


def test_a_non_object_headers_field_is_refused(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "providers": {"gw": {"kind": "openai", "base_url": "https://gw/v1",
                             "extra_headers": ["not", "an", "object"]}},
        "defaults": {"provider": "gw", "model": "m"},
    }))
    with pytest.raises(ConfigError, match="extra_headers"):
        load(path, warn=False)


# ── the credentials writer ───────────────────────────────────────────────────


def _creds(tmp_path, mode=0o600):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "version": "1.0.0",
        "providers": {"ollama": {"kind": "ollama", "base_url": "http://localhost:11434"}},
        "models": {"known": {"qwen2.5-coder:7b": {"context_window": 32768}}},
        "policy": {"default_autonomy": {"R-REWORK": "auto"}},
        "defaults": {"provider": "ollama", "model": "qwen2.5-coder:7b"},
    }, indent=2))
    os.chmod(path, mode)
    return path


def test_write_provider_adds_one_entry(tmp_path):
    path = _creds(tmp_path)
    write_provider(path, {"kind": "openai", "base_url": "https://api.groq.com/openai/v1",
                          "api_key": "gsk_" + "x" * 20}, provider_id="groq")
    document = json.loads(path.read_text())
    assert "groq" in document["providers"]
    assert document["providers"]["groq"]["base_url"] == "https://api.groq.com/openai/v1"


def test_write_provider_merges_rather_than_replaces(tmp_path):
    """The whole point: everything that was not edited must survive.

    A naive write would drop the other provider, the model windows and the policy block — the user's
    existing configuration, gone the first time they added an endpoint.
    """
    path = _creds(tmp_path)
    write_provider(path, {"kind": "openai", "base_url": "https://x/v1"}, provider_id="new")
    document = json.loads(path.read_text())
    assert "ollama" in document["providers"], "the existing provider must survive"
    assert document["models"]["known"]["qwen2.5-coder:7b"]["context_window"] == 32768
    assert document["policy"]["default_autonomy"]["R-REWORK"] == "auto"
    assert document["defaults"]["provider"] == "ollama"


def test_write_provider_strips_a_duplicated_id(tmp_path):
    """The id is the map key; carrying it inside the entry as well is a second source of truth."""
    path = _creds(tmp_path)
    write_provider(path, {"id": "dup", "kind": "openai", "base_url": "https://x/v1"},
                   provider_id="dup")
    assert "id" not in json.loads(path.read_text())["providers"]["dup"]


def test_write_provider_replaces_an_existing_entry(tmp_path):
    path = _creds(tmp_path)
    write_provider(path, {"kind": "openai", "base_url": "https://new/v1"}, provider_id="ollama")
    document = json.loads(path.read_text())
    assert document["providers"]["ollama"]["base_url"] == "https://new/v1"
    assert document["providers"]["ollama"]["kind"] == "openai"


def test_write_provider_removes_an_entry(tmp_path):
    path = _creds(tmp_path)
    write_provider(path, {}, provider_id="ollama", remove=True)
    assert "ollama" not in json.loads(path.read_text())["providers"]


def test_write_provider_removing_an_absent_entry_is_not_an_error(tmp_path):
    path = _creds(tmp_path)
    write_provider(path, {}, provider_id="ghost", remove=True)
    assert "ollama" in json.loads(path.read_text())["providers"]


def _reviewer_creds(tmp_path, reviewer: dict) -> pathlib.Path:
    """Two providers with `defaults.reviewer` naming the one that is about to go."""
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "version": "1.0.0",
        "providers": {
            "only-one": {"kind": "ollama", "base_url": "http://localhost:11434"},
            "keeper": {"kind": "ollama", "base_url": "http://localhost:11434"},
        },
        "models": {"known": {"m": {"context_window": 32768}}},
        "defaults": {"provider": "only-one", "model": "m", "reviewer": reviewer},
    }, indent=2))
    os.chmod(path, 0o600)
    return path


def test_removing_a_provider_prunes_the_reviewer_reference_too(tmp_path):
    """`defaults.reviewer.provider` is a third place a provider is named, and it dangled.

    The writer pruned `per_provider_limits[pid]` and `defaults.provider`, so a removal left the
    reviewer pointing at an endpoint that no longer existed. The reviewer sub-object is otherwise
    untouched — its `model` still means something without its provider — which is the minimal fix:
    removing the reference, not the setting.
    """
    path = _reviewer_creds(tmp_path, {"provider": "only-one", "model": "m"})
    write_provider(path, {}, provider_id="only-one", remove=True)
    document = json.loads(path.read_text())
    assert "provider" not in document["defaults"]
    assert "provider" not in document["defaults"]["reviewer"]
    assert document["defaults"]["reviewer"]["model"] == "m"


def test_an_emptied_reviewer_sub_object_is_dropped(tmp_path):
    """A reviewer sub-object left with nothing in it is a key with no content; `set_defaults` drops
    one, so the removal-writer does too rather than leaving an empty object to interpret."""
    path = _reviewer_creds(tmp_path, {"provider": "only-one"})
    write_provider(path, {}, provider_id="only-one", remove=True)
    assert "reviewer" not in json.loads(path.read_text())["defaults"]


def test_a_reviewer_left_without_a_provider_still_loads(tmp_path):
    """**What the loader does with the pruned sub-object**, verified rather than assumed.

    A reviewer left with a model and no provider is tolerated: `_build_defaults` copies the model into
    `reviewer_model` and leaves `reviewer_provider` empty, and review resolution reads empty as "the
    builders' provider". So what the removal writes resolves to the default pair instead of refusing
    to load — which is why pruning the reference is enough and the model is left alone.
    """
    path = _reviewer_creds(tmp_path, {"provider": "only-one", "model": "m"})
    write_provider(path, {}, provider_id="only-one", remove=True)
    config = load(path, warn=False)
    assert config.default.reviewer_provider == "", "empty means 'use the builders' provider'"
    assert config.default.reviewer_model == "m"
    assert sorted(config.providers) == ["keeper"]


def test_write_provider_sets_mode_0600(tmp_path):
    """A key on disk in a world-readable file is a leaked key."""
    path = _creds(tmp_path, mode=0o644)
    write_provider(path, {"kind": "openai", "base_url": "https://x/v1",
                          "api_key": "gsk_" + "x" * 20}, provider_id="groq")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_provider_refuses_a_missing_file(tmp_path):
    """A typo must not create a second credentials file the engine will never read."""
    with pytest.raises(ConfigError, match="refusing to create"):
        write_provider(tmp_path / "nope.json",
                       {"kind": "openai", "base_url": "https://x/v1"}, provider_id="x")


def test_write_provider_needs_an_id(tmp_path):
    path = _creds(tmp_path)
    with pytest.raises(ConfigError, match="needs an id"):
        write_provider(path, {"kind": "openai", "base_url": "https://x/v1"})


def test_write_provider_refuses_an_unreadable_document(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="cannot read"):
        write_provider(path, {"kind": "openai", "base_url": "https://x/v1"}, provider_id="x")


def test_a_written_provider_loads_back(tmp_path):
    """Round trip: what the writer produces must be what the loader accepts."""
    path = _creds(tmp_path)
    write_provider(path, {"kind": "openai", "base_url": "https://gw/v1",
                          "api_key_env": "GW_KEY", "extra_headers": {"X-Tenant": "acme"},
                          "concurrency": 4}, provider_id="gw")
    config = load(path, warn=False)
    spec = config.providers["gw"]
    assert spec.api_key_env == "GW_KEY"
    assert spec.extra_headers == {"X-Tenant": "acme"}
    assert spec.concurrency == 4


def test_the_writer_leaves_no_temp_file_behind(tmp_path):
    path = _creds(tmp_path)
    write_provider(path, {"kind": "openai", "base_url": "https://x/v1"}, provider_id="groq")
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []


# ── the base URL is the base, not the endpoint ───────────────────────────────
#
# The failure these pin: someone pastes the endpoint their provider's docs show —
# `https://ollama.com/v1/chat/completions` — into the base-URL field. The engine then appends its own
# operation and probes `.../chat/completions/models`, which 404s, and reports the provider as
# unreachable. A correct key against a correct URL, described as a broken configuration.


@pytest.mark.parametrize("kind,pasted,want", [
    # The reported case: an OpenAI-compatible cloud endpoint whose base keeps `/v1`.
    ("openai", "https://ollama.com/v1/chat/completions", "https://ollama.com/v1"),
    ("openai", "https://api.openai.com/v1/chat/completions", "https://api.openai.com/v1"),
    ("openai", "https://x/v1/models", "https://x/v1"),
    # Anthropic's operation *includes* v1, so the base is the bare host.
    ("anthropic", "https://api.anthropic.com/v1/messages", "https://api.anthropic.com"),
    # Ollama's native operation lives under /api.
    ("ollama", "https://ollama.com/api/chat", "https://ollama.com"),
    # Already correct: untouched, and no note.
    ("openai", "https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1"),
    ("openai", "https://ollama.com/v1", "https://ollama.com/v1"),
    ("ollama", "http://localhost:11434", "http://localhost:11434"),
])
def test_a_pasted_endpoint_is_reduced_to_its_base(kind, pasted, want):
    spec = ProviderConfig(id="p", kind=kind, base_url=pasted)
    assert spec.base_url == want


def test_an_already_correct_base_is_not_touched_and_says_nothing():
    """No note means no change was made — the panel shows one only when it corrected something."""
    assert ProviderConfig(id="p", kind="openai",
                          base_url="https://api.groq.com/openai/v1").base_url_note == ""


def test_a_corrected_base_explains_itself():
    """The correction is *told*, not silently applied: the user pasted a real endpoint and should
    learn what happened rather than re-checking a key that was never wrong."""
    note = ProviderConfig(id="p", kind="openai",
                          base_url="https://ollama.com/v1/chat/completions").base_url_note
    assert "https://ollama.com/v1" in note
    assert "chat/completions" in note


def test_the_note_is_derived_and_never_persisted(tmp_path):
    """`base_url_note` describes a load-time adjustment; storing it would freeze an explanation into
    the config and make it reappear on a URL that needs no correction."""
    from engine.config import normalize_base_url

    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "providers": {"p": {"kind": "openai", "base_url": "https://ollama.com/v1/chat/completions"}},
        "defaults": {"provider": "p", "model": "m"},
    }))
    spec = load(path, warn=False).providers["p"]
    assert spec.base_url == "https://ollama.com/v1"
    assert spec.base_url_note  # present in memory

    # Writing the entry back must not carry the note into the document.
    write_provider(path, {"kind": "openai", "base_url": spec.base_url}, provider_id="p")
    stored = json.loads(path.read_text())["providers"]["p"]
    assert "base_url_note" not in stored
    assert normalize_base_url(stored["base_url"])[1] == ""


def test_a_base_is_never_reduced_to_nothing():
    """`https://host/models` is a real base with a trailing operation word; stripping it must not
    leave an unusable empty URL."""
    spec = ProviderConfig(id="p", kind="openai", base_url="https://host/models")
    assert spec.base_url == "https://host"
    assert "://" in spec.base_url


# ── TLS trust ────────────────────────────────────────────────────────────────
#
# The second silent failure: on a python.org macOS build, `ssl.create_default_context()` can return a
# context with **zero** CA certificates, because the path it points at is not shipped. Every HTTPS
# provider then fails identically with CERTIFICATE_VERIFY_FAILED, which reads as "the provider is down"
# when the truth is "this machine has no roots loaded".


def test_the_transport_gets_a_context_that_trusts_something():
    """A context with no CAs makes every HTTPS call fail, and the error blames the provider."""
    from engine.providers.http import HttpTransport, default_ssl_context

    context = default_ssl_context()
    assert context.cert_store_stats().get("x509_ca", 0) > 0, (
        "the resolved TLS context trusts no CAs, so every HTTPS provider will fail with a "
        "certificate error that names the provider rather than the machine")
    assert HttpTransport().ssl_context is not None


def test_a_broken_bundle_is_not_accepted_as_working(tmp_path):
    """An empty bundle must be rejected rather than returned: a context that loads zero CAs would put
    us straight back in the broken state while looking like a successful resolution."""
    from engine.providers.http import default_ssl_context

    empty = tmp_path / "empty.pem"
    empty.write_text("")
    os.environ["SSL_CERT_FILE"] = str(empty)
    try:
        # Falls through the override to the discovered stores rather than failing outright.
        assert default_ssl_context().cert_store_stats().get("x509_ca", 0) > 0
    finally:
        os.environ.pop("SSL_CERT_FILE", None)


def test_an_explicit_bundle_is_honoured(tmp_path):
    """`$SSL_CERT_FILE` is the standard way an operator points at a corporate bundle, so it wins."""
    import ssl

    from engine.providers.http import _CA_CANDIDATES, default_ssl_context

    existing = next((c for c in _CA_CANDIDATES if os.path.isfile(c)), None)
    if existing is None:
        pytest.skip("no system CA bundle on this machine to point at")
    os.environ["SSL_CERT_FILE"] = existing
    try:
        context = default_ssl_context()
        assert isinstance(context, ssl.SSLContext)
        assert context.cert_store_stats().get("x509_ca", 0) > 0
    finally:
        os.environ.pop("SSL_CERT_FILE", None)


# ── redaction catches this key shape ─────────────────────────────────────────
#
# Found while diagnosing the reported failure: the key the user pasted (`<32 hex>.<token>`) matched
# none of the redaction patterns, so a *bare* occurrence — `api_key = <value>`, or a JSON field —
# would survive into `trace.jsonl` unredacted. The existing `Bearer`/`api_key="…"` rules only catch it
# when it happens to be quoted or prefixed a particular way.

_OLLAMA_STYLE_KEY = "0123456789abcdef0123456789abcdef.SYNTHETIC-not-a-real-key"


def test_a_hex_dot_token_key_is_redacted_bare():
    from engine.config import redact

    assert _OLLAMA_STYLE_KEY not in redact(f"api_key = {_OLLAMA_STYLE_KEY}")


def test_a_hex_dot_token_key_is_redacted_in_json():
    from engine.config import redact

    assert _OLLAMA_STYLE_KEY not in redact('{"api_key": "' + _OLLAMA_STYLE_KEY + '"}')


def test_a_hex_dot_token_key_is_redacted_in_a_header():
    from engine.config import redact

    assert _OLLAMA_STYLE_KEY not in redact(f"Authorization: Bearer {_OLLAMA_STYLE_KEY}")


def test_redaction_does_not_mangle_ordinary_prose():
    """Aggressive is not the same as careless: a bare commit hash and a version number must survive,
    or every log line becomes noise nobody reads."""
    from engine.config import redact

    # A bare hex string with no dot is not a key: an ordinary commit hash must survive redaction, or
    # every log line becomes noise nobody reads.
    prose = "commit 0123456789abcdef0123456789abcdef is fine, and 3.14 is a version"
    assert redact(prose) == prose


def test_the_redaction_is_aggressive_enough_for_the_other_known_shapes():
    """A guard on the ordering: adding a pattern must not disarm the ones before it."""
    from engine.config import redact

    for value in ("sk-" + "a" * 32, "sk-ant-" + "a" * 32, "ghp_" + "a" * 32,
                  "AKIA" + "A" * 16):
        assert value not in redact(f"value = {value}"), value
