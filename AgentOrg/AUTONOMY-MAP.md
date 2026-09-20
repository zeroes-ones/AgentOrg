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
              HUMAN gate (kind: human)   ← terminal authority: released by an `unattended` goal
                                           with evidence, or decided by you
```

The gate's `kind` never changes — the planner always emits `kind: human`, so the graph always has a
reachable terminal gate and the library's own validator stays satisfied. What changes is *who may
answer it*, which is the goal's **posture**: an `unattended` goal may release it (guarded, evidenced,
ledgered), and a `supervised` goal waits for you exactly as before. See §12.

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

**The cache is durable, not just measured** (`engine/cachestore.py`). Cache hits are only a discount if
the prefix is genuinely reused, so the pinned prefix hash, every prefix-shape observation and the
per-request savings are written under `.agent_state/cache/`:

```
.agent_state/cache/
  prefix/<hash>.json     one per pinned prefix: hash, skill, tools, size, first/last seen
  shapes.jsonl           every shape observation — so "why did the prefix change" survives a restart
  savings.jsonl          read/write cache tokens and the cost delta, from the provider's own Usage
```

The store is append-only with a bounded size, so an overnight run cannot fill the disk. Two honesty
rules matter: a hit rate the provider did not report is stored as **absent**, never as zero, and the
store is derived from the provider's reported `Usage` rather than estimated. Because the store is on
disk, a *resumed* run can prove its prefix is unchanged — the question "is this run still cache-warm?"
has an answer after a restart, which in-memory tracking alone could not give.

**The store is consulted, not merely written** (`engine/context/cachealign.py`). A provider reuses a
request only up to its first changed byte, so a scattered removal invalidates the whole tail. Eviction
therefore drops one *contiguous run* of evictable turns, chosen with the store's warm-prefix verdict,
and a compaction that breaks a warm prefix is recorded with the hash and the character at which the
cut fell (`session.cache_invalidated`). A resumed run re-pins from the store's record rather than
re-deriving, so a continuation does not re-pay for bytes the provider already holds.

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

**Every node edge produces one of these.** The executor assembles the payload from data it already
computed, validates it stage by stage, and only then lets it cross. The stages are the contract's own
ordering, which is why R7 is checked *after* acceptance rather than before it — acceptance is
precisely what satisfies the rule, so checking it earlier would refuse every legitimate handoff. A
payload that clears every stage is:

- **persisted** to `.agent_state/handoffs/<handoff_id>.json`, so a crossing outlives the process;
- **emitted** as `handoff.proposed` → `.accepted` → `.fulfilled` → `.verified`, each carrying
  `handoff_id`, `from_node`, `to_node` and a summary — which is what the flow board reads, and why that
  board shows real edges rather than an empty list;
- **recorded in the decision ledger** at the edge's own gate name, so "which node handed what to whom,
  and on what grounds" is answerable afterwards.

A refusal is never a crash: it is returned as a result so the runner's bounded rework handles it, and
it surfaces as the run's `stop_reason` with the rule that fired. The rework has two shapes, because a
node has two places to be in: a **loop member** is retried by the loop's own `max_iterations`, and a
node **outside any loop** is retried by `--contract-rework` (bounded, and each retry is told the rule
that fired). When the bound is spent the refusal parks as a gate exactly as it always did — the window
changes who recovers while the run is moving, never whether the contract is enforced. One handoff is
produced *per node*, deliberately — a swarm's voters or a fan-out's items would otherwise put edges on
the board that no successor ever consumed.

Two guardrails sit on the edge (`engine/guardrail.py`): a **secret** found in a payload blocks it
(critical), and an **instruction-shaped phrase** blocks it (agent output is data, never instruction).
A block is recorded with its reason and surfaced as the run's `stop_reason`.

### When a model gets stuck: the repetition guard

A node's tool loop is bounded by `max_steps`, and the final step runs **without tools** so the model
must answer. That covers running out of budget, but not the failure that matters most for a run you
leave alone: a model that re-issues the *same* call every step. It spends the entire bound and finishes
with nothing, and because nobody is watching the steps go by, the first symptom is a node that
produced no work.

So the loop also watches for repetition (`engine/agentloop.py`, governed by
`goal.repeat_call_reminders`, default `(3, 5, 8)`):

- **It reminds, it does not stop.** A repeated call is often a legitimate retry after a transient
  failure, so the calls still execute; the loop injects a message naming the streak and suggesting a
  different call, different arguments, or stating the blocker.
- **The reminder follows the tool results it comments on**, so it reads as feedback on a completed
  step rather than an instruction issued before it — and so a model's tool calls stay adjacent to
  their results, which provider APIs expect.
- **Detected on the whole call signature, not the tool name.** A model working through a list issues
  the same tool with different arguments every step, and that is correct behaviour, not a loop. Order
  is ignored, so swapping two independent calls does not count as a repeat either.
- **Off unless configured.** An empty tuple disables it, so a caller that never asked for the guard
  is unaffected.

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
> but walks them **one at a time** — it is a single-node scheduler by construction, so real
> concurrency has to live inside a node. That is now where it does: a group whose members opt in with
> `concurrent: true` has its members dispatched concurrently *inside one `execute_node` call*
> (`engine/parallel.py`), bounded by the same ceiling a fan-out honours and with results aggregated by
> node id so the outcome does not depend on completion order. The runner is unchanged, so its
> traversal, step budget, per-node checkpoint and `--state` resume behave exactly as before.
> `join: any|majority` still maps to `all`. See gap 1 in Section 13 for what this does *not* cover.

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
is spent, the round cap is reached, or you stop it. When the goal's own verdict lands, `mission_sync`
moves the objective — so a **mission advances autonomously** from a goal's verdict, never from a
heuristic.

```
mission set → arm → start(goal) → run → agent reports complete → objective done → next objective
                                     └ reports blocked → objective blocked → mission blocked
