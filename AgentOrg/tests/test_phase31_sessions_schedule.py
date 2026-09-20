#!/usr/bin/env python3
"""Phase 31 tests — portable sessions, and runs that fire on a clock.

Two features, one theme: state that already exists on disk, made *usable* rather than merely present.
A session could be resumed and inspected but not listed, handed over or branched, so the only way to
try an alternative continuation was to re-run the objective and destroy the evidence of the first
attempt. A goal could continue itself but not be *started* on a schedule, so "run this every morning"
had no answer that was not a shell loop with no record and no bound.

What these tests pin, in the order the risk matters:

1. **A fork cannot damage what it forked from.** The source files are hashed before and after, and
   that is the assertion the whole feature rests on — a branch that could corrupt its source is worse
   than no branch.
2. **A fork never overwrites, and an export describes itself.** The two write paths refuse in the
   ways that matter, and the archive is checked against its own manifest rather than trusted.
3. **Reading tolerates a broken session.** Someone asking "what do I have" must get an answer even
   when one of the answers is "this one is unreadable".
4. **A schedule never re-arms a goal a previous fire left parked.** This is the module's whole safety
   argument: a schedule repeats something that spends, so a failing entry left armed is an unattended
   spend loop, which is the exact failure the autonomy design exists to prevent.
5. **The new commands work and refuse, from the CLI.** The documented path and the refusal path, in
   the style of `test_phase4_cli.py`.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.cli import EXIT_CHECK_FAILED, EXIT_OK, EXIT_USAGE, build_parser
from engine.goal import Goal, GoalPolicy, GoalState
from engine.orchestrator import GateRequest, Orchestrator
from engine.schedules import (
    DEFAULT_TICK_S,
    MAX_TICK_S,
    MIN_TICK_S,
    PARKED_OUTCOMES,
    ScheduleEntry,
    ScheduleError,
    ScheduleStore,
    apply_fire,
    parse_duration,
    parse_when,
    watch,
)
from engine.session_exchange import (
    SessionError,
    export_session,
    fork_session,
    list_sessions,
    session_detail,
    session_summary,
    sessions_under,
    verify_export,
)
from engine.state import Workspace


def _run(*args: str) -> subprocess.CompletedProcess:
    """Run the CLI as a subprocess, so the real entry point is exercised."""
    return subprocess.run([sys.executable, "-m", "engine.cli", *args],
                          capture_output=True, text=True, cwd=str(ROOT))


# ── fixtures ─────────────────────────────────────────────────────────────────


_MANIFEST = """name: probe
version: "1.0.0"
description: session probe
payloads:
  handoff-v1:
    - status
    - summary
start: dev
nodes:
  - id: dev
    skill: backend-developer
    outputs: [change]
gates:
  - id: release
    type: gate
    kind: human
    requires: [change]
    description: Owner release approval
edges:
  - from: dev
    to: release
    when: dev.status == done
    payload: handoff-v1
