# SESSION-STATE.md — where this work stands, and what is next

> **Purpose:** a durable record so a later session does not have to re-derive it. Everything under
> "verified" was run and observed in this session; everything under "in flight" is a running agent
> whose result nobody has checked yet.
>
> **Last written:** 2026-09-19, ~10:45 local.

---

## 1. The four asks, and their state

| # | Ask | State |
|---|---|---|
| 1 | macOS app + CLI can control the system "for anything granted" | **Half done.** 9 agent tools work; *you* cannot use any of them yet |
| 2 | App icon + smooth process | **Done and verified** |
| 3 | CLI should handle everything the app does | **In flight** (agent-19) |
| 4 | Best-practice HIG onboarding, every step shown | **Designed, not built** (agent-20 building the engine half) |

---

## 2. Verified this session

Each of these was run and its output inspected. Do not re-derive them.

### 2.1 The macOS app works end to end

- **Engine readiness is no longer starved.** Both pipe handlers shared one serial queue, so the
  stderr read blocked the stdout handler and `engine.ready` was never decoded — the app sat on
  "launching the engine…" forever while the engine was healthy. Found by sampling the live process
  and seeing the thread parked in `read()`. Fixed with one serial queue **per pipe**.
  Regression test **proven to fail** on the old code before passing on the fix.
- **The app starts, attaches, and lands on Now.** `scripts/run-macos-app.sh --build-only`, `open`,
  then verified: engine child alive, window titled correctly, terminal showing `engine ready
  (5 providers)`.
- **A provider can be added.** `provider_test` + `provider_add` both succeed and persist.
- **App and engine agree on the project directory.** The app hardcoded slug `demo` while
  `serve` defaults to `console`, so the History panel read a directory that never existed. Verified
  fixed: the app spawns `serve --slug console --root …/projects`.

### 2.2 The icon is real

`macos/Resources/AppIcon.svg` → `.build/AppIcon.icns` → bundle, named by `CFBundleIconFile`.
I rendered it and **looked at it**: a macOS squircle holding an org tree (amber owner node above
three equal worker nodes). I also rendered it at **16px** and confirmed the tree is still
distinguishable rather than a smear.

### 2.3 Two real bugs fixed in the delegation path

Both were found by an agent and verified by me afterwards.

1. **`system=` was never passed at the executor call site** (`engine/executor.py`), so every system
   tool was **inert in a live run** — the registry supported them and nothing offered them.
2. **`system:` grants were auto-approved at T1.** `DelegationConfig.elevated_markers()` was **dead
   code**: `classify_tier` carried its own hardcoded copy of the marker list, so editing the config
   changed nothing. Now any `system:` capability classifies **T3, Owner gate** — measured.

### 2.4 System control: scoped grants, and a full-access mode

`engine/sysctl_tools.py` (1254 lines) ships **9 tools** across 6 capabilities, an ask-once consent
ledger, and a catalogue the registry reads for advertisement *and* enforcement.

Verified behaviour:
- `system_state`, `read_clipboard`, `get_volume` work when granted.
- Without the grant: refused, naming what was required and what the agent holds.
- **A sibling grant does not leak**: holding `system:clipboard` does not give `system:state`.
- **State changes need consent**, once per agent per tool, recorded in the decision ledger.
- **An agent cannot approve itself** — `grant_consent` refuses a `by` that names an agent.
- **Consent and the allowlist are independent**: granting consent left the allowlist binding.

