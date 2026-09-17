#!/usr/bin/env python3
"""Phase 5 tests — memory, telemetry and diagnostics.

The emphasis is on the two guards that matter: recalled memory is *context, never instruction*, and a
diagnostics bundle refuses to carry a secret. Both fail invisibly if they fail at all.
"""

from __future__ import annotations

import json
import pathlib
import sys
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine.diagnostics import (
    BUNDLE_EXCLUDE,
    BUNDLE_INCLUDE,
    Diagnostics,
    DiagnosticsError,
    correlation,
    read_log,
)
from engine.memory import (
    CONTEXT_ONLY_LABEL,
    MAX_RECALL_CHARS,
    MemoryEntry,
    MemoryError,
    MemoryStore,
    memory_entry_from_state,
)
from engine.telemetry import (
    SamplingPolicy,
    Span,
    SpanExporter,
    load_spans,
    naming,
)


# ── memory: writing and reading ──────────────────────────────────────────────


def _entry(run_id: str = "run_1", **overrides) -> MemoryEntry:
    base = dict(
        workflow="booking", run_id=run_id, outcome="complete",
        task="build the booking API", skills=["backend-developer"],
        decisions=[{"gate": "auth", "choice": "argon2id", "rationale": "memory hardness"}],
        open_questions=["keep bcrypt for legacy rows?"],
        steps_used=12, tokens_used=3400, cost_usd=0.012,
    )
    base.update(overrides)
    return MemoryEntry(**base)