end: [release]
"""

_STUB = '''CRITERIA = [
    "Every accepted finding addressed with a concrete diff",
]


def execute_node(node_id, state, ctx):
    if node_id == "release":
        return {"status": "needs_review", "verdict": "awaiting_owner",
                "summary": "gate reached", "evidence": ["g"]}
    return {"status": "done", "verdict": "ok", "summary": "implemented",
            "evidence": ["src/app.py#abc"], "criteria_met": list(CRITERIA),
            "artifacts": [{"name": "change", "path": "src/app.py", "sha": "abc",
                           "type": "change"}]}
'''


def _workspace(tmp_path, slug: str = "probe", *, root: pathlib.Path | None = None) -> Workspace:
    workspace = Workspace.for_project(slug, root=root or (tmp_path / "projects"))
    workspace.ensure()
    return workspace


def _populate(workspace: Workspace, *, objective: str = "add cursor pagination",
              phase: str = "awaiting_gate") -> None:
    """Write a session's worth of real state, through the engine's own writers.

    Written through `Goal.save` and `Workspace.save_checkpoint` rather than with `write_text`, so the
    fixture is the shape the engine actually produces — a hand-rolled fixture would let `list`/`show`
    pass against a file no run would ever create.
    """
    goal = Goal.new(objective, policy=GoalPolicy(posture="supervised"))
    goal.arm(by="test")
    goal.record_round(tokens=1200, requests=3, cost_usd=0.02)
    goal.save(workspace)

    workspace.save_checkpoint({
        "run_state_version": "1.0.0",
        "run_id": f"run_1_{workspace.slug}",
        "slug": workspace.slug,
        "goal": objective,
        "phase": phase,
        "outcome": {"nodes": {
            "dev": {"status": "done", "verdict": "ok", "summary": "implemented"},
            "release": {"status": "needs_review", "verdict": "awaiting_owner", "summary": "gate"},
        }, "artifacts": ["change"]},
        "gate": GateRequest(gate_id="release", kind="human", reason="release",
                            requires=["change"], present=["change"]).as_dict(),
        "stop_reason": "",
        "budget": {"steps_used": 2, "cost": {"tokens_in": 900, "tokens_out": 200,
                                             "cost_usd": 0.02, "measured": True}},
    })
    workspace.trace_path.write_text(
        json.dumps({"seq": 1, "type": "run.start", "run_id": f"run_1_{workspace.slug}",
                    "payload": {}}) + "\n", encoding="utf-8")
    (workspace.state_dir / "ledger.jsonl").write_text(
        json.dumps({"op": "record", "record": {
            "gate": "auth", "choice": "argon2id", "rationale": "memory hardness", "by": "ag_1",
            "node_id": "dev", "attempt": 1, "confidence": "high", "reversible": False,
            "rejected_alternatives": [], "constraints": [], "superseded": False,
            "superseded_by": "", "superseded_rationale": "", "at": "2026-09-18T06:00:00.000Z"}},
            sort_keys=True) + "\n", encoding="utf-8")

    from engine.org.handoff import Handoff

    handoff = Handoff(
        payload={"status": "done", "summary": "implemented", "artifacts": [], "decisions": [],
                 "open_questions": [], "verification_evidence": ["src/app.py#abc"], "context": {},
                 "budget": {}, "next": "release"},
        origin="dev", target="release")
    workspace.handoffs_dir.mkdir(parents=True, exist_ok=True)
    (workspace.handoffs_dir / f"{handoff.handoff_id}.json").write_text(
        json.dumps(handoff.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    # A plan beside the state, as a real run leaves it, so a fork has something to bring along.
    (workspace.path / "probe.yaml").write_text(_MANIFEST, encoding="utf-8")


def _hashes(directory: pathlib.Path) -> dict[str, str]:
    """sha256 of every file under a directory, keyed by its relative path.

    The whole point of the fork test: a byte-for-byte comparison over the entire tree, so a change
    anywhere — a rewritten checkpoint, a touched mtime-only file, an extra temp file — is visible.
    """
    out: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            out[path.relative_to(directory).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return out


# ── fork: the source is untouched ────────────────────────────────────────────


def test_fork_leaves_the_source_byte_identical(tmp_path):
    """The assertion the feature rests on: branching must not change what was branched from.

    Hashed over every file in the *project*, not just `.agent_state/`, because a fork that rewrote a
    checkpoint in place or repointed the original's plan would still leave the state directory intact
    while destroying the original's continuation.
    """
    source = _workspace(tmp_path)
    _populate(source)
    before = _hashes(source.path)

    fork_session(source, "probe-alt", root=tmp_path / "projects")

    after = _hashes(source.path)
    assert after == before, (
        "the source session changed; a fork must copy, never move or rewrite. "
        f"differing files: {sorted(set(before) ^ set(after)) or
                               [k for k in before if before[k] != after.get(k)]}"
    )


def test_fork_carries_the_state_into_the_new_slug(tmp_path):
    source = _workspace(tmp_path)
    _populate(source)
    report = fork_session(source, "probe-alt", root=tmp_path / "projects")

    assert report["from_slug"] == "probe"
    assert report["slug"] == "probe-alt"
    destination = Workspace.for_project("probe-alt", root=tmp_path / "projects")
    assert destination.state_dir.is_dir()

    fork_detail = session_detail(destination)
    original_detail = session_detail(source)
    assert fork_detail["objective"] == original_detail["objective"]
    assert fork_detail["phase"] == original_detail["phase"]
    assert fork_detail["handoffs"] == original_detail["handoffs"]
    assert fork_detail["spend"] == original_detail["spend"]


def test_the_fork_records_where_it_came_from(tmp_path):
    """Provenance, because "which of these two is the original" is the first question asked."""
    source = _workspace(tmp_path)
    _populate(source)
    fork_session(source, "probe-alt", root=tmp_path / "projects")
    record = json.loads(
        (Workspace.for_project("probe-alt", root=tmp_path / "projects")
         .state_dir / "fork.json").read_text())
    assert record["from_slug"] == "probe"
    assert record["from_path"] == str(source.path)
    assert record["to_slug"] == "probe-alt"


def test_fork_refuses_an_existing_slug(tmp_path):
    """Refused rather than overwritten: a fork that destroyed a session to make one is not a fork."""
    source = _workspace(tmp_path)
    _populate(source)
    fork_session(source, "probe-alt", root=tmp_path / "projects")
    existing = Workspace.for_project("probe-alt", root=tmp_path / "projects")
    before = _hashes(existing.path)

    with pytest.raises(SessionError, match="already exists"):
        fork_session(source, "probe-alt", root=tmp_path / "projects")
    assert _hashes(existing.path) == before


def test_fork_refuses_a_session_onto_itself(tmp_path):
    source = _workspace(tmp_path)
    _populate(source)
    with pytest.raises(SessionError, match="onto itself"):
        fork_session(source, "probe", root=tmp_path / "projects")


def test_fork_refuses_a_session_that_has_no_state(tmp_path):
    """A fork copies state, so there has to be state to copy — said plainly, not as an empty fork."""
    empty = _workspace(tmp_path, "empty")
    import shutil

    shutil.rmtree(empty.state_dir)
    with pytest.raises(SessionError, match="no .agent_state"):
        fork_session(empty, "empty-alt", root=tmp_path / "projects")


def test_fork_skips_a_torn_temporary_file(tmp_path):
    """An interrupted atomic write leaves `run_state.json.tmp.<pid>`; a fork must not carry it."""
    source = _workspace(tmp_path)
    _populate(source)
    (source.state_dir / "run_state.json.tmp.4132").write_text('{"run_id": "torn"')
    fork_session(source, "probe-alt", root=tmp_path / "projects")
    destination = Workspace.for_project("probe-alt", root=tmp_path / "projects")
    assert not list(destination.state_dir.glob("*.tmp.*"))
    # And the fork's checkpoint is the real one, not the torn file.
    assert session_summary(destination)["run_id"] == f"run_1_{source.slug}"


# ── export ───────────────────────────────────────────────────────────────────


def test_export_produces_a_zip_that_reloads(tmp_path):
    source = _workspace(tmp_path)
    _populate(source)
    target = tmp_path / "probe.zip"
    report = export_session(source, target)
    assert target.is_file()
    assert report["manifest"]["slug"] == "probe"

    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert {"run_state.json", "goal.json", "manifest.json"} <= names
        assert "cache/summary.json" in names
        # The trace and ledger travel: they are what a receiving machine reads to explain the run.
        assert {"trace.jsonl", "ledger.jsonl"} <= names
        assert any(name.startswith("handoffs/") for name in names)


def test_export_manifest_names_every_member_with_its_hash(tmp_path):
    """The manifest is the contract: what it lists is what is inside, and it is checkable."""
    source = _workspace(tmp_path)
    _populate(source)
    target = tmp_path / "probe.zip"
    manifest = export_session(source, target)["manifest"]

    with zipfile.ZipFile(target) as archive:
        for entry in manifest["contains"]:
            assert entry["name"] in archive.namelist()
            digest = hashlib.sha256(archive.read(entry["name"])).hexdigest()
            assert digest == entry["sha256"]
            assert entry["bytes"] > 0
    # The schema versions of what went in, read from the documents themselves.
    assert manifest["schema_versions"]["run_state.json:run_state_version"] == "1.0.0"
    assert manifest["schema_versions"]["goal.json:goal_version"] == "1.0.0"


def test_export_verifies_against_its_own_manifest(tmp_path):
    source = _workspace(tmp_path)
    _populate(source)
    target = tmp_path / "probe.zip"
    export_session(source, target)
    manifest = verify_export(target)
    assert manifest["slug"] == "probe"
    assert manifest["counts"]["handoffs"] == 1


def test_export_refuses_an_archive_without_a_manifest(tmp_path):
    target = tmp_path / "not-a-session.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("readme.txt", "hello")
    with pytest.raises(SessionError, match="no manifest.json"):
        verify_export(target)


def test_export_refuses_a_zip_whose_member_does_not_match(tmp_path):
    """A truncated or edited archive must be refused, not resumed from."""
    source = _workspace(tmp_path)
    _populate(source)
    target = tmp_path / "probe.zip"
    export_session(source, target)
    # Rewrite one member, leaving the manifest's hash stale — the exact shape of a corrupt archive.
    with zipfile.ZipFile(target) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["goal.json"] = b'{"objective": "something else"}'
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    with pytest.raises(SessionError, match="does not hash"):
        verify_export(target)


def test_export_refuses_a_session_that_was_never_run(tmp_path):
    empty = _workspace(tmp_path, "never")
    with pytest.raises(SessionError, match="nothing to export"):
        export_session(empty, tmp_path / "never.zip")


def test_export_refuses_to_carry_a_credential(tmp_path):
    """An archive's purpose is to leave this machine, so it must not carry a key."""
    source = _workspace(tmp_path)
    _populate(source)
    (source.state_dir / "trace.jsonl").write_text(
        json.dumps({"note": "key sk-111122223333444455556666"}) + "\n", encoding="utf-8")
    with pytest.raises(SessionError, match="key-shaped"):
        export_session(source, tmp_path / "leaky.zip")
    assert not (tmp_path / "leaky.zip").exists(), "a refused export must leave no archive behind"


