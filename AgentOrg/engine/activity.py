#!/usr/bin/env python3
"""activity.py — one answer to "what is happening, and where is it going?".

WHY THIS EXISTS
---------------
The engine records *everything*: a run checkpoint, an append-only trace, diagnostics, the goal, the
task pool, child transcripts, proposals. What it did not have was one place that reads all of them and
says, in order and in plain words, what the org is doing, why it stopped, and what happens next.

That gap is not cosmetic. A person who sets a goal and comes back later saw a run that said
`pm = blocked / guardrail-blocked` with every other node `pending` and no explanation anywhere in the
product — so the honest summary of the experience was "nothing is happening and I do not know why".
The reason *was* recorded, in the checkpoint's own `log`: an edge guardrail refuses the payload before
a summary is ever attached to the node record, so the timeline — which read the trace and the engine's
diagnostics and not that log — carried the token and none of the sentence. Reading the checkpoint's
`log` beside those sources, and saying what the token means, is what closes it.

DESIGN
------
- **Derived, never stored.** This module writes nothing. It reads the artifacts the engine already
  produces — the checkpoint, the trace, the goal, the children, the proposals — and folds them into a
  timeline. A second store of "what happened" would be a second thing to keep true, and the engine
  already refuses that class of duplication.
- **Every source is optional.** A fresh workspace has no run, no trace and no goal; that is a normal
  answer ("nothing is running yet"), not an error. A missing or half-written file is skipped.
- **Bounded.** The trace is read tail-first and capped, so a long run yields a summary rather than a
  file dump — the same reason `LogStore` is a ring buffer.
- **One headline, one next action.** The two things a person wants first are "what is it doing now"
  and "what should I do" — so they are computed explicitly rather than left for the reader to infer
  from the timeline.
- **A stop is explained, in the engine's own words.** A node that did not finish gets its reason from
  its record's `summary`, or — when there is none, which is exactly the guardrail case — from the entry
  its own `log` carries, glossed by whatever the verdict token means. The reading of that log lives in
  `flow`, which builds the board's `blocked_by` from the same entries: two readings of the same actions
  would be two things to keep true, and one of them would drift.

Usage:
    from engine.activity import build_activity
    report = build_activity(workspace)            # reads everything from disk
    report = build_activity(workspace, run_status=status, goal_status=goal)  # reuse loaded state
    report["headline"]                            # "Waiting on you: approve the release"
    report["timeline"]                            # ordered entries, newest last
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# The reading of the checkpoint's `log` — which actions count as a cause, which statuses and verdicts
# mean a node stopped, what a verdict token means, and which command resolves a stop — lives in `flow`,
# where the board's `blocked_by` is built from the same entries. Importing it is deliberate: the board
# and this timeline describe one run, and a second reading of the same log actions would be a second
# answer to "why did it stop". `flow` imports nothing from this module, so this is a leaf.
from .flow import clip, is_stuck, recovery_command, stop_report, stop_words, why_stopped

__all__ = ["ActivityEntry", "build_activity", "ACTIVITY_VERSION"]

#: Bumped when the report's shape changes incompatibly, so a cached consumer can tell.
ACTIVITY_VERSION = "1.0.0"

#: How many trace lines to read from the tail by default. A run emits one line per model call, per
#: node, per tool step; a whole-file read on a long run is megabytes for a summary that needs the end.
DEFAULT_TRACE_TAIL = 400
#: The most timeline entries returned. Older history is summarised in `counts`, not listed.
DEFAULT_LIMIT = 120


@dataclass
class ActivityEntry:
    """One thing that happened, in the order it happened."""

    at: str
    kind: str                 # goal | plan | run | node | gate | swarm | subagent | proposal | budget
    title: str
    detail: str = ""
    tone: str = "info"        # info | good | warn | bad
    node_id: str = ""
    agent_id: str = ""
    ref: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, "kind": self.kind, "title": self.title, "detail": self.detail,
                "tone": self.tone, "node_id": self.node_id, "agent_id": self.agent_id,
                "ref": self.ref}


# ── path resolution ──────────────────────────────────────────────────────────

def _state_dir(workspace: Any) -> Path | None:
    """The `.agent_state/` a workspace points at, tolerating a Workspace, a path, or nothing."""
    if workspace is None:
        return None
    state = getattr(workspace, "state_dir", None)
    if state is not None:
        return Path(state)
    path = Path(workspace)
    if path.name == ".agent_state":
        return path
    return path / ".agent_state"


def _read_json(path: Path) -> dict[str, Any]:
    """Read one JSON object, returning {} for anything unreadable. Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    """The last `limit` JSON objects from a JSONL file, oldest first.

    Tail-first so a long trace does not cost a full read; a partial final line (a write in flight) is
    skipped rather than failing the whole read.
    """
    if limit <= 0:
        return []
    try:
        # `stat` first: a missing file is the common case and must be cheap.
        if not path.is_file():
            return []
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            # Read a bounded chunk from the end, then keep the last `limit` complete lines. 400 bytes
            # per record is generous for these events; the cap keeps memory flat on a big trace.
            chunk = min(size, max(64_000, limit * 400))
            handle.seek(size - chunk)
            blob = handle.read().decode("utf-8", errors="replace")
        lines = blob.splitlines()
        # Drop a leading partial line when we did not read from the start.
        if chunk < size and lines:
            lines = lines[1:]
        records: list[dict[str, Any]] = []
        for line in lines[-limit * 2:]:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
        return records[-limit:]
    except OSError:
        return []


