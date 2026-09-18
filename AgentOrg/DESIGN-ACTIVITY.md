# AgentOrg — Design: Activity, and the Org That Matches the Goal

How a person can tell what an autonomous org is doing — and how the org it runs is
the one the goal actually asked for.

Fourth of the amendments:

| Amendment | Question it answers |
|---|---|
| [`DESIGN-WORKSPACE.md`](DESIGN-WORKSPACE.md) | *Which folder is the code?* |
| [`DESIGN-GOAL.md`](DESIGN-GOAL.md) | *What does "keep going" mean?* |
| [`DESIGN-SUBAGENTS.md`](DESIGN-SUBAGENTS.md) | *How is parallel work isolated and read back?* |
| **DESIGN-ACTIVITY.md** (this) | *What is it doing, why, and is the org the right one?* |

---

## 1. The problem stated exactly

The engine recorded everything and explained nothing. A real run made the gap
concrete: a goal that said *"use the CEO skill and bring a market researcher and
capture market"* produced

```
manifest.proposed … nodes: [pm, architect, api, developer, reviewer, qa, security]
… 219 s …
nodes: { pm: { status: blocked, verdict: guardrail-blocked }, architect: pending, … }
```

Three defects, all silent:

1. **The org was wrong, and nothing said so.** `planner._select_skills` composed a
   fixed software pipeline plus a keyword table of *software* words. "CEO", "market
   researcher" and "capture market" matched none of them, so the goal ran as an
   engineering build. The agents the Owner had hired were never consulted.
2. **The CEO the Owner hired was invisible.** `cmd_agents` and every run command
   resolved the roster from `--root` (a directory *of* projects) and ignored
   `--project` (the attached folder). The CEO lived in `Ideas/.agentorg/roster.json`
   and was never loaded.
3. **Why it stopped was thrown away.** The runner wrote
   `pm → blocked / guardrail-blocked` and a `log` entry naming the reason; `_settle`
   kept only `{status, verdict}`. So the run's own record contained the explanation
   and the product showed none of it.

The honest summary of the experience — *nothing is happening and I cannot tell why* —
is the failure this amendment removes.

## 2. The org matches the goal

A goal is classified into a **domain**, and the domain chooses the org:

| Domain | Build chain | Verifiers |
|---|---|---|
| `software` | product-manager → system-architect → api-designer → backend-developer | code-reviewer, qa-engineer, security-reviewer |
| `strategy` | ceo-strategist → business-strategist → fp-and-a-analyst | bizdev-manager, critical-thinker |
| `gtm` | product-manager → marketing-manager → growth-engineer → content-strategist | product-analyst, critical-thinker |
| `research` | product-manager → ux-researcher → business-intelligence-engineer | product-analyst, critical-thinker |
| `data` | product-manager → system-architect → data-engineer | product-analyst, critical-thinker |

Three rules keep this honest:

- **A named capability is an instruction.** "use the CEO skill and bring a market
  researcher" puts *both* in the plan, because naming a procedure is not a hint to be
  voted on.
- **Intent beats a technical noun.** A stated domain word ("strategy", "go-to-market",
  "research") is tested before `software`, so "build a booking SaaS" stays software
  while "capture market" does not drag a business goal back into engineering.
- **The graph's invariants are unchanged.** Every domain composes the *same* shape —
  sequential phases, a parallel verification fan-out, one bounded rework loop, one
  reachable human gate. Only the people differ, because termination is a property of
  the graph, not of the work. The software domain composes byte-identically to before,
  which the existing planner tests assert.

The chosen shape is printed (`Shape: strategy`) and travels with the plan, because
"why this org?" must not be something the Owner infers from the node list.

## 3. A gap is named with its fix

When a roster is known, the planner reports the capabilities the plan needs that
nobody holds, each with the exact hire that closes it:

```
Staffing gaps — nobody in the roster holds these capabilities:
  ux-researcher    ux-researcher    no agent in the roster holds this skill
                   close it: engine.cli hire <name> --skill ux-researcher
```

