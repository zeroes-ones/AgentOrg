#!/usr/bin/env python3
"""fanout.py — the second swarm primitive: split work across N agents, rather than vote on one thing.

WHY THIS EXISTS
---------------
The engine has two genuinely different notions of "many agents", and conflating them would be wrong:

- **Vote** (`BindingPolicy.SWARM`): N agents answer *one* question and the majority decides. This is
  what makes a reviewer trustworthy — the primitive exists for independent judgment.
- **Fan-out** (this module): N agents each do a *different* piece of one job, in parallel. The
  primitive exists for throughput — reviewing twenty files, migrating thirty call sites, researching
  ten libraries.

They answer different questions ("is this right?" vs "do all of this"), so they are separate. This is
the fan-out half, modelled on the `AgentSwarm` pattern: one `prompt_template` containing `{{item}}`,
plus an `items` array, expands to one concrete task per item.

DESIGN
------
- **`{{item}}` is mandatory and enforced.** A template without it would send N byte-identical prompts,
  which is N times the cost for one answer — the failure mode that makes fan-out look useless. It is
  refused before any agent starts, not after.
- **An item count of one is refused.** A "swarm" of one is a sequential call wearing a label.
- **Expanded prompts must be distinct.** Two items that expand to the same prompt are the same
  duplicate failure one level down, and they are rejected with the colliding item named.
- **Bounded and queued.** Work is split into batches of `max_parallel`, so a 128-item fan-out does not
  open 128 concurrent model calls and melt a local provider. The bound is explicit and reported.
- **One item failing does not lose the others.** A fan-out is for independent work; a single failure
  is recorded against its item and the rest still run, because discarding 19 good results over one
  bad one is worse than reporting the one.
- **Order is preserved in the result, not in the execution.** Results come back indexed by item, so a
  caller can zip them against the input regardless of which finished first.
- **The rate-limit signal is typed, never read out of prose.** A limit is a fact about the transport,
  so it arrives either as a typed exception (`GatewayError.kind`) or as an explicit flag on the tuple
  the caller returns. Matching the words "429" or "rate limit" in the text an item *reported* is a
  different thing entirely: that text is the model's own summary, and a reviewer writing "line 429
  changed" once cost a full re-execution of an item that had already finished.

Usage:
    plan = plan_fanout("Review {{item}} for regressions.", items=["src/a.ts", "src/b.ts"])
    for batch in plan.batches(max_parallel=4):
        ...
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "FanoutError", "FanoutItem", "FanoutPlan", "ItemOutcome", "plan_fanout", "run_fanout",
    "PLACEHOLDER", "MAX_ITEMS", "ConcurrentSlot", "run_wave",
]

#: The one placeholder a template may use. Exactly this spelling.
PLACEHOLDER = "{{item}}"

#: The ceiling a plan will accept. High enough for a genuine bulk job, low enough that a runaway
#: decomposition is caught as a mistake rather than executed.
MAX_ITEMS = 128


class FanoutError(RuntimeError):
    """A fan-out that cannot be honoured, named so the caller can fix it before it costs anything."""


@dataclass
class ItemOutcome:
    """What one item's call produced, with the signals the queue is *allowed* to act on.

    `run_one` may return this instead of the legacy `(output, error, tokens)` tuple. It exists so a
    caller can report a provider limit without either raising or writing a magic word into `error`:
    a limit that has to be *inferred from the error text* is a limit that a model's own summary can
    forge, which is how a successful item came to be re-executed in full.

    `artifacts` and `usage` are carried per item for the same reason `outcome` is a type rather than
    three positional values: a fan-out node's result is the sum of its items, and anything the items
    know that the tuple cannot express is silently dropped on aggregation.
    """

    output: str = ""
    error: str = ""
    tokens: int = 0
    #: True only when the *transport* refused the call. Never derived from `error`'s text.
    limit_hit: bool = False
    #: Artifact references this item produced, in the same shape the executor's node results use.
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    #: The item's own usage counters, so a fan-out node can add them up instead of zeroing them.
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class FanoutItem:
    """One expanded unit of fan-out work."""

    index: int
    item: str
    prompt: str
    #: Filled in by the run: the agent that handled it, its output, and any failure.
    agent_id: str = ""
    output: str = ""
    error: str = ""
    tokens: int = 0
    #: Filled in by the run when the caller reports them, so the fan-out's aggregate can carry what
    #: its items actually produced rather than a zeroed placeholder.
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "item": self.item, "prompt": self.prompt,
            "agent_id": self.agent_id, "output": self.output, "error": self.error,
            "tokens": self.tokens, "ok": self.ok, "artifacts": list(self.artifacts),
            "usage": dict(self.usage),
        }


@dataclass
class FanoutPlan:
    """A validated expansion of a template over items, ready to run."""

    template: str
    items: list[FanoutItem] = field(default_factory=list)
    skill: str = ""
    #: The subagent profile the whole swarm should use, when the caller names one.
    agent_profile: str = ""

    def __len__(self) -> int:
        return len(self.items)

    def batches(self, *, max_parallel: int = 4) -> list[list[FanoutItem]]:
        """Split into batches no larger than `max_parallel`.

        Batching rather than launching everything at once is what keeps a bulk fan-out usable on a
        provider with a small concurrency limit — and a plan that cannot be run is not a plan.
        """
        size = max(1, int(max_parallel))
        return [self.items[i:i + size] for i in range(0, len(self.items), size)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "template": self.template, "skill": self.skill, "agent_profile": self.agent_profile,
            "count": len(self.items), "items": [i.as_dict() for i in self.items],
        }

    def summary(self) -> dict[str, Any]:
        """The run's outcome: how many succeeded, and which failed and why.

        `artifacts` and `usage` are aggregates over the successful items rather than zeros: a node
        whose result is the sum of its items has to be able to *say* what those items produced, and a
        handoff that reports `artifacts: []` for a fan-out that wrote twenty files is a downstream node
        being told there is nothing to consume.
        """
        failures = [{"index": i.index, "item": i.item, "error": i.error}
                    for i in self.items if not i.ok]
        artifacts = [ref for i in self.items if i.ok for ref in i.artifacts]
        usage = {
            "tokens_in": sum(int((i.usage or {}).get("tokens_in") or 0)
                             for i in self.items if i.ok),
            "tokens_out": sum(int((i.usage or {}).get("tokens_out") or 0)
                              for i in self.items if i.ok),
            "cost_usd": sum(float((i.usage or {}).get("cost_usd") or 0.0)
                            for i in self.items if i.ok),
        }
        return {
            "count": len(self.items),
            "succeeded": len(self.items) - len(failures),
            "failed": len(failures),
            "failures": failures,
            "tokens": sum(i.tokens for i in self.items),
            "artifacts": artifacts,
            "usage": usage,
            "complete": not failures,
        }


def plan_fanout(template: str, items: Sequence[str], *, skill: str = "",
                agent_profile: str = "", resume: Iterable[str] = ()) -> FanoutPlan:
    """Validate a template and expand it over the items.

    Every rule here is checked *before* anything runs, because each of them costs real tokens to
    discover at runtime: a missing placeholder means N duplicate calls, and N items of which two are
    identical means a duplicated call nobody asked for.
    """
    text = str(template or "")
    if not text.strip():
        raise FanoutError("a fan-out needs a prompt template")

    resume_items = [str(r) for r in resume]
    item_list = [str(i) for i in items]

    if not item_list and not resume_items:
        raise FanoutError("a fan-out needs at least one item, or one resume id")
    if item_list and PLACEHOLDER not in text:
        raise FanoutError(
            f"the prompt template must contain {PLACEHOLDER}; without it every subagent would "
            "receive the same prompt, which is N times the cost for one answer"
        )
    if len(item_list) == 1 and not resume_items:
        raise FanoutError(
            "a fan-out of one item is a sequential call with extra machinery; run it directly"
        )
    if len(item_list) > MAX_ITEMS:
        raise FanoutError(
            f"{len(item_list)} items exceeds the {MAX_ITEMS} ceiling; split the job into several "
            "fan-outs so a runaway decomposition is caught rather than executed"
        )

    expanded: list[FanoutItem] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(item_list):
        if not item.strip():
            raise FanoutError(f"item {index} is empty, so its subagent would have no work")
        prompt = text.replace(PLACEHOLDER, item)
        # The same check one level down: two different items that expand to one prompt are a
        # duplicate call, and naming the collision is what makes it fixable.
        if prompt in seen:
            raise FanoutError(
                f"items {seen[prompt]} and {index} expand to the same prompt; each subagent needs a "
                "distinct piece of work"
            )
        seen[prompt] = index
        expanded.append(FanoutItem(index=index, item=item, prompt=prompt))

    return FanoutPlan(template=text, items=expanded, skill=skill, agent_profile=agent_profile)


def run_fanout(plan: FanoutPlan, run_one: Callable[[FanoutItem, str], Any],
               *, agents: Sequence[str], max_parallel: int = 4,
               on_event: Callable[[str, dict[str, Any]], None] | None = None) -> FanoutPlan:
    """Run a plan through a bounded, *adaptive* queue.

    `run_one(item, agent_id)` is injected rather than implemented here, so this module owns the
    *fan-out policy* (validation, scheduling, error isolation, ordering) and the caller owns *how a
    call is made*. That split is what lets the same plan drive the real executor and a test without
    either one knowing about the other.

    It may return an :class:`ItemOutcome`, a 4-tuple `(output, error, tokens, limit_hit)`, or the
    legacy 3-tuple `(output, error, tokens)`; see `_outcome_of`. Only the typed flag — or an exception
    raised by the call — can mean "the provider pushed back". The error *string* an item reports is
    the model's own summary and is never scanned for magic words.

    Why a queue rather than `for batch in plan.batches()`: a fixed batch assumes the provider's
    concurrency never changes, and it does — a 429 means the next batch of four will also be refused,
    so the retry storm costs N refusals instead of one. This scheduler instead:

    - launches up to `max_parallel` at a time;
    - **halves the limit on a rate-limit signal** and re-queues the item rather than failing it, so a
      throttled provider produces slowness instead of errors;
    - **recovers one slot at a time** after a quiet interval, because jumping straight back to full
      concurrency on a provider that has just stopped throttling reproduces the 429;
    - never drops below one in flight, because the provider is still usable.

    Errors are isolated per item: a fan-out is for independent work, so one failure must not discard
    the rest. Rate limits are the one exception — they are the *provider's* state, not the item's, so
    they are retried rather than reported as a failure. That retry is bounded twice over: once per
    item, so an item that can never run surfaces, and once for the wave, so a provider refusing the
    whole job stops the fan-out instead of being billed a refusal per item per retry.
    """
    if not agents:
        raise FanoutError("a fan-out needs at least one agent to distribute work to")
    scheduler = _FanoutQueue(plan=plan, agents=list(agents), max_parallel=max_parallel,
                             on_event=on_event)
    return scheduler.run(run_one)


#: How the queue treats a provider that is pushing back.
#:
#: Shrinking by half rather than by one means a badly-throttled provider is backed off quickly; the
#: floor of one means work still progresses, because a rate limit is throttling and not failure.
RATE_LIMIT_SHRINK_FACTOR = 0.5
#: How many consecutive successes before the limit is allowed to grow by one. Growing immediately
#: after a single success walks straight back into the limit.
RATE_LIMIT_RECOVERY_SUCCESSES = 3
#: How many times one item may be re-queued for a rate limit before it is reported as a failure.
#: Bounded, because an item that can never run must surface rather than loop forever.
#:
#: **Per item**, keyed by index. It used to be one counter for the whole fan-out, which meant that
#: after five requeue *events* — anywhere in the run — every later rate-limited item was failed
#: without ever being retried, and the summary blamed those items for the provider's state.
RATE_LIMIT_MAX_REQUEUES = 5
#: How many rate-limit requeue events the whole fan-out tolerates before it stops and says so.
#:
#: The per-item bound alone cannot answer "the provider is refusing this job" — 128 items each
#: allowed five retries is 640 refusals, which is hammering a throttled provider rather than backing
#: off from it. A provider that has refused this many retries is not throttling one item, so the wave
#: is abandoned and the *provider* is named, instead of every remaining item being failed as if it
#: had done something wrong. Generous relative to the per-item bound because a genuinely recovering
#: provider produces a burst of signals before it clears.
RATE_LIMIT_MAX_WAVE_REQUEUES = 40

#: Substrings that identify a provider limit in a *transport* error's own message.
#:
#: They are matched only against an exception — a `GatewayError` from the provider layer, or a raw
#: SDK error that carries no typed kind. They are deliberately **never** matched against an error
#: string that a runner *returned*: that string is the item's reported summary, and matching prose
#: against it is precisely the defect this constant is now scoped away from.
RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "429", "too many requests")


def _is_rate_limit(exc: BaseException) -> bool:
    """Whether an *exception* is the provider pushing back.

    The typed classification is consulted first and, when present, is final: a `GatewayError` whose
    kind is `server` is not a rate limit however its message reads. Only an exception with no typed
    kind falls back to its own message, and even that is the transport's text — a provider SDK's
    error, not a sentence a model wrote.
    """
    kind = getattr(exc, "kind", None)
    if kind is not None:
        return str(getattr(kind, "value", kind)).strip().lower() == "rate_limit"
    lowered = str(exc).lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def _outcome_of(value: Any) -> ItemOutcome:
    """Normalise what `run_one` returned into an :class:`ItemOutcome`.

    Three shapes are accepted, and the difference between them is the whole point:

    - an `ItemOutcome` — the typed form, where a limit is a flag;
    - a 4-tuple `(output, error, tokens, limit_hit)` — the same flag positionally, which is what the
      executor can produce with a one-line change;
    - a legacy 3-tuple `(output, error, tokens)` — accepted so every existing caller keeps working,
      and **its `error` is prose**: it can never mean "the provider pushed back".
    """
    if isinstance(value, ItemOutcome):
        return value
    if not isinstance(value, (tuple, list)):
        raise TypeError(
            "a fan-out runner must return (output, error, tokens), a 4-tuple with a limit flag, or "
            f"an ItemOutcome; got {type(value).__name__}"
        )
    if len(value) == 3:
        output, error, tokens = value
        return ItemOutcome(output=str(output or ""), error=str(error or ""),
                           tokens=int(tokens or 0))
    if len(value) == 4:
        output, error, tokens, limit_hit = value
        return ItemOutcome(output=str(output or ""), error=str(error or ""),
                           tokens=int(tokens or 0), limit_hit=bool(limit_hit))
    raise TypeError(
        "a fan-out runner returned a tuple of length "
        f"{len(value)}; expected 3 (output, error, tokens) or 4 with a limit flag"
    )


class _AdaptiveLimit:
    """A concurrency bound that shrinks on a provider limit and recovers one slot at a time.

    Extracted from the fan-out queue so the queue and the parallel-node wave share *one* answer to
    "how many may be in flight" — two independent copies of this state machine is how one of them
    silently stops honouring backpressure.

    Kept as an object rather than locals because the state that matters — the current limit, and how
    many successes have accumulated since the last limit — must survive across items; threading it
    through a loop's locals is exactly how the recovery logic gets lost.
    """

    def __init__(self, max_parallel: int, *, on_change: Callable[[str, dict[str, Any]], None]
                 | None = None) -> None:
        self.limit = max(1, int(max_parallel))
        self.ceiling = max(1, int(max_parallel))
        self._successes_since_limit = 0
        #: Reported once per shrink/recover rather than per call, so a long run does not bury the
        #: console in identical events.
        self._last_reported_limit = self.limit
        self._on_change = on_change

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(kind, payload)
        except Exception:  # noqa: BLE001 - a sink must never break a run
            pass

    def shrink(self, reason: str, *, requeued: Any = None) -> int:
        """Back off by half and return the new limit. Never below one: throttling is not failure."""
        previous = self.limit
        self.limit = max(1, int(self.limit * RATE_LIMIT_SHRINK_FACTOR))
        self._successes_since_limit = 0
        if self.limit != self._last_reported_limit:
            self._last_reported_limit = self.limit
            self._emit("fanout.backpressure", {
                "previous_limit": previous, "new_limit": self.limit,
                "requeued": requeued, "reason": str(reason)[:200],
            })
        return self.limit

    def succeed(self) -> None:
        """Count one success, growing the limit by one once enough have accumulated."""
        self._successes_since_limit += 1
        if self._successes_since_limit < RATE_LIMIT_RECOVERY_SUCCESSES:
            return
        if self.limit >= self.ceiling:
            return
        self.limit += 1
        self._successes_since_limit = 0
        self._last_reported_limit = self.limit
        self._emit("fanout.recovered", {"new_limit": self.limit, "ceiling": self.ceiling})


class _FanoutQueue:
    """A bounded queue with adaptive concurrency.

    Kept as a class rather than a loop because the state that matters — how many are in flight, how
    many succeeded since the last limit, how many times an item has been retried — must persist
    across items, and threading it through a loop's locals is how the recovery logic gets lost.
    """

    def __init__(self, *, plan: FanoutPlan, agents: list[str], max_parallel: int,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.plan = plan
        self.agents = agents
        self._bound = _AdaptiveLimit(max_parallel, on_change=on_event)
        self.on_event = on_event
        self._pending: list[tuple[int, FanoutItem]] = list(enumerate(plan.items))
        #: Requeue counts **per item index**. One shared counter made the documented per-item budget
        #: a global one, so the sixth rate-limited item in a large fan-out was failed on its first
        #: refusal — never retried, and reported as if the item were at fault.
        self._requeues: dict[int, int] = {}
        #: Total requeue events across the wave, which is what answers "is the provider refusing the
        #: job" rather than "is this item unlucky".
        self._wave_requeues = 0
        #: Set when the wave budget trips, so the run loop stops dispatching instead of hammering.
        self._abandoned_reason: str = ""

    @property
    def limit(self) -> int:
        return self._bound.limit

    @property
    def ceiling(self) -> int:
        return self._bound.ceiling

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:  # noqa: BLE001 - a sink must never break a fan-out
            pass

    def _shrink(self, item: FanoutItem, reason: str) -> None:
        """Back off after a provider signalled a limit, and re-queue the item.

        The item is re-queued at the *front* so it is retried before work that has not been attempted
        — the provider has already seen it, and fairness between items matters less than not losing a
        call that was refused for a reason unrelated to its content.

        The retry budget is charged to *this item*, and separately to the wave. Both bounds exist
        because they answer different questions: the item's own budget answers "can this item ever
        run?" (so it surfaces rather than looping), and the wave's answers "is the provider refusing
        the whole job?" (so the wave stops rather than firing hundreds of refusals and blaming each
        item for them).
        """
        attempts = self._requeues.get(item.index, 0)
        if attempts >= RATE_LIMIT_MAX_REQUEUES:
            # Bounded: an item that can never run must be reported, not looped on forever. Named
            # against the item *and* the provider, because the reason is the provider's.
            item.error = (f"item {item.index} ({item.item!r}) was refused by the provider "
                          f"{attempts} times on rate limits: {reason}")
            self._emit("fanout.item_failed", {"index": item.index, "item": item.item,
                                              "error": item.error})
            return
        self._requeues[item.index] = attempts + 1
        self._wave_requeues += 1
        if not self._abandoned_reason and self._wave_requeues >= RATE_LIMIT_MAX_WAVE_REQUEUES:
            self._abandoned_reason = reason
        self._bound.shrink(reason, requeued=item.index)
        self._pending.insert(0, (item.index, item))

    def _abandon(self) -> None:
        """Stop the wave, failing what was never attempted as *the provider's* doing.

        Nothing here is charged to an item: every remaining item's error names the throttle and says
        it has no result. Reporting them as item failures would misdirect the operator to the wrong
        object — the manifest items are fine, the provider is refusing.
        """
        remaining = list(self._pending)
        self._pending = []
        reason = (self._abandoned_reason or "the provider is throttling")[:200]
        for _index, item in remaining:
            item.error = (f"no result: the provider was still throttling after "
                          f"{self._wave_requeues} rate-limit retries across this fan-out "
                          f"({reason}); this item was abandoned with the wave")
        self._emit("fanout.throttled", {
            "requeues": self._wave_requeues, "abandoned": len(remaining), "reason": reason,
            "items": [item.index for _index, item in remaining],
        })
        for _index, item in remaining:
            self._emit("fanout.item_failed", {"index": item.index, "item": item.item,
                                              "error": item.error})

    def _recover(self) -> None:
        self._bound.succeed()

    def run(self, run_one: Callable[[FanoutItem, str], Any]) -> FanoutPlan:
        """Drain the queue, up to `limit` items genuinely in flight at once.

        One wave at a time, sized to the *current* limit, which is what makes this adaptive: a shrink
        between waves is honoured by the next wave rather than after a fixed batch.

        The wave is dispatched through `run_wave` rather than a `for` loop, because a plain loop over
        the wave is sequential whatever the limit says — the bound was reported and never used, so a
        four-wide fan-out cost four times the wall clock of one item. The limit is a ceiling on
        *concurrency*, and `run_wave` is what makes that true.
        """
        while self._pending:
            if self._abandoned_reason:
                # Checked *between* waves, where the queue is the only writer: abandoning from inside
                # a worker would leave the wave still draining items it had already been handed.
                self._abandon()
                break
            wave = self._pending[:self._bound.limit]
            self._pending = self._pending[self._bound.limit:]

            def _one(index_item: tuple[int, FanoutItem]) -> Any:
                index, item = index_item
                # Round-robin on the item's own index, so distribution is stable regardless of how
                # the waves happen to be sized — a wave that shrinks under backpressure must not
                # change which agent an item was going to.
                agent_id = self.agents[index % len(self.agents)]
                item.agent_id = agent_id
                self._emit("fanout.item_started", {"index": item.index, "item": item.item,
                                                   "agent_id": agent_id})
                try:
                    outcome = _outcome_of(run_one(item, agent_id))
                except Exception as exc:  # noqa: BLE001 - one item must not lose the others
                    error = str(exc) or exc.__class__.__name__
                    # A rate limit is the provider's state, not this item's failure, so it backs the
                    # queue off and retries instead of being reported against the item. The decision is
                    # made on the *exception*, which is the transport: its typed kind when it has one,
                    # its own message only when it does not. A returned error string is never consulted
                    # this way — that string is the model's summary, and "line 429 changed" is not a
                    # provider refusing a call.
                    if _is_rate_limit(exc):
                        self._emit("fanout.rate_limited", {"index": item.index,
                                                           "agent_id": agent_id,
                                                           "error": error[:200]})
                        self._shrink(item, error)
                        return None
                    item.error = error
                    self._emit("fanout.item_failed", {"index": item.index, "item": item.item,
                                                      "error": error[:200]})
                    return None
                if outcome.limit_hit:
                    # The caller saw the provider's own refusal and said so in the typed flag. This is
                    # the only way a *returned* value can mean a limit; nothing is inferred from text.
                    self._emit("fanout.rate_limited", {"index": item.index, "agent_id": agent_id,
                                                       "error": (outcome.error or "")[:200]})
                    self._shrink(item, outcome.error or "provider limit")
                    return None
                item.output = outcome.output
                item.error = outcome.error
                item.tokens = outcome.tokens
                item.artifacts = list(outcome.artifacts)
                item.usage = dict(outcome.usage)
                self._emit("fanout.item_done", {"index": item.index, "item": item.item,
                                                "ok": item.ok, "tokens": item.tokens})
                self._recover()
                return outcome

            run_wave(wave, _one)
        self._emit("fanout.finished", self.plan.summary())
        return self.plan


class ConcurrentSlot:
    """A bound on how many callables run at once, with a probe for how many actually did.

    The engine is I/O-bound — inference happens in Ollama/LM Studio/a cloud provider, not here — so
    Python threads are the right mechanism and the GIL is released across the call. What has to be
    *owned* rather than assumed is the bound: opening 128 concurrent model calls melts a local
    provider, which is why every concurrency site in this engine carries an explicit ceiling.

    The high-water mark is not a statistic. It is the evidence that a claimed bound was real, because
    a bound that is declared and never observed to be reached is indistinguishable from a sequential
    loop wearing a concurrency label — which is exactly the defect this class exists to make visible.
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._live = 0
        self.peak = 0

    def __enter__(self) -> "ConcurrentSlot":
        with self._lock:
            self._live += 1
            if self._live > self.peak:
                self.peak = self._live
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        with self._lock:
            self._live -= 1
        return False

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._live


