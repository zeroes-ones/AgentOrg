#!/usr/bin/env python3
"""subagents.py — isolated child contexts, with transcripts the parent can page through.

WHY THIS EXISTS
---------------
The engine already dispatches many agents — a swarm votes (`BindingPolicy.SWARM`) and a fan-out splits
work (`fanout.py`). What it did not do is *isolate* them. A subagent ran through the same node
machinery as the agent that spawned it, so:

- everything a helper read landed in the parent's context accounting, and ten reviewers reading twenty
  files each filled the parent's window with transcripts it never asked to keep;
- there was no child transcript to read back, so a fan-out result was all-or-nothing: a 4,000-line
  finding, or none of it;
- a stopped child could not be resumed — the parent re-ran it from scratch.

This module is the missing half: the unit of isolation is the **session** (already owned by
`context/session.py`), the transcript is durable, and what the parent sees is a bounded *reference*
plus one tool to page the bytes it actually needs.

DESIGN
------
- **Isolation is the log, not the prefix.** Children share the parent's pinned prefix — the cacheable
  bytes are `(skill, tools)`-scoped and agent-independent by design (`prefix.py`). A child that
  re-derived its own prefix would pay full price for the same procedure and break the one invariant
  this codebase treats as load-bearing. A child's identity and task go in the **tail**, always.
- **The transcript is durable and never re-sent whole.** JSONL under
  `.agent_state/children/<run_id>/<child_id>.jsonl`, matching `trace.jsonl`; a parent reads a *byte
  range* through `read_subagent_result`.
- **Truncation is reported, never silent.** Every read returns `returned_bytes`/`total_bytes`/`more`,
  because a model that believes it saw the whole result is the confident-wrong-output failure the eval
  suite exists to catch.
- **The preview is the child's own summary**, not an arbitrary byte slice, so the default view is
  meaningful even when the model never pages.
- **A stopped child is resumable.** Its transcript and session id are kept, so a continuation appends
  rather than restarting — the fan-out analogue of `run_state.json`'s resume.

Usage:
    store = ChildStore(workspace)
    ref = store.open(child_id="sub_1", agent_id="ag_9c1d", skill="code-reviewer", task="review a.ts")
    store.append(child_id="sub_1", kind="turn", text="…")
    ref = store.close(child_id="sub_1", status="done", summary="looks correct", steps=5)
    page = store.read(child_id="sub_1", offset_bytes=0, limit_bytes=8192)
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "SubagentError", "ChildRef", "ChildPage", "ChildStore",
    "CHILDREN_DIRNAME", "DEFAULT_PREVIEW_BYTES", "DEFAULT_PAGE_BYTES", "MAX_PAGE_BYTES",
    "MAX_SUBAGENT_DEPTH", "VALID_STATUS",
]

#: Where child transcripts live, under the engine's state directory.
CHILDREN_DIRNAME = "children"
#: How much of a child's result the parent sees by default. Small on purpose: the point of isolation
#: is that N children cost the parent N *previews*, not N transcripts.
DEFAULT_PREVIEW_BYTES = 1200
#: How much one `read_subagent_result` call returns by default.
DEFAULT_PAGE_BYTES = 8192
#: The hard ceiling on one read. A model cannot ask for a 40MB transcript and blow its own window.
MAX_PAGE_BYTES = 64 * 1024
#: Root is depth 0, first-layer children are 1. Nested delegation stops here so recursion is bounded.
MAX_SUBAGENT_DEPTH = 2
VALID_STATUS = ("running", "done", "needs_review", "failed")

_CHILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SubagentError(RuntimeError):
    """A subagent operation that cannot be honoured, named so the caller can act on it."""


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


@dataclass
class ChildRef:
    """The bounded frame a parent receives for one child.

    This is the *only* thing a child costs the parent's context: a status, a few counters, a preview
    the child wrote, and a pointer. Everything else is on disk until asked for.
    """

    child_id: str
    agent_id: str = ""
    skill: str = ""
    task: str = ""
    status: str = "running"
    summary: str = ""
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    depth: int = 0
    transcript: str = ""
    bytes: int = 0
    created_at: str = ""
    updated_at: str = ""
    error: str = ""

    def as_dict(self, *, preview_bytes: int = DEFAULT_PREVIEW_BYTES) -> dict[str, Any]:
        preview = self.summary[:preview_bytes]
        return {
            "child_id": self.child_id, "agent_id": self.agent_id, "skill": self.skill,
            "task": self.task[:400], "status": self.status, "steps": self.steps,
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "depth": self.depth,
            "preview": preview, "preview_bytes": len(preview.encode("utf-8")),
            "transcript": self.transcript, "bytes": self.bytes,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChildRef":
        return cls(
            child_id=str(data.get("child_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            skill=str(data.get("skill") or ""),
            task=str(data.get("task") or ""),
            status=str(data.get("status") or "running"),
            summary=str(data.get("summary") or ""),
            steps=int(data.get("steps") or 0),
            tokens_in=int(data.get("tokens_in") or 0),
            tokens_out=int(data.get("tokens_out") or 0),
            depth=int(data.get("depth") or 0),
            transcript=str(data.get("transcript") or ""),
            bytes=int(data.get("bytes") or 0),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            error=str(data.get("error") or ""),
        )


@dataclass
class ChildPage:
    """One byte-addressed slice of a child transcript, honest about truncation."""

    child_id: str
    text: str
    offset_bytes: int
    returned_bytes: int
    total_bytes: int

    @property
    def more(self) -> bool:
        """True when there is more transcript after this slice."""
        return self.offset_bytes + self.returned_bytes < self.total_bytes

    def as_dict(self) -> dict[str, Any]:
        return {"child_id": self.child_id, "text": self.text, "offset_bytes": self.offset_bytes,
                "returned_bytes": self.returned_bytes, "total_bytes": self.total_bytes,
                "more": self.more, "next_offset_bytes": self.offset_bytes + self.returned_bytes
                if self.more else None}


@dataclass
class ChildStore:
    """Durable child transcripts and their reference frames, for one workspace.

    Parameters
    ----------
    workspace:
        The workspace whose `.agent_state/children/` holds the transcripts. Accepts a `Workspace` or a
        plain path, so the store is usable from the executor (which has a path) and from a test.
    run_id:
        Scopes the transcripts to one run, so a re-run cannot read a previous run's children.
    """

    workspace: Any
    run_id: str = "run"
    #: Set when the store is opened on a real filesystem. Kept private because the layout is this
    #: class's business.
    _opened: dict[str, ChildRef] = field(default_factory=dict)

    # ── layout ──────────────────────────────────────────────────────────────

    @property
    def state_dir(self) -> Path:
        base = getattr(self.workspace, "state_dir", None)
        if base is not None:
            return Path(base)
        return Path(self.workspace)

    @property
    def root(self) -> Path:
        return self.state_dir / CHILDREN_DIRNAME / _safe(self.run_id)

    def transcript_path(self, child_id: str) -> Path:
        return self.root / f"{_safe(child_id)}.jsonl"

    def meta_path(self, child_id: str) -> Path:
        return self.root / f"{_safe(child_id)}.meta.json"

    # ── lifecycle ───────────────────────────────────────────────────────────

    def open(self, *, child_id: str, agent_id: str = "", skill: str = "",
             task: str = "", depth: int = 0) -> ChildRef:
        """Start a child: create its transcript and write its reference frame.

        Depth is checked here rather than at dispatch, so the limit holds no matter which caller
        starts a child — the bound belongs to the child, not to the tool that requested it.
        """
        if not _CHILD_ID_RE.match(child_id or ""):
            raise SubagentError(f"invalid child id {child_id!r}")
        if depth > MAX_SUBAGENT_DEPTH:
            raise SubagentError(
                f"subagent depth {depth} exceeds the cap of {MAX_SUBAGENT_DEPTH}; nested delegation "
                "stops there so recursion is bounded")
        self.root.mkdir(parents=True, exist_ok=True)
        ref = ChildRef(child_id=child_id, agent_id=agent_id, skill=skill, task=task,
                       status="running", depth=depth, created_at=_iso_now(), updated_at=_iso_now())
        # The transcript is created (empty) immediately, so `read` before any turn is a valid, empty
        # page rather than a missing file — an absent file and an empty child look identical to a
        # model, and only one of them is an error.
        self.transcript_path(child_id).touch()
        self._write_meta(ref)
        self._opened[child_id] = ref
        return ref

    def append(self, *, child_id: str, kind: str, text: str = "", **extra: Any) -> None:
        """Append one record to the transcript. Append-only, like the trace.

        Append-only matters for the same reason it does in `prefix.py`: rewriting an earlier record
        would invalidate every byte offset after it, which is exactly what paging relies on.
        """
        record = {"at": _iso_now(), "kind": str(kind), "text": str(text)}
        record.update({k: v for k, v in extra.items()})
        path = self.transcript_path(child_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def close(self, *, child_id: str, status: str = "done", summary: str = "",
              steps: int = 0, tokens_in: int = 0, tokens_out: int = 0,
              error: str = "") -> ChildRef:
        """Finish a child and freeze its reference frame.

        The `summary` is what the parent sees by default, and it is the *child's own* account rather
        than an arbitrary slice — which is what makes the default view meaningful without paging.
        """
        if status not in VALID_STATUS:
            raise SubagentError(f"unknown child status {status!r}; expected one of {VALID_STATUS}")
        ref = self._load_meta(child_id)
        if ref is None:
            raise SubagentError(f"no child {child_id!r} to close")
        ref.status = status
        ref.summary = summary
        ref.steps = int(steps)
        ref.tokens_in = int(tokens_in)
        ref.tokens_out = int(tokens_out)
        ref.error = error
        ref.updated_at = _iso_now()
        path = self.transcript_path(child_id)
        ref.bytes = path.stat().st_size if path.is_file() else 0
        ref.transcript = str(path)
        self._write_meta(ref)
        self._opened[child_id] = ref
        return ref

    def read(self, *, child_id: str, offset_bytes: int = 0,
             limit_bytes: int = DEFAULT_PAGE_BYTES) -> ChildPage:
        """Return one byte range of a child transcript.

        Byte-addressed rather than line-addressed: offsets stay stable across appends and re-reads,
        while a line number means something different the moment a transcript grows. The limit is
        clamped, so a model cannot ask for more transcript than its own window can hold.
        """
        path = self.transcript_path(child_id)
        if not path.is_file():
            raise SubagentError(f"no transcript for child {child_id!r}")
        raw = path.read_bytes()
        start = max(0, int(offset_bytes or 0))
        limit = min(max(1, int(limit_bytes or DEFAULT_PAGE_BYTES)), MAX_PAGE_BYTES)
        window = raw[start:start + limit]
        return ChildPage(child_id=child_id, text=window.decode("utf-8", errors="replace"),
                         offset_bytes=start, returned_bytes=len(window), total_bytes=len(raw))

    def ref(self, child_id: str) -> ChildRef | None:
        return self._load_meta(child_id)

    def children(self) -> list[ChildRef]:
        """Every child started in this run, in creation order. `done` when there are none."""
        if not self.root.is_dir():
            return []
        refs: list[ChildRef] = []
        for path in sorted(self.root.glob("*.meta.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            refs.append(ChildRef.from_dict(data))
        refs.sort(key=lambda r: r.created_at or "")
        return refs

    def resumable(self, child_id: str) -> bool:
        """Whether a stopped child can be continued from its transcript.

        A child that reached a verdict of its own can be resumed; one that crashed outright is better
        re-run, because its transcript stops mid-thought and appending to it would build on a partial
        turn.
        """
        ref = self._load_meta(child_id)
        return ref is not None and ref.status == "needs_review"

    # ── the reference frame, on disk ────────────────────────────────────────

    def _write_meta(self, ref: ChildRef) -> None:
        """Write the reference frame. Stores `summary` itself, not just its preview.

        The on-disk frame must carry the child's full summary: the *preview* is a presentation decision
        made per reader (how much to show the parent), while the summary is the child's own account and
        has to survive a reload. Writing only the preview would silently truncate a resumed child's
        result to whatever size the last reader happened to want.
        """
        target = self.meta_path(ref.child_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = ref.as_dict()
        payload["summary"] = ref.summary
        payload.pop("preview", None)
        payload.pop("preview_bytes", None)
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)

    def _load_meta(self, child_id: str) -> ChildRef | None:
        target = self.meta_path(child_id)
        if not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return ChildRef.from_dict(data)


def _safe(value: str) -> str:
    """A filesystem-safe fragment. A child id or run id never becomes a path escape."""
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
    return text or "x"


@dataclass
class SubagentRunner:
    """Dispatch and collect isolated children for one executor.

    The runner is the *policy* half — depth, budget, isolation, transcript — while the injected
    `run_child` callable is the *mechanism* half (how one child actually makes model calls). That split
    is the same one `fanout.run_fanout` uses, and it is what lets the whole dispatch be tested without
    a provider.

    Parameters
    ----------
    store:
        Where transcripts and reference frames live.
    run_child:
        `run_child(prompt, skill, child_id) -> (summary, status, tokens_in, tokens_out, steps)`.
        Injected rather than implemented here so this module never depends on the gateway.
    agents:
        The agent ids a child may run as, highest priority first. Empty means the caller has no roster
        and dispatch is refused rather than invented.
    max_parallel:
        How many children run at once. Reuses `fanout`'s adaptive batching, minus its retry policy,
        because a child is a fresh context rather than a rate-limited call.
    parent_depth:
        The depth of the agent doing the delegating. Children are one deeper; the cap is enforced here.
    parent_tokens:
        The parent's remaining budget, which a fleet is carved from so a fan-out cannot outspend its
        parent.
    """

    store: ChildStore
    run_child: Any
    agents: list[str] = field(default_factory=list)
    max_parallel: int = 4
    parent_depth: int = 0
    parent_tokens: int = 0
    on_event: Any = None
    _counter: int = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"sub_{self._counter:03d}"

    def _budget_for(self, count: int) -> int:
        """The per-child share of the parent's remaining tokens.

        Carved rather than shared: a fleet that each assumed the parent's whole budget would let N
        children spend N times it, which is the cost explosion the delegation design partitions against.
        """
        if self.parent_tokens <= 0 or count <= 0:
            return 0
        return max(1, self.parent_tokens // (count + 1))

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:  # noqa: BLE001 - an event sink must never break a child
            pass

    def run_task(self, *, prompt: str, skill: str = "") -> dict[str, Any]:
        """Run one child and return its reference frame."""
        refs = self._run([prompt], skill=skill)
        if not refs:
            raise SubagentError("the subagent produced no result")
        return refs[0]

    def run_fleet(self, *, tasks: list[str], skill: str = "") -> list[dict[str, Any]]:
        """Run N children, one per task, bounded in parallel and returned in order."""
        return self._run(list(tasks), skill=skill)

    def _run(self, tasks: list[str], *, skill: str) -> list[dict[str, Any]]:
        if not self.agents:
            raise SubagentError(
                "no agent is available to run a subagent, so there is nobody to delegate to")
        depth = self.parent_depth + 1
        if depth > MAX_SUBAGENT_DEPTH:
            raise SubagentError(
                f"cannot delegate at depth {depth}: the cap is {MAX_SUBAGENT_DEPTH}. Nested "
                "delegation stops there so recursion is bounded.")
        share = self._budget_for(len(tasks))

        refs: list[dict[str, Any]] = []
        # Bounded batches rather than all at once: a 128-task fleet launched together would open 128
        # concurrent model calls and exhaust a local provider in one burst.
        batch_size = max(1, int(self.max_parallel or 1))
        for start in range(0, len(tasks), batch_size):
            batch = tasks[start:start + batch_size]
            for offset, task in enumerate(batch):
                agent_id = self.agents[(start + offset) % len(self.agents)]
                refs.append(self._run_one(prompt=task, skill=skill, agent_id=agent_id,
                                          depth=depth, budget=share))
        return refs

    def _run_one(self, *, prompt: str, skill: str, agent_id: str, depth: int,
                 budget: int) -> dict[str, Any]:
        """Run exactly one child, writing its transcript as it goes."""
        child_id = self._next_id()
        self.store.open(child_id=child_id, agent_id=agent_id, skill=skill, task=prompt, depth=depth)
        self.store.append(child_id=child_id, kind="task", text=prompt)
        self._emit(EventNames.SPAWNED, {"child_id": child_id, "agent_id": agent_id, "skill": skill,
                                        "depth": depth, "budget": budget})
        try:
            summary, status, tokens_in, tokens_out, steps = self.run_child(
                prompt=prompt, skill=skill, child_id=child_id, agent_id=agent_id,
                depth=depth, budget=budget)
        except Exception as exc:  # noqa: BLE001 - one child failing must not lose the others
            ref = self.store.close(child_id=child_id, status="failed", summary="",
                                   error=f"{type(exc).__name__}: {exc}")
            self._emit(EventNames.FAILED, {"child_id": child_id, "error": ref.error})
            return ref.as_dict()

        self.store.append(child_id=child_id, kind="result", text=summary, status=status)
        ref = self.store.close(child_id=child_id, status=status, summary=summary, steps=steps,
                               tokens_in=tokens_in, tokens_out=tokens_out)
        self._emit(EventNames.DONE, {"child_id": child_id, "status": status, "steps": steps})
        return ref.as_dict()

    def read(self, *, child_id: str, offset_bytes: int = 0,
             limit_bytes: int = DEFAULT_PAGE_BYTES) -> ChildPage:
        """Page a child transcript, emitting what range was read so the console can show it."""
        page = self.store.read(child_id=child_id, offset_bytes=offset_bytes, limit_bytes=limit_bytes)
        self._emit(EventNames.READ, {"child_id": child_id, "offset_bytes": page.offset_bytes,
                                     "returned_bytes": page.returned_bytes,
                                     "total_bytes": page.total_bytes})
        return page


class EventNames:
    """The subagent event names, spelled once so `protocol.EventType` and this module agree."""

    SPAWNED = "subagent.spawned"
    DONE = "subagent.done"
    FAILED = "subagent.failed"
    READ = "subagent.read"
