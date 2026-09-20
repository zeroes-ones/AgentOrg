#!/usr/bin/env python3
"""goal.py — a durable, ownable objective that keeps being worked on until it is done.

WHY THIS EXISTS
---------------
Everything else in this engine is *bounded*. A node is one `execute_node` call, a run is one graph,
and a review loop is capped at an iteration count. That is correct for "run this plan" and wrong for
the thing people actually want from a coding agent: *state a goal, point it at a repo, close the
laptop, come back later.* Between those two points the old answer was "nothing happens", because a
graph that finishes is a run that has stopped.

A Goal is what continues. It is deliberately **not** a graph, a plan, or a quality gate: it is an
objective that stays armed until it is completed, blocked, paused, cleared, or budget-exhausted.

DESIGN
------
- **Durability is the file; activation is explicit.** The state is written atomically to
  `.agent_state/goal.json` and schema-versioned, exactly like the run checkpoint. But it is loaded
  **disarmed**: a goal restored from disk requires an explicit `resume` before it continues. An
  unattended loop that re-arms itself when a process restarts is an unbounded spend nobody
  authorised, and that is the one failure mode worth friction to prevent.
- **Completion is a tool call, not a host judgement.** The agent reports `update_goal(complete)` or
  `update_goal(blocked)`; no node percentage, todo count or evaluator decides. The host's job is to
  keep the loop honest and cheap to stop, not to second-guess "done".
- **No ceiling by default, but never blind.** `token_budget = 0` means continue indefinitely, which is
  the intended default for a tool you leave running. The compensation is that rounds, tokens,
  requests and cost are *always* accumulated and reported, so "no ceiling" never means "no idea".
- **A budget is sliced, not reset.** Reaching a positive budget pauses with reason `budget_spend`, and
  `resume` grants a fresh slice while the cumulative statistics stay intact — so spend is bounded per
  slice and fully visible across slices.
- **Gates still hold.** A Goal continues past a *model final* and past *ordinary node completion*; it
  parks at a human gate or a policy `confirm`. Continuing past work finishing is the point; deciding a
  question you asked to decide is not.

Usage:
    goal = Goal.load(workspace) or Goal.new(objective="Add cursor pagination", workspace=workspace)
    goal.arm(by="cli")
    goal.record_round(tokens=1200, requests=1, cost_usd=0.02)
    if goal.budget_reached():
        goal.pause(reason="budget_spend")
    goal.save()
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "GoalError", "GoalState", "GoalSpend", "Goal", "GoalDecision", "GoalPolicy", "Posture",
    "GOAL_FILENAME", "GOAL_VERSION", "DECISION_FILENAME",
]

#: Written beside the run checkpoint, inside `.agent_state/`.
GOAL_FILENAME = "goal.json"
#: The agent's own verdict, written by the `update_goal` tool and read by the orchestrator.
DECISION_FILENAME = "goal_decision.json"
#: Bumped when the document's shape changes incompatibly.
GOAL_VERSION = "1.0.0"


class GoalError(RuntimeError):
    """A goal that cannot be read, written or transitioned, named so the reason is actionable."""


class GoalState(str, Enum):
    """Where a goal is. `armed` is the only state in which the loop continues."""

    CLEARED = "cleared"          # no objective; the fresh-workspace state
    ARMED = "armed"              # live: the loop continues after a final
    PAUSED = "paused"            # stopped, resumable: manual · gate · budget_spend · restored
    COMPLETED = "completed"      # the agent reported the whole objective done
    BLOCKED = "blocked"          # the agent reported a concrete, persistent blocker

    @property
    def is_live(self) -> bool:
        """True when the goal should keep driving execution."""
        return self is GoalState.ARMED

    @property
    def is_open(self) -> bool:
        """True while the objective has not been finished or abandoned.

        A paused goal is still *open* — it is waiting, not done — which is why `resume` is meaningful
        for it and for a completed/blocked goal alike (a new slice, keeping the statistics).
        """
        return self is not GoalState.CLEARED


def _iso_now() -> str:
    """UTC timestamp with millisecond precision, matching the protocol's format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


