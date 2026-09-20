# Architecture

How the code is organised, and which layer owns which invariant. Read this before changing anything.

For the reasoning behind these choices, see [DESIGN.md](DESIGN.md). For the context, memory,
telemetry and evaluation subsystems in depth, see [EVALUATION.md](EVALUATION.md). This document is
the map.

---

## The five rings

Dependencies point **downward only**. This is what makes the recovery loop testable with no network
and no Swift: the provider layer sits behind an abstract base and the UI behind an event stream.

```
┌───────────────────────────────────────────────────────────────────────────┐
│ RING 5 · NATIVE SHELL        SwiftUI + Process/Pipe          (built)      │
│   knows: NDJSON events.  knows nothing about: prompts, HTTP               │
├───────────────────────────────────────────────────────────────────────────┤
│ RING 4 · GATEWAY             provider adapters, routing      (built)      │
│   knows: HTTP wire formats.  knows nothing about: phases                  │
├───────────────────────────────────────────────────────────────────────────┤
│ RING 3 · ORCHESTRATION   graph, loops, gates, CLI, evals     (built)      │
│   knows: tasks, artifacts, contracts.  owns: control flow                 │
├───────────────────────────────────────────────────────────────────────────┤
│ RING 2 · ORG MODEL           agents, routing, delegation     (built)      │
│   knows: who exists, who reports to whom, who may do what                 │
├───────────────────────────────────────────────────────────────────────────┤
│ RING 1 · LIBRARY ADAPTER     SKILL.md → SkillBundle          (built)      │
│   knows: the library's frontmatter and checklist grammar                  │
└───────────────────────────────────────────────────────────────────────────┘
   cross-cutting ── protocol · bus · state · secrets · budget · health
```

## Module map

