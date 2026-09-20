# AgentOrg — Design: the Default Model, Goal Autonomy, and the Flow Board

How "which model does everyone run on", "who decides when the org is stuck", and "who is working on
what" each became one answer in one place.

Sixth of the amendments:

| Amendment | Question it answers |
|---|---|
| [`DESIGN-WORKSPACE.md`](DESIGN-WORKSPACE.md) | *Which folder is the code?* |
| [`DESIGN-GOAL.md`](DESIGN-GOAL.md) | *What does "keep going" mean?* |
| [`DESIGN-SUBAGENTS.md`](DESIGN-SUBAGENTS.md) | *How is parallel work isolated and read back?* |
| [`DESIGN-ACTIVITY.md`](DESIGN-ACTIVITY.md) | *What is it doing, why, and is the org the right one?* |
| [`DESIGN-AUTONOMY.md`](DESIGN-AUTONOMY.md) | *What is it all for, and who decides when it is stuck?* |
| **DESIGN-DEFAULTS-AUTONOMY.md** (this) | *What model do my people run on, who is involved, and who is on what?* |

The end-to-end picture — every level, every command, every event, and where it is autonomous — is
[`AUTONOMY-MAP.md`](AUTONOMY-MAP.md). This document is the design reasoning behind the three additions
that map records.

---

## 1. The problem

Three gaps, reported together, and they compound:

1. **"The default" was three different answers.** `hire` defaulted to `ollama`, `default_company` to
   `qwen2.5-coder:7b`, and the planner to whatever the catalog listed first. So a person could hire an
   agent onto one model and watch the built-in company run on another, and there was no single command
   to say "everyone runs on this unless I say otherwise".
2. **A goal always parked at a gate.** `executor.auto_pass_auto_gates` existed in the config and was
   *never read* — dead code. Arming a goal therefore never helped past the first gate, and a node
   whose skill nobody held stopped the run three nodes in, far from the cause.
3. **"Who is working on what" had no surface.** The engine recorded every fact needed — the binding per
   node, the handoffs, the spawns — but the only views were a chronological story (`activity`) and a
   snapshot of one run (`status`). Nothing answered the question a person running an org actually asks
   when several things are in flight at once.

## 2. One default, resolved once

```
credentials.json [defaults]  ──▶  Config.default_pair()  ──▶  (provider, model, reason)
                                        │
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
             default_company      People.hire        auto-staff helper
             (the built-ins)      (your hires)       (a created person)
```

`Config.default_pair()` is the single resolution, and it returns a **reason** as well as the pair, so a
caller can always say *why* the answer differs from the file. The order is:

1. `defaults.provider` + `defaults.model` when both are configured and usable.
2. the configured provider with a declared model that has a known window.
3. the first usable provider with any model, else the first configured provider at all.

Three decisions make it safe rather than merely convenient:

- **A stale pointer degrades, it does not refuse.** A default naming a removed provider resolves to a
  usable one and records the reason as a warning. Refusing here would brick `doctor`, which exists to
  explain exactly that problem — the same reasoning `write_provider(remove=True)` already follows.
- **A window can be declared.** A model the provider never probed has no known window, and an agent
  cannot bind to it. `defaults.context_window` resolves it without a probe. That is the commonest
  first-run failure ("I set the default and it says no agent can be bound"), so the override is a
  first-class field rather than a workaround.
- **The write merges.** `set_defaults` changes only the keys named, atomically, mode `0600`, sharing
  one implementation with `write_provider`. A person setting their default must never lose their keys,
  and a torn write must never replace a good file.

The reviewer pair is deliberately separate (`defaults.reviewer`, written in a readable nested form the
loader also accepts), because `verification-independence-engineer` requires a verifier that differs
from its producer. Making the distinct model a default rather than a request is what keeps the property
holding without the Owner having to know to configure it.

## 3. Autonomy: a human is involved only if you chose one

The product is a tool you point at a repo and leave. So the polarity is **autonomous by default**, and
the exceptions are explicit. The authority lives in two places:

