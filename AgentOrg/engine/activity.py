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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

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
        entries.append(ActivityEntry("", "node", title, summary[:160], tone=tone, node_id=name))
    return entries


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

def _headline(run: dict[str, Any], goal: dict[str, Any], nodes: dict[str, Any],
              workspace_name: str) -> str:
    """One line: what is the org doing *right now*."""
    phase = str(run.get("phase") or "idle")
    running = bool(run.get("running"))
    stop = str(run.get("stop_reason") or "")
    gate = run.get("gate") if isinstance(run.get("gate"), dict) else None

    if gate is not None:
        return f"Waiting on you: {str(gate.get('reason') or gate.get('gate_id') or 'a gate')[:110]}"
    if stop and phase not in ("done",):
        return f"Stopped — {stop[:150]}"
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


def _next_action(run: dict[str, Any], goal: dict[str, Any],
                 staffing: list[dict[str, Any]]) -> dict[str, Any]:
    """The one thing the Owner should do next, if anything.

    Ordered by urgency: a gate or a block is work only they can unblock; a staffing gap is a hire; an
    armed-but-idle goal is a resume. An empty result means there is genuinely nothing to do.
    """
    gate = run.get("gate") if isinstance(run.get("gate"), dict) else None
    if gate is not None:
        return {"kind": "decide", "label": f"Decide gate {gate.get('gate_id')!r}",
                "detail": str(gate.get("reason") or "")[:200],
                "command": f"engine.cli decide --slug {run.get('slug', '')} --approve --note \"...\""}
    if staffing:
        return {"kind": "hire", "label": f"Hire for {len(staffing)} unstaffed capabilit(ies)",
                "detail": "; ".join(str(g.get("skill")) for g in staffing[:6]),
                "command": str((staffing[0] or {}).get("hire") or "")}
    stop = str(run.get("stop_reason") or "")
    if stop:
        return {"kind": "investigate", "label": "Investigate why the run stopped",
                "detail": stop[:200],
                "command": f"engine.cli status --slug {run.get('slug', '')}"}
    if goal.get("objective") and not goal.get("live") and goal.get("open"):
        return {"kind": "resume", "label": "Resume the goal to continue",
                "detail": str(goal.get("pause_reason") or "paused"),
                "command": "engine.cli goal resume"}
    if goal.get("objective") and not run.get("run_id"):
        return {"kind": "start", "label": "Start a run for this goal",
                "detail": str(goal.get("objective"))[:200],
                "command": "engine.cli run --goal \"...\""}
    return {"kind": "none", "label": "Nothing needs you", "detail": "", "command": ""}


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
            str(child.get("preview") or "")[:160], tone=tone,
            agent_id=str(child.get("agent_id") or "")))

    # A proposal is work waiting on a person, so it belongs in the story.
    for proposal in proposals:
        timeline.append(ActivityEntry(
            str(proposal.get("at") or ""), "proposal",
            f"proposal: {str(proposal.get('title') or proposal.get('id') or '')[:80]}",
            str(proposal.get("summary") or proposal.get("finding") or "")[:160], tone="warn",
            ref=str(proposal.get("file") or "")))

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
    headline = _headline(run, goal, nodes, workspace_name)
    next_action = _next_action(run, goal, staffing)

    # Where it is going: the phase and, when a goal is armed, the fact that it will continue.
    going = {
        "phase": str(run.get("phase") or "idle"),
        "live": bool(goal.get("live")),
        "continues": bool(goal.get("live")),
        "phase_note": str(goal.get("pause_reason") or "") if not goal.get("live") else "",
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
