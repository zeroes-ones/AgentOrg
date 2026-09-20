# Operations

How to tune the org: policy, cost, concurrency, health and delegation. Each section states the
default, what raising or lowering it does, and the failure it prevents.

For diagnosing a specific problem, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Contents

- [The knobs at a glance](#the-knobs-at-a-glance)
- [Policy tuning](#policy-tuning)
- [Cost control](#cost-control)
- [Concurrency and provider limits](#concurrency-and-provider-limits)
- [Context and rotation](#context-and-rotation)
- [Health and SLOs](#health-and-slos)
- [Delegation and hiring](#delegation-and-hiring)
- [Reading the metrics](#reading-the-metrics)
- [A configuration that works well](#a-configuration-that-works-well)

---

## The knobs at a glance

Everything lives in `credentials.json`. The defaults are chosen so that a first run is *safe* rather
than fastest: local concurrency 1, escalation gated, a 3-attempt loop, a $25 run ceiling.

| Block | Setting | Default | Raising it | Lowering it |
|---|---|---|---|---|
| `policy` | `default_autonomy.R-*` | auto/confirm | `confirm` asks more | `auto` moves faster, less oversight |
| `policy` | `allow_autonomous_escalation` | `false` | **removes the safety floor** | keep as-is |
| `policy.router` | `threshold` | 0.62 | routes less often, asks more | routes on weaker matches |
| `policy.router` | `margin` | 0.15 | asks when the top two are close | picks a winner in a near-tie |
| `budget` | `run_max_usd` | 25.0 | longer runs | earlier stop |
| `budget` | `run_max_tokens` | 4M | — | tighter token bound |
| `context` | `compact_at` | 0.70 | compacts later, cheaper, riskier | compacts earlier, safer |
| `context` | `max_rotations_per_node` | 4 | longer nodes | escalates sooner |
| `concurrency` | `per_provider_limits` | local 1, cloud 3–4 | more parallel | more serial |
| `concurrency` | `queue_max_depth` | 64 | more work buffered | sheds sooner |
| `concurrency` | `heartbeat_s` | 30 | slower hang detection | faster detection, more false alarms |
| `concurrency` | `stall_timeout_s` | 1800 | a slow local model gets room to finish a reply | a wedge is reported sooner |
| `health` | `min_samples` | 5 | more patience with a new hire | judges sooner, noisier |
| `health` | `healthy_at` / `degraded_at` | 0.80 / 0.50 | more agents degraded | more quarantined |
| `delegation` | `max_depth` | 3 | longer chains, more compounding | flatter |
| `delegation` | `budget_share_max` | 0.50 | children may take more | children are cheaper |
| `delegation` | `span_of_control` | 5 | wider trees | flatter |
| `executor` | `max_output_tokens` | 32768 | longer artifacts survive whole | cheaper, but a long artifact truncates |
| `executor` | `max_tool_steps` | 12 | more investigation before answering | answers sooner |
| `goal` | `auto_pass_auto_gates` | `true` | — | `false` makes every goal wait at a gate |
| `goal` | `auto_hire_missing` | `true` | — | `false` reports a staffing gap instead |
| `goal` | `persist_auto_hires` | `false` | auto-created helpers survive on the roster | helpers die with the run |

### `executor.max_output_tokens` is the one that bites hardest

It caps **one model reply**. The default was hardcoded at 4096, which silently truncated any long
artifact — a PRD, a design doc, a large diff — *before* the model could emit its machine-readable
trailer. The node then failed its own completion contract with *"declared criteria not covered"*,
which points at the model while the real cause is this ceiling. Measured on a 1M-token model writing a
PRD: the reply needed more than 16384 tokens and was still being cut off mid-JSON.

Two things now bound it, so raising it is safe: the model's own declared `max_output` when it has one,
and never more than **half the context window** (the other half is the prompt). If a run reports
`finish_reason: length` in the trace, or a node fails with "no parsable trailer", this is the first
knob to check.

## Policy tuning

The policy decides how much the org does before asking you. It is **per route class**, which is the
point: automate the routine path, gate the risky one.

### The six route classes

| Class | When it fires | Default | Why that default |
|---|---|---|---|
| `R-CONTRACT` | A normal forward handoff between phases | `auto` | Routine; asking about each handoff would make the org unusable |
| `R-REWORK` | Reviewer → developer revision | `auto` | Already guarded by the attempt cap; the loop knows when to stop |
| `R-DELEGATE` | An agent hires a helper or peer | `auto` | Bounded by the six invariants; a helper dies with its task |
| `R-ESCALATE` | Exhaustion, retry cap, more than three open questions | `confirm` | This is where being wrong is expensive |
| `R-CONFLICT` | Two agents contradict | `confirm` | A silent override is the worst outcome |
| `R-MATCH-FAIL` | The router finds no confident match | `confirm` | Guessing a route is worse than asking for one |

### The safety floor

`R-ESCALATE` and `R-CONFLICT` cannot resolve below `confirm`, at any layer, unless you set:

```json
{ "policy": { "allow_autonomous_escalation": true } }
```

The floor exists because a single per-agent setting should not be able to remove every human gate.
Think about what it means before you cross it: with it set, an org that decides to escalate *acts on
that decision*.

### Layered resolution

More specific wins, in this order:

```
org  →  team  →  agent  →  run  →  task
```

So "the whole org is autonomous on routine work, but the Security team confirms everything" is
expressible without a global change:

```python
resolver.set("org", "", RouteClass.CONTRACT, "auto")
resolver.set("team", "Security", RouteClass.CONTRACT, "confirm")
```

A rejected change is not half-applied: if a `set` fails the safety floor, the previous value stands.

### Seeing the effective policy

```bash
python3 -m engine.cli org
```

```
Route class          level     layer
R-CONTRACT           auto      org
R-ESCALATE           confirm   org
```

The `layer` column answers "why is it doing that?" — it names which scope decided.

### When to loosen

- **`R-REWORK` → `auto` with a long loop** is safe: the convergence window stops it early when passes
  produce no new information.
- **`R-DELEGATE` → `auto`** is safe: the invariants (depth, cycle, budget, privilege, context,
  lineage) still apply, and a helper that exceeds them is refused.

### When to tighten

- **`R-CONTRACT` → `confirm`** for a first run in an unfamiliar domain, to watch the handoffs.
- **`R-DELEGATE` → `confirm`** when budget is the constraint: every hire then costs you a decision.
- **Any class → `manual`** to make the org wait for you to initiate. Useful for a review-only run.

## Cost control

### Three ceilings, in order of authority

| Ceiling | Where | Enforced |
|---|---|---|
| Per agent | `AgentSpec.budget` | Checked at admission; an exhausted agent is not given work |
| Per run | `budget.run_max_usd` / `run_max_tokens` | Checked **before** every call by the gateway |
| Per day | `budget.org_max_usd_daily` | Configured; intended as the org-level backstop |

The run ceiling is the one that actually stops a runaway, and it stops *before* spending. A post-hoc
check would report the overspend after it happened.

### Cost is measured, not guessed

Four labels, and the distinction is not cosmetic:

| Label | Source | Rendered |
|---|---|---|
| `measured` | The provider reported it | exact |
| `estimated` | Measured tokens × a price table | approximate |
| `free` | A local model — a *known* zero | `$0.00` |
| `unknown` | No usage reported, no price known | **unknown** |

An unmeasured call is never shown as `$0.00`. That is the difference between "we spent nothing" and
"we have no idea", and conflating them is how a cost dashboard lies.

### The metric that matters

**Cost per success**, not cost. A cheap run that fails and retries costs more than a dear one that
works. The library uses the same vocabulary (`cost_per_success_usd`, `cost_unreported_runs`) so its
own `skill-sli-report.py` stays a valid cross-check on these numbers.

### Practical levers, most effective first

1. **Bind routine nodes to a local model.** Building is high-volume and low-judgment; review is the
   opposite. A local model costs nothing and is private.
2. **Use a stronger model only for review.** That is also where it buys independence.
3. **Keep the review loop at 3 attempts.** Raising it does not raise quality; it delays the
   escalation that would help.
4. **Prefer the compiled skill form.** A raw `SKILL.md` is ~18k tokens; the compiled XML is ~2.2k.
   Over a four-node pipeline that is the difference between a ~9k and a ~72k prompt.
5. **Trust the convergence window.** A loop producing identical passes stops early rather than
   burning its full budget.

## Concurrency and provider limits

### What the ceiling is derived from

`resources.py` measures the machine and derives a ceiling with a stated reason. The real constraint is
rarely CPU:

- **Local model memory.** On Apple Silicon, GPU and CPU share one pool, so two resident models cause
  system-wide swap — the whole Mac slows, not just the app. This is why a local provider defaults to a
  concurrency of **1**.
- **Provider rate limits.** Exceeding them produces 429s, and the limiter shrinks adaptively rather
  than storming.
- **The budget.** The one limit that cannot be retried away.

```bash
python3 -m engine.cli doctor | grep machine
# OK   machine   10 cpus, 32.0 GB, ceiling 9 (cpu-bound only)
```

The reason string is printed so "why only four agents?" is answerable. With a local model in play the
ceiling drops — on a 32 GB machine to about 4, because ~6.5 GiB per 7B model plus a reserve for the OS
is the real arithmetic.

### Three tiers of concurrency

| Tier | Mechanism | Default |
|---|---|---|
| Global | A ceiling from measured capacity | cpu_count − 1, or memory-bound when local models are in use |
| Per provider | A semaphore per provider | 1 local, 3–4 cloud |
| Per agent | Single-flight | 1 — one agent, one task |

Single-flight is not a limit to raise: two concurrent tasks for one agent would interleave its context
and corrupt both.

### Backpressure

On a 429 the provider's limit shrinks by one and recovers one step at a time. Shrinking rather than
failing is what turns a rate limit into throttling; recovering one step rather than jumping back is
what stops the 429 from reproducing.

```python
scheduler.backpressure_state()   # per-provider limit, inflight, throttled
scheduler.on_rate_limited("openai", retry_after_s=12)
scheduler.on_provider_recovered("openai")
```

### The queue is bounded and sheds explicitly

When the queue is full the scheduler drops the **lowest-priority** work with a recorded reason rather
than growing memory:

| Priority | Applied to |
|---|---|
| `GATE_BLOCKED` | Work blocking a gate — an idle human is worse than an idle worker |
| `REWORK` | A revision pass, already mid-flight |
| `ACTIVE_RUN` | Normal work in a live run |
| `NEW_WORK` | Starting something new, which cannot unblock anything |

```python
scheduler.shed_log()   # what was dropped, and why — never silent
```

### Liveness

The watchdog turns silence into an escalation rather than an indefinite wait:

| State | Silence | Action |
|---|---|---|
| `slow` | 1–2 heartbeats | watch |
| `warned` | + grace | SIGTERM after the grace period |
| `wedged` | beyond that | SIGKILL, then resume from the checkpoint |

A wedged slot is reclaimed so one hang does not permanently reduce the ceiling. Preemption is at a
turn boundary, never mid-generation — killing mid-generation corrupts state and wastes the tokens
already spent.

## Context and rotation

The session lifecycle is Phase 5, but the thresholds live in config and are worth understanding now
because they decide how much of a model's window you can use.

### The compaction ladder

| Band | Saturation | Action |
|---|---|---|
| HEALTHY | < 70% | nothing |
| WARNING | 70–84% | find redundancy, score staleness, prepare evictions |
| CRITICAL | 85–94% | evict tier 3, compress history, check for unproductive loops |
| OVERFLOW | ≥ 95% | tier 1 only, emergency compression |

**70% is not arbitrary.** The library's research is that a model attends effectively to roughly 70% of
its window; beyond that, adding context dilutes rather than informs. Compaction is about attention
quality, not capacity — which is why it is proactive rather than reactive.

### Rotation triggers

| Trigger | Condition | Why |
|---|---|---|
| Capacity | Still ≥ 85% after full compaction | In-session compaction is exhausted |
| Attention decay | `e^(−0.1·turns) < 0.30` (≈ turn 12) | A rule read at turn 1 is only ~60% as likely to be followed by turn 15 |
| Phase change | The node advances INTAKE→EXECUTE→VERIFY→DECIDE | A natural checkpoint; carried research becomes noise |

A rotation is *not* only about space. It re-pins decaying guardrails to the primacy zone, so a rotated
session is safer than a bloated one.

### Invariants that cannot be tuned away

- `NEVER` / `MUST NOT` constraints and every `non_negotiable` constraint are preserved **verbatim**. A
  post-pass counts them; if the count drops, the compaction is reverted.
- Compaction and rotation happen only at turn boundaries, never during active generation.
- If a *fresh* session immediately overflows, the run does not rotate again — that means the
  irreducible content is too large, and the fix is a lower skill tier or a bigger window, not another
  rotation.

## Health and SLOs

### Health acts automatically, on evidence

| State | Means | Behaviour |
|---|---|---|
| `healthy` | Composite ≥ 0.80 | Routed normally |
| `degraded` | Composite 0.50–0.80 | Deprioritised but still usable |
| `quarantined` | Composite < 0.50, or a hard trigger | Removed from the pool, Owner notified |

Three safeguards keep it from being reckless:

1. **The minimum-sample guard (5).** A new hire is never quarantined on its first bad task.
2. **Hard triggers override the score.** Three consecutive contract breaches, or every task tripping a
   guardrail, quarantines immediately regardless of a healthy average. A good average is exactly how a
   severe fault hides.
3. **Recovery needs a probe.** A quarantined agent returns by passing its skill's golden cases — and
   lands in `degraded`, not healthy, because trust is rebuilt with real work.

### Tuning the sensitivity

| Setting | Effect |
|---|---|
| `min_samples` ↑ | More patience with a new hire; a bad agent lingers |
| `healthy_at` ↑ | More agents degraded; less risk |
| `degraded_at` ↓ | More agents quarantined; a stricter org |
| `hard_trigger_contract_breaches` | The consecutive-breach count that overrides the score |

### When every capable agent is quarantined

The org must not spin. `monitor.no_capable_agent(...)` returns True, and the correct response is to
escalate rather than route to nobody. In practice: hire another agent with that skill, restore one with
evidence, or raise the escalation to a gate you can clear.

### SLOs and burn rate

Alert on **burn rate**, not a raw error rate. A raw threshold fires constantly and teaches people to
ignore it.

| Band | Rate | Meaning |
|---|---|---|
| INFO | < 2× | Within budget |
| WARNING | ≥ 2× | Above the sustainable rate; investigate in hours |
| CRITICAL | ≥ 14.4× | A month's budget would go in about two days; page now |

A budget reduced to zero is raised to critical even at a low rate, because there is no margin left for
the next failure. Alerts are rate-limited per objective, so a condition that persists does not produce
an alert per evaluation.

## Delegation and hiring

### The reuse-first ladder

Spawning is the **last** rung. An agent must first show that no existing agent could do the work:

1. Reuse an existing agent with the skill.
2. Try an agent with a wider capability.
3. Then — and only then — request a helper or a specialist.

A request without ladder evidence is auto-rejected. This is what stops the roster growing for work an
idle colleague could have done, which is how token-per-task climbs silently.

### What a requisition must state

| Field | Why |
|---|---|
| `capability_gap.needed` | The specific capabilities, not a wish for help |
| `capability_gap.why_existing_insufficient` | Why nothing existing works |
| `expected_outcome` | The outcome, not the request — an outcome can be judged |
| `ladder_evidence` | Proof reuse was tried, with each rejection's reason |
| Five context elements | Otherwise the delegate re-discovers the problem from scratch |

### The six invariants

| | Invariant | Prevents |
|---|---|---|
| S1 | Depth ≤ 3 | Unbounded chains — hallucination compounds ~15–20% per hop |
| S2 | No cycle in the active chain | A→B→A burning budget without progress |
| S3 | Budget carved from the parent's remainder | A tree outspending its root |
| S4 | Least-privilege capabilities | A child inheriting the parent's authority |
| S5 | Five context elements | A delegate starting from zero |
| S6 | Lineage recorded | An orphaned agent nobody can attribute |

### Approval tiers

| Tier | Conditions | Decision |
|---|---|---|
| T0 | Helper · existing skill · ≤ 40k tokens · read-only or no extra caps | Auto |
| T1 | Helper · ≤ 80k tokens | Auto, notified |
| T2 | Specialist · or > 80k tokens · or above the USD threshold | **Owner gate** |
| T3 | Elevated capabilities (`write:`, `deploy:`, `exec:`, `admin:`) · or > 200k tokens | **Owner gate, explicit** |

T0 makes a temporary, reversed-by-nature hire free of ceremony. T3 makes a `deploy:` capability a
deliberate decision, because that is the one that can affect something outside the run.

### Letting agents act on the Mac

Everything above confines an agent to a project. `[system]` is the other question — may it read the
battery, use the clipboard, take a screenshot, change the volume, open an application, run an
AppleScript. It is **off by default**, and turning it on grants nothing: it only makes the tools
offerable.

```toml
[system]
enabled = true                 # the operator's switch; grants nothing by itself
allow_apps = ["Safari"]        # applications `open_app` may launch. Empty = none
allow_automation = ["mail"]    # AppleScript handler prefixes. Empty = none
screenshot_dir = ""            # default: inside the workspace
max_seconds = 20               # a command parked on a dialog is stopped, not awaited
allow_full_access = false      # see below
```

Then grant per agent — the capability is the scope, and a sibling grant never implies another:

```bash
engine.cli hire SysOp --skill backend-developer \
  --capability "read:*" --capability "system:state" --capability "system:screenshot"
```

| Grant | Reaches |
|---|---|
| `system:state` | battery, disk, uptime, running apps — read-only |
| `system:clipboard` | read and write the clipboard |
| `system:screenshot` | capture into the workspace |
| `system:media` | volume, mute |
| `system:open` | launch an application named in `allow_apps` |
| `system:automation` | run an AppleScript handler matching `allow_automation` |

**Two gates, not one.** The allowlist is the *scope* of the grant, and a state-changing action
additionally needs the Owner's consent **once per agent per tool**, recorded in the decision ledger.
An agent cannot grant itself consent — `grant_consent` refuses a `by` that names an agent, which makes
that a property of the code rather than a convention. Granting consent does not widen an allowlist;
they are independent answers.

#### Full access

Both reference agents ship a mode where the agent simply is not interrupted — Reasonix's
`--permission-mode bypassPermissions` with `sandbox = false`, Kimi's `--auto` ("never interrupts you;
everything runs and is decided automatically"). `system.allow_full_access` is that mode:

```toml
[system]
enabled = true
allow_full_access = true   # no allowlists, no consent prompt
```

It is right for a machine you have handed over — a build box, a scratch VM, a laptop you are not
using — and wrong for the one you are working on. What it changes, and nothing more:

- `open_app` may launch any installed application, not only those in `allow_apps`
- `run_automation` may run any AppleScript, not only allowlisted handlers
- a state-changing action no longer waits for consent

What it does **not** change: `enabled` is still required, the agent still needs the relevant
`system:*` grant, every call is still bounded by `max_seconds` and `max_output_bytes`, and every
action is still recorded. Full access is permission to *act*, never permission to stop being audited
— a mode that also silenced the record would make the ledger useless exactly where it matters most.

> **It is a second switch, not the default, deliberately.** `allow_apps` answers "which applications
> may this agent start"; `allow_full_access` answers "do I still want to be consulted at all". A person
> who wants the first does not thereby want the second, and a single switch for both would leave them
> no way to say so.

### Anti-sprawl

`token-per-completed-task` per agent, tracked as the library's own anti-leak metric. Growth without a
topology change means a delegation leak or context bloat. A parent also cannot hold more than
`span_of_control` active reports — beyond that it must route to a peer rather than widen the tree.

## Reading the metrics

### The five questions the console will answer

Each view answers exactly one question, because a dashboard that needs interpreting has failed:

| View | Question |
|---|---|
| Org health | *Is the org healthy?* |
| Work progress | *Where is work stuck?* |
| Economics | *What is this costing?* |
| Agent detail | *Which agent is failing, and why?* |
| Completion | *What did we actually finish?* |

### From the CLI today

```bash
python3 -m engine.cli doctor --json            # environment + ceiling
python3 -m engine.cli org --json               # roster with per-agent stats and state
python3 -m engine.cli models --json            # catalog with provenance
```

```python
monitor.health_report(agent_ids)     # per-agent state, score, signals
monitor.sli_rollup()                 # org-level SLIs in the library's vocabulary
slo.report()                         # objectives, burn rates, alerts
scheduler.stats()                    # ceiling, queue, providers, utilisation, watchdog
desk.anti_sprawl()                   # token-per-task suspects
```

### The metric that tells you the system is working

Not throughput, and not cost alone: **cost per success**. It is the only figure that combines whether
work finished with what it took to finish it. A run that is cheap and fails is not cheap.

## A configuration that works well

A starting point for a laptop with a local model and one cloud key.

```json
{
  "defaults": { "provider": "ollama", "model": "qwen2.5-coder:7b" },
  "models": {
    "known": {
      "qwen2.5-coder:7b":        { "context_window": 32768,  "locality": "local" },
      "claude-sonnet-4-20250514":{ "context_window": 200000, "max_output": 8192,
                                    "locality": "cloud" }
    }
  },
  "concurrency": {
    "cpu_headroom": 1,
    "queue_max_depth": 32,
    "per_provider_limits": { "ollama": 1, "anthropic": 3 }
  },
  "context": { "compact_at": 0.70, "evict_at": 0.85, "overflow_at": 0.95 },
  "health": { "min_samples": 5, "healthy_at": 0.80, "degraded_at": 0.50 },
  "policy": {
    "default_autonomy": {
      "R-CONTRACT": "auto", "R-REWORK": "auto", "R-DELEGATE": "auto",
      "R-ESCALATE": "confirm", "R-CONFLICT": "confirm", "R-MATCH-FAIL": "confirm"
    },
    "allow_autonomous_escalation": false,
    "router": { "threshold": 0.62, "margin": 0.15 }
  },
  "budget": { "run_max_usd": 25.0, "run_max_tokens": 4000000, "org_max_usd_daily": 100.0 },
  "delegation": {
    "max_depth": 3, "span_of_control": 5, "budget_share_max": 0.5,
    "allow_ephemeral": true
  }
}
```

**Why these values:**

- **`per_provider_limits.ollama: 1`** — the unified-memory guard. The single most important setting on
  a Mac.
- **`compact_at: 0.70`** — proactive, because attention degrades before capacity runs out.
- **`R-ESCALATE: confirm`** — the org moves itself on the routine path and asks at the expensive one.
- **`max_depth: 3`** — the library's default, chosen because each hop compounds error.
- **`budget_share_max: 0.5`** — a child may take at most half the parent's remainder, so a tree fans
  out without outspending its root.

**What to change first if you want speed:** bind builders to the local model and keep only reviewers on
the cloud model. That is the largest cost and latency win available, and it strengthens independence at
the same time.