| Level | Setting | Scope |
|---|---|---|
| Config | `goal.default_posture` | the posture a *new* goal inherits |
| Config | `goal.auto_pass_auto_gates`, `goal.auto_hire_missing`, `goal.persist_auto_hires`, `goal.max_rounds` | the narrower switches a new goal inherits beneath it |
| Goal | `GoalPolicy` (`--posture`, `--auto-approve/…`, `--auto-hire/…`, `--persist-hires`) | what *this* objective may decide |

A goal inherits the config at creation and then owns its copy. Inheriting rather than consulting the
config live is deliberate: a goal's authority should not change under it because someone edited
`credentials.json` mid-run.

### The posture: one word for "do I have to be here?"

Three overlapping switches had grown for one question — the config's `auto_pass_auto_gates`, the goal's
`human_gate`, the goal's `auto_approve` — and none of them answered it directly. So the authority is
stated once, as the goal's **posture**:

```
posture          who answers each gate
──────────────────────────────────────────────────────────────────────────────────────────
unattended       agent gate: the goal, when it has a route
(default)        policy gate: the goal, only if the config already permits that class
                 human gate: the goal, with evidence present, on the record  ← this is the change
supervised       nobody but you, at every gate
```

`human_gate: true` is kept as a **legacy alias** that resolves to `supervised`, so a `goal.json` written
by an earlier build — and every existing CLI flag and console control — keeps meaning exactly what its
owner asked for.

### What may be released, and what may never

```
gate kind        unattended                          supervised
──────────────────────────────────────────────────────────────────────────────────────────────
human            released, if the four guards hold   parked, always
agent            passed, when it has a route         parked
policy           passed, only if the config permits  parked
mystery          refused                             refused
```

Releasing the *terminal* gate is the decision that lets an unattended goal finish, so it is the most
guarded path in the engine. Four refusals, each emitting its own reason so the trace names which one
fired:

- **The evidence must be present.** The gate declares `requires`; every requirement must resolve —
  `<node>.summary` against the run's node records, anything else against the artifact index. A gate
  that cannot show its evidence parks exactly as it did before autonomy existed. A gate declaring
  *nothing* is treated as incomplete, never as trivially satisfied.
- **No safety control may have fired.** A guardrail block or a contract violation is refused outright.
  Autonomy may decide the work is done; it may not decide that a control which fired was wrong.
- **No node may be blocked.** A blocked node is a stated, concrete failure, and releasing over it would
  record "done" against a run that said it could not proceed.
- **The ledger must accept the record.** The release is written as a decision at the gate's own name.
  If the ledger refuses it, the run parks rather than releasing unrecorded.

Beyond those, the two old refusals still hold: an agent gate with no untried route is left for you
(approving with nothing to act on advances the graph with no corrective action — the definition of an
infinite loop), and an unknown gate kind is never approvable.

And every automatic decision records `by: goal`, never `by: owner`. "The org released this" and "you
released this" are different facts about a run, and conflating them would make the audit trail lie.

### "Create the person if they do not exist"

A plan whose node names a skill nobody holds would otherwise stop three nodes in. With autonomy on,
`prepare` closes the gap *before* binding:

- **An existing holder always wins.** The gap is computed against the real roster first, so a helper is
  only ever created for a capability nobody has.
- **The helper runs on the resolved default pair** — the same one the rest of the org uses.
- **Ephemeral by default.** It does the work and leaves no roster entry to clean up. With
  `persist_hires` it is written to the roster root and reused next run, so the capability accretes
  rather than being re-created each time.
- **The gaps are re-measured after**, so the graph the Owner approves is the graph that will run, not
  the one that was planned before the helpers existed.

An auto-created person is recorded with `origin: goal` (durable) or `origin: ephemeral`, distinct from
`owner`, so the roster and the audit trail can always tell who created whom.

## 4. The flow board: who is on what

Three views of a run, three questions, deliberately not collapsed into one:

| View | Question | Shape |
|---|---|---|
| `status` | *Where is the run?* | a snapshot |
| `activity` | *What happened, in order, and why?* | a story |
| `flow` | *Who is working on what, and what crossed between them?* | a board |

