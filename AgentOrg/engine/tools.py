#!/usr/bin/env python3
"""tools.py — the tools an agent can actually call, and the gate that decides whether it may.

WHY THIS EXISTS
---------------
Before this module the engine could not read a project file. A node received *artifacts a previous
node produced* — a path and a sha256 — and nothing else. So an agent asked to "add a field to
User.swift" never saw `User.swift`: it wrote plausible code from the task text alone.

That is the difference between a pipeline that hands work down and an agent that works *on a repo*.
This module closes it by giving an agent the four things a coding agent needs — read a file, list a
directory, search, and write — and by making the *permission* to do each one a first-class check
rather than an assumption.

DESIGN
------
- **Every tool is bounded by the workspace.** A path is resolved against the project root and a path
  that escapes it is refused. The check is on the *resolved* path, so a symlink pointing out of the
  project is caught the same as a `../` traversal.
- **Read and write are different permissions.** `read_file` needs `read:<path>`; `write_file` needs
  `write:<path>`. A reviewer hired with only `read:*` can inspect the code it judges and cannot touch
  it — which is what makes the independence guarantee real rather than advisory.
- **A refusal explains itself.** A tool that returns "denied" teaches the model nothing, and it will
  try the same call again. The result names what was attempted, the permission required, and what the
  agent actually holds, so the model can adapt or ask.
- **Reading is cheap and writing is not.** Reads have a generous size cap (a truncated read is still
  useful, and says it was truncated); writes are capped far lower and always go through the
  workspace's atomic writer, so a reader never sees a half-written file.
- **Nothing here shells out.** `run_command` is deliberately absent: an unconstrained shell inside a
  real repository is a much larger risk than a file write, and it deserves its own decision rather
  than being smuggled in beside `read_file`.

Usage:
    registry = ToolRegistry(workspace_root=Path("~/code/my-app"), agent=alice)
    for spec in registry.specs():        # what to advertise to the model
        ...
    result = registry.call("read_file", {"path": "src/app.py"})
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .state import ENGINE_STATE_DIRNAME

__all__ = [
    "ToolError", "ToolResult", "Tool", "ToolRegistry",
    "READ_CAPABILITY", "WRITE_CAPABILITY",
]

#: Capability namespaces. An agent's grant list is matched by prefix, so `read:src/` covers
#: `read:src/app.py` — the least-privilege shape the delegation ladder already speaks.
READ_CAPABILITY = "read"
WRITE_CAPABILITY = "write"

#: The largest file a read will return. A truncated read still tells the agent most of what it needs
#: and the result says it was truncated, which is far more useful than refusing outright.
MAX_READ_BYTES = 256 * 1024
#: The largest write accepted through a tool call. Lower than the read cap deliberately: a model that
#: wants to write 1 MB is almost certainly doing something other than editing a source file.
MAX_WRITE_BYTES = 512 * 1024
#: Directory entries returned for one listing, so a large tree cannot flood a prompt.
MAX_DIR_ENTRIES = 500
#: Search matches returned for one query.
MAX_SEARCH_HITS = 100
#: Files a search will open before giving up, so a search over a huge tree stays bounded.
MAX_SEARCH_FILES = 5_000

#: Directory names never descended into: build output and dependency trees are noise, and scanning
#: them wastes the one budget the agent cannot get back.
SKIP_DIRS = frozenset({
    ".git", ".build", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "DerivedData", ".swiftpm", "Pods",
})

#: The engine's own state directory. An agent may neither read nor write it, because the checkpoint,
#: the effect journal and the trace are what makes a run resumable and idempotent — an agent that can
#: rewrite its own checkpoint can fabricate a resume, and one that can delete `effects.jsonl` can
#: defeat idempotency. Pointing a workspace at a real repository (``Workspace.attach``) puts this
#: directory inside the tree the tools reach, so the exclusion is explicit rather than incidental.
#: Imported from `state.py`, which owns the layout, so the name cannot drift into two spellings.


class ToolError(RuntimeError):
    """A tool call that could not be honoured, named so the model can read the reason."""


def _format_ref(ref: dict[str, Any], *, preview_bytes: int = 1200) -> str:
    """Render one subagent reference for the model.

    Deliberately compact: the whole point of isolation is that N children cost the parent N *previews*
    rather than N transcripts, so a reference names the status, the counters, the child's own summary,
    and the transcript it can page — never the transcript itself.
    """
    preview = str(ref.get("preview") or ref.get("summary") or "")[:preview_bytes]
    lines = [
        f"[subagent {ref.get('child_id')}] {ref.get('status')}"
        + (f" · agent {ref.get('agent_id')}" if ref.get("agent_id") else "")
        + (f" · skill {ref.get('skill')}" if ref.get("skill") else ""),
        f"  task      : {str(ref.get('task') or '')[:200]}",
        f"  steps     : {ref.get('steps')}  tokens: {ref.get('tokens_in')}in/{ref.get('tokens_out')}out",
    ]
    if preview:
        lines.append(f"  summary   : {preview}")
    if ref.get("error"):
        lines.append(f"  error     : {ref['error']}")
    total = ref.get("bytes") or 0
    if total:
        lines.append(f"  transcript: {total} bytes — read with read_subagent_result("
                     f"child_id=\"{ref.get('child_id')}\")")
    return "\n".join(lines)


@dataclass
class ToolResult:
    """What a tool call produced.

    `ok` is separate from the text because a refusal is a *successful* call that returned a no: the
    model must see the reason, and the loop must know not to treat it as a crash.
    """

    ok: bool
    text: str
    #: True when a read hit the cap and the content is incomplete. The model must know, or it will
    #: reason about a file as though it had seen all of it.
    truncated: bool = False
    #: Files touched, for the run's artifact record.
    paths: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "text": self.text, "truncated": self.truncated,
                "paths": list(self.paths)}


@dataclass(frozen=True)
class Tool:
    """One callable tool: its advertised schema and the implementation."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult]
    #: Whether this tool modifies the workspace. Used to gate it and to label it in the prompt.
    mutates: bool = False

    def spec(self) -> Any:
        """The provider-neutral spec to advertise."""
        from .providers.base import ToolSpec

        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