def run_wave(items: Sequence[Any], run_one: Callable[[Any], Any], *,
             limit: int | None = None,
             on_error: Callable[[Any, BaseException], None] | None = None) -> list[Any]:
    """Run every item in `items` with at most `limit` in flight, returning results in input order.

    `limit` defaults to the size of the wave, which is what the fan-out queue wants: it has already
    sliced the work to its current bound, so the wave *is* the pool. A caller that hands over more
    work than it wants concurrent — a `parallel:` group whose members outnumber the machine's ceiling —
    passes `limit` and gets a real pool rather than a launch-everything-and-sleep design.

    Three properties this function owns, each of which has already been a defect elsewhere in this
    engine when left to the caller:

    - **Input order is the output order.** Results come back indexed against what went in, so an
      aggregator can zip them however items happened to finish. Ordering by completion would make the
      aggregated result depend on provider latency, which is non-determinism for no benefit.
    - **One item failing does not abort its siblings.** The failure is handed to `on_error`, which
      records it against *that* item; the pool still drains. A wave is for independent work, so
      discarding nineteen good results because one raised is strictly worse than reporting the one.
    - **Every worker is joined before returning.** A thread left behind would outlive the wave and
      mutate shared state after the caller believed it had finished — and the failure mode there is
      not a crash but a late write to the session map or the effect journal.

    The bound is enforced by handing *one* shared cursor to the workers rather than by slicing the
    items here: a thread that finishes early takes the next item, so a slow item cannot leave a slot
    idle while work remains. Slicing into fixed batches would look equivalent and would not be — it
    would make the wall clock depend on how the items happened to sort.
    """
    if not items:
        return []
    bound = max(1, int(limit)) if limit is not None else len(items)
    width = min(bound, len(items))
    results: list[Any] = [None] * len(items)
    failures: list[tuple[int, BaseException]] = []
    guard = threading.Lock()
    cursor = {"next": 0}

    def _take() -> int | None:
        """Claim the next unclaimed item index. One claim per index, whatever the thread count."""
        with guard:
            index = cursor["next"]
            if index >= len(items):
                return None
            cursor["next"] = index + 1
            return index

    def _worker() -> None:
        while True:
            index = _take()
            if index is None:
                return
            try:
                results[index] = run_one(items[index])
            except BaseException as exc:  # noqa: BLE001 - one item must not abort the wave
                with guard:
                    failures.append((index, exc))
                if on_error is not None:
                    try:
                        on_error(items[index], exc)
                    except Exception:  # noqa: BLE001 - a failure handler must not kill its worker
                        pass

    threads = [threading.Thread(target=_worker, name=f"wave-{i}") for i in range(width)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failures and on_error is None:
        # No handler means the caller asked for results only, and a silent hole in the list would read
        # as "this item produced nothing" rather than "this item failed". Saying so is the honest
        # option at a seam where there is nowhere to record the failure.
        first = sorted(failures)[0]
        raise RuntimeError(
            f"{len(failures)} of {len(items)} wave items failed and no `on_error` was supplied; "
            f"first failure at index {first[0]}: {first[1]}"
        ) from first[1]
    return results
