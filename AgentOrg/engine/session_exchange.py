#!/usr/bin/env python3
"""session_exchange.py — a session you can list, hand over, and branch.

WHY THIS EXISTS
---------------
A run's whole story is already on disk in `.agent_state/`: the checkpoint, the goal, the trace, the
ledger, the crossings between nodes and the cache record. What was missing was every operation over
that story a person actually wants. You could not see *which* sessions a projects root holds, you
could not hand one to another machine (or archive one before deleting the project it lived in), and
you could not try an alternative continuation — re-running the same objective overwrites the state
that recorded what the first attempt did, which destroys the evidence you wanted to compare against.
The reference agent answers those three with `list`, `export` and `fork`; these are the same three,
over state that already exists rather than over a second store.

DESIGN
------
- **A session is a workspace.** `run_state.json` is one checkpoint, so a project's `.agent_state/`
  *is* one session, and a projects root is the set of sessions a person has. Hence `list` takes a
  root and everything else takes one workspace — not an index file that could disagree with the
  directories it claims to describe.
- **Reading is defensive; writing is not.** A session whose `run_state.json` is the library runner's
  shape, or whose goal was written by an older build, is reported as unreadable rather than crashing
  the listing: someone asking "what do I have" must get an answer even when one of the answers is
  "this one is broken". `export` and `fork`, by contrast, refuse loudly, because an archive that
  quietly omits a file is worse than no archive.
- **`fork` copies and never moves.** It writes only into a directory that does not exist yet, so the
  original is byte-identical afterwards by construction rather than by care. A branch that could
  damage the thing it branched from would be worse than no branch.
- **An export describes itself.** `manifest.json` names every member with its size and hash and
  records the schema versions of what went in, so the receiving side can tell what it has *before*
  trusting it (`verify_export`), rather than after a run has already resumed from it.

Usage:
    sessions = list_sessions(sessions_under(Path("projects")))
    detail = session_detail(Workspace.for_project("console"))
    export_session(session, Path("console-session.zip"))
    fork_session(session, "console-alt")
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable

from .goal import Goal, GoalError
from .state import StateError, Workspace

__all__ = [
    "SessionError",
    "SESSION_EXPORT_VERSION",
    "EXPORTED_FILES",
    "EXPORTED_DIRS",
    "session_summary",
    "session_detail",
    "list_sessions",
    "sessions_under",
    "export_session",
    "verify_export",
    "fork_session",
]

#: Bumped when the archive's shape changes incompatibly. The manifest carries it so a receiver can
#: refuse an archive it does not understand rather than half-reading it.
SESSION_EXPORT_VERSION = "1.0.0"

#: The session files an export carries, relative to `.agent_state/`. Named once here so the manifest
#: and the archive cannot disagree about what a session is. `telemetry/` and `memory/` travel too:
#: they are what a receiving machine uses to explain a run it did not execute.
EXPORTED_FILES: tuple[str, ...] = (
    "run_state.json",
    "goal.json",
    "goal_decision.json",
    "mission.json",
    "runner_state.json",
    "ledger.jsonl",
    "trace.jsonl",
    "diagnostics.jsonl",
    "effects.jsonl",
    "telemetry/spans.jsonl",
)

#: Directories copied wholesale: one file per crossing, and the memory store.
EXPORTED_DIRS: tuple[str, ...] = ("handoffs", "memory")

#: The cache is summarised rather than shipped — it is a diagnostic store keyed by content hash, so a
#: receiving machine's own store is as valid as ours; what a person wants to know is *how the cache
#: behaved*, which is one small document.
CACHE_SUMMARY_NAME = "cache/summary.json"

#: What a fork records in the new session, so the branch is attributable to what it came from.
FORK_FILENAME = "fork.json"

_ISO = "%Y-%m-%dT%H:%M:%S"


class SessionError(RuntimeError):
    """A session operation that cannot be honoured, named so the reason is actionable."""


def _iso_now() -> str:
    return time.strftime(_ISO, time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def _iso_at(epoch: float) -> str:
    return time.strftime(_ISO, time.gmtime(epoch)) + f".{int(epoch * 1000) % 1000:03d}Z"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace(session: Any) -> Workspace:
    """Coerce whatever the caller passed into a Workspace, or say why it cannot be one.

    Tolerating a path keeps the module usable from a script that has a directory in hand; refusing a
    path that does not exist keeps a typo from reading as an empty session.
    """
    if isinstance(session, Workspace):
        return session
    path = Path(session).expanduser()
    if not path.is_dir():
        raise SessionError(
            f"{path} is not a directory, so there is no session to read. Point at a project folder "
            "or use --slug/--root for a managed one."
        )
    try:
        return Workspace.attach(path)
    except StateError as exc:
        raise SessionError(str(exc)) from exc


def _is_temporary(name: str) -> bool:
    """Whether a file is a half-written temp file from an interrupted atomic write.

    Every durable write in the engine is temp-then-`os.replace`, so an interrupted process can leave
    `run_state.json.tmp.4132` behind. Copying one would carry a torn document into an archive whose
    whole promise is that it is complete.
    """
    return ".tmp." in name


# ── reading a session ────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON document, returning None when it is absent or unreadable.

    None rather than raising: a listing must survive one broken session, and "unreadable" is a fact
    about that session that the caller reports rather than an error it propagates.
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL document, skipping malformed lines (an interrupted append)."""
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    out.append(data)
    except OSError:
        return out
    return out


