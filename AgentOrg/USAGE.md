# Usage Guide

How to drive AgentOrg: the CLI, the organisation, planning and approving work, and the workflow of a
real run.

If you have not installed it yet, start with [README.md](README.md). If something is broken, go to
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Contents

- [The mental model](#the-mental-model)
- [Running the macOS app](#running-the-macos-app)
- [The CLI](#the-cli)
- [Hiring: how an agent is defined](#hiring-how-an-agent-is-defined)
- [Planning: goal to approved graph](#planning-goal-to-approved-graph)
- [Executing a run](#executing-a-run)
- [Reading a run](#reading-a-run)
- [Working with skills](#working-with-skills)
- [Working with models](#working-with-models)
- [Approving and intervening](#approving-and-intervening)
- [Doing this efficiently](#doing-this-efficiently)
- [Common tasks, by intent](#common-tasks-by-intent)

---

## The mental model

Three objects, and the relationships between them are the whole system:

```
Skill  ──bound to──▶  Agent  ──binds to──▶  Node in a graph
(a procedure)         (a person)            (a unit of work)
```

- A **skill** is a procedure from the library. It carries a typed contract — what it consumes, what
  it produces, what "done" means, and how many revision attempts it tolerates.
- An **agent** is a person you hired. It has a name, one or more skills, a model, a budget and a
  mailbox. The same skill can belong to many agents.
- A **node** in a workflow graph names a *capability*, and the org supplies the person. That
  indirection is what lets you run the same graph with Alice on a local model today and Bob on
  Anthropic tomorrow without editing the graph.

A **run** walks the graph. At each node an agent does the work, produces artifacts, and hands off.
When something needs you — an escalation, an expensive hire, a release — the run stops at a gate.

## Running the macOS app

```bash
./scripts/run-macos-app.sh            # build, bundle, launch
./scripts/run-macos-app.sh --debug    # a debug build
./scripts/run-macos-app.sh --build-only
```

Then press **Launch Engine** (⌘⇧L). The app starts the engine itself — you do not run `serve` by hand.

Before the first real run, the engine's preconditions must hold. `doctor` names whatever is missing:

```bash
cd AgentOrg
cp credentials.example.json credentials.json && chmod 600 credentials.json
python3 -m engine.cli doctor
```

The app locates the repository by walking up from its own executable until it finds
`AgentOrg/engine/cli.py`, so it works from `swift run`, a debug build, and the bundled `.app` alike.
If it cannot find an interpreter it opens anyway and says why, rather than failing silently.

| Tab | What you do there |
|---|---|
| **Org** | See the roster and its health; hire agents (they appear in `engine.cli agents` too) |
| **Work** | Watch the run, resolve a gate, and see any swarm's per-item progress |
| **Cost** | Spend, cost-per-success, and the prompt-cache hit rate with what it saved |
| **Context** | How full each agent's window is, and whether a rotation is due |
| **Resources** | Machine pressure and the concurrency ceiling |

## The CLI

Every command supports `--json`, and exit codes are meaningful: **0** success, **1** a check failed,
**2** a usage error. So a script can branch on the result.

```bash
python3 -m engine.cli --help                    # the full surface
python3 -m engine.cli doctor                    # check the environment first
python3 -m engine.cli --config /path/credentials.json --json doctor
```

| Command | Answers |
|---|---|
| `doctor` | *Why will this not start?* Checks seven preconditions and names each failure |
| `skills list` | *What can the org do?* Every skill with its criteria and checklist counts |
| `skills show <name>` | *What will an agent bound to this be held to?* The contract, checklist, research gate |
| `models` | *What can I bind?* Every model with its window and its provenance |
| `models --refresh` | Re-probe providers, bypassing the cache |
| `plan --goal "…"` | *What graph would this goal produce?* A validated manifest, shown not written |
| `plan --goal "…" --out f.yaml` | The same, written as Safe YAML the library's runner can read |
| `org` | *Who do I have?* The roster, teams, and the effective policy matrix |
| `org --goal "…"` | *Can I run this?* The plan, the staffing gaps, and the bindings |
| `delegation` | *What are the hiring rules?* The six invariants and the tier thresholds |
| `run --goal "…"` | *Execute a goal.* Plans, binds, stops at a gate rather than proceeding past one |
| `run --manifest f.yaml` | Execute an existing graph instead of planning a new one |
| `run --goal "…" --dry-run` | Plan and bind, but execute nothing |
| `status --slug s` | *Where is it?* Phase, gate, gaps, instructions, cost and per-node outcomes |
| `decide --slug s --approve` | Resolve a gate: continue past it (`--reject --note "…"` parks instead) |
| `instruct --slug s "…"` | Push guidance into a run (`--constraint` makes it survive every handoff) |
| `chat` | *Talk to a model, or to the org.* A conversational loop; `/help` lists the commands |
| `chat -m "…"` | One shot: send a message and exit, for scripting |
| `chat --agent Sana` | Answer as a named agent, on that agent's model |
| `hire <name> --skill s` | *Create an agent.* Persisted to your root; `run` then uses it |
| `agents` | *Who do I actually have?* The built-ins plus every hire, and where they came from |
| `skills new <name>` | *Author a skill.* Writes an enforceable SOP you can then bind an agent to |
| `pool add "…" --skill s` | *Offer work.* Capability-routed work an agent pulls rather than is pushed |
| `pool list --slug s` | *What is queued?* Counts and tasks by state |
| `pool claim --agent a` | *Have an agent take work.* Refuses work it is not capable of |
| `serve` | *Run the engine for the app.* NDJSON on stdin/stdout; this is what the console launches |
| `fanout "…{{item}}…" --item a --item b` | *Split a job across agents.* Add `--dry-run` to see the expanded prompts without spending |

Global flags: `--config`, `--library`, `--json`.

`serve` is the native console's transport, and its contract is narrow: one JSON command per line on
stdin, one JSON event per line on stdout, **stdout reserved for the protocol** and diagnostics on
stderr. Every command is acknowledged with a `command.ack` carrying its `cmd_id` — including commands
that fail, because an unacknowledged command is indistinguishable from a lost one. Closing stdin ends
the server once its queued commands have been answered.

**The two commands worth running before any real work:**

```bash
python3 -m engine.cli doctor
python3 -m engine.cli org --goal "your actual goal"
```

`doctor` tells you whether the environment is sound. `org --goal` tells you whether the *org* is —
which capabilities the plan needs that nobody holds, and which agent would take each node.

### Reading `doctor` output

```
OK   configuration   credentials.json with 5 providers
OK   skills library  /path/to/Skills at commit 8fbfda61
OK   skill bundles   327 skills parsed with criteria and checklists
OK   providers       built ['lmstudio', 'ollama']; skipped 3
OK   machine         10 cpus, 32.0 GB, ceiling 9 (cpu-bound only)
OK   model catalog   9 models with declared windows
OK   secret hygiene  no key material found in the run-state directory
```

Each line is a check, not a claim. `skipped 3` with the reasons printed below means three providers
are configured but unusable — usually a missing API key, and the fix is named:

```
     skipped: openai: provider 'openai' (openai) has no API key. Set the $OPENAI_API_KEY ...
```

`ceiling 9 (cpu-bound only)` is the concurrency the machine was measured to support. The reason is
printed because "why only four agents?" should be answerable without reading source.

## Hiring: how an agent is defined

An agent needs five things, and the engine refuses to create one without them:

| Field | Why it is required |
|---|---|
| `name` | You will see it in logs, the roster and handoffs; anonymous actors are unmanageable |
| `skills` | An agent with no capability cannot be routed work |
| `provider` + `model` | Without both, it cannot be called |
| `context_window` | The session projection cannot size a prompt without it, so a binding without one is refused |

That last refusal is deliberate and catches a real class of bug: assume a window and you overflow in
production. The catalogue tells you which models have a *probed* or *declared* window:

```bash
python3 -m engine.cli models
```

```
provider     model                               window     out loc    source
anthropic    claude-sonnet-4-20250514            200000    8192 cloud  declared
ollama       qwen2.5-coder:7b                     32768    8192 local  declared
```

`source` is the honesty column:

| `source` | Means | Bindable? |
|---|---|---|
| `probed` | The provider reported it (Ollama via `/api/show`) | yes |
| `declared` | Your `models.known` table or a provider alias | yes |
| `assumed` | Unknown — we did not find out | **no** |

The default company gives you the seven roles a build needs, with the Owner as a human agent:

```bash
python3 -m engine.cli org
```

```
AgentOrg: 8 agents across 4 teams
  Platform (lead: —)
    Arjun          System Architect     idle        ollama/qwen2.5-coder:7b
    Alice          Backend Developer    idle        ollama/qwen2.5-coder:7b
  Quality (lead: Sana)
    Sana           Code Reviewer        idle        lmstudio/llama3.1:8b
  ...
```

Note that Sana is on a **different model** than Alice. That is not decoration: the library's
`verification-independence-engineer` requires a verifier that differs from the producer, and the
default company arranges it so you do not have to know to.

### Hiring from Python

```python
from engine.org import default_company, AgentSpec, AgentLevel, Budget

org = default_company(provider="ollama", model="qwen2.5-coder:7b", context_window=32768)

# A second backend developer on a different model, with its own budget.
org.hire(AgentSpec(
    id="ag_bob", name="Bob", title="Backend Developer",
    skills=["backend-developer"], provider="anthropic",
    model="claude-sonnet-4-20250514", context_window=200000,
    level=AgentLevel.STAFF, budget=Budget(allocated_usd=5.0),
))
org.save(".agent_state/org.json")
```

Names must be unique — two agents called Alice would make the roster and every log line ambiguous.
Renaming keeps the id, so history stays attached.

## Planning: goal to approved graph

A plan is a *proposal*. You approve it; the engine does not start from a goal alone.

```bash
python3 -m engine.cli plan --goal "Build a booking API with auth and payments"
```

```
Goal: Build a booking API with auth and payments
Workflow: build-a-booking-api-with-auth-and-payments  (validated: yes)

Sequence:
  pm  (skill: product-manager) -> [product-spec]
  architect  (skill: system-architect) -> [architecture]
  api  (skill: api-designer) -> [api-spec]
  developer  (skill: backend-developer) -> [change]
  reviewer  (skill: code-reviewer) -> [review-report]
  ...

Loops (bounded, with an exit condition and escalation):
  review-fix-loop: reviewer -> qa -> security -> developer
    exit when reviewer.verdict == pass  max 3 iterations  escalate to human-gate

Gates:
  human-gate  kind=human  Owner approval: the release ...
```

Four things to check in a plan:

1. **`validated: yes`** — the library's own validator accepted it. The planner refuses to emit a plan
   that fails, falling back to a leaner shape and saying so.
2. **The loop is bounded** — `exit when`, `max iterations`, and an `escalate to` target. An unbounded
   loop is a run that never finishes.
3. **The gate is reachable** — a run must have somewhere to stop, and only you hold terminal
   authority.
4. **The sequence matches the goal** — goal keywords add specialists (infra adds a devops engineer, a
   macOS goal adds a macOS developer).

Write it when you are happy:

```bash
python3 -m engine.cli plan --goal "..." --slug booking --out booking.yaml
```

The file is emitted in the library's **Safe YAML Subset**, which is narrower than YAML — no flow
maps, no anchors. That is not an accident: the library's runner parses that subset, so a manifest
that validates in memory but is unreadable from disk would be useless.

## Executing a run

`plan` shows you a graph; `run` executes one. It plans (or adopts a manifest), binds each node to an
agent, and stops at the first gate rather than proceeding past it.

```bash
# Plan and execute a goal
python3 -m engine.cli run --goal "Build a booking API with auth and payments" --slug booking

# Bind and print the plan without executing anything
python3 -m engine.cli run --goal "…" --slug booking --dry-run

# Execute a manifest you wrote with `plan --out`
python3 -m engine.cli run --manifest booking.yaml
```

If a node needs a capability nobody holds, `run` prints the **staffing gaps** and names them rather
than failing mid-graph — hire the agent, or amend the plan, then re-run.

When the run reaches a gate it prints the gate and how to resolve it:

```
  GATE: human-gate — Owner approval: the release ...
    requires: owner-approval
    present : (none)

  Decide with:  engine.cli decide --slug booking --approve|--reject --note ...
```

```bash
python3 -m engine.cli status  --slug booking              # where is it, and what has it cost?
python3 -m engine.cli decide  --slug booking --approve    # continue past the gate
python3 -m engine.cli decide  --slug booking --reject --note "auth spec is missing rate limits"
python3 -m engine.cli instruct --slug booking "use UTC everywhere"
python3 -m engine.cli instruct --slug booking --constraint "never log a full card number"
```

`--reject` records the note so agents do not re-attempt identically, and `--constraint` makes the
text non-negotiable: the AR-04 machinery then preserves it verbatim across every later compaction and
rotation for the rest of the run.

## Attaching your own project

By default a run works in a *managed* project the engine owns, under
`AgentOrg/projects/<slug>/`. To work on a repository you already have, attach it:

```bash
python3 -m engine.cli run --project ~/code/my-app --goal "Add cursor pagination to /v1/items"
python3 -m engine.cli status --project ~/code/my-app
```

`--project` is on every command that reaches the filesystem, and it makes `--slug` optional — the
folder names the project itself. What changes:

| | Managed | Attached |
|---|---|---|
| The agents read/write | `projects/<slug>/` (engine-owned) | **your folder** |
| Engine state lives in | `projects/<slug>/.agent_state/` | `<folder>/.agent_state/` |
| `docs/`, `src/` created? | yes | **never** — the engine will not add directories to your tree |
| `.agent_state/` reachable by agents? | n/a | **no** — not readable or writable |
| `.git/` writable by agents? | n/a | **no** |

Add `.agent_state/` to your `.gitignore`; the engine reports it as missing rather than editing the file
for you. If the folder already has state from a previous run, it is resumed — but see *Goals* below for
why that never resumes spending.

In the app, **Open Project…** (⌘O, or the toolbar button) does the same thing. It stops the engine so
the run checkpoints, then relaunches it attached — a live re-root would split a run across two projects.

## Goals: keeping a run going

A graph that finishes is a run that has stopped. A **goal** is an objective that keeps being worked on
until the agent says it is done or blocked — across turns, across a restart, across a pause.

```bash
python3 -m engine.cli goal set "Add cursor pagination to /v1/items with tests" --project ~/code/my-app
python3 -m engine.cli goal status --project ~/code/my-app     # state, rounds, tokens, cost
python3 -m engine.cli goal pause  --project ~/code/my-app
python3 -m engine.cli goal resume --project ~/code/my-app     # grants a fresh budget slice
python3 -m engine.cli goal clear  --project ~/code/my-app
```

Three things to know, because they are the whole safety argument:

1. **A goal has no spend ceiling by default.** It continues until completion, a genuine blocker, a
   gate, or you stop it. Spend is *always* tracked and shown. For anything unattended, set a ceiling:

   ```toml
   [goal]
   token_budget = 20000000   # tokens per slice; 0 = off (the default)
   ```

2. **A goal restored from disk comes back disarmed.** Restart the engine and you will see
   `paused (restored)`. It cannot re-arm itself, so a crash or a reboot can never resume an unattended
   loop on its own. `goal resume` is the only thing that continues it.

3. **Completion is the agent's call.** The agent reports it through `update_goal(complete|blocked)`;
   no percentage or evaluator decides. A gate still parks the run — arming a goal authorises
   *continuing past work finishing*, not *deciding a question you asked to decide*.

## Isolated subagents

A swarm (N agents vote) and a fan-out (N agents split one job) are node-level and one-shot. With
subagents enabled, an agent can also dispatch work into **its own context** and page the result back:

```toml
[executor]
subagents_enabled = true        # off by default: a fan-out of tool-using children multiplies cost
subagent_fleet_enabled = false  # `fleet` (N at once) is a second, deliberate switch
subagent_max_parallel = 4
```

- `task` runs **one** child in an isolated context and returns a short reference.
- `fleet` runs **many**, one per task, bounded in parallel.
- `read_subagent_result(child_id, offset_bytes, limit_bytes)` pages the child's transcript, and always
  reports whether more remains — so the parent reads the 8 KB it needs rather than all of it or none.

A child shares the parent's **pinned prefix** (so isolation does not break prompt caching) while
getting its own conversation log. Transcripts live under
`<workspace>/.agent_state/children/<run_id>/` and are append-only; a child that reached
`needs_review` can be continued from its transcript.

The console's **Work** panel shows the subagent tree for a run, and *Read* opens a paged transcript
viewer — the same byte-addressed read the agent's tool performs.

## Adding a provider (in the app)

The **Providers** tab adds, tests and removes model endpoints without hand-editing JSON. The order
matters and the UI enforces it: **Test and fetch models** runs first, so a wrong URL or a bad key is
caught *before* anything is written to `credentials.json`.

1. Fill in an **id** (e.g. `groq`), a **kind** (OpenAI-compatible, Anthropic, or Ollama) and the
   **base_url**. That is the **base**, not the endpoint: for OpenAI-compatible hosts the part ending
   in `/v1` — `https://api.groq.com/openai/v1`, `http://localhost:1234/v1`, and for Ollama Cloud
   `https://ollama.com/v1`.

   If you paste the full endpoint instead (`https://ollama.com/v1/chat/completions`, as Ollama's docs
   show it), the engine reduces it to its base and tells you — a base is the part *before* the call,
   and appending `/models` to a full endpoint probes a path that does not exist. The form then holds
   the corrected value, so what you see is what is saved.
2. Give it a key, either as a **variable name** (`GROQ_API_KEY`) or a literal. Prefer the variable: it
   is not read into logs or traces.
3. If the endpoint wants its own header — a gateway routing key, an organisation id, an `X-Api-Key`
   instead of a Bearer token — add it under **Extra headers** as `Name: value`.
4. Press **Test and fetch models**. You get *Connected — N models* with the names, or a concrete reason
   it failed (unreachable, refused, no models). Nothing is saved yet.
5. Press **Save provider**. The engine merges *just this entry* into `credentials.json` (mode `0600`),
   preserving every other provider, the model windows and the policy block, then reloads the config so
   the next run sees it.

The same thing from the CLI:

```bash
python3 -m engine.cli models --refresh          # what every provider currently offers
```

A key is never sent back to the window: editing a provider starts with the field empty, and an
untouched save leaves the stored value alone rather than blanking it.

## Hiring and editing agents (in the app)

The **People** tab is the roster — the built-in company, the Owner, and the agents you hired.

- **Hire** picks a name, a **skill from the library's 327** (a real picker, not a text field, because a
  mistyped skill is refused and you would have no way to know the right spelling), a level, and the
  provider/model. The model list is the one that provider actually reported.
- **Edit** changes the model, level, team or title. It **keeps the agent's id**, which is what the
  mailbox, session history, ledger entries and health record are keyed on — so "I only changed the
  model" does not look like a brand-new employee with no past.
- **Retire** removes the agent. The Owner cannot be retired: it holds terminal authority.
- A name is only re-sent when you change it, so a model-only edit cannot fail because another agent
  already holds that name.

From the CLI the same operations are:

```bash
python3 -m engine.cli agents                       # the roster, with who is hired vs built-in
python3 -m engine.cli hire --name Nadia --skill code-reviewer --model ollama/qwen2.5-coder:7b
python3 -m engine.cli skills list | head           # the skill names to choose from
```

Hires are written to `<project>/.agentorg/roster.json`, and the built-in company is deliberately
*not* frozen into that file — otherwise a later change to the defaults would be silently shadowed by a
stale snapshot.

## Reading a run

A run writes everything under `projects/<slug>/.agent_state/`:

```
projects/booking/
├── docs/           prd.md · api_spec.md
├── src/            app.py
└── .agent_state/
    ├── org.json                 the roster: names, skills, models, reporting lines
    ├── run_state.json           the checkpoint a resume reads
    ├── trace.jsonl              every event, in order
    ├── review_feedback.json     the latest rejection dossier
    ├── effects.jsonl            the idempotency journal (no side effect applied twice)
    ├── agents/<id>/mailbox.jsonl
    ├── sessions/…               archived session transcripts
    └── telemetry/spans.jsonl    OTel-shaped spans
```

**The three files to look at when something seems wrong:**

| File | Tells you |
|---|---|
| `trace.jsonl` | What happened, in order — every event with its sequence number |
| `run_state.json` | Where the run is: current node, iteration, budget spent, open questions |
| `review_feedback.json` | Why a review was rejected, with severities and file:line |

`trace.jsonl` is NDJSON, so ordinary tools work on it:

```bash
tail -20 .agent_state/trace.jsonl                                  # recent events
grep '"type":"review.rejected"' .agent_state/trace.jsonl           # every rejection
python3 -c "import json,sys; [print(json.loads(l)['type']) for l in sys.stdin]" \
  < .agent_state/trace.jsonl | sort | uniq -c | sort -rn           # event histogram
```

## Working with skills

Inspect what an agent is actually held to before you wonder why it behaved a certain way:

```bash
python3 -m engine.cli skills show code-reviewer
```

```
## Contract
  inputs   : change
  outputs  : review-report
  evidence : required
  escalate : human-gate

## Completion criteria (3)
  1. Findings reference concrete files and lines
  2. Verdict states pass or changes_requested with rationale
  3. Severity grading matches the six-dimension severity model

## Checklist (14) — the prompt requires every id
  [CR1] Six-dimension review completed: security, performance, code quality, ...
  ...
```

Two things to notice:

- **`evidence: required`** means a criterion with no evidence is an *open item*, not a pass. That is
  why a review cannot simply assert success.
- **The checklist ids are named in the prompt.** `CR1`…`CR14` are extracted and the agent must report
  each as PASS, FAIL or N/A with evidence — which is what makes a review checkable rather than
  prose.

An agent with no checklist still gets one. Where the library supplies no bracketed id, positional ids
are assigned (`PC1`, `PC2`, …), because an item nobody is asked about is an item nobody reports.

## Working with models

```bash
python3 -m engine.cli models                 # cached view, fast
python3 -m engine.cli models --refresh       # re-probe providers
python3 -m engine.cli models --json          # for a tool
```

Two behaviours worth knowing:

- **A provider that is down still lists its configured models.** The app must open on a machine where
  Ollama is not running, so a failed probe becomes a status (`down`, `misconfigured`) rather than an
  empty list.
- **Each provider lists only models it can serve.** `gpt-4o` appears under OpenAI, not under
  Anthropic. Offering a model a provider cannot serve wastes your time and then fails at call time.

## Approving and intervening

You hold terminal authority. During a run:

| You want to | Effect |
|---|---|
| **Approve** a gate | The run continues past it |
| **Reject** with a reason | The work returns to the agent, with your reason in its context |
| **Instruct** | Guidance is injected into the running agent's context without stopping the run |
| **Inject a constraint** | A new `non_negotiable` constraint, which then survives every later handoff |
| **Reassign** | Move a task to a different agent — the manual form of the router's job |
| **Take over** | Act as the agent yourself, then hand back or forward |
| **Force a route** | Decide when the router finds no confident match |
| **Abort / park** | Stop a run, keeping the checkpoint for a resume |

A human action is recorded the same way as an automated one — same contract, same ledger, same audit
trail — because the Owner is modelled as an agent, not as a special case. So "who changed this?" has a
single answer regardless of whether it was you or Sana.

## Doing this efficiently

Practical advice, in rough order of impact.

**Start local, escalate selectively.** A local model costs nothing and is private. Bind the *builders*
to Ollama and the *reviewers* to a cloud model: review is where a stronger model pays for itself, and
it also gives you genuine independence.

**Respect the local-model concurrency of 1.** On Apple Silicon the GPU and CPU share one memory pool,
so loading two models at once causes system-wide swap — the whole Mac gets slow, not just the app.
This is the single most important setting on a laptop. It is the default; leave it.

**Watch cost-per-success, not cost.** A cheap run that fails and retries costs more than a dear one
that works. `cost_per_success_usd` is the metric that matters, and an unmeasured figure is shown as
unknown rather than free.

**Keep the review loop at 3 attempts.** The convergence window means a loop that produces no new
information stops early rather than burning its full budget. Raising the cap does not raise quality —
it delays the escalation that would actually help.

**Let the router propose when it is unsure.** The default policy is `auto` for routine handoffs and
`confirm` for escalation and conflict. That is the setting most worth keeping: it means the org moves
by itself on the routine path and asks you at exactly the points where being wrong is expensive.

**Read the staffing gap before the run.** `org --goal` costs nothing and prevents the most annoying
failure mode — a graph that runs three nodes and then stops because nobody holds a capability.

**Use `--json` when wiring things up.** Every read command supports it, so the same information that
you read in a table can drive a script without parsing.

## Common tasks, by intent

**"I want the org to review a change it made."**
```bash
python3 -m engine.cli plan --goal "Review the auth change in src/app.py" --slug review
```
The planner includes a reviewer node, binds it to a different agent than the producer, and bounds the
fix loop.

**"I want to know why the reviewer rejected twice."**
```bash
cat projects/<slug>/.agent_state/review_feedback.json
grep '"type":"review.rejected"' projects/<slug>/.agent_state/trace.jsonl
```

**"I want to add a specialist without restarting."**
```python
org.hire(AgentSpec(id="ag_dana", name="Dana", title="DevOps Engineer",
                   skills=["devops-engineer"], provider="ollama",
                   model="qwen2.5-coder:7b", context_window=32768))
org.save(".agent_state/org.json")
```

**"I want to know what this skill will make an agent do."**
```bash
python3 -m engine.cli skills show <skill-name>
```

**"I want to change how much the org does on its own."**
See [OPERATIONS.md](OPERATIONS.md#policy-tuning) — the policy is per route class, and the safety floor
protects escalation and conflict.

**"I want to find out why an agent keeps failing."**
```bash
python3 -m engine.cli org --json | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['roster'], indent=2))"
```
Then see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#debugging-an-agent).
