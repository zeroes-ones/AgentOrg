#!/usr/bin/env python3
"""mission.py — a durable *why* above the goals that serve it.

WHY THIS EXISTS
---------------
A **Goal** is one durable objective: "add cursor pagination", "capture the market". It is the right
unit for "leave it running until this is done" and it is deliberately flat — one objective, one loop.

Real work is not flat. A person does not have one objective; they have a **mission** — a standing
purpose ("make this product ready for its first paying customers") that decomposes into a sequence of
objectives, each of which is worked by goals and runs. Without that level, the product can say *what
it is doing now* (a goal) but not *what it is for*, so a long-running org looks like an endless
sequence of unrelated objectives with no thread through them.

So this module adds the missing top of the hierarchy:

```
Mission  →  Objective  →  Goal  →  Run  →  Node
(why)       (a step)      (a loop)  (one graph)  (one agent, one skill)
```

A Mission owns an ordered list of **objectives** and activates them **one at a time**. It does not
invent work: it does not plan, route, or spend. It answers three questions and nothing else:

1. *What is the standing purpose?* — the mission statement.
2. *Which step are we on, and what is next?* — the objective list and the active cursor.
3. *Is the mission done, blocked, or waiting?* — a state derived from its objectives.

DESIGN
------
- **The Mission does not spend; a Goal spends.** Arming is still exclusively a Goal transition. A
  mission that could arm a goal on its own would be an unattended spend with a friendly name, so
  ``advance`` returns the objective text and leaves the arming to the caller. This is the same
  separation `goal.py` draws between creating and arming, one level up.
- **Durable and disarmed on load, exactly like a Goal.** `mission.json` is written atomically
  (temp + ``os.replace`` + fsync) and schema-versioned, and a mission read from disk comes back
  *paused*. A mission must never resume itself because a process restarted.
- **Objectives are a plan, not a promise.** An objective can be added, edited, reordered, skipped or
  blocked; its status is one of a small closed set. The mission's own state is *derived* from them,
  so it cannot disagree with the list a person is looking at.
- **One active objective at a time.** Concurrency lives *inside* a run (swarm, fan-out, subagents);
  a mission advancing two objectives at once would be two conflicting spends and no single thread.

Usage:
    mission = Mission.load(workspace) or Mission.new("ship the MVP", workspace=workspace)
    mission.add_objective("get the auth flow green")
    mission.activate(0)
    mission.objective_now()        # the active objective, or None
    mission.mark_objective("done", summary="auth green")
    mission.next_objective()       # the next pending one, without activating it
    mission.state                  # derived: paused | active | completed | blocked
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "MissionError", "MissionState", "ObjectiveState", "Objective", "Mission",
    "MISSION_FILENAME", "MISSION_VERSION",
]

#: Written beside `goal.json`, inside `.agent_state/`.
MISSION_FILENAME = "mission.json"
#: Bumped when the document's shape changes incompatibly.
MISSION_VERSION = "1.0.0"


class MissionError(RuntimeError):
    """A mission that cannot be read, written or transitioned, named so the reason is actionable."""


class ObjectiveState(str, Enum):
    """Where one objective is. Ordered as the lifecycle: pending → active → done.

    `blocked` and `skipped` are both terminal-but-not-done, kept distinct because the difference is
    what a person needs: `blocked` means *this is in the way*, `skipped` means *we chose to move on*.
    """

    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    BLOCKED = "blocked"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self in (ObjectiveState.DONE, ObjectiveState.SKIPPED)

    @property
    def finished(self) -> bool:
        """Whether this objective will not be worked again — done, skipped, or blocked-and-parked."""
        return self in (ObjectiveState.DONE, ObjectiveState.SKIPPED, ObjectiveState.BLOCKED)


class MissionState(str, Enum):
    """Where a mission is. *Derived* from its objectives, never set directly.

    Making this derived is what guarantees the mission's headline cannot contradict the list below it
    — the class of bug where a dashboard says "active" while every item in it is done.
    """

    PAUSED = "paused"          # not being worked; resumable (the state after a load)
    ACTIVE = "active"          # an objective is active and the mission is armed
    BLOCKED = "blocked"        # the active objective is blocked; the mission needs a person
    COMPLETED = "completed"    # every objective is terminal and at least one was done
    EMPTY = "empty"            # a mission with no objectives yet

    @property
    def is_live(self) -> bool:
        return self is MissionState.ACTIVE

    @property
    def terminal(self) -> bool:
        """Whether the mission will not be worked again — finished, or with nothing left but a
        parked blocker. `blocked` is terminal for a *mission*: it is waiting on a person, so a new
        statement may replace it the same way a completed one may.
        """
        return self in (MissionState.COMPLETED, MissionState.BLOCKED, MissionState.EMPTY)

    @property
    def is_open(self) -> bool:
        """Whether the mission still has work in it — the opposite of terminal."""
        return not self.terminal


@dataclass
class Objective:
    """One step toward the mission.

    A plain record: an objective is prose the agent will be handed, plus a status and whatever it
    produced. It carries no plan — planning an objective is the planner's job, one level down.
    """

    text: str
    state: ObjectiveState = ObjectiveState.PENDING
    #: The run or goal this objective was worked by, so a person can trace it to the artifacts.
    goal_id: str = ""
    run_id: str = ""
    summary: str = ""
    blocked_reason: str = ""
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "state": self.state.value,
            "goal_id": self.goal_id,
            "run_id": self.run_id,
            "summary": self.summary,
            "blocked_reason": self.blocked_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Objective":
        try:
            state = ObjectiveState(str(data.get("state") or ObjectiveState.PENDING.value))
        except ValueError as exc:
            raise MissionError(f"objective has an unknown state: {data.get('state')!r}") from exc
        return cls(
            text=str(data.get("text") or ""),
            state=state,
            goal_id=str(data.get("goal_id") or ""),
            run_id=str(data.get("run_id") or ""),
            summary=str(data.get("summary") or ""),
            blocked_reason=str(data.get("blocked_reason") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


def _iso_now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


@dataclass
class Mission:
    """A standing purpose and the objectives that serve it.

    Parameters
    ----------
    statement:
        The mission, in words. Free text, because it is for a person to read, not for the engine to
        parse.
    objectives:
        The ordered steps. Order is meaningful: it is the reading order and the default advance order.
    armed:
        Whether the mission is being worked. `False` after any load; set only by an explicit
        :meth:`arm`.
    """

    statement: str = ""
    objectives: list[Objective] = field(default_factory=list)
    armed: bool = False
    pause_reason: str = ""
    completed_summary: str = ""
    blocked_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    version: str = MISSION_VERSION

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def new(cls, statement: str) -> "Mission":
        """A fresh, **unarmed** mission.

        Creating and arming are separate for the same reason `Goal` separates them: stating a purpose
        must not begin an unattended sequence of objectives.
        """
        text = (statement or "").strip()
        if not text:
            raise MissionError("a mission needs a statement; an empty one would serve nothing")
        now = _iso_now()
        return cls(statement=text, created_at=now, updated_at=now)

    # ── objectives ──────────────────────────────────────────────────────────

    def _note(self, kind: str, detail: str = "") -> None:
        self.history.append({"at": _iso_now(), "kind": kind, "detail": detail[:500]})

    def add_objective(self, text: str, *, at: int | None = None) -> Objective:
        """Append (or insert) an objective. Refuses a blank one and an exact duplicate.

        A duplicate objective is almost always a double-submit, and two identical steps would both be
        worked and both spend — so it is refused with the index of the existing one.
        """
        clean = (text or "").strip()
        if not clean:
            raise MissionError("an objective needs text")
        existing = self.index_of(clean)
        if existing is not None:
            raise MissionError(
                f"this objective is already #{existing}: {clean!r}. Edit or remove that one instead "
                "of adding a second identical step."
            )
        now = _iso_now()
        objective = Objective(text=clean, created_at=now, updated_at=now)
        if at is None:
            self.objectives.append(objective)
        else:
            index = max(0, min(int(at), len(self.objectives)))
            self.objectives.insert(index, objective)
        self.updated_at = now
        self._note("objective.added", clean)
        return objective

    def index_of(self, text: str) -> int | None:
        """The index of an objective with this exact text, or None."""
        wanted = (text or "").strip().lower()
        for index, objective in enumerate(self.objectives):
            if objective.text.strip().lower() == wanted:
                return index
        return None

    def objective_at(self, index: int) -> Objective:
        try:
            return self.objectives[int(index)]
        except (IndexError, ValueError) as exc:
            raise MissionError(
                f"no objective #{index}; this mission has {len(self.objectives)} "
                f"(#{0}–#{max(0, len(self.objectives) - 1)})"
            ) from exc

    def remove_objective(self, index: int) -> Objective:
        objective = self.objective_at(index)
        del self.objectives[int(index)]
        self.updated_at = _iso_now()
        self._note("objective.removed", objective.text)
        return objective

    # ── transitions ─────────────────────────────────────────────────────────

    def arm(self, *, by: str = "cli") -> None:
        """Begin working the mission.

        Arming the mission does **not** arm a goal — it records that the mission is being worked and
        leaves the spend decision to `goal set`/`goal resume`. See the module docstring for why.
        """
        if not self.objectives:
            raise MissionError("cannot work a mission with no objectives; add one first")
        self.armed = True
        self.pause_reason = ""
        self.updated_at = _iso_now()
        self._note("armed", by)

    def pause(self, *, reason: str = "manual") -> None:
        self.armed = False
        self.pause_reason = str(reason or "manual")
        self.updated_at = _iso_now()
        self._note("paused", self.pause_reason)

    def clear(self) -> None:
        """Forget the mission, keeping the history of what it was."""
        statement = self.statement
        self.armed = False
        self.statement = ""
        self.objectives = []
        self.pause_reason = ""
        self.completed_summary = ""
        self.blocked_reason = ""
        self.updated_at = _iso_now()
        self._note("cleared", statement[:200])

    def disarm_on_load(self) -> None:
        """Come back from disk **not running**.

        The same safety property `Goal` has, for the same reason: a mission restored from a file must
        not silently resume an unattended sequence of objectives because a process restarted.
        """
        if self.armed:
            self.armed = False
            self.pause_reason = "restored"
            self._note("paused", "restored: a mission must be explicitly resumed after a reload")

    # ── activation and progress ─────────────────────────────────────────────

    def active_index(self) -> int | None:
        """The index of the active objective, or None."""
        for index, objective in enumerate(self.objectives):
            if objective.state is ObjectiveState.ACTIVE:
                return index
        return None

    def objective_now(self) -> Objective | None:
        index = self.active_index()
        return self.objectives[index] if index is not None else None

    def next_index(self, *, after: int | None = None) -> int | None:
        """The next *pending* objective at or after `after`, else the first pending one, else None.

        "Active, else next pending" is the advance rule: a mission being worked always has exactly one
        objective in hand, and finishing one moves the cursor to the next rather than leaving it
        dangling on a completed step.
        """
        start = after if after is not None else -1
        for index in range(start + 1, len(self.objectives)):
            if self.objectives[index].state is ObjectiveState.PENDING:
                return index
        for index in range(0, max(0, start + 1)):
            if self.objectives[index].state is ObjectiveState.PENDING:
                return index
        return None

    def activate(self, index: int | None = None) -> Objective | None:
        """Make one objective active, demoting any other (one active objective at a time).

        With no index, activates the next pending one. Returns the active objective, or None when
        there is nothing left to work.
        """
        if index is None:
            # Prefer the current active objective; else the next pending one.
            current = self.active_index()
            index = current if current is not None else self.next_index()
            if index is None:
                return None
        objective = self.objective_at(index)
        # Demote any other active objective back to pending: exactly one in hand.
        for other_index, other in enumerate(self.objectives):
            if other_index != int(index) and other.state is ObjectiveState.ACTIVE:
                other.state = ObjectiveState.PENDING
                other.updated_at = _iso_now()
        objective.state = ObjectiveState.ACTIVE
        objective.updated_at = _iso_now()
        self.updated_at = objective.updated_at
        self._note("objective.active", f"#{int(index)} {objective.text}")
        return objective

    def mark(self, index: int, state: ObjectiveState | str, *, summary: str = "",
             run_id: str = "", goal_id: str = "") -> Objective:
        """Set one objective's state, with its outcome. The single write path for progress.

        One method rather than five, because the *state* is the only thing that differs and a caller
        that could set ``state`` directly would eventually forget to stamp ``updated_at``.
        """
        objective = self.objective_at(index)
        try:
            resolved = state if isinstance(state, ObjectiveState) else ObjectiveState(str(state))
        except ValueError as exc:
            raise MissionError(
                f"unknown objective state {state!r}; choose one of "
                f"{', '.join(s.value for s in ObjectiveState)}"
            ) from exc
        objective.state = resolved
        objective.updated_at = _iso_now()
        if summary:
            objective.summary = summary.strip()
        if run_id:
            objective.run_id = run_id
        if goal_id:
            objective.goal_id = goal_id
        if resolved is ObjectiveState.BLOCKED:
            objective.blocked_reason = summary.strip()
        self.updated_at = objective.updated_at
        self._note("objective.marked", f"#{int(index)} {resolved.value}: {objective.text}")
        return objective

    def advance(self, *, summary: str = "") -> Objective | None:
        """Finish the active objective and activate the next pending one.

        Returns the *newly* active objective, or None when the mission has no more work. `summary` on
        the finished objective is how a person sees what the step produced.
        """
        index = self.active_index()
        if index is not None:
            self.mark(index, ObjectiveState.DONE, summary=summary)
        self.activate()
        return self.objective_now()

    # ── derived state ───────────────────────────────────────────────────────

    def state(self) -> MissionState:
        """Where the mission is, *computed* from its objectives and armed flag.

        Deriving rather than storing is what keeps the headline honest: it cannot say "active" while
        every objective is done, nor "completed" while one is still pending.

        Arming is the switch. An armed mission with work left is **active** whether or not an objective
        has been explicitly activated yet — "armed but nothing started" is still being worked, and
        reporting it as paused would make `mission arm` look like a no-op.
        """
        if not self.objectives:
            return MissionState.EMPTY
        active = self.objective_now()
        pending = [o for o in self.objectives if o.state is ObjectiveState.PENDING]
        if self.armed and (active is not None or pending):
            return MissionState.ACTIVE
        if active is not None or pending:
            return MissionState.PAUSED
        # Nothing active, nothing pending: either a parked blocker or a finished mission.
        blocked = [o for o in self.objectives if o.state is ObjectiveState.BLOCKED]
        if blocked:
            return MissionState.BLOCKED
        return MissionState.COMPLETED

    def counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in ObjectiveState}
        for objective in self.objectives:
            counts[objective.state.value] = counts.get(objective.state.value, 0) + 1
        counts["total"] = len(self.objectives)
        return counts

    def progress(self) -> dict[str, Any]:
        """How far along the mission is: done / total, and the next step.

        A derived figure rather than a stored one, so it is never stale. The denominator is the
        *non-skipped* total: a skipped objective must not make a finished mission look 80% done.
        """
        counted = [o for o in self.objectives if o.state is not ObjectiveState.SKIPPED]
        done = sum(1 for o in counted if o.state is ObjectiveState.DONE)
        total = len(counted)
        next_index = self.active_index()
        if next_index is None:
            next_index = self.next_index()
        return {
            "done": done,
            "total": total,
            "fraction": (done / total) if total else 0.0,
            "active_index": self.active_index(),
            "next_index": next_index,
            "next": self.objectives[next_index].text if next_index is not None else "",
        }

    # ── serialisation ───────────────────────────────────────────────────────

    def as_dict(self) -> dict[str, Any]:
        return {
            "mission_version": self.version,
            "statement": self.statement,
            "armed": self.armed,
            "pause_reason": self.pause_reason,
            "completed_summary": self.completed_summary,
            "blocked_reason": self.blocked_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "objectives": [o.as_dict() for o in self.objectives],
            "history": list(self.history),
        }

    def public(self) -> dict[str, Any]:
        """The shape the protocol and the console read.

        Separate from :meth:`as_dict` so the on-disk document can grow without the UI contract moving,
        and so the UI gets the derived values it renders (`state`, `live`, `progress`, `now`).
        """
        now = self.objective_now()
        state = self.state()
        return {
            "statement": self.statement,
            "state": state.value,
            "live": state.is_live,
            "armed": self.armed,
            "pause_reason": self.pause_reason,
            "completed_summary": self.completed_summary,
            "blocked_reason": self.blocked_reason,
            "counts": self.counts(),
            "progress": self.progress(),
            "now": now.as_dict() if now is not None else None,
            "objectives": [o.as_dict() for o in self.objectives],
            "history": list(self.history[-20:]),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Mission":
        version = str(data.get("mission_version") or MISSION_VERSION)
        if version.split(".")[0] != MISSION_VERSION.split(".")[0]:
            raise MissionError(
                f"mission file version {version} is not compatible with {MISSION_VERSION}; it was "
                "written by a different build. Clear it, or re-set the mission."
            )
        return cls(
            statement=str(data.get("statement") or ""),
            objectives=[Objective.from_dict(o) for o in (data.get("objectives") or [])
                        if isinstance(o, dict)],
            armed=bool(data.get("armed")),
            pause_reason=str(data.get("pause_reason") or ""),
            completed_summary=str(data.get("completed_summary") or ""),
            blocked_reason=str(data.get("blocked_reason") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            history=list(data.get("history") or []),
            version=version,
        )

    # ── persistence ─────────────────────────────────────────────────────────

    @staticmethod
    def path_for(workspace: Any) -> Path:
        """Where the mission lives for a workspace: `.agent_state/mission.json`."""
        return Path(getattr(workspace, "state_dir", workspace)) / MISSION_FILENAME

    def save(self, workspace: Any) -> Path:
        """Persist atomically, temp-then-`os.replace`, so a reader never sees a torn mission."""
        target = self.path_for(workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.as_dict(), fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise MissionError(f"failed to write mission {target}: {exc}") from exc
        return target

    @classmethod
    def load(cls, workspace: Any) -> "Mission | None":
        """Read the mission, or None when the workspace has none.

        Returns it **disarmed**: reading is not resuming. None rather than an empty mission keeps the
        distinction between "no mission" and "a mission that is not running".
        """
        target = cls.path_for(workspace)
        if not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MissionError(
                f"mission {target} is corrupt: {exc.msg} (line {exc.lineno}). Refusing to resume "
                "from unreadable state."
            ) from exc
        if not isinstance(data, dict):
            raise MissionError(f"mission {target} must be a JSON object")
        mission = cls.from_dict(data)
        mission.disarm_on_load()
        return mission