def _goal_for(workspace: Workspace) -> dict[str, Any]:
    """The goal document, tolerating one written by an older or newer build.

    Read through `Goal.from_dict` rather than `Goal.load`: `load` deliberately **disarms** what it
    reads (nothing may resume because a process restarted), and `session show` must report the state
    the goal is actually in on disk. Disarming here would print "paused (restored)" for an armed goal.
    """
    path = Goal.path_for(workspace)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        goal = Goal.from_dict(data)
    except (OSError, json.JSONDecodeError, GoalError):
        return {}
    return goal.public()


def _spend(checkpoint: dict[str, Any], goal_doc: dict[str, Any]) -> dict[str, Any]:
    """What this session has cost, from the engine's own two accounting paths.

    The goal's cumulative spend is preferred, because it is the figure that survives a pause and
    answers the question a person asks ("what has this cost me"). Falling back to the runner's
    per-node cost is what makes a session *without* a goal — a plain manifest run — still report a
    figure instead of nothing.

    An unmeasured cost stays `None`. Rendering it as `0.0` would state that a run was free when the
    truth is that nobody reported what it cost, which is the honesty rule the whole cache and
    telemetry layer keeps.
    """
    spend = goal_doc.get("spend") if isinstance(goal_doc.get("spend"), dict) else {}
    if spend:
        return {
            "source": "goal",
            "cost_usd": float(spend.get("cost_usd") or 0.0),
            "tokens": int(spend.get("tokens") or 0),
            "rounds": int(spend.get("rounds") or 0),
            "requests": int(spend.get("requests") or 0),
            "measured": True,
        }
    budget = checkpoint.get("budget") if isinstance(checkpoint.get("budget"), dict) else {}
    cost = budget.get("cost") if isinstance(budget.get("cost"), dict) else {}
    measured = bool(cost.get("measured"))
    tokens = ((cost.get("tokens_in") or 0) + (cost.get("tokens_out") or 0)) if measured else None
    return {
        "source": "run" if measured else "",
        "cost_usd": float(cost.get("cost_usd") or 0.0) if measured else None,
        "tokens": tokens,
        "rounds": 0,
        "requests": 0,
        "measured": measured,
    }


def _moved_at(workspace: Workspace, checkpoint: dict[str, Any]) -> str:
    """When this session last moved: the checkpoint's own stamp, else the newest state file's mtime.

    The file fallback exists for the legacy workspace whose `run_state.json` is the runner's shape —
    it has no `updated` field, and reporting it as "never" would hide a session that is plainly there.
    """
    updated = str(checkpoint.get("updated") or "")
    if updated:
        return updated
    newest = 0.0
    for name in ("run_state.json", "goal.json", "trace.jsonl", "ledger.jsonl"):
        try:
            newest = max(newest, (workspace.state_dir / name).stat().st_mtime)
        except OSError:
            continue
    return _iso_at(newest) if newest else ""


