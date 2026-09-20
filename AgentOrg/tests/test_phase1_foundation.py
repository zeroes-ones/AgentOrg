#!/usr/bin/env python3
"""Phase 1 tests — library pinning, config, protocol, bus, artifacts, versioning, idempotency, resources.

These are the foundations every later phase stands on, so the tests focus on the
*failure* paths: a tampered library, an unsafe config, a malformed frame, a path escape,
a schema from the future, a duplicated effect. A foundation that only works when
everything goes right is not a foundation.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine import artifacts as art
from engine import config as cfgmod
from engine import idempotency as idem
from engine import protocol as proto
from engine import resources as res
from engine import state as statemod
from engine import versioning as ver
from engine.bus import EventBus, load_trace, trace_summary
from engine.library import LibraryError, resolve

EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / "credentials.example.json"


# ── library ──────────────────────────────────────────────────────────────────


def test_library_resolves_and_asserts_capabilities():
    lib = resolve()
    assert lib.files.runner.is_file()
    assert lib.files.schema.is_dir()
    assert lib.capabilities_verified
    # Resolving with no pin compares no content, so the pin facts must stay false. Asserting them
    # here is the point: a single "verified" flag was true on this path, which reads as "hash
    # checked" on a checkout where not one hash was compared.
    assert not lib.pinned
    assert not lib.commit_pinned and not lib.manifest_pinned


def test_library_registers_no_directory_it_never_reads():
    """`workflow/templates`, `.skills-compiled`, `evals/golden` and `evals/tier3-behavioral` serve
    the library's own skill and eval toolchain. This engine opens none of them, so registering them
    would put a path in every diagnostic — and, for a required one, refuse startup over a directory
    no line of code touches."""
    lib = resolve()
    registered = set(lib.files.__dataclass_fields__)
    reported = lib.files.as_dict()
    for unread in ("templates", "compiled", "golden", "behavioral"):
        assert unread not in registered
        assert unread not in reported


def test_library_finds_skills_and_rejects_bad_names():
    lib = resolve()
    assert lib.find_skill("code-reviewer") is not None
    assert lib.find_skill("not-a-real-skill") is None
    # A name that is not a slug must not be used to build a path.
    assert lib.find_skill("../../etc/passwd") is None
    assert lib.find_skill("UPPER") is None


def test_library_missing_root_fails_loudly():
    with pytest.raises(LibraryError, match="not found"):
        resolve(root="/tmp/definitely-not-a-skills-library")


def test_library_manifest_detects_tampering():
    lib = resolve()
    manifest = lib.build_manifest()
    assert manifest, "manifest must not be empty"
    recorded = dict(manifest)
    # A clean manifest verifies — and the flag says which fact was checked.
    matched = resolve(expected_manifest=recorded)
    assert matched.manifest_pinned and matched.pinned
    assert "content pin matched" in matched.verification_summary()
    # Any changed hash is reported, and the offending path is named.
    key = next(iter(recorded))
    tampered = dict(recorded)
    tampered[key] = "0" * 64
    with pytest.raises(LibraryError) as excinfo:
        resolve(expected_manifest=tampered)
    assert key in str(excinfo.value)


def test_library_commit_pin_is_enforced():
    lib = resolve()
    if lib.commit:  # only meaningful for a git checkout
        with pytest.raises(LibraryError, match="commit mismatch"):
            resolve(expected_commit="0" * 40)


# ── config ───────────────────────────────────────────────────────────────────


def _write_cfg(tmp: pathlib.Path, mutate=None) -> pathlib.Path:
    data = json.loads(EXAMPLE.read_text())
    if mutate:
        mutate(data)
    path = tmp / "credentials.json"
    path.write_text(json.dumps(data))
    return path


def test_config_loads_example():
    # Explicit example path, not a bare `load()`: `load()` prefers `./credentials.json`, which is a
    # *local* file a developer may have pointed at only their own providers — so asserting on the
    # example's contents through it made the test pass or fail on whoever ran it. This test is about
    # the shipped example, so it loads the shipped example.
    cfg = cfgmod.load(EXAMPLE)
    assert cfg.providers
    assert cfg.provider("ollama").kind == "ollama"
    assert cfg.provider("anthropic").kind == "anthropic"
    assert cfg.context.compact_at < cfg.context.evict_at < cfg.context.overflow_at


def test_config_unknown_model_has_no_invented_window():
    cfg = cfgmod.load(EXAMPLE)
    spec = cfg.model_spec("no-such-model")
    assert spec.context_window is None
    assert spec.source == "assumed"


def test_config_alias_expansion():
    cfg = cfgmod.load(EXAMPLE)
    assert cfg.alias("anthropic", "claude-sonnet") == "claude-sonnet-4-20250514"


def test_config_env_first_key_resolution(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-from-env-000000000000")
    cfg = cfgmod.load(EXAMPLE)
    assert cfg.provider("openai").resolve_key() == "sk-test-from-env-000000000000"


def test_config_repr_does_not_leak_key():
    cfg = cfgmod.load()
    assert "sk-" not in repr(cfg.provider("openai"))


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda d: d["providers"]["ollama"].__setitem__("kind", "weird"), "unsupported kind"),
        (lambda d: d["providers"]["openai"].pop("base_url"), "missing base_url"),
        (lambda d: d["context"].__setitem__("compact_at", 0.9), "thresholds must satisfy"),
        (lambda d: d["health"].__setitem__("min_samples", 0), "min_samples"),
        (lambda d: d["delegation"].__setitem__("max_depth", 0), "max_depth"),
        (lambda d: d["policy"]["default_autonomy"].__setitem__("R-BOGUS", "auto"), "unknown route class"),
        (lambda d: d["policy"]["default_autonomy"].__setitem__("R-ESCALATE", "auto"), "safety floor"),
        (lambda d: d["budget"].__setitem__("run_max_usd", -1), "must be > 0"),
        (lambda d: d["policy"]["router"].__setitem__("threshold", 1.5), "must be in"),
        (lambda d: d["policy"]["router"].__setitem__("margin", -1), "must be in"),
    ],
)
def test_config_rejects_unsafe_values(tmp_path, mutate, match):
    path = _write_cfg(tmp_path, mutate)
    with pytest.raises(cfgmod.ConfigError, match=match):
        cfgmod.load(path, warn=False)


@pytest.mark.parametrize(
    "mutate, warning_match",
    [
        # A stale pointer, not a typo: these are *left behind* by removing a provider from the
        # console, and refusing to load bricked every command — including `doctor`, which exists to
        # diagnose it. They degrade with a warning instead, which is the whole fix.
        (lambda d: d["concurrency"]["per_provider_limits"].__setitem__("ghost", 1),
         "were pruned"),
        (lambda d: d["defaults"].__setitem__("provider", "ghost"),
         "not a configured provider"),
    ],
)
def test_a_stale_provider_reference_is_recovered_not_fatal(tmp_path, mutate, warning_match):
    """The engine must stay startable when the config carries a dangling provider reference."""
    path = _write_cfg(tmp_path, mutate)
    cfg = cfgmod.load(path, warn=False)  # must NOT raise
    assert any(warning_match in w for w in cfg.raw.get("_warnings") or []), cfg.raw.get("_warnings")
    # And the resulting config is usable: a real provider remains, and the stale entry is gone.
    assert cfg.providers
    assert "ghost" not in cfg.concurrency.per_provider_limits


def test_config_safety_floor_allows_explicit_opt_in(tmp_path):
    def mutate(d):
        d["policy"]["default_autonomy"]["R-ESCALATE"] = "auto"
        d["policy"]["allow_autonomous_escalation"] = True

    cfg = cfgmod.load(_write_cfg(tmp_path, mutate), warn=False)
    assert cfg.policy.default_autonomy["R-ESCALATE"] == "auto"


def test_config_router_coerces_to_dataclass(tmp_path):
    """A raw dict must become a validated RouterConfig, not bypass its checks."""
    cfg = cfgmod.load(_write_cfg(tmp_path, lambda d: d["policy"].update(
        {"router": {"threshold": 0.5, "margin": 0.1}})), warn=False)
    assert isinstance(cfg.policy.router, cfgmod.RouterConfig)
    assert cfg.policy.router.threshold == 0.5


def test_config_missing_file_message_lists_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTORG_CREDENTIALS", str(tmp_path / "nope.json"))
    with pytest.raises(cfgmod.ConfigError, match="no configuration found"):
        cfgmod.load(warn=False)


def test_explicit_config_path_is_not_silently_replaced(tmp_path, monkeypatch):
    """An explicit pointer must fail loudly rather than fall back to the example.

    Falling back would mean running with providers and budgets the operator did not
    choose, which is worse than refusing to start.
    """
    monkeypatch.setenv("AGENTORG_CREDENTIALS", str(tmp_path / "missing.json"))
    with pytest.raises(cfgmod.ConfigError, match="no fallback was attempted"):
        cfgmod.load(warn=False)
    with pytest.raises(cfgmod.ConfigError, match="no configuration found"):
        cfgmod.load(tmp_path / "also-missing.json", warn=False)


def test_permission_warning_at_0644_and_silent_at_0600(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["providers"]["openai"]["api_key"] = "sk-aaaabbbbccccddddeeeeffff"
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(data))

    os.chmod(path, 0o644)
    assert cfgmod.load(path).raw["_warnings"], "0644 must warn"

    os.chmod(path, 0o600)
    assert not cfgmod.load(path).raw["_warnings"], "0600 must not warn"


def test_redaction_covers_key_shapes():
    cases = [
        "key sk-abcdefghijklmnopqrstuvwx",
        "Authorization: Bearer abcdef1234567890xyz",
        'api_key="supersecretvalue123"',
    ]
    for text in cases:
        out = cfgmod.redact(text)
        assert "[REDACTED]" in out
        assert "abcdefghijklmnopqrstuvwx" not in out
    assert cfgmod.redact("nothing secret here") == "nothing secret here"


def test_leak_scan_finds_without_echoing(tmp_path):
    root = tmp_path / ".agent_state"
    root.mkdir()
    (root / "clean.json").write_text('{"ok":true}')
    assert cfgmod.scan_for_leaks(root) == []

    (root / "bad.json").write_text('{"key":"sk-111122223333444455556666"}')
    hits = cfgmod.scan_for_leaks(root)
    assert len(hits) == 1
    assert "sk-111122223333444455556666" not in str(hits), "the secret must not be echoed"


# ── protocol ─────────────────────────────────────────────────────────────────


def test_protocol_round_trip_event_and_command():
    ev = proto.Event(seq=1, type=proto.EventType.NODE_ENTER, node_id="fixer",
                     run_id="run_1", payload={"phase": "DEVELOP"})
    back = proto.decode_event(proto.encode(ev))
    assert back.type_value == "node.enter"
    assert back.node_id == "fixer"
    assert back.payload == {"phase": "DEVELOP"}
    assert back.v == proto.PROTOCOL_VERSION

    cmd = proto.Command(cmd_id="c1", type=proto.CommandType.APPROVE, payload={"gate_id": "g"})
    cb = proto.decode_command(proto.encode(cmd))
    assert cb.cmd_id == "c1"
    assert cb.type_value == "approve"


def test_protocol_covers_every_event_type():
    """Every declared event type must survive an encode/decode cycle."""
    for event_type in proto.EventType:
        ev = proto.Event(seq=1, type=event_type, payload={"k": "v"})
        back = proto.decode_event(proto.encode(ev))
        assert back.type_value == event_type.value


def test_protocol_covers_every_command_type():
    for command_type in proto.CommandType:
        cmd = proto.Command(cmd_id="c", type=command_type)
        assert proto.decode_command(proto.encode(cmd)).type_value == command_type.value


def test_protocol_ack_carries_cmd_id():
    ack = proto.Ack(cmd_id="c9", ok=False, error="nope")
    ev = ack.to_event(seq=3)
    assert ev.payload["cmd_id"] == "c9"
    assert ev.payload["ok"] is False
    assert ev.payload["error"] == "nope"


def test_protocol_unknown_type_is_tolerated():
    ev = proto.decode_event(json.dumps({"v": 1, "seq": 1, "type": "future.event", "payload": {}}))
    assert not ev.is_known()
    assert ev.type_value == "future.event"


@pytest.mark.parametrize(
    "line, match",
    [
        (json.dumps({"v": 1, "type": "x"}), "missing required field 'seq'"),
        (json.dumps({"v": 1, "seq": "one", "type": "x"}), "'seq' must be an integer"),
        (json.dumps({"v": 1, "seq": 1}), "missing required field 'type'"),
        (json.dumps({"v": 99, "seq": 1, "type": "x"}), "protocol v99"),
        ("{not json", "malformed"),
        (json.dumps({"v": 1, "seq": 1, "type": "x", "payload": []}), "must be an object"),
        (json.dumps({"v": 1, "seq": 1, "type": "x", "payload": "s"}), "must be an object"),
        ("", "empty"),
    ],
)
def test_protocol_rejects_malformed_events(line, match):
    with pytest.raises(proto.ProtocolError, match=match):
        proto.decode_event(line)


def test_protocol_command_requires_cmd_id():
    with pytest.raises(proto.ProtocolError, match="cmd_id"):
        proto.decode_command(json.dumps({"v": 1, "type": "start"}))


def test_protocol_parse_stream_tolerates_torn_line():
    lines = [
        proto.encode(proto.Event(seq=1, type="a")),
        '{"torn',
        proto.encode(proto.Event(seq=2, type="b")),
    ]
    assert len(proto.parse_stream(lines)) == 2
    with pytest.raises(proto.ProtocolError):
        proto.parse_stream(lines, strict=True)


def test_protocol_oversized_frame_is_rejected():
    huge = proto.Event(seq=1, type="x", payload={"blob": "z" * (proto.MAX_FRAME_BYTES + 10)})
    with pytest.raises(proto.ProtocolError, match="exceeds"):
        proto.encode(huge)


def test_cmd_ids_are_unique():
    ids = {proto.new_cmd_id() for _ in range(500)}
    assert len(ids) == 500


# ── bus ──────────────────────────────────────────────────────────────────────


def test_bus_orders_numbers_and_writes_trace(tmp_path):
    trace = tmp_path / "trace.jsonl"
    bus = EventBus(run_id="run_1", trace_path=trace)
    for i in range(5):
        bus.emit(proto.EventType.NODE_ENTER, payload={"i": i})
    assert bus.last_seq == 5
    assert len(load_trace(trace)) == 5
    assert trace_summary(trace)["events"] == 5
    bus.close()


def test_bus_degrades_when_the_trace_is_not_writable(tmp_path):
    """A read-only command must not crash because the trace cannot be opened.

    A `goal status` (or `flow`, or `activity`) in a workspace whose `.agent_state/` is not writable
    used to die with a raw `PermissionError` out of the *bus constructor* — the opposite of "it just
    works", and it made the CLI unusable on a read-only checkout. Losing the trace is a smaller
    failure than losing the command, so the bus records the reason and holds events in memory.
    """
    # A path whose parent is a *file*, so the mkdir/open cannot succeed on any platform.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    bus = EventBus(run_id="run_1", trace_path=blocker / "trace.jsonl")
    assert bus.trace_error, "the reason must be recorded, not swallowed"
    event = bus.emit(proto.EventType.NODE_ENTER, payload={"i": 0})
    assert event is not None, "emitting still works; only persistence is lost"
    assert bus.last_seq == 1
    bus.close()


def test_bus_bounds_history_and_counts_drops():
    bus = EventBus(history_size=3)
    for i in range(10):
        bus.emit(proto.EventType.AGENT_LOG, payload={"i": i})
    assert len(bus.history()) == 3
    assert bus.stats()["dropped_from_buffer"] == 7, "drops must be counted, not hidden"
    bus.close()


def test_bus_redacts_at_the_boundary():
    bus = EventBus()
    ev = bus.emit(proto.EventType.AGENT_LOG, payload={"text": "key sk-abcdefghijklmnopqrstuvwx"})
    assert "sk-" not in json.dumps(ev.payload)
    assert "[REDACTED]" in ev.payload["text"]
    bus.close()


def test_bus_disables_a_raising_subscriber_but_keeps_delivering():
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    bus.subscribe(lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.emit(proto.EventType.AGENT_LOG, payload={})
    assert len(seen) == 1, "a bad subscriber must not stop delivery"
    assert any("boom" in v for v in bus.disabled_subscribers().values())
    bus.close()


def test_bus_history_since_seq():
    bus = EventBus()
    for i in range(6):
        bus.emit(proto.EventType.AGENT_LOG, payload={"i": i})
    assert [e.seq for e in bus.history(since_seq=4)] == [5, 6]
    bus.close()


def test_bus_unsubscribe_is_safe():
    bus = EventBus()
    fn = bus.subscribe(lambda ev: None)
    bus.unsubscribe(fn)
    bus.unsubscribe(fn)  # idempotent
    bus.close()


# ── artifacts ────────────────────────────────────────────────────────────────


def test_artifact_write_read_and_hash(tmp_path):
    store = art.ArtifactStore(workspace_root=tmp_path / "ws")
    ref = store.write("src/app.py", "print(1)\n", producer="ag_1", artifact_type="change")
    assert store.read("src/app.py") == "print(1)\n"
    assert store.hash_of("src/app.py") == ref.sha256
    assert store.exists("src/app.py")
    assert not store.exists("missing.py")


def test_artifact_write_is_idempotent_by_content(tmp_path):
    """Identical bytes must yield an identical hash — the review loop depends on it."""
    store = art.ArtifactStore(workspace_root=tmp_path / "ws")
    a = store.write("src/app.py", "same\n")
    b = store.write("src/app.py", "same\n")
    c = store.write("src/app.py", "different\n")
    assert a.sha256 == b.sha256
    assert a.sha256 != c.sha256


@pytest.mark.parametrize(
    "path",
    ["../escape.txt", "src/../../escape.txt", "/etc/passwd", "src/CON.txt", ""],
)
def test_artifact_containment_rejects_unsafe_paths(tmp_path, path):
    store = art.ArtifactStore(workspace_root=tmp_path / "ws")
    with pytest.raises(art.WorkspaceError):
        store.write(path, "x")


def test_artifact_rejects_symlink_escape(tmp_path):
    root = tmp_path / "ws"
    store = art.ArtifactStore(workspace_root=root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(art.WorkspaceError, match="escapes the workspace"):
        store.write("link/pwned.txt", "x")


def test_artifact_rejects_oversized_content(tmp_path):
    store = art.ArtifactStore(workspace_root=tmp_path / "ws")
    with pytest.raises(art.WorkspaceError, match="over the"):
        store.write("big.bin", b"x" * (art.MAX_ARTIFACT_BYTES + 1))


def test_artifact_concurrent_writers_leave_no_temp_files(tmp_path):
    store = art.ArtifactStore(workspace_root=tmp_path / "ws")
    errors: list[Exception] = []

    def worker():
        try:
            store.write("shared.txt", "x" * 2000)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.exists("shared.txt")
    assert not any(".tmp." in f for f in store.list_files())


def test_artifact_append_line_accumulates(tmp_path):
    store = art.ArtifactStore(workspace_root=tmp_path / "ws")
    store.append_line("mailbox.jsonl", '{"m":1}')
    ref = store.append_line("mailbox.jsonl", '{"m":2}')
    assert store.read("mailbox.jsonl").count("\n") == 2
    assert ref.type == "journal"


# ── versioning ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [("1.2.3", (1, 2, 3)), ("v2", (2, 0, 0)), ("1.2.0-beta", (1, 2, 0)), ("3", (3, 0, 0))],
)
def test_parse_version(text, expected):
    assert ver.parse_version(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "1.x.3", "1.2.3.4"])
def test_parse_version_rejects_garbage(bad):
    with pytest.raises(ver.SchemaVersionError):
        ver.parse_version(bad)


def test_version_compatibility_is_major_based():
    assert ver.is_compatible("1.9.0", "1.0.0")
    assert not ver.is_compatible("2.0.0", "1.0.0")


def test_registry_refuses_newer_schema(tmp_path):
    reg = ver.default_registry({"run_state": "1.0.0"})
    path = tmp_path / "run_state.json"
    path.write_text(json.dumps({"run_state_version": "9.0.0"}))
    with pytest.raises(ver.SchemaTooNewError):
        reg.load(path, kind="run_state")
    with pytest.raises(ver.SchemaTooNewError):
        reg.load(path, kind="run_state", migrate=False)


def test_registry_migrates_forward_and_records_history(tmp_path):
    reg = ver.default_registry({"run_state": "1.2.0"})
    reg.register("run_state", "1.0.0", "1.1.0", lambda d: {**d, "stage": "new"})
    reg.register("run_state", "1.1.0", "1.2.0", lambda d: {**d, "attempts": []})

    path = tmp_path / "run_state.json"
    path.write_text(json.dumps({"run_state_version": "1.0.0", "run_id": "r"}))
    doc = reg.load(path, kind="run_state")

    assert doc["run_state_version"] == "1.2.0"
    assert doc["stage"] == "new" and doc["attempts"] == [], "migrated fields must be visible"
    assert doc["run_id"] == "r", "original data must survive migration"
    assert doc["_migrations"][0]["steps"] == ["1.0.0->1.1.0", "1.1.0->1.2.0"]


def test_registry_reports_missing_migration_path(tmp_path):
    reg = ver.Registry({"run_state": "3.0.0"})
    path = tmp_path / "run_state.json"
    path.write_text(json.dumps({"run_state_version": "1.0.0"}))
    with pytest.raises(ver.SchemaError, match="no migration path"):
        reg.load(path, kind="run_state")


def test_registry_save_stamps_current_version(tmp_path):
    reg = ver.default_registry({"run_state": "1.0.0"})
    path = reg.save(tmp_path / "run_state.json", {"run_id": "r"}, kind="run_state")
    assert json.loads(path.read_text())["run_state_version"] == "1.0.0"


def test_registry_tolerates_additive_minor_ahead(tmp_path):
    reg = ver.default_registry({"run_state": "1.2.0"})
    path = tmp_path / "run_state.json"
    path.write_text(json.dumps({"run_state_version": "1.2.5", "run_id": "r"}))
    assert reg.load(path, kind="run_state")["run_state_version"] == "1.2.5"


# ── idempotency ──────────────────────────────────────────────────────────────


def test_effect_key_is_deterministic_and_attempt_sensitive():
    a = idem.effect_key(run_id="r", node_id="n", attempt=1, inputs_hash="h", effect="e", target="t")
    b = idem.effect_key(run_id="r", node_id="n", attempt=1, inputs_hash="h", effect="e", target="t")
    c = idem.effect_key(run_id="r", node_id="n", attempt=2, inputs_hash="h", effect="e", target="t")
    assert a == b
    assert a != c, "a deliberate rework must not look like an accidental retry"


def test_journal_replays_completed_effect(tmp_path):
    journal = idem.EffectJournal(path=tmp_path / "effects.jsonl")
    applications: list[int] = []

    def run(attempt: int):
        with journal.effect("write", run_id="r", node_id="n", attempt=attempt, inputs_hash="h") as rec:
            if rec.replayed:
                return rec.outcome
            applications.append(attempt)
            rec.record({"ok": True})
            return rec.outcome

    assert run(1) is idem.Outcome.APPLY
    assert run(1) is idem.Outcome.REPLAY, "a retry of the same attempt must replay"
    assert applications == [1], "the side effect must not be applied twice"
    journal.close()


def test_journal_survives_reopen_without_duplicating(tmp_path):
    path = tmp_path / "effects.jsonl"
    first = idem.EffectJournal(path=path)
    with first.effect("write", run_id="r", node_id="n", attempt=1, inputs_hash="h") as rec:
        rec.record({"sha": "abc"})
    first.close()

    second = idem.EffectJournal(path=path)
    applied: list[int] = []
    with second.effect("write", run_id="r", node_id="n", attempt=1, inputs_hash="h") as rec:
        if not rec.replayed:
            applied.append(1)
    assert applied == [], "resume must not re-apply a completed effect"
    assert rec.result == {"sha": "abc"}, "the recorded result must be available on replay"
    second.close()


def test_journal_marks_failed_effect_as_retryable(tmp_path):
    path = tmp_path / "effects.jsonl"
    journal = idem.EffectJournal(path=path)
    with pytest.raises(RuntimeError):
        with journal.effect("spend", run_id="r", node_id="n", attempt=1, inputs_hash="h") as rec:
            raise RuntimeError("provider died")
    assert journal.counts()["failed"] == 1
    journal.close()

    reopened = idem.EffectJournal(path=path)
    with reopened.effect("spend", run_id="r", node_id="n", attempt=1, inputs_hash="h") as rec:
        assert rec.outcome is idem.Outcome.RETRY
    reopened.close()


def test_journal_records_in_flight_before_applying(tmp_path):
    """A crash during an effect must be detectable, not silently 'not started'."""
    path = tmp_path / "effects.jsonl"
    journal = idem.EffectJournal(path=path)
    with journal.effect("spend", run_id="r", node_id="n", attempt=1, inputs_hash="h"):
        # Reserved on entry; the effect is now in flight.
        assert len(journal.in_flight()) == 1
    journal.close()


def test_journal_tolerates_torn_final_line(tmp_path):
    path = tmp_path / "effects.jsonl"
    good = {"key": "k1", "effect": "e", "run_id": "r", "node_id": "n",
            "attempt": 1, "state": "completed", "result": {"ok": 1}}
    path.write_text(json.dumps(good) + "\n" + '{"torn":')
    journal = idem.EffectJournal(path=path)
    assert journal.lookup("k1") is not None
    journal.close()


def test_journal_distinguishes_targets(tmp_path):
    journal = idem.EffectJournal(path=tmp_path / "effects.jsonl")
    with journal.effect("write", run_id="r", node_id="n", attempt=1,
                        inputs_hash="h", target="a.py") as rec:
        assert rec.outcome is idem.Outcome.APPLY
        rec.record({})
    with journal.effect("write", run_id="r", node_id="n", attempt=1,
                        inputs_hash="h", target="b.py") as rec:
        assert rec.outcome is idem.Outcome.APPLY, "a different target is a different effect"
    journal.close()


# ── resources ────────────────────────────────────────────────────────────────


def test_detect_reports_plausible_machine():
    caps = res.detect()
    assert caps.cpu_count >= 1
    assert caps.physical_memory_bytes > 0
    assert caps.machine


def test_local_model_footprint_scales_with_size():
    small = res.estimate_local_model_footprint("qwen2.5:1b")
    big = res.estimate_local_model_footprint("llama3.1:70b")
    assert small < big
    # An unrecognised model is assumed mid-size, not tiny — under-estimating causes swap.
    assert res.estimate_local_model_footprint("mystery") >= 4.0
    # Quantisation must reduce the estimate.
    assert res.estimate_local_model_footprint("qwen2.5:32b-q4") < res.estimate_local_model_footprint("qwen2.5:32b")


def test_ceiling_respects_cpu_headroom():
    caps = res.detect()
    result = res.derive_ceiling(caps, cpu_headroom=1)
    assert result["ceiling"] <= caps.cpu_count
    assert result["cpu_bound"] == max(1, caps.cpu_count - 1)


def test_ceiling_shrinks_with_local_models():
    caps = res.detect()
    cpu_only = res.derive_ceiling(caps)
    with_local = res.derive_ceiling(caps, local_models_in_use=2, local_model_ids=["qwen2.5-coder:7b"])
    assert with_local["ceiling"] <= cpu_only["ceiling"]
    assert "memory" in with_local["reason"]


def test_ceiling_honours_a_lower_configured_value_only():
    caps = res.detect()
    lower = res.derive_ceiling(caps, configured_ceiling=1)
    assert lower["ceiling"] == 1
    # A higher configured value must not raise the ceiling above what the machine supports.
    higher = res.derive_ceiling(caps, configured_ceiling=caps.cpu_count + 50,
                                local_models_in_use=1, local_model_ids=["llama3.1:70b"])
    assert higher["ceiling"] <= caps.cpu_count


# ── state / workspace ────────────────────────────────────────────────────────


def _workspace(tmp_path, slug="demo"):
    ws = statemod.Workspace.for_project(slug, root=tmp_path / "projects")
    ws.ensure()
    return ws


def test_workspace_layout_is_created_idempotently(tmp_path):
    ws = _workspace(tmp_path)
    for directory in (ws.state_dir, ws.docs_dir, ws.src_dir,
                      ws.agents_dir, ws.sessions_dir, ws.telemetry_dir):
        assert directory.is_dir()
    ws.ensure()  # must not raise on a second call


def test_workspace_fresh_project_has_no_checkpoint(tmp_path):
    """A fresh project must report 'nothing to resume' rather than an empty checkpoint."""
    assert _workspace(tmp_path).load_checkpoint() is None


def test_checkpoint_round_trip_preserves_budget_and_log(tmp_path):
    ws = _workspace(tmp_path)
    cp = statemod.RunCheckpoint(run_id="run_1", workflow="spec-to-ship", manifest_sha="abc")
    cp.append_log(node="fixer", action="verify", verdict="changes_requested")
    cp.budget["steps_used"] = 5
    cp.delegation_chain = ["ag_a", "ag_b"]
    ws.save_checkpoint(cp)

    resumed = ws.load_checkpoint()
    assert resumed is not None
    assert resumed.run_id == "run_1"
    assert resumed.budget["steps_used"] == 5, "a resumed run must not get a fresh budget"
    assert resumed.delegation_chain == ["ag_a", "ag_b"], "delegation lineage must survive"
    assert resumed.log[0]["action"] == "verify"


def test_checkpoint_write_is_atomic(tmp_path):
    ws = _workspace(tmp_path)
    ws.save_checkpoint(statemod.RunCheckpoint(run_id="r"))
    assert not any(".tmp." in p.name for p in ws.state_dir.iterdir())


def test_checkpoint_from_dict_ignores_unknown_fields(tmp_path):
    """A checkpoint from a newer minor version must still resume."""
    cp = statemod.RunCheckpoint.from_dict(
        {"run_id": "r", "node": "fixer", "some_future_field": 1, "budget": {"steps_used": 2}}
    )
    assert cp.run_id == "r" and cp.node == "fixer"
    assert cp.budget["steps_used"] == 2
    assert cp.budget["max_steps"] == 0, "missing counters must be backfilled"


def test_checkpoint_backfills_missing_budget_keys():
    cp = statemod.RunCheckpoint.from_dict({"run_id": "r", "budget": {}})
    for key in ("max_steps", "steps_used", "tokens_used", "usd_used", "iterations"):
        assert key in cp.budget


def test_corrupt_checkpoint_refuses_to_resume(tmp_path):
    ws = _workspace(tmp_path)
    ws.checkpoint_path.write_text("{broken")
    with pytest.raises(statemod.StateError, match="corrupt"):
        ws.load_checkpoint()


@pytest.mark.parametrize("slug", ["../evil", "bad/slash", "", "UPPER", ".hidden"])
def test_workspace_rejects_invalid_slugs(tmp_path, slug):
    with pytest.raises(statemod.StateError):
        statemod.Workspace.for_project(slug, root=tmp_path)


@pytest.mark.parametrize("agent_id", ["../../etc", "a/b", ""])
def test_workspace_rejects_invalid_agent_ids(tmp_path, agent_id):
    ws = _workspace(tmp_path)
    with pytest.raises(statemod.StateError):
        ws.mailbox_path(agent_id)


@pytest.mark.parametrize("rel", ["../escape.json", "/tmp/abs.json", "a/../../b.json"])
def test_workspace_state_paths_are_contained(tmp_path, rel):
    ws = _workspace(tmp_path)
    with pytest.raises(statemod.StateError):
        ws.write_json(rel, {})


def test_workspace_json_document_round_trip(tmp_path):
    ws = _workspace(tmp_path)
    ws.write_json("org.json", {"agents": ["a"]})
    assert ws.read_json("org.json") == {"agents": ["a"]}
    assert ws.read_json("missing.json") is None


def test_workspace_lists_projects(tmp_path):
    root = tmp_path / "projects"
    statemod.Workspace.for_project("alpha", root=root).ensure()
    statemod.Workspace.for_project("beta", root=root).ensure()
    assert statemod.Workspace.list_projects(root=root) == ["alpha", "beta"]


def test_removing_a_provider_prunes_its_references(tmp_path):
    """Removing a provider must leave a config that loads with *no* repair needed.

    The bug this prevents: the console's Remove-provider path deleted the provider but left
    `concurrency.per_provider_limits[pid]` and a possible `defaults.provider` pointing at it, and the
    loader then refused the whole config — so removing a provider bricked the engine. The writer now
    prunes both, so the invalid state is never created.
    """
    path = _write_cfg(tmp_path)  # from the example, which configures anthropic + its limit
    cfgmod.write_provider(path, {}, provider_id="anthropic", remove=True)
    after = json.loads(path.read_text())
    assert "anthropic" not in after["providers"]
    assert "anthropic" not in after["concurrency"]["per_provider_limits"]
    # The config loads and needs no repair: no warning about a stale reference.
    cfg = cfgmod.load(path, warn=False)
    assert not cfg.raw.get("_warnings"), cfg.raw.get("_warnings")


def test_removing_the_default_provider_drops_the_default(tmp_path):
    """A default naming the removed provider is pruned too, rather than left for the loader to fix."""
    path = _write_cfg(tmp_path)
    cfgmod.write_provider(path, {}, provider_id="ollama", remove=True)  # the example's default
    after = json.loads(path.read_text())
    assert after["defaults"].get("provider") in (None, "")
    cfg = cfgmod.load(path, warn=False)
    assert not cfg.raw.get("_warnings"), cfg.raw.get("_warnings")
    assert cfg.providers
