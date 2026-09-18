#!/usr/bin/env python3
"""Phase 22 tests — the activity timeline: one answer to "what is happening?".

The engine already records a run checkpoint, a trace, diagnostics, a goal, children and proposals.
What it lacked was one place that reads all of them and says, in order and in plain words, what the
org is doing, why it stopped, and what the Owner should do next.

So these tests assert the *story*, not the plumbing: a blocked run yields a reason, a run waiting at
a gate yields a "waiting on you" headline, and a fresh workspace yields a calm "nothing is running"
rather than an error.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.activity import build_activity
from engine.state import Workspace


@pytest.fixture
def workspace(tmp_path):
    ws = Workspace.for_project("activity-probe", root=tmp_path / "projects")
    ws.ensure()
    return ws


def _write(state_dir: pathlib.Path, name: str, text: str) -> None:
    (state_dir / name).write_text(text, encoding="utf-8")


def _jsonl(*records: dict) -> str:
    return "\n".join(json.dumps(r) for r in records) + "\n"


# ── the empty case ───────────────────────────────────────────────────────────


def test_a_fresh_workspace_says_so_calmly(workspace):
    """No run, no goal, no trace — that is a normal answer, not an error."""
    report = build_activity(workspace)
    assert "Nothing is running" in report["headline"]
    assert report["timeline"] == []
    assert report["next_action"]["kind"] == "none"
    assert report["counts"]["nodes"] == 0


# ── the reported failure: a run that stopped with no explanation ──────────────


def test_a_blocked_run_explains_why(workspace):
    """The Ideas failure: `pm = blocked / guardrail-blocked`, everything else pending, no reason."""
    state = {
        "run_id": "run_1", "slug": "console", "phase": "awaiting_human",
        "goal": "use the CEO skill",
        "stop_reason": "pm: a hand-off payload was blocked by the edge guardrail",
        "outcome": {"outcome": "guardrail-block", "nodes": {
            "pm": {"status": "blocked", "verdict": "guardrail-blocked",
                   "summary": "instruction-shaped phrase"},
            "architect": {"status": "pending", "verdict": None},
        }},
    }
    _write(workspace.state_dir, "run_state.json", json.dumps(state))

    report = build_activity(workspace)
    assert "Stopped" in report["headline"]
    assert "guardrail" in report["headline"]
    assert report["stop_reason"]
    assert report["counts"]["blocked"] == 1
    assert "pm" in report["blocked_nodes"]
    assert report["next_action"]["kind"] == "investigate"
    # The blocked node's own words are in the timeline.
    assert any("pm: blocked" in e["title"] for e in report["timeline"])
    assert any("instruction-shaped phrase" in (e["detail"] or "") for e in report["timeline"])


def test_a_gate_makes_the_next_action_a_decision(workspace):
    state = {
        "run_id": "run_2", "slug": "console", "phase": "awaiting_gate",
        "gate": {"gate_id": "release", "kind": "human", "reason": "Owner release approval",
                 "requires": ["change"], "present": ["change"]},
        "outcome": {"nodes": {"dev": {"status": "done", "verdict": "ok"}}},
    }
    _write(workspace.state_dir, "run_state.json", json.dumps(state))
    report = build_activity(workspace)
    assert report["headline"].startswith("Waiting on you")
    assert "release" in report["headline"]
    assert report["next_action"]["kind"] == "decide"
    assert "decide" in report["next_action"]["command"]


# ── the timeline ─────────────────────────────────────────────────────────────


def test_the_timeline_reads_the_trace_and_diagnostics(workspace):
    _write(workspace.state_dir, "trace.jsonl", _jsonl(
        {"seq": 1, "type": "manifest.proposed", "payload": {"nodes": ["pm", "dev"]},
         "ts": "2026-01-01T00:00:01Z"},
        {"seq": 2, "type": "manifest.approved", "payload": {}, "ts": "2026-01-01T00:00:02Z"},
        {"seq": 3, "type": "run.start", "payload": {"workflow": "w"}, "ts": "2026-01-01T00:00:03Z"},
        {"seq": 4, "type": "run.end", "payload": {"outcome": "complete", "ok": True},
         "ts": "2026-01-01T00:00:09Z"},
    ))
    _write(workspace.state_dir, "diagnostics.jsonl", _jsonl(
        {"event": "guardrail.blocked", "level": "warning", "message": "blocked a secret",
         "at": "2026-01-01T00:00:05Z", "detail": {"category": "secret"}},
    ))
    report = build_activity(workspace)
    titles = [e["title"] for e in report["timeline"]]
    assert "graph proposed" in titles
    assert "graph approved" in titles
    assert "run started" in titles
    assert any("run ended" in t for t in titles)
    # The guardrail diagnostic is promoted with its explanation.
    assert any(e["kind"] == "diagnostic" and "blocked a secret" == e["detail"]
               for e in report["timeline"])


def test_adjacent_duplicate_events_are_collapsed(workspace):
    """The engine relays some transitions twice; a timeline that repeats itself reads as noise."""
    _write(workspace.state_dir, "trace.jsonl", _jsonl(
        {"seq": 1, "type": "run.start", "payload": {}, "ts": "2026-01-01T00:00:01Z"},
        {"seq": 2, "type": "run.start", "payload": {}, "ts": "2026-01-01T00:00:01Z"},
    ))
    report = build_activity(workspace)
    starts = [e for e in report["timeline"] if e["title"] == "run started"]
    assert len(starts) == 1


def test_the_timeline_is_bounded(workspace):
    records = [{"seq": i, "type": "node.enter", "node_id": f"n{i}", "payload": {},
                "ts": f"2026-01-01T00:00:{i:02d}Z"} for i in range(50)]
    _write(workspace.state_dir, "trace.jsonl", _jsonl(*records))
    report = build_activity(workspace, limit=5)
    assert len(report["timeline"]) == 5


def test_a_malformed_trace_line_is_skipped_not_fatal(workspace):
    _write(workspace.state_dir, "trace.jsonl",
           '{"seq":1,"type":"run.start","payload":{},"ts":"2026-01-01T00:00:01Z"}\n'
           'not json at all\n'
           '{"seq":2,"type":"run.end","payload":{"outcome":"complete"},"ts":"2026-01-01T00:00:02Z"}\n')
    report = build_activity(workspace)
    assert any(e["title"] == "run started" for e in report["timeline"])


# ── the goal ─────────────────────────────────────────────────────────────────


def test_an_armed_goal_read_from_disk_reports_it_continues(workspace):
    """A goal read from `goal.json` has no derived `live`; activity must derive it, not read False."""
    _write(workspace.state_dir, "goal.json", json.dumps({
        "goal_version": "1.0.0", "objective": "capture the market", "state": "armed",
        "token_budget": 0, "spend": {"rounds": 2, "tokens": 10, "cost_usd": 0.0},
    }))
    report = build_activity(workspace)
    assert report["goal"]["live"] is True
    assert report["going"]["continues"] is True
    assert "armed" in report["headline"]


def test_a_paused_goal_suggests_a_resume(workspace):
    _write(workspace.state_dir, "goal.json", json.dumps({
        "goal_version": "1.0.0", "objective": "keep going", "state": "paused",
        "pause_reason": "manual",
    }))
    report = build_activity(workspace)
    assert report["goal"]["live"] is False
    assert report["next_action"]["kind"] == "resume"


def test_a_staffing_gap_becomes_a_hire_action(workspace):
    _write(workspace.state_dir, "run_state.json", json.dumps({
        "run_id": "run_3", "slug": "s", "phase": "ready",
        "staffing_gaps": [{"node_id": "ceo", "skill": "ceo-strategist",
                           "reason": "no agent in the roster holds this skill",
                           "hire": "engine.cli hire <name> --skill ceo-strategist"}],
        "outcome": {"nodes": {}},
    }))
    report = build_activity(workspace)
    assert report["next_action"]["kind"] == "hire"
    assert "ceo-strategist" in report["next_action"]["detail"]
    assert "hire" in report["next_action"]["command"]


# ── names and rosters ────────────────────────────────────────────────────────


class _StubOrg:
    class _A:
        def __init__(self, aid, name, skills):
            self.id, self.name, self.skills = aid, name, skills

    def __init__(self):
        self.agents = {
            "ag_1": self._A("ag_1", "Sana", ["code-reviewer"]),
            "ag_2": self._A("ag_2", "Alice", ["backend-developer"]),
        }


def test_the_report_maps_skills_to_holders(workspace):
    report = build_activity(workspace, org=_StubOrg())
    assert report["skill_holders"]["code-reviewer"] == ["Sana"]
    assert report["agents"]["ag_2"] == "Alice"


def test_a_broken_roster_does_not_break_the_report(workspace):
    report = build_activity(workspace, org=object())  # no `agents` attribute at all
    assert "headline" in report