def session_summary(session: Any) -> dict[str, Any]:
    """One session's headline: what it is, where it is, what it cost, when it last moved.

    Read from the engine's own loaders rather than by re-parsing the state directory: the checkpoint
    is `Workspace.read_checkpoint_raw`, the goal is `Goal.load`, the handoffs are `Handoff.from_dict`.
    A second reader would drift from the first, and the first is what a run actually resumes from.
    """
    workspace = _workspace(session)
    state = workspace.state_dir
    summary: dict[str, Any] = {
        "slug": workspace.slug,
        "name": workspace.display_name,
        "path": str(workspace.path),
        "state_dir": str(state),
        "attached": workspace.is_attached,
        "present": state.is_dir(),
        "error": "",
        "run_id": "",
        "phase": "idle",
        "objective": "",
        "goal_state": "",
        "posture": "",
        "live": False,
        "stop_reason": "",
        "gate": "",
        "nodes": 0,
        "blocked_nodes": 0,
        "handoffs": 0,
        "spend": {"source": "", "cost_usd": None, "tokens": None, "measured": False},
        "updated": "",
    }
    if not state.is_dir():
        return summary

    checkpoint = _read_json(state / "run_state.json") or {}
    goal_doc = _goal_for(workspace)
    if not checkpoint and not goal_doc:
        summary["error"] = (
            "no readable run checkpoint or goal in this state directory; "
            "the session may have been written by a different build"
        )
        summary["updated"] = _moved_at(workspace, checkpoint)
        return summary

    outcome_nodes = (checkpoint.get("outcome") or {}).get("nodes") or {}
    nodes = outcome_nodes or (checkpoint.get("nodes") if isinstance(checkpoint.get("nodes"), dict) else {})
    gate = checkpoint.get("gate") if isinstance(checkpoint.get("gate"), dict) else {}
    handoffs_dir = state / "handoffs"
    handoff_count = 0
    if handoffs_dir.is_dir():
        handoff_count = sum(1 for path in handoffs_dir.glob("*.json")
                            if not _is_temporary(path.name))

    summary.update({
        "run_id": str(checkpoint.get("run_id") or checkpoint.get("workflow") or ""),
        "phase": str(checkpoint.get("phase") or checkpoint.get("status") or "idle"),
        "objective": str(goal_doc.get("objective") or checkpoint.get("goal")
                         or checkpoint.get("run_goal") or ""),
        "goal_state": str(goal_doc.get("state") or ""),
        "posture": str(goal_doc.get("posture") or ""),
        "live": bool(goal_doc.get("live")),
        "stop_reason": str(checkpoint.get("stop_reason") or ""),
        "gate": str(gate.get("gate_id") or ""),
        "nodes": len(nodes),
        "blocked_nodes": sum(1 for record in nodes.values()
                             if isinstance(record, dict)
                             and str(record.get("status")) == "blocked"),
        "handoffs": handoff_count,
        "spend": _spend(checkpoint, goal_doc),
        "updated": _moved_at(workspace, checkpoint),
    })
    if checkpoint.get("workflow") and not checkpoint.get("run_id"):
        # The library runner's checkpoint: this engine can read what happened but has no resumable
        # run here. Saying so is the honest answer; `session_detail` still shows the node table.
        summary["phase"] = str(checkpoint.get("phase") or checkpoint.get("status") or "idle")
    return summary


def _handoffs_for(workspace: Workspace) -> dict[str, Any]:
    """Every persisted crossing, read through the handoff contract that validates it.

    `Handoff.from_dict` verifies the payload checksum (rule R4), so a handoff that fails it is
    *reported* as refused rather than shown as if it were sound — a corrupted crossing is exactly the
    state whose whole point is to be caught.
    """
    from .org.handoff import Handoff, HandoffError

    directory = workspace.state_dir / "handoffs"
    out: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            if _is_temporary(path.name):
                continue
            data = _read_json(path)
            if data is None:
                refused.append({"handoff_id": path.stem, "reason": "unreadable JSON"})
                continue
            try:
                out.append(Handoff.from_dict(data).as_dict())
            except HandoffError as exc:
                refused.append({"handoff_id": path.stem, "reason": str(exc)})
    return {"handoffs": out, "refused": refused}


