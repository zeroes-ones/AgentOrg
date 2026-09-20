#!/usr/bin/env python3
"""state.py — the workspace layout and the durable run checkpoint.

WHY THIS EXISTS
---------------
A run must survive the engine dying. That means the engine's location conventions and
its checkpoint format have to be defined in exactly one place, or half the code will
invent its own paths and crash-resume will silently read the wrong file.

This module owns two things:

1. **Where everything lives.** `projects/<slug>/.agent_state/` holds the org, the
   run checkpoint, the trace, the review feedback, per-agent mailboxes, session
   transcripts and telemetry. Naming that once prevents drift between the engine, the
   Swift inspector and the diagnostics bundle.
2. **The checkpoint.** `run_state.json` carries everything needed to resume: the
   manifest hash, the current node and loop iteration, per-node status and verdicts,
   the artifact index, open questions, the delegation chain and the budget counters.

DESIGN
------
- **Checkpoints are atomic and schema-versioned.** A torn checkpoint is worse than an
  old one, so writes go temp-then-`os.replace` and carry a `run_state_version`.
- **Resume is explicit, not implicit.** :meth:`Workspace.load_checkpoint` returns None
  rather than a half-built object when there is nothing to resume, so the caller
  decides whether a fresh run is appropriate.
- **Budget counters live in the checkpoint**, because a resumed run must not be handed
  a fresh budget — that would be an unbounded-spend hole through the crash path.
- **Layout is created on demand and is idempotent**, so opening an existing project
  twice is not an error.

Usage:
    ws = Workspace.for_project("demo", root=Path("projects"))
    ws.ensure()
    cp = ws.load_checkpoint()          # None on a fresh project
    ws.save_checkpoint({"node": "fixer", "iteration": 1, ...})
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["StateError", "Workspace", "RunCheckpoint", "ENGINE_STATE_DIRNAME"]

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
CHECKPOINT_VERSION = "1.0.0"

#: The engine's own state directory, named once here because the layout is owned here. Tools and the
#: artifact store import it rather than repeating the literal, so "the engine's state" cannot drift
#: into two spellings that disagree about what is project content.
ENGINE_STATE_DIRNAME = ".agent_state"


class StateError(RuntimeError):
    """Raised on an invalid project name or a corrupt, unreadable checkpoint."""


def _iso_now() -> str:
    """UTC timestamp with millisecond precision, matching the protocol's format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def _slugify(name: str) -> str:
    """Normalise a folder name into the slug grammar, without losing the real name.

    A real folder is called ``My_App`` or ``Foo.Bar``; a slug must match
    ``[a-z0-9][a-z0-9._-]*``. Lowercasing and replacing the disallowed characters keeps the
    identifier valid while :attr:`Workspace.display_name` keeps the name a person would recognise,
    so normalisation never reaches the filesystem.
    """
    lowered = re.sub(r"[^a-z0-9._-]+", "_", (name or "").strip().lower()).strip("_")
    # A slug must start alphanumeric; a folder called `_draft` would otherwise be rejected outright
    # when it can be represented honestly as `draft`.
    lowered = lowered.lstrip("._-")
    return lowered or "project"


