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
from engine.flow import NEXT_SEP, build_flow, stop_report, why_stopped
from engine.state import Workspace

#: The gloss `flow._STOP_WORDS` gives the `guardrail-blocked` token — what it means, as opposed to the
#: token itself. Spelled out here so the assertion tests the *contract* rather than restating the code.
_GLOSS = "the work finished, but what it handed on was refused at the edge — a contract failure, " \
         "not a crash"


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


def test_a_parked_plan_makes_the_next_action_an_approval(workspace):
    """The one phase whose whole meaning is "waiting for the Owner" used to say nothing.

    An `awaiting_approval` run has no gate, no stop and no staffing gap, so every branch of
    `_next_action` fell through and the run read as `none` — the same "nothing is happening and I do
    not know why" this module exists to end, in the one state a person *must* act in for anything to
    happen at all. `serve._cmd_status` offered the plan card; the CLI's `activity` closed with no
    `Next:` line, and a roll-up across projects had no way to find the run either.
    """
    state = {
        "run_id": "run_3", "slug": "health-endpoint", "phase": "awaiting_approval",
        "goal": "add a health endpoint",
        "plan": {"manifest": {"nodes": [{"id": "pm"}, {"id": "api"}]}, "validation": {"valid": True}},
        "outcome": {"nodes": {}},
    }
    _write(workspace.state_dir, "run_state.json", json.dumps(state))

    report = build_activity(workspace)
    assert report["headline"] == "Waiting on you: approve the parked plan"
    action = report["next_action"]
    assert action["kind"] == "approve_plan"
    assert action["command"] == "engine.cli run --approve-plan --slug health-endpoint"
    # The step list comes from the checkpoint's own plan, unnamed by this module.
    assert "pm" in action["detail"] and "api" in action["detail"]
    # The console has a real destination for it — its plan card carries the control — so the engine
    # says a surface can offer it, rather than pointing a person at a command.
    assert action["performable"] is True and action["needs"] == ""


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


# ── a stuck node explains itself, on both surfaces the Owner has ──────────────
#
# The reported state, built here rather than read from a project, because the defect is a *shape*: a
# node record with no `summary` — an edge guardrail refuses the payload before one is ever attached —
# and a `log` entry that carries the reason. Both surfaces used to print `guardrail-blocked` and stop:
# the board read only the record's `summary`, which is empty, and the timeline read the trace and the
# diagnostics, which never carried the reason at all. Nothing but the shape matters to the reading, so
# the shape is what this builds.

#: The runner's own log action for the report's case, verbatim from
#: `projects/console/.agent_state/run_state.json`.
_GUARDRAIL_REASON = ("the payload is missing summary. A receiver that cannot see the status or a summary "
                     "cannot tell what it was given.")


def _guardrail_state(slug: str = "activity-probe") -> dict:
    return {
        "run_id": "run_9", "slug": slug, "workflow": slug, "phase": "escalated",
        "nodes": {"pm": {"status": "blocked", "iterations": 1, "verdict": "guardrail-blocked"},
                  "architect": {"status": "pending", "iterations": 0}},
        "log": [{"step": 1, "node": "pm", "action": "guardrail", "detail": _GUARDRAIL_REASON}],
    }


def _guardrail_workspace(workspace, **overrides) -> None:
    """The state on disk, plus the manifest a recovery command has to be able to name.

    The goal is armed, as it is on the real project this came from: that is what makes "will the loop
    continue?" a question worth answering rather than one that is already false.
    """
    _write(workspace.state_dir.parent, "activity-probe.yaml", "name: activity-probe\n")
    _write(workspace.state_dir, "run_state.json", json.dumps({**_guardrail_state(), **overrides}))
    _write(workspace.state_dir, "goal.json", json.dumps({
        "goal_version": "1.0.0", "objective": "build the console", "state": "armed"}))