def session_detail(session: Any) -> dict[str, Any]:
    """One session in full: the goal, the run, the node table, the crossings, the spend.

    The node table comes from `engine.flow.build_flow`, which is the engine's one reader for "who is
    on what, and what moved between them" — the board the console shows. Reusing it here is what
    makes `session show` and `flow` agree instead of offering two accounts of one run.
    """
    workspace = _workspace(session)
    summary = session_summary(workspace)
    detail: dict[str, Any] = {
        **summary,
        "goal": _goal_for(workspace),
        "checkpoint": _read_json(workspace.state_dir / "run_state.json") or {},
        "runner_checkpoint": _read_json(workspace.state_dir / "runner_state.json") or {},
        "mission": _read_json(workspace.state_dir / "mission.json") or {},
        "ledger": _read_jsonl(workspace.state_dir / "ledger.jsonl"),
        "trace_events": len(_read_jsonl(workspace.state_dir / "trace.jsonl")),
        **_handoffs_for(workspace),
        "rows": [],
        "cache": {},
    }
    try:
        from .cachestore import CacheStore

        detail["cache"] = CacheStore.for_workspace(workspace).summary()
    except Exception as exc:  # noqa: BLE001 - an unreadable cache store is a missing figure, not a failure
        detail["cache"] = {"load_error": str(exc)}

    if workspace.state_dir.is_dir():
        try:
            from .flow import build_flow

            board = build_flow(workspace, limit=800)
            detail["rows"] = board.get("rows") or []
            detail["headline"] = board.get("headline") or ""
        except Exception as exc:  # noqa: BLE001 - the node table degrades, the session still shows
            detail["rows"] = []
            detail["headline"] = f"the node table could not be built: {exc}"
    return detail


def sessions_under(root: Any = None) -> list[Workspace]:
    """Every project under a projects root, as workspaces, in a stable order.

    Uses `Workspace.list_projects` rather than walking the directory here: the root's own notion of
    what a project is (a non-hidden directory) and where the default root is both live in one place,
    and a second walk would eventually disagree with the first. `root=None` means the engine's
    default projects directory, which is what every other command's `--root` defaults to.
    """
    base = Workspace.for_project("placeholder").root if root is None else Path(root).expanduser()
    return [Workspace.for_project(slug, root=base) for slug in Workspace.list_projects(base)]


def list_sessions(sessions: Iterable[Any]) -> list[dict[str, Any]]:
    """Summaries for several sessions, newest first.

    A session that cannot be read is still listed, with its error, because omitting it would make a
    broken session look like a session that never existed — the two need different answers from a
    person, and only one of them is "nothing to see here".
    """
    summaries = [session_summary(session) for session in sessions]
    summaries = [summary for summary in summaries if summary.get("present")]
    summaries.sort(key=lambda entry: (str(entry.get("updated") or ""), entry["slug"]), reverse=True)
    return summaries


# ── exporting ────────────────────────────────────────────────────────────────


def _export_members(workspace: Workspace) -> list[tuple[str, Path]]:
    """The `(arcname, source path)` pairs an export carries, skipping whatever is absent.

    Absent is not an error — most sessions have no `mission.json` and a fresh one has no trace yet —
    but it *is* recorded in the manifest, so a receiver can tell a session that never had a ledger
    from one whose ledger was dropped in transit.
    """
    state = workspace.state_dir
    members: list[tuple[str, Path]] = []
    for name in EXPORTED_FILES:
        path = state / name
        if path.is_file() and not _is_temporary(path.name):
            members.append((name, path))
    for dirname in EXPORTED_DIRS:
        directory = state / dirname
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not _is_temporary(path.name):
                members.append((f"{dirname}/{path.relative_to(directory).as_posix()}", path))
    return members