```

The goal's **posture** decides whether it can get there without you:

```bash
engine.cli goal set "add pagination" --posture unattended   # finish alone (the default)
engine.cli goal set "add pagination" --posture supervised   # stop at every gate
engine.cli run --goal "add pagination" --posture supervised # the same choice, stated on the run
engine.cli defaults autonomy --posture supervised           # what every *new* goal inherits
```

A goal copies the configured posture when it is created and then owns its copy, so editing
`credentials.json` cannot change the authority of an objective already in flight. `--human-gate` is
still accepted as the legacy alias for `--posture supervised`.

**Files:** `engine/mission.py`, `engine/goal.py`, `engine/orchestrator.py` (`_run_with_goal`,
`mission_sync`, `_release_terminal_gate`)

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

## 11a. Sessions you can list, hand over, and branch

A run's whole story is already on disk, so a **session is a workspace**: `run_state.json` is one
checkpoint, and a projects root is the set of sessions you have. There is no index file, because an
index is a second thing that can disagree with the directories it describes.

```bash
engine.cli session list                          # every session under a projects root, newest first
engine.cli session show --slug my-app            # the goal, the run, the nodes, the handoffs, the spend
engine.cli session export --slug my-app -o app.zip   # a self-contained ZIP: trace, goal, ledger, handoffs, cache
engine.cli session fork --slug my-app --to my-app-b  # branch the state; the original is untouched
```

Two properties make this safe rather than convenient:

- **`fork` copies and never moves.** It writes only into a slug that does not exist yet, so the
  original is byte-identical afterwards *by construction* rather than by care — which is the whole
  point, since the reason to branch is to compare against the attempt you already made.
- **Reading is defensive; writing is not.** `list` reports a session it cannot parse as unreadable
  rather than crashing, because "what do I have" must answer even when one answer is "this one is
  broken". `export` and `fork` refuse loudly: an archive that quietly omits a file is worse than none.

## 11b. Schedules: starting work without you

```bash
engine.cli schedules add --slug my-app --goal "close the open findings" \
  --posture unattended --every 6h