`engine/flow.py` derives the board and **stores nothing**, because the engine already records every
fact it needs and a second store would be a second thing to keep true:

```
run_state.json      the node outcomes (the library runner's checkpoint)
run-context.json    the bindings + the roster in force  → the owner of each node
diagnostics.jsonl   node.bind events                    → the owner when the checkpoint has none
trace.jsonl         handoff.* events                    → what crossed, and what came back
```

A row carries the **owner** (resolved from the run context first, so a resumed run still names who ran
it — a `People.load` mints new ids, so the run's own roster is merged underneath), **in from / out
to**, and the **status, verdict and the runner's own reason** when it is not done. A blank owner is a
node nobody holds: the staffing gap, visible on the board.

Two properties it inherits from `activity.py`, for the same reasons: it is **bounded** (the trace is
read from the tail, so a long run yields a board rather than a file dump) and **every source is
optional** (a fresh workspace yields a calm empty board, not an error). A torn trace line is skipped
rather than breaking the board.

The board is exposed identically to the CLI (`engine.cli flow`) and the protocol (`flow`, and it
travels with `status`), and the app's **Flow** panel is fed from it — so the CLI and the console cannot
disagree about who is working on what.

## 5. What is deliberately not here

- **The board does not predict.** It reports the binding that was made and the handoff that was
  recorded. A node nobody has reached has no owner, and the board says so rather than showing the
  agent who *would* be chosen.
- **Autonomy is per goal, not global.** The config sets the posture a goal inherits; the goal owns its
  own authority. A global switch would make "this one objective, with you involved" impossible to
  express — and `supervised` is that floor, in one word, asserted by `check_autonomy_floor` rather
  than documented.
- **Auto-hiring does not recurse.** A helper created to fill a gap cannot itself create another; the
  delegation desk's depth cap and the tier ceiling (`goal.auto_hire_max_tier`) both still apply. The
  gaps are filled once, from the plan's own skill list.
- **The tier ceiling bounds a real classification, not a label.** The helper the engine creates reads
  anywhere in the project and writes inside it, which the delegation desk's own `classify_tier` scores
  as the safest tier — so the default cap admits it and auto-staffing works. A helper that would land
  above the cap is not created: the gap is reported to you with the tier it would have reached and the
  reason, so it arrives as work to staff rather than as a silent hire. Because the cap is computed from
  the same capability definition the helper is created with, the two cannot drift apart.
- **The default is one pair, not a policy per role.** Per-agent binding (`hire --provider --model`,
  the People panel) is how you make a reviewer run on a different model; `defaults` is the *floor*
  everything else inherits.

## 6. Shipped as

| Surface | Where |
|---|---|
| `DefaultsConfig`, `Config.default_pair` | `engine/config.py` |
| `set_defaults`, `set_autonomy` (atomic, `0600`) | `engine/config.py` |
| `[goal]` posture, autonomy settings and `max_rounds` | `engine/config.py` |
| `Posture`, `GoalPolicy`, `Goal.policy` | `engine/goal.py` |
| `_auto_pass`, `_release_terminal_gate`, `_gate_evidence`, `_auto_staff`, `prepare(auto_staff=…)` | `engine/orchestrator.py` |
| `CacheStore` (durable prefix/shape/savings) | `engine/cachestore.py` |
| `build_flow`, `FlowRow`, `FlowHandoff` | `engine/flow.py` |
| `Handoff` + `validate_handoff` (the typed contract, now on every edge) | `engine/org/handoff.py`, `engine/executor.py` |
| `defaults`, `flow`, `defaults_set`, `autonomy_set` | `engine/cli.py`, `engine/serve.py`, `engine/protocol.py` |
| The **Flow** panel, the **Defaults** editor, the goal posture controls | `macos/Sources/AgentOrg/` |
| Tests | `tests/test_phase28_defaults_autonomy_flow.py`, `tests/test_phase29_handoff_wiring.py`, `tests/test_phase30_cachestore.py` |