```
AgentOrg/
├── README.md · USAGE.md · TROUBLESHOOTING.md · ARCHITECTURE.md · OPERATIONS.md
├── DESIGN*.md                 the design record (master + ten amendments)
├── credentials.example.json   committed template
├── credentials.json           gitignored, 0600
├── run_tests.py               the dependency-free test runner
├── pytest_shim.py             the minimal pytest surface that runner needs
│
├── engine/
│   ├── cli.py                 the command surface (doctor, skills, models, plan, org, delegation)
│   ├── __main__.py            so `python3 -m engine` works
│   │
│   ├── config.py              config load, validation, redaction, leak scan,
│   │                          and the one atomic provider writer (0600)
│   ├── library.py             pin + hash-verify the Skills library
│   ├── protocol.py            ★ the NDJSON event/command contract
│   ├── bus.py                 event fan-out, ring buffer, trace.jsonl
│   ├── state.py               workspace layout + the run checkpoint
│   ├── artifacts.py           atomic, contained, hashed artifact I/O
│   ├── versioning.py          schema versions, migration, refusal
│   ├── idempotency.py         the effect journal (exactly-once side effects)
│   ├── resources.py           machine detection → the concurrency ceiling
│   │
│   ├── tokens.py              estimator + calibration
│   ├── catalog.py             live model discovery with provenance
│   ├── gateway.py             ★ the model-agnostic entry point
│   ├── rpc.py                 the gateway as a service over a Unix socket
│   ├── providers/
│   │   ├── base.py            canonical types + the Provider ABC
│   │   ├── http.py            one transport: retries, backoff, SSE
│   │   ├── openai.py          OpenAI / DeepSeek / LM Studio / vLLM
│   │   ├── anthropic.py       native /v1/messages
│   │   ├── ollama.py          native /api/chat
│   │   ├── fake.py            the deterministic test double
│   │   └── registry.py        config → adapters
│   │
│   ├── skills/
│   │   ├── frontmatter.py     the strict YAML-subset parser
│   │   ├── bundle.py          ★ SKILL.md → SkillBundle
│   │   ├── filesystem.py      read from the pinned library
│   │   ├── graph.py           ★ the library's chain: dependency graph
│   │   ├── roles.py           ★ verifier-vs-producer, derived from the skill
│   │   └── source.py          the SkillSource protocol
│   │
│   ├── prompts.py             ★ checklist-enforcing prompt assembly
│   ├── planner.py             ★ goal → validated manifest
│   │
│   ├── org/
│   │   ├── agent.py           AgentSpec, AgentRuntime, Budget
│   │   ├── roster.py          Org, Team, default_company
│   │   ├── mailbox.py         per-agent message log
│   │   ├── binding.py         node → agent (pinned/round-robin/load-balanced/swarm)
│   │   ├── policy.py          autonomy, route classes, the safety floor
│   │   ├── handoff.py         contract lifecycle + rules R1–R8
│   │   ├── ledger.py          the decision gate ledger
│   │   ├── router.py          filters, scoring, the confidence gate
│   │   ├── delegation.py      the reuse-first ladder, requisitions, S1–S6
│   │   ├── scheduler.py       admission, ceilings, backpressure, watchdog
│   │   ├── health.py          golden signals, graduated states, probes
│   │   └── slo.py             objectives, burn rate, alerts
│   │
│   ├── context/               session · projection · compaction
│   │                          rotation · assembly                (built)
│   ├── memory.py              run memory + the poisoning guard   (built)
│   ├── telemetry.py           OTel-shaped spans                  (built)
│   ├── diagnostics.py         correlated logging, health, bundle (built)
│   ├── evals/                 behavioural suite + baseline gate  (built)
│   ├── executor.py            the runner plugin                  (built)
│   ├── guardrail.py           the classify() hook                (built)
│   ├── host.py                runner subprocess supervision      (built)
│   ├── orchestrator.py        ★ lifecycle, gates, resume         (built)
│   ├── runcontext.py          the handoff across that boundary   (built)
│   ├── pool.py                the pull-based task pool           (built)
│   ├── chat.py                the conversational front door      (built)
│   ├── people.py              hiring, and the merged roster      (built)
│   ├── authoring.py           writing your own skills            (built)
│   ├── usercfg.py             where your content lives           (built)
│   ├── serve.py               the NDJSON server the console drives (built)
│   ├── cache.py               prefix-shape hashing + miss attribution (built)
│   ├── prefix.py              the pinned prefix + its invariant  (built)
│   ├── pinning.py             holding it fixed for a run          (built)
│   ├── decompose.py           a goal → a validated fan-out        (built)
│   ├── fanout.py              template × items → N subagents      (built)
│   ├── subagents.py           isolated child contexts + transcripts (built)
│   ├── goal.py                the durable objective + its verdict  (built)
│   ├── mission.py             the standing purpose above the goal   (built)
│   ├── portfolio.py           the principal and the orgs they run   (built)
│   ├── fleet.py               several orgs running at once, bounded (built)
│   ├── activity.py            "what is happening" — one derived timeline (built)
│   ├── flow.py                "who is working on what" — the org board (built)
│   ├── improver.py            detect/draft/validate/promote — never applies (built)
│   ├── tools.py               read/list/search/write + the gate   (built)
│   └── agentloop.py           the bounded tool-calling loop       (built)
│
├── macos/                     the native console                 (built)
├── tests/                     1298 tests across twenty-one phases
└── projects/                  user workspaces (gitignored)
```

★ marks the three files named in the original brief.

## The invariants each layer owns

This is the useful part of the map: when a rule matters, exactly one layer is responsible for it.