engine.cli schedules list                        # what is armed, what is due, what is paused
engine.cli schedules watch --once                # start what is due, in the foreground
engine.cli schedules remove --slug my-app
engine.cli schedules enable --slug my-app        # the deliberate act that re-arms a paused entry
```

A scheduled run goes through **the same posture path** a manual one does, so it is exactly as
auditable — same gates, same ledger, same evidence rules. The one design decision that matters:

> **A fire that ends parked disables its own entry.** If a run finishes paused, blocked, gated or
> failed, the schedule does not fire again until you explicitly `enable` it. A schedule that re-armed
> a failing goal on every tick would be an unattended spend loop with a friendly name — precisely the
> failure the whole autonomy design exists to prevent, and the reason `mission` may not arm a goal and
> `portfolio` may not run anything.

`max_fires` bounds a repeating entry, and a slug with no project disables its entry rather than
crashing the watcher.

**Files:** `engine/session_exchange.py`, `engine/schedules.py`

---

## 12. Where it is autonomous, and where it deliberately is not

**Autonomous (no person in the loop):**

- planning, classifying the goal and composing the org;
- binding every node, keeping reviewers independent;
- executing, iterating the rework loop, detecting stagnation;
- **bounded reroutes** after exhaustion (the agent gate);
- **passing an auto-approvable gate when a goal authorises it** — the agent gate and a policy route
  class the config already answered, recorded as decided by `goal`, not by you;
- **releasing the terminal gate, when the goal's posture is `unattended`** — the release is guarded
  four ways and is the one decision that lets a goal *finish* with nobody watching (see below);
- **staffing a gap** — a node whose skill no agent holds gets a helper on the default model, ephemeral
  by default, so the plan is runnable rather than blocked on a roster accident;
- fan-out, swarms, subagents, pool claims;
- a goal continuing past a model final, and a mission advancing on a goal's verdict;
- memory consolidation, and the improver detecting and drafting.

**Deliberately not autonomous (a person must act):**

| Stop | Why |
|---|---|
| a goal whose posture is `supervised` | you asked to be involved; every gate parks |
| a release with no evidence | the gate's declared artifacts are not all present, so there is nothing to release |
| a release after a guardrail or contract failure | a safety control *fired*; autonomy may decide the work is done, not that a control was wrong |
| a release over a blocked node | the node said it could not proceed, so "done" would be a lie |
| an unrecordable release | if the ledger refuses the decision, the run parks rather than releasing silently |
| a goal whose `auto_approve`/`auto_hire` is off | the same pause, narrowed to one decision rather than all |
| an agent gate with no untried route | approving with nothing to act on would loop, so it reaches you |
| a policy `confirm` route class | you asked to be asked *and* the goal did not override it |
| hiring beyond the auto tier | a spend and an identity decision — `goal.auto_hire_max_tier` caps the delegation tier an auto-created helper may reach; above it the gap is reported to you with the tier and the reason rather than staffed |
| applying an improvement | the improver proposes; it never applies |
| arming a goal or a mission | unattended spend must be an explicit act |
| clearing a guardrail block | the block is a decision, and the reason is surfaced, not auto-resolved |

> **The default polarity.** A goal is *unattended* unless you chose otherwise, because the product is a
> tool you leave running. The authority is one word — the goal's **posture**:
>
> | Gate kind | `unattended` (default) | `supervised` |
> |---|---|---|
> | `agent` (bounded reroute) | passed when it carries a route | parked |
> | `policy` (a route class) | passed only if the config already permits it | parked |
> | `human` **terminal** | released by the goal, with evidence, on the record | parked |
>
> Four things keep that honest. The manifest is unchanged — the planner still emits `kind: human`, so
> the graph keeps a reachable terminal gate and the library's own invariant still holds. A release
> requires the gate's evidence to be **present**; a gate that cannot show its evidence parks exactly as
> it always did. A release is **refused** when a guardrail or contract failure fired, or when any node
> ended blocked. And every automatic decision is written to the decision **ledger** and recorded
> `by: goal`, so "the org released this" and "you released this" never conflate.
>
> `supervised` is the entire safety floor in one word, and it is asserted by an eval rather than
> documented: `check_autonomy_floor` in `engine/evals/runner.py`.
>
> See [`DESIGN-DEFAULTS-AUTONOMY.md`](DESIGN-DEFAULTS-AUTONOMY.md).

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

1. **`parallel:` overlaps only where the group opts in, and the runner is still one node at a time.**
   A group marked `concurrent: true` has its members dispatched genuinely concurrently inside one
   `execute_node` call (`engine/parallel.py`), bounded by the same ceiling a fan-out honours, with
   results aggregated by node id. It is opt-in because a parallel run must produce the same final
   result as a sequential one, and that is only claimed for a group whose members are proven
   independent — disjoint outputs, no edge between them, no member consuming a sibling's artifact,
   and a ceiling of at least two. Anything else is refused with the reason and falls back to the
   runner's own order. What is **not** done: the shared library runner
   (`Skills/scripts/workflow-runner.py`) still visits one node at a time, so two *unrelated* branches
   of a graph that no group declares still run nose-to-tail, and making the runner overlap them means
   teaching it to dispatch a group, merge N run-state deltas deterministically and keep `--state`
   resume correct. That is the correct long-term home — it is a change to a program other tools run,
   so it has not been made.
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