@dataclass
class GoalSpend:
    """Cumulative spend, kept across pauses and resumes.

    Cumulative rather than per-slice, because the question a person actually asks is "what has this
    cost me", and a figure that reset on every resume would answer a different one.

    `cost_unknown` carries the same rule the run's :class:`~engine.gateway.CostLedger` does: `cost_usd`
    is the total spend *that was reported*, and once any round's cost nobody reported it is a floor
    rather than a total. Rendering the two identically would make a run that was never measured look
    like a cheap one, which is the cost illusion the ledger exists to prevent.
    """

    rounds: int = 0
    tokens: int = 0
    requests: int = 0
    cost_usd: float = 0.0
    #: True once any round's cost was unreported, so `cost_usd` is a lower bound.
    cost_unknown: bool = False
    #: Calls whose cost the provider never reported, kept so the ledger's own count can be diffed.
    unknown_cost_calls: int = 0

    def add(self, *, tokens: int = 0, requests: int = 0, cost_usd: float | None = 0.0,
            rounds: int = 0, unknown_cost_calls: int = 0) -> None:
        """Fold one contribution in. `cost_usd=None` means "unreported", not "zero"."""
        self.rounds += int(rounds)
        self.tokens += int(tokens)
        self.requests += int(requests)
        if cost_usd is None:
            self.cost_unknown = True
        else:
            self.cost_usd = round(self.cost_usd + float(cost_usd), 6)
        self.unknown_cost_calls += int(unknown_cost_calls)
        if unknown_cost_calls:
            self.cost_unknown = True

    @property
    def cost_complete(self) -> bool:
        """True when every round's cost was reported, so `cost_usd` is a total, not a floor."""
        return not self.cost_unknown

    def as_dict(self) -> dict[str, Any]:
        return {"rounds": self.rounds, "tokens": self.tokens,
                "requests": self.requests, "cost_usd": self.cost_usd,
                "cost_complete": self.cost_complete,
                "unknown_cost_calls": self.unknown_cost_calls}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GoalSpend":
        data = data or {}
        return cls(rounds=int(data.get("rounds") or 0), tokens=int(data.get("tokens") or 0),
                   requests=int(data.get("requests") or 0),
                   cost_usd=float(data.get("cost_usd") or 0.0),
                   # Absent means complete: a goal written before this field existed was accounted
                   # under the old rule, and re-reading a gap into it would invent one.
                   cost_unknown=not bool(data.get("cost_complete", True)),
                   unknown_cost_calls=int(data.get("unknown_cost_calls") or 0))


class Posture(str, Enum):
    """How far a goal's autonomy reaches — one word, resolved once.

    The engine grew three overlapping switches for this (`goal.auto_pass_auto_gates` in the config,
    `GoalPolicy.human_gate` and `GoalPolicy.auto_approve` on the goal), and none of them answered the
    question a person actually asks: *do I have to be here for this to finish?* A posture answers it.

    - ``SUPERVISED`` — a human is involved at every gate, exactly as before this existed. The run
      parks and waits, and nothing decides on the Owner's behalf.
    - ``UNATTENDED`` — the goal may answer the gates it is able to answer, including the **terminal**
      gate, which is what makes a goal able to finish with nobody watching. The terminal release is
      not a rubber stamp: it requires the gate's evidence to be present, refuses when a safety control
      fired, and is recorded in the ledger as `by: goal`.

    The default is ``UNATTENDED`` because that is the polarity the product already documents — "a human
    is involved only if you chose one" — and because the gate vocabulary keeps its meaning either way:
    the manifest still declares `kind: human`, so a `supervised` goal parks on exactly the gate it
    always did.
    """

    SUPERVISED = "supervised"
    UNATTENDED = "unattended"

    @property
    def involves_a_human(self) -> bool:
        """Whether every gate waits for the Owner."""
        return self is Posture.SUPERVISED


