# AgentOrg

A native macOS application that runs a **model-agnostic, multi-agent software engineering
organisation**. You own the org: you hire named agents, bind each to a skill from the
[`zeroes-ones/Skills`](https://github.com/zeroes-ones/Skills) library, give them a model, and point
them at a project. They hand off work to each other through typed contracts, iterate in bounded
loops, and stop at a gate you control.

The premise is that an effective engineering org is mostly *procedure plus people*, and both are
already written down. The skill library holds the procedures — 327 SOPs with typed contracts,
completion criteria and production checklists. This application supplies the people: the same skill
can exist many times under different names, on different models, with different budgets, and they
work together the way a team does.

---

## Contents

| Document | Read it when |
|---|---|
| **README.md** (this file) | You want to know what this is, install it, and run it |
| [AUTONOMY-MAP.md](AUTONOMY-MAP.md) | You want the whole picture: portfolio → mission → goal → run, skills, hand-off, hiring, swarms, gates — and where it is autonomous (and where it deliberately is not) |
| [USAGE.md](USAGE.md) | You are using it day to day: the CLI, the app, hiring agents, planning work |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Something is wrong, or you need to debug a run |
| [ARCHITECTURE.md](ARCHITECTURE.md) | You are changing the code |
| [OPERATIONS.md](OPERATIONS.md) | You are tuning cost, concurrency, health or policy |
| [EVALUATION.md](EVALUATION.md) | You want to understand context, memory, telemetry or the eval suite |
| [DESIGN.md](DESIGN.md) | You want the full design record (master plus the focused amendments) |

---

## The idea in one table

| Concept | Is | Identity | Who owns it |
|---|---|---|---|
| **Skill** | A procedure: markdown plus a typed contract | `code-reviewer` | The Skills library (immutable) |
| **Agent** | An employee: a named instance bound to skills + a model | `ag_7f3a` / "Sana" | You |
| **Team** | A group with a lead | "Quality" | You |
| **Org** | Roster, reporting lines, topology | `org_tesla` | You |
| **Principal** | The person who runs several orgs | `pr_owner` / "you" | You |
| **Portfolio** | The register: the principal and their orgs | `portfolio.json` | You |
| **Task** | One unit of work routed to one agent | `task_014` | The orchestrator |
| **Artifact** | A typed, hashed output | `change`, `prd` | The producing agent |
| **Handoff** | A contract-checked artifact transfer | `Alice → Sana` | The orchestrator |
| **Run** | One project execution | `run_2026…` | The engine |

A skill is **capability**; an agent is **headcount**. That split is what makes "the same skill,
three different people" natural rather than special-cased — and a **principal** running several
**orgs** is the same idea one level up: the person is shared, the agents are not.

## How work is assigned

Two complementary models, and the difference is worth knowing because they answer different problems.

**Push — a planned graph.** The default. The planner turns a goal into a validated graph, the binder
chooses an agent per node, and the runner executes it. This is what makes a run reproducible and its
bindings auditable: *why did this node run as that agent?* has a recorded answer.

**Pull — a task pool.** For work that is *discovered* rather than planned. An agent that finds a
missing migration puts it in the pool; any capable agent can then claim it. Capability decides who
does the work (`required_skills`, `required_capabilities`), so a backend developer cannot accidentally
claim a database migration — and a claim carries a lease, so a worker that dies mid-task does not
strand it forever.

```bash
engine.cli pool add "backfill the new column" --skill database-designer --priority 80
engine.cli pool claim --agent Migrator        # refused unless it holds the skill
```

A node can also **pull** rather than be pushed. Declaring `from_pool: true` on a node makes the agent
that runs it claim the best task it is capable of, so a run can drain queued work:

```yaml
nodes:
  - id: drain
    skill: database-designer
    from_pool: true          # take the next task this agent can do
```

Pooled work that a node fails is marked failed with the node's summary, so it does not silently
re-enter the pool behind a worker that already tried it.

**A swarm is a node that votes.** Binding a node with `binding: swarm` runs several agents on the same
question and takes the **majority** — which is the only reason a swarm is worth its N-times cost. A
2–1 split yields the majority verdict and records the tally, so disagreement stays visible instead of
being averaged away. The voter count is capped (3 by default, `executor.swarm_max_voters`) and the cap
is reported.

**A fan-out is a node that splits.** Declaring `fanout` with a `{{item}}` template and an `items` list
runs one subagent per item — the *other* swarm primitive, for throughput rather than judgment:

```yaml
- id: review-all
  skill: code-reviewer
  fanout: "Review {{item}} for regressions."
  items: ["src/a.ts", "src/b.ts", "src/c.ts"]
```

Or from the CLI, with a spend-free rehearsal first:

```bash
engine.cli fanout "Review {{item}} for regressions." \
  --item src/a.ts --item src/b.ts --dry-run
```

**Or let a lead agent decide the work.** Naming the items is fine when you already know them; it fails
for the case the design is aimed at — *"harden the auth flow"*, where deciding what the work **is** is
most of the job. `--goal` hands that step to a lead agent, which explores the project with the same
capability-gated tools the workers use, decides whether a swarm is warranted at all, and produces the
items:

```bash
engine.cli fanout --goal "Find and fix the security problems in src/" --slug my-app --dry-run
```

Three things make it a lead rather than a generator:

- **It looks first.** A decomposition made without reading the project is guesswork.
- **It can decline.** A swarm on one coherent change multiplies cost and fragments accountability, so
  *"not separable"* is a real outcome with a reason — and a decline reached without reading the
  project says so, rather than presenting itself as considered.
- **Its output is validated, not trusted.** A swarm of one, a template with no placeholder, two items
  that expand alike, or a **path that does not exist in the project** is refused before a worker
  starts. That last one is not hypothetical: asked to review every file in `src/`, `qwen2.5-coder:14b`
  invented `file1.js … file4.js` for a project containing `auth.py` and `db.py`. The plan was
  well-formed and entirely fiction.

Each item runs as a **full node execution** — same skill, same criteria, same evidence contract — so a
fan-out weakens no guarantee. The two are deliberately separate: a vote decides *whether something is
right*, a fan-out gets *all of it* done. `{{item}}` is enforced, duplicate expansions are refused, and
the node is `done` only when every item succeeded — a half-reviewed change set reports `needs_review`
with the failures named.

**A fan-out queue adapts to a provider that pushes back.** A fixed batch assumes concurrency never
changes, and it does: a 429 means the *next* batch of four will also be refused. So the queue halves
its limit on a rate limit and **re-queues** the refused item — a limit is the provider's state, not
the item's failure — then recovers one slot at a time after sustained success, and never drops below
one. Retries are bounded so an item that can never run is reported rather than looped on.

## Working on your own project

Point the engine at a folder and agents work *on it* rather than on scratch artifacts:

```bash
export AGENTORG_SKILLS_ROOT=/path/to/Skills
python3 -m engine.cli run --goal "add a subtract() function to calc.py with a docstring" \
  --root ~/code --slug my-app
```

`--root` is the **parent** and `--slug` the folder, so the above targets `~/code/my-app`. The engine
writes its state to `~/code/my-app/.agent_state/` and never outside it.

**Agents can read and change your code.** A node in a producing phase gets tools — `read_file`,
`list_dir`, `search`, `write_file` — so it reads the real files before deciding anything:

```
read_file  src/calc.py           → the actual contents, with line numbers
search     "def compute_cost"    → engine/gateway.py:343
write_file src/calc.py           → the file on disk changes, atomically
```

**And the permission to do so is scoped, not blanket.** An agent's capabilities decide what it may
reach, and a write outside them is refused with the reason:

| Agent | Capabilities | May do |
|---|---|---|
| Developer | `read:*` `write:src/**` | Read anything; write under `src/` only |
| Reviewer | `read:*` | Read anything; **write nothing** — a verifier cannot edit what it judges |

The gate is enforced at the tool, not asked for in a prompt, and a refusal tells the model what it
would need rather than leaving it to retry. Paths that escape the project — `../`, absolute, `~`, or a
symlink pointing out — are refused before anything is read or written.

**Nothing shells out.** `run_command` is deliberately absent: an unconstrained shell inside a real
repository is a much larger decision than a file write, and it deserves its own one.

**A node that uses tools is bounded.** It gets `executor.max_tool_steps` model calls (12 by default),
checked against the budget *before* each one, and a node that runs out of steps is reported
`needs_review` with the reason — a truncated investigation is never reported as finished.

**Verified against a real local model**, not only a scripted one. Asked to add a `subtract()` function
to a file, `qwen2.5-coder:14b` read the file with `read_file`, wrote it with `write_file`, and the
change landed on disk — autonomously chosen tool calls, three steps, 11 seconds.

That measurement mattered: it exposed three defects a scripted model could not, each of which made the
loop unusable on the default provider.

| Defect | Symptom |
|---|---|
| `Message.text` ignored `tool_result` blocks | The model saw its own tool call and `content: null` for the answer — blind |
| Ollama rejected the OpenAI tool shape | `HTTP 400` on every multi-turn tool conversation |
| Ollama returns no `tool_calls` for this model | The call sat in the text as bare JSON; the loop stopped after one step having done nothing |

The last two are Ollama's template quirks: it wants the call rendered *as text* inside `<tool_call>`
tags, and its server-side parser frequently finds nothing even when the model did the right thing. So
the adapter renders calls that way and **recovers** one the model wrote as plain text — accepted only
when it names a tool that was actually advertised, so a model's illustrative example is never executed
as a real call.

## Prompt caching

Providers bill cached input at a fraction of the miss rate — DeepSeek at roughly a tenth — and they
can only reuse a prefix when the **exact bytes** match. That makes caching a property of the prompt's
*shape*, not a setting, and it fails silently: the request succeeds, the answer is right, and the only
symptom is a larger bill.

**Cache stability is not a feature here; it is an invariant the multi-agent layer is designed
around.** Four things enforce it:

1. **Nothing agent-specific sits in the prefix.** The system prompt and the skill's procedure are
   byte-identical for every agent working that skill. The agent's *name* used to open the system
   prompt — so two reviewers voting on one question diverged at character 8, shared 4.6% of their
   prompt, and each paid full price for the same 19KB of procedure. The name now travels in a short
   tail instead. Measured: **4.6% → 99.3% shared, and a 3-voter swarm costs 57% less.**

   | Surface | Shared prefix |
   |---|---|
   | Vote swarm (N agents, one question) | **99.3%** |
   | Fan-out (one skill, N items) | 85% — the rest is the genuinely different task text |

   There is a real tension here, and the smaller end wins. The output contract is the last thing the
   model reads, but it is **8.8% of the prompt**; the identity is **0.58%**. Putting identity before
   the contract would make every voter in a swarm re-pay for that 8.8%, so identity trails instead —
   and its wording is written to reinforce the contract rather than appear to relax it, because an
   earlier phrasing ("nothing above changes because of your name") read as if the rules above were
   merely informational.

2. **A `Prefix` object owns the cacheable bytes, and a run *pins* them.** The prefix is computed per
   (skill, tools) pair, hashed over the bytes actually sent, and held fixed for the life of the run.
   Two things make this enforcement rather than convention:

   - `Prefix.for_skill` **refuses** an agent name inside the system prompt, because that specific
     mistake is the one that cost 58% on every swarm.
   - `PrefixPins` detects a prefix that would move and **reports it with both hashes** instead of
     letting it move. This matters because the executor loads the skill bundle on every node and the
     skill source deliberately re-parses when a `SKILL.md`'s hash changes — so without pinning, an
     edit *during* a run silently changed the prefix for every later node, and the only symptom was a
     larger bill. Now the pinned bytes keep being sent and the drift is stated; accepting the change
     is a deliberate call.

   Its `diff()` names which region changed, so a miss is actionable — "the procedure changed" rather
   than "the cache stopped working". Tool schemas are sorted first, because the same tools in a
   different order are different bytes to a provider.

3. **The cache is read and billed.** Every dialect that reports cache tokens is parsed
   (`prompt_cache_hit_tokens`, `prompt_tokens_details.cached_tokens`, `cache_read_input_tokens`), and
   cached input is billed at the provider's cached rate with the saving computed from a counterfactual.

4. **Anthropic is asked explicitly.** Claude caches only what a `cache_control` marker covers —
   unlike DeepSeek and OpenAI, which reuse a matching prefix automatically. The engine read Anthropic's
   cache counters while never requesting caching, so on Claude nothing ever cached. Breakpoints now
   cover the system block and the tool list.

An unreported cache stays `None` throughout — never `0%`, never `$0.00` saved — for the same reason an
unmeasured cost is never rendered as free.

## What it actually guarantees

These are enforced in code, not requested in a prompt. Each is covered by a test.

- **No self-review.** A reviewer is structurally barred from judging its own work, and the default
  company binds reviewers to a *different model* than the producers —
  `verification-independence-engineer` requires the verifier to differ from the producer.
- **No unbounded loops.** Every generated graph contains a bounded loop with an explicit exit
  condition, a convergence window and an escalation target. Validated by the library's own
  validator, in memory and from disk.
- **No unmeasured spend.** A budget stop happens *before* a call, and every cost figure is labelled
  `measured`, `estimated`, `free` or `unknown`. An unmeasured run is never rendered as `$0.00`.
- **No silent delegation loops.** Depth is capped, cycles are refused, and a child's budget is
  *carved from* its parent's remainder rather than created.
- **No unverifiable completion.** A node's own skill supplies the criteria it must evidence, and a
  criterion with no evidence is an open item rather than a checkbox.
- **No hidden health action.** An agent is quarantined only past a minimum sample, on evidence, and
  the transition is recorded. A healthy average cannot hide a guardrail trip.
- **No secret in the repo.** Keys resolve from the environment, everything crossing the event bus is
  redacted, and a startup scan fails loudly if key material reaches the run-state directory.
- **Context rotation that preserves constraints.** `NEVER`/`MUST NOT` rules are counted and
  re-pinned; a rotation that would lose one is reverted.
- **A swarm vote is a majority, not an average.** A node bound `swarm` runs its voters and takes the
  majority verdict; a split is reported with its tally, and a quorum that was not reached is flagged
  rather than passed. Cost is summed over every voter, so voting cannot hide its own expense.
- **Pooled work cannot be taken by the wrong agent.** A task's required skills and capabilities are
  checked at claim time, its lease expires so a dead worker cannot strand it, and a completion that
  does not match its declared JSON schema is refused rather than stored.
- **The console cannot hang on a command.** The engine runs as a subprocess speaking NDJSON, every
  command is acknowledged with the reason for a refusal, and stdout carries the protocol only — so a
  diagnostic can never be mistaken for an event.
- **A cache discount is never assumed.** Cached input is billed at the provider's cached rate and the
  saving is computed from a counterfactual; an unreported cache stays unknown rather than becoming
  `0%` or `$0.00`, and a miss is attributed to a named cause rather than guessed at.
- **A fan-out keeps every result.** One item failing does not discard the others, and a partial
  fan-out reports `needs_review` — a half-reviewed change set is never reported as done.
- **A rate limit slows a fan-out, it does not break it.** The queue halves its concurrency on a 429
  and re-queues the refused item, because a limit is the provider's state and not the item's failure.
  Recovery is one slot at a time, and retries are bounded so nothing loops forever.
- **An agent cannot write outside what it was granted.** Tools are gated on capabilities checked at
  the call, not requested in a prompt: a reviewer holds `read:*` and cannot edit the artifact it
  judges, and a path that escapes the project is refused before anything is touched.
- **A truncated tool loop is never reported as done.** The loop is bounded, the budget is checked
  *before* each step, and running out of steps yields `needs_review` with the reason.

## Requirements

| Requirement | Notes |
|---|---|
| macOS 14+ | The native console targets SwiftUI on macOS 14 |
| Python 3.11+ | The engine is stdlib-only; no pip install needed |
| The Skills library | A checkout of `zeroes-ones/Skills` containing `scripts/workflow-runner.py` |
| A provider | Ollama or LM Studio locally, or an API key for OpenAI / Anthropic / DeepSeek |
| Swift 5.9+ (optional) | Only to build the macOS app; the engine runs without it |

Nothing is installed system-wide. The engine is pure stdlib, so there is no virtualenv to manage and
no dependency to drift.

## Install

```bash
cd AgentOrg

# 1. Tell it where the Skills library is (or let it find the default location).
export AGENTORG_SKILLS_ROOT=/path/to/Skills

# 2. Create your credentials from the template.
cp credentials.example.json credentials.json
chmod 600 credentials.json          # it holds secrets; keep it private

# 3. Prove the environment before doing anything else.
python3 -m engine.cli doctor
```

`doctor` checks seven things and names any that fail. Expect output like:

```
OK   configuration   credentials.json with 5 providers
OK   skills library  /path/to/Skills at commit 8fbfda61016b — capabilities checked; content unpinned
OK   skill bundles   327 skills parsed with criteria and checklists
OK   providers       built ['lmstudio', 'ollama']; skipped 3
OK   machine         10 cpus, 32.0 GB, ceiling 9 (cpu-bound only)
OK   model catalog   9 models with declared windows
OK   secret hygiene  no key material found in the run-state directory
```

The three `skipped` providers are the cloud ones without keys. That is expected and reported rather
than hidden — add `OPENAI_API_KEY` and re-run to see them build.

## First five minutes

```bash
# See what the library offers and whether each skill is enforceable.
python3 -m engine.cli skills list

# Inspect one skill: what an agent bound to it is actually held to.
python3 -m engine.cli skills show code-reviewer

# See which models you can bind, and how each window was determined.
python3 -m engine.cli models

# Turn a goal into a validated workflow, and show it rather than writing it.
python3 -m engine.cli plan --goal "Build a booking API with auth and payments"

# See the org you would run it with, and what it is missing.
python3 -m engine.cli org --goal "Build a booking API with auth and payments"

# What is happening: the headline, the timeline, the gaps and the one next step.
python3 -m engine.cli activity --project ~/code/my-app

# Every project that needs you, with the step that resolves each — no slug to remember.
python3 -m engine.cli attention

# Who is working on what: every unit of work, its owner, its handoffs and progress.
python3 -m engine.cli flow --project ~/code/my-app

# Every run this projects root holds; hand one over, or branch it to try an alternative.
python3 -m engine.cli session list
python3 -m engine.cli session export --slug my-app -o my-app.zip
python3 -m engine.cli session fork --slug my-app --to my-app-alt

# Start work on a timer — a fired run that ends parked disables its own entry rather than looping.
python3 -m engine.cli schedules add --slug my-app --goal "close the open findings" --every 6h
python3 -m engine.cli schedules watch

# What model does everyone run on? And how autonomous is a goal by default?
python3 -m engine.cli defaults
```

That penultimate command is the one to run before any real work: it prints the roster, the policy
matrix, the plan, the **staffing gaps** (capabilities the plan needs that no agent holds) and the
bindings — so you know what to hire before a run rather than discovering it mid-graph.

**One default, for everyone.** `defaults` is the single answer to *which model do my people run on*:
the built-in company, every hire you make, and every helper the engine creates for you all inherit it
unless you bind them to something else. `defaults set --provider P --model M` changes it in one
command (merging, never replacing — your keys and policy survive), and `defaults autonomy
--posture supervised` sets how autonomous a new goal is.

**A goal is unattended unless you chose otherwise.** Arming a goal is standing authority to answer the
gates it can answer and to create the person a missing skill needs — on the default model, ephemeral by
default, so a plan never parks three nodes in on a roster accident. That includes the **terminal** gate,
which is what lets a long goal actually finish with nobody watching — but only with the gate's evidence
present, never after a guardrail or contract failure, never over a blocked node, and always recorded in
the decision ledger as the *goal's* decision rather than yours. Set `--posture supervised` on a goal,
or flip the picker in the app's Work panel, and every gate waits for you exactly as before.

**What crosses between agents is contract-checked.** Every node edge produces a typed handoff — nine
required fields, eight mechanical rules — which is validated, persisted to `.agent_state/handoffs/`,
emitted as trace events, and recorded in the ledger. `flow` shows those crossings; a refused payload
becomes a rework iteration with the rule that fired named, never a crash.

**Who is working on what.** `flow` is the board: one row per unit of work with its owner, what came in
from which agent, what went out to whom, and why it stopped if it did. It reads what the engine already
recorded — the run's bindings, the handoffs, the spawns — so the CLI and the app's **Flow** panel
cannot disagree.

**The org follows the goal.** The planner classifies a goal and composes the *right* company for it:
a software build keeps the product-manager → architect → developer pipeline; a strategy goal staffs
the CEO, a business strategist and an FP&A analyst; a go-to-market goal staffs marketing and growth;
a research goal staffs a UX researcher. A named capability is an instruction, so *"use the CEO skill
and bring a market researcher"* puts both in the plan. The chosen shape is printed (`Shape: strategy`)
and travels with the plan. Before this, a non-software goal silently ran as an engineering build —
the goal text never reached the planner's keyword table, so the org was wrong and nothing said so.

**Why a run stopped is a first-class fact.** Every run records a `stop_reason` derived from the
runner's own log — a blocked hand-off (`pm: a hand-off payload was blocked by the edge guardrail — …`),
a violated contract, an exhausted loop, a cost ceiling — and it is shown by `run`, `status`, `activity`
and the app. A node's own summary survives onto the checkpoint too, so "blocked" always comes with the
reason.

`engine.cli activity` is the one to reach for when you do not know what is going on: it reads what the
engine already wrote and answers *what is it doing now*, *how did it get here*, *why did it stop* and
*what do I do next* — with the same report the app's **Activity** tab renders.

`engine.cli attention` answers the question one step further out: **what needs me across every project**.
`status` and `activity` are scoped to one workspace, so a run parked at a gate in a folder you had not
registered was invisible to the app and unreachable from the CLI at once. `attention` enumerates the
projects under a root and prints, for each one that is waiting, what it waits for and the exact command
that resolves it — and it decides nothing, listing gates without answering them. The app's **Now**
destination renders the same report for the workspaces that are not the one the window is acting on,
each with the engine's own next step and an **Adopt as an org** step for the ones that are not registered
yet (a run in another folder cannot be acted on until it is).

## Testing

There are three suites, and they answer different questions.

**The engine unit suite** proves the engine does what the code says.

```bash
python3 -m pytest tests/ -q      # if pytest is installed
python3 run_tests.py             # dependency-free, identical result
python3 run_tests.py -k routing  # filter by name
```

1482 tests, all offline: no network, no credentials, no provider required. They use a deterministic
in-process fake provider, so a failing test is reproducible rather than a coin flip. (Four
tests in `test_phase8_authoring.py` need a model with a probed context window and fail without a
reachable provider; they are the only environment-dependent ones.)

The macOS console has its own suite, in the same spirit and likewise offline:

```bash
cd macos && swift test
```

81 tests over the process bridge, the protocol models, the log store and the safe file writer — the
parts that must not be guessed at, exercisable without launching the UI.

**The behavioural suite** proves the engine makes *good decisions* — which a unit test cannot, because
"did the reviewer reject correctly" has no boolean to assert.

```bash
python3 -m engine.evals.runner                # 17 scenarios, with the regression gate
python3 -m engine.evals.runner --freeze       # record the current results as the baseline
python3 -m engine.evals.runner --only constraint-survival
```

Its failure mode is not a crash, it is **confident wrong output** — a run that completes and is wrong.
So each scenario names an invariant the system promises and the reason it matters:

| Scenario | The invariant it protects |
|---|---|
| `constraint-survival` | A `NEVER` rule survives compaction, rotation and re-pinning |
| `reviewer-independence` | A reviewer is never the producer of the artifact it judges |
| `delegation-safety` | S1–S5 refuse their violations; least privilege and lineage hold |
| `autonomy-floor` | No single setting can silently disable every human gate — including the `supervised` posture |
| `rotation-refuses-when-impossible` | An irreducible overflow is refused, not looped on |
| `diagnostics-refuses-secrets` | A shared bundle never carries a credential |
| `idempotent-effects` | A retried effect is not applied twice, while a rework is |
| …and 10 more | See `engine/evals/scenarios.json` |

The gate compares against a **frozen baseline** and blocks on a regression. It is a *delta* comparison
rather than an absolute one, because a run that gains on one scenario and loses on three is a
regression even at the same total — and a total is exactly what hides that. Lost coverage counts as a
regression too, and a scenario declared but unimplemented **fails** rather than skipping, because a
silently skipped safety check is how a suite decays into decoration.

## Configuring providers

`credentials.json` is where you say which models exist. You can write it by hand, or add a provider in
the app's **Providers** tab — *Test and fetch models* probes the endpoint before anything is saved, so
a wrong URL is caught before it reaches the file. The shape:

```json
{
  "providers": {
    "ollama":   { "kind": "ollama", "base_url": "http://localhost:11434", "concurrency": 1 },
    "anthropic":{ "kind": "anthropic", "base_url": "https://api.anthropic.com",
                  "api_key_env": "ANTHROPIC_API_KEY", "api_version": "2023-06-01" },
    "groq":     { "kind": "openai", "base_url": "https://api.groq.com/openai/v1",
                  "api_key_env": "GROQ_API_KEY",
                  "extra_headers": { "X-Tenant": "acme" } }
  },
  "models": {
    "known": {
      "qwen2.5-coder:7b": { "context_window": 32768, "max_output": 8192, "locality": "local" },
      "claude-sonnet-4-20250514": { "context_window": 200000, "max_output": 8192,
                                    "locality": "cloud" }
    }
  },
  "defaults": { "provider": "ollama", "model": "qwen2.5-coder:7b" },
  "budget": { "run_max_usd": 25.0 }
}
```

Four rules worth knowing:

1. **Prefer `api_key_env` over `api_key`.** The environment is not read into logs or traces, and a
   committed file cannot leak what it does not contain.
2. **`context_window` must be real.** An agent bound to a model with an unknown window is refused at
   binding time, because the session-projection design cannot size a prompt without it. `Ollama` can
   be *probed* for its real window (`models --refresh`); cloud endpoints usually report only ids, so
   declare those in `models.known`.
3. **`base_url` is the base, not the endpoint.** For an OpenAI-compatible host that is the part
   ending in `/v1` — `https://ollama.com/v1`, not `https://ollama.com/v1/chat/completions`. A full
   endpoint pasted in is reduced to its base automatically and the console says so, because appending
   `/models` to the full endpoint probes a path that does not exist and reports a good key as broken.
4. **`extra_headers` if the endpoint wants one.** A gateway routing key, an organisation id, or an
   `X-Api-Key` instead of a Bearer token. They are sent on every dialect (OpenAI-compatible, Anthropic,
   Ollama) and cannot override the protocol's own headers — `anthropic-version` or the auth header stay
   the adapter's business. Only the header *names* are ever reported or logged; a value may be a
   credential.

## How work flows

```
Your goal
   ↓  plan  ──────────────────────────  a validated graph you approve
PM → Architect → API Designer → Developer
                                    ↓
                     ┌──────────────┴──────────────┐
                     ↓              ↓              ↓
                 Reviewer        QA Engineer    Security
                     ↓              ↓              ↓
                     └────── review-verdict ──────┘
                                    ↓
                     verdict != pass → back to the Developer (bounded, max 3)
                                    ↓
                              Human release gate
```

An agent that needs help can hire one — but only after showing that reuse was tried, within a depth
cap, with a budget carved from its own, and with the five context elements the library requires. Cheap
reversible helpers are automatic; durable or privileged hires reach you with the full justification.

## The macOS console

The engine is the product; the console is the window onto it. It runs the engine as a **subprocess**
and reads its NDJSON event stream, so the app can never hang on agent work and a wedged run is killed
without taking anything else down.

```bash
./scripts/run-macos-app.sh            # build, bundle as a .app, and launch
./scripts/run-macos-app.sh --debug    # a debug build, when you want a readable stack trace
```

Then press **Launch Engine** (⌘⇧L) in the app — or open `macos/Package.swift` in Xcode and run.

**Why a script rather than `swift run`.** `swift run AgentOrg` does start it, but SwiftPM produces a
*bare executable*, and macOS decides how to treat a process from its bundle. Without an `Info.plist`
the app has no Dock presence and its window does not reliably come to the front, so it looks like
nothing happened. The script assembles a minimal `.app` around the same binary — and ad-hoc signs it,
so it launches locally without Gatekeeper quarantining it.

**Before a real run**, the app needs the engine's preconditions met, and it will tell you which is
missing rather than failing silently:

```bash
cd AgentOrg
cp credentials.example.json credentials.json && chmod 600 credentials.json
python3 -m engine.cli doctor          # all seven checks, named
export AGENTORG_SKILLS_ROOT=/path/to/Skills   # a checkout with scripts/workflow-runner.py
```

The app finds the repository by walking up from its own executable until it sees
`AgentOrg/engine/cli.py`, so it works from a `swift run`, a `--debug` build, and a bundled `.app`
alike.

One window, ten tabs, each answering exactly one question — a dashboard with no single question is
sprawl:

| Tab | The question it answers |
|---|---|
| **Portfolio** | *Which orgs am I running, and what is each doing?* — the principal and every org, with Run/Stop/Switch |
| **Org** | *Who do I have, and is the org healthy?* |
| **People** | *Who can I hire, and what are they on?* — hire, edit (keeping history), retire |
| **Providers** | *Which models can I reach, and with what?* — add a key/URL/headers, test, fetch models |
| **Improve** | *What does the system think is wrong with itself?* — proposals, and what it refused |
| **Activity** | *What is happening, why, and what do I do next?* — the headline, the timeline, the gaps, and the one next step |
| **Work** | *Where is work stuck?* — the run, its gates, the goal, and any swarm in flight |
| **Cost** | *What is this costing?* — including the prompt-cache hit rate and what it saved |
| **Context** | *How full are the agents' contexts?* |
| **Resources** | *Is the machine coping?* |

**Portfolio** is first because it is the *whole* picture: a person is not one org. It lists the several
orgs one principal runs — each with its own agents, missions, goals and budget — with the live mission,
spend and blockers per org, and Run / Stop / Switch on each. The engine keeps a fleet, so one org's run
does not block another's, bounded by one global concurrency ceiling and a per-org daily budget.

**Activity** answers the question a person actually asks of an autonomous org: *what is it doing?*
It renders one report the engine derives from the run checkpoint, the trace, the goal, the child
transcripts and the proposals — the present tense first (a headline like "Waiting on you: …" or
"Stopped — pm: a hand-off payload was blocked by the edge guardrail"), then progress and unstaffed
capabilities, then the single next action, then the ordered timeline of how it got there. It badges
the sidebar when work is waiting on you, and it is the same report `engine.cli activity` prints, so
the CLI and the app never disagree about what happened.

Two of those tabs close a gap worth naming: the engine was config-driven and the console could only
*show* the result. **Providers** now adds an endpoint (with a custom header if the gateway needs one),
**tests it before saving** so a bad URL never reaches the config, fetches the model list, and only
then lets you pick a model. **People** hires an agent, edits its model or level while **keeping its
id** — so the mailbox, session history, ledger entries and health record survive — and retires it.
Both write the same files the CLI does (`credentials.json` at `0600`, `.agentorg/roster.json`).

A note that matters on some macOS Pythons: `ssl.create_default_context()` can return a context that
trusts **nothing** (a python.org build points at a CA file it does not ship), which makes every HTTPS
provider fail identically and blame the endpoint. The transport resolves a real bundle —
`$SSL_CERT_FILE`, then the system stores — and refuses a bundle that loaded zero CAs, so a certificate
problem cannot masquerade as a provider being down.

API keys are stored in `credentials.json` today; the macOS Keychain is the designed path and is not
built yet. A key is never sent back to the window, so an edit starts with the field empty and an
untouched save leaves the stored value alone.

The terminal is always present because a run is the thing being watched. `AgentOrgKit` holds
everything with no view code — the process bridge, the protocol models, the log store, the safe file
writer — so the bridge can be unit-tested without a running UI; `swift test` exercises exactly that.

## The state of this project

Built and verified:

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundation: pinned library, config, NDJSON protocol, event bus, artifacts, versioning, idempotency, resources | complete, 109 tests |
| 2 | Model-agnostic gateway: 4 provider dialects, live model discovery, token calibration, cost correctness, RPC | complete, 112 tests |
| 3 | Skill ingestion, checklist-enforcing prompts, the planner | complete, 177 tests |
| 4 | Org model, routing, delegation, scheduling, health, SLOs, CLI | complete, 202 tests |
| 5 | Session/context lifecycle, memory, telemetry, behavioural eval suite | complete, 172 tests |
| 6 | Executor, host, and the native macOS console | complete, 74 engine + 81 Swift tests |
| 7 | Attach an existing project folder; the durable Goal runtime; isolated subagents | complete, 1184 engine + 97 Swift tests |
| 8 | Provider management and agent hiring/editing in the console | complete, 1265 engine + 108 Swift tests |
| 9 | The console's macOS UI, background running, and the propose-only self-improvement loop | complete, 1298 engine + 122 Swift tests |

Phases 1–6 build the engine and its console. **Phase 7 is what makes it a coding
agent you leave running on a real repository**, and it is the part to read first if
that is what you came for:

- **Attach your own folder.** `engine.cli run --project ~/code/my-app …`, or
  *Open Project…* (⌘O) in the app. The agents read and write *your* files; the
  engine's own state goes in `<folder>/.agent_state/`, and nothing else is created.
- **State a goal and let it continue.** `engine.cli goal set "…"` keeps working past
  a finished model turn until the agent reports the objective done or blocked. It is
  off unless armed, and a goal restored from disk comes back **disarmed** — a restart
  can never resume spending on its own.
- **Fan out over isolated subagents.** With `executor.subagents_enabled`, an agent can
  dispatch `task` (one child) or `fleet` (many), each in its own context sharing the
  parent's pinned prefix, and page a child's transcript with `read_subagent_result`
  instead of taking all of it or none.

The three design amendments behind them —
[`DESIGN-WORKSPACE.md`](DESIGN-WORKSPACE.md), [`DESIGN-GOAL.md`](DESIGN-GOAL.md),
[`DESIGN-SUBAGENTS.md`](DESIGN-SUBAGENTS.md) — record the reasoning and the
trade-offs, including where these choices *weaken* an existing guarantee. A fourth,
[`DESIGN-ACTIVITY.md`](DESIGN-ACTIVITY.md), records the org that matches the goal and
the one report that answers *what is it doing, why, and what next?*

The engine drives a goal end to end: `run` plans, binds and executes a graph,
`status` reports where it is, and `decide` / `instruct` are how you resolve a gate
or steer a running agent. That sequencing was deliberate: the design's own argument
is to make the engine work and be *provable* from the CLI before building a UI on top
of it.

**Known limitations, stated plainly:**

- **Agent-written code is not executed.** No sandbox exists, so nothing runs the produced code.
- **Credentials live in a `0600` file.** The macOS Keychain is the designed path, not yet built; a key added in the console is written to `credentials.json` like any other provider entry.
- **Self-improvement is propose-only.** The `trace → draft → promote` loop exists (`DESIGN-IMPROVER.md`, see the **Improve** tab): it detects defects from its own traces, drafts fixes, proves them against the eval baseline, and **stops** for you. Nothing is applied by any code path — autonomously applying changes is designed and documented, not built, and the engine's safety surfaces (eval gate, guardrail, budget config, credentials) are refused outright.
- **Local model concurrency defaults to 1**, which is correct for a unified-memory Mac and leaves
  throughput unused on a machine with large VRAM.
- **A Goal has no spend ceiling by default** (matching the reference agent). Spend is always tracked
  and shown; set `[goal] token_budget` for anything unattended.
- **Attaching widens the blast radius to your real tree.** A managed workspace bounded the damage at
  `projects/<slug>`; attachment deliberately removes that bound. The capability gate, `read_only`
  runs, the effect journal and the gate are what replace it, plus `.agent_state/` and `.git/` being
  off-limits to agent file tools.
- **Subagent dispatch is opt-in** (`executor.subagents_enabled`); `fleet` is a second switch, because
  a fan-out of tool-using children multiplies cost.
- **A provider edit rewrites `credentials.json` in place.** It merges one entry — every other
  provider, model window and policy block is preserved, and the file is written at `0600` — but
  it is a real write to a file you own, so keep it in version control and review the diff.

## Where to go next

- **[USAGE.md](USAGE.md)** — the CLI and app in detail, hiring agents, planning and approving work.
- **[AUTONOMY-MAP.md](AUTONOMY-MAP.md)** — the end-to-end map: mission → goal → run, skills and their
  contracts, typed handoffs, hiring and binding, the four parallelism primitives, gates, memory, and
  the honest list of where it is *not* autonomous.
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — a symptom→cause→fix runbook, and how to debug a run.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the module map and the invariants each layer owns.
- **[OPERATIONS.md](OPERATIONS.md)** — cost, concurrency, health and policy tuning.
- **[EVALUATION.md](EVALUATION.md)** — the context lifecycle, memory, telemetry and the behavioural suite.
- **[DESIGN.md](DESIGN.md)** — the complete design record.

## Licence

The engine is original work. The skills it consumes are MIT-licensed by their author
(Sandeep Kumar Penchala) and remain their property; AgentOrg does not vendor them. Record a pin
with `python3 -m engine.cli skills pin`, and every later run checks the library's commit and
content hashes against it — refusing to start rather than running prompt content that changed.