# ── list and show ────────────────────────────────────────────────────────────


def test_list_agrees_with_the_engine_and_orders_newest_first(tmp_path):
    root = tmp_path / "projects"
    older = _workspace(tmp_path, "older", root=root)
    _populate(older, objective="the older objective")
    newer = _workspace(tmp_path, "newer", root=root)
    _populate(newer, objective="the newer objective")
    # One second apart, so "newest first" is a real ordering rather than a tie broken by name.
    import os
    import time

    past = time.time() - 60
    for name in ("run_state.json", "goal.json", "trace.jsonl", "ledger.jsonl"):
        os.utime(older.state_dir / name, (past, past))

    rows = list_sessions(sessions_under(root))
    assert [row["slug"] for row in rows] == ["newer", "older"]
    assert rows[1]["objective"] == "the older objective"
    # The spend is the goal's cumulative figure, which is what a person asks about.
    assert rows[1]["spend"]["cost_usd"] == pytest.approx(0.02)
    assert rows[1]["spend"]["tokens"] == 1200
    assert rows[0]["phase"] == "awaiting_gate"
    assert rows[0]["handoffs"] == 1
    assert rows[0]["nodes"] == 2


def test_show_reports_the_run_the_nodes_and_the_handoffs(tmp_path):
    source = _workspace(tmp_path)
    _populate(source)
    detail = session_detail(source)
    assert detail["run_id"] == "run_1_probe"
    assert detail["objective"] == "add cursor pagination"
    assert detail["goal_state"] == "armed"
    assert detail["posture"] == "supervised"
    assert len(detail["ledger"]) == 1
    assert detail["ledger"][0]["record"]["choice"] == "argon2id"
    assert detail["trace_events"] == 1
    assert len(detail["handoffs"]) == 1
    assert detail["handoffs"][0]["payload"]["summary"] == "implemented"
    assert detail["refused"] == []
    rows = {row["node_id"]: row for row in detail["rows"]}
    assert rows["dev"]["status"] == "done"


def test_a_session_with_no_state_is_reported_not_crashed(tmp_path):
    """An absent workspace is a fact to report, not an exception that stops the listing."""
    workspace = Workspace.for_project("never-ran", root=tmp_path / "projects")
    summary = session_summary(workspace)
    assert summary["present"] is False
    assert summary["phase"] == "idle"
    detail = session_detail(workspace)
    assert detail["rows"] == []
    assert detail["spend"]["measured"] is False


def test_a_corrupt_session_is_reported_rather_than_raising(tmp_path):
    """Someone asking "what do I have" must get an answer even when one session is unreadable."""
    source = _workspace(tmp_path)
    _populate(source)
    source.checkpoint_path.write_text("{ this is not json", encoding="utf-8")
    summary = session_summary(source)
    # The goal is still readable, so the session is listed with what it can say and no crash.
    assert summary["objective"] == "add cursor pagination"
    assert summary["run_id"] == ""
    assert summary["phase"] == "idle"


def test_a_session_with_neither_checkpoint_nor_goal_says_so(tmp_path):
    source = _workspace(tmp_path)
    (source.state_dir / "run_state.json").write_text("not json", encoding="utf-8")
    summary = session_summary(source)
    assert "no readable run checkpoint" in summary["error"]


def test_a_refused_handoff_is_reported_not_shown_as_sound(tmp_path):
    """The checksum is the reason a persisted handoff is worth reading; a broken one must be named."""
    source = _workspace(tmp_path)
    _populate(source)
    path = next(source.handoffs_dir.glob("*.json"))
    document = json.loads(path.read_text())
    document["payload"]["summary"] = "edited after the fact"
    path.write_text(json.dumps(document), encoding="utf-8")

    detail = session_detail(source)
    assert detail["handoffs"] == []
    assert len(detail["refused"]) == 1
    assert "checksum" in detail["refused"][0]["reason"]


def test_an_unreadable_cache_store_does_not_hide_the_session(tmp_path):
    source = _workspace(tmp_path)
    _populate(source)
    source.cache_dir.mkdir(parents=True, exist_ok=True)
    (source.cache_dir / "shapes.jsonl").write_text("{not json\n", encoding="utf-8")
    detail = session_detail(source)
    assert detail["objective"] == "add cursor pagination"


# ── schedule parsing and due-time evaluation ─────────────────────────────────


def test_parse_duration_reads_the_units_people_type():
    assert parse_duration("30s") == 30
    assert parse_duration("15m") == 900
    assert parse_duration("6h") == 21_600
    assert parse_duration("2d") == 172_800
    # A bare number means minutes, because "every 30" is a person meaning half an hour.
    assert parse_duration("30") == 1800


def test_parse_duration_refuses_nonsense_and_zero():
    with pytest.raises(ScheduleError, match="cannot read"):
        parse_duration("soon")
    with pytest.raises(ScheduleError, match="at least one second"):
        parse_duration("0m")


def test_parse_when_accepts_the_form_the_engine_writes():
    assert parse_when("2026-09-18T07:30:00Z") == parse_when("2026-09-18T07:30:00")
    assert parse_when("2026-09-18T07:30:00.250Z") == parse_when("2026-09-18T07:30:00")


def test_parse_when_refuses_a_time_it_cannot_place():
    """A relaxed parser would read a local-time string as UTC and fire an hour early."""
    with pytest.raises(ScheduleError, match="UTC ISO instant"):
        parse_when("tomorrow")


def test_the_store_survives_a_reload_with_its_due_time(tmp_path):
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="triage the overnight issues", every="1h")

    reloaded = ScheduleStore(workspace)
    assert reloaded.entries[0].id == entry.id
    assert reloaded.entries[0].objective == "triage the overnight issues"
    assert reloaded.entries[0].interval_s == 3600
    assert reloaded.entries[0].next_due_at == entry.next_due_at