# ── the sources ──────────────────────────────────────────────────────────────

def _trace_entries(records: Iterable[dict[str, Any]]) -> list[ActivityEntry]:
    """Turn trace/event records into timeline entries, keeping only the ones a reader cares about.

    A trace line per model call is noise in a timeline; a line per node transition, gate, swarm and
    goal change is the story. This is a deliberate filter, not an oversight.
    """
    entries: list[ActivityEntry] = []
    for record in records:
        etype = str(record.get("type") or "")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        at = str(record.get("ts") or "")
        node_id = str(record.get("node_id") or "")
        agent_id = str(record.get("agent_id") or "")
        entry: ActivityEntry | None = None
        if etype == "manifest.proposed":
            nodes = payload.get("nodes") or []
            entry = ActivityEntry(at, "plan", "graph proposed",
                                  f"{len(nodes)} node(s): {', '.join(str(n) for n in nodes[:10])}",
                                  tone="info")
        elif etype == "manifest.approved":
            entry = ActivityEntry(at, "plan", "graph approved", "the run may execute", tone="good")
        elif etype == "run.start":
            entry = ActivityEntry(at, "run", "run started",
                                  str(payload.get("workflow") or ""), tone="info")
        elif etype == "run.end":
            outcome = str(payload.get("outcome") or payload.get("state") or "")
            ok = payload.get("ok")
            gated = payload.get("gated")
            tone = "good" if ok else ("warn" if gated else "bad")
            entry = ActivityEntry(at, "run", f"run ended: {outcome or payload.get('state')}",
                                  f"exit {payload.get('exit_code')}", tone=tone)
        elif etype == "run.paused":
            entry = ActivityEntry(at, "run", "run paused", "", tone="warn")
        elif etype == "run.resumed":
            entry = ActivityEntry(at, "run", "run resumed", "from the checkpoint", tone="info")
        elif etype in ("node.enter", "node.exit"):
            entry = ActivityEntry(at, "node",
                                  f"{'entering' if etype == 'node.enter' else 'left'} {node_id}",
                                  "", tone="info", node_id=node_id, agent_id=agent_id)
        elif etype == "human.gate":
            entry = ActivityEntry(at, "gate", f"gate reached: {node_id}",
                                  str(payload.get("reason") or "")[:120], tone="warn")
        elif etype == "human.decision":
            entry = ActivityEntry(at, "gate",
                                  f"decision: {'approved' if payload.get('approved') else 'rejected'} "
                                  f"{payload.get('gate_id') or ''}".strip(),
                                  str(payload.get("note") or "")[:120], tone="good")
        elif etype == "goal.armed":
            entry = ActivityEntry(at, "goal", "goal armed",
                                  str(payload.get("objective") or "")[:120], tone="good")
        elif etype == "goal.completed":
            entry = ActivityEntry(at, "goal", "goal completed",
                                  str(payload.get("summary") or "")[:120], tone="good")
        elif etype == "goal.blocked":
            entry = ActivityEntry(at, "goal", "goal blocked",
                                  str(payload.get("reason") or "")[:120], tone="bad")
        elif etype == "goal.paused":
            entry = ActivityEntry(at, "goal", "goal paused",
                                  str(payload.get("reason") or "")[:120], tone="warn")
        elif etype == "goal.resumed":
            entry = ActivityEntry(at, "goal", "goal resumed", "", tone="info")
        elif etype == "goal.cleared":
            entry = ActivityEntry(at, "goal", "goal cleared", "", tone="info")
        elif etype.startswith("swarm.") or etype.startswith("fanout."):
            entry = ActivityEntry(at, "swarm", etype.replace(".", " "),
                                  str(payload.get("item") or payload.get("detail") or "")[:120],
                                  tone="info")
        elif etype.startswith("subagent."):
            title = {
                "subagent.spawned": "subagent spawned",
                "subagent.done": "subagent done",
                "subagent.failed": "subagent failed",
                "subagent.progress": "subagent progress",
                "subagent.read": "subagent read back",
            }.get(etype, etype.replace(".", " "))
            tone = "bad" if etype == "subagent.failed" else (
                "good" if etype == "subagent.done" else "info")
            entry = ActivityEntry(at, "subagent", title,
                                  str(payload.get("child_id") or payload.get("task") or "")[:120],
                                  tone=tone, agent_id=agent_id)
        elif etype == "review.rejected":
            entry = ActivityEntry(at, "node", f"review rejected: {node_id}",
                                  str(payload.get("reason") or "")[:120], tone="warn",
                                  node_id=node_id)
        elif etype == "review.approved":
            entry = ActivityEntry(at, "node", f"review passed: {node_id}", "", tone="good",
                                  node_id=node_id)
        elif etype in ("cost.ceiling", "budget.burn"):
            entry = ActivityEntry(at, "budget", etype.replace(".", " "),
                                  json.dumps(payload, default=str)[:120], tone="warn")
        if entry is not None:
            entries.append(entry)
    return entries


