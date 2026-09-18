# AgentOrg — Design: Mission, the Skill Graph, Derived Roles, and Agent Gates

How the org's *intent* became durable, and its *decisions* autonomous where that is safe.

Fifth of the amendments:

| Amendment | Question it answers |
|---|---|
| [`DESIGN-WORKSPACE.md`](DESIGN-WORKSPACE.md) | *Which folder is the code?* |
| [`DESIGN-GOAL.md`](DESIGN-GOAL.md) | *What does "keep going" mean?* |
| [`DESIGN-SUBAGENTS.md`](DESIGN-SUBAGENTS.md) | *How is parallel work isolated and read back?* |
| [`DESIGN-ACTIVITY.md`](DESIGN-ACTIVITY.md) | *What is it doing, why, and is the org the right one?* |
| **DESIGN-AUTONOMY.md** (this) | *What is it all for, and who decides when it is stuck?* |

Later amendments: [`DESIGN-DEFAULTS-AUTONOMY.md`](DESIGN-DEFAULTS-AUTONOMY.md) adds the default
provider/model, per-goal autonomy (a gate the org can pass, a gap it can staff), and the flow board.

The end-to-end picture — every level, every command, every event, and where it is autonomous — is
[`AUTONOMY-MAP.md`](AUTONOMY-MAP.md). This document is the design reasoning behind the four additions
that map records.

---

## 1. The problem

The engine had a **Goal** — a durable objective that keeps going — and nothing above it. So a
long-running org could say *what it is doing now* but not *what it is for*: a sequence of objectives
with no thread through them. Three narrower gaps compounded it:

1. **The library shipped a dependency graph the engine never read.** All 327 skills declare
   `chain: consumes_from/feeds_into` — ~2,200 symmetric edges — and the library's own
   `workflow-graph-authoring` skill says node selection should use it. The engine parsed the fields
   into `SkillBundle` and then composed plans from hand-written tables, discarding the library's own
   knowledge about which procedure depends on which.
2. **"Who judges vs who produces" was hardcoded, twice, disagreeing.** `binding.py` and `planner.py`
   each kept their own reviewer set, so a skill added to one was a producer in the other — and a plan
   would bind a reviewer to its own producer.
3. **Nothing emitted an agent gate.** The manifest vocabulary, the validator and the runner all
   supported a `kind: agent` gate with bounded reroutes; no plan ever contained one, so every loop
   exhaustion went straight to a person.

## 2. The Mission: a durable *why*

```
Mission  →  Objective  →  Goal  →  Run  →  Node
(why)       (a step)      (a loop)  (a graph) (one agent, one skill)
```

A Mission owns an ordered list of **objectives** and activates **one at a time**. It is deliberately
narrow: it does not plan, route, or spend. It answers three questions — *what is the purpose, which
step are we on, what is next* — and nothing else (`engine/mission.py`).

Four decisions make it safe:

- **A mission never spends.** Arming is still exclusively a Goal transition. `mission start` *sets*
  the goal that works the objective; the goal is what arms. A mission that could arm a goal would be
  an unattended spend with a friendly name, so `mission_start` takes `--no-arm` and defaults the
  spend decision to the caller, exactly as `goal set` does.
- **State is derived, never stored.** `Mission.state` and `Mission.progress` are computed from the
  objectives, so the headline cannot say "active" while every item is done, nor "completed" while one
  is pending — the class of bug where a dashboard contradicts its own list.
- **Loaded disarmed, exactly like a Goal.** `mission.json` is written atomically
  (temp + `os.replace` + fsync) and schema-versioned, and a mission read from disk comes back paused
  with reason `restored`. A mission must never resume itself because a process restarted.
- **One active objective.** Concurrency lives *inside* a run (swarm, fan-out, subagents); a mission
  advancing two objectives at once would be two conflicting spends and no single thread.

**It advances itself, conservatively.** `Orchestrator.mission_sync` runs after each run settles: if
the active objective's goal reported `complete` or `blocked`, the objective follows it and the mission
moves on. Only a goal's *own* verdict moves an objective — never a heuristic — which is the same
completion rule the Goal runtime already uses.

## 3. The library's chain: graph, made usable

`engine/skills/graph.py` reads the corpus's `chain:` edges and answers the questions a planner, a
reviewer or a person actually asks: what does this skill depend on, what depends on it, what is the
right order, what does this set leave out.