@dataclass
class RunCheckpoint:
    """The resumable state of one run.

    Fields mirror the library's `run-state` shape where they overlap (`node`,
    `iteration`, `budget`, `nodes`, `artifacts`, `open_questions`, `log`) so the engine
    can exchange state with `workflow-runner.py` without a translation layer.
    """

    run_id: str
    workflow: str = ""
    manifest_sha: str = ""
    node: str | None = None
    iteration: int = 0
    nodes: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    # Delegation lineage: [agent_id, ...] in the order work was delegated.
    delegation_chain: list[str] = field(default_factory=list)
    # Per-session state so a resume can pick up context without re-deriving it.
    sessions: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=lambda: {
        "max_steps": 0, "steps_used": 0, "tokens_used": 0, "usd_used": 0.0, "iterations": {},
    })
    status: str = "running"
    agent_health: dict[str, Any] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    created: str = field(default_factory=_iso_now)
    updated: str = field(default_factory=_iso_now)
    run_state_version: str = CHECKPOINT_VERSION

    def touch(self) -> None:
        """Refresh the `updated` timestamp. Called on every mutation before saving."""
        self.updated = _iso_now()

    def append_log(self, *, node: str, action: str, verdict: str | None = None,
                   detail: str | None = None, step: int | None = None) -> None:
        """Append a log entry, which is what the SLI rollup and UI timeline read.

        Kept small and append-only: this list is the human-readable spine of a run and
        must never be rewritten, so a resumed run's history stays intact.
        """
        entry: dict[str, Any] = {"node": node, "action": action, "ts": _iso_now()}
        if verdict is not None:
            entry["verdict"] = verdict
        if detail:
            entry["detail"] = detail
        entry["step"] = step if step is not None else self.budget.get("steps_used", 0)
        self.log.append(entry)
        self.touch()

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form for persistence."""
        return {
            "run_state_version": self.run_state_version,
            "run_id": self.run_id,
            "workflow": self.workflow,
            "manifest_sha": self.manifest_sha,
            "node": self.node,
            "iteration": self.iteration,
            "nodes": self.nodes,
            "fields": self.fields,
            "artifacts": self.artifacts,
            "decisions": self.decisions,
            "open_questions": self.open_questions,
            "delegation_chain": self.delegation_chain,
            "sessions": self.sessions,
            "budget": self.budget,
            "status": self.status,
            "agent_health": self.agent_health,
            "log": self.log,
            "created": self.created,
            "updated": self.updated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunCheckpoint":
        """Rebuild from persisted JSON, ignoring keys this build does not know.

        Unknown keys are ignored rather than rejected so a checkpoint written by a
        newer minor version still resumes, while a newer *major* is refused upstream by
        the schema registry.
        """
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.setdefault("run_id", data.get("run_id", "unknown"))
        cp = cls(**kwargs)  # type: ignore[arg-type]
        # Merge budget keys so a checkpoint predating a counter still resumes.
        for key, default in (("max_steps", 0), ("steps_used", 0), ("tokens_used", 0),
                             ("usd_used", 0.0), ("iterations", {})):
            cp.budget.setdefault(key, default)
        return cp


@dataclass
class Workspace:
    """The on-disk layout for one project.

    Parameters
    ----------
    slug:
        Project directory name. Validated as a slug so a name can never contain a path
        separator or a traversal segment.
    root:
        Directory holding all projects (``AgentOrg/projects`` by default).
    attached:
        The real directory when this workspace *is* an existing project folder rather
        than a managed one under ``root``. Set only by :meth:`attach`. When present it is
        the project directory — the path is stored, never re-derived from ``slug``, because
        a slug is normalised and a real folder name is not.
    """

    slug: str
    root: Path
    attached: Path | None = None

    def __post_init__(self) -> None:
        if not _SLUG_RE.match(self.slug or ""):
            raise StateError(
                f"invalid project name {self.slug!r}: expected a lowercase slug "
                "matching [a-z0-9][a-z0-9._-]*"
            )
        self.root = Path(self.root).expanduser()
        if self.attached is not None:
            self.attached = Path(self.attached).expanduser()

    # ── layout ──────────────────────────────────────────────────────────────

    @classmethod
    def for_project(cls, slug: str, root: os.PathLike | str | None = None) -> "Workspace":
        """Build a workspace for a project, defaulting the root to AgentOrg/projects."""
        base = Path(root) if root is not None else Path(__file__).resolve().parent.parent / "projects"
        return cls(slug=slug, root=base)

    @classmethod
    def attach(cls, directory: os.PathLike | str) -> "Workspace":
        """Point a workspace at an **existing** project folder.

        This is the difference between a managed workspace (``projects/<slug>/``, which the
        engine creates and owns) and an attached one (your real repository, which the engine
        merely *inhabits*). The agents' file tools are rooted at :attr:`path`, so attaching is
        what makes "add a field to ``User.swift``" edit your ``User.swift``.

        The directory **must already exist**: a typo would otherwise create a project somewhere
        unintended, which is a silent wrong-directory failure rather than a loud one. The path is
        resolved (symlinks followed) because every containment check runs on the resolved path —
        attaching a symlink must mean its target.

        ``slug`` is derived from the folder name and normalised to the slug grammar, so a folder
        called ``My_App`` still yields a valid identifier; the *real* path is kept on the object so
        the console shows ``My_App`` and state lands in the right place.
        """
        target = Path(directory).expanduser()
        if not target.exists():
            raise StateError(
                f"cannot attach {target}: it does not exist. Create the folder first, or use "
                "--slug/--root for a managed workspace the engine owns."
            )
        if not target.is_dir():
            raise StateError(f"cannot attach {target}: it is not a directory")
        resolved = target.resolve()
        return cls(slug=_slugify(resolved.name), root=resolved.parent, attached=resolved)

    @property
    def path(self) -> Path:
        """The project directory — the attached folder, or ``root/<slug>`` when managed."""
        return self.attached if self.attached is not None else self.root / self.slug

    @property
    def is_attached(self) -> bool:
        """True when this workspace is an existing folder rather than a managed project."""
        return self.attached is not None

    @property
    def display_name(self) -> str:
        """The name a person recognises: the real folder name, not the normalised slug."""
        return self.attached.name if self.attached is not None else self.slug


    @property
    def state_dir(self) -> Path:
        """`.agent_state/` — everything the engine persists for this project."""
        return self.path / ENGINE_STATE_DIRNAME

    @property
    def docs_dir(self) -> Path:
        """`docs/` — PRD, API spec and other human-readable outputs."""
        return self.path / "docs"

    @property
    def src_dir(self) -> Path:
        """`src/` — the code the agents produce."""
        return self.path / "src"

    @property
    def checkpoint_path(self) -> Path:
        """`run_state.json` — the orchestrator's resumable checkpoint."""
        return self.state_dir / "run_state.json"

    @property
    def runner_state_path(self) -> Path:
        """`runner_state.json` — the **library runner's** per-node checkpoint.

        Deliberately a different file from :attr:`checkpoint_path`. Both sides called their file
        `run_state.json`, and they wrote it in turn: the runner writes `{workflow, manifest_sha, nodes}`
        and the orchestrator writes `{run_id, phase, gate, outcome, …}`. So after the orchestrator's
        post-run write, the runner's `load_state` found no `workflow`/`manifest_sha` and returned
        `None` — meaning **every continuation restarted the graph from scratch** and re-ran the node
        that had just failed. That is the "it keeps rejecting at some point" symptom: approving a gate
        re-ran `pm`, hit the identical contract violation, and parked again, for ever.

        Two writers, one file, incompatible shapes. One file each is the fix that cannot be got wrong.
        """
        return self.state_dir / "runner_state.json"

    @property
    def trace_path(self) -> Path:
        """`trace.jsonl` — the append-only event trace."""
        return self.state_dir / "trace.jsonl"

    @property
    def org_path(self) -> Path:
        """`org.json` — the roster: names, skills, models, reporting lines."""
        return self.state_dir / "org.json"

    @property
    def review_feedback_path(self) -> Path:
        """`review_feedback.json` — the latest rejection dossier."""
        return self.state_dir / "review_feedback.json"

    @property
    def effects_path(self) -> Path:
        """`effects.jsonl` — the idempotency journal."""
        return self.state_dir / "effects.jsonl"

    @property
    def pool_path(self) -> Path:
        """`pool.json` — the task pool agents pull work from.

        Beside the other run state rather than in a database, because the pool is small, must survive
        a restart, and must be inspectable by a person debugging a run.
        """
        return self.state_dir / "pool.json"

    @property
    def library_manifest_path(self) -> Path:
        """`library_manifest.json` — the pinned Skills content manifest."""
        return self.state_dir / "library_manifest.json"

    @property
    def handoffs_dir(self) -> Path:
        """`handoffs/` — one JSON document per edge crossing, keyed by the handoff's own id.

        The handoff is the only thing that crosses a node boundary, so it is the one artifact worth
        keeping per *crossing* rather than per node: a resumed process, or the flow board, can read
        back exactly what one node handed the next instead of re-deriving it from node summaries.
        """
        return self.state_dir / "handoffs"

    @property
    def agents_dir(self) -> Path:
        """`agents/` — one subdirectory per agent (mailbox, sessions)."""
        return self.state_dir / "agents"

    @property
    def sessions_dir(self) -> Path:
        """`sessions/` — archived session transcripts, never re-sent."""
        return self.state_dir / "sessions"

    @property
    def telemetry_dir(self) -> Path:
        """`telemetry/` — exported spans."""
        return self.state_dir / "telemetry"

    @property
    def spans_path(self) -> Path:
        """`telemetry/spans.jsonl`."""
        return self.telemetry_dir / "spans.jsonl"

    @property
    def cache_dir(self) -> Path:
        """`cache/` — the durable prefix-cache record: pinned prefixes, shape history, savings.

        Its own directory rather than files beside the checkpoint, because it is a *diagnostic* store
        with its own bound and its own lifetime: it is safe to delete, and a person debugging a bill
        should be able to read it without wading through run state. See :mod:`engine.cachestore`.
        """
        return self.state_dir / "cache"

    def agent_dir(self, agent_id: str) -> Path:
        """Per-agent directory, with the id validated the same way as the slug."""
        if not _SLUG_RE.match(agent_id or ""):
            raise StateError(f"invalid agent id {agent_id!r}")
        return self.agents_dir / agent_id

    def mailbox_path(self, agent_id: str) -> Path:
        """Per-agent append-only mailbox."""
        return self.agent_dir(agent_id) / "mailbox.jsonl"

    def session_dir(self, agent_id: str, session_id: str) -> Path:
        """Per-session archive directory."""
        if not _SLUG_RE.match(session_id or ""):
            raise StateError(f"invalid session id {session_id!r}")
        return self.sessions_dir / agent_id / session_id

    def ensure(self) -> "Workspace":
        """Create the directory tree. Idempotent, so opening twice is not an error.

        The one behavioural difference between the two modes: an **attached** workspace already
        exists and belongs to the user, so nothing outside ``.agent_state/`` is created. A managed
        workspace owns its ``docs/`` and ``src/``, but creating an empty ``src/`` inside a Go or Rust
        repository would be a visible, wrong mutation of the user's tree — performed by a command
        that may only have meant to inspect it.
        """
        if self.is_attached:
            for directory in (
                self.state_dir, self.agents_dir, self.handoffs_dir, self.sessions_dir,
                self.telemetry_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            return self
        for directory in (
            self.path, self.state_dir, self.docs_dir, self.src_dir,
            self.agents_dir, self.handoffs_dir, self.sessions_dir, self.telemetry_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def exists(self) -> bool:
        """True when the project directory has been initialised."""
        return self.state_dir.is_dir()

    # ── checkpoint ──────────────────────────────────────────────────────────

    def save_checkpoint(self, checkpoint: RunCheckpoint | dict[str, Any]) -> Path:
        """Atomically persist the run checkpoint.

        Temp-then-`os.replace` with an fsync, so a reader (the Swift inspector, or a
        resumed engine) sees either the previous complete checkpoint or the new one.
        """
        data = checkpoint.to_dict() if isinstance(checkpoint, RunCheckpoint) else dict(checkpoint)
        data["run_state_version"] = str(data.get("run_state_version", CHECKPOINT_VERSION))
        data["updated"] = _iso_now()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        target = self.checkpoint_path
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StateError(f"failed to write checkpoint {target}: {exc}") from exc
        return target

    def load_checkpoint(self) -> RunCheckpoint | None:
        """Load the checkpoint, or None when the project has no run to resume.

        Returning None rather than an empty checkpoint keeps the decision with the
        caller: a fresh run and a resumed run are different, and conflating them is how
        a resumed run gets handed a fresh budget.
        """
        target = self.checkpoint_path
        if not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateError(
                f"checkpoint {target} is corrupt: {exc.msg} (line {exc.lineno}). "
                "Refusing to resume from unreadable state."
            ) from exc
        if not isinstance(data, dict):
            raise StateError(f"checkpoint {target} must be a JSON object")
        return RunCheckpoint.from_dict(data)

    def read_checkpoint_raw(self) -> dict[str, Any] | None:
        """Raw checkpoint dict, for the schema registry to version-check before use."""
        target = self.checkpoint_path
        if not target.is_file():
            return None
        return json.loads(target.read_text(encoding="utf-8"))

    # ── generic documents ───────────────────────────────────────────────────

    def write_json(self, relative: str, data: dict[str, Any]) -> Path:
        """Atomically write a JSON document under the state directory.

        The relative path is validated so a caller cannot escape `.agent_state/`.
        """
        target = self._resolve_state_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StateError(f"failed to write {relative}: {exc}") from exc
        return target

    def read_json(self, relative: str) -> dict[str, Any] | None:
        """Read a JSON document under the state directory, or None when absent."""
        target = self._resolve_state_path(relative)
        if not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateError(f"{relative} is corrupt: {exc.msg}") from exc
        return data if isinstance(data, dict) else None

    def _resolve_state_path(self, relative: str) -> Path:
        """Resolve a path inside `.agent_state/`, rejecting any escape attempt."""
        candidate = Path(relative)
        if candidate.is_absolute():
            raise StateError(f"state path must be relative: {relative}")
        for part in candidate.parts:
            if part in ("..", "."):
                raise StateError(f"state path contains a traversal segment: {relative}")
        target = (self.state_dir / candidate).resolve(strict=False)
        try:
            target.relative_to(self.state_dir.resolve(strict=False))
        except ValueError:
            raise StateError(f"state path escapes .agent_state/: {relative}") from None
        return target

    # ── housekeeping ────────────────────────────────────────────────────────

    def list_projects(root: os.PathLike | str | None = None) -> list[str]:
        """List project slugs under the root, sorted. A convenience for the CLI/UI."""
        base = Path(root) if root is not None else Path(__file__).resolve().parent.parent / "projects"
        if not base.is_dir():
            return []
        return sorted(
            entry.name for entry in base.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )

    def size_bytes(self) -> int:
        """Total bytes under the project directory, for the resources view."""
        if not self.path.exists():
            return 0
        total = 0
        for path in self.path.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total