@dataclass
class GoalPolicy:
    """What a goal is authorised to decide on its own.

    The person asked for a tool they can leave running, so the polarity is **autonomous unless a human
    gate was chosen**. The switches:

    - ``posture`` — the one word that settles whether a human is involved. See :class:`Posture`. This
      is the field to set; ``human_gate`` is kept as a legacy alias that maps onto it.
    - ``auto_approve`` — may a gate the *org* can decide be passed without asking. Under
      ``SUPERVISED`` this is forced false.
    - ``auto_hire`` — may a staffing gap be closed by spawning a helper on the default model, rather
      than parking the run. The work is what the objective asked for; the gap is an administrative
      accident of the roster, not a decision.
    - ``persist_hires`` — does that helper become a durable roster entry, or stay ephemeral. Off by
      default: an ephemeral subagent leaves nothing to clean up.
    - ``human_gate`` — **legacy**, superseded by ``posture``. `True` means ``SUPERVISED``. Kept so a
      `goal.json` written by an earlier build keeps behaving, and so the existing CLI flags and the
      console's toggle do not break.

    Under ``SUPERVISED`` the *terminal* gate is still parked for the same reason it always was (it is
    the release/close/spend authority), and it is additionally parked by the posture itself.
    """

    auto_approve: bool = True
    auto_hire: bool = True
    persist_hires: bool = False
    human_gate: bool = False
    posture: Posture = Posture.UNATTENDED

    def __post_init__(self) -> None:
        # Tolerate a string posture — the CLI and the protocol carry it as text — so the dataclass has
        # exactly one representation internally and `as_dict` is always safe.
        if not isinstance(self.posture, Posture):
            try:
                self.posture = Posture(str(self.posture).strip().lower())
            except ValueError as exc:
                raise GoalError(
                    f"unknown posture {self.posture!r}; expected one of "
                    f"{', '.join(p.value for p in Posture)}"
                ) from exc
        # One source of truth. A caller that sets the legacy flag (or an older document that only has
        # it) gets the posture it means, so the two can never disagree.
        if self.human_gate:
            self.posture = Posture.SUPERVISED
        elif self.posture is Posture.SUPERVISED:
            self.human_gate = True

    @property
    def unattended(self) -> bool:
        """Whether the goal may answer its gates itself."""
        return self.posture is Posture.UNATTENDED

    def effective(self) -> "GoalPolicy":
        """The policy with the posture applied, so callers never repeat the override."""
        if self.unattended:
            return self
        return GoalPolicy(auto_approve=False, auto_hire=False,
                          persist_hires=self.persist_hires, human_gate=True,
                          posture=Posture.SUPERVISED)

    def as_dict(self) -> dict[str, Any]:
        return {"auto_approve": self.auto_approve, "auto_hire": self.auto_hire,
                "persist_hires": self.persist_hires, "human_gate": self.human_gate,
                "posture": self.posture.value}

    @classmethod
    def from_dict(cls, data: Any) -> "GoalPolicy":
        """Read a policy, tolerating a document written before any of this existed.

        An older goal.json has no `policy` key at all, or one with no `posture`. In both cases the
        defaults apply — which are the autonomous ones — because that is the polarity the product
        documents and the person who set the goal chose no human gate. A document that *does* carry
        `human_gate: true` resolves to `SUPERVISED`, so an old "I want to be involved" goal stays
        exactly as involved as its owner asked for.
        """
        if not isinstance(data, dict):
            return cls()
        posture = data.get("posture")
        if posture:
            try:
                resolved = Posture(str(posture).strip().lower())
            except ValueError as exc:
                raise GoalError(
                    f"goal policy has an unknown posture: {posture!r}; expected one of "
                    f"{', '.join(p.value for p in Posture)}"
                ) from exc
        else:
            resolved = Posture.SUPERVISED if data.get("human_gate") else Posture.UNATTENDED
        return cls(
            auto_approve=bool(data.get("auto_approve", True)),
            auto_hire=bool(data.get("auto_hire", True)),
            persist_hires=bool(data.get("persist_hires", False)),
            human_gate=bool(data.get("human_gate", False)),
            posture=resolved,
        )