def test_the_schedule_file_is_versioned_and_written_atomically(tmp_path):
    workspace = _workspace(tmp_path)
    ScheduleStore(workspace).add(objective="x", every="1h")
    document = json.loads((workspace.state_dir / "schedules.json").read_text())
    assert document["schedule_version"] == "1.0.0"
    assert document["workspace"] == "probe"
    # No temp file is left behind, and nothing else appeared beside it.
    assert not list(workspace.state_dir.glob("schedules.json.tmp.*"))


def test_an_interval_entry_is_due_once_its_time_has_passed(tmp_path):
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="hourly", every="1h", now=1000.0)
    assert entry.is_due(1000.0) is False, "an entry is not due the instant it is created"
    assert entry.is_due(1000.0 + 3599) is False
    assert entry.is_due(1000.0 + 3600) is True
    assert [e.id for e in store.due(1000.0 + 3600)] == [entry.id]


def test_a_oneshot_entry_fires_once_and_then_retires(tmp_path):
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="release the patch", at="2026-09-18T07:30:00")
    assert entry.is_oneshot() is True
    assert [e.id for e in store.due("2026-09-18T07:30:01")] == [entry.id]

    apply_fire(store, entry, {"outcome": "done", "run_id": "run_1"}, now=parse_when(
        "2026-09-18T07:30:01"))
    assert entry.enabled is False
    assert "one-shot" in entry.disabled_reason
    # And it is not due again, at any later time.
    assert store.due("2027-01-01T00:00:00") == []


def test_a_repeating_entry_advances_from_the_fire_time(tmp_path):
    """Advanced from *now* rather than the previous due time, so a missed window fires once."""
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="hourly", every="1h", now=1000.0)
    # Fired four hours late — a laptop that was asleep. It must not replay four windows.
    apply_fire(store, entry, {"outcome": "done", "run_id": "run_1"}, now=1000.0 + 4 * 3600)
    assert entry.next_due_at == parse_when_free(1000.0 + 5 * 3600)
    assert [e.id for e in store.due(1000.0 + 4 * 3600)] == []
    assert [e.id for e in store.due(1000.0 + 5 * 3600)] == [entry.id]


def parse_when_free(epoch: float) -> str:
    """The instant the store writes for an epoch — the same form `parse_when` reads back."""
    import time as _time

    return (_time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime(epoch))
            + f".{int(epoch * 1000) % 1000:03d}Z")


def test_due_is_read_only(tmp_path):
    """A query that advanced the clock would mean asking twice fired twice."""
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="hourly", every="1h", now=1000.0)
    before = (entry.next_due_at, entry.last_fired_at, entry.enabled)
    store.due(1000.0 + 7200)
    store.due(1000.0 + 7200)
    assert (entry.next_due_at, entry.last_fired_at, entry.enabled) == before


def test_a_disabled_entry_is_not_due(tmp_path):
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="hourly", every="1h", now=1000.0, enabled=False)
    assert store.due(1000.0 + 7200) == []
    store.enable(entry.id, at=1000.0 + 7200)
    assert [e.id for e in store.due(1000.0 + 7200)] == [entry.id]


def test_an_incompatible_schedule_file_refuses_rather_than_guessing(tmp_path):
    workspace = _workspace(tmp_path)
    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    (workspace.state_dir / "schedules.json").write_text(json.dumps({
        "schedule_version": "9.0.0", "entries": [{"slug": "probe", "objective": "x"}]}))
    store = ScheduleStore(workspace)
    assert "not compatible" in store.load_error
    assert store.due(10**10) == []


def test_a_broken_entry_is_skipped_without_losing_the_rest(tmp_path):
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    store.add(objective="good one", every="1h")
    document = json.loads((workspace.state_dir / "schedules.json").read_text())
    document["entries"].append({"id": "sch_bad", "slug": "probe", "objective": ""})
    (workspace.state_dir / "schedules.json").write_text(json.dumps(document))

    reloaded = ScheduleStore(workspace)
    assert [entry.objective for entry in reloaded.entries] == ["good one"]
    assert "skipped an entry" in reloaded.load_error


def test_add_refuses_an_unspecified_time(tmp_path):
    """The default would be a guess about intent, and firing is spending."""
    store = ScheduleStore(_workspace(tmp_path))
    with pytest.raises(ScheduleError, match="say when it should fire"):
        store.add(objective="do the thing")


def test_add_refuses_two_competing_times(tmp_path):
    store = ScheduleStore(_workspace(tmp_path))
    with pytest.raises(ScheduleError, match="exactly one of"):
        store.add(objective="do the thing", every="1h", at="2026-09-18T07:30:00")


def test_add_refuses_an_empty_objective(tmp_path):
    store = ScheduleStore(_workspace(tmp_path))
    with pytest.raises(ScheduleError, match="needs an objective"):
        store.add(objective="   ", every="1h")


def test_remove_refuses_a_slug_that_matches_two_entries(tmp_path):
    """Removing the wrong schedule is not a mistake that can be undone."""
    store = ScheduleStore(_workspace(tmp_path))
    first = store.add(objective="one", every="1h")
    store.add(objective="two", every="2h")
    with pytest.raises(ScheduleError, match="matches 2 entries"):
        store.remove("probe")
    assert len(store.entries) == 2
    assert store.remove(first.id).objective == "one"


def test_remove_refuses_an_unknown_reference(tmp_path):
    store = ScheduleStore(_workspace(tmp_path))
    with pytest.raises(ScheduleError, match="no schedule entry matches"):
        store.remove("sch_nope")


# ── the no-re-arm rule ───────────────────────────────────────────────────────


@pytest.mark.parametrize("outcome", list(PARKED_OUTCOMES))
def test_a_parked_fire_disables_the_entry_instead_of_firing_again(tmp_path, outcome):
    """The safety argument of the whole module, asserted for every parked outcome.

    A schedule repeats something that spends. Leaving a failing objective armed means it fires again
    on the next tick, and again on the one after — an unattended spend loop on a goal that has already
    said it cannot proceed. So a parked fire disables the entry, and re-arming it is a deliberate act.
    """
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="hourly triage", every="1h", now=1000.0)

    report = apply_fire(store, entry, {"outcome": outcome, "detail": "the run is waiting",
                                       "run_id": "run_1"}, now=1000.0 + 3600)
    assert entry.enabled is False, f"a {outcome} fire must not leave the entry armed"
    assert outcome in entry.disabled_reason
    assert report["entry_enabled"] is False
    assert "never re-arms" in entry.disabled_reason

    # And it really does not fire again, however many windows pass: the due time is untouched.
    assert store.due(1000.0 + 3600) == []
    assert store.due(1000.0 + 100 * 3600) == []
    # The refusal is durable, not in-memory.
    assert ScheduleStore(workspace).entries[0].enabled is False


