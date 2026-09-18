# AUTONOMY-MAP — how AgentOrg actually works, end to end

The one document that answers *"what happens when I say 'make this better', and what is doing it?"*

Every claim below is grounded in code, and each section names the file(s) it is describing. Where a
capability is **not** wired end to end, that is stated plainly rather than implied — an autonomy map
that oversells is worse than none.

---

## 0. The six levels, and which one decides what

```
PRINCIPAL  the person — one identity across orgs       portfolio.json    you are it
  └ ORG        an organisation you run                 roster.json       you register it
      └ MISSION    a standing purpose you state once   mission.json      you state it
          └ OBJECTIVE  one step toward it              (in mission.json) one at a time
              └ GOAL       a durable objective         goal.json         armed by you
                  └ RUN        one graph execution     run_state.json    the orchestrator
                      └ NODE       one agent, one skill checkpoint       the executor
                          └ CALL       one model call, or a loop of them
```

A **principal** runs several orgs; a **fleet** (`engine/fleet.py`) runs them **at once**, each on its
own orchestrator and thread, bounded by one global concurrency ceiling and a per-org daily budget. The
orgs are peers: they share the person, never each other's agents, budgets, missions or mailboxes.

Ownership is deliberate at every level:

| Level | Who ends it | Where it lives | File |
|---|---|---|---|
| Principal | you | `~/.agentorg/portfolio.json` | `engine/portfolio.py` |
| Org | you (register/enable) | `<org>/.agentorg/roster.json` | `engine/org/roster.py` |
| Mission | you (`mission clear`) or derived from its objectives | `.agent_state/mission.json` | `engine/mission.py` |
| Goal | the agent's `update_goal`, a gate, a budget, or you | `.agent_state/goal.json` | `engine/goal.py` |
| Run | the graph finishing, a gate, or a kill | `.agent_state/run_state.json` | `engine/orchestrator.py` |
| Node | the executor, checked against the skill's contract | the checkpoint | `engine/executor.py` |
| Call | the model, or the bounded tool loop | the trace | `engine/agentloop.py` |

> **The spend rule.** Only a **Goal** spends, and only when you explicitly arm it. A Mission is
> explicitly *not* able to arm a goal — `mission start` sets the goal, and the goal is what arms — and
> a Portfolio cannot run or budget anything at all: a Fleet only *runs* work a goal already authorised.
> So neither level can silently run up a bill (see `mission.py`, `portfolio.py`, `fleet.py`).

---

## 1. The pipeline: goal → approved graph → run

```
"make this better"
   │
   ▼  engine/planner.py
   classify the goal into a DOMAIN ──────────────►  software | strategy | gtm | research | data
   pick the domain's BUILD chain + VERIFIER set
   add named roles ("use the CEO skill") and keyword specialists
   compose: chain → parallel verification → bounded rework loop → agent gate → human gate
   ask the LIBRARY GRAPH how the plan hangs together (coherence + consensus gaps)
   report STAFFING GAPS (nobody holds this skill) with the exact hire
   │
   ▼  engine/orchestrator.py  prepare()
   validate against the library's own validator
   write the manifest, bind every node to an agent (reviewers kept independent)
   present it for approval — nothing executes yet
   │
   ▼  approve() → execute()
   engine/host.py spawns the library's workflow-runner.py with a generated executor + guardrail
```

The graph's shape is the same in every domain, because termination is a property of the graph, not of
the work:

```
build chain ──► ╭─ reviewers (parallel) ─╮
                ╰────────────────────────╯
                     │  verdict != pass
                     ▼
              bounded rework loop  ──(max_iterations, exit_when, convergence)──►
                     │ exhaustion
                     ▼
              AGENT gate (kind: agent, max_reroutes: 2)  ← the org reroutes itself, bounded
                     │ exhausted
                     ▼
              HUMAN gate (kind: human)   ← only you hold terminal authority
```

**Files:** `engine/planner.py`, `engine/orchestrator.py`, `engine/host.py`, `Skills/scripts/workflow-runner.py`

---

## 2. Skills: what an agent is actually held to

A **skill** is a procedure from the pinned library, plus a *typed contract* read from its frontmatter.

