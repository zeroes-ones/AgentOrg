#!/usr/bin/env python3
"""parallel.py — make a manifest's `parallel:` group actually overlap.

WHY THIS EXISTS
---------------
A `parallel:` block in a manifest is the graph's way of saying "these nodes do not depend on each
other". The library's runner honours the *join* half of that faithfully — a group's member edges are
held until every member has reported — but it visits one node at a time, so the members still run
nose-to-tail. The declaration was therefore a join and never a fan-out, which is exactly the gap
`AUTONOMY-MAP.md` recorded: "`parallel:` runs sequentially in the stdlib runner. Concurrency is
inside a node."

WHERE THE FIX BELONGS, AND WHY IT IS HERE
-----------------------------------------
The scheduler that walks the nodes and edges is the **library's** runner
(`Skills/scripts/workflow-runner.py`), which is shared with other tools and is not ours to change:
`engine/host.py` invokes it as a subprocess precisely so the engine cannot diverge from it. The
runner's main loop is `active.pop(0)` → `execute_node(nid, ...)` → checkpoint, and it is structurally
one node at a time — the run-state it threads through every call is one mutable dict, and every step
of the loop mutates it (`active`, `seen`, `done`, `budget.steps_used`, `state["handoff"]`). Making it
overlap means teaching *that* file to dispatch a group concurrently, merge N run-state deltas
deterministically, and keep `--state` resume correct. That is a real change to a shared program and it
is the correct long-term home; it is documented in the module docstring's UPSTREAM section below
rather than attempted here.

What the engine *can* own is the half that is legitimately its own: **one `execute_node` call is
already the unit the runner is willing to run concurrently** (the runner's own self-test drives
`execute_node` for a group, and `NodeExecutor`'s class docstring says fan-out runs several nodes
concurrently). So a group's members are overlapped *within the node that the runner hands us*, by
dispatching them to `NodeExecutor` on a bounded wave and joining deterministically before returning
one result. The graph the runner sees is unchanged — same nodes, same edges, same one-node-at-a-time
traversal and per-node checkpoint — so `--state` resume, the step budget and the join semantics all
behave exactly as before. The overlap is real; the control flow the runner owns is untouched.

That constraint is also what makes the correctness argument tractable. The runner's own scheduler is
excluded from the parallel region by construction, so the only shared state the concurrent members
can race on is the engine's: the per-agent session map, the artifact store, the effect journal, the
cost ledger, and the executor's node registries. Each of those is addressed in DESIGN below, and
`engine/executor.py` holds the locks.

UPSTREAM — WHAT THE LIBRARY RUNNER WOULD NEED, PRECISELY
-------------------------------------------------------
Not attempted, and stated so the gap is not overclaimed. In `Skills/scripts/workflow-runner.py`:

1. **`Runner.run`** (`while active or loop_active`, line 893) pops one node and calls
   `self.executor.execute_node(nid, self.state, ctx)` at line 721. A group would need to be popped
   as a *set* and dispatched with one `execute_node` per member on a bounded wave.
2. **`Runner._advance_from`** (line 848) holds member edges at the join by reading
   `state["nodes"][m]["status"]` for every member. Concurrent dispatch would have to write all N
   results into run-state *before* this runs, or the join would fire on partially-applied state.
3. **`record_usage` / `_mark_done` / `_apply_contract` / `_apply_guardrail`** each mutate
   `state["budget"]`, `state["nodes"]`, `state["log"]`, `state["artifacts"]` and `state["handoff"]`.
   They are per-node today; N members finishing at once means N mutations of one dict.
   `state["handoff"]` is the sharpest: it is a single slot holding the *last* crossing, so two
   concurrent members overwrite each other and the graph records one handoff where two occurred.
4. **`save_state`** is called per node and writes the whole state atomically. Concurrent members would
   need one checkpoint after the join, or the writes would interleave.
5. **The step budget** is checked at the top of the loop (`state["budget"]["steps_used"] >=
   self.max_steps`); a wave of N would need to reserve N steps or the last member could overshoot.

The engine-side implementation in this module is deliberately shaped so that an upstream fix would
*replace* it with the same interface rather than duplicate it: `plan_group` decides what may overlap,
`run_group` bounds and aggregates, and only the caller (`NodeExecutor._run_group`) knows it is
running inside one `execute_node` call rather than inside the runner's own loop.

DESIGN
------
- **Opt-in, because a wrong default is worse than a slow one.** `parallel_nodes` is off unless the
  config enables it (`executor.parallel_nodes`). A parallel run must produce the same final result as
  the sequential one, and this module only claims that for a *declared* group whose members are
  actually independent; anything it cannot prove it refuses to overlap and reports why.
- **Independence is checked, not assumed.** Members must declare disjoint outputs — the library's own
  validator enforces the same rule (`parallel %s: conflicting writer`) — and must not be each other's
  successors. A group whose members write the same artifact is not a fan-out; running it concurrently
  would be two writers racing on one file, which the sequential order was silently preventing.
- **Bounded by the ceiling the rest of the engine already honours.** The wave size comes from
  `derive_ceiling`-derived capacity, capped by the configured `fanout_max_parallel`, so a wide group
  cannot open more concurrent model calls than a fan-out can. Opening 128 at once melts a local
  provider; that reasoning does not change because the work arrived as nodes rather than items.
- **One member failing does not abort its siblings**, and the failure is reported against its own node
  rather than merged into a group verdict — a fan-out is for independent work, so discarding the
  others over one failure is strictly worse than reporting the one.
- **Aggregation is deterministic and order-independent.** Results are keyed by node id, never by
  completion order, so the same inputs give byte-identical output however the provider happened to
  schedule them. `join: all` is the only semantics implemented: `any`/`majority` degrade to `all`
  here exactly as the runner documents, because a group whose members gate a successor is a
  conjunction and pretending otherwise would release an edge on a quorum the graph never declared.

Usage:
    group = find_group(manifest, "reviewers")
    plan = plan_group(group, manifest, members=[...], state=state, ceiling=8)
    outcome = run_group(plan, lambda node_id: executor.execute_node(node_id, state, {}))
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .fanout import ConcurrentSlot, run_wave

__all__ = [
    "ParallelError", "GroupPlan", "GroupOutcome",
    "find_group", "plan_group", "run_group", "group_ceiling", "shared_gate",
]

#: The statuses that mean a member has finished, matching the runner's own `_STATUS_WORDS`. A member
#: that reached any of these has reported, so the join may consider it done — including `blocked`,
#: because a blocked member is a finished decision and holding the group forever on one is how a run
#: stalls invisibly.
TERMINAL_STATUSES = ("done", "blocked", "needs_review", "skipped")

#: The default bound on how many group members overlap. Matches `executor.fanout_max_parallel`'s
#: default, because a group member is the same kind of model call a fan-out item is.
DEFAULT_CEILING = 4


class ParallelError(RuntimeError):
    """A group that cannot legitimately overlap, named so the caller can act on the reason."""


@dataclass(frozen=True)
class GroupPlan:
    """A validated decision to overlap one group's members, with the reason it is safe.

    Carries `members` in *declared* order rather than any computed order: the wave dispatches in this
    order and results come back indexed against it, so the plan is the single place ordering is
    decided and an aggregation cannot accidentally depend on completion timing.
    """

    group_id: str
    members: list[str]
    join: str
    ceiling: int
    #: Why this group may overlap. Surfaced rather than assumed, because "the members are independent"
    #: is a claim about the manifest that a reader should be able to check.
    reason: str

    def __len__(self) -> int:
        return len(self.members)

    def as_dict(self) -> dict[str, Any]:
        return {"group_id": self.group_id, "members": list(self.members), "join": self.join,
                "ceiling": self.ceiling, "reason": self.reason}


@dataclass
class GroupOutcome:
    """What a group run produced: one result per member, in declared order.

    `peak_in_flight` is recorded deliberately. A bound that is declared but never observed to be
    reached is indistinguishable from a sequential loop wearing a concurrency label, and that is not a
    hypothetical here — it is the defect this module was written to fix, found in `fanout`'s own queue.
    """

    group_id: str
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    peak_in_flight: int = 0
    ceiling: int = 0
    #: Members that never ran. Kept distinct from `failures`: "did not run" and "ran and failed" need
    #: different follow-up, and collapsing them would report a member that never started as one that
    #: tried and lost.
    skipped: list[str] = field(default_factory=list)
    #: The exception behind each failure, kept so a caller can re-raise the original rather than
    #: report a string. Excluded from `as_dict` because it is a live object, not outcome data.
    exceptions: dict[str, BaseException] = field(default_factory=dict, repr=False)

    @property
    def complete(self) -> bool:
        """True when every member ran and none failed."""
        return not self.failures and not self.skipped

    @property
    def failed(self) -> bool:
        return bool(self.failures)

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "members": list(self.results),
            "failed": dict(self.failures),
            "skipped": list(self.skipped),
            "complete": self.complete,
            "peak_in_flight": self.peak_in_flight,
            "ceiling": self.ceiling,
        }


def shared_gate(manifest: dict[str, Any], members: Sequence[str]) -> list[str]:
    """Member pairs where one consumes an artifact another member of the same group produces.

    The intra-group dependency check, and the reason some groups must not be overlapped. `plan_group`
    already refuses members with an *edge* between them, but an edge is not the only way to express
    order: member B can declare `inputs: [x]` while member A declares `outputs: [x]`, and the
    sequential walk would then run B after A and hand it the artifact. Overlapping that pair runs B
    before `x` exists, which is a *different* result for the same inputs — the one thing a parallel
    run must never do quietly.

    Returns descriptions of the dependent pairs, in declared order. Empty means the members are
    genuinely independent, which is the only state in which overlapping is claimed to be equivalent.
    """
    produced: dict[str, str] = {}
    for member in members:
        for name in _outputs_of(manifest, member):
            produced.setdefault(name, member)
    dependent: list[str] = []
    for member in members:
        for name in [str(i) for i in (_declared_declaration(manifest, member).get("inputs") or [])]:
            owner = produced.get(name)
            if owner and owner != member:
                dependent.append(
                    f"{member} consumes {name!r}, produced by {owner} in the same group")
    return dependent


def group_ceiling(config: Any = None, *, capacity: int | None = None) -> int:
    """How many group members may overlap: the engine's own bound, not a second one.

    Two numbers could answer this — `executor.fanout_max_parallel`, and the machine-derived capacity
    the scheduler uses. Taking the *minimum* is the whole point: a fan-out bound of 8 on a machine
    whose ceiling is 2 must not become 8 because the work arrived as nodes. When capacity cannot be
    derived the configured fan-out bound is used, which is honest about not having measured rather
    than pretending to a number the machine never gave.

    Returns at least one: a group that overlaps zero members is the sequential behaviour again, and
    reporting it as a "parallel" group would be the original dishonesty in a new place.
    """
    section = getattr(config, "executor", None) if config is not None else None
    raw = getattr(section, "fanout_max_parallel", None) if section is not None else None
    try:
        configured = max(1, int(raw)) if raw is not None else DEFAULT_CEILING
    except (TypeError, ValueError):
        configured = DEFAULT_CEILING
    if capacity is None:
        return configured
    try:
        measured = max(1, int(capacity))
    except (TypeError, ValueError):
        return configured
    return max(1, min(configured, measured))


def find_group(manifest: dict[str, Any], group_id: str) -> dict[str, Any] | None:
    """The `parallel:` block with this id, or None when the manifest declares no such group."""
    for block in manifest.get("parallel") or []:
        if not isinstance(block, dict):
            continue
        if str(block.get("id") or "") == str(group_id):
            return block
    return None


def _outputs_of(manifest: dict[str, Any], node_id: str) -> list[str]:
    for node in manifest.get("nodes") or []:
        if isinstance(node, dict) and str(node.get("id")) == node_id:
            return [str(o) for o in (node.get("outputs") or [])]
    return []


def _declared_declaration(manifest: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in manifest.get("nodes") or []:
        if isinstance(node, dict) and str(node.get("id")) == node_id:
            return node
    for gate in manifest.get("gates") or []:
        if isinstance(gate, dict) and str(gate.get("id")) == node_id:
            return gate
    return {}


def _conflicting_outputs(manifest: dict[str, Any], members: Sequence[str]) -> list[str]:
    """Outputs more than one member declares. A shared output is a write race, not a fan-out.

    The same rule the library's validator states as `parallel %s: conflicting writer for
    field/artifact`. Enforced again at run time because a manifest can reach the runner without having
    been validated — the planner emits these, and an edited file is a supported path.
    """
    seen: dict[str, int] = {}
    for node_id in members:
        for name in _outputs_of(manifest, node_id):
            seen[name] = seen.get(name, 0) + 1
    return sorted(name for name, count in seen.items() if count > 1)


def _successor_closures(manifest: dict[str, Any]) -> dict[str, set[str]]:
    """node id -> every node reachable from it by following edges. Cycle-safe."""
    edges: dict[str, list[str]] = {}
    for edge in manifest.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if source and target:
            edges.setdefault(source, []).append(target)
    out: dict[str, set[str]] = {}
    for start in list(edges):
        reached: set[str] = set()
        stack = list(edges.get(start, []))
        while stack:
            nid = stack.pop()
            if nid in reached or nid == start:
                continue
            reached.add(nid)
            stack.extend(edges.get(nid, []))
        out[start] = reached
    return out


def plan_group(group: dict[str, Any] | None, manifest: dict[str, Any], *,
               members: Iterable[str] | None = None, ceiling: int | None = None) -> GroupPlan:
    """Decide whether a group's members may genuinely overlap, and how wide.

    Every refusal here names the reason, because each one is a case where overlapping would produce a
    *different final result* than the sequential walk — which is the one thing that must not happen
    quietly.

    Raises
    ------
    ParallelError
        When the group is absent or malformed, has fewer than two runnable members, its members write
        the same output, a member depends on another member, or the requested ceiling admits only one.
    """
    if group is None:
        raise ParallelError("no parallel group to plan")
    group_id = str(group.get("id") or "")
    declared = [str(m) for m in (group.get("nodes") or [])]
    if members is not None:
        wanted = {str(m) for m in members}
        declared = [m for m in declared if m in wanted]
    join = str(group.get("join") or "all")

    runnable = [m for m in declared if _declared_declaration(manifest, m)]
    if len(runnable) < 2:
        # The library's validator refuses a group of fewer than two members for the same reason a
        # fan-out refuses one item: one thing is not a fan-out, and calling it one is a label not a
        # behaviour.
        raise ParallelError(
            f"parallel group {group_id!r} has {len(runnable)} runnable member(s); overlapping needs "
            "at least two, and a group of one is sequential work wearing a concurrency label"
        )

    conflicts = _conflicting_outputs(manifest, runnable)
    if conflicts:
        raise ParallelError(
            f"parallel group {group_id!r} has members writing the same output ({', '.join(conflicts)}); "
            "running them concurrently would race two writers onto one artifact, so the group is "
            "refused rather than overlapped"
        )

    closures = _successor_closures(manifest)
    ordered: list[str] = []
    for index, member in enumerate(runnable):
        for later in runnable[index + 1:]:
            if later in closures.get(member, ()) or member in closures.get(later, ()):
                raise ParallelError(
                    f"parallel group {group_id!r} lists {member!r} and {later!r}, but the graph has an "
                    "edge between them, so they are ordered work and overlapping would run a successor "
                    "before its predecessor"
                )
        ordered.append(member)

    # An edge is not the only way to express order. A member that consumes what another member of the
    # same group produces is ordered by *data* rather than by the graph, and the sequential walk would
    # have run them in that order. Overlapping is refused here for the same reason as an edge.
    dependent = shared_gate(manifest, ordered)
    if dependent:
        raise ParallelError(
            f"parallel group {group_id!r} is not independent: {'; '.join(dependent)}. Overlapping "
            "these would run a consumer before its producer, which changes the result"
        )

    width = _normalise_ceiling(ceiling)
    if width < 2:
        raise ParallelError(
            f"parallel group {group_id!r} has a ceiling of {width}, which admits no overlap; the "
            "members would run sequentially, so the group is reported rather than silently serialised"
        )

    return GroupPlan(
        group_id=group_id, members=ordered, join=join, ceiling=width,
        reason=(f"{len(ordered)} members declare disjoint outputs and no edges between them; "
                f"overlapping up to {width} at a time"),
    )


def _normalise_ceiling(ceiling: int | None) -> int:
    """Normalise a requested ceiling. A missing bound is the default, never unbounded."""
    if ceiling is None:
        return DEFAULT_CEILING
    try:
        return max(1, int(ceiling))
    except (TypeError, ValueError):
        return DEFAULT_CEILING


def run_group(plan: GroupPlan,
              run_member: Callable[[str], dict[str, Any]],
              *, on_event: Callable[[str, dict[str, Any]], None] | None = None) -> GroupOutcome:
    """Run a group's members on a bounded wave and aggregate the results deterministically.

    `run_member(node_id) -> result` is injected rather than implemented here, so this module owns the
    group *policy* — the bound, the wave, error isolation, ordering — and the executor owns what a
    member actually does. That split is what lets the same plan drive a real run and a test.

    The aggregation is keyed by node id and never by completion order, so the outcome for identical
    inputs is identical whatever order the provider finished them in. That is the property that lets a
    parallel run be compared against a sequential one at all.
    """
    outcome = GroupOutcome(group_id=plan.group_id, ceiling=plan.ceiling)
    results: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    exceptions: dict[str, BaseException] = {}
    guard = threading.Lock()
    slot = ConcurrentSlot(plan.ceiling)
    started: list[str] = []

    def _emit(kind: str, payload: dict[str, Any]) -> None:
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:  # noqa: BLE001 - a sink must never break a group
            pass

    def _member(member: str) -> tuple[str, dict[str, Any] | None, str, BaseException | None]:
        # The slot is held across the call, so the ceiling is a real bound on how many members are in
        # flight rather than a decoration on the wave that launched them.
        with slot:
            with guard:
                started.append(member)
            _emit("parallel.member_started", {"group": plan.group_id, "node": member})
            try:
                result = run_member(member) or {}
            except Exception as exc:  # noqa: BLE001 - one member must not lose the others
                return member, None, str(exc) or exc.__class__.__name__, exc
        return member, result, "", None

    def _record(member: str, result: dict[str, Any] | None, error: str,
                exc: BaseException | None) -> None:
        if error:
            failures[member] = error
            if exc is not None:
                exceptions[member] = exc
            _emit("parallel.member_failed", {"group": plan.group_id, "node": member,
                                             "error": error[:200]})
            return
        results[member] = result or {}
        _emit("parallel.member_done", {"group": plan.group_id, "node": member,
                                       "status": str((result or {}).get("status") or "")})

    def _one(member: str) -> None:
        member_id, result, error, exc = _member(member)
        with guard:
            _record(member_id, result, error, exc)

    _emit("parallel.group_started", {"group": plan.group_id, "members": list(plan.members),
                                     "ceiling": plan.ceiling, "reason": plan.reason})
    # `limit` is the ceiling: a group whose members outnumber the machine's bound must queue, not
    # launch everything and rely on the provider to cope. The bound is what the rest of this engine
    # enforces for the same reason — opening N concurrent model calls melts a local provider.
    run_wave(list(plan.members), _one, limit=plan.ceiling)

    outcome.results = results
    outcome.failures = failures
    outcome.exceptions = exceptions
    outcome.peak_in_flight = slot.peak
    _emit("parallel.group_finished", outcome.as_dict())
    return outcome