def test_a_successful_fire_keeps_the_entry_armed(tmp_path):
    """The rule is about *parked* fires; a schedule that works must keep working."""
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="hourly triage", every="1h", now=1000.0)
    apply_fire(store, entry, {"outcome": "done", "run_id": "run_1"}, now=1000.0 + 3600)
    assert entry.enabled is True
    assert entry.last_outcome == "done"
    assert [e.id for e in store.due(1000.0 + 7200)] == [entry.id]


def test_enabling_a_parked_entry_is_the_deliberate_act_that_rearms_it(tmp_path):
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="hourly triage", every="1h", now=1000.0)
    apply_fire(store, entry, {"outcome": "blocked", "detail": "no provider"}, now=1000.0 + 3600)
    assert store.due(1000.0 + 7200) == []

    store.enable(entry.id, at=1000.0 + 9000)
    assert entry.enabled is True
    assert entry.disabled_reason == ""
    assert [e.id for e in store.due(1000.0 + 9000)] == [entry.id]


def test_the_watcher_does_not_refire_a_parked_goal(tmp_path):
    """The rule end to end through the watcher: one fire, then the entry is paused and silent."""
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="always fails", every="1h", now=1000.0)
    entry.next_due_at = "1970-01-01T00:16:40.000Z"  # due at 1000s
    store.save()

    clock = {"now": 1000.0}
    launched: list[str] = []

    def fake_fire(orch, entry, workspace, **kwargs):
        launched.append(entry.id)
        return {"outcome": "failed", "detail": "no provider could be built", "run_id": ""}

    report = watch(store, resolve=lambda slug: ("orchestrator", workspace),
                   tick_s=MIN_TICK_S, ticks=5, now=lambda: clock["now"],
                   sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
                   fire_fn=fake_fire, policy_for=lambda orch, posture: posture)

    assert launched == [entry.id], "the goal must fire exactly once, not once per tick"
    assert report["ticks"] == 5, "the watcher keeps watching; the entry is what stops"
    assert len(report["disabled"]) == 1
    assert report["disabled"][0]["id"] == entry.id
    assert store.entries[0].enabled is False


def test_the_watcher_fires_a_healthy_entry_on_its_interval(tmp_path):
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="hourly triage", every="1h", now=1000.0)
    entry.next_due_at = "1970-01-01T00:16:40.000Z"
    store.save()

    clock = {"now": 1000.0}
    launched: list[float] = []

    def fake_fire(orch, entry, workspace, **kwargs):
        launched.append(clock["now"])
        return {"outcome": "done", "run_id": "run_1"}

    # Two hours of ticks at the floor interval: the 1h entry fires once per window, and the second
    # window is what proves the entry re-armed itself rather than firing on every tick.
    watch(store, resolve=lambda slug: ("orchestrator", workspace),
          tick_s=MIN_TICK_S, ticks=1440, now=lambda: clock["now"],
          sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
          fire_fn=fake_fire, policy_for=lambda orch, posture: posture)

    assert len(launched) == 2, launched
    assert launched[1] - launched[0] >= 3600


def test_the_watcher_refuses_a_tick_outside_the_permitted_range(tmp_path):
    """Refused rather than clamped: a sub-floor tick is a busy loop dressed as a schedule."""
    store = ScheduleStore(_workspace(tmp_path))
    with pytest.raises(ScheduleError, match="outside the permitted range"):
        watch(store, resolve=lambda slug: (None, None), tick_s=0, ticks=1)
    with pytest.raises(ScheduleError, match="outside the permitted range"):
        watch(store, resolve=lambda slug: (None, None), tick_s=MAX_TICK_S + 1, ticks=1)
    # The documented bounds are the ones enforced.
    assert MIN_TICK_S >= 1 and MAX_TICK_S > DEFAULT_TICK_S > MIN_TICK_S


def test_the_watcher_honours_max_fires(tmp_path):
    """The second bound: a watch started by a script has nobody to press Ctrl-C."""
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    for index in range(3):
        entry = store.add(objective=f"task {index}", every="1s", now=1000.0)
        entry.next_due_at = "1970-01-01T00:16:40.000Z"
    store.save()

    clock = {"now": 1000.0}
    report = watch(store, resolve=lambda slug: ("orchestrator", workspace),
                   tick_s=MIN_TICK_S, ticks=10, max_fires=2, now=lambda: clock["now"],
                   sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
                   fire_fn=lambda *a, **k: {"outcome": "done", "run_id": "r"},
                   policy_for=lambda orch, posture: posture)
    assert len(report["fired"]) == 2
    assert "max-fires=2" in report["stopped"]


def test_the_watcher_refuses_to_run_on_an_unreadable_schedule(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace.state_dir / "schedules.json").write_text("{ not json")
    store = ScheduleStore(workspace)
    with pytest.raises(ScheduleError, match="refusing to watch"):
        watch(store, resolve=lambda slug: (None, None), tick_s=DEFAULT_TICK_S, ticks=1)


def test_an_unresolvable_slug_disables_its_entry_rather_than_crashing(tmp_path):
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="x", every="1h", now=1000.0)
    entry.next_due_at = "1970-01-01T00:16:40.000Z"
    store.save()

    def explode(slug):
        raise RuntimeError("no such workspace")

    report = watch(store, resolve=explode, tick_s=MIN_TICK_S, ticks=1, now=lambda: 1000.0,
                   sleep=lambda seconds: None)
    assert report["fired"] == []
    assert len(report["disabled"]) == 1
    assert "cannot resolve the workspace" in report["disabled"][0]["reason"]


# ── two writers, one file ────────────────────────────────────────────────────
#
# The watcher holds a schedule in memory across ticks; the CLI and the app edit the same file from
# other processes. Everything in this section is about that: what the watcher writes back must be the
# schedule as it *is*, not the copy it read when it started. The defect these tests pin down was
# visible to a person as "I pressed Forget and the entry came back".


def _fast_and_future(store: ScheduleStore) -> tuple[ScheduleEntry, ScheduleEntry]:
    """One entry that fires every tick, and one that never fires but is in memory.

    The pair is what makes the concurrent case testable without a real clock: the first forces a save
    on every tick, and the second is the entry a removal will be aimed at.

    Both are added *before* the due time is edited, because every mutation reloads the file first —
    an in-memory edit that has not been saved yet is a change the file does not have, and the next
    mutation is entitled to overwrite it.
    """
    keeper = store.add(objective="fires every tick so the watcher saves again", every="5s",
                       now=1000.0)
    doomed = store.add(objective="the entry a person forgot", every="1h", now=1000.0)
    keeper.next_due_at = "1970-01-01T00:16:40.000Z"      # due at 1000s, i.e. immediately
    store.save()
    return keeper, doomed