```
SKILL.md
 ├─ workflow:            inputs · outputs · completion criteria · evidence: required · escalate_to
 ├─ Production Checklist ids the prompt requires every one of
 ├─ Ground Rules / NEVER      → the primacy zone (attended to most)
 ├─ anti-rationalization      → the excuses this role may not make
 └─ chain:                   consumes_from · feeds_into   → the dependency graph
```

Prompt assembly is cache-first (`engine/prompts.py`): guardrails first, the ~28 KB SOP in the middle
(byte-identical across nodes so it caches), the volatile intake last-but-one, the output contract last.

**The role is derived, not listed** (`engine/skills/roles.py`): whether a node *judges* or *produces*
comes from the skill's own name convention, declared outputs and description — so the binder and the
planner agree, and a judging skill the library adds needs no code change.

**The graph is read, not ignored** (`engine/skills/graph.py`): the library's ~2,200 `chain:` edges are
used to review a plan — which of its skills the corpus says are unrelated, and which prerequisites
several of them declare that the plan omits. A raw gap check over a graph this dense is noise, so the
view is calibrated to consensus.

```bash
engine.cli skills list                     # what the org can do
engine.cli skills show code-reviewer       # the contract, checklist and research gate
engine.cli skills graph                    # the dependency graph, whole or one skill
engine.cli skills graph --review A B C     # review a chosen set as one plan
```

---

## 3. Hiring and binding: who does the work

Two questions, two mechanisms, and they are separate on purpose.

**Capacity — who exists** (`engine/people.py`, `engine/org/roster.py`)

```bash
engine.cli hire Sana --skill code-reviewer --model qwen2.5-coder:14b
engine.cli agents                          # built-ins + every hire, from both roots
```

The roster is *yours*, not the library's: `~/.agentorg/roster.json` (global) and
`<project>/.agentorg/roster.json` (project, wins per agent name). `--project P` points both roster and
skills at `P/.agentorg/`, so an agent hired for one repository is used by that repository's runs.

**Selection — who does *this* node** (`engine/org/binding.py`, `engine/org/router.py`)

| Policy | Picks | When the plan wants it |
|---|---|---|
| `pinned` | the agent named in config | a specific person must do it |
| `round-robin` | next candidate | spread work across equals |
| `load-balanced` (default) | idle → most capable → name | the ordinary case |
| `swarm` | **every** candidate, for a quorum vote | one question, N opinions |

Two refusals make this safe: a **reviewer is never its own producer**
(`binding.assert_independent`), and a **hire with an unknown context window is refused** rather than
failing mid-run.

---

## 4. Handoff: what crosses a node boundary

A handoff is the *only* thing that crosses between agents, so it is contract-checked in code, never
requested in a prompt (`engine/org/handoff.py`).

```
PROPOSED → ACCEPTED/REJECTED → IN_PROGRESS → FULFILLED | BREACHED → ESCALATED
```

Nine required payload fields (`status`, `summary`, `artifacts`, `decisions`, `open_questions`,
`verification_evidence`, `context`, `budget`, `next`), enforced by eight mechanical rules R1–R8 —
each with an id, so a refusal reads *"R2: constraint count dropped from 5 to 3"*.

Two guardrails sit on the edge (`engine/guardrail.py`): a **secret** found in a payload blocks it
(critical), and an **instruction-shaped phrase** blocks it (agent output is data, never instruction).
A block is recorded with its reason and surfaced as the run's `stop_reason`.

---

## 5. The four ways work runs in parallel

The library distinguishes them; the engine implements all four.

| Primitive | Question | Declared by | Runs in |
|---|---|---|---|
| **Fan-out** | split *one job* across N agents | node `fanout:` + `items:` | `engine/fanout.py` |
| **Swarm** | ask N agents *one question*, take the majority | node `binding: swarm` | `engine/executor.py` |
| **Subagents** | isolated children with their own context | executor fleet | `engine/subagents.py` |
| **Pool pull** | an agent takes the next task it can do | node `from_pool: true` | `engine/pool.py` |