def _diagnostic_entries(records: Iterable[dict[str, Any]]) -> list[ActivityEntry]:
    """Diagnostics worth promoting into the timeline: blocks, escalations, guardrail trips."""
    interesting = ("guardrail.blocked", "run.phase", "run.prepared", "run.approved",
                   "escalate", "trailer.repair.failed", "trailer.unparsable", "node.tools.end")
    entries: list[ActivityEntry] = []
    for record in records:
        event = str(record.get("event") or "")
        if event not in interesting:
            continue
        level = str(record.get("level") or "info")
        tone = {"warning": "warn", "error": "bad", "critical": "bad"}.get(level, "info")
        # The message is the explanation; when it is empty the detail still carries the facts.
        message = str(record.get("message") or "").strip()
        detail = json.dumps(record.get("detail") or {}, default=str)[:120]
        entries.append(ActivityEntry(
            str(record.get("at") or ""), "diagnostic", event,
            message or detail, tone=tone,
            node_id=str(record.get("node_id") or ""),
            agent_id=str(record.get("agent_id") or "")))
    return entries


def _node_entries(nodes: dict[str, Any]) -> list[ActivityEntry]:
    """The current state of every node, whether or not the trace showed its transitions.

    The trace can be trimmed; the checkpoint's node table cannot. So the timeline always ends with the
    authoritative per-node state, even for a run whose trace has rolled past.
    """
    entries: list[ActivityEntry] = []
    for name, record in sorted(nodes.items()):
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "pending")
        verdict = str(record.get("verdict") or "")
        summary = str(record.get("summary") or "").strip()
        tone = {"done": "good", "blocked": "bad", "failed": "bad",
                "needs_review": "warn", "pending": "info"}.get(status, "info")
        title = f"{name}: {status}" + (f" ({verdict})" if verdict else "")
        entries.append(ActivityEntry("", "node", title, clip(summary, 160), tone=tone, node_id=name))
    return entries


def _stop_entries(stops: list[dict[str, Any]]) -> list[ActivityEntry]:
    """Why a stopped node stopped, from the checkpoint's own `log`.

    This is the source the timeline was missing, not a new one: the reason for a guardrail block is
    written *here* and nowhere else — the edge refuses the payload before a summary is attached to the
    node record, and the engine's diagnostics do not carry it. So a story built from the trace and the
    diagnostics could show that a run ended and never why, which is the complaint this module exists
    for.

    Only entries for a node that is *still* stopped are promoted (`flow.stop_report` decides that), so
    this is a handful of lines rather than the run's whole log.
    """
    return [
        ActivityEntry(
            str(stop.get("at") or ""), "node",
            f"{stop['node']}: {stop.get('word', '')}".strip().rstrip(":"),
            clip(str(stop.get("detail") or stop.get("why") or ""), 200),
            tone=str(stop.get("tone") or "warn"), node_id=str(stop.get("node") or ""))
        for stop in stops
    ]