The design problem is that the graph is **deliberately dense and mutual**: a producer and its reviewer
reference each other, and nearly every skill lists ~40 upstream. A raw "missing dependency" check
returns *hundreds* of names and says nothing about a particular plan. So the useful view is
**calibrated** to two signals that survive the density:

- **Coherence** — how many of a plan's own nodes the corpus relates to each other. A node with *zero*
  in-plan neighbours is one the library says is unrelated to everything else being run: a real
  "these belong in different runs" warning.
- **Consensus prerequisites** — a skill several of the plan's nodes declare they consume, which the
  plan omits. That is the "everyone here needs X and X is missing" gap, which is exactly what a person
  misses by hand.

Two more rules keep it honest: **cycles are reported, not smoothed over**
(`topological_order` returns `(ordered, cyclic)`, because a mutual pair is a real property and a plan
that silently dropped one would misreport its own order), and **everything is deterministic** (sorted
edges, name-broken ties), so two runs over the same corpus produce the same order and a plan stays
reviewable.

**The graph informs; it does not dictate.** It is a read-only diagnosis on a plan the planner already
composed (`Plan.graph_review`), surfaced in `plan`'s output and reviewable directly with
`engine.cli skills graph --review A B C`. Using it to *generate* the plan is a larger change and is
listed as a known gap in the map.

## 4. Roles derived, not listed

`engine/skills/roles.py` answers one question — *does this node judge, or produce?* — from the skill
itself: its name convention (`*-reviewer`, `*-auditor`, `verification-*`), its declared `outputs` (a
`*-report` is a judgement), and its description. The old hardcoded sets are kept as a **floor**
(`KNOWN_VERIFIERS`), so replacing them cannot lose a role that already worked; the derived signal only
*adds*.

Two calibration choices were forced by real mistakes the tests now pin:

- A `-plan` output is **not** a verdict. Including it swept `code-formatting-and-linting`
  (`enforcement-plan`) into the verifier set.
- The default is **producer**, not a guess at "gate". A node wrongly treated as a verifier is bound to
  a different agent and denied write tools, which breaks real work; a gate is a narrower claim the
  metadata cannot make reliably.

`binding.py`, `planner.py` and (for phase assignment) the planner's `_phase_for` all consume this one
answer, so the binder and the planner cannot disagree about who a reviewer is.

## 5. Agent gates: autonomous while bounded

When the rework loop exhausts its iterations, the runner asks the gate for a decision. A
`kind: agent` gate answers with an untried channel to lead one fresh pass — a decision the org can make
itself, bounded by `max_reroutes`, before bothering a person (`Skills/scripts/workflow-runner.py`,
`engine/executor.py`'s `mode: identify`).

The runner and the executor always supported this; **nothing emitted one**. The planner now does:

```
loop --exhaustion--> reroute-gate (kind: agent, pool = loop members, max_reroutes: 2)
                          |
                          +-- reroute (bounded) --> back into the loop, corrective channel first
                          +-- exhausted/stagnant --> human-gate (kind: human)
```

The gate's pool is the loop's members, so a reroute stays inside the rework; and the gate escalates
onward to the human gate, so the terminal authority is unchanged. The eval scenario that asserted
"loop exhaustion must reach the human gate" was updated to the stronger, still-true invariant:
exhaustion reaches a human gate **in at most one bounded hop**, and an agent gate that did not reach a
human would be unbounded autonomy — exactly what the check forbids.

**One bug this exposed.** The `software` domain reused a single `_DEFAULT_SHAPE` that contained *both*
the build chain and its verifiers, so identifying "the chain's producer" to hand findings back to
resolved to the last entry — a reviewer, which cannot fix anything. The build chain and the verifier
set are now separate for every domain, and the loop hands findings to `developer`.

## 6. What is deliberately not here

- **The graph does not compose plans.** It reviews them. Composition stays in the domain tables, where
  it is explicit and testable; the graph is the check.
- **Missions do not generate objectives.** A person states them. Decomposing a mission statement into
  objectives is a planner one level up, and it is not built.
- **`type: supervisor` is not emitted or executed.** The vocabulary and validator support it; the
  runner does not. Work that wants a supervisor uses fan-out plus a lead agent (`engine/decompose.py`).
  Recorded as a gap in the map rather than implied to work.
- **No auto-hiring.** A staffing gap names the hire; it does not make it — that is a spend and an
  identity decision.