A **swarm** is worth its N× cost only because the majority decides; it is capped (`swarm_max_voters`,
default 3) and the cap is *reported*, not applied silently. **Subagents** each get a fresh session but
share the parent's pinned prefix, so the cacheable bytes are not re-derived per child — and their
transcripts are durable and paged, so a 4,000-line result is read as a reference, not re-sent whole.

> **A note on `parallel:`.** The manifest vocabulary and the validator support a `parallel:` block,
> and the planner emits one for the review fan-out. The stdlib runner **joins** the members correctly
> but executes them **sequentially** — real concurrency lives inside a node (fan-out / swarm /
> subagents), not across nodes. `join: any|majority` maps to `all`. This is stated because a "parallel
> review" that is actually serial is the kind of thing a map should not paper over.

---

## 6. Agents hiring agents

`engine/org/delegation.py` is mostly refusals, because agent-recursion is the most dangerous feature:

- a **reuse-first ladder** — spawning is the last rung, and the evidence it was tried is required;
- **six hard invariants S1–S6** — depth cap, cycle detection, budget *partitioning* (never creation),
  least-privilege capabilities, the five-element context pass-through, and recorded lineage;
- a **tiered approval** — cheap and reversible auto-approves; durable, privileged or expensive reaches
  you with the full justification.

Helpers die with their scope; specialists are retired deliberately (`retirement_review`,
`anti_sprawl`).

---

## 7. Missions, goals, and how they keep going

```
engine.cli mission set "ship the MVP" --objective "auth green" --objective "pagination" --arm
engine.cli mission start --index 0          # hands the active objective to a Goal (spends)
engine.cli mission status                   # objectives, progress, what is active
engine.cli mission advance --summary "…"
```

A mission is **one active objective at a time**; finishing one advances to the next. Its state is
*derived* from the objectives, so it cannot say "active" while everything is done.

The Goal runtime is what actually keeps working: `goal set` arms it, and after each run it re-enters
execution until the agent reports `update_goal(complete|blocked)`, a gate parks it, the budget slice
is spent, or you stop it. When the goal's own verdict lands, `mission_sync` moves the objective — so a
**mission advances autonomously** from a goal's verdict, never from a heuristic.

```
mission set → arm → start(goal) → run → agent reports complete → objective done → next objective
                                     └ reports blocked → objective blocked → mission blocked
```

**Files:** `engine/mission.py`, `engine/goal.py`, `engine/orchestrator.py` (`_run_with_goal`, `mission_sync`)

---

## 8. The portfolio: one person, several orgs

```
engine.cli portfolio init "Elon Musk"
engine.cli portfolio add Tesla  --slug tesla  --path ~/work/tesla  --daily-budget-usd 50
engine.cli portfolio add SpaceX --slug spacex --path ~/work/spacex
engine.cli portfolio status --live                 # every org's mission, spend and blockers
engine.cli run --goal "add pagination" --org tesla # any command, scoped to one org
engine.cli portfolio run spacex "close the launch checklist"   # runs *in parallel* with Tesla
```

A **principal** is one identity across every org; an **org** is a folder plus a roster, with a stable
`id` that survives a rename and a `principal_id` that links it to the person. The register
(`~/.agentorg/portfolio.json`) points at orgs — it never copies one — and is deliberately inert: it
cannot plan, run or spend.

