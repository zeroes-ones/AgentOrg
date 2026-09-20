# Troubleshooting & Debugging

A runbook. Find the symptom, get the cause, apply the fix. Every command here was run against this
build.

**Start with `python3 -m engine.cli doctor`.** It checks seven preconditions and names each failure,
which resolves most of what follows in one step.

---

## Contents

- [Startup](#startup)
- [Configuration and credentials](#configuration-and-credentials)
- [The skills library](#the-skills-library)
- [Providers and models](#providers-and-models)
- [Planning](#planning)
- [Debugging a run](#debugging-a-run)
- [Debugging an agent](#debugging-an-agent)
- [Cost and budget](#cost-and-budget)
- [Concurrency and hangs](#concurrency-and-hangs)
- [Health and quarantine](#health-and-quarantine)
- [Context, memory and evaluation](#context-memory-and-evaluation)
- [Tests and development](#tests-and-development)
- [How to file a useful bug report](#how-to-file-a-useful-bug-report)

---

## Startup

### `doctor` reports `FAIL configuration`

Read the message — it names the file and the problem:

```
FAIL configuration
  config /path/credentials.json is not valid JSON: Expecting ',' delimiter (line 12, column 3)
```

A JSON syntax error is the most common cause. Validate it independently:

```bash
python3 -m json.tool credentials.json > /dev/null && echo "valid JSON"
```

If the message says `no configuration found`, the engine looked in this order:
`--config`, `$AGENTORG_CREDENTIALS`, `./credentials.json`, `./credentials.example.json`. Note that an
*explicit* pointer (`--config` or the env var) is honoured exclusively — the engine will not silently
fall back to a different file, because that would mean running with providers and budgets you did not
choose.

**Fix:** `cp credentials.example.json credentials.json`.

### `concurrency.per_provider_limits names unknown providers: …`

A per-provider limit names a provider that is not configured. This happens when a provider is
*removed* but its limit is left behind — the `anthropic` case below is exactly a provider the example
ships and a user deletes because they have no key for it:

```
configuration error: concurrency.per_provider_limits names unknown providers: anthropic
```

**This no longer stops the engine.** The loader prunes the dangling entry and records a warning, so the
engine starts and `doctor` says:

```
OK   configuration   …/credentials.json with 5 providers
     warning: concurrency.per_provider_limits named providers that are not configured
     (anthropic); those entries were pruned. This is left behind when a provider is removed.
```

The same applies to `defaults.provider` naming a removed provider: it is dropped with a warning and a
configured provider is used instead. And removing a provider from the console now prunes both
references in the same write, so the state is no longer created in the first place.

If you want the warning gone (it is harmless): remove the stale key from `credentials.json`, or just
add the provider back in the **Providers** tab.

### The app says the engine is running, but nothing works

**Fixed.** The console used to set its state to *running* the moment the engine process *spawned*, so a
bootstrap failure — a refused config, a missing library — showed a green "Engine running" with a pid and
an idle UI.

Now the engine sends an `engine.ready` frame as the first thing on the wire, and the app only reports
running once it has received it. If the engine dies during startup it writes a typed `error` frame with
the reason, which the app shows as a red banner:

```
The engine could not start
the engine could not start: provider 'ollama' has unsupported kind 'weird'; supported: openai, anthropic, ollama
```

Press **Try again** after fixing the cause, or run `engine.cli doctor` — it checks every precondition
and names the one that failed.

### `doctor` reports `FAIL skills library`

```
FAIL skills library  Skills library not found. AgentOrg requires a checkout of zeroes-ones/Skills
                     containing scripts/workflow-runner.py.
```

The engine probes `$AGENTORG_SKILLS_ROOT`, `~/Documents/Projects/Skills`, `~/.zeroes-ones/skills`,
`~/.agentorg/skills`. It checks for the **runner**, not merely a directory — a directory that looks
right but lacks `scripts/workflow-runner.py` is exactly the failure worth catching early.

**Fix:**
```bash
export AGENTORG_SKILLS_ROOT=/path/to/Skills
python3 -m engine.cli --library /path/to/Skills doctor   # or pass it explicitly
```

### `doctor` reports `FAIL skill bundles`

A skill parsed but produced no criteria. The error names it:

```
FAIL skill bundles  skill 'x' declares no completion criteria: no `workflow:` block, no
                    Verification section, and no `Complete when:` checkpoint.
```

That skill cannot be gated safely, so it is refused rather than run ungated. The library documents
three criteria sources and the loader tries all three; a skill failing all of them is genuinely
unusable.

**Fix:** add a `workflow:` block, a Verification section, or a `Complete when:` line to that skill — or
exclude it from your graph.

### `python3 -m engine.cli` gives `No module named engine`

You are in the wrong directory. The commands assume the repository root:

```bash
cd AgentOrg
python3 -m engine.cli doctor
```

### A permission warning appears

```
warning: credentials.json is group/world readable (mode 0o644). Run: chmod 600 credentials.json
```

**Fix:** `chmod 600 credentials.json`. The warning only fires when a *literal* `api_key` is present;
an env-referenced config holds no secret, so a lax mode on it is noise rather than a finding.

## Configuration and credentials

### `provider 'openai' is configured but could not be constructed`

The reason is included, and it is usually a key:

```
provider 'openai' is configured but could not be constructed:
  openai: provider 'openai' (openai) has no API key. Set the $OPENAI_API_KEY environment variable.
```

**Fix:**
```bash
export OPENAI_API_KEY=sk-...
python3 -m engine.cli doctor
```

A *local* provider needs no key. A cloud provider with no resolvable key is treated as a
configuration error rather than a runtime surprise, because failing at startup with the variable name
is far easier to fix than a 401 three nodes into a run.

### `config 'policy.default_autonomy['R-ESCALATE'] = 'auto' is below the safety floor`

This is the safety floor refusing a change, not a bug. `R-ESCALATE` and `R-CONFLICT` cannot resolve
below `confirm` because a single setting must not be able to disable every human gate.

**Fix, if you genuinely mean it:**
```json
{ "policy": { "allow_autonomous_escalation": true } }
```

Only do this deliberately. With it set, an org that decides to escalate will act on that decision
instead of asking you.

### `context thresholds must satisfy 0 < compact_at < evict_at < overflow_at <= 1`

The compaction ladder is inverted. The defaults are `0.70 / 0.85 / 0.95`.

**Fix:** restore the ordering — compacting *later* than evicting would mean the eviction tier never
runs.

### A model is refused at binding time

```
agent 'Alice' is bound to model 'mystery-model' with an unknown context window. The session
projection cannot size a prompt without it, so this binding is refused.
```

This is the design working. An assumed window causes real overflows later, silently.

**Fix, in order of preference:**
1. Probe it: `python3 -m engine.cli models --refresh` (Ollama reports its real window via `/api/show`).
2. Declare it in `models.known`:
   ```json
   { "models": { "known": { "mystery-model": { "context_window": 32768, "locality": "cloud" } } } }
   ```

Verify with `python3 -m engine.cli models` — the `source` column should read `probed` or `declared`,
not `assumed`.

## The skills library

### `library content manifest mismatch — the pinned library has been modified`

A skill or a library script changed after it was pinned. This is the supply-chain check doing its job:
a modified `SKILL.md` is modified system-prompt content.

```bash
cd /path/to/Skills
git status                     # see what changed
git diff skills-flat/code-reviewer/SKILL.md
```

**Fix:** review the change, then re-pin deliberately. Do not disable the check — it exists because
prompt content is a real attack surface.

```bash
cd /path/to/Skills && git diff                # after reviewing the change
python3 -m engine.cli skills pin              # records the new baseline
```

### `library commit mismatch: expected …, found …`

The checkout moved. Same reasoning: review the change, then re-pin with `skills pin`.

A `content unpinned` in `doctor` is *not* this failure: it means no pin has been recorded for this
checkout yet, so only the runner's capability surface was checked. Record one with
`python3 -m engine.cli skills pin`.

### `workflow-runner.py is missing CLI capabilities AgentOrg depends on: --guardrail`

The library version is older than this engine expects. The engine asserts the flags it actually calls,
so a dropped `--guardrail` is caught at startup rather than silently disabling handoff safety.

**Fix:** update the library, or pin an older engine.

### A skill parses differently than expected

```bash
python3 -m engine.cli skills show <name>          # what the engine extracted
head -40 /path/to/Skills/skills-flat/<name>/SKILL.md   # the source
```

The engine extracts the contract, the Production Checklist ids, the `RP1–RP8` research gate and the
anti-rationalization rules. If something is missing from the first command but present in the second,
that is a bug worth reporting — include both outputs.

### `skill 'x' has unparsable frontmatter: frontmatter opening delimiter was never closed`

A truncated file. The parser refuses rather than reading it as "no frontmatter", because a missing
contract would make the node ungated.

**Fix:** `cd /path/to/Skills && git status` — the file is probably half-written.

## Providers and models

### A provider shows `status: down`

```
  lmstudio: down+configured (2 models)  error: lmstudio unreachable: [Errno 61] Connection refused
```

Nothing is listening. `down+configured` means the app still offers the configured models, so it stays
usable.

**Fix, for Ollama:**
```bash
ollama serve                    # start it
ollama list                     # confirm models are present
curl -s http://localhost:11434/api/tags | head -c 200
```

**For LM Studio:** open the app and start the local server, then confirm the port in
`credentials.json` (`base_url`).

### A provider shows `status: misconfigured`

A key was rejected. The distinction matters: `down` means unreachable, `misconfigured` means reachable
and refused.

**Fix:** check the key is exported in *this* shell (`echo $OPENAI_API_KEY`), and that
`api_key_env` names the right variable.

### `model not present locally; run: ollama pull <model>`

**Fix:**
```bash
ollama pull qwen2.5-coder:7b
python3 -m engine.cli models --refresh
```

### The model list is empty for a cloud provider

Cloud `/v1/models` endpoints usually report ids but no capability metadata, and an id with no known
window cannot be bound. If a provider reports nothing at all, the offline table fills in from
`models.known`.

**Fix:** declare the models you intend to use:
```json
{ "models": { "known": {
    "claude-sonnet-4-20250514": { "context_window": 200000, "max_output": 8192, "locality": "cloud" }
} } }
```

### A model appears under one provider but not another

That is correct behaviour. Each provider lists only models it can serve — `gpt-4o` under OpenAI, not
under Anthropic. An earlier version offered every model everywhere; it was fixed because a picker that
suggests an invalid model wastes your time and then fails at call time.

## Planning

### `no candidate manifest validated, which indicates a defect in the planner`

This should not happen: the planner falls back to full → lean → minimal, and the minimal shape is
plain. If you see it, the bug is in the planner, not your goal.

**Report it with:** the goal text, `--json` output, and the library commit
(`python3 -m engine.cli doctor` prints it).

### The plan is missing a phase I expected

Goal keywords add specialists. If your goal did not match a keyword, add the specialist to the org and
bind it, or be more specific in the goal.

```bash
python3 -m engine.cli plan --goal "..." --json | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('nodes:', [n['id'] for n in d['manifest']['nodes']])
print('dropped:', d['dropped'])"
```

The `dropped` list explains anything the planner omitted, with the reason.

### The emitted YAML will not parse

`--out` writes the library's **Safe YAML Subset**, which is narrower than YAML — no flow maps (`{a: 1}`),
no anchors, no block scalars. If you hand-edit the file, keep to the subset.

**Check it:**
```bash
python3 /path/to/Skills/scripts/validate-workflows.py --manifest booking.yaml
```

### `every agent holding 'x' was filtered out for node 'y'`

The router's filters rejected everyone. The reasons are in the message and in `--json`:

| Reason | Fix |
|---|---|
| "it produced the artifact under review" | Hire a second agent with that skill — this is the independence rule |
| "its allocated budget is exhausted" | Raise the budget, or reset it for a new run |
| "it is quarantined" | See [health](#health-and-quarantine) |
| "level … is below the required L…" | Hire a more senior agent, or lower the requirement |

## Debugging a run

### Find what happened, in order

```bash
STATE=projects/<slug>/.agent_state

# The last 20 events
tail -20 $STATE/trace.jsonl

# Just the type of each event, counted
python3 -c "
import json,sys,collections
c=collections.Counter(json.loads(l)['type'] for l in sys.stdin if l.strip())
for k,v in c.most_common(): print(f'{v:6d}  {k}')" < $STATE/trace.jsonl

# Every rejection, with the reviewer's reason
grep '"type":"review.rejected"' $STATE/trace.jsonl | python3 -c "
import json,sys
for line in sys.stdin:
    e=json.loads(line); p=e['payload']
    print(f\"attempt {p.get('attempt')}: {p.get('summary','')[:100]}\")"
```

### Where is the run right now?

```bash
python3 -c "
import json
s=json.load(open('projects/<slug>/.agent_state/run_state.json'))
print('status   :', s['status'])
print('node     :', s['node'], 'iteration', s['iteration'])
print('budget   :', s['budget'])
print('blocked  :', s.get('open_questions'))"
```

### Why was a review rejected?

```bash
python3 -m json.tool projects/<slug>/.agent_state/review_feedback.json
```

The dossier carries each finding with its severity, `file:line`, the issue and the required fix, plus
which checklist ids failed and which criteria remain unsatisfied. That is exactly what the developer
receives on the next attempt.

### The run stopped at a gate

That is the design, not a fault. A gate is a deliberate pause: `escalate_to` in the skill, an
exhausted retry budget, a `Critical` finding, or a policy that says `confirm`.

```bash
grep '"type":"human.gate"' projects/<slug>/.agent_state/trace.jsonl | tail -1 | python3 -m json.tool
```

The payload names the gate, the reason, and what it requires. Resolve it by approving, rejecting with
a reason, or instructing.

### The run stopped with no gate and no error

Check the trace tail for `error` and for `watchdog.restart`:

```bash
grep -E '"type":"(error|watchdog.restart|cost.ceiling)"' projects/<slug>/.agent_state/trace.jsonl | tail -5
```

| Event | Means |
|---|---|
| `cost.ceiling` | The run budget was reached, and the run parked rather than overspending |
| `watchdog.restart` | A worker stopped responding and was killed and resumed from the checkpoint |
| `error` with `needs_compaction: true` | The prompt exceeded the window; the context should have compacted |
| `error` with `retryable: false` | A failure retrying cannot fix — a bad key, a missing model, a bad request |

### Resume after a crash

The checkpoint and the effect journal are what make this safe: `run_state.json` says where the run
was, and `effects.jsonl` records which side effects were already applied, so a resume does not apply
one twice. If a resumed run repeats work you think was done, that is a real bug — capture
`effects.jsonl` and the trace.

## Debugging an agent

### Which agent is failing?

```bash
python3 -m engine.cli org --json | python3 -c "
import json,sys
for a in json.load(sys.stdin)['roster']:
    if a['kind'] != 'ai': continue
    st=a['stats']
    rate = st['tasks_completed']/(st['tasks_completed']+st['tasks_failed'] or 1)
    print(f\"{a['name']:12s} {a['state']:11s} ok={rate:5.0%} \"
          f\"done={st['tasks_completed']} fail={st['tasks_failed']} \"
          f\"esc={st['escalations']} breach={st['contract_breaches']}\")"
```

### An agent keeps producing work that is rejected

Look at what it is being *asked* for, then compare with its own history:

```bash
cat projects/<slug>/.agent_state/review_feedback.json   # what is wrong with its output
python3 -m engine.cli skills show <its-skill>           # what it is held to
```

The usual causes, in order:

1. **The wrong model for the job.** A 7B local model doing architectural review will not meet a
   Staff-level checklist. Move the *reviewer* to a stronger model before moving the builder.
2. **A context that rotated and lost something.** Check for `session.rotate.requested` and
   `session.compact` in the trace. Constraints are preserved verbatim by design, but the *artifacts*
   it was working from may have been tiered down.
3. **A criterion it cannot satisfy.** Read `criteria_unsatisfied` in the feedback — if it names
   something the agent has no way to do (no test runner, no network), the plan is wrong, not the
   agent.

### An agent is blocked

`blocked` means it is waiting on a gate or on you, not that it crashed. The mailbox says why:

```bash
tail -5 projects/<slug>/.agent_state/agents/<agent_id>/mailbox.jsonl | python3 -c "
import json,sys
for line in sys.stdin:
    m=json.loads(line); print(f\"[{m['kind']}] {m['body'][:120]}\")"
```

### An agent is not receiving work

Three checks, in order:

```bash
python3 -m engine.cli org --goal "your goal"      # is it bound to any node?
```

1. **Skill mismatch.** It holds a skill no node asks for. Check the bindings output.
2. **Quarantined.** See below.
3. **Single-flight.** One agent runs one task at a time. If it is busy, the router correctly picks
   someone else or queues.

## Cost and budget

### The run parked with `cost.ceiling`

The budget is a hard stop, checked *before* spending — a post-hoc check would report the overspend
after it happened.

```bash
grep '"type":"cost.ceiling"' projects/<slug>/.agent_state/trace.jsonl | tail -1 | python3 -m json.tool
```

**Fix:** raise `budget.run_max_usd`, or reduce scope (a smaller goal, fewer specialists, a
lower-level model for routine nodes).

### The cost looks wrong

Every figure is labelled, and the label is the point:

| Label | Means | Rendered as |
|---|---|---|
| `measured` | The provider reported it | an exact number |
| `estimated` | Measured tokens × a price table | an estimate |
| `free` | A local model — a *known* zero | `$0.00` |
| `unknown` | No usage reported and no price known | **unknown**, never `$0.00` |

An unmeasured run is never shown as free. If you see `unknown` where you expected a number, the
provider is not reporting usage — check `cost_unreported_runs` in the rollup alongside it. `gpt-4o`
via a compatible proxy that strips the `usage` block is the usual cause.

### Cost per success looks bad

That is often the *correct* reading. A cheap run that fails and retries costs more than a dear one that
works, which is why `cost_per_success_usd` is the metric rather than raw cost. Compare it across agents
before changing anything.

## Concurrency and hangs

### The app is slow while agents run

Expected on a machine with a local model, and bounded by design:

- The engine measures the machine and derives a ceiling (`doctor` prints it with the reason).
- A local provider defaults to a concurrency of **1** because on Apple Silicon GPU and CPU share one
  memory pool — loading two models at once causes system-wide swap.

Check the effective limits:

```bash
python3 -m engine.cli doctor | grep machine
```

If the whole *Mac* is slow rather than the app alone, a second model is loaded. Confirm and unload:

```bash
ollama ps                       # what is resident right now
```

### Agents are queuing and not running

```python
scheduler.stats()   # ceiling, running, queued, providers, utilization
```

| Reading | Means |
|---|---|
| `running == ceiling` | Genuinely at capacity; the queue is correct |
| `queued` high, `running` low | Single-flight or a provider limit is the constraint |
| `providers.<id>.limit == 1` on a local provider | The unified-memory guard, working as intended |
| `backpressure.on` in the trace | A provider rate-limited us and the limiter shrank |

### An agent seems hung

The watchdog distinguishes "slow" from "wedged" and escalates in stages:

| State | Silence | Action |
|---|---|---|
| `alive` | < 1 heartbeat | none |
| `slow` | 1–2 heartbeats | watch |
| `warned` | 2 heartbeats + grace | SIGTERM after the grace period |
| `wedged` | beyond that | SIGKILL, then resume from the checkpoint |

```python
scheduler.wedged()      # every unhealthy ticket, with its silence and its planned action
```

A single hang must not permanently reduce the ceiling, so a wedged slot is reclaimed.

## Health and quarantine

### An agent was quarantined

Every transition is recorded **with the reason**, by design:

```bash
grep '"type":"agent.health.changed"' projects/<slug>/.agent_state/trace.jsonl | tail -3 | python3 -m json.tool
```

The reasons and their fixes:

| Reason | Cause | Fix |
|---|---|---|
| `N consecutive contract breaches` | A hard trigger — it is not converging | Look at its rejections; usually the wrong model for the job |
| `every observed task tripped a guardrail` | A safety signal, not bad luck | Read the guardrail event; the cause is often a prompt or a model that ignores output constraints |
| `composite … < 0.50` | Sustained poor success, escalation or checklist rate | Compare its signals against its peers |
| `probe failed (n/m cases)` | It did not pass its skill's golden cases | It is genuinely not ready; retrain the prompt or change the model |

Note what is *absent*: a new agent is never quarantined on its first bad task. The minimum-sample
guard (5 by default) exists so a hire is not punished for being new.

### How do I bring a quarantined agent back?

By evidence, not by a timer:

```python
monitor.try_recover(agent_id, skill)          # runs the skill's golden cases as a probe
```

A pass restores it to **degraded**, not healthy — trust is rebuilt with real work. A fail leaves it
quarantined with the case results. The probe scores the artifact and its evidence, never the agent's
reasoning, so it cannot inherit the blind spot it is testing for.

If the probe is unavailable, or you have context it lacks, you can restore by explicit decision:

```python
monitor.restore(agent_id, by="owner", reason="false positive on a new skill")
```

That is recorded with who made it. It is a first-class path because you hold terminal authority.

### Every agent for a skill is quarantined

The engine must not spin in that state:

```python
monitor.no_capable_agent(skill="code-reviewer", holders=[...])   # True → escalate
```

**Fix:** hire another agent with that skill, restore one, or raise the escalation to a gate you can
clear. A graph that needs a capability nobody healthy provides should stop and say so.

## Context, memory and evaluation

The mechanism behind these — and why each behaves as it does — is in
[EVALUATION.md](EVALUATION.md). The symptom-level entries:

| Symptom | Likely cause | Check |
|---|---|---|
| A session rotated but nothing was wrong with its size | Attention decay, not capacity | The rotation reason says `attention_decay` and the saturation is low |
| A rotation keeps happening | The rotation cap or the impossible case | The reason says `rotation-cap` or `irreducible-overflow` |
| A prompt is refused | Irreducible overflow, or a reducible one | The diagnosis says "compaction should recover" or "Rotating will not help" |
| A constraint is missing after a rotation | Should be impossible | `session_handoff.json` should list it; if not, that is a bug |
| Cost shows `unknown` | The provider reported no usage | A compatible proxy may be stripping the `usage` block — `unknown` is correct, not a fault |
| The eval suite fails after a library update | A skill lost its criteria | `python3 -m engine.evals.runner --json` names the scenario and the error |

```bash
# The rotation history for a run, with the trigger for each
grep session.rotate projects/<slug>/.agent_state/trace.jsonl

# What a rotation carried
python3 -c "import json; h=json.load(open('projects/<slug>/.agent_state/session_handoff.json')); print(len(h['constraints']), 'constraint(s);', h['context_pruned'])"

# The behavioural suite, with the failing scenario named
python3 -m engine.evals.runner --json | python3 -c "
import json,sys; d=json.load(sys.stdin)
[print(n, '->', r['error']) for n, r in d['results'].items() if not r['passed']]"
```

## Tests and development

### Running the suite

```bash
python3 run_tests.py                 # unit suite, no dependencies
python3 -m pytest tests/ -q          # if pytest is installed
python3 -m engine.evals.runner       # behavioural suite, with the regression gate
```

Both unit runners give the same result — 1096 passed. The behavioural suite is separate and answers a
different question; see [EVALUATION.md](EVALUATION.md#evaluation-proving-the-judgments). The stdlib runner is a real runner: it discovers files,
expands `parametrize`, resolves fixtures, runs generator teardown, and reports failures with their
traceback.

```bash
python3 run_tests.py -q tests/test_phase4_org.py    # one file
python3 run_tests.py -q -k router                   # by name
python3 run_tests.py -q -k health                   # by name
```

### A test fails but I did not change anything

The suite touches the real library for the parser-agreement tests. If the library changed, that is
the likely cause:

```bash
cd /path/to/Skills && git log --oneline -3 && git status
python3 -m engine.cli doctor | grep skills
```

The frontmatter parser is asserted to be byte-identical to PyYAML across all 327 skills, so a library
change that breaks that is a genuine signal, not a flaky test.

### Adding a test

Tests live in `tests/test_<phase>_<area>.py`. Both `pytest` and `run_tests.py` discover `test_*`
functions. Fixtures work under both; `module` scope is honoured by the stdlib runner so an expensive
fixture loads once per file.

## How to file a useful bug report

Include these five things and the problem is usually diagnosable without a round trip:

```bash
# 1. The environment — one command, includes the library commit
python3 -m engine.cli doctor > doctor.txt 2>&1

# 2. What you ran, and what it said
python3 -m engine.cli <your command> > output.txt 2>&1

# 3. The relevant slice of the run, if a run was involved
grep -E '"type":"(error|human.gate|review.rejected|agent.health.changed|cost.ceiling)"' \
  projects/<slug>/.agent_state/trace.jsonl | tail -50 > events.jsonl

# 4. The state, if a run was involved
cp projects/<slug>/.agent_state/run_state.json run_state.json

# 5. The version
python3 -c "import engine; print(engine.__version__)"
```

**Redact before sharing.** `credentials.json` is never needed. Check that `doctor.txt` and `output.txt`
contain no key material — the engine redacts `sk-…`, `Bearer …` and `x-api-key` patterns on the way to
the event bus, but a shell `set -x` or a hand-written command can still echo one.