| Layer | Owns | Enforced by |
|---|---|---|
| `library.py` | The skill corpus cannot change under us | A recorded commit + content-manifest pin, compared on every resolve; the engine reports which of the two facts it actually checked |
| `config.py` | No secret reaches a log or a file | Env-first resolution, a redactor, a leak scan; a provider write merges one entry, sets `0600` before the secret, and never creates the file |
| `protocol.py` | The engine and the app cannot silently disagree | A versioned schema; unknown types tolerated, malformed frames refused |
| `state.py` / `artifacts.py` | No torn write, no path escape | Temp + `os.replace`, resolved-path containment |
| `versioning.py` | An old build never misreads a new workspace | Versions on every artifact; a newer major is refused |
| `idempotency.py` | No side effect is applied twice | An effect journal keyed by a stable identity |
| `skills/frontmatter.py` | A contract is never silently dropped | A strict subset; out-of-subset input refused with a line number |
| `skills/bundle.py` | A node always has criteria | Three documented sources, then a refusal |
| `prompts.py` | No checklist id is skippable | Every id named; PASS/FAIL/N/A **with evidence** required |
| `planner.py` | Every graph terminates, and the org matches the goal | The library's validator plus a bounded loop and a reachable gate; a per-domain composition |
| `activity.py` | A run's state is always explainable | Reads the artifacts the engine already writes; a missing source is skipped, never an error |
| `flow.py` | "Who is on what" is never inferred from names | Reads the run context's bindings, the `node.bind` diagnostics and the `handoff.*` events; a node nobody holds shows a blank owner |
| `goal.py` (policy) | An automatic gate decision is never confused with yours | `GoalPolicy` per objective; a terminal gate is never passed; every auto-approval records `by: goal` |
| `orchestrator.py` (`_auto_staff`) | A missing capability never stalls a run on a roster accident | An existing holder always wins; the helper runs on the resolved default and is ephemeral unless `persist_hires` |
| `mission.py` | A mission never spends, and never contradicts its own list | State is derived from the objectives; arming is separate from starting a goal; load disarms |
| `skills/roles.py` | The binder and the planner agree on who judges | One derived answer, not two hardcoded sets |
| `portfolio.py` | A register never runs or spends | It points at orgs; execution and budget stay one level down |
| `fleet.py` | N orgs cannot over-subscribe the machine or the budget | One global ceiling (the scheduler's own) + a per-org daily budget |
| `serve.py` (bootstrap) | A dead engine is never reported as running | `engine.ready` is the readiness proof; a fatal bootstrap failure is a typed `error` frame on stdout |
| `binding.py` | A reviewer is never its own producer | The independence refusal, plus model preference |
| `policy.py` | No setting disables every human gate | The safety floor, with an explicit opt-in to cross it |
| `handoff.py` | Corrupt or lossy state never propagates | Rules R1–R8, each named on violation |
| `ledger.py` | An override is never silent | Superseding requires a rationale and marks the prior entry |
| `delegation.py` | A delegation tree cannot outspend or outrun its root | S1–S6 |
| `scheduler.py` | A hang cannot consume a slot forever | The watchdog and reclaim |
| `health.py` | Noise never quarantines; a severe fault never hides | The minimum-sample guard, hard triggers, probe recovery |
| `slo.py` | An exhausted budget is never reported as fine | Burn-rate bands plus a budget-floor escalation |
| `context/session.py` | Turns are taken only at a boundary, on an active session | The lifecycle refuses a turn on a sealed session |
| `context/projection.py` | Rotating is never attempted when it cannot help | The irreducible/reducible split, with the fix named |
| `context/compaction.py` | A pinned constraint is never lost | AR-04's count check reverts the whole compaction |
| `context/rotation.py` | A rotation cannot storm or corrupt | The cap, the no-progress guard, and the checksum |
| `context/assembly.py` | Guardrails are read, not buried | Re-pinning to the primacy zone, verified structurally |
| `memory.py` | Recall is never obeyed as instruction | The context-only label and provenance on every entry |
| `telemetry.py` | An unmeasured run is never called free | `usage_reported` and `cost_measured` flags |
| `diagnostics.py` | A shared bundle never carries a secret | Every source scanned before packaging |
| `evals/runner.py` | A judgment regression cannot merge quietly | A frozen baseline and a delta gate |

## The one seam: the protocol

Everything else can be rewritten on one side without the other noticing. That is why the protocol is
defined first and separately in `protocol.py`.

- **`stdout` carries NDJSON events only.** Diagnostics go to `stderr`. This is what lets the app and
  the engine be built and tested independently, and what the RPC layer relies on.
- **Every event has a `seq`** assigned by the bus, so ordering is a property of one place rather than
  a convention every caller must remember.
- **Every command has a `cmd_id`**, echoed in its `command.ack`. Without it the UI cannot correlate a
  reply.
- **An unknown event type is tolerated**, but a malformed frame is refused. Forward compatibility is
  free; ambiguity is not.
- **Frames are size-capped**, because a runaway payload usually means an artifact body was inlined
  instead of referenced.

## Why the library is a dependency, not a text file

The engine reads the library as *data* and never executes it, but it is still a dependency in the
supply-chain sense: a modified `SKILL.md` is modified system-prompt content. So:

- the root is pinned to a **commit**
- 445 files are hashed into a **manifest**, verified at startup
- the exact **CLI capabilities** the engine calls are asserted present, so a dropped `--guardrail`
  fails loudly rather than silently disabling handoff safety
- **every span records the skill's content hash**, so "which prompt produced this output?" is
  answerable

## The test strategy

1482 tests, all offline. The point is that the interesting failures are not crashes.

| Suite | Tests | What it proves |
|---|---|---|
| `test_phase1_foundation.py` | 111 | Pinning, config refusals, the protocol round-trip, containment, schema refusal, effect replay, and the self-healing config (a stale provider reference is repaired, not fatal) |
| `test_phase2_gateway.py` | 90 | Every adapter's wire shape, retry/backoff, SSE reassembly, catalog provenance, cost labelling |
| `test_phase2_rpc.py` | 22 | The socket seam: privacy, error propagation, path-length handling |
| `test_phase3_skills.py` | 72 | The parser agreeing with PyYAML **on all 327 skills**, three criteria fallbacks, checklist ids |
| `test_phase3_prompts.py` | 52 | Checklist enforcement, attention placement, trailer parsing |
| `test_phase3_planner.py` | 67 | Validation, termination invariants, Safe-YAML round-trip, and the domain classification that chooses the org (strategy/gtm/research/data vs software) |
| `test_phase4_org.py` | 147 | Every org, routing, delegation, health and scheduler refusal, plus an end-to-end pass |
| `test_phase4_cli.py` | 64 | Every command the documentation tells you to run, plus `--project`/`--root` roster and skill resolution |
| `test_phase5_context.py` | 79 | The band thresholds, attention decay, priority eviction, AR-04 revert, rotation guards |
| `test_phase5_memory.py` | 68 | The poisoning guard, consolidation, span honesty flags, sampling, the bundle leak refusal |
| `test_phase5_evals.py` | 25 | The behavioural suite is sound: every scenario implemented, the gate blocks |
| `test_phase6_executor.py` | 39 | The runner plugin: node execution, contract checks, the effect journal, cost labelling |
| `test_phase6_orchestrator.py` | 41 | Lifecycle, gates, resume, reassignment, takeover, why a run stopped, and the run the UI reads |
| `test_phase7_chat.py` | 18 | The conversational loop: every command has a handler, cost is never rendered free, a failed turn is not kept |
| `test_phase8_authoring.py` | 21 | The layered roots, authoring an enforceable skill, hiring with its refusals and persistence |
| `test_phase9_swarm.py` | 20 | A swarm votes and the majority decides; the run context carries roster, bindings and skill roots |
| `test_phase10_pool.py` | 26 | Capability routing, lease expiry, offers with reasons, dependencies, schema-checked completions |
| `test_phase11_serve.py` | 44 | The console's transport: every command acked, stdout is protocol-only, stdin EOF exits cleanly |
| `test_phase12_cache.py` | 45 | Cache parsing per dialect, cache-aware cost, miss attribution, and the 86–90% stable prefix |
| `test_phase13_fanout.py` | 24 | Fan-out refusals before any call, bounded batches, error isolation, and that a fan-out is not a vote |
| `test_phase14_tools.py` | 49 | Tools, the capability gate, path containment, and the bounded agentic loop |
| `test_phase15_decompose.py` | 22 | A goal becomes a swarm: the lead explores, can decline, and its items are grounded |
| `test_phase16_attach.py` | 14 | Attaching a real folder: the path is the folder, `docs/`/`src/` are never created, `.agent_state/` and `.git/` are out of reach |
| `test_phase17_goal.py` | 30 | The durable goal: loaded **disarmed**, completion is the agent's call, the budget slices rather than resets |
| `test_phase18_subagents.py` | 36 | Isolated children: own session, shared prefix, durable transcripts, byte paging that reports truncation, depth and budget bounds |
| `test_phase19_providers.py` | 38 | Custom headers on every dialect, and the credentials writer: merge not replace, mode 0600, never invent a path |
| `test_phase20_roster.py` | 26 |
| `test_phase21_improver.py` | 31 | The self-improvement loop: measurement not opinion, a baseline delta as the only proof, and the safety boundary that refuses anything touching its own judging machinery | Hiring and editing: the id (and so the history) survives an edit, save and load agree on one path |
| `test_phase26_portfolio.py` | 24 | The register: the principal, org identity that survives a rename, and a portfolio that runs and spends nothing |
| `test_phase27_fleet.py` | 19 | Several orgs at once: lazy load, per-org isolation, the global ceiling and per-org budget, and every refusal named |
| `test_phase22_activity.py` | 12 | The "what is happening" report: a blocked run explains itself, a gate becomes a decision, the timeline is bounded and deduped, a fresh workspace reads calmly |
| `test_phase23_mission.py` | 30 | The mission: one active objective, derived state, advance, disarm-on-load, and autonomous sync from a goal's verdict |
| `test_phase24_skill_graph.py` | 17 | The library's chain graph: edges, closures, ordering with cycles reported, and a calibrated plan review |
| `test_phase25_skill_roles.py` | 27 | Verifier vs producer derived from the skill (security-engineer produces, security-reviewer judges) |

Plus **17 behavioural scenarios** in `engine/evals/`, scored against a frozen baseline with a gate that
blocks a regression — see the README's testing section for why that is a separate suite.

The `macos/` package adds **130 Swift tests** (`swift test`) over the process bridge, protocol models,
log store and the safe file writer. They live in the kit rather than a UI test so the parts that must
not be guessed at stay exercisable without launching the app.

Three tests are worth reading as documentation in their own right:

- `test_strict_parser_agrees_with_pyyaml_on_the_whole_library` — the stdlib parser is byte-identical
  to PyYAML across the corpus, so behaviour cannot depend on whether PyYAML is installed.
- `test_calibration_converges_and_shrinks_error` — the estimator improves rather than drifting,
  which is what makes the rotation thresholds trustworthy.
- `test_hard_trigger_overrides_a_healthy_average` — ten successes do not save an agent that has
  tripped three consecutive breaches.

## Extending it

**A new provider.** Subclass `Provider`, declare `kind`, translate to and from the canonical types,
and add a branch in `providers/registry.py`. Then add a golden-body test — the translations that fail
are the quiet ones.

**A new skill source.** Implement `SkillSource` (four methods). The prompt builder and the executor
do not change.

**A new route class.** Add it to `RouteClass`, decide its `SAFETY_FLOOR` status deliberately, and add
it to `DEFAULT_POLICY`. The floor is the decision that matters.

**A new engine stage.** Add a subcommand in `cli.py` and a test file. The CLI is the contract for
what the engine can do, so a capability with no command is a capability nobody can use.

## Sequencing, and why

The design's own argument for building bottom-up:

1. **Phases 1–4 (done)** make the engine inspect and plan a real project from the CLI, with no Swift
   involved. Everything here is verifiable in isolation.
2. **Phase 5 (done)** adds the session lifecycle and the behavioural eval suite — the part that makes
   judgments testable rather than merely asserted. With it, the engine's decisions are *provable*:
   a constraint survives rotation, a reviewer is never its own producer, and a regression in either
   fails the gate.
3. **Phase 6 (done)** adds the executor, the host and the console. It came last because a UI over an
   unproven engine is a UI that hides bugs — and because by then the engine had something to prove
   itself against while it was built.

The engine drove a project before any Swift was written, so the native layer rests on something
already shown to work.