def test_the_board_reads_a_guardrail_blocks_reason_from_the_log(workspace):
    _guardrail_workspace(workspace)
    board = build_flow(workspace)
    row = next(r for r in board["rows"] if r["node_id"] == "pm")
    assert "missing summary" in row["blocked_by"], "the log's sentence, not the token"
    assert row["blocked_by"] != row["verdict"], "the token alone is not the answer"
    # The token is still there, and it is *said* rather than repeated: what it means is that the node
    # finished its work and the hand-off refused what it produced.
    assert row["verdict"] == "guardrail-blocked"
    assert "refused at the edge" in row["blocked_by"]
    assert "missing summary" in board["headline"]
    assert board["counts"]["stuck"] == 1


def test_a_recorded_summary_still_wins_over_the_log(workspace):
    """The log is a fallback, not a replacement: the node's own sentence is the better answer."""
    _guardrail_workspace(workspace, nodes={
        "pm": {"status": "needs_review", "verdict": "contract-violation",
               "summary": "declared criteria not covered: c1"},
    }, log=[{"step": 2, "node": "pm", "action": "contract",
             "detail": "declared criteria not covered: c1, c2"}])
    board = build_flow(workspace)
    row = next(r for r in board["rows"] if r["node_id"] == "pm")
    assert row["blocked_by"] == "declared criteria not covered: c1"
    assert "c1" in board["headline"] and "c2" not in board["headline"]


def test_activity_says_why_and_what_next_for_a_guardrail_block(workspace):
    _guardrail_workspace(workspace)
    report = build_activity(workspace)
    assert "pm is stuck" in report["headline"]
    assert "missing summary" in report["headline"]
    # The reason reaches the timeline from the checkpoint's own log — the source it was missing.
    assert any("missing summary" in (e["detail"] or "") for e in report["timeline"])
    assert any(e["node_id"] == "pm" and e["tone"] == "bad" for e in report["timeline"])
    # And the next action is a command that applies, not a fixed suggestion.
    assert report["next_action"]["kind"] == "retry"
    assert "run --slug activity-probe --manifest" in report["next_action"]["command"]
    assert "missing summary" in report["next_action"]["detail"]
    # A goal may not release a safety control that fired, so the report must not claim it will — even
    # though the goal on disk is armed, which is the state that makes this claim worth checking.
    assert report["goal"]["live"] is True
    assert report["going"]["continues"] is False


def test_the_next_line_names_the_recovery_and_is_the_same_sentence_in_json(workspace):
    _guardrail_workspace(workspace)
    board = build_flow(workspace)
    command, why = board["next"].split(NEXT_SEP, 1)
    assert command.startswith("engine.cli run --slug activity-probe --manifest ")
    assert why, "the line carries the reason it is the next move"
    # The verbs that look like recoveries are refused for a run whose state is the *runner's*
    # checkpoint: `reassign`, `takeover` and `abort` all answer `no run found`, so none may be offered.
    for refused in ("reassign", "takeover", "abort"):
        assert refused not in board["next"]


def test_no_next_move_is_offered_when_the_manifest_cannot_be_named(workspace):
    """A command naming a file this workspace does not have is the refusal to come, not a next move."""
    _write(workspace.state_dir, "run_state.json", json.dumps(_guardrail_state()))
    board = build_flow(workspace)
    assert board["next"] == ""
    assert "missing summary" in board["headline"], "the reason is still said"
    assert build_activity(workspace)["next_action"]["kind"] != "retry"


# ── the vocabulary: what counts as a cause, and what a token means ────────────


def test_only_the_stopping_actions_are_read_as_a_cause(workspace):
    state = _guardrail_state()
    state["log"] = [
        {"step": 1, "node": "pm", "action": "contract-warning", "detail": "outputs not produced"},
        {"step": 2, "node": "pm", "action": "guardrail", "detail": _GUARDRAIL_REASON},
    ]
    assert why_stopped("pm", state["nodes"]["pm"], state["log"]) == \
        f"{_GLOSS}: {_GUARDRAIL_REASON}"
    # A warning never stops a node, so it is not a cause — and the newest cause for the node wins.
    assert "outputs not produced" not in why_stopped("pm", state["nodes"]["pm"], state["log"])
    assert [stop["action"] for stop in stop_report(state["nodes"], state["log"])] == ["guardrail"]