def test_a_removal_during_a_live_watcher_is_not_resurrected(tmp_path):
    """The load-bearing one: Forget wins, even when the watcher fires straight afterwards.

    Written against the file, not against the watcher's memory — the memory being right is not what a
    person sees; the entry coming back in `schedules list` is. The removal happens between two ticks
    (from `on_tick`, through a second store, exactly as another process reaches the same file) and
    the assertion is taken after the *next* fire, because that next save is what used to undo it.
    """
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    keeper, doomed = _fast_and_future(store)

    clock = {"now": 1000.0}
    other = ScheduleStore(workspace)          # "another process": its own store, its own lock
    removed_at_tick: list[int] = []

    def on_tick(tick_report):
        # Tick 1: the keeper has fired, so the removal lands after a save and before the next one.
        # What is recorded is how many entries were due on that tick, so the assertion below can
        # prove a fire really did happen before the removal rather than assuming it.
        if not removed_at_tick:
            removed_at_tick.append(len(tick_report["due"]))
            other.remove(doomed.id)

    report = watch(store, resolve=lambda slug: ("orchestrator", workspace),
                   tick_s=MIN_TICK_S, ticks=2, now=lambda: clock["now"],
                   sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
                   fire_fn=lambda *a, **k: {"outcome": "done", "run_id": "run_1"},
                   policy_for=lambda orch, posture: posture, on_tick=on_tick)

    assert removed_at_tick and removed_at_tick[0] == 1, (
        "the removal must land on the tick after a fire, or nothing would have written the stale "
        "list back")
    assert len(report["fired"]) >= 2, "the keeper must fire again *after* the removal, or nothing " \
                                      "would have written the stale list back"
    on_disk = ScheduleStore(workspace)
    assert on_disk.by_id(doomed.id) is None, "the removed entry came back"
    assert [entry.id for entry in on_disk.entries] == [keeper.id]
    # And the watcher's own memory agrees, so its next tick cannot resurrect it either. The report's
    # `removed` list is deliberately not asserted here: nothing fired the doomed entry, so there is no
    # fire of its own to report — that path is the unit test below, where a fire *was* in flight.
    assert store.by_id(doomed.id) is None


def test_a_removal_during_a_fire_is_reported_and_not_undone(tmp_path):
    """The same rule where the entry's *own* fire is in flight: Forget wins and the watcher says so.

    A fire takes minutes, so this is the likelier version of the bug in practice — the run started
    while the entry existed and finished after someone had removed it. The entry must not come back,
    and the report must say what happened rather than dressing the removal up as a disabled entry.
    """
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="the entry a person forgets mid-run", every="1h", now=1000.0)
    entry.next_due_at = "1970-01-01T00:16:40.000Z"
    store.save()
    other = ScheduleStore(workspace)

    def fire_that_gets_forgotten(orch, entry, workspace, **kwargs):
        other.remove(entry.id)                     # the person pressed Forget while the run ran
        return {"outcome": "done", "run_id": "run_1"}

    report = watch(store, resolve=lambda slug: ("orchestrator", workspace),
                   tick_s=MIN_TICK_S, ticks=1, now=lambda: 1000.0,
                   sleep=lambda seconds: None, fire_fn=fire_that_gets_forgotten,
                   policy_for=lambda orch, posture: posture)

    assert [record["id"] for record in report["fired"]] == [entry.id]
    assert [record["id"] for record in report["removed"]] == [entry.id]
    assert report["disabled"] == [], "a removal is not a disabled entry"
    assert ScheduleStore(workspace).entries == [], "the removed entry came back"


def test_a_concurrent_add_is_not_lost_and_is_noticed(tmp_path):
    """An entry added beside the watcher survives its next save, and fires on the next tick.

    Losing it is the mirror image of the resurrected removal: both are one process writing a list it
    read before the other process changed it.
    """
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    keeper, _ = _fast_and_future(store)

    clock = {"now": 1000.0}
    other = ScheduleStore(workspace)
    added: list[ScheduleEntry] = []

    def on_tick(tick_report):
        if not added:
            entry = other.add(objective="added beside the watcher", every="1h", now=1000.0)
            entry.next_due_at = "1970-01-01T00:16:40.000Z"
            other.save()
            added.append(entry)

    # Two ticks, and the add lands on the first: the entry has to be *seen* on the very next tick,
    # not merely preserved until something else happens to fire. That is what the read-only reload in
    # `due` buys, and it is the difference between a schedule a person adds being picked up in the
    # minute they added it and being picked up at some later fire of somebody else's entry.
    report = watch(store, resolve=lambda slug: ("orchestrator", workspace),
                   tick_s=MIN_TICK_S, ticks=2, now=lambda: clock["now"],
                   sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
                   fire_fn=lambda *a, **k: {"outcome": "done", "run_id": "run_1"},
                   policy_for=lambda orch, posture: posture, on_tick=on_tick)

    assert added, "the add never happened, so this test proves nothing"
    on_disk = ScheduleStore(workspace)
    assert on_disk.by_id(added[0].id) is not None, "the added entry was written away by the watcher"
    assert keeper.id in [entry.id for entry in on_disk.entries]
    # It was not merely preserved: the watcher saw it and fired it on the tick after the add.
    assert added[0].id in [record["id"] for record in report["fired"]]


def test_a_write_is_refused_rather_than_made_blind(tmp_path):
    """A schedule this process cannot read is never overwritten with the copy it is holding.

    The difference between a corrupt file and a truncated one, and the reason `reload` distinguishes
    "unreadable" from "empty".
    """
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="hourly", every="1h")
    (workspace.state_dir / "schedules.json").write_text("{ not json", encoding="utf-8")

    with pytest.raises(ScheduleError, match="refusing to write"):
        store.remove(entry.id)
    assert (workspace.state_dir / "schedules.json").read_text(encoding="utf-8") == "{ not json"


def test_a_removal_beats_a_fire_that_was_already_in_flight(tmp_path):
    """The unit-level form of the watcher test: a fire that lands after a removal writes nothing.

    `apply_fire` is what the next save goes through, so this is where the entry has to be looked up
    again. The report says what happened rather than pretending the entry is still there.
    """
    workspace = _workspace(tmp_path)
    store = ScheduleStore(workspace)
    entry = store.add(objective="hourly", every="1h", now=1000.0)
    ScheduleStore(workspace).remove(entry.id)

    folded = apply_fire(store, entry, {"outcome": "done", "run_id": "run_1"}, now=1000.0 + 3600)
    assert folded["entry_removed"] is True
    assert folded["entry_enabled"] is False
    assert ScheduleStore(workspace).entries == []


