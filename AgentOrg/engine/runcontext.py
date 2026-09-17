#!/usr/bin/env python3
"""runcontext.py — the handoff from the orchestrator to the process that actually executes.

WHY THIS EXISTS
---------------
The runner is a **subprocess**, and the executor is generated per run inside that subprocess. That
boundary is deliberate — it is what stops a wedged run taking the app down — but it had a serious
consequence nobody had noticed: the subprocess rebuilt everything from scratch.

So the roster the Owner hired, the skills the Owner authored, and the bindings the orchestrator had
already decided were all **discarded at the boundary**. The executing process knew only the built-in
company, the pinned library, and no pins at all. Three promises were therefore false in execution:

- "`run` executes with the roster you hired" — it did not.
- "an authored skill reaches a run" — it did not.
- "a node runs as the agent the orchestrator chose" — it re-chose from the defaults.

This module is that handoff, made explicit and checkable. The orchestrator writes one JSON document
beside the manifest; the generated plugin reads it and builds its context from it. If the file is
missing the plugin falls back to the built-in company, so an older workspace still runs — but the
fallback is a *degradation*, logged rather than silent.

DESIGN
------
- **One file, written atomically.** The plugin may be started the instant the manifest is written, so
  a partial read would be a crash at run start. Temp file plus `os.replace`, like every other write.
- **Explicit, not inferred.** The document names the roster, the skill roots and the per-node
  bindings. Re-deriving any of them in the subprocess is how the three bugs happened; the whole point
  is that the executing process is *told* rather than left to guess.
- **Bindings are data.** A binding's policy (`pinned`, `swarm`, …) travels as a string, so the plugin
  does not need the enum to know it should run four agents.
- **Schema-versioned.** A plugin and a context file written by different builds must not be paired
  silently; the version is checked and a mismatch is refused with a clear message.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["RunContext", "RunContextError", "CONTEXT_FILENAME", "CONTEXT_VERSION"]

#: Written beside the manifest, inside the project workspace.
CONTEXT_FILENAME = "run-context.json"
#: Bumped when the document's shape changes incompatibly.
CONTEXT_VERSION = 1


class RunContextError(RuntimeError):
    """A run context that cannot be honoured, named so the reason is actionable."""


@dataclass
class RunContext:
    """Everything the executing process needs, and nothing it must guess.

    Parameters
    ----------
    org:
        The effective roster, as :meth:`engine.org.Org.to_dict` produced it. The plugin rebuilds an
        ``Org`` from this, so a hired agent is present in the process that runs the node.
    bindings:
        Node id -> the binding the orchestrator decided, as
        :meth:`engine.org.binding.NodeBinding.as_dict` produced it. A node absent from this map is
        bound by the plugin's own binder, which is the old behaviour and still correct for a graph
        nobody pre-bound.
    skill_roots:
        The user skill roots to layer over the library, highest priority first. Empty means
        library-only, which is what an unconfigured project has.
    """

    org: dict[str, Any] = field(default_factory=dict)
    bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    skill_roots: list[str] = field(default_factory=list)
    #: Owner-injected instructions and non-negotiable constraints, carried into the run.
    instructions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    #: True when a goal is armed for this run. The executing process needs it because `update_goal` is
    #: only advertised when a goal is in play — and a tool that is not advertised is not in the prefix.
    goal_active: bool = False
    #: The active objective, quoted to the agent so it knows what "complete" means.
    goal_objective: str = ""
    version: int = CONTEXT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "org": self.org,
            "bindings": self.bindings,
            "skill_roots": list(self.skill_roots),
            "instructions": list(self.instructions),
            "constraints": list(self.constraints),
            "goal_active": bool(self.goal_active),
            "goal_objective": self.goal_objective,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunContext":
        version = int(data.get("version") or 0)
        if version != CONTEXT_VERSION:
            raise RunContextError(
                f"run context version {version} is not {CONTEXT_VERSION}; the executor plugin and "
                "the context file were written by different builds. Re-plan the run."
            )
        return cls(
            org=dict(data.get("org") or {}),
            bindings={str(k): dict(v) for k, v in (data.get("bindings") or {}).items()},
            skill_roots=[str(p) for p in (data.get("skill_roots") or [])],
            instructions=[str(i) for i in (data.get("instructions") or [])],
            constraints=[str(c) for c in (data.get("constraints") or [])],
            goal_active=bool(data.get("goal_active")),
            goal_objective=str(data.get("goal_objective") or ""),
            version=version,
        )


def path_for(workspace: Path | str) -> Path:
    """Where the context document lives for a workspace."""
    return Path(workspace) / CONTEXT_FILENAME


def write(workspace: Path | str, context: RunContext) -> Path:
    """Write the context atomically, so a plugin starting immediately never reads a partial file."""
    target = path_for(workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(context.as_dict(), indent=2, sort_keys=True, default=str),
                   encoding="utf-8")
    os.replace(tmp, target)
    return target


def read(workspace: Path | str) -> RunContext | None:
    """Read the context, or None when this workspace predates it.

    None rather than raising, because the caller's fallback — the built-in company — is a working
    configuration, and refusing to run an old workspace would be worse than running it as it was.
    """
    target = path_for(workspace)
    if not target.is_file():
        return None
    try:
        return RunContext.from_dict(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunContextError(f"run context at {target} is unreadable: {exc}") from exc