This is the planning-time twin of `Binder.staffing_gaps`, placed *before* approval
because a gap is cheapest to fix while the Owner is looking at the graph. A planner
with no roster reports no gaps — it must not invent one it cannot know about.

## 4. `--project` means the whole project

One resolver, `_project_root_for(args)`, feeds every command that reads or writes
user content. `--project P` names the project folder; `--root D` names a directory of
projects; both mean "the roster and skills for this work live here". So `hire`,
`agents`, `plan`, `org`, `skills new`, `pool` and `fanout` all read and write
`P/.agentorg/`, and an agent hired for one repository is used by that repository's
runs.

The same rule applies to a run's *own* state. `Orchestrator._run_workspace` returns
the attached workspace when there is one, so `run_state.json` and the manifest land
beside the trace rather than in a sibling `root/<slug>/`. Before this, `status`
(reading the attached folder) saw no run while the trace said otherwise — a
split-brain where the product and its own record disagreed.

## 5. Why a run stopped is a first-class fact

`Run.stop_reason` is derived from the runner's own record, in priority order:

1. a named blocking action in the runner's log (`guardrail`, `contract`, `error`) —
   the most specific cause, including the blocked node's own summary;
2. otherwise a blocked node, named with its verdict and its words;
3. otherwise the runner's outcome (`loop-max-iterations`, `cost-budget`, …) with the
   escalation detail;
4. otherwise an abort or an error.

`_settle` also keeps each node's `summary` and `iterations` and a tail of the runner's
log, so the explanation survives into the checkpoint. `run`, `status`, `activity` and
the app all print it; a run that says `blocked` now says *why* in the same breath.

**It is derived, never stored twice.** No new writer, no second source of truth —
the reason is a projection of what the engine already wrote.

## 6. Activity: one answer to "what is happening?"

`engine/activity.py` reads the run checkpoint, the trace, the diagnostics, the goal,
the child transcripts and the proposals, and folds them into one report:

```jsonc
{
  "headline": "Stopped — pm: a hand-off payload was blocked by the edge guardrail",
  "phase": "awaiting_human",
  "counts": { "nodes": 7, "done": 0, "blocked": 1, "pending": 6, "subagents": 0 },
  "going": { "phase": "awaiting_human", "continues": false, "phase_note": "gate" },
  "next_action": { "kind": "investigate", "label": "Investigate why the run stopped",
                   "command": "engine.cli status --slug console" },
  "timeline": [ … ordered entries, newest last … ]
}
```

Design rules, each earning its place:

- **Derived, never stored.** A second store of "what happened" is a second thing to
  keep true, and this codebase refuses that class of duplication everywhere else.
- **Every source optional.** A fresh workspace is a calm answer ("nothing is running"),
  not an error; a half-written file is skipped.
- **Bounded.** The trace is read tail-first and capped, so a long run yields a summary
  rather than a file dump — the same reason `LogStore` is a ring buffer.
- **One headline, one next action.** The two things a person wants first are computed
  explicitly rather than left to be inferred from the timeline. The next action is
  ordered by urgency: a gate or a block is work only the Owner can unblock, then a
  staffing gap, then an armed-but-idle goal.

The **same report** backs `engine.cli activity` and the app's **Activity** tab, via
the `status` snapshot, so the CLI and the console cannot disagree about what happened.
The tab badges the sidebar when work is waiting on you, because that is exactly what
"go here and look" means.

## 7. What is deliberately not here

- **No auto-hiring.** A gap names the hire; it does not make it. Hiring is a spend and
  an identity decision, which is the Owner's.
- **No auto-resume past a gate.** Activity *shows* the waiting decision; `decide` still
  resolves it. A read-only report that also acted would be a second control plane.
- **No streamed timeline in the app.** The report is a snapshot on the poll the panels
  already make. A live ticker is a feature about the *app*, not about the work, and the
  event terminal already covers the raw stream.