def _control_fired(stops: list[dict[str, Any]], run: dict[str, Any]) -> bool:
    """Whether a safety control stopped this run, so a goal may not release it.

    The engine's own rule, not a second one: `orchestrator._UNRELEASABLE_STOP_REASONS` names exactly
    `guardrail`, `contract` and `error` and refuses to let a goal release a run whose stop reason
    mentions one — autonomy decides that work is done, never that a control which fired was wrong. The
    log's cause actions are the per-node form of the same three.
    """
    controls = ("guardrail", "contract", "error")
    if any(str(stop.get("action") or "") in controls for stop in stops):
        return True
    reason = str(run.get("stop_reason") or "").lower()
    return any(word in reason for word in controls)


def _subagent_entries(workspace: Any, run_id: str) -> list[dict[str, Any]]:
    """Every child the run started, as reference frames. Empty when there are none."""
    try:
        from .subagents import ChildStore

        store = ChildStore(workspace, run_id=run_id or "run")
        return [ref.as_dict() for ref in store.children()]
    except Exception:  # noqa: BLE001 - a missing child store is "no children"
        return []


def _proposal_entries(state_dir: Path | None) -> list[dict[str, Any]]:
    """Proposals waiting on the Owner, newest first, plus how many were refused."""
    if state_dir is None:
        return []
    directory = state_dir / "proposals"
    if not directory.is_dir():
        return []
    proposals: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if data:
            data["file"] = str(path.with_suffix(".md"))
            proposals.append(data)
    proposals.sort(key=lambda p: str(p.get("at") or ""), reverse=True)
    return proposals


# ── the headline and the next action ─────────────────────────────────────────

def _stuck_nodes(nodes: dict[str, Any], log: Any = None) -> dict[str, str]:
    """The nodes that stopped short, in the order the checkpoint lists them, each with its reason.

    The rule for "stopped short" is `flow.is_stuck`, the *same* predicate the board counts and headlines
    with — one reading of a status and a verdict, so the report and the board cannot disagree about
    which node is the stopped one. It is why a *finished* node is never stuck here whatever verdict it
    still carries: the runner keeps a released gate's `awaiting_owner`, and reporting an approved gate as
    stuck is the false positive `orchestrator._detect_gate` was fixed for.

    The reason comes from `flow.why_stopped`, which reads the node's `summary` and then the entry its own
    `log` carries — the guardrail case, where there is no summary because the payload was refused before
    one could be attached.
    """
    stuck: dict[str, str] = {}
    for name, record in nodes.items():
        if not isinstance(record, dict):
            continue
        if is_stuck(str(record.get("status") or "pending"), str(record.get("verdict") or "")):
            stuck[str(name)] = why_stopped(str(name), record, log)
    return stuck