`system.allow_full_access` is the mode both references ship ([Reasonix
`bypassPermissions`](https://github.com/esengine/deepseek-reasonix), Kimi `--auto`). It lifts exactly
three things (the two allowlists and the consent prompt) and **not** `enabled`, the agent's grant, the
time/output bounds, or the record. Verified both directions.

### 2.5 The capability vocabulary, and the drift guard

`SystemConfig.CAPABILITIES` now declares **12**:

```
system:state  clipboard  screenshot  media  open  automation
notify  search  power  network  shortcuts  softwareupdate
```

`test_phase37_system_tools.py` contains a guard asserting every declared capability has at least one
tool. **It is currently RED**, and that is correct: the six new capabilities have no tools until
agent-21 lands them. Do not "fix" it by trimming the config.

---

## 3. In flight — nobody has checked these yet

| Agent | Work | Files it owns |
|---|---|---|
| agent-19 | CLI parity: 15 operations the app can drive and the CLI cannot | `engine/cli.py` |
| agent-20 | Shared onboarding model + CLI first-run flow | `engine/onboarding.py`, `chat.py`, `cli.py` |
| agent-21 | Tools for the six new capabilities | `engine/sysctl_tools.py` |

**`engine/cli.py` is contested by two agents** (19 and 20). Do not edit it until both report.

### 3.1 Built while those ran: `engine/syscap.py` (verified, 10 tests)

The *person-facing* half of system control — "what am I being asked to allow, and what does it
reach". `sysctl_tools` answers the question for a model; this answers it for a human, and the console
cannot derive one from the other. Delivered:

- `describe()` → every declared capability with a title, what it reaches, **what changes** (in the
  words of what a person would notice), and a `caution` **only where there is a real one**. Order is
  deliberate: reads, then state changes, then the powerful ones.
- `granted_in`, `unmet`, `summary` — a status a bar can render.
- `available` marks a grant with no tool yet, so a console survives the tree being mid-build rather
  than offering a switch that does nothing.

**Verified: the display agrees with what the tool layer enforces** — compared on a real agent for
four grants, all agreeing. That is the failure this module exists to prevent: a console showing a
grant as held while the tool refuses it is confidently wrong about a permission, which is worse than
showing nothing.

**Test discipline worth repeating:** the first version of the mutating-state test hand-wrote its own
list of which capabilities change state, and was wrong twice — it excluded `system:screenshot` (which
does write a file) and asserted about `system:notify` (whose tools do not exist yet). It now derives
from the tool module's own `MUTATING_TOOLS` and skips capabilities with no tools, so it tests the
invariant that can actually be wrong instead of a second hand-maintained copy.

### The parity gap agent-19 is closing

```
abort  reassign  takeover  agent_update  agent_retire
providers  provider_add  provider_remove  provider_test
defaults_set  autonomy_set  improve  proposals  subagents  subagent_result
```

### The architectural gap agent-20 is closing

Onboarding logic is **Swift-only** today. `SetupGate` lives in
`macos/Sources/AgentOrgKit/Setup.swift` and the CLI has nothing (`grep "first-run" engine/cli.py` →
0). The app walks a person through setup; the terminal leaves them to read `USAGE.md`. Moving the
*decision* into the engine makes both surfaces read one answer — the same reasoning that produced
`doctor_checks`.

---

## 4. Next, in order

1. **Let the three agents finish, then verify each** — do not accept a self-report. For each: run its
   tests, check the claim against the code, and confirm the drift guard goes green.
2. **Build the user-facing half of system control.** This is the ask still genuinely unmet:
   - **CLI**: commands a person runs directly — `engine.cli system state|clipboard|screenshot|…`
     driven through the *same* `SystemTools` the agents use, so there is one implementation.
   - **App**: a **System** panel (in Setup, or its own section) that shows each capability, what it
     reaches, whether it is granted, any outstanding consent request, and a way to grant/revoke.
3. **Finish the onboarding to HIG.** The audit below is the spec.
4. **Full regression** on Python + Swift + evals, then report.

---

## 5. The onboarding HIG audit — the spec for step 3

Run against the `apple-hig-expert` skill's own checker. Current score **60/100** (its bar for
"ship" is 90; 70 means "fix before release").

**Failing:**

| Rule | Evidence |
|---|---|
| Reduce Motion | no `accessibilityReduceMotion` in `SetupPane.swift`; step transitions animate unconditionally |
| Reduce Transparency | no opaque fallback for the translucent step cards |
| Whole journey visible | only the current step is shown — no view of all steps, what each unlocks, or where it leads |
| Explains how it works | nothing says what the org does, what a run looks like, or what happens after setup |

**Passing:** semantic colours only (no hardcoded hex), VoiceOver labels (31 occurrences), Dynamic
Type (no fixed-point sizes), targets ≥ 44pt.

> **Do not over-correct on one checker result.** It flagged text fields as under 44×44 pt. That rule
> is for **touch** targets and does not apply to macOS text fields; "fixing" it would make the form
> worse. Verified as a false positive — leave it.

The improvement should be driven by the engine's `journey()` (agent-20): every step with its title,
what it is for, whether it is satisfied, the one command that resolves it, and **what it unlocks**.
That is what turns "three things must be true" into a person understanding the whole path.

---

## 6. Traps a later session must not fall into

- **`engine/cli.py` is contested.** Two agents are editing it. Wait for both.
- **The drift guard is red on purpose** (§2.5). It is not a regression.
- **Do not weaken a test to make a suite green.** Two existing tests in this session encoded buggy
  behaviour; each was changed only with the reasoning recorded in its docstring, and each new test
  was **proven to fail** on the old code before being accepted.
- **No self-reported agent result is trusted.** Every item in §2 was re-run and observed by hand.
- **A `git stash` in a loop is destructive.** One earlier session lost files to that. Check
  `git stash list` is empty before and after any long operation.
- **`credentials.json` is gitignored and holds live keys.** Never paste one into a chat; if one
  appears, tell the person to rotate it. Never commit it.

## 7. Environment facts

- The macOS app is non-sandboxed and its engine root sits under `~/Documents`, which macOS protects.
  The first launch raises a TCC prompt; `Info.plist` now carries usage descriptions explaining it.
  **A TCC prompt cannot be answered programmatically** — it needs the person to click.
- `reasonix` is at `/opt/homebrew/bin/reasonix`, `kimi` at `/Users/sp.vm/.kimi-code/bin/kimi`. Both
  are useful for checking what the references actually ship — `--help` only, never an agent run.
- Live provider: `Olla` (openai-compatible, `https://ollama.com/v1`), model `deepseek-v4.1-flash`.

## 8. Out of scope, decided deliberately

- **No OS-level sandbox for `system:automation`.** AppleScript has no capability model, so an
  allowlisted *prefix* constrains transport, not the snippet's power. Documented as a known limit in
  the module rather than papered over.
- **No semantic/vector memory.** Different feature, different failure modes.
- **Text-field hit targets** — see §5.

---

## 9. Unattended self-repair from a contract refusal (2026-09-19, ~13:15)

**The defect.** A run given `--posture unattended` still parked on a person, at node one, over a
recoverable contract refusal:

```
python3 -m engine.cli run --project .../Ideas --goal "Produce deep research documentation …" \
  --posture unattended --slug wide-market-deep-research
→ outcome: gated, steps: 1, phase: awaiting_gate, exit 1
  GATE: pm — handoff propose refused: R6: 9 open questions exceed the 3 ceiling.
  every other node still 'pending'
```

Four composed causes, all fixed: (1) `_detect_gate` minted a human gate from **any** `needs_review`
node and read the manifest only for the gate's *wording*; (2) a node outside a loop had **no retry at
all** (`workflow-runner.py` escalated a contract violation immediately); (3) so nothing carried the
fired rule back to the node; (4) R6 counts the questions the run has *accumulated*, so a retry that
kept the refused attempt's questions would fail identically for ever.

### What changed

- **`engine/orchestrator.py`** — `_detect_gate` now distinguishes a declared gate from a stuck node:
  `verdict == "awaiting_owner"` is a declaration and still parks; `status == "needs_review"` parks
  only when `_gate_declaration(node)` exists **or** `_stalled_node(state)` says the run has genuinely
  stopped (the runner's phase is no longer `execute`). New `_contract_rework_attempts(run)` reads the
  window width from `config.executor.contract_rework`, gated on the goal's posture — `unattended`
  gets the window, `supervised` gets 0. `execute` threads it onto the host before spawning.
- **`engine/host.py`** — `contract_rework()` / `with_contract_rework()`; the flag is passed only when
  non-zero, so a supervised run's command line is byte-identical to before. Also `resume_run` now
  threads `extra_args` (a continuation previously dropped the executor override).
- **`engine/prompts.py`** — a `CONTRACT REPAIR` block, rendered only when a rework context exists,
  carrying the refusal verbatim plus field-level guidance. It names the run's contract generically
  and only says "handoff" nowhere — the *real* reproduced run was refused by the **completion**
  contract (missing `evidence` / `criteria_satisfied`), so a block that assumed the handoff path told
  the node to fix the wrong thing.
- **`engine/executor.py`** — `_rework_context(state)` reads the refusal the runner left in run-state
  and threads it into `TaskContext.contract_rework`.
- **`/Users/sp.vm/Documents/Projects/Skills/scripts/workflow-runner.py`** — `--contract-rework N`
  (default 0 = today's behaviour). A refusal outside any loop is retried in place up to N times,
  carrying `{reason, rule, attempt, max_attempts, open_question_limit, open_questions}` in both `ctx`
  and run-state, **withdrawing the discarded attempt's own open questions first** (without that, R6
  refuses the retry with the identical count — a loop, not a rework). Exhaustion logs `escalate` with
  the reason, so the stop reason names it. A node's own `max_iterations` is a floor, not a cap.

### Measured — the real re-run, this machine

Fresh slug `wm-deep-research-r2`, identical command otherwise, real provider (Olla / deepseek-v4.1-flash):

```
outcome : gated     steps : 4     phase : awaiting_gate
log: pm contract-warning declared artifacts.outputs not produced: product-spec
     pm done verdict=contract-violation                       ← step 0
     pm contract-rework attempt 1/3: …                        ← step 1
     pm contract-rework attempt 2/3: …                        ← step 2
     pm contract-rework attempt 3/3: …                        ← step 3
     pm escalate contract rework exhausted after 3 attempt(s): rework window spent (3/3)
GATE: pm — handoff propose refused: R6: 6 open questions exceed the 3 ceiling.
```

**Partial improvement, honestly:** where the old run stopped at **step 1**, this one ran **4 steps**
and spent its whole bounded window trying to repair before parking — with the exhaustion named.
Downstream nodes (`uxr`, `bi`, `technical-writer`, `analyst`, `critic`) never ran, because the model
never satisfied the completion contract (it kept omitting `evidence` / `criteria_satisfied`). The
bound is what parks it; the window is what gives a *recoverable* refusal a chance first.

**Tests:** `python3 run_tests.py tests/test_phase41_autonomous_recovery.py` → **24 passed, 0 failed**.
Library selftest `python3 scripts/workflow-runner.py --selftest` → **27 checks, 0 failed**.
Full suite run at the end of this session — see §10 if it disagrees with this line.

### Known limits / what remains

- **The window did not repair the completion-contract case on this model.** The prompt now names the
  offending fields (`criteria_satisfied`, `evidence`), which is the actionable version — but the
  engine already has a *focused* trailer-repair turn (`_repair_trailer`) that exists for exactly this
  failure. Whether the rework pass should invoke that, rather than re-prompting the whole node, is
  the open design question. Re-running the command above with the new prompt would answer it.
- **The runner checkpoint is one file per workspace** (`.agent_state/runner_state.json`), not per run,
  so runs in the same project reuse each other's node records. Worth checking before reading a fresh
  slug's `steps` as its own.
- **`engine/cli.py` was not touched** (contested). The posture is applied by the existing `cmd_run`
  path; no CLI change was needed.
- Do **not** weaken R1–R8 or the completion contract to make a run finish. The change is who
  recovers, not whether the rule is enforced.

---

## 10. §9 addendum — the second re-run, the full suite, and two honest caveats

**A second end-to-end run** with the improved repair prompt (fresh slug `wm-deep-research-r3`), so the
prompt half of §9 is measured rather than assumed:

```
outcome : gated     steps : 4     phase : awaiting_gate
step 0  pm done verdict=contract-violation
step 1  pm contract-rework attempt 1/3: completion.criteria declares 3 criteria … no criteria_met coverage
step 2  pm contract-rework attempt 2/3: … ; 16 open question(s) withdrawn   ← the withdrawal works
step 3  pm contract-rework attempt 3/3: completion.evidence is 'required' but the node reported no evidence
step 4  pm escalate contract rework exhausted after 3 attempt(s): rework window spent (3/3)
GATE: pm — contract violation: completion.criteria declares 3 criteria (c1, c2, c3) …
```

Same 4 steps, same bounded exit, same honest gate — **twice, reproducibly**. The withdrawal line is
direct evidence of the R6 fix; `steps` went 1 → 4 both times.

**Why it does not repair *this* case on *this* model.** The diagnostics are unambiguous:

```
trailer.unparsable 20   trailer.repair 20   trailer.repair.incomplete 9   trailer.repair.ok 2
```

The model cannot emit a parsable trailer for a 3-criteria node at all, so every attempt fails the
completion contract at the same clause regardless of prompt. That is a **model-capability** wall, not
a control-flow one: the honest outcome for it is the bounded stop we now get, and the earlier `steps:
1` stop was strictly worse because it never even tried.

### The full suite

`python3 run_tests.py` → **2263 passed, 1 failed** (my run) and **2262 passed, 2 failed** (the parent's
concurrent run). **The failures are not mine, and here is the proof:**

- Every failure landed in `tests/test_phase11_serve.py`, whose subject is the parent's in-flight
  console work — different tests each time (`test_goal_events_reach_the_console`,
  `test_status_carries_the_swarm_in_both_branches`, `test_status_carries_the_workspace_and_subagent_tree`).
- A control tree (pristine `HEAD`, plus *only* the parent's `serve.py` + serve test) flaked **58
  passed / 4 failed** while the working tree passed **62 / 0** in the same interleaved window.
- The cause is in `Server.serve_forever`: commands run off a daemon worker thread while `_drain` waits
  on `empty() and _in_flight == 0` — a load-dependent race in the *serve* path, untouched by this work.
- `test_phase41_autonomous_recovery.py` passes 24/24 standalone, in the full suite, and when run after
  the prompt/cache modules.

**Do not read the `serve.py` flake as a regression from this track.** It reproduces on pristine
`HEAD` and is a separate defect in the console work, worth its own fix.

### The one thing still worth doing

The engine already has a *focused* trailer-repair turn (`NodeExecutor._repair_trailer`, two bounded
turns, coverage-checked) built for exactly the failure above. The rework window re-prompts the whole
node instead. Wiring the rework to that focused turn — rather than a full node re-run — is the open
design question, and it is the change most likely to make this class actually recover.

---

## 11. §11 — the user-facing half of system control, and the switches that were never written

**Written:** 2026-09-19, ~15:00 local. Everything below was run and observed in this session.

### 11.1 The finding that mattered most: nothing could turn it on

`SystemConfig` was **readable but had no writer**. `engine/serve.py::_cmd_system` answered "enabled:
no", every system tool refused with *"the machine tools are off"*, and **no command in the engine
could change that**. The `system` section was not present in `credentials.json` at all.

So the honest state before this session was: 18 tools, 12 capabilities, a consent ledger, a full
capability description — and a person could not use any of it. The user had said "I already gave
permission" and there was nowhere to record that.

Added `engine/config.py::set_system(path, system={...})`, the third deliberate write alongside
`set_defaults` and `set_autonomy`, following the same merge-never-replace and atomic-0600 rules.
Verified round trip: writes, reloads, parses. Blank entries are dropped from allowlists, because an
allowlist holding `""` is one entry that matches nothing.

### 11.2 Two ways to grant, one per surface

- **CLI** — `engine.cli system enable --on [--full-access] [--allow-apps NAME ...]`. With no flags it
  is a *question*, not a no-op write: it prints the current switches, the file, and the exact command.
- **App** — new `system_set` serve command (`engine/protocol.py::CommandType.SYSTEM_SET`,
  `serve.py::_cmd_system_set`), dispatching by the existing naming convention. Only the keys present
  in the payload are written, so a panel sends one switch without restating the others — the same
  rule `autonomy_set` follows. Reloads in place and returns the fresh `system` document.

Verified live: `system_set` wrote `enabled: true` and `allow_apps: ['Safari']` to disk and reported
the reloaded state.

### 11.3 The CLI surface, wired

`engine/systemcli.py` (agent-24) is installed by one line at the end of `cli.build_parser()`. Every
command goes through `ToolRegistry.call`, so the terminal and the agents share **one gate**. Exit
codes: `0` worked, `1` ran and failed, `2` usage, `3` **refused by the engine** — a distinct outcome
from a failure, because "you are not allowed" and "it did not work" need different responses.

Verified live after enabling:

```
$ python3 -m engine.cli system state
  battery   : 100%, charged, 0:00 remaining, on ac power
  disk      : 416Gi free of 926Gi on /, 3% used
  uptime    : 2 days, 3 hours, 49 minutes
  software  : macOS 27.2 (26B5086k)
  apps      : 165 running (applications and background agents)
```

`screenshot` wrote a real 12.7 MB PNG. `system list` renders `syscap.describe()` — the same function
`serve._cmd_system` returns to the app — so the terminal and the GUI cannot say different words about
the same grant.

### 11.4 The journey now reaches the app

`engine/onboarding.py` was CLI-only. `serve._cmd_status` now carries `journey` (from
`onboarding.journey_payload`), so the wizard reads the whole path — four steps, each with its
purpose and what it unlocks. `probe=False` deliberately: status is polled every few seconds, and the
default probe reaches the network per provider.

**`_journey()` is in *both* branches of `_cmd_status`.** The first version was only in the running
branch, so the idle case — the one where a person is actually setting up — returned no journey at
all. The file's own docstring says every key is present in both branches; this is the second time
that rule has bitten, so it is worth re-reading before adding a key.

### 11.5 Two tests that failed *because the feature was switched on*

Both are the same defect and both are fixed:

- `tests/test_phase40_capability_surface.py::test_the_console_can_ask_the_engine_what_each_grant_means`
  read the live `credentials.json` and asserted `enabled is False`.
- `macos/Tests/AgentOrgKitTests/SystemPanelTests.swift::testTheSwitchesAndTheSummaryTravelWithTheCapabilities`
  asserted the same about the developer's machine.

A test that goes red when the feature is used teaches the next person to switch the feature back
off, which is the opposite of what a test is for. Both now construct a config for the test (Python)
or assert the round trip and internal consistency instead of an environmental fact (Swift).

### 11.6 Verified in this session

| Check | Result |
|---|---|
| `python3 run_tests.py tests/test_phase37/38/39/40/41/42` | **328 passed, 0 failed** (drift guard green: 12 declared, 12 with tools) |
| `swift test` | **345 tests, 0 failures** |
| `scripts/run-macos-app.sh --build-only` | builds |
| App launch | process alive, engine child spawned (`serve --slug console --root .../projects`) |
| `system state` / `screenshot` / `consent list` | real output, exit 0 |
| `system_save`… `system_set` round trip | written to disk, reloaded |

### 11.7 Still open, honestly

1. **The autonomous run does not yet finish.** The bounded rework window now runs (§9) — the run
   advances 4 steps instead of parking at step 1 — but this model cannot emit a parsable completion
   trailer for a multi-criteria node (`trailer.unparsable 20 / ok 2` in the diagnostics), so the
   window is spent and the run parks with the exhaustion named. §10 names the fix: route the rework
   through the existing focused `NodeExecutor._repair_trailer` turn instead of re-running the node.
2. **The `serve_forever` load race** (§10) is untouched and is not from any track here.
3. **The app UI was not visually re-verified.** The screen locked before the final capture. The panel
   is covered by 15 `SystemPanelTests` and the app launches with its engine attached, but nobody has
   looked at the rendered System pane since the last edit.

---

## 12. §12 — parity with Kimi and Reasonix, measured rather than asserted

**Written:** 2026-09-19, later in the same session. Both references were inspected with `--help`
only — **never run as agents**, per §6.

### 12.1 What each reference actually offers

Read from `reasonix --help` and `kimi --help`:

| Reference | Autonomy switch | Permission model | Shell completion | Session control |
|---|---|---|---|---|
| Reasonix | `--permission-mode MODE`, `run --max-steps N` | `bypassPermissions` + `sandbox = false` | **`reasonix completion bash\|zsh\|fish`** | `session list\|show\|status\|recovery`, `task list\|show\|stop\|cancel\|monitor` |
| Kimi | `--auto` ("never interrupts you"), `-y/--yolo` | ask-when-needed vs never-ask | shell integration (not a subcommand) | `-S/--session`, `-c/--continue`, `fork`, `export` |
| **AgentOrg** | `--posture unattended\|supervised`, plus `goal.auto_pass_auto_gates` | 12 scoped `system:*` grants + per-tool consent ledger, `allow_full_access` as a second switch | **`engine.cli completion bash\|zsh\|fish`** — added this session | `session list\|show\|export\|fork`, `schedules` |

### 12.2 Where we are now ahead, and where they are

**Ahead:**
- **Granularity.** Both references expose autonomy as a *level* (bypass permissions / never ask). We
  expose **12 independent capabilities each with an allowlist**, and `allow_full_access` as a
  deliberately *separate second* switch — because "which apps may you start" and "do I still want to
  be consulted" are different questions, and one switch for both gives a person no way to answer them
  differently. Reasonix's model is one bit; ours is a scope per capability.
- **A record of who approved what.** `grant_consent` refuses a `by` naming an agent, and every
  approval is a ledger entry with a reason. Neither reference documents a per-action approval ledger.
- **One gate for both surfaces.** The app's `system_invoke` and the CLI's `system <verb>` both reach
  the machine through `ToolRegistry.call`, so the person and an agent mid-run pass the identical
  check. That is what makes "there is one implementation of set_volume" a fact rather than a hope.

**Behind / different:**
- **Session recovery tooling.** Reasonix has `session recovery` and a conflict-diagnostic zip;
  Kimi has ACP and a web UI. We have `session` commands but no recovery-diagnostic export.
- **`--add-dir`.** Both accept extra workspace directories. We take one `--project`.
- **Streaming output format.** Kimi has `--output-format text|stream-json`. We have `--json`
  (whole-document). Deliberately not duplicated — see §12.3.

### 12.3 What was deliberately *not* copied

`--output-format stream-json` was not added. `--json` already answers "give me machine-readable
output", and this codebase's discipline is one answer per question; a second, parallel flag set for
the same intent is the drift this repo has already been bitten by. If streaming is wanted later, it
belongs as a property of `--json`, not a rival to it.

### 12.4 Measured surface (this session)

| Fact | Value | How to re-check |
|---|---|---|
| Command paths in the CLI | **106** (36 top level) | `python3 -c "from engine.cli import build_parser; from engine.completion import command_tree; print(len(command_tree(build_parser())))"` |
| System capabilities | **12** declared, **12** with tools | `python3 -m engine.cli system list` |
| System tools | **18** | `python3 -c "from engine.sysctl_tools import CATALOGUE; print(len(CATALOGUE))"` |
| State-changing tools needing consent | **10** | `python3 -c "from engine.sysctl_tools import CONSENT_REQUIRED; print(len(CONSENT_REQUIRED))"` |
| Shells with working completion | **3** (bash, zsh syntax-verified; fish emitted but **not** verified — fish is not installed here) | `engine.cli completion bash \| bash -n` |

### 12.5 The console can now act, not just describe

Three commands close the gap between "the app shows 12 capabilities" and "the app can use one":

- `system_set` — writes the switches and the allowlists. **Only the keys sent are changed**, so a
  panel cannot revert a change made elsewhere (tested).
- `system_consent` — records an approval in the run's ledger; refuses one an agent could self-issue.
- `system_invoke` — runs a capability through `ToolRegistry.call`, the same gate an agent passes.
  Returns a refusal as data (`refused: true` + the reason) rather than raising, because the reason is
  the useful part.

`tests/test_phase45_system_actions.py` — **13 tests, 0 failed**. The load-bearing one substitutes
`ToolRegistry` and asserts the command went through it: a panel with its own shortcut would be a
route to the machine the ledger does not record.

### 12.6 Shell completion, derived from the parser

`engine/completion.py` walks `argparse`'s own actions recursively — the same technique
`systemcli._command_names` uses — so the script cannot offer a verb that no longer exists. A
hand-written list would rot silently, because completion is advisory and a stale name produces no
error at all.

**A real defect was found by running the script rather than reading it:** the position check ran
before the flag check, so `engine.cli --js<TAB>` completed to *nothing* — the global flags, the thing
a person most wants completed, were the one thing that could not be. Fixed, and pinned by a test that
sources the script in a real bash and calls its function.