A **fleet** (`engine/fleet.py`) holds one orchestrator per org, built lazily, and runs several at
once — each on its own thread. Two ceilings bound it: a **global concurrency ceiling** (the same
machine-derived number the scheduler uses, so the fleet can never promise more than the machine holds)
and a **per-org daily budget** (so one runaway org cannot consume the principal's whole allowance).
Isolation is by construction — one orchestrator, one workspace, one bus per org — so org A's agents
never see org B.

`--org` scopes **every** existing command to a chosen org, resolving the org's folder through the same
resolver `--project` uses, so multi-org needs no second set of commands. In the app, the **Portfolio**
panel (first in the sidebar) lists the orgs with their mission and spend and offers Run / Stop / Switch.

```
portfolio add → org has identity (id + principal) → fleet runs N orgs concurrently, bounded
              → each org keeps its own mission → goal → runs → ledger
```

**Files:** `engine/portfolio.py`, `engine/fleet.py`, `engine/org/roster.py` (identity), `engine/cli.py`, `engine/serve.py`

---

## 9. Memory, evals, improvement

| Layer | What it does | File |
|---|---|---|
| **Memory** | write → consolidate (count, don't re-summarise) → read behind a *context-only* trust label | `engine/memory.py` |
| **Evals** | behavioural scenarios scored against a frozen baseline; a regression blocks | `engine/evals/` |
| **Improver** | reads its own traces, drafts fixes, proves them against the baseline, **stops** — it never applies | `engine/improver.py` |

Recalled memory is injected as *background knowledge, never a directive*, and every memory entry
carries that label in the prompt (`prompts.py`), which is the poisoning defence.

---

## 10. Observability: "what is happening?"

```bash
engine.cli activity --project ~/code/my-app     # headline, timeline, gaps, one next action
engine.cli status                               # phase, gate, gaps, stop_reason, per-node detail
```

`engine/activity.py` reads what the engine already wrote — checkpoint, trace, diagnostics, mission,
goal, child transcripts, proposals — and folds it into one ordered story. The same report backs the
app's **Activity** tab, so CLI and console cannot disagree.

**Why a run stopped** is a first-class fact (`Run.stop_reason`), derived from the runner's own log: a
blocked hand-off, a violated contract, an exhausted loop, a cost ceiling. It is shown by `run`,
`status`, `activity` and the app.

**Files:** `engine/activity.py`, `engine/diagnostics.py`, `engine/telemetry.py`

---

## 11. The events, so you can watch it

The engine emits a typed event per transition (`engine/protocol.py`, mirrored in
`macos/Sources/AgentOrgKit/ProtocolModels.swift`). The groups, so you know the vocabulary:

| Group | Examples |
|---|---|
| run lifecycle | `run.start`, `run.queued`, `run.admitted`, `run.paused`, `run.resumed`, `run.end` |
| graph | `manifest.proposed`, `manifest.approved`, `node.enter`, `node.exit`, `loop.pass`, `loop.stagnation` |
| agents | `agent.spawn`, `agent.status`, `agent.org.changed`, `agent.quarantined` |
| routing & handoff | `route.proposed`, `route.decided`, `handoff.proposed`, `handoff.breached`, `handoff.verified` |
| work products | `artifact.written`, `checklist.result`, `review.rejected`, `review.approved` |
| subagents | `subagent.spawned`, `subagent.progress`, `subagent.done`, `subagent.read` |
| goal | `goal.armed`, `goal.progress`, `goal.paused`, `goal.completed`, `goal.blocked` |
| human control | `human.gate`, `human.decision`, `human.takeover`, `instruct`, `decision.recorded` |

`trace.jsonl` is NDJSON, so ordinary tools work on it:

```bash
tail -20 .agent_state/trace.jsonl
grep '"type":"review.rejected"' .agent_state/trace.jsonl
```

---

## 12. Where it is autonomous, and where it deliberately is not

**Autonomous (no person in the loop):**

- planning, classifying the goal and composing the org;
- binding every node, keeping reviewers independent;
- executing, iterating the rework loop, detecting stagnation;
- **bounded reroutes** after exhaustion (the agent gate);
- **passing an auto-approvable gate when a goal authorises it** — the agent gate and a policy route
  class the config already answered, recorded as decided by `goal`, not by you. A **terminal** gate is
  never passed (see the table below);
- **staffing a gap** — a node whose skill no agent holds gets a helper on the default model, ephemeral
  by default, so the plan is runnable rather than blocked on a roster accident;
- fan-out, swarms, subagents, pool claims;
- a goal continuing past a model final, and a mission advancing on a goal's verdict;
- memory consolidation, and the improver detecting and drafting.

**Deliberately not autonomous (a person must act):**

| Stop | Why |
|---|---|
| release / close / spend | a *terminal* gate (`kind: human`) — only you hold that authority, and no setting passes it |
| a goal that chose `--human-gate` | you asked to be involved; the goal parks at every gate |
| a goal whose `auto_approve`/`auto_hire` is off | same, narrowed to one decision rather than all |
| an agent gate with no untried route | approving with nothing to act on would loop, so it reaches you |
| a policy `confirm` route class | you asked to be asked *and* the goal did not override it |
| hiring beyond the auto tier | a spend and an identity decision (`goal.auto_hire_max_tier`) |
| applying an improvement | the improver proposes; it never applies |
| arming a goal or a mission | unattended spend must be an explicit act |
| clearing a guardrail block | the block is a decision, and the reason is surfaced, not auto-resolved |

> **The default polarity.** A goal is *autonomous* unless you chose otherwise, because the product is a
> tool you leave running. Three things keep that honest: a terminal gate is never passed, a goal that
> asked for a human gate parks everywhere, and every automatic decision records `by: goal` in the run's
> decisions — so "the org passed this" and "you passed this" are never confused. See
> [`DESIGN-DEFAULTS-AUTONOMY.md`](DESIGN-DEFAULTS-AUTONOMY.md).

---

## 12a. Who is working on what: the flow board

`activity` answers *what is happening* as a story. `flow` answers *who is on what* as a board — one row
per unit of work with its owner and its information flow. It reads the run context, the checkpoint, the
`node.bind` diagnostics and the `handoff.*` trace events, and projects them; it stores nothing.

```bash
engine.cli flow --project ~/code/my-app     # the board, for a person
engine.cli flow --json                      # the same, for a tool
```

Each row carries the **owner** (the agent bound to that node, from the run context and the binding
diagnostics, so a resumed run still names who ran it), **in from / out to** (the handoff that brought
its inputs and the one it produced), and the **status, verdict and the runner's own reason** when it is
not done. A blank owner is a node nobody holds — the staffing gap, visible on the board.

The app's **Flow** panel is fed from the same `flow` command, so CLI and console cannot disagree.

---

## 13. The honest gap list

Things this map would be lying to omit:

1. **`parallel:` runs sequentially** in the stdlib runner (Section 5). Concurrency is inside a node.
2. **`type: supervisor` is unreachable.** The manifest vocabulary and the validator support it
   (`workers`, `routing: parallel|sequential|select`); neither the planner emits one nor the runner
   executes one. Work that wants a supervisor today uses fan-out + a lead agent (`engine/decompose.py`).
3. **Agent-written code is not executed.** No sandbox exists, so nothing runs produced code.
4. **Credentials live in a `0600` file**, not the Keychain (designed, not built).
5. **The chain graph is used to *review*, not to *compose*.** It is a diagnosis on a plan, not yet a
   generator of one — the planner still composes from its domain tables, then asks the graph whether
   the result hangs together.
6. **Mission objectives are stated, not generated.** The engine does not yet decompose a mission
   statement into objectives on its own; that is a planner one level up, not built.
7. **The fleet's concurrency is process-local.** A `Fleet` lives inside one `serve` process; two
   engines pointed at the same portfolio would each keep their own ceiling and their own run
   registry, so the *combined* concurrency could exceed the machine-derived number. Running a single
   engine per portfolio is the intended deployment.
8. **The portfolio is global-only.** `portfolio.json` lives in `~/.agentorg/`, not per-project. A
   project-local portfolio (a different set of orgs for one checkout) is a further refinement, not
   built.

---

## 14. The one-page version

```
you: portfolio init "you" + add the orgs you run        (one person, several companies)
  → switch to an org (`--org`, or the Portfolio panel)
      → mission set "ship the MVP" + objectives, arm
          → mission start → a Goal per objective
              → planner classifies the goal, picks the org, plans the graph, checks it against the library graph
              → you approve once
                  → the runner executes: bind → hand off (typed, guardrailed) → work → verify
                      → rework loop iterates, bounded; exhaustion → agent gate reroutes, bounded; then a human
                      → fan-out / swarm / subagents / pool where the plan asks
                  → the agent reports complete|blocked → the goal ends → the mission advances
              → memory records it; the improver may propose a fix (never applies)
              → `activity` and the Activity tab show the whole story, including why it stopped
  → `portfolio status --live` (and the Portfolio panel) show every org at once,
    with the fleet running several in parallel under one global ceiling and per-org budgets
```

Everything above is either a file in `engine/`, a command in `engine.cli`, or an event in
`engine/protocol.py`. When a claim here and the code disagree, the code is right and this file is a
bug.