def _headline(run: dict[str, Any], goal: dict[str, Any], nodes: dict[str, Any],
              workspace_name: str, stuck: dict[str, str] | None = None) -> str:
    """One line: what is the org doing *right now*."""
    phase = str(run.get("phase") or "idle")
    running = bool(run.get("running"))
    stop = str(run.get("stop_reason") or "")
    gate = run.get("gate") if isinstance(run.get("gate"), dict) else None

    if gate is not None:
        return f"Waiting on you: {clip(str(gate.get('reason') or gate.get('gate_id') or 'a gate'), 110)}"
    # A parked plan is waiting on the same person for the same reason a gate is — nothing proceeds
    # until they answer — and it had no sentence here at all, so the run read as `awaiting_approval —
    # no active work`, which is a state rather than an ask. The wording matches the next action's
    # (`_next_action`), so the headline and the step under it say one thing.
    if phase == "awaiting_approval":
        return "Waiting on you: approve the parked plan"
    if stop and phase not in ("done",):
        return f"Stopped — {clip(stop, 150)}"
    # A stopped node outranks "running", because a run with a node that cannot advance will not finish,
    # and that node is the one thing a person can act on. It sits *after* the run's own `stop_reason`
    # because that sentence is the engine's summary of the whole run, and where it exists it already
    # names the cause.
    if stuck:
        node = next(iter(stuck))
        return f"{node} is stuck — {clip(stuck[node] or 'no reason recorded', 160)}"
    if running:
        done = sum(1 for r in nodes.values()
                   if isinstance(r, dict) and r.get("status") in ("done", "skipped"))
        current = next((n for n, r in nodes.items()
                        if isinstance(r, dict) and r.get("status") == "in_progress"), None)
        where = f" (on {current})" if current else ""
        return f"Running {phase}{where} — {done} of {len(nodes)} node(s) done"
    if str(run.get("run_id") or ""):
        return f"Run {phase} — {str(run.get('outcome', {}).get('outcome') or 'no active work')}"
    if goal.get("objective"):
        state = str(goal.get("state") or "cleared")
        if goal.get("live"):
            return f"Goal armed — the loop will continue: {str(goal['objective'])[:100]}"
        return f"Goal {state}: {str(goal['objective'])[:100]}"
    return f"Nothing is running in {workspace_name}. Set a goal to start."


#: What *performing* a next action asks of the surface that shows it, keyed by the kind — or absent,
#: which means no surface performs it and the action's `command` is how a person takes it.
#:
#: The engine names the action; whether a console may carry it out itself is the surface's problem, and
#: the one thing that must not happen is the surface answering that by listing the kinds: it did exactly
#: that (`macos/Sources/AgentOrgKit/Spine.swift`, `NextLine.canPerform`), and a Swift list of engine
#: kinds goes stale silently every time a kind is added — the day `retry` appeared the app had no
#: opinion about it until someone widened the switch. So the answers live here, beside the kinds they
#: describe, and travel with the action.
#:
#: `""` means nothing beyond the engine's own report: the surface has a real destination for it (a hire
#: form, the goal's own resume, the composer's start, the runs list) and can offer to do it. `"gate"`
#: means the offer is the person's only while the gate is still theirs to answer — the one condition a
#: surface holds and this function cannot see, because the checkpoint says a gate is waiting but not
#: whether the engine has since answered it itself or the goal's posture lets it. A kind absent from the
#: table is one no surface performs, which is the safe reading of a kind this build has not seen.
_NEXT_PERFORMABLE: dict[str, str] = {
    "decide": "gate",
    # A parked plan has a real destination on a surface: the console's plan card carries the control
    # (`approve_plan`) and the terminal has `run --approve-plan`. Like `decide`, the control itself
    # lives where the plan is drawn rather than in a generic row, so `""` here does not mean a second
    # button is added anywhere.
    "approve_plan": "",
    "hire": "",
    "investigate": "",
    "resume": "",
    "start": "",
}


def _action(kind: str, label: str, detail: str, command: str) -> dict[str, Any]:
    """One next action, with the surface question answered rather than left to be re-derived.

    `performable` is the answer where it is unconditional; `needs` names what is still required when it
    is not. Both are always present, so a surface reads two fields instead of enumerating the kinds —
    and a kind added here tomorrow is handled by whatever surface reads it, without a second edit.
    """
    needs = _NEXT_PERFORMABLE.get(kind)
    return {
        "kind": kind, "label": label, "detail": detail, "command": command,
        "performable": needs == "",
        "needs": needs or "",
    }