@dataclass
class Goal:
    """One durable objective, and everything needed to resume it honestly.

    Parameters
    ----------
    objective:
        What is to be achieved, in words. Free text, because it is the agent's instruction.
    state:
        See :class:`GoalState`. A goal read from disk is **never** armed; see :meth:`load`.
    slice_spend:
        Tokens/requests spent in the *current* slice, which is what a positive `token_budget` bounds.
    spend:
        Cumulative totals across every slice, which is what a person is shown.
    """

    objective: str = ""
    state: GoalState = GoalState.CLEARED
    #: 0 means no ceiling. A positive value bounds one slice; `resume` grants a fresh one.
    token_budget: int = 0
    armed_at: str = ""
    armed_by: str = ""
    pause_reason: str = ""
    summary: str = ""            # the agent's own final report on completion
    blocked_reason: str = ""
    slice_spend: GoalSpend = field(default_factory=GoalSpend)
    spend: GoalSpend = field(default_factory=GoalSpend)
    history: list[dict[str, Any]] = field(default_factory=list)
    #: What this goal may decide without the Owner. See :class:`GoalPolicy`.
    policy: GoalPolicy = field(default_factory=GoalPolicy)
    version: str = GOAL_VERSION

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def new(cls, objective: str, *, token_budget: int | None = None,
            policy: GoalPolicy | None = None) -> "Goal":
        """A fresh goal, **not yet armed**.

        Creating and arming are separate so that `goal set` can record an objective without starting
        an unattended loop — arming is the deliberate act that begins spending.
        """
        text = (objective or "").strip()
        if not text:
            raise GoalError("a goal needs an objective; an empty one would arm nothing")
        return cls(objective=text, token_budget=int(token_budget or 0),
                   policy=policy or GoalPolicy())

    # ── transitions ─────────────────────────────────────────────────────────

    def _note(self, kind: str, detail: str = "") -> None:
        self.history.append({"at": _iso_now(), "kind": kind, "detail": detail[:500]})

    def arm(self, *, by: str = "cli") -> None:
        """Start (or restart) the loop, granting a fresh slice.

        This is the only transition that makes the engine continue, and it is reachable only from an
        explicit user action — `goal set`/`goal resume`, the CLI, or a console button.
        """
        if not self.objective:
            raise GoalError("cannot arm a goal with no objective")
        self.state = GoalState.ARMED
        self.armed_at = _iso_now()
        self.armed_by = by
        self.pause_reason = ""
        self.slice_spend = GoalSpend()
        self._note("armed", by)

    def pause(self, *, reason: str = "manual") -> None:
        """Stop the loop, keeping the objective so it can be resumed."""
        if self.state is GoalState.CLEARED:
            raise GoalError("cannot pause a goal that was never set")
        self.state = GoalState.PAUSED
        self.pause_reason = str(reason or "manual")
        self._note("paused", self.pause_reason)

    def complete(self, summary: str = "") -> None:
        """The agent's own verdict that the whole objective is done."""
        self.state = GoalState.COMPLETED
        self.summary = (summary or "").strip()
        self.pause_reason = ""
        self._note("complete", self.summary)

    def block(self, reason: str = "") -> None:
        """The agent's own verdict that it cannot proceed without the user."""
        self.state = GoalState.BLOCKED
        self.blocked_reason = (reason or "").strip()
        self.pause_reason = ""
        self._note("blocked", self.blocked_reason)

    def clear(self) -> None:
        """Forget the objective, keeping the history of what it cost."""
        self._note("cleared", self.objective[:200])
        self.state = GoalState.CLEARED
        self.objective = ""
        self.pause_reason = ""

    def disarm_on_load(self) -> None:
        """Come back from disk **not running**.

        The single most important safety property of this module. A goal restored from a file is
        paused with reason `restored`; continuing requires an explicit resume. Without this, restarting
        the engine would silently re-arm whatever objective was last set and spend unattended.
        """
        if self.state is GoalState.ARMED:
            self.state = GoalState.PAUSED
            self.pause_reason = "restored"
            self._note("paused", "restored: a goal must be explicitly resumed after a reload")

    def adopt(self) -> None:
        """Take an on-disk `armed` goal as live, for an **explicit** execution request.

        `disarm_on_load` exists to stop a *silent* resume — a process starting up must not begin
        spending on its own. It also made the CLI unable to drive a goal at all: every command is a new
        process, so `goal set` armed the goal and the very next `run` disarmed it in memory and never
        continued it. Only the long-lived `serve` process could drive a loop, because it held the
        orchestrator across commands.

        This is the narrow counterpart: a caller that is *explicitly executing* (a `run` command, or the
        console's Start) says so, and the goal it was told about keeps its authority. The safety
        property is unchanged — nothing resumes without an explicit act, and nothing resumes merely
        because a process restarted.
        """
        if self.state is GoalState.PAUSED and self.pause_reason == "restored":
            self.state = GoalState.ARMED
            self.pause_reason = ""
            self._note("armed", "adopted for an explicit run")

    # ── budget ──────────────────────────────────────────────────────────────

    def budget_reached(self) -> bool:
        """Whether the current slice has spent its configured slice budget.

        `token_budget == 0` is never reached by construction — the ceiling is opt-in.
        """
        return self.token_budget > 0 and self.slice_spend.tokens >= self.token_budget

    def record_round(self, *, tokens: int = 0, requests: int = 0, cost_usd: float | None = 0.0,
                     unknown_cost_calls: int = 0) -> None:
        """Fold one continuation round into both the slice and the cumulative totals.

        `cost_usd=None` records a round whose cost nobody reported, and marks the cumulative figure
        a floor — distinct from a round that genuinely spent nothing.
        """
        self.slice_spend.add(tokens=tokens, requests=requests, cost_usd=cost_usd,
                             unknown_cost_calls=unknown_cost_calls, rounds=1)
        self.spend.add(tokens=tokens, requests=requests, cost_usd=cost_usd,
                       unknown_cost_calls=unknown_cost_calls, rounds=1)

    # ── serialisation ───────────────────────────────────────────────────────

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal_version": self.version,
            "objective": self.objective,
            "state": self.state.value,
            "token_budget": self.token_budget,
            "armed_at": self.armed_at,
            "armed_by": self.armed_by,
            "pause_reason": self.pause_reason,
            "summary": self.summary,
            "blocked_reason": self.blocked_reason,
            "slice_spend": self.slice_spend.as_dict(),
            "spend": self.spend.as_dict(),
            "history": list(self.history),
            "policy": self.policy.as_dict(),
        }

    def public(self) -> dict[str, Any]:
        """The shape the protocol and the console read.

        Kept separate from :meth:`as_dict` so the on-disk document can grow without the UI contract
        moving with it, and so the console gets the derived booleans it renders (`live`, `budget`).
        """
        return {
            "objective": self.objective,
            "state": self.state.value,
            "live": self.state.is_live,
            "open": self.state.is_open,
            "token_budget": self.token_budget,
            "budget_enabled": self.token_budget > 0,
            "pause_reason": self.pause_reason,
            "summary": self.summary,
            "blocked_reason": self.blocked_reason,
            "armed_at": self.armed_at,
            "armed_by": self.armed_by,
            "slice": self.slice_spend.as_dict(),
            "spend": self.spend.as_dict(),
            "history": list(self.history[-20:]),
            "policy": self.policy.as_dict(),
            # The two derived booleans the UI asks about, so no caller re-implements the override.
            "decides_gates": self.policy.effective().auto_approve,
            "staffs_gaps": self.policy.effective().auto_hire,
            # Surfaced at the top level as well as inside `policy`: this is the one word the console
            # renders, and making the UI reach into a nested dict for it invites disagreement.
            "posture": self.policy.posture.value,
            "unattended": self.policy.unattended,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Goal":
        version = str(data.get("goal_version") or GOAL_VERSION)
        if version.split(".")[0] != GOAL_VERSION.split(".")[0]:
            raise GoalError(
                f"goal file version {version} is not compatible with {GOAL_VERSION}; the goal was "
                "written by a different build. Clear it, or re-set the objective."
            )
        try:
            state = GoalState(str(data.get("state") or GoalState.CLEARED.value))
        except ValueError as exc:
            raise GoalError(f"goal file has an unknown state: {data.get('state')!r}") from exc
        return cls(
            objective=str(data.get("objective") or ""),
            state=state,
            token_budget=int(data.get("token_budget") or 0),
            armed_at=str(data.get("armed_at") or ""),
            armed_by=str(data.get("armed_by") or ""),
            pause_reason=str(data.get("pause_reason") or ""),
            summary=str(data.get("summary") or ""),
            blocked_reason=str(data.get("blocked_reason") or ""),
            slice_spend=GoalSpend.from_dict(data.get("slice_spend")),
            spend=GoalSpend.from_dict(data.get("spend")),
            history=list(data.get("history") or []),
            policy=GoalPolicy.from_dict(data.get("policy")),
            version=version,
        )

    # ── persistence ─────────────────────────────────────────────────────────

    @staticmethod
    def path_for(workspace: Any) -> Path:
        """Where the goal lives for a workspace: `.agent_state/goal.json`."""
        return Path(getattr(workspace, "state_dir", workspace)) / GOAL_FILENAME

    def save(self, workspace: Any) -> Path:
        """Persist atomically, temp-then-`os.replace`, so a reader never sees a torn goal.

        A torn goal is worse than a stale one: it could come back armed, or with a spend figure that
        understates what was really spent.
        """
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
            raise GoalError(f"failed to write goal {target}: {exc}") from exc
        return target

    @classmethod
    def load(cls, workspace: Any) -> "Goal | None":
        """Read the goal, or None when the workspace has none.

        Returns the goal **disarmed** (see :meth:`disarm_on_load`): reading is not resuming. None
        rather than an empty goal keeps the decision with the caller — "no goal" and "a goal that is
        not running" are different things, and conflating them is how a fresh run inherits an old
        objective.
        """
        target = cls.path_for(workspace)
        if not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GoalError(
                f"goal {target} is corrupt: {exc.msg} (line {exc.lineno}). Refusing to resume from "
                "unreadable state."
            ) from exc
        if not isinstance(data, dict):
            raise GoalError(f"goal {target} must be a JSON object")
        goal = cls.from_dict(data)
        goal.disarm_on_load()
        return goal

    @classmethod
    def load_or_new(cls, objective: str, workspace: Any, *,
                    token_budget: int | None = None) -> "Goal":
        """The goal to drive a run with: the existing one when it is still open, else a new one.

        A completed or cleared objective is not quietly replaced — a *new* goal is created only when
        the old one is finished, so an in-flight objective is never overwritten by a re-run.
        """
        existing = cls.load(workspace)
        if existing is not None and existing.state.is_open:
            return existing
        return cls.new(objective, token_budget=token_budget)


@dataclass
class GoalDecision:
    """The agent's own verdict on the objective, carried across the process boundary.

    The executor runs in a subprocess (`host.py`), so the `update_goal` tool call — which is the *only*
    authority on completion — cannot reach the orchestrator in memory. It writes this small record to
    `.agent_state/goal_decision.json`; the orchestrator reads it after the subprocess returns and acts.

    Written atomically and then *consumed* (removed) on read, so a decision authorises exactly one
    transition: a stale "complete" must not keep the loop from re-arming a later round.
    """

    verdict: str = ""            # complete | blocked
    summary: str = ""
    at: str = ""

    @staticmethod
    def path_for(workspace: Any) -> Path:
        return Path(getattr(workspace, "state_dir", workspace)) / DECISION_FILENAME

    def save(self, workspace: Any) -> Path:
        """Write the decision, temp-then-`os.replace`."""
        target = self.path_for(workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        payload = {"verdict": self.verdict, "summary": self.summary,
                   "at": self.at or _iso_now()}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        return target

    @classmethod
    def consume(cls, workspace: Any) -> "GoalDecision | None":
        """Read and remove the decision, or None when the agent made none this round.

        Consuming rather than reading matters: a decision is a one-shot authorisation to stop, and a
        file left behind would silently stop the *next* round too.
        """
        target = cls.path_for(workspace)
        if not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        finally:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        if not isinstance(data, dict):
            return None
        verdict = str(data.get("verdict") or "")
        if verdict not in ("complete", "blocked"):
            return None
        return cls(verdict=verdict, summary=str(data.get("summary") or ""),
                   at=str(data.get("at") or ""))