# ── firing a real goal, on the same path a manual run takes ──────────────────


def _stub_file(tmp_path) -> pathlib.Path:
    """The executor plugin a real fire needs, so `fire` can be driven end to end."""
    path = tmp_path / "stub_executor.py"
    path.write_text(_STUB, encoding="utf-8")
    return path


def _orchestrator(tmp_path, slug: str = "probe"):
    from engine.config import load
    from engine.library import resolve

    workspace = _workspace(tmp_path, slug)
    return Orchestrator(config=load(), library=resolve(), workspace=workspace), workspace


def test_fire_sets_an_armed_goal_through_the_orchestrator(tmp_path):
    """A scheduled run must be the same run a person starts, so `fire` goes through `goal_set`.

    Verified at the seam: the goal the orchestrator holds afterwards is armed, carries the entry's
    posture, and is recorded as armed by the schedule — which is what makes a scheduled run
    attributable to the schedule rather than looking like somebody typed it.
    """
    from engine.schedules import fire

    orch, workspace = _orchestrator(tmp_path)
    entry = ScheduleEntry(slug="probe", objective="triage the overnight issues",
                          posture="supervised", interval_s=3600)

    class Abort(Exception):
        pass

    def stop():
        raise Abort

    report = fire(orch, entry, workspace, plan=stop)
    # `plan` is the seam: the fire reached the point of planning, which is after the goal was set.
    assert report["outcome"] == "failed"
    assert "Abort" in report["detail"]

    goal = orch.goal()
    assert goal is not None
    assert goal.objective == "triage the overnight issues"
    assert goal.state is GoalState.ARMED
    assert goal.armed_by == "schedule"
    assert goal.policy.posture.value == "supervised"
    # And it is on disk, so a later process sees what the schedule started.
    reloaded = Goal.load(workspace)
    assert reloaded is not None and reloaded.objective == "triage the overnight issues"


def test_fire_reports_a_gated_run_as_gated_rather_than_done(tmp_path):
    """The outcome names *what happened to the goal*, because that is what decides a re-arm.

    Driven through a real plan and a real executor until the graph parks at its terminal gate: the
    point is that `fire` reaches the same end state a manual `run` does and *classifies* it, rather
    than reporting a run sitting at a gate as done.
    """
    from engine.schedules import fire

    orch, workspace = _orchestrator(tmp_path)
    entry = ScheduleEntry(slug="probe", objective="Add cursor pagination to the booking API",
                          posture="supervised")
    report = fire(orch, entry, workspace, executor=str(_stub_file(tmp_path)))
    assert report["phase"] in ("awaiting_gate", "awaiting_human"), report["phase"]
    assert report["outcome"] == "gated"
    assert report["run_id"]
    assert report["goal_state"] == "paused", "a fired goal that parks is paused, not left armed"


def test_fire_reports_a_goal_that_cannot_start_rather_than_raising(tmp_path):
    """A fire that cannot even plan is a report the watcher can act on, not an exception."""
    from engine.schedules import fire

    orch, workspace = _orchestrator(tmp_path)
    entry = ScheduleEntry(slug="probe", objective="triage", posture="supervised")

    class Abort(Exception):
        pass

    def stop():
        raise Abort

    report = fire(orch, entry, workspace, plan=stop)
    assert report["outcome"] == "failed"
    assert "could not be started" in report["detail"]
    # The reason is named, not an empty string after the colon — an operator has to act on this line.
    assert "Abort" in report["detail"]
    # And the goal it set is still on disk, armed: the failure is recorded, not hidden.
    goal = orch.goal()
    assert goal is not None and goal.objective == "triage"


# ── the CLI surface ──────────────────────────────────────────────────────────


def test_the_parser_accepts_the_new_commands():
    parser = build_parser()
    for argv in (["session", "list"], ["session", "list", "--root", "/tmp/x"],
                 ["session", "show", "--slug", "a"],
                 ["session", "export", "--slug", "a", "--out", "a.zip", "--verify"],
                 ["session", "fork", "--slug", "a", "--to", "b"],
                 ["schedules", "list", "--slug", "a"],
                 ["schedules", "add", "do it", "--every", "1h"],
                 ["schedules", "add", "do it", "--at", "2026-09-18T07:30:00"],
                 ["schedules", "add", "do it", "--due-now", "--posture", "supervised"],
                 ["schedules", "remove", "sch_1"], ["schedules", "enable", "sch_1"],
                 ["schedules", "watch", "--interval", "30", "--ticks", "2"]):
        assert parser.parse_args(argv).func is not None, f"{argv} does not resolve to a command"


def test_the_help_lists_the_new_commands():
    text = build_parser().format_help()
    assert "session" in text
    assert "schedules" in text


def test_session_list_reads_a_projects_root(tmp_path):
    root = tmp_path / "projects"
    first = _workspace(tmp_path, "first", root=root)
    _populate(first, objective="the first objective")
    second = _workspace(tmp_path, "second", root=root)
    _populate(second, objective="the second objective")

    result = _run("--json", "session", "list", "--root", str(root))
    assert result.returncode == EXIT_OK, result.stderr[:400]
    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert {row["slug"] for row in payload["sessions"]} == {"first", "second"}
    assert all(row["objective"] for row in payload["sessions"])


