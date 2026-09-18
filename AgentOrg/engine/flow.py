#!/usr/bin/env python3
"""flow.py — one board answering "who is working on what, and what crossed between them".

WHY THIS EXISTS
---------------
The engine already records every fact this needs — the run checkpoint names each node's agent, the
trace records every `node.bind`, `handoff.*` and `agent.spawn`, and the roster names the people. What
it did not have was one place that folds them into the picture a person actually asks for when running
several orgs: **which agent is on which piece of work, what information moved to whom, how far along it
is, and what came back.**

That is not the same question as `activity` (a chronological story) or `status` (a snapshot of one
run). It is a *board*: one row per unit of work, with its owner, its information flow and its state —
the thing a lead looks at to see whether the team is moving and where a handoff is stuck.

DESIGN
------
- **Derived, never stored.** Like `activity.py`, this module writes nothing. It reads the checkpoint,
  the trace and the roster and projects them. A second store of "who is doing what" would be a second
  thing to keep true, and the run checkpoint is already the authority.
- **The org is the frame.** A Flow is scoped to one org (its roster, its agents, its run). The
  `--org`/portfolio layer selects *which* org, so "which agent is working on what" is answerable per
  company and — through the fleet — across all of them at once.
- **Bounded.** The trace is read from the tail and capped, so a long run yields a board rather than a
  file dump — the same reason `LogStore` is a ring buffer.
- **Everything is optional.** A fresh workspace has no run, no trace and no bindings; the honest answer
  is an empty board with a reason, not an error.
- **Handoffs are first class.** The board does not flatten a handoff into a log line: each row carries
  what it received and what it sent on, because "the information moved from A to B and B did this with
  it" is the whole point of a typed handoff.

Usage:
    from engine.flow import build_flow
    board = build_flow(workspace, org=org)          # reads everything from disk
    board = build_flow(workspace, run_status=status, org=org)   # reuse loaded state
    board["rows"]                                   # one row per unit of work
    board["handoffs"]                               # the transfers, in order
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = ["FlowRow", "FlowHandoff", "build_flow", "FLOW_VERSION"]

#: Bumped when the board's shape changes incompatibly, so a cached consumer can tell.
FLOW_VERSION = "1.0.0"

#: How many trace lines to read from the tail. A run emits a line per model call, per node, per tool
#: step; a whole-file read on a long run is megabytes for a board that only needs the end.
DEFAULT_TRACE_TAIL = 800

#: The trace events that describe information crossing an agent boundary, in the order the state
#: machine moves. Used to build the handoff list without matching strings at every call site.
_HANDOFF_EVENTS = {
    "handoff.proposed": "proposed",
    "handoff.accepted": "accepted",
    "handoff.rejected": "rejected",
    "handoff.fulfilled": "fulfilled",
    "handoff.breached": "breached",
    "handoff.verified": "verified",
    "handoff.escalated": "escalated",
}

#: What each node status means for the board's tone, so "on track" and "stuck" are not a guess by the
#: UI. Keyed by the status the runner writes.
_STATUS_TONE = {
    "done": "good",
    "pass": "good",
    "running": "info",
    "working": "info",
    "pending": "muted",
    "queued": "muted",
    "needs_review": "warn",
    "awaiting_owner": "warn",
    "blocked": "bad",
    "failed": "bad",
}


@dataclass
class FlowHandoff:
    """One transfer of information between two agents.

    A handoff is the only thing that crosses a node boundary, so the board treats it as a first-class
    fact rather than a log line: *from*, *to*, what state it reached, and the payload's own status.
    """

    handoff_id: str
    state: str = "proposed"           # the last state observed
    from_agent: str = ""
    from_node: str = ""
    to_agent: str = ""
    to_node: str = ""
    summary: str = ""
    payload_status: str = ""
    artifacts: list[str] = field(default_factory=list)
    at: str = ""
    tone: str = "info"
    breaches: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id, "state": self.state,
            "from_agent": self.from_agent, "from_node": self.from_node,
            "to_agent": self.to_agent, "to_node": self.to_node,
            "summary": self.summary, "payload_status": self.payload_status,
            "artifacts": list(self.artifacts), "at": self.at, "tone": self.tone,
            "breaches": self.breaches,
        }


@dataclass
class FlowRow:
    """One unit of work: a node, the agent on it, its information flow and its progress."""

    node_id: str
    skill: str = ""
    agent_id: str = ""
    agent_name: str = ""
    title: str = ""
    status: str = "pending"
    verdict: str = ""
    tone: str = "muted"               # good | info | warn | bad | muted
    phase: str = ""
    iterations: int = 0
    summary: str = ""
    #: What this node *received* — the handoff that brought its inputs.
    received_from: str = ""
    received_summary: str = ""
    #: What this node *sent on* — the handoff it produced for the next node.
    sent_to: str = ""
    sent_summary: str = ""
    #: True when a reviewer judged this node's work and the verdict came back here ("handoff back").
    judged_by: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    #: Why the node is not done, when it is not — the runner's own words, never a guess.
    blocked_by: str = ""
    is_gate: bool = False
    gate_kind: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id, "skill": self.skill,
            "agent_id": self.agent_id, "agent_name": self.agent_name, "title": self.title,
            "status": self.status, "verdict": self.verdict, "tone": self.tone,
            "phase": self.phase, "iterations": self.iterations, "summary": self.summary,
            "received_from": self.received_from, "received_summary": self.received_summary,
            "sent_to": self.sent_to, "sent_summary": self.sent_summary,
            "judged_by": list(self.judged_by), "artifacts": list(self.artifacts),
            "blocked_by": self.blocked_by,
            "is_gate": self.is_gate, "gate_kind": self.gate_kind,
        }


# ── path resolution ──────────────────────────────────────────────────────────

def _state_dir(workspace: Any) -> Path | None:
    """The `.agent_state/` a workspace points at, tolerating a Workspace, a path, or nothing."""
    if workspace is None:
        return None
    state_dir = getattr(workspace, "state_dir", None)
    if state_dir:
        return Path(state_dir)
    path = getattr(workspace, "path", None) or workspace
    try:
        return Path(path) / ".agent_state"
    except TypeError:
        return None


def _read_json(path: Path | None) -> dict[str, Any]:
    """Read a JSON document, returning an empty dict for anything missing or half-written."""
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_trace(state_dir: Path | None, *, limit: int) -> list[dict[str, Any]]:
    """Tail-read the trace, skipping malformed lines so a torn write cannot break the board."""
    if state_dir is None:
        return []
    path = state_dir / "trace.jsonl"
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _read_diagnostics(state_dir: Path | None, *, limit: int) -> list[dict[str, Any]]:
    """Tail-read `diagnostics.jsonl`, which is where `node.bind` records the chosen agent.

    The binding decision is emitted as a diagnostic, not a trace event, because it is an engine fact
    rather than a model interaction. Reading it is what lets a row name its owner from the run itself
    when the checkpoint (written by the library's runner) does not carry bindings.
    """
    if state_dir is None:
        return []
    path = state_dir / "diagnostics.jsonl"
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


# ── agent resolution ─────────────────────────────────────────────────────────

def _run_context_path(workspace: Any, state_dir: Path | None) -> Path | None:
    """Where the orchestrator wrote what it decided for this run.

    `run-context.json` lives at the *project* root (one level above `.agent_state/`), which is where the
    executor subprocess is handed it. Reading it is what makes the board authoritative rather than
    inferred: it carries the binding per node, the reason for it, and the roster in force.
    """
    root = getattr(workspace, "path", None)
    if root:
        candidate = Path(root) / "run-context.json"
        if candidate.is_file():
            return candidate
    if state_dir is not None:
        candidate = state_dir.parent / "run-context.json"
        if candidate.is_file():
            return candidate
        # Some builds keep it inside the state directory.
        candidate = state_dir / "run-context.json"
        if candidate.is_file():
            return candidate
    return None


def _read_manifest(state_dir: Path | None, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """The manifest for this run, from the checkpoint's path or from the project folder."""
    path = str(checkpoint.get("manifest_path") or "")
    if path:
        document = _read_manifest_file(Path(path))
        if document:
            return document
    if state_dir is None:
        return {}
    project_root = state_dir.parent
    manifest_name = str(checkpoint.get("slug") or checkpoint.get("workflow") or "")
    for candidate in ([project_root / f"{manifest_name}.yaml"] if manifest_name else []):
        document = _read_manifest_file(candidate)
        if document:
            return document
    return {}


def _read_manifest_file(path: Path) -> dict[str, Any]:
    """Parse a Safe-YAML manifest, tolerating a missing file or an unreadable shim.

    The manifest is the plan, and the board needs it only to name each node's skill and phase. A parse
    failure is therefore not fatal: the board falls back to what the checkpoint records.
    """
    if not path or not path.is_file():
        return {}
    try:
        import importlib.util
        import sys as _sys

        # The library ships the Safe-YAML reader the whole engine uses; reuse it rather than adding a
        # second YAML parser whose edge cases would differ from the runner's.
        scripts = _library_scripts_dir()
        if scripts and str(scripts) not in _sys.path:
            _sys.path.insert(0, str(scripts))
        spec = importlib.util.spec_from_file_location(
            "_agentorg_safe_yaml_flow", scripts / "lib" / "safe_yaml.py")
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            parsed = module.parse(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                return parsed
    except Exception:  # noqa: BLE001 - a missing manifest is an empty board, not an error
        return {}
    return {}


def _library_scripts_dir() -> Path | None:
    """The Skills library's `scripts/` directory, resolved from the environment or the default root."""
    candidates = []
    env = os.environ.get("AGENTORG_SKILLS_ROOT")
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parent.parent.parent / "Skills")
    for base in candidates:
        scripts = base / "scripts"
        if (scripts / "lib" / "safe_yaml.py").is_file():
            return scripts
    return None


def _has_manifest_nodes(manifest_nodes: dict[str, Any]) -> bool:
    """Whether a manifest with real node declarations was found, as opposed to an empty or stub one."""
    return any(str(node_id) != "manifest" for node_id in manifest_nodes)


def _agent_index(org: Any) -> dict[str, dict[str, Any]]:
    """An `{agent_id: {name,title,model,state,team}}` index, so a row can name its owner.

    Built from whatever the caller hands us — an `Org`, a `roster_view()` dict, or nothing. A missing
    name is left empty rather than invented, because a board that guessed an owner would be worse than
    one that showed a gap.
    """
    out: dict[str, dict[str, Any]] = {}
    if org is None:
        return out
    # A live Org: iterate its agents.
    agents = getattr(org, "agents", None)
    if isinstance(agents, dict):
        for aid, spec in agents.items():
            out[str(aid)] = _agent_entry(aid, spec)
        return out
    # A roster view dict: `{"agents": [...], ...}` or a bare list.
    if isinstance(org, dict):
        entries = org.get("agents")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("id"):
                    out[str(entry["id"])] = _agent_entry(entry["id"], entry)
    return out


def _agent_entry(agent_id: Any, spec: Any) -> dict[str, Any]:
    """One agent's display fields, from an `AgentSpec` or a plain dict."""
    def field(name: str, default: str = "") -> str:
        if isinstance(spec, dict):
            value = spec.get(name)
        else:
            value = getattr(spec, name, None)
        if value is None or value == "":
            return default
        # `level` is an enum with a label; `state` may be an enum too.
        label = getattr(value, "label", None)
        return str(label if label is not None else value)

    state = ""
    if isinstance(spec, dict):
        state = str(spec.get("state") or "")
    else:
        runtime_state = getattr(spec, "state", None)
        state = str(getattr(runtime_state, "value", runtime_state) or "")
    return {
        "id": str(agent_id),
        "name": field("name", "unknown"),
        "title": field("title"),
        "provider": field("provider"),
        "model": field("model"),
        "team": field("team"),
        "level": field("level"),
        "state": state,
    }


# ── the board ────────────────────────────────────────────────────────────────

def build_flow(workspace: Any, *, run_status: dict[str, Any] | None = None,
               org: Any = None, limit: int = DEFAULT_TRACE_TAIL,
               org_name: str = "", org_id: str = "") -> dict[str, Any]:
    """Fold a run's checkpoint, trace and roster into the flow board.

    Parameters
    ----------
    workspace:
        The workspace (or its `.agent_state` directory) to read.
    run_status:
        An already-loaded `Orchestrator.status()` result, so the board agrees with the panels without
        re-reading. When omitted the checkpoint is read directly.
    org:
        The roster — a live `Org` or a `roster_view()` dict — used to name each row's owner.
    org_name, org_id:
        Identity of the org this board is scoped to, so a fleet can show several boards side by side
        and a person can tell which company a row belongs to.
    """
    state_dir = _state_dir(workspace)
    checkpoint = _read_json(state_dir / "run_state.json" if state_dir else None)
    status = run_status if isinstance(run_status, dict) and run_status else {}
    if not checkpoint and status:
        # `status()` spreads the checkpoint, so it is a valid source when the file is not readable.
        checkpoint = status
    trace = _read_trace(state_dir, limit=limit)
    diagnostics = _read_diagnostics(state_dir, limit=limit)
    agents = _agent_index(org)

    # The engine writes node results under `outcome.nodes`; the *library runner's* checkpoint (a
    # different file, and different shape) uses top-level `nodes`. Both are read, because a Flow board
    # that showed "No work is assigned yet" for a run that had just produced a PRD is worse than
    # showing nothing — it contradicts the run.
    nodes = checkpoint.get("nodes") or (checkpoint.get("outcome") or {}).get("nodes") or {}
    manifest_nodes = _manifest_nodes(checkpoint)
    # The run context is the orchestrator's own record of what it decided: the binding per node and the
    # roster it ran with. It is the richest source of "who is on what", so it is read first and the
    # checkpoint's bindings and the diagnostics fill anything it does not cover.
    context = _read_json(_run_context_path(workspace, state_dir))
    binding_index = _binding_index(context.get("bindings") or checkpoint.get("bindings") or {})
    _merge_binding_diagnostics(binding_index, diagnostics)
    # The live roster names today's agents; the roster recorded *in the run context* names the agents
    # that actually ran. A resumed run — or one whose roster has since been re-hired, minting new ids —
    # would otherwise show a column of blanks, so the run's own roster is merged in underneath, and the
    # live one wins on a shared id.
    run_agents = _agent_index(context.get("org") if isinstance(context.get("org"), dict) else None)
    for agent_id, entry in run_agents.items():
        agents.setdefault(agent_id, entry)
    if not _has_manifest_nodes(manifest_nodes):
        manifest_nodes = _manifest_nodes({"manifest": _read_manifest(state_dir, checkpoint)})

    handoffs = _handoffs(trace, agents)
    received, sent = _handoff_edges(handoffs)

    rows = _rows(nodes, manifest_nodes, binding_index, agents, received, sent, checkpoint)

    counts = _counts(rows)
    return {
        "flow_version": FLOW_VERSION,
        "org_id": org_id or str(checkpoint.get("org_id") or ""),
        "org_name": org_name,
        "run_id": str(checkpoint.get("run_id") or ""),
        "slug": str(checkpoint.get("slug") or ""),
        "goal": str(checkpoint.get("run_goal") or checkpoint.get("goal") or ""),
        "phase": str(checkpoint.get("phase") or "idle"),
        "headline": _headline(rows, handoffs, checkpoint),
        "rows": [row.as_dict() for row in rows],
        "handoffs": [h.as_dict() for h in handoffs],
        "counts": counts,
        "agents": sorted(agents.values(), key=lambda a: a["name"]),
    }


def _merge_binding_diagnostics(bindings: dict[str, dict[str, Any]],
                               diagnostics: list[dict[str, Any]]) -> None:
    """Fold `node.bind` diagnostics into the binding index, newest per node.

    The checkpoint's own bindings win when present (they are the orchestrator's decision); a `node.bind`
    diagnostic fills a node the checkpoint does not cover. Newest wins, so a resumed run shows the agent
    that actually ran most recently.
    """
    for record in diagnostics:
        if str(record.get("event") or "") != "node.bind":
            continue
        node_id = str(record.get("node_id") or "")
        agent_id = str(record.get("agent_id") or "")
        if not node_id or not agent_id:
            continue
        existing = bindings.get(node_id)
        if existing and existing.get("agent_id"):
            continue
        detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
        bindings[node_id] = {
            "agent_id": agent_id,
            "agent_name": "",
            "reason": str(detail.get("reason") or ""),
            "policy": str(detail.get("policy") or ""),
            "agents": [agent_id],
        }


def _manifest_nodes(checkpoint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The manifest's node declarations, so a row can name the skill even before a node runs."""
    manifest = checkpoint.get("manifest")
    if not isinstance(manifest, dict):
        # The checkpoint stores the plan's nodes directly under `plan` in some builds.
        plan = checkpoint.get("plan")
        manifest = plan if isinstance(plan, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for node in manifest.get("nodes") or []:
        if isinstance(node, dict) and node.get("id"):
            out[str(node["id"])] = node
    for gate in manifest.get("gates") or []:
        if isinstance(gate, dict) and gate.get("id"):
            out[str(gate["id"])] = {**gate, "type": "gate"}
    return out


def _binding_index(bindings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`{node_id: {agent_id, agent_name, reason}}` from the checkpoint's bindings."""
    out: dict[str, dict[str, Any]] = {}
    for node_id, binding in (bindings or {}).items():
        if isinstance(binding, dict):
            agent_ids = binding.get("agents") or []
            first = str(agent_ids[0]) if agent_ids else str(binding.get("pinned_id") or "")
            out[str(node_id)] = {
                "agent_id": first,
                "agent_name": str(binding.get("agent_name") or ""),
                "reason": str(binding.get("reason") or ""),
                "policy": str(binding.get("policy") or ""),
                "agents": [str(a) for a in agent_ids],
            }
    return out


def _handoffs(trace: list[dict[str, Any]], agents: dict[str, dict[str, Any]]) -> list[FlowHandoff]:
    """Build the handoff list from the trace, newest state per id.

    The trace emits one line per transition; the board wants the last state each handoff reached, plus
    a running breach count so a handoff that was rejected and retried is visible as such.
    """
    found: dict[str, FlowHandoff] = {}
    order: list[str] = []
    for event in trace:
        kind = str(event.get("type") or "")
        if kind not in _HANDOFF_EVENTS:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        hid = str(payload.get("handoff_id") or payload.get("id") or "")
        if not hid:
            # A handoff event with no id is keyed on its endpoints, so it still appears once.
            hid = f"{event.get('node_id') or payload.get('to_node') or '?'}:" \
                  f"{payload.get('from_node') or '?'}"
        entry = found.get(hid)
        if entry is None:
            entry = FlowHandoff(handoff_id=hid)
            found[hid] = entry
            order.append(hid)
        entry.state = _HANDOFF_EVENTS[kind]
        entry.at = str(event.get("ts") or entry.at)
        if kind == "handoff.breached":
            entry.breaches += 1
        # Fields are filled in from whichever transition carries them, so a later sparse event does
        # not erase what an earlier one told us.
        for key, attr in (("from_agent", "from_agent"), ("to_agent", "to_agent"),
                          ("from_node", "from_node"), ("to_node", "to_node"),
                          ("summary", "summary"), ("status", "payload_status")):
            value = payload.get(key)
            if value:
                setattr(entry, attr, str(value))
        for key, attr in (("from_agent_id", "from_agent"), ("to_agent_id", "to_agent")):
            value = payload.get(key)
            if value:
                setattr(entry, attr, str(value))
        artifacts = payload.get("artifacts")
        if isinstance(artifacts, list):
            entry.artifacts = [str(a) for a in artifacts][:12]
        if not entry.from_node and event.get("node_id"):
            entry.from_node = str(event["node_id"])
    out = [found[hid] for hid in order]
    # Name the agents, so the board reads as people rather than ids.
    for entry in out:
        entry.from_agent = _name_for(entry.from_agent, agents)
        entry.to_agent = _name_for(entry.to_agent, agents)
        entry.tone = _handoff_tone(entry)
    return out


def _name_for(agent_ref: str, agents: dict[str, dict[str, Any]]) -> str:
    """Resolve an agent id to a name, leaving a non-id reference as it is."""
    if not agent_ref:
        return ""
    entry = agents.get(agent_ref)
    return entry["name"] if entry else agent_ref


def _handoff_tone(handoff: FlowHandoff) -> str:
    """The colour for a handoff, from its state — a breach is bad, a fulfilment good."""
    return {
        "fulfilled": "good",
        "verified": "good",
        "accepted": "info",
        "proposed": "muted",
        "rejected": "warn",
        "breached": "bad",
        "escalated": "bad",
    }.get(handoff.state, "info")


def _handoff_edges(handoffs: Iterable[FlowHandoff]) -> tuple[dict[str, FlowHandoff],
                                                             dict[str, FlowHandoff]]:
    """Index handoffs by the node that received them and by the node that sent them.

    A handoff names nodes when the trace carries them; when it only names agents, the node index still
    lets a row say "sent on" rather than nothing.
    """
    received: dict[str, FlowHandoff] = {}
    sent: dict[str, FlowHandoff] = {}
    for handoff in handoffs:
        if handoff.to_node:
            received[handoff.to_node] = handoff
        if handoff.from_node:
            sent[handoff.from_node] = handoff
    return received, sent


def _rows(nodes: dict[str, Any], manifest_nodes: dict[str, dict[str, Any]],
          bindings: dict[str, dict[str, Any]], agents: dict[str, dict[str, Any]],
          received: dict[str, FlowHandoff], sent: dict[str, FlowHandoff],
          checkpoint: dict[str, Any]) -> list[FlowRow]:
    """One row per unit of work, ordered as the manifest declares them.

    The order comes from the manifest so the board reads top-to-bottom as the work flows, rather than
    as a dict happened to iterate. A node in the checkpoint but not the manifest (an authored graph,
    or a resumed run from an older build) is appended rather than dropped.
    """
    artifacts = checkpoint.get("artifacts") if isinstance(checkpoint.get("artifacts"), dict) else {}
    rows: list[FlowRow] = []
    seen: set[str] = set()

    def add(node_id: str, declaration: dict[str, Any]) -> None:
        seen.add(node_id)
        record = nodes.get(node_id) if isinstance(nodes.get(node_id), dict) else {}
        binding = bindings.get(node_id, {})
        agent_id = str(binding.get("agent_id") or "")
        agent = agents.get(agent_id, {})
        status = str(record.get("status") or "pending")
        is_gate = str(declaration.get("type") or "") == "gate" or "gate" in node_id
        gate_kind = str(declaration.get("kind") or "") if is_gate else ""
        row = FlowRow(
            node_id=node_id,
            skill=str(declaration.get("skill") or ""),
            agent_id=agent_id,
            agent_name=str(agent.get("name") or binding.get("agent_name") or ""),
            title=str(declaration.get("title") or agent.get("title") or ""),
            status=status,
            verdict=str(record.get("verdict") or ""),
            tone=_STATUS_TONE.get(status, "info"),
            phase=str(declaration.get("phase") or ""),
            iterations=int(record.get("iterations") or 0),
            summary=str(record.get("summary") or ""),
            artifacts=sorted(a for a, meta in artifacts.items()
                             if isinstance(meta, dict) and meta.get("node") == node_id),
            blocked_by=str(record.get("summary") or "") if status in ("blocked", "failed",
                                                                     "needs_review") else "",
            is_gate=is_gate,
            gate_kind=gate_kind,
        )
        inbound = received.get(node_id)
        if inbound is not None:
            row.received_from = inbound.from_agent or inbound.from_node
            row.received_summary = inbound.summary
        outbound = sent.get(node_id)
        if outbound is not None:
            row.sent_to = outbound.to_agent or outbound.to_node
            row.sent_summary = outbound.summary
        rows.append(row)

    # Gates are declared among the nodes in some manifests and in a `gates` list in others; both are
    # honoured, and a gate is placed where the manifest puts it.
    for node_id, declaration in manifest_nodes.items():
        add(node_id, declaration)
    for node_id, record in (nodes or {}).items():
        if str(node_id) not in seen:
            add(str(node_id), {})
    # A node that only the handoffs name still exists — it moved information, so it belongs on the
    # board. Without this, a resumed run whose manifest could not be read would show no rows at all
    # while the handoffs beneath it named real work.
    for node_id in list(received) + list(sent):
        if node_id and node_id not in seen:
            add(node_id, {})

    # "Handoff back": a review node whose verdict concerns a producing node. Derived from the handoff
    # that left the producer and reached the judge, so it is the real information path, not a guess
    # from node names.
    for row in rows:
        if row.sent_to:
            target = next((r for r in rows if r.node_id == row.sent_to
                           or r.agent_name == row.sent_to), None)
            if target is not None and target is not row:
                if target.node_id not in row.judged_by:
                    row.judged_by.append(target.node_id)
    return rows


def _counts(rows: list[FlowRow]) -> dict[str, int]:
    """The tallies the board shows, computed once so the UI does not.
    """
    counts = {"total": len(rows), "done": 0, "working": 0, "waiting": 0, "stuck": 0, "gates": 0}
    for row in rows:
        if row.is_gate:
            counts["gates"] += 1
        if row.status in ("done", "pass"):
            counts["done"] += 1
        elif row.status in ("running", "working"):
            counts["working"] += 1
        elif row.status in ("pending", "queued"):
            counts["waiting"] += 1
        elif row.status in ("blocked", "failed", "needs_review", "awaiting_owner"):
            counts["stuck"] += 1
    return counts


def _headline(rows: list[FlowRow], handoffs: list[FlowHandoff],
              checkpoint: dict[str, Any]) -> str:
    """One line answering *where is the work*, derived from the rows rather than stored."""
    if not rows:
        return "No work is assigned yet."
    working = [r for r in rows if r.status in ("running", "working")]
    if working:
        owner = working[0].agent_name or working[0].node_id
        return f"{owner} is working on {working[0].node_id} " \
               f"({len(working)} of {len(rows)} in flight)."
    stuck = [r for r in rows if r.status in ("blocked", "failed", "needs_review")]
    if stuck:
        reason = stuck[0].blocked_by or stuck[0].verdict or "no reason recorded"
        return f"{stuck[0].node_id} is stuck — {reason[:120]}"
    breaches = [h for h in handoffs if h.state in ("breached", "rejected")]
    if breaches:
        return f"{len(breaches)} handoff(s) need attention."
    done = [r for r in rows if r.status in ("done", "pass")]
    if len(done) == len(rows):
        return "Every piece of work is done."
    phase = str(checkpoint.get("phase") or "")
    return f"{len(done)} of {len(rows)} done" + (f" (run {phase})" if phase else "")