def test_a_stop_for_a_node_that_is_no_longer_stopped_is_not_promoted(workspace):
    """The timeline is not a log dump: only a stop that is still in force belongs on it."""
    state = _guardrail_state()
    state["nodes"]["pm"] = {"status": "done", "verdict": "ok", "summary": "the PRD"}
    _write(workspace.state_dir, "run_state.json", json.dumps(state))
    assert stop_report(state["nodes"], state["log"]) == []
    report = build_activity(workspace)
    assert not any("refused at the edge" in e["title"] for e in report["timeline"])
    assert "is stuck" not in report["headline"]


def test_a_released_gate_is_not_counted_as_stuck(workspace):
    """The runner keeps a gate's `awaiting_owner` verdict after a release; done means done."""
    _write(workspace.state_dir, "run_state.json", json.dumps({
        "nodes": {"release": {"status": "done", "verdict": "awaiting_owner"}}}))
    assert build_flow(workspace)["counts"]["stuck"] == 0


def test_the_guardrail_verdict_alone_still_counts_as_stuck(workspace):
    """The app counts `verdict == "guardrail-blocked"`; the engine must not drop such a node."""
    _write(workspace.state_dir, "run_state.json", json.dumps({
        "nodes": {"pm": {"status": "pending", "verdict": "guardrail-blocked"}}}))
    board = build_flow(workspace)
    assert board["counts"]["stuck"] == 1
    assert next(r for r in board["rows"] if r["node_id"] == "pm")["tone"] == "bad"


def test_an_adopted_runs_node_list_does_not_break_the_board(workspace):
    """The orchestrator writes `outcome.nodes` as a *list of ids* on an adopted run.

    Reading that list as a table raised `'list' object has no attribute 'get'`, so a board that must
    never error on an unfamiliar shape errored on one the engine itself writes. The nodes it can name —
    here, the ones the plan declares — are the honest answer.
    """
    _write(workspace.state_dir, "run_state.json", json.dumps({
        "run_id": "run_1", "slug": "activity-probe", "phase": "awaiting_approval",
        "outcome": {"nodes": ["pm", "dev"]},
        "plan": {"nodes": [{"id": "pm", "skill": "product-manager"},
                           {"id": "dev", "skill": "backend-developer"}]},
    }))
    board = build_flow(workspace)
    assert [row["node_id"] for row in board["rows"]] == ["pm", "dev"]
    assert board["counts"]["waiting"] == 2


# ── whether a surface can perform the next action ────────────────────────────


def test_the_next_action_says_whether_a_surface_can_perform_it(workspace):
    """The app used to answer this itself, from a Swift list of the engine's kinds.

    `SpineModel.NextLine.canPerform` switched over `_next_action`'s kinds — `decide`, `hire`,
    `investigate`, `retry`, `resume`, `start`, `none` — so every kind the engine gained was a kind the
    app silently had no opinion about until someone widened the switch. `retry` was the one that proved
    it. The engine now answers with the action: `performable`, and `needs` for the one condition it
    cannot see for itself.

    Driven through the states rather than asserted as a table, because the *states* are what the engine
    resolves — a gate, a staffing gap, a stop, a paused goal, an idle one — and a table written here
    would be the second list this change removes.
    """
    from engine.activity import _next_action

    def action(run=None, goal=None, staffing=None, stuck=None):
        return _next_action(run or {}, goal or {}, staffing or [], stuck=stuck)

    cases = [
        ({"gate": {"gate_id": "release", "reason": "Owner release approval"}, "slug": "s"},
         {}, [], "decide", False, "gate"),
        ({}, {}, [{"skill": "ceo-strategist", "hire": "engine.cli hire <name> --skill x"}],
         "hire", True, ""),
        ({"stop_reason": "pm: refused at the edge", "slug": "s"}, {}, [],
         "investigate", True, ""),
        ({}, {"objective": "keep going", "live": False, "open": True}, [], "resume", True, ""),
        ({}, {"objective": "keep going"}, [], "start", True, ""),
        ({}, {}, [], "none", False, ""),
    ]
    for run, goal, staffing, kind, performable, needs in cases:
        result = action(run, goal, staffing)
        assert result["kind"] == kind, result
        assert result["performable"] is performable, (
            f"{kind}: the engine said performable={result['performable']}")
        assert result["needs"] == needs, f"{kind}: the engine said needs={result['needs']!r}"

    # `retry` needs a recovery command that actually applies, so it is driven through the report the
    # board and the app both read.
    _guardrail_workspace(workspace)
    retry = build_activity(workspace)["next_action"]
    assert retry["kind"] == "retry"
    assert retry["performable"] is False, "no `serve` command re-runs a graph"
    assert retry["needs"] == "", "and nothing would make it performable later"