def test_session_list_on_an_empty_root_says_so(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    result = _run("session", "list", "--root", str(root))
    assert result.returncode == EXIT_OK
    assert "no sessions found" in result.stdout


def test_session_show_prints_only_json_under_json(tmp_path):
    root = tmp_path / "projects"
    workspace = _workspace(tmp_path, "shown", root=root)
    _populate(workspace, objective="the shown objective")
    result = _run("--json", "session", "show", "--slug", "shown", "--root", str(root))
    assert result.returncode == EXIT_OK, result.stderr[:400]
    payload = json.loads(result.stdout)  # must not raise: `--json` is only JSON
    assert payload["objective"] == "the shown objective"
    assert payload["spend"]["cost_usd"] == pytest.approx(0.02)


def test_session_export_and_verify_from_the_cli(tmp_path):
    root = tmp_path / "projects"
    workspace = _workspace(tmp_path, "exported", root=root)
    _populate(workspace)
    target = tmp_path / "exported.zip"
    result = _run("session", "export", "--slug", "exported", "--root", str(root),
                  "--out", str(target), "--verify")
    assert result.returncode == EXIT_OK, result.stderr[:400]
    assert target.is_file()
    assert "verified" in result.stdout
    manifest = verify_export(target)
    assert manifest["slug"] == "exported"


def test_session_fork_from_the_cli_leaves_the_source_alone(tmp_path):
    root = tmp_path / "projects"
    workspace = _workspace(tmp_path, "branched", root=root)
    _populate(workspace)
    before = _hashes(workspace.path)

    result = _run("session", "fork", "--slug", "branched", "--root", str(root), "--to", "branched-b")
    assert result.returncode == EXIT_OK, result.stderr[:400]
    assert "branched -> branched-b" in result.stdout
    assert _hashes(workspace.path) == before
    assert (root / "branched-b" / ".agent_state" / "goal.json").is_file()


def test_session_fork_refuses_an_existing_slug_from_the_cli(tmp_path):
    """The refusal path, asserted on the exit code and the message a person reads."""
    root = tmp_path / "projects"
    workspace = _workspace(tmp_path, "branched", root=root)
    _populate(workspace)
    (root / "taken").mkdir(parents=True)
    result = _run("session", "fork", "--slug", "branched", "--root", str(root), "--to", "taken")
    assert result.returncode == EXIT_CHECK_FAILED
    assert "already exists" in result.stderr
    assert "never overwrites" in result.stderr


def test_session_show_fails_clearly_for_an_absent_session(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    result = _run("session", "show", "--slug", "never-ran", "--root", str(root), "--json")
    # A managed workspace that was never created has no state: reported, not crashed.
    assert result.returncode == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["present"] is False


def test_schedules_add_list_remove_end_to_end(tmp_path):
    root = tmp_path / "projects"
    workspace = _workspace(tmp_path, "clocked", root=root)

    added = _run("schedules", "add", "triage the overnight issues", "--every", "2h",
                 "--slug", "clocked", "--root", str(root), "--json")
    assert added.returncode == EXIT_OK, added.stderr[:400]
    entry = json.loads(added.stdout)
    assert entry["interval_s"] == 7200
    assert entry["enabled"] is True

    listed = _run("--json", "schedules", "list", "--slug", "clocked", "--root", str(root))
    assert listed.returncode == EXIT_OK, listed.stderr[:400]
    view = json.loads(listed.stdout)
    assert view["counts"]["enabled"] == 1
    assert view["entries"][0]["id"] == entry["id"]

    removed = _run("schedules", "remove", entry["id"], "--slug", "clocked", "--root", str(root))
    assert removed.returncode == EXIT_OK, removed.stderr[:400]
    assert "removed" in removed.stdout
    view = json.loads(_run("--json", "schedules", "list", "--slug", "clocked",
                           "--root", str(root)).stdout)
    assert view["counts"]["total"] == 0


def test_schedules_add_refuses_an_unspecified_time_from_the_cli(tmp_path):
    root = tmp_path / "projects"
    _workspace(tmp_path, "clocked", root=root)
    result = _run("schedules", "add", "do the thing", "--slug", "clocked", "--root", str(root))
    assert result.returncode == EXIT_USAGE
    assert "say when it should fire" in result.stderr


def test_schedules_remove_refuses_an_unknown_entry(tmp_path):
    root = tmp_path / "projects"
    _workspace(tmp_path, "clocked", root=root)
    result = _run("schedules", "remove", "sch_nope", "--slug", "clocked", "--root", str(root))
    assert result.returncode == EXIT_CHECK_FAILED
    assert "no schedule entry matches" in result.stderr


def test_schedules_watch_fires_a_due_entry_and_pauses_after_a_failure(tmp_path):
    """The refusal the whole feature is built around, driven from the CLI.

    A one-shot entry set to the immediate past is due at the first tick. The goal it starts cannot
    run — there is no executor the provider could build for a bare objective here — so the fire fails,
    the entry pauses itself, and the watcher says why. `--ticks 3` keeps the test finite while leaving
    room for a second fire to be possible: if the no-re-arm rule were missing, it would happen.
    """
    root = tmp_path / "projects"
    workspace = _workspace(tmp_path, "clocked", root=root)
    _run("schedules", "add", "triage the overnight issues", "--due-now",
         "--slug", "clocked", "--root", str(root))

    result = _run("schedules", "watch", "--slug", "clocked", "--root", str(root),
                  "--interval", "5", "--ticks", "3")
    assert result.returncode == EXIT_OK, result.stderr[:400]
    # Whatever the fire produced, the entry is now paused or advanced — never left to fire twice
    # for one failure.
    view = json.loads(_run("--json", "schedules", "list", "--slug", "clocked",
                           "--root", str(root)).stdout)
    entry = view["entries"][0]
    assert entry["last_outcome"], "the watcher must record what the fire produced"
    if entry["last_outcome"] in PARKED_OUTCOMES:
        assert entry["enabled"] is False
        assert "never re-arms" in entry["disabled_reason"]
        assert "PAUSED" in result.stderr
    else:
        assert entry["enabled"] is False, "a one-shot retires after firing"


def test_schedules_watch_refuses_a_tick_outside_the_range_from_the_cli(tmp_path):
    root = tmp_path / "projects"
    _workspace(tmp_path, "clocked", root=root)
    result = _run("schedules", "watch", "--slug", "clocked", "--root", str(root),
                  "--interval", "0", "--ticks", "1")
    assert result.returncode == EXIT_USAGE
    assert "outside the permitted range" in result.stderr


def test_schedules_watch_json_emits_only_json_lines(tmp_path):
    """A watcher under `--json` is NDJSON: one object per line, so the stream stays parseable."""
    root = tmp_path / "projects"
    _workspace(tmp_path, "clocked", root=root)
    _run("schedules", "add", "a scheduled objective", "--every", "1h",
         "--slug", "clocked", "--root", str(root))
    result = _run("--json", "schedules", "watch", "--slug", "clocked", "--root", str(root),
                  "--interval", "5", "--ticks", "1")
    assert result.returncode == EXIT_OK, result.stderr[:400]
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines
    frames = [json.loads(line) for line in lines]
    assert frames[-1]["type"] == "summary"
    assert frames[-1]["ticks"] == 1


def test_schedules_enable_rearms_a_paused_entry_from_the_cli(tmp_path):
    root = tmp_path / "projects"
    workspace = _workspace(tmp_path, "clocked", root=root)
    store = ScheduleStore(workspace)
    entry = store.add(objective="triage", every="1h", now=1000.0)
    apply_fire(store, entry, {"outcome": "blocked", "detail": "no provider"}, now=1000.0 + 3600)
    assert store.entries[0].enabled is False

    result = _run("schedules", "enable", entry.id, "--slug", "clocked", "--root", str(root),
                  "--json")
    assert result.returncode == EXIT_OK, result.stderr[:400]
    payload = json.loads(result.stdout)
    assert payload["enabled"] is True
    assert payload["next_due_at"], "re-enabling sets a fresh due time rather than firing at once"