class ToolRegistry:
    """The tools one agent may use, with capability enforcement.

    Parameters
    ----------
    workspace_root:
        The project root. Every path a tool touches is resolved against it and must stay inside it.
    agent:
        The agent whose capabilities are checked. `None` means no agent context — used for internal
        calls (the planner reading a manifest) where the capability gate would be meaningless.
    writer:
        Optional atomic writer (the workspace's `ArtifactStore`). When supplied, writes go through it
        so containment and atomicity have one implementation rather than two.
    read_only:
        Forbids every mutating tool regardless of capability. Set when a run is inspecting a project
        rather than changing it, so "nothing is written" is a property of the run and not a hope.
    """

    def __init__(self, *, workspace_root: Path | str, agent: Any = None,
                 writer: Any = None, read_only: bool = False,
                 goal_workspace: Any = None, subagents: Any = None) -> None:
        self.root = Path(workspace_root).resolve()
        self.agent = agent
        self.writer = writer
        self.read_only = read_only
        #: Set when a goal is armed: the workspace whose `.agent_state/` receives the agent's verdict.
        #: None means no goal is in play, so `update_goal` is not advertised at all.
        self.goal_workspace = goal_workspace
        #: Set when isolated subagents are available: an object with `.run_task()` / `.run_fleet()`
        #: (see `subagents.SubagentRunner`). None means `task`/`fleet` are not advertised, so a node
        #: without a runner cannot dispatch children it has no way to collect.
        self.subagents = subagents
        self._tools: dict[str, Tool] = {}
        self._register_defaults()

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[Any]:
        """The specs to advertise, in a stable order (which matters: a reordering is a cache miss)."""
        return [self._tools[name].spec() for name in self.names()]

    # ── the permission gate ─────────────────────────────────────────────────

    def _capabilities(self) -> list[str]:
        if self.agent is None:
            # No agent: internal use. The workspace containment check still applies.
            return [f"{READ_CAPABILITY}:*", f"{WRITE_CAPABILITY}:*"]
        return list(getattr(self.agent, "capabilities", []) or [])

    def _grants(self, kind: str, relative: str) -> bool:
        """Whether the agent holds `kind` over a workspace-relative path.

        Matched by prefix on the *normalised* relative path, so `read:src/` grants `read:src/app.py`
        and `read:*` grants everything. A grant is a path scope, not a boolean, which is what lets one
        agent be trusted with `src/` and not with `.github/`.
        """
        need = f"{kind}:{relative}"
        for granted in self._capabilities():
            if not granted.startswith(f"{kind}:"):
                continue
            scope = granted.split(":", 1)[1]
            if scope in ("*", ""):
                return True
            # `read:src/**` and `read:src/` and `read:src` all mean "under src".
            stem = scope.rstrip("*").rstrip("/")
            if not stem:
                return True
            if relative == stem or relative.startswith(stem + "/"):
                return True
        return False

    def _resolve(self, raw: Any, *, must_exist: bool = False) -> Path:
        """Resolve a workspace-relative path, refusing anything outside the workspace.

        Refuses rather than clamping: a tool call naming `../../.ssh/id_rsa` is either a mistake or an
        attack, and silently rewriting it to a safe path would hide both.
        """
        text = str(raw or "").strip()
        if not text:
            raise ToolError("a path is required")
        candidate = Path(text)
        if candidate.is_absolute():
            raise ToolError(
                f"paths must be relative to the project, got an absolute path: {text}")
        for part in candidate.parts:
            if part == "..":
                raise ToolError(
                    f"the path {text!r} contains a traversal segment, which is never allowed")
        # `~` is named explicitly rather than left to resolve as a literal directory: a model writing
        # `~/.ssh/id_rsa` means the user's home, and saying so is more useful than reporting that a
        # directory called `~` does not exist inside the project. It is also the shape of a path that
        # would escape if any layer expanded it, so the refusal is worth stating plainly.
        if any(part.startswith("~") for part in candidate.parts):
            raise ToolError(
                f"the path {text!r} looks like a home-directory path; tools only reach inside the "
                "project")
        resolved = (self.root / candidate).resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ToolError(
                f"the path {text!r} resolves outside the project "
                f"({resolved} is not under {self.root})") from None
        # The engine's own state is not project content. Checked on the *resolved* path so a symlink
        # into `.agent_state/` is refused too, and checked here rather than per-tool so every tool —
        # read, write, list, glob, grep — inherits the exclusion from one place.
        if ENGINE_STATE_DIRNAME in resolved.relative_to(self.root).parts:
            raise ToolError(
                f"the path {text!r} is inside {ENGINE_STATE_DIRNAME}/, which is the engine's own run "
                "state (checkpoint, effect journal, trace). It is not part of the project and is not "
                "readable or writable by tools.")
        if must_exist and not resolved.exists():
            raise ToolError(f"no such file or directory: {text}")
        return resolved

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    # ── dispatch ────────────────────────────────────────────────────────────

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Invoke a tool, enforcing capability and containment.

        A refusal is returned as a `ToolResult(ok=False, ...)` rather than raised, because an agent
        that is told *why* it may not do something can carry on and route around it; an exception
        would end the node and lose the work it had already done.
        """
        tool = self._tools.get(str(name))
        if tool is None:
            return ToolResult(False, f"no tool named {name!r}; available: {', '.join(self.names())}")
        if tool.mutates and self.read_only:
            return ToolResult(
                False,
                f"{name} is not available: this run is read-only, so nothing in the project is "
                "modified. Report what you would change instead.")
        try:
            return tool.handler(dict(arguments or {}))
        except ToolError as exc:
            return ToolResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001 - a tool must never kill the node
            return ToolResult(False, f"{name} failed: {type(exc).__name__}: {exc}")

    # ── the tools ───────────────────────────────────────────────────────────

    def _register_defaults(self) -> None:
        self.register(Tool(
            name="read_file",
            description=(
                "Read a text file from the project. Returns the contents with line numbers so you can "
                "cite an exact location. The path is relative to the project root."),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "project-relative path"},
                    "start_line": {"type": "integer", "description": "1-based first line"},
                    "max_lines": {"type": "integer", "description": "how many lines to return"},
                },
                "required": ["path"],
            },
            handler=self._read_file,
        ))
        self.register(Tool(
            name="list_dir",
            description=(
                "List the entries of a project directory. Directories are marked with a trailing "
                "slash. Use this to learn the layout before reading individual files."),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "project-relative directory (default .)"},
                },
            },
            handler=self._list_dir,
        ))
        self.register(Tool(
            name="search",
            description=(
                "Search project files for a literal string. Returns matching lines with their file "
                "and line number. Use this to find where something is defined or used."),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "the literal text to find"},
                    "path": {"type": "string", "description": "directory to search (default .)"},
                    "max_hits": {"type": "integer", "description": "cap on matches returned"},
                },
                "required": ["query"],
            },
            handler=self._search,
        ))
        self.register(Tool(
            name="write_file",
            description=(
                "Write a text file in the project, replacing anything already there. The write is "
                "atomic. This is how you make a change — produce the ENTIRE file contents."),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "project-relative path"},
                    "content": {"type": "string", "description": "the entire file contents"},
                },
                "required": ["path", "content"],
            },
            handler=self._write_file,
            mutates=True,
        ))
        if self.goal_workspace is not None:
            # Only advertised when a goal is armed. The same instinct as `read_only`: a capability not
            # in play should not be in the prompt, and because the tool block feeds the prefix hash,
            # arming a goal is a *cache-shape* change that `cache.py` attributes by name.
            self.register(Tool(
                name="update_goal",
                description=(
                    "Report the outcome of the ACTIVE GOAL. Call this when the whole objective is "
                    "finished (complete) or when you are genuinely unable to proceed without the user "
                    "(blocked). This is the only way a goal ends — no other signal stops it, so "
                    "calling it with a false 'complete' ends the run prematurely."),
                parameters={
                    "type": "object",
                    "properties": {
                        "verdict": {"type": "string",
                                    "description": "'complete' or 'blocked'",
                                    "enum": ["complete", "blocked"]},
                        "summary": {"type": "string",
                                    "description": ("for complete: what was achieved. For blocked: the "
                                                    "concrete blocker and what you need.")},
                    },
                    "required": ["verdict", "summary"],
                },
                handler=self._update_goal,
            ))
        if self.subagents is not None:
            # Advertised together, because they are one capability: dispatch work to isolated children
            # and read what they found. `task`/`fleet` are the two Kimi shapes — one child, or N in
            # parallel — and `read_subagent_result` is how the parent pages a child's transcript
            # instead of taking all of it or none.
            self.register(Tool(
                name="task",
                description=(
                    "Run ONE subagent in its own isolated context. It gets its own conversation and "
                    "returns a short summary plus a reference. Use it for a focused question you do "
                    "not want cluttering your own context. Read the full result with "
                    "read_subagent_result if the summary is not enough."),
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string",
                                   "description": "the task for the subagent, self-contained"},
                        "skill": {"type": "string",
                                  "description": "which skill's holder should do it (default: yours)"},
                    },
                    "required": ["prompt"],
                },
                handler=self._task,
            ))
            self.register(Tool(
                name="fleet",
                description=(
                    "Run SEVERAL subagents in parallel, each in its own isolated context, and return "
                    "one reference per task. Read-only by policy: use it for research and review that "
                    "can be split. For writes, this is the wrong tool — do the work directly."),
                parameters={
                    "type": "object",
                    "properties": {
                        "tasks": {"type": "array", "items": {"type": "string"},
                                  "description": "one prompt per subagent"},
                        "skill": {"type": "string",
                                  "description": "which skill's holders should do them"},
                    },
                    "required": ["tasks"],
                },
                handler=self._fleet,
            ))
            self.register(Tool(
                name="read_subagent_result",
                description=(
                    "Read a slice of a subagent's full transcript. The result says how many bytes came "
                    "back and whether more remain — page with offset_bytes instead of assuming you "
                    "saw everything."),
                parameters={
                    "type": "object",
                    "properties": {
                        "child_id": {"type": "string",
                                     "description": "the child id from a task/fleet reference"},
                        "offset_bytes": {"type": "integer",
                                         "description": "byte offset to start at (default 0)"},
                        "limit_bytes": {"type": "integer",
                                        "description": "how many bytes to return (default 8192)"},
                    },
                    "required": ["child_id"],
                },
                handler=self._read_subagent_result,
            ))

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    # -- read ---------------------------------------------------------------

    def _read_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve(args.get("path"), must_exist=True)
        relative = self._relative(path)
        if not self._granted(READ_CAPABILITY, relative):
            return self._denied(READ_CAPABILITY, relative, "read_file")
        if path.is_dir():
            return ToolResult(False, f"{relative} is a directory; use list_dir")

        try:
            raw = path.read_bytes()
        except OSError as exc:
            return ToolResult(False, f"cannot read {relative}: {exc}")
        truncated = len(raw) > MAX_READ_BYTES
        text = raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")

        lines = text.splitlines()
        start = max(1, int(args.get("start_line") or 1))
        limit = int(args.get("max_lines") or 0) or len(lines)
        window = lines[start - 1:start - 1 + limit]
        body = "\n".join(f"{start + i:6d}\t{line}" for i, line in enumerate(window))
        note = ""
        if truncated:
            note = (f"\n\n[truncated at {MAX_READ_BYTES} bytes; the file is larger. "
                    "Read a line range to see the rest.]")
        elif start - 1 + len(window) < len(lines):
            note = f"\n\n[showing lines {start}-{start + len(window) - 1} of {len(lines)}]"
        return ToolResult(True, f"{relative} ({len(lines)} lines):\n{body}{note}", truncated,
                          paths=[relative])

    def _list_dir(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve(args.get("path") or ".", must_exist=True)
        relative = self._relative(path) if str(args.get("path") or ".") not in ("", ".") else "."
        if not self._granted(READ_CAPABILITY, "" if relative == "." else relative):
            return self._denied(READ_CAPABILITY, relative, "list_dir")
        if not path.is_dir():
            return ToolResult(False, f"{relative} is a file; use read_file")

        entries: list[str] = []
        try:
            for entry in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
                if entry.name in SKIP_DIRS:
                    continue
                suffix = "/" if entry.is_dir() else ""
                entries.append(entry.name + suffix)
                if len(entries) >= MAX_DIR_ENTRIES:
                    break
        except OSError as exc:
            return ToolResult(False, f"cannot list {relative}: {exc}")
        more = "" if len(entries) < MAX_DIR_ENTRIES else "\n[more entries omitted]"
        return ToolResult(True, f"{relative}:\n" + "\n".join(entries) + more,
                          paths=[relative])

    def _search(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "")
        if not query:
            return ToolResult(False, "a query is required")
        root = self._resolve(args.get("path") or ".", must_exist=True)
        relative_root = self._relative(root)
        if not self._granted(READ_CAPABILITY, "" if relative_root == "." else relative_root):
            return self._denied(READ_CAPABILITY, relative_root, "search")
        limit = min(int(args.get("max_hits") or MAX_SEARCH_HITS), MAX_SEARCH_HITS)

        hits: list[str] = []
        scanned = 0
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                scanned += 1
                if scanned > MAX_SEARCH_FILES:
                    break
                full = Path(directory) / filename
                try:
                    if full.stat().st_size > MAX_READ_BYTES:
                        continue
                    text = full.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for number, line in enumerate(text.splitlines(), start=1):
                    if query in line:
                        hits.append(f"{self._relative(full)}:{number}: {line.strip()[:200]}")
                        if len(hits) >= limit:
                            break
                if len(hits) >= limit:
                    break
            if len(hits) >= limit or scanned > MAX_SEARCH_FILES:
                break
        if not hits:
            return ToolResult(True, f"no matches for {query!r}")
        body = "\n".join(hits)
        note = "" if len(hits) < limit else f"\n[stopped at {limit} matches]"
        return ToolResult(True, body + note)

    # -- write --------------------------------------------------------------

    def _write_file(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        content = args.get("content")
        if content is None:
            return ToolResult(False, "content is required: a write with no contents would truncate "
                                     "the file")
        text = str(content)
        if len(text.encode("utf-8")) > MAX_WRITE_BYTES:
            return ToolResult(False, f"the content is larger than {MAX_WRITE_BYTES} bytes; split it "
                                     "into several files or write a narrower change")
        path = self._resolve(raw_path)
        relative = self._relative(path)
        if not self._granted(WRITE_CAPABILITY, relative):
            return self._denied(WRITE_CAPABILITY, relative, "write_file")
        if path.exists() and path.is_dir():
            return ToolResult(False, f"{relative} is a directory")

        # History is the one part of a repository that cannot be re-derived, and a bad ref write is not
        # a checkpoint you can resume from — so a *file* write into `.git/` is refused. A `git` tool that
        # runs commands is a separate, reviewable capability; this only closes the direct-write path.
        if ".git" in path.relative_to(self.root).parts:
            return ToolResult(False, f"{relative} is inside .git/, which tools do not write to: "
                                     "repository history is not a file an agent should edit directly")

        # A write is refused when the target is a symlink out of the workspace: `_resolve` already
        # catches the resolved escape, and this catches the case where the *link* would be followed
        # on a later read.
        if path.is_symlink():
            return ToolResult(False, f"{relative} is a symlink; refusing to write through it")

        if self.writer is not None:
            # The workspace's own writer: one implementation of containment + atomicity.
            try:
                self.writer.write(relative, text, artifact_type="file")
            except Exception as exc:  # noqa: BLE001 - reported to the model, not raised
                return ToolResult(False, f"cannot write {relative}: {exc}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        return ToolResult(True, f"wrote {relative} ({len(text.splitlines())} lines)",
                          paths=[relative])

    # -- the goal verdict ---------------------------------------------------

    def _update_goal(self, args: dict[str, Any]) -> ToolResult:
        """Record the agent's verdict on the active goal, for the orchestrator to act on.

        Deliberately a *write* rather than a return value: the executor runs in a subprocess, so the
        only channel back to the orchestrator is the filesystem. The verdict is consumed on read, so a
        stale 'complete' cannot stop a later round.
        """
        from .goal import GoalDecision

        verdict = str(args.get("verdict") or "").strip().lower()
        summary = str(args.get("summary") or "").strip()
        if verdict not in ("complete", "blocked"):
            return ToolResult(False, "verdict must be 'complete' or 'blocked'")
        if not summary:
            # A completion with no summary is the silent-done failure the design refuses: there would
            # be no record of what was achieved.
            return ToolResult(False, "a summary is required, so the outcome is on the record")
        if self.goal_workspace is None:
            return ToolResult(False, "no goal is active, so there is nothing to update")
        try:
            GoalDecision(verdict=verdict, summary=summary).save(self.goal_workspace)
        except Exception as exc:  # noqa: BLE001 - reported to the model, not raised
            return ToolResult(False, f"cannot record the goal outcome: {exc}")
        wording = "completed" if verdict == "complete" else "blocked"
        return ToolResult(True, f"goal {wording} recorded: {summary[:200]}")

    # -- isolated subagents ------------------------------------------------

    def _task(self, args: dict[str, Any]) -> ToolResult:
        """Run one subagent in its own context and return its reference."""
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(False, "a task needs a prompt")
        skill = str(args.get("skill") or "").strip() or self._current_skill()
        try:
            ref = self.subagents.run_task(prompt=prompt, skill=skill)
        except Exception as exc:  # noqa: BLE001 - a child failure is reported, not raised
            return ToolResult(False, f"the subagent could not run: {exc}")
        if ref.get("status") == "failed" and ref.get("error"):
            return ToolResult(False, f"subagent failed: {ref['error']}")
        return ToolResult(True, _format_ref(ref), paths=[])

    def _fleet(self, args: dict[str, Any]) -> ToolResult:
        """Run N subagents in parallel, each isolated, and return one reference per task."""
        raw = args.get("tasks") or []
        if not isinstance(raw, list) or not raw:
            return ToolResult(False, "fleet needs a non-empty tasks array")
        tasks = [str(t).strip() for t in raw if str(t).strip()]
        if not tasks:
            return ToolResult(False, "fleet needs at least one non-empty task")
        skill = str(args.get("skill") or "").strip() or self._current_skill()
        try:
            refs = self.subagents.run_fleet(tasks=tasks, skill=skill)
        except Exception as exc:  # noqa: BLE001 - reported to the model, not raised
            return ToolResult(False, f"the fleet could not run: {exc}")
        if not refs:
            return ToolResult(False, "the fleet produced no results")
        body = "\n\n".join(_format_ref(r) for r in refs)
        return ToolResult(True, f"{len(refs)} subagent(s):\n\n{body}")

    def _read_subagent_result(self, args: dict[str, Any]) -> ToolResult:
        """Return one byte range of a child's transcript, saying whether more remains."""
        child_id = str(args.get("child_id") or "").strip()
        if not child_id:
            return ToolResult(False, "a child id is required")
        try:
            page = self.subagents.read(child_id=child_id,
                                       offset_bytes=int(args.get("offset_bytes") or 0),
                                       limit_bytes=int(args.get("limit_bytes") or 0))
        except Exception as exc:  # noqa: BLE001 - a missing child is reported, not raised
            return ToolResult(False, f"cannot read that subagent result: {exc}")
        note = ""
        if page.more:
            note = (f"\n\n[read {page.returned_bytes} of {page.total_bytes} bytes; more remains. "
                    f"Continue with offset_bytes={page.offset_bytes + page.returned_bytes}]")
        else:
            note = f"\n\n[read {page.returned_bytes} of {page.total_bytes} bytes; end of transcript]"
        return ToolResult(True, f"{child_id}:\n{page.text}{note}", truncated=page.more)

    def _current_skill(self) -> str:
        """The agent's own primary skill, so a delegated task defaults to a peer rather than nobody."""
        return str(getattr(self.agent, "skill", "") or "")

    # -- the refusal --------------------------------------------------------

    def _granted(self, kind: str, relative: str) -> bool:
        return self._grants(kind, relative)

    def _denied(self, kind: str, relative: str, tool: str) -> ToolResult:
        """Explain a refusal in terms the model can act on.

        Naming the required permission *and* what the agent holds is what lets it either work around
        the restriction or tell the Owner what it needs — rather than retrying the same call, which is
        what an unexplained "denied" produces.
        """
        held = ", ".join(self._capabilities()) or "(none)"
        return ToolResult(
            False,
            f"{tool} refused: {kind} access to {relative!r} is not granted to this agent.\n"
            f"  required: {kind}:{relative}\n"
            f"  held    : {held}\n"
            "Do not retry this call. Complete what you can within your permissions, and record what "
            "you could not do as an open question."
        )
