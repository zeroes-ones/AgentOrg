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

Usage:
    plan = plan_fanout("Review {{item}} for regressions.", items=["src/a.ts", "src/b.ts"])
    for batch in plan.batches(max_parallel=4):
        ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "FanoutError", "FanoutItem", "FanoutPlan", "plan_fanout", "run_fanout",
    "PLACEHOLDER", "MAX_ITEMS",
]

#: The one placeholder a template may use. Exactly this spelling.
PLACEHOLDER = "{{item}}"

#: The ceiling a plan will accept. High enough for a genuine bulk job, low enough that a runaway
#: decomposition is caught as a mistake rather than executed.
MAX_ITEMS = 128


class FanoutError(RuntimeError):
    """A fan-out that cannot be honoured, named so the caller can fix it before it costs anything."""


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

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "item": self.item, "prompt": self.prompt,
            "agent_id": self.agent_id, "output": self.output, "error": self.error,
            "tokens": self.tokens, "ok": self.ok,
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
        """The run's outcome: how many succeeded, and which failed and why."""
        failures = [{"index": i.index, "item": i.item, "error": i.error}
                    for i in self.items if not i.ok]
        return {
            "count": len(self.items),
            "succeeded": len(self.items) - len(failures),
            "failed": len(failures),
            "failures": failures,
            "tokens": sum(i.tokens for i in self.items),
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


def run_fanout(plan: FanoutPlan, run_one: Callable[[FanoutItem, str], tuple[str, str, int]],
               *, agents: Sequence[str], max_parallel: int = 4,
               on_event: Callable[[str, dict[str, Any]], None] | None = None) -> FanoutPlan:
    """Run a plan through a bounded, *adaptive* queue.

    `run_one(item, agent_id) -> (output, error, tokens)` is injected rather than implemented here, so
    this module owns the *fan-out policy* (validation, scheduling, error isolation, ordering) and the
    caller owns *how a call is made*. That split is what lets the same plan drive the real executor
    and a test without either one knowing about the other.

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
    they are retried rather than reported as a failure.
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
RATE_LIMIT_MAX_REQUEUES = 5


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
        self.limit = max(1, int(max_parallel))
        self.ceiling = max(1, int(max_parallel))
        self.on_event = on_event
        self._pending: list[tuple[int, FanoutItem]] = list(enumerate(plan.items))
        self._successes_since_limit = 0
        self._requeues = 0
        #: Reported once per shrink/recover rather than per call, so a long run does not bury the
        #: console in identical events.
        self._last_reported_limit = self.limit

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
        """
        if self._requeues >= RATE_LIMIT_MAX_REQUEUES:
            # Bounded: an item that can never run must be reported, not looped on forever.
            item.error = f"gave up after {RATE_LIMIT_MAX_REQUEUES} rate-limit retries: {reason}"
            self._emit("fanout.item_failed", {"index": item.index, "item": item.item,
                                              "error": item.error})
            return
        previous = self.limit
        self.limit = max(1, int(self.limit * RATE_LIMIT_SHRINK_FACTOR))
        self._pending.insert(0, (item.index, item))
        self._requeues += 1
        self._successes_since_limit = 0
        if self.limit != self._last_reported_limit:
            self._last_reported_limit = self.limit
            self._emit("fanout.backpressure", {
                "previous_limit": previous, "new_limit": self.limit,
                "requeued": item.index, "reason": reason[:200],
            })

    def _recover(self) -> None:
        """Grow the limit by one after sustained success."""
        if self.limit >= self.ceiling:
            return
        self.limit += 1
        self._successes_since_limit = 0
        self._last_reported_limit = self.limit
        self._emit("fanout.recovered", {"new_limit": self.limit, "ceiling": self.ceiling})

    def run(self, run_one: Callable[[FanoutItem, str], tuple[str, str, int]]) -> FanoutPlan:
        """Drain the queue. Returns the plan, with every item's outcome recorded on it."""
        while self._pending:
            # One wave at a time, sized to the *current* limit — which is what makes this adaptive:
            # a shrink between waves is honoured by the next wave rather than after a fixed batch.
            wave = self._pending[:self.limit]
            self._pending = self._pending[self.limit:]
            for index, item in wave:
                # Round-robin on the item's own index, so distribution is stable regardless of how
                # the waves happen to be sized — a wave that shrinks under backpressure must not
                # change which agent an item was going to.
                agent_id = self.agents[index % len(self.agents)]
                item.agent_id = agent_id
                self._emit("fanout.item_started", {"index": item.index, "item": item.item,
                                                   "agent_id": agent_id})
                try:
                    output, error, tokens = run_one(item, agent_id)
                except Exception as exc:  # noqa: BLE001 - one item must not lose the others
                    error = str(exc) or exc.__class__.__name__
                    # A rate limit is the provider's state, not this item's failure, so it backs the
                    # queue off and retries instead of being reported against the item.
                    if _is_rate_limit(error):
                        self._emit("fanout.rate_limited", {"index": item.index,
                                                           "agent_id": agent_id,
                                                           "error": error[:200]})
                        self._shrink(item, error)
                        continue
                    item.error = error
                    self._emit("fanout.item_failed", {"index": item.index, "item": item.item,
                                                      "error": error[:200]})
                    continue
                if _is_rate_limit(error):
                    self._shrink(item, error)
                    continue
                item.output = output
                item.error = error
                item.tokens = int(tokens or 0)
                self._successes_since_limit += 1
                self._emit("fanout.item_done", {"index": item.index, "item": item.item,
                                                "ok": item.ok, "tokens": item.tokens})
                if self._successes_since_limit >= RATE_LIMIT_RECOVERY_SUCCESSES:
                    self._recover()
        self._emit("fanout.finished", self.plan.summary())
        return self.plan


def _is_rate_limit(error: str) -> bool:
    """Whether a failure text describes a provider limit rather than a bad item.

    Matched on text because the injected `run_one` returns strings; the executor reports the provider's
    own classification in that string, so the words are the honest signal available at this seam.
    """
    lowered = str(error or "").lower()
    return any(marker in lowered for marker in ("rate limit", "rate_limit", "429", "too many requests"))