def _next_action(run: dict[str, Any], goal: dict[str, Any],
                 staffing: list[dict[str, Any]], stuck: dict[str, str] | None = None,
                 state_dir: Path | None = None) -> dict[str, Any]:
    """The one thing the Owner should do next, if anything.

    Ordered by urgency: a gate is a decision only they can take; so is a parked plan, which nothing has
    executed yet; an unstaffed node is a hire; a stopped run is a reason to look; a node that cannot
    advance is a *retry*, because the work was produced and only a new attempt can satisfy the edge that
    refused it; then an armed-but-idle goal is a resume. An empty result means there is genuinely
    nothing to do.

    The retry's command is derived from the state by `flow.recovery_command`, which is the one place
    that knows which verbs the engine will actually accept — so this can never point a person at a
    command that comes back `no run found`.
    """
    gate = run.get("gate") if isinstance(run.get("gate"), dict) else None
    if gate is not None:
        return _action("decide", f"Decide gate {gate.get('gate_id')!r}",
                       clip(str(gate.get("reason") or ""), 200),
                       f"engine.cli decide --slug {run.get('slug', '')} --approve --note \"...\"")
    # A parked plan is the whole run: nothing has executed, and approving it is the one decision that
    # starts it — the route `run --approve-plan` and the console's own `approve_plan` both take. It used
    # to be reported as `none`, because an `awaiting_approval` run has no gate, no stop and no staffing
    # gap for the branches below to catch — so the one phase whose whole meaning is "waiting for the
    # Owner" was the one phase with nothing to say. That is how a plan parked in a project nobody had
    # open became a run that no surface could find and no surface could act on.
    if str(run.get("phase") or "") == "awaiting_approval":
        plan = run.get("plan") if isinstance(run.get("plan"), dict) else {}
        manifest = plan.get("manifest") if isinstance(plan.get("manifest"), dict) else {}
        steps = [str(n.get("id")) for n in (manifest.get("nodes") or [])
                 if isinstance(n, dict) and n.get("id")]
        detail = (f"the graph is parked with {len(steps)} step(s): {', '.join(steps[:8])}"
                  if steps else "a plan is parked, waiting for your approval")
        return _action("approve_plan", "Approve the parked plan and run it", detail,
                       f"engine.cli run --approve-plan --slug {run.get('slug', '')}")
    if staffing:
        return _action("hire", f"Hire for {len(staffing)} unstaffed capabilit(ies)",
                       "; ".join(str(g.get("skill")) for g in staffing[:6]),
                       str((staffing[0] or {}).get("hire") or ""))
    stop = str(run.get("stop_reason") or "")
    if stop:
        return _action("investigate", "Investigate why the run stopped", clip(stop, 200),
                       f"engine.cli status --slug {run.get('slug', '')}")
    if stuck:
        node = next(iter(stuck))
        recovery = recovery_command(str(run.get("slug") or ""), run, state_dir, node=node)
        if recovery["command"]:
            return _action("retry", f"Re-run the graph so {node} gets another attempt",
                           stuck[node] or recovery["why"], recovery["command"])
    if goal.get("objective") and not goal.get("live") and goal.get("open"):
        return _action("resume", "Resume the goal to continue",
                       str(goal.get("pause_reason") or "paused"), "engine.cli goal resume")
    if goal.get("objective") and not run.get("run_id"):
        return _action("start", "Start a run for this goal",
                       clip(str(goal.get("objective")), 200), "engine.cli run --goal \"...\"")
    return _action("none", "Nothing needs you", "", "")


# ── the report ───────────────────────────────────────────────────────────────

def _same_event(a: ActivityEntry, b: ActivityEntry) -> bool:
    """Whether two adjacent entries describe the same event.

    The engine relays some transitions from two producers (its own bus and the executing runner's
    host), so the raw stream carries identical neighbours — one carrying no detail and one carrying
    the workflow. Timestamps differ by milliseconds, so the comparison is on kind/title rather than on
    `at`, and an empty detail is treated as compatible with a populated one.
    """
    if a.kind != b.kind or a.title != b.title or a.node_id != b.node_id:
        return False
    return not (a.detail and b.detail) or a.detail == b.detail