def test_a_kind_the_engine_has_no_answer_for_is_not_performable():
    """The safe reading of a kind this build has not seen: show the command, offer no button.

    Derived from the engine's own table rather than restating it: what is asserted is the *relationship*
    (`needs` empty means performable, anything else does not) and that a kind outside the table is
    carried with an honest "no". A future kind that a surface *can* do is then a one-line engine change
    plus nothing at all in Swift — which is the point of sending this at all.
    """
    from engine.activity import _NEXT_PERFORMABLE, _action

    assert _NEXT_PERFORMABLE, "the engine must answer for the kinds it knows"
    for kind, needs in _NEXT_PERFORMABLE.items():
        result = _action(kind, "a label", "a detail", "a command")
        assert result["kind"] == kind
        assert result["needs"] == needs
        assert result["performable"] is (needs == ""), kind

    unknown = _action("a-kind-from-a-later-engine", "a label", "a detail", "engine.cli whatever")
    assert unknown["performable"] is False
    assert unknown["needs"] == ""
    assert unknown["kind"] == "a-kind-from-a-later-engine", "the kind itself is never dropped"
    assert unknown["command"] == "engine.cli whatever", "so the fallback line still says what to run"


# ── the stop vocabulary travels ──────────────────────────────────────────────


def test_both_reports_carry_the_engines_own_wording_for_a_stop(workspace):
    """One token, one sentence — and it is the engine's, sent, rather than rewritten in Swift.

    The app glossed `guardrail-blocked` and `contract-violation` with its own copy of the engine's
    sentences, worded identically *on purpose* so one token would not be described two ways on one
    screen. Two homes kept in step by hand is one home too many: the copy is the one that goes stale,
    and nothing would have caught it. So the table travels in the reports that render tokens — the
    board's rows carry a bare `verdict`, and the Now pane shows the run's `stop_reason` — and the app
    decodes it (`StopWords`).

    Compare against `_STOP_WORDS` itself, so this cannot become a second copy of the wording.
    """
    from engine.flow import _STOP_WORDS, stop_words

    vocabulary = stop_words()
    assert vocabulary == _STOP_WORDS, "the accessor answers with the engine's table"
    assert vocabulary is not _STOP_WORDS, "a copy, so a caller cannot edit the vocabulary for everyone"
    vocabulary["a-token-from-a-test"] = "edited"
    assert "a-token-from-a-test" not in _STOP_WORDS

    _guardrail_workspace(workspace)
    board = build_flow(workspace)
    activity = build_activity(workspace)
    assert board["stop_words"] == _STOP_WORDS
    assert activity["stop_words"] == _STOP_WORDS
    # The token a row carries is one the vocabulary answers for, which is what the app glosses from.
    row = next(r for r in board["rows"] if r["node_id"] == "pm")
    assert _STOP_WORDS[row["verdict"]] == _GLOSS
    # And a token the engine does not know is absent rather than invented — the app keeps the token.
    assert "awaiting_owner" not in board["stop_words"], (
        "the engine has no sentence for it; the app's own gloss for that state is the app's")
