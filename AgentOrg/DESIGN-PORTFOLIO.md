# AgentOrg — Design: The Portfolio (One Person, Several Orgs)

How one principal runs many organisations at once — each with its own agents, missions, goals, budget
and risk — without any of them touching another.

Sixth of the amendments:

| Amendment | Question it answers |
|---|---|
| [`DESIGN-WORKSPACE.md`](DESIGN-WORKSPACE.md) | *Which folder is the code?* |
| [`DESIGN-GOAL.md`](DESIGN-GOAL.md) | *What does "keep going" mean?* |
| [`DESIGN-SUBAGENTS.md`](DESIGN-SUBAGENTS.md) | *How is parallel work isolated and read back?* |
| [`DESIGN-ACTIVITY.md`](DESIGN-ACTIVITY.md) | *What is it doing, why, and is the org the right one?* |
| [`DESIGN-AUTONOMY.md`](DESIGN-AUTONOMY.md) | *What is it all for, and who decides when it is stuck?* |
| **DESIGN-PORTFOLIO.md** (this) | *Can one person run several orgs at once?* |

See also [`AUTONOMY-MAP.md`](AUTONOMY-MAP.md) for the end-to-end picture.

---

## 1. The problem

The engine's unit was a **workspace**: one project folder, one org, one roster. That is correct for a
project and wrong for a *person*. In the real world one principal runs several organisations at once —
a CEO who is also a founder, a CTO of a second company, the chair of a foundation — and each has its
own agents, missions, goals, budget and risk. The person's only option was to keep several folders and
remember which was which, because nothing modelled *the person* or *the set of orgs*.

Three things were missing:

1. **A layer above the org.** There was a Goal and a Mission, but nothing that owned *several* orgs.
2. **A person.** `OWNER_ID = "ag_owner"` was hardcoded into every org with the same id, so there was no
   shared principal — no way to say the CEO of one company is the same human as the founder of another.
3. **Org identity.** `Org.name` was a display string defaulting to `"AgentOrg"`. A rename would have
   been indistinguishable from a different org, so nothing could safely point at one.

## 2. The shape

```
Principal   the human — one identity across every org            ~/.agentorg/portfolio.json
  └ OrgEntry  a named org the principal runs    → workspace + roster + missions/goals/runs + budget
```

A **Portfolio** is a register: who the principal is, which orgs they run, where each lives, and what
policy each carries (enabled, a per-org daily budget). It is deliberately **inert** — it does not plan,
route, run or spend. Execution stays in `Orchestrator` (one per org), and the identity of an agent
stays inside its own org. Keeping the register inert is what stops it becoming a second control plane.

**Org = Workspace.** The isolation boundary already existed and was already the right one: `Org` is a
constructor argument to `Binder`, `Router`, `Planner`, `HiringDesk`, `NodeExecutor` and `Run` (109
references, all through `self.org`, never a global singleton). So an org is its folder and its roster,
and the portfolio stores a **pointer** to that folder, never a copy. A second copy of a roster would be
a second thing to keep true.

## 3. Identity, so a rename is not a new org

An `Org` gained two fields:

- **`id`** — a stable identity, generated once (`org_<slug>`), never re-derived. A rename changes the
  label, not the identity, so the roster, missions, goals and spend ledger are never orphaned.
- **`principal_id`** — the human who owns it. Several orgs share one principal id; their rosters,
  budgets and mailboxes never touch. That is the **shared principal, independent agents** split.

Both are additive: `Org.from_dict` fills them from a file that has them and leaves them empty for a
roster written before they existed, so an old roster still opens.

## 4. The fleet: several orgs at once, bounded

A `Fleet` (in `engine/fleet.py`) turns the register into live orchestrators — **one per org, built
lazily** — and lets several run at the same time, each on its own thread. That concurrency is the whole
point: a person with three companies does not work them one at a time, and neither should their orgs.

Two ceilings make it safe, and they exist precisely **because** the orgs are now independent:

1. **A global concurrency ceiling.** The orgs share one machine, one set of providers and one budget.
   Without a cap, N orgs each starting a run would each start their own swarm and the sum melts the
   machine. The ceiling is the *same* machine-derived number the scheduler already computes
   (`resources.derive_ceiling`) — reused, not reinvented, so the fleet can never promise more
   concurrency than the machine holds.
2. **A per-org daily budget.** The global budget in `credentials.json` bounds the *whole* principal; a
   per-org ceiling (`OrgEntry.daily_budget_usd`) stops one runaway org consuming the principal's entire
   allowance before the others get a turn.

Isolation is by construction: each orchestrator owns its workspace, bus, ledger, diagnostics and org.
The fleet adds no shared mutable state except the two ceilings and the thread registry, so "org A's
agents never see org B" is true without a single new isolation check.

**The fleet never spends on its own.** It runs work an org's *goal* already authorised; it has no arm,
no budget of its own to grant, and no way to start a mission. Every refusal is named — a disabled org,
a missing folder, the ceiling, the budget — so a person can act on it rather than guess.

## 5. Surfaces

**CLI.** A `portfolio` command tree (`init`/`status`/`add`/`use`/`update`/`remove`/`show`/`run`/`stop`)
plus one flag that scopes *every existing command* to an org:

```bash
engine.cli portfolio init "Elon Musk"
engine.cli portfolio add Tesla --slug tesla --path ~/work/tesla --daily-budget-usd 50
engine.cli portfolio add SpaceX --slug spacex --path ~/work/spacex
engine.cli portfolio status --live                 # every org's mission, spend and blockers
engine.cli run --goal "add pagination" --org tesla # any command, scoped to one org
engine.cli portfolio run spacex "close the launch checklist"
```

`--org` resolves the org's folder through the *same* resolver `--project` uses, which is what keeps
every existing command org-scoped without a second set of commands. It wins over `--project` only
because it is the more explicit way to name the same thing.

**The app.** A **Portfolio** panel — first in the sidebar, because the whole picture precedes any single
org — lists the orgs with their mission, spend and blockers, and offers Run / Stop / Switch per org. It
badges the sidebar when *any* org has work waiting on you. The register travels with the status poll;
the *live* cross-org picture is a separate, deliberate fetch, because gathering it builds an
orchestrator per org and the cost is wanted exactly when the panel is open.

**Serve.** The NDJSON server holds the portfolio and one persistent `Fleet`, and exposes
`portfolio`, `portfolio_live`, `portfolio_run`, `portfolio_stop`, `portfolio_select`, `portfolio_add`
and `portfolio_remove`. The fleet is held by the server rather than rebuilt per command because the
fleet *is* the concurrency: an org running on its thread must survive the next command.

## 6. What is deliberately not here

- **No cross-org agent sharing.** The design chose *shared principal, independent agents*: an agent
  belongs to one org. An "agency model" where a specialist serves several orgs was considered and
  declined — it would require per-org budget, health and mailbox accounting for the same agent id,
  which is a second model of identity, not a small addition.
- **No cross-org budget pooling.** Each org keeps its own ledger; the portfolio *rolls up* spend but
  does not pool it. A pooled budget would make one org's overspend another's problem, which is exactly
  what a per-org ceiling prevents.
- **No org hierarchy.** Orgs are peers under one principal. A holding company that owns other orgs is a
  further level and is not built.
- **The portfolio does not run anything.** Stated first and again here because it is the load-bearing
  safety property: a register that could start work would be an unattended spend with a friendly name.