def _schema_versions(documents: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The schema version each exported document carries, read from the documents themselves.

    Read rather than asserted from constants: a session written by an older build must be archived
    *as* that version, so the receiver can see what it is dealing with instead of being told a version
    this build happens to speak.
    """
    versions: dict[str, str] = {}
    for name, document in documents.items():
        for key in ("run_state_version", "goal_version", "mission_version", "handoff_version",
                    "run_phase_version"):
            value = document.get(key)
            if value:
                versions[f"{name}:{key}"] = str(value)
                break
    return versions


def _leak_check(members: list[tuple[str, Path]]) -> None:
    """Refuse to build an archive that would carry a credential.

    The trace is already redacted at the bus boundary, so a hit here means something wrote a secret
    where it should not have — and an archive is the one artifact whose purpose is to leave this
    machine. `Diagnostics.bundle` refuses for the same reason; refusing in one place and warning in
    the other would make "is it safe to send?" depend on which command you happened to run.
    """
    from .config import scan_for_leaks

    findings: list[str] = []
    for arcname, path in members:
        for leak in scan_for_leaks(path, max_bytes=16 << 20):
            findings.append(f"{arcname}:{leak.line}")
    if findings:
        raise SessionError(
            f"refusing to export: {len(findings)} key-shaped string(s) found in the session "
            f"({', '.join(sorted(set(findings))[:5])}). An archive is meant to leave this machine, "
            "so it must not carry a credential. Remove the secret from the session first."
        )


def export_session(session: Any, destination: Any, *, leak_check: bool = True) -> dict[str, Any]:
    """Write a self-contained ZIP of one session, and return what went into it.

    `destination` is a file path, not a directory: the archive is the deliverable, and a command that
    guessed the filename would be one more thing to discover. An existing file is overwritten —
    unlike `fork`, nothing here is the sole copy of anything, and the export is reproducible from the
    session it came from.
    """
    workspace = _workspace(session)
    target = Path(destination).expanduser()
    if not workspace.state_dir.is_dir():
        raise SessionError(
            f"nothing to export: {workspace.path} has no .agent_state/. Point at a project the "
            "engine has run, or use --slug/--root for a managed one."
        )
    members = _export_members(workspace)
    if not members:
        raise SessionError(
            f"nothing to export: {workspace.state_dir} holds none of the session files "
            f"({', '.join(EXPORTED_FILES[:4])}…). Refusing to write an empty archive that looks "
            "like a session."
        )
    if leak_check:
        _leak_check(members)

    summary = session_summary(workspace)
    documents = {arcname: data for arcname, path in members
                 if (data := _read_json(path)) is not None}
    cache_summary: dict[str, Any] = {}
    try:
        from .cachestore import CacheStore

        cache_summary = CacheStore.for_workspace(workspace).summary()
    except Exception as exc:  # noqa: BLE001 - a missing cache figure is not a reason to refuse
        cache_summary = {"load_error": str(exc)}

    contains = [
        {"name": arcname, "bytes": path.stat().st_size, "sha256": _sha256_file(path)}
        for arcname, path in members
    ]
    absent = [name for name in EXPORTED_FILES
              if not (workspace.state_dir / name).is_file()]
    manifest = {
        "session_export_version": SESSION_EXPORT_VERSION,
        "slug": workspace.slug,
        "name": workspace.display_name,
        "exported_at": _iso_now(),
        "objective": summary["objective"],
        "phase": summary["phase"],
        "run_id": summary["run_id"],
        "updated": summary["updated"],
        "schema_versions": _schema_versions(documents),
        "contains": contains,
        "absent": absent,
        "counts": {
            "files": len(contains),
            "bytes": sum(entry["bytes"] for entry in contains),
            "handoffs": summary["handoffs"],
            "nodes": summary["nodes"],
        },
        "spend": summary["spend"],
    }

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            for arcname, path in members:
                archive.write(path, arcname=arcname)
            archive.writestr(CACHE_SUMMARY_NAME,
                             json.dumps(cache_summary, indent=2, sort_keys=True, default=str))
            archive.writestr("manifest.json",
                             json.dumps(manifest, indent=2, sort_keys=True, default=str))
        # The archive is the deliverable and it may be the only copy of a run, so it lands the way
        # every other durable write in the engine does: temp, replace, never a half-written ZIP.
        os.replace(tmp, target)
    except (OSError, zipfile.BadZipFile) as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise SessionError(f"cannot write the session archive to {target}: {exc}") from exc

    return {"path": str(target), "bytes": target.stat().st_size, "manifest": manifest}


def verify_export(archive: Any) -> dict[str, Any]:
    """Read an export's manifest and check every member against it. Returns the manifest.

    Raises when the archive is unreadable, has no manifest, or a listed member is missing or does not
    hash to what the manifest claims. The point of exporting a *hash* per member is that the receiving
    side can refuse a truncated or corrupted archive rather than resuming a run from it.
    """
    target = Path(archive).expanduser()
    if not target.is_file():
        raise SessionError(f"no session archive at {target}")
    try:
        with zipfile.ZipFile(target, "r") as handle:
            names = set(handle.namelist())
            if "manifest.json" not in names:
                raise SessionError(
                    f"{target} has no manifest.json, so it is not a session export. Refusing to "
                    "guess what it contains."
                )
            manifest = json.loads(handle.read("manifest.json").decode("utf-8"))
            if not isinstance(manifest, dict):
                raise SessionError(f"{target}: manifest.json must be a JSON object")
            version = str(manifest.get("session_export_version") or "")
            if version.split(".")[0] != SESSION_EXPORT_VERSION.split(".")[0]:
                raise SessionError(
                    f"{target}: session export version {version or '(none)'} is not compatible with "
                    f"{SESSION_EXPORT_VERSION}; it was written by a different build"
                )
            for entry in manifest.get("contains") or []:
                name = str(entry.get("name") or "")
                if name not in names:
                    raise SessionError(
                        f"{target}: the manifest claims {name!r}, which is not in the archive. "
                        "The archive is incomplete — do not fork or resume from it."
                    )
                digest = hashlib.sha256(handle.read(name)).hexdigest()
                if digest != str(entry.get("sha256") or ""):
                    raise SessionError(
                        f"{target}: {name} does not hash to what the manifest recorded. "
                        "The archive is corrupt — request a fresh export."
                    )
    except zipfile.BadZipFile as exc:
        raise SessionError(f"{target} is not a readable ZIP archive: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SessionError(f"{target}: manifest.json is not valid JSON: {exc}") from exc
    return manifest


# ── forking ──────────────────────────────────────────────────────────────────


def _copy_tree(source: Path, destination: Path) -> dict[str, int]:
    """Copy a state directory file by file, preserving mtimes, skipping interrupted writes."""
    files = 0
    total = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(_is_temporary(part) for part in relative.parts):
            continue
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # `copy2` keeps the mtime, which is what makes the fork's own "when did this last move"
        # answer the same question the original answered rather than the moment of copying.
        target.write_bytes(path.read_bytes())
        try:
            os.utime(target, (path.stat().st_atime, path.stat().st_mtime))
        except OSError:
            pass
        files += 1
        total += target.stat().st_size
    return {"files": files, "bytes": total}


def fork_session(session: Any, slug: str, *, root: Any = None,
                 copy_cache: bool = True) -> dict[str, Any]:
    """Branch a session into a **new slug**, leaving the source untouched.

    The safety property, in order of how it is guaranteed:

    1. **Nothing is written outside the new directory.** The source is only ever read, so it is
       byte-identical afterwards by construction — a `move` or an in-place edit would make the
       original's continuation depend on the branch's, which is the opposite of what a branch is for.
    2. **An existing destination is refused, never overwritten.** Forking onto a slug that already
       exists would destroy a session to create one, and the caller almost certainly meant a
       different name or did not know the other session was there.
    3. **The copy is complete before it is stamped.** The provenance record and the rewritten
       manifest path are written last, so an interrupted fork leaves an unstamped directory the caller
       can see and delete rather than a half-session that looks finished.

    The new session's checkpoint keeps its `run_id` — it *is* the same run, branched — and gains a
    `fork.json` naming where it came from and when.
    """
    source = _workspace(session)
    if not source.state_dir.is_dir():
        raise SessionError(
            f"nothing to fork: {source.path} has no .agent_state/. A fork copies a session's state, "
            "so there has to be state to copy."
        )
    try:
        destination = Workspace.for_project(
            slug, root=Path(root).expanduser() if root is not None else source.root)
    except StateError as exc:
        raise SessionError(f"cannot fork onto {slug!r}: {exc}") from exc
    if destination.path.resolve() == source.path.resolve():
        raise SessionError(
            f"refusing to fork {source.slug!r} onto itself. A fork must name a different slug, "
            "because a branch that overwrites its source is not a branch."
        )
    if destination.path.exists():
        raise SessionError(
            f"refusing to fork onto {destination.path}: it already exists. A fork never overwrites "
            "another session — choose a slug that is not in use, or remove that project first."
        )

    destination.state_dir.mkdir(parents=True, exist_ok=False)
    copied = _copy_tree(source.state_dir, destination.state_dir)
    if not copy_cache:
        cache_dir = destination.state_dir / "cache"
        if cache_dir.is_dir():
            for path in sorted(cache_dir.rglob("*"), reverse=True):
                try:
                    path.unlink() if path.is_file() else path.rmdir()
                except OSError:
                    continue

    notes: list[str] = []
    # The manifest and the run context live in the project folder, not in `.agent_state/`, so a fork
    # that copied only the state would be a session whose plan still points into the original folder —
    # and whose next run would write its artifacts back there.
    rewritten = _bring_the_plan_along(source, destination, notes)
    copied["files"] += rewritten["files"]
    if rewritten["manifest_path"]:
        notes.append(f"plan copied to {rewritten['manifest_path']}")

    (destination.state_dir / FORK_FILENAME).write_text(
        json.dumps({
            "fork_version": "1.0.0",
            "from_slug": source.slug,
            "from_path": str(source.path),
            "to_slug": destination.slug,
            "at": _iso_now(),
            "files": copied["files"],
            "bytes": copied["bytes"],
        }, indent=2, sort_keys=True), encoding="utf-8")

    return {
        "from": str(source.path),
        "from_slug": source.slug,
        "to": str(destination.path),
        "slug": destination.slug,
        "files": copied["files"],
        "bytes": copied["bytes"],
        "manifest_path": rewritten["manifest_path"],
        "notes": notes,
    }


def _bring_the_plan_along(source: Workspace, destination: Workspace,
                          notes: list[str]) -> dict[str, Any]:
    """Copy the run's plan and run context into the fork, and repoint the checkpoint at them.

    A checkpoint records `manifest_path` as an absolute path into the project that ran it. Left alone,
    a fork would execute the *original's* plan file and write the fork's artifacts beside the original
    — two sessions sharing one plan directory, which is exactly the confusion forking exists to avoid.
    """
    out: dict[str, Any] = {"files": 0, "manifest_path": ""}
    checkpoint = _read_json(source.state_dir / "run_state.json")
    if checkpoint is None:
        notes.append("run_state.json is unreadable; copied verbatim, without repointing the plan")
        return out

    raw_manifest = str(checkpoint.get("manifest_path") or "")
    if raw_manifest:
        plan = Path(raw_manifest)
        if plan.is_file():
            target = destination.path / plan.name
            target.write_bytes(plan.read_bytes())
            out["files"] += 1
            checkpoint["manifest_path"] = str(target)
            out["manifest_path"] = str(target)
        else:
            notes.append(f"the run's plan is missing at {plan}; the checkpoint was still repointed")

    context = source.path / "run-context.json"
    if context.is_file():
        (destination.path / context.name).write_bytes(context.read_bytes())
        out["files"] += 1

    if out["manifest_path"] or raw_manifest:
        try:
            destination.write_json("run_state.json", checkpoint)
        except StateError as exc:  # noqa: BLE001 - the copy is already complete and usable
            notes.append(f"could not repoint the checkpoint: {exc}")
    return out