def build_activity(workspace: Any, *, run_status: dict[str, Any] | None = None,
                   goal_status: dict[str, Any] | None = None,
                   org: Any = None, staffing: list[dict[str, Any]] | None = None,
                   trace_tail: int = DEFAULT_TRACE_TAIL,
                   limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Assemble the activity report for a workspace.

    Parameters
    ----------
    workspace:
        A `Workspace`, a `.agent_state` path, or a project path. Everything is read from here.
    run_status, goal_status:
        Optional already-loaded status, so the caller (the serve loop) does not read twice.
    org:
        Optional roster, used only to name the agent behind a node when one is bound.
    staffing:
        Optional staffing gaps to fold into the next-action decision.
    trace_tail, limit:
        Bounds on how much history is read and returned.
    """
    state_dir = _state_dir(workspace)
    workspace_name = str(getattr(workspace, "display_name", "") or
                         getattr(workspace, "slug", "") or
                         (Path(str(workspace)).name if workspace else "this workspace"))

    run = dict(run_status or {})
    if not run and state_dir is not None:
        run = _read_json(state_dir / "run_state.json")
    goal = dict(goal_status or {})
    if not goal and state_dir is not None:
        goal = _read_json(state_dir / "goal.json")
    # A goal read straight from disk carries the stored document, which has no derived `live`/`open`
    # booleans (those come from `Goal.public`). Derive them here so a goal read from a checkpoint and
    # one read from a live orchestrator render identically — otherwise "continues" would read False
    # for an armed goal whenever the CLI read the file rather than the orchestrator.
    if goal and "live" not in goal:
        state = str(goal.get("state") or "cleared")
        goal["live"] = state == "armed"
        goal.setdefault("open", state in ("armed", "paused"))

    # A run loaded from disk has the checkpoint's shape; normalise the two fields the headline reads.
    run_id = str(run.get("run_id") or "")
    nodes = run.get("outcome", {}).get("nodes") if isinstance(run.get("outcome"), dict) else None
    if not isinstance(nodes, dict):
        nodes = run.get("nodes") if isinstance(run.get("nodes"), dict) else {}
    for key in ("phase", "running", "stop_reason", "gate", "slug", "staffing_gaps"):
        run.setdefault(key, run.get(key) or (False if key == "running" else ""))

    # Agent names, so a node can be shown as "Sana (code-reviewer)" rather than by id.
    agent_names: dict[str, str] = {}
    skill_holders: dict[str, list[str]] = {}
    if org is not None:
        try:
            for spec in getattr(org, "agents", {}).values():
                agent_names[str(getattr(spec, "id", ""))] = str(getattr(spec, "name", ""))
                for skill in (getattr(spec, "skills", None) or []):
                    skill_holders.setdefault(str(skill), []).append(str(getattr(spec, "name", "")))
        except Exception:  # noqa: BLE001 - a roster we cannot read is only a naming nicety
            pass

    # ── gather the sources ───────────────────────────────────────────────────
    timeline: list[ActivityEntry] = []
    if state_dir is not None:
        timeline += _trace_entries(_read_jsonl_tail(state_dir / "trace.jsonl", trace_tail))
        timeline += _diagnostic_entries(
            _read_jsonl_tail(state_dir / "diagnostics.jsonl", trace_tail))

    proposals = _proposal_entries(state_dir)
    children = _subagent_entries(workspace, run_id)
    for child in children:
        status = str(child.get("status") or "running")
        tone = {"done": "good", "failed": "bad", "running": "info"}.get(status, "info")
        timeline.append(ActivityEntry(
            str(child.get("updated_at") or child.get("created_at") or ""), "subagent",
            f"subagent {status}: {str(child.get('task') or child.get('child_id') or '')[:80]}",
            clip(str(child.get("preview") or ""), 160), tone=tone,
            agent_id=str(child.get("agent_id") or "")))

    # A proposal is work waiting on a person, so it belongs in the story.
    for proposal in proposals:
        timeline.append(ActivityEntry(
            str(proposal.get("at") or ""), "proposal",
            f"proposal: {str(proposal.get('title') or proposal.get('id') or '')[:80]}",
            clip(str(proposal.get("summary") or proposal.get("finding") or ""), 160), tone="warn",
            ref=str(proposal.get("file") or "")))

    # Why a node stopped, from the checkpoint's own `log` — the source the timeline was missing. It sits
    # before the per-node state so the story reads "then this was refused" and closes with what the run
    # looks like now, and it carries no timestamp when the runner wrote none (the runner's entries have
    # none), which pins it with the current-state entries at the end — where it belongs, since a stop
    # that is still in force is current state rather than history.
    stops = stop_report(nodes, run.get("log"))
    timeline += _stop_entries(stops)

    # The authoritative per-node state closes the timeline, so it is never stale.
    timeline += _node_entries(nodes)

    # ── order ────────────────────────────────────────────────────────────────
    # Entries with a timestamp sort by it; the node-state entries (no timestamp) are pinned to the
    # end, because they are the *current* state rather than an event in the past.
    def _sort_key(pair: tuple[int, ActivityEntry]) -> tuple[int, str]:
        index, entry = pair
        if not entry.at:
            return (1, f"{index:08d}")
        return (0, entry.at)

    ordered = [entry for _i, entry in sorted(enumerate(timeline), key=_sort_key)]

    # Collapse adjacent duplicates. The engine records the same transition from two places — a
    # `run.start` on the bus and the same event relayed from the runner's host, for instance — so the
    # raw stream carries near-identical neighbours. A timeline that repeats itself reads as noise.
    deduped: list[ActivityEntry] = []
    for entry in ordered:
        if deduped and _same_event(deduped[-1], entry):
            continue
        deduped.append(entry)
    ordered = deduped
    counts_kinds: dict[str, int] = {}
    for entry in ordered:
        counts_kinds[entry.kind] = counts_kinds.get(entry.kind, 0) + 1

    # ── counts ───────────────────────────────────────────────────────────────
    done = sum(1 for r in nodes.values()
               if isinstance(r, dict) and r.get("status") in ("done", "skipped"))
    blocked = [name for name, r in nodes.items()
               if isinstance(r, dict) and r.get("status") == "blocked"]
    in_flight = [name for name, r in nodes.items()
                 if isinstance(r, dict) and r.get("status") in ("in_progress", "running")]
    counts = {
        "nodes": len(nodes),
        "done": done,
        "blocked": len(blocked),
        "in_flight": len(in_flight),
        "pending": max(0, len(nodes) - done - len(blocked) - len(in_flight)),
        "swarms": counts_kinds.get("swarm", 0),
        "subagents": len(children),
        "subagents_running": sum(1 for c in children if str(c.get("status")) == "running"),
        "proposals": len(proposals),
        "timeline": len(ordered),
    }

    staffing = staffing if staffing is not None else list(run.get("staffing_gaps") or [])
    stuck = _stuck_nodes(nodes, run.get("log"))
    headline = _headline(run, goal, nodes, workspace_name, stuck)
    next_action = _next_action(run, goal, staffing, stuck=stuck, state_dir=state_dir)

    # Where it is going: the phase and, when a goal is armed, the fact that it will continue.
    #
    # `continues` is not `goal.live` on its own. A guardrail block or a contract violation is a safety
    # control *firing*, and the engine refuses to let a goal release one
    # (`orchestrator._UNRELEASABLE_STOP_REASONS`) — so a report that said "continues" over a refused node
    # was telling a person to wait for something the engine had already decided will not happen, which is
    # the same "nothing is happening and I do not know why" this module exists to end.
    control_fired = _control_fired(stops, run)
    going = {
        "phase": str(run.get("phase") or "idle"),
        "live": bool(goal.get("live")),
        "continues": bool(goal.get("live")) and not control_fired,
        "phase_note": ("a guardrail or contract control stopped this run, and a goal may not release "
                       "one" if control_fired
                       else str(goal.get("pause_reason") or "") if not goal.get("live") else ""),
    }

    return {
        "activity_version": ACTIVITY_VERSION,
        "workspace": workspace_name,
        "run_id": run_id,
        "headline": headline,
        "objective": str(goal.get("objective") or run.get("goal") or ""),
        "phase": str(run.get("phase") or "idle"),
        "running": bool(run.get("running")),
        "stop_reason": str(run.get("stop_reason") or ""),
        "gate": run.get("gate") if isinstance(run.get("gate"), dict) else None,
        "goal": {
            "state": str(goal.get("state") or "cleared"),
            "live": bool(goal.get("live")),
            "pause_reason": str(goal.get("pause_reason") or ""),
            "spend": goal.get("spend") if isinstance(goal.get("spend"), dict) else {},
        },
        "going": going,
        "next_action": next_action,
        # The wording for every stop token this build knows. `stop_reason` above is sometimes a bare
        # verdict token rather than a sentence (`orchestrator._derive_stop_reason` glosses most, not
        # all), and this is what lets the surface that renders it say what *this* engine means by the
        # token instead of keeping its own copy of the sentence — see `flow.stop_words`.
        "stop_words": stop_words(),
        "counts": counts,
        "staffing_gaps": [dict(g) for g in staffing],
        "nodes": [
            {"node": name, **{k: v for k, v in (record or {}).items() if k != "node"}}
            for name, record in sorted(nodes.items()) if isinstance(record, dict)
        ],
        "blocked_nodes": blocked,
        "skill_holders": skill_holders,
        "agents": agent_names,
        "subagents": children,
        "proposals": proposals,
        "timeline": [entry.as_dict() for entry in ordered[-limit:]],
    }