def test_memory_round_trips_through_a_file(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.write(_entry("run_1"))
    entries = store.read("booking", limit=10)
    assert len(entries) == 1
    assert entries[0].run_id == "run_1"
    assert entries[0].decisions[0]["choice"] == "argon2id"


def test_memory_returns_the_most_recent_up_to_the_limit(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    for i in range(6):
        store.write(_entry(f"run_{i}"))
    recent = store.read("booking", limit=3)
    assert [e.run_id for e in recent] == ["run_3", "run_4", "run_5"]


def test_memory_of_an_unknown_workflow_is_empty_not_an_error(tmp_path):
    """A first run has nothing to recall, which is not a failure."""
    assert MemoryStore(tmp_path / "memory").read("never-run") == []


def test_a_disabled_store_writes_nothing(tmp_path):
    store = MemoryStore(None)
    assert store.write(_entry()) is None
    assert store.read("booking") == []
    assert store.stats()["enabled"] is False


def test_memory_requires_a_workflow_name(tmp_path):
    """Without one the entry cannot be retrieved by the read path."""
    with pytest.raises(MemoryError, match="requires a workflow name"):
        MemoryStore(tmp_path / "memory").write(_entry(workflow=""))


def test_a_workflow_name_cannot_escape_the_memory_directory(tmp_path):
    """A traversal in a workflow name must not write outside the store."""
    store = MemoryStore(tmp_path / "memory")
    store.write(_entry(workflow="../../evil"))
    written = sorted(p.name for p in (tmp_path / "memory").glob("*"))
    assert written == ["..-..-evil.jsonl"], written
    assert not (tmp_path.parent / "evil.jsonl").exists()


def test_memory_tolerates_a_torn_final_line(tmp_path):
    directory = tmp_path / "memory"
    directory.mkdir()
    (directory / "booking.jsonl").write_text(
        json.dumps(_entry().as_dict()) + "\n" + '{"torn":')
    assert len(MemoryStore(directory).read("booking", limit=10)) == 1


def test_unmeasured_cost_stays_unknown_and_is_never_zero(tmp_path):
    """The cost convention holds here too: unknown is not the same as free."""
    store = MemoryStore(tmp_path / "memory")
    entry = _entry(cost_usd=None)
    store.write(entry)
    read_back = store.read("booking", limit=1)[0]
    assert read_back.cost_usd is None
    assert read_back.cost_known is False
    assert "cost unknown" in read_back.render()


def test_a_measured_zero_cost_reads_as_free(tmp_path):
    """A local model is a genuine zero, which is different from unmeasured."""
    entry = _entry(cost_usd=0.0)
    assert entry.cost_known is True
    assert "cost $0.0000" in entry.render()


def test_memory_entry_round_trips_through_a_dict():
    entry = _entry()
    restored = MemoryEntry.from_dict(entry.as_dict())
    assert restored.workflow == entry.workflow
    assert restored.decisions == entry.decisions
    assert restored.cost_usd == entry.cost_usd


def test_memory_entry_tolerates_an_unknown_field():
    data = _entry().as_dict()
    data["future_field"] = {"nested": True}
    assert MemoryEntry.from_dict(data).run_id == "run_1"


def test_stats_report_outcomes_per_workflow(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.write(_entry("a", outcome="complete"))
    store.write(_entry("b", outcome="escalated"))
    store.write(_entry("c", outcome="complete"))
    stats = store.stats()
    assert stats["workflows"]["booking"]["outcomes"] == {"complete": 2, "escalated": 1}
    assert stats["total_entries"] == 3


# ── memory: consolidation ────────────────────────────────────────────────────


def test_consolidation_folds_old_entries_into_a_count(tmp_path):
    """Counting rather than re-summarising is what makes memory drift-proof."""
    store = MemoryStore(tmp_path / "memory")
    for i in range(10):
        store.write(_entry(f"run_{i}"))
    result = store.consolidate("booking", keep=4)
    assert result["consolidated"] is True
    assert result["folded"] == 6

    entries = store.read("booking", limit=20)
    assert len(entries) == 5, "one summary plus four retained"
    summary = next(e for e in entries if e.outcome == "consolidated")
    assert summary.verdicts["folded_runs"] == 6
    assert summary.verdicts["outcomes"] == {"complete": 6}
    assert "not a summary" in summary.verdicts["note"]


def test_consolidation_is_a_no_op_below_the_ceiling(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.write(_entry())
    result = store.consolidate("booking", keep=10)
    assert result["consolidated"] is False
    assert "within the" in result["reason"]


def test_consolidation_preserves_an_unknown_cost_as_unknown(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    for i in range(5):
        store.write(_entry(f"run_{i}", cost_usd=None))
    store.consolidate("booking", keep=1)
    summary = next(e for e in store.read("booking", limit=10) if e.outcome == "consolidated")
    assert summary.cost_known is False


def test_consolidation_sums_a_known_cost(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    for i in range(5):
        store.write(_entry(f"run_{i}", cost_usd=0.01))
    store.consolidate("booking", keep=1)
    summary = next(e for e in store.read("booking", limit=10) if e.outcome == "consolidated")
    assert summary.cost_usd == pytest.approx(0.04)


def test_consolidation_on_a_disabled_store_reports_so(tmp_path):
    assert MemoryStore(None).consolidate("booking")["consolidated"] is False


# ── memory: recall and the poisoning guard ───────────────────────────────────


def test_recall_block_is_labelled_context_only(tmp_path):
    """An agent reading a prior run's output as an instruction would obey one bad run forever."""
    store = MemoryStore(tmp_path / "memory")
    store.write(_entry())
    block = store.context_block("booking", limit=3)
    assert CONTEXT_ONLY_LABEL in block
    assert "background, not directives" in block
    assert "verify anything you rely on" in block


def test_recall_block_is_empty_when_there_is_nothing_to_recall(tmp_path):
    """So a caller can concatenate it unconditionally."""
    assert MemoryStore(tmp_path / "memory").context_block("nothing-here") == ""


def test_recall_block_includes_decisions_and_costs(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.write(_entry())
    block = store.context_block("booking", limit=3)
    assert "argon2id" in block
    assert "memory hardness" in block
    assert "cost $0.0120" in block


def test_recall_block_is_bounded(tmp_path):
    """A recall block is part of the prompt, so an unbounded one eats the window it saves."""
    store = MemoryStore(tmp_path / "memory")
    for i in range(20):
        store.write(_entry(f"run_{i}", task="x" * 900, decisions=[
            {"gate": f"g{j}", "choice": "c", "rationale": "r" * 300} for j in range(8)]))
    block = store.context_block("booking", limit=20, max_chars=4000)
    assert len(block) <= MAX_RECALL_CHARS + 400, len(block)
    assert "omitted to keep recall within its token budget" in block


def test_recall_can_cross_workflows_by_skill(tmp_path):
    """A new workflow should benefit from what a related one learned."""
    store = MemoryStore(tmp_path / "memory")
    store.write(_entry("a", workflow="booking", skills=["backend-developer"]))
    store.write(_entry("b", workflow="other", skills=["qa-engineer"]))
    found = store.read_for_skills(["backend-developer"], limit=5)
    assert [e.run_id for e in found] == ["a"]


def test_recall_falls_back_to_skills_when_the_workflow_is_new(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.write(_entry("a", skills=["backend-developer"]))
    block = store.context_block("brand-new-workflow", skills=["backend-developer"], limit=3)
    assert CONTEXT_ONLY_LABEL in block


def test_memory_entry_from_run_state_reads_the_librarys_shape():
    """Consuming the runner's own run-state avoids a parallel record that could disagree."""
    state = {
        "status": "complete",
        "nodes": {"fixer": {"skill": "backend-developer", "status": "done", "verdict": "pass"}},
        "artifacts": {"change": {"type": "change", "path": "src/app.py", "sha": "a" * 64}},
        "budget": {"steps_used": 7},
        "usage": {"total_tokens": 1200, "cost_usd": 0.004},
        "decisions": [{"gate": "auth", "choice": "argon2id"}],
        "open_questions": [{"question": "legacy bcrypt?"}],
        "log": [{"action": "escalate"}, {"action": "guardrail"}],
    }
    entry = memory_entry_from_state(state, workflow="booking", run_id="run_9")
    assert entry.run_id == "run_9"
    assert entry.outcome == "complete"
    assert entry.skills == ["backend-developer"]
    assert entry.verdicts["fixer"]["verdict"] == "pass"
    assert entry.steps_used == 7
    assert entry.tokens_used == 1200
    assert entry.cost_usd == pytest.approx(0.004)
    assert entry.escalations == 1
    assert entry.guardrail_blocks == 1
    assert entry.artifacts[0]["path"] == "src/app.py"


def test_memory_entry_from_run_state_leaves_cost_unknown_without_usage():
    entry = memory_entry_from_state({"status": "complete", "log": []},
                                    workflow="w", run_id="r")
    assert entry.cost_usd is None
    assert entry.cost_known is False


# ── telemetry: naming contract ───────────────────────────────────────────────


def test_span_naming_uses_the_librarys_stable_names():
    """A pipeline built against the library's exporter must consume ours unchanged."""
    names = naming()
    assert names["session"] == "session.{workflow}"
    assert names["node"] == "workflow.{workflow}.node.{node}"


def test_span_names_are_built_from_the_patterns():
    exporter = SpanExporter()
    session = exporter.session_span(workflow="booking", outcome="complete")
    node = exporter.node_span(workflow="booking", node="fixer", status="done")
    assert session.name == "session.booking"
    assert node.name == "workflow.booking.node.fixer"


def test_all_spans_share_one_trace_id():
    exporter = SpanExporter(run_id="run_1")
    spans = [
        exporter.session_span(workflow="w", outcome="complete"),
        exporter.node_span(workflow="w", node="a", status="done"),
        exporter.rotation_span(agent="ag_1", index=2, trigger="capacity", reason="full",
                               saturation=0.9, attention_weight=0.4, constraints_carried=3),
        exporter.delegation_span(agent="ag_1", index=1, skill="s", kind="helper", tier="T0",
                                 approved=True),
        exporter.health_span(agent="ag_1", previous="healthy", current="quarantined",
                             reason="r", score=0.3),
    ]
    assert len({s.trace_id for s in spans}) == 1
    assert all(s.span_id for s in spans)


def test_span_ids_are_unique():
    exporter = SpanExporter()
    spans = [exporter.node_span(workflow="w", node=f"n{i}", status="done") for i in range(10)]
    assert len({s.span_id for s in spans}) == 10


def test_a_node_span_carries_the_skill_hash():
    """So 'which prompt produced this output?' is answerable from the span alone."""
    span = SpanExporter().node_span(workflow="w", node="reviewer", status="done",
                                    skill_hash="deadbeef")
    assert span.skill_hashes == {"reviewer": "deadbeef"}
    assert span.as_dict()["skill_hashes"]["reviewer"] == "deadbeef"


# ── telemetry: the honesty flags ─────────────────────────────────────────────


def test_an_unmeasured_node_span_reports_itself_as_unmeasured():
    """`usage_reported` and `cost_measured` distinguish 'spent nothing' from 'not measured'."""
    span = SpanExporter().node_span(workflow="w", node="n", status="done")
    payload = span.as_dict()
    assert payload["usage_reported"] is False
    assert payload["cost"]["measured"] is False
    assert payload["cost"]["usd"] is None
    assert payload["tokens"]["total"] is None


def test_a_measured_node_span_reports_real_figures():
    span = SpanExporter().node_span(workflow="w", node="n", status="done",
                                    tokens_prompt=1000, tokens_completion=200, cost_usd=0.004)
    payload = span.as_dict()
    assert payload["usage_reported"] is True
    assert payload["tokens"]["total"] == 1200
    assert payload["cost"]["measured"] is True
    assert payload["cost"]["usd"] == pytest.approx(0.004)


def test_a_zero_cost_is_measured_not_unknown():
    """A local model is genuinely free, which is a real figure rather than a missing one."""
    span = SpanExporter().node_span(workflow="w", node="n", status="done",
                                    tokens_prompt=100, tokens_completion=10, cost_usd=0.0)
    assert span.as_dict()["cost"]["usd"] == 0.0
    assert span.as_dict()["cost"]["measured"] is True


# ── telemetry: rollup vocabulary ─────────────────────────────────────────────


def test_sli_rollup_uses_the_librarys_metric_names():
    """So `skill-sli-report.py` stays a valid cross-check rather than a competing definition."""
    rollup = SpanExporter().sli_rollup()
    for key in ("runs", "complete", "escalated", "escalation_rate", "guardrail_blocks",
                "cost_per_success_usd", "cost_unreported_spans"):
        assert key in rollup


def test_rollup_counts_escalations_and_completion():
    exporter = SpanExporter()
    exporter.session_span(workflow="w", outcome="complete")
    exporter.session_span(workflow="w", outcome="escalated")
    rollup = exporter.sli_rollup()
    assert rollup["runs"] == 2
    assert rollup["complete"] == 1
    assert rollup["escalated"] == 1
    assert rollup["escalation_rate"] == pytest.approx(0.5)


def test_rollup_flags_unmeasured_nodes_rather_than_calling_them_free():
    exporter = SpanExporter()
    exporter.node_span(workflow="w", node="a", status="done")                    # unmeasured
    exporter.node_span(workflow="w", node="b", status="done", tokens_prompt=10,
                       tokens_completion=2, cost_usd=0.01)                        # measured
    rollup = exporter.sli_rollup()
    assert rollup["cost_measured_spans"] == 1
    assert rollup["cost_unreported_spans"] == 1
    assert "unknown, not zero" in exporter.summary()


def test_rollup_does_not_count_structural_spans_as_unmeasured():
    """A rotation or health span has no usage by nature, so it is not 'unreported'."""
    exporter = SpanExporter()
    exporter.rotation_span(agent="a", index=2, trigger="capacity", reason="r",
                           saturation=0.9, attention_weight=0.3, constraints_carried=1)
    exporter.health_span(agent="a", previous="healthy", current="degraded", reason="r", score=0.6)
    assert exporter.sli_rollup()["cost_unreported_spans"] == 0


def test_rollup_reports_cost_per_success():
    """The metric that matters: a cheap failing run is not cheap."""
    exporter = SpanExporter()
    exporter.session_span(workflow="w", outcome="complete")
    exporter.node_span(workflow="w", node="n", status="done", tokens_prompt=10,
                       tokens_completion=2, cost_usd=0.02)
    assert exporter.sli_rollup()["cost_per_success_usd"] == pytest.approx(0.02)


# ── telemetry: sampling ──────────────────────────────────────────────────────


def test_sampling_keeps_every_escalation():
    """100% on escalations is the library's policy: those are the spans worth having."""
    exporter = SpanExporter(sampling=SamplingPolicy(default=0.1, on_escalation=1.0))
    kept = sum(1 for i in range(20)
               if exporter.node_span(workflow="w", node=f"n{i}", status="escalated"))
    assert kept == 20


def test_sampling_keeps_every_health_transition():
    exporter = SpanExporter(sampling=SamplingPolicy(default=0.1, on_health_change=1.0))
    kept = sum(1 for i in range(10)
               if exporter.health_span(agent="a", previous="healthy", current="degraded",
                                       reason="r", score=0.5))
    assert kept == 10


def test_sampling_keeps_every_guardrail_trip():
    exporter = SpanExporter(sampling=SamplingPolicy(default=0.1, on_guardrail=1.0))
    kept = sum(1 for i in range(10)
               if exporter.node_span(workflow="w", node=f"g{i}", status="done",
                                     attributes={"guardrail_block": True}))
    assert kept == 10


def test_sampling_reduces_ordinary_spans_and_counts_what_it_dropped():
    """A quiet run must be distinguishable from a sampled one."""
    exporter = SpanExporter(sampling=SamplingPolicy(default=0.25))
    kept = sum(1 for i in range(40)
               if exporter.node_span(workflow="w", node=f"n{i}", status="done"))
    assert 0 < kept < 40
    assert exporter.stats()["dropped"] > 0


def test_sampling_is_deterministic():
    """A flaky sampling decision would make two traces impossible to compare."""
    def run():
        exporter = SpanExporter(sampling=SamplingPolicy(default=0.3))
        return [bool(exporter.node_span(workflow="w", node=f"n{i}", status="done"))
                for i in range(20)]
    assert run() == run()


def test_sampling_takes_the_most_generous_applicable_rate():
    policy = SamplingPolicy(default=0.1, on_escalation=0.5, on_guardrail=1.0)
    assert policy.rate_for(escalated=True) == 0.5
    assert policy.rate_for(escalated=True, guardrail=True) == 1.0


# ── telemetry: export ────────────────────────────────────────────────────────


def test_spans_are_written_as_jsonl_and_read_back(tmp_path):
    path = tmp_path / "spans.jsonl"
    exporter = SpanExporter(path=path, run_id="run_1")
    exporter.session_span(workflow="w", outcome="complete")
    exporter.node_span(workflow="w", node="n", status="done")
    exporter.close()
    spans = load_spans(path)
    assert len(spans) == 2
    assert spans[0]["name"] == "session.w"
    assert spans[0]["trace_id"]


def test_export_is_disabled_without_a_path():
    exporter = SpanExporter(path=None)
    assert exporter.session_span(workflow="w", outcome="complete") is not None
    assert exporter.stats()["enabled"] is False


def test_the_in_memory_tail_is_bounded(tmp_path):
    exporter = SpanExporter(tail_size=10)
    for i in range(40):
        exporter.node_span(workflow="w", node=f"n{i}", status="done")
    assert exporter.stats()["tail"] == 10
    assert exporter.stats()["emitted"] == 40


def test_tail_can_be_filtered_by_kind():
    exporter = SpanExporter()
    exporter.session_span(workflow="w", outcome="complete")
    exporter.node_span(workflow="w", node="n", status="done")
    assert len(exporter.tail(kind="session")) == 1
    assert len(exporter.tail(kind="node")) == 1
    assert len(exporter.tail()) == 2


def test_a_torn_span_line_is_skipped_on_read(tmp_path):
    path = tmp_path / "spans.jsonl"
    path.write_text(json.dumps({"name": "session.w", "trace_id": "t"}) + "\n" + '{"torn":')
    assert len(load_spans(path)) == 1


# ── diagnostics ──────────────────────────────────────────────────────────────


def test_log_records_carry_the_correlation_chain(tmp_path):
    """So a failure is one lookup rather than five greps and a guess."""
    diag = Diagnostics(run_id="run_1", state_dir=tmp_path)
    record = diag.info("node.enter", node_id="fixer", agent_id="ag_1", attempt=2, phase="BUILD")
    assert record.chain["run_id"] == "run_1"
    assert record.chain["node_id"] == "fixer"
    assert record.chain["agent_id"] == "ag_1"
    assert record.chain["attempt"] == 2
    assert record.chain["phase"] == "BUILD"


def test_the_run_id_is_stamped_automatically(tmp_path):
    diag = Diagnostics(run_id="run_1", state_dir=tmp_path)
    assert diag.info("anything").chain["run_id"] == "run_1"


def test_log_level_filters_lower_records(tmp_path):
    diag = Diagnostics(run_id="run_1", state_dir=tmp_path, level="warning")
    assert diag.info("ignored") is None
    assert diag.warning("kept") is not None


def test_log_records_are_redacted_on_the_way_in(tmp_path):
    """A record that once held a key is a leak regardless of what a reader later does."""
    diag = Diagnostics(run_id="run_1", state_dir=tmp_path)
    record = diag.info("llm.request", message="using sk-abcdefghijklmnopqrstuvwx",
                       detail={"auth": "Bearer abcdef1234567890xyz"})
    assert "sk-" not in record.message
    assert "[REDACTED]" in record.message
    assert "Bearer abc" not in json.dumps(record.detail)


def test_a_redacted_record_on_disk_is_also_clean(tmp_path):
    diag = Diagnostics(run_id="run_1", state_dir=tmp_path)
    diag.info("x", message="key sk-abcdefghijklmnopqrstuvwx")
    diag.close()
    text = (tmp_path / "diagnostics.jsonl").read_text()
    assert "sk-abcdefghijklmnopqrstuvwx" not in text


def test_the_log_persists_and_reads_back(tmp_path):
    diag = Diagnostics(run_id="run_1", state_dir=tmp_path)
    diag.info("a")
    diag.error("b", message="failed")
    diag.close()
    records = read_log(tmp_path / "diagnostics.jsonl")
    assert len(records) == 2
    assert records[1]["level"] == "error"


def test_a_torn_log_line_is_skipped(tmp_path):
    path = tmp_path / "diagnostics.jsonl"
    path.write_text('{"level":"info","event":"a","seq":1}\n{"torn":')
    assert len(read_log(path)) == 1


def test_the_log_tail_is_bounded(tmp_path):
    diag = Diagnostics(run_id="run_1", state_dir=tmp_path, tail_size=5)
    for i in range(20):
        diag.info(f"event_{i}")
    assert len(diag.tail(limit=100)) == 5


def test_render_tail_uses_a_fixed_field_order(tmp_path):
    """Two records must be comparable by eye."""
    diag = Diagnostics(run_id="run_1", state_dir=tmp_path)
    diag.info("node.enter", node_id="fixer")
    line = diag.render_tail(limit=1)
    assert line.index("run_id=") < line.index("node_id=")


def test_correlation_helper_omits_empty_fields():
    assert correlation(run_id="r", node_id="") == {"run_id": "r"}
    assert correlation() == {}


# ── diagnostics: health and bundle ───────────────────────────────────────────


def test_health_never_raises_and_reports_the_essentials(tmp_path):
    diag = Diagnostics(run_id="run_1", state_dir=tmp_path)
    health = diag.health()
    assert health["status"] == "ok"
    assert health["run_id"] == "run_1"
    assert "records" in health
    assert health["log_writable"] is True


def test_health_reports_a_missing_state_directory_rather_than_failing():
    diag = Diagnostics(run_id="run_1", state_dir="/definitely/not/a/directory")
    health = diag.health()
    assert health["status"] == "ok"
    assert health["state_dir_exists"] is False
    assert health["log_writable"] is False


def test_health_redacts_its_extra_block(tmp_path):
    diag = Diagnostics(run_id="run_1", state_dir=tmp_path)
    health = diag.health(extra={"key": "sk-abcdefghijklmnopqrstuvwx"})
    assert "sk-" not in json.dumps(health["extra"])


def test_a_bundle_is_assembled_with_health_environment_and_naming(tmp_path):
    state = tmp_path / ".agent_state"
    state.mkdir()
    (state / "trace.jsonl").write_text('{"type":"run.start"}\n')
    (state / "run_state.json").write_text('{"status":"running"}')
    diag = Diagnostics(run_id="run_1", state_dir=state)
    diag.info("run.start")

    target = tmp_path / "diag.zip"
    diag.bundle(target)
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert {"health.json", "environment.json", "naming.json", "trace.jsonl"} <= names
        assert json.loads(archive.read("health.json"))["status"] == "ok"
        # The naming contract travels with the bundle, so what produced it is documented.
        assert json.loads(archive.read("naming.json"))["node"] == "workflow.{workflow}.node.{node}"
        environment = json.loads(archive.read("environment.json"))
        assert "hostname" not in environment, "a shared bundle should not carry a hostname"


def test_a_bundle_refuses_to_carry_a_secret(tmp_path):
    """A bundle is meant to be handed to someone else, so it must be safe by construction."""
    state = tmp_path / ".agent_state"
    state.mkdir()
    (state / "trace.jsonl").write_text('{"note":"key sk-111122223333444455556666"}\n')
    diag = Diagnostics(run_id="run_1", state_dir=state)
    with pytest.raises(DiagnosticsError, match="refusing to bundle"):
        diag.bundle(tmp_path / "diag.zip")
    assert not (tmp_path / "diag.zip").exists(), "a refused bundle must not be left behind"


def test_the_leak_scan_works_on_a_single_file(tmp_path):
    """`rglob` on a file path yields nothing, which would make the check unable to fail.

    The bundle scan passes individual files, so a scan that silently returns no findings for a file
    would be a leak check that never fires.
    """
    from engine.config import scan_for_leaks

    clean = tmp_path / "clean.jsonl"
    clean.write_text('{"note":"nothing sensitive"}\n')
    assert scan_for_leaks(clean) == []

    leaky = tmp_path / "leaky.jsonl"
    leaky.write_text('{"note":"key sk-111122223333444455556666"}\n')
    findings = scan_for_leaks(leaky)
    assert len(findings) == 1, "a file scan must actually detect a leak"
    assert "sk-111122223333444455556666" not in str(findings), "the secret must not be echoed"


def test_credentials_are_never_in_the_bundle_include_list():
    assert "credentials.json" in BUNDLE_EXCLUDE
    assert "credentials.json" not in BUNDLE_INCLUDE


def test_a_bundle_with_no_state_directory_still_produces_health(tmp_path):
    diag = Diagnostics(run_id="run_1", state_dir=None)
    target = tmp_path / "diag.zip"
    diag.bundle(target)
    with zipfile.ZipFile(target) as archive:
        assert "health.json" in archive.namelist()


def test_the_bundle_records_what_it_included(tmp_path):
    state = tmp_path / ".agent_state"
    state.mkdir()
    (state / "trace.jsonl").write_text('{"type":"x"}\n')
    diag = Diagnostics(run_id="run_1", state_dir=state)
    diag.close()
    diag.bundle(tmp_path / "diag.zip")
    events = [r["event"] for r in diag.tail(limit=10)]
    assert "diagnostics.exported" in events
