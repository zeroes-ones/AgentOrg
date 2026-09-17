#!/usr/bin/env python3
"""decompose.py — turn a goal into a fan-out, the way a lead agent does.

WHY THIS EXISTS
---------------
Everything around this is already built: the fan-out executor (`fanout.py`), the capability-gated tools
(`tools.py`), the bounded agent loop (`agentloop.py`), and a cache-first prefix discipline that makes a
swarm affordable. What was missing is the **bridge** — the step where a goal becomes the list of items.

Today you type them:

    engine.cli fanout "Review {{item}}" --item src/a.ts --item src/b.ts

That is fine when you already know the work. It fails for the case the whole design is aimed at: "make
this project better" — where *deciding what the work is* is most of the job. Kimi's lead agent does
that step: it explores, decides whether a swarm is even warranted, and only then splits.

So this module is a **lead agent in miniature**, with the same three-phase shape:

1. **Explore** — read the project with the tools, because a decomposition made without looking is
   guesswork. The bound here is deliberately small: this is orientation, not the work.
2. **Decide** — perhaps no swarm is warranted. A goal that is one coherent change is *worse* as a
   fan-out, and saying so is a real outcome rather than a failure.
3. **Split** — emit items that are independent, non-overlapping, and each one capable of being
   verified on its own.

DESIGN
------
- **The decompose prompt is itself cache-aligned.** It reuses the same immutable prefix discipline:
  the instruction goes last, the shared procedure first. A lead agent runs once per goal, but its
  prompt shares a prefix with every worker that follows on the same skill.
- **The output is validated, not trusted.** Items are checked for overlap, count, and emptiness by the
  same `plan_fanout` the executor uses — a model that emits 400 items or two identical ones is refused
  before a single worker starts.
- **Declining is first-class.** `Decomposition.swarm` is False when the work should be done directly,
  with the reason recorded. Forcing a swarm onto one coherent task multiplies cost and fragments
  accountability.
- **Exploration is bounded and reported.** The lead gets at most `explore_steps` tool calls; what it
  looked at travels with the plan, so a person can see *why* it split the work that way.

Usage:
    lead = Decomposer(complete=..., tools=registry, max_items=12)
    plan = lead.decompose("harden the auth flow")
    if plan.swarm:
        ...run plan.items
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .fanout import MAX_ITEMS, FanoutError, plan_fanout

__all__ = ["DecomposeError", "Decomposition", "Decomposer", "DECOMPOSE_PROMPT"]


class DecomposeError(RuntimeError):
    """A decomposition that could not be produced, named so the caller can act."""


#: The instruction handed to the lead agent, deliberately placed *after* whatever shared procedure the
#: caller supplies, so the prompt stays cache-compatible with the workers it is about to spawn.
#:
#: It asks for JSON rather than prose because the result is going into a scheduler, and it names the
#: refusal explicitly — a model told only "split this" will split a one-line change into five items.
DECOMPOSE_PROMPT = """You are acting as the lead on this goal. Decide the work, then either do it
yourself or split it.

GOAL
{goal}

{authored}

## Your task

1. **Explore first if the goal depends on what the project contains.** Use `list_dir`, `search` and
   `read_file` to find the actual work. A decomposition made without looking is guesswork, and the
   goal above may be about files you have not seen. If the items are named in the goal itself, you may
   skip this.

2. **Decide whether a swarm is warranted.**
   - A swarm is right when the work is **N similar, independent pieces** — reviewing many files,
     migrating many call sites, covering many cases.
   - A swarm is **wrong** for one coherent change, or for pieces that must agree on shared design.
     Splitting those multiplies cost and fragments accountability. Say so instead: set
     `"swarm": false` and explain in `"reason"`.

3. **If a swarm is warranted, emit the items.** Each must be:
   - **independent** — it can be done without waiting for another item;
   - **non-overlapping** — two items must not touch the same file or the same decision;
   - **verifiable on its own** — a person can check this item without reading the others.

## Reply

End your reply with exactly one fenced block tagged `decompose` containing this JSON and nothing else:

```decompose
{{
  "swarm": true,
  "reason": "one sentence: why splitting is right, or why it is not",
  "skill": "the skill name that best fits these items",
  "prompt_template": "the prompt each worker receives, containing the literal {{item}} placeholder",
  "items": ["the first concrete item", "the second", "and so on"]
}}
```

Rules:
- `prompt_template` MUST contain `{{item}}`. Without it every worker would receive the same prompt.
- Items must be concrete and distinct: a file path, a case name, a call site — not "part 1", "part 2".
- At most {max_items} items. Fewer, well-scoped items beat many thin ones.
- When `"swarm"` is false, `items` may be empty, but `reason` is required.
"""

#: The fenced tag the decomposition is parsed from. Distinct from the node trailer's tag so the two
#: parsers cannot pick up each other's block.
DECOMPOSE_FENCE = "decompose"
_FENCE_RE = re.compile(
    rf"```{DECOMPOSE_FENCE}\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", re.DOTALL | re.IGNORECASE)


@dataclass
class Decomposition:
    """What the lead decided: whether to swarm, and over what."""

    goal: str
    swarm: bool
    reason: str = ""
    skill: str = ""
    prompt_template: str = ""
    items: list[str] = field(default_factory=list)
    #: What the lead looked at while deciding, so the split can be explained rather than trusted.
    explored: list[str] = field(default_factory=list)
    #: The validated fan-out plan, present only when `swarm` is true and it passed validation.
    plan: Any = None
    #: Model calls the decomposition cost, summed across exploration and the decision.
    steps: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal, "swarm": self.swarm, "reason": self.reason, "skill": self.skill,
            "prompt_template": self.prompt_template, "items": list(self.items),
            "explored": list(self.explored), "steps": self.steps,
            "planned": len(self.items) if self.swarm else 0,
        }

    def summary(self) -> str:
        if not self.swarm:
            return f"direct (no swarm): {self.reason}"
        return (f"swarm of {len(self.items)} on {self.skill!r}: "
                + ", ".join(self.items[:4]) + ("…" if len(self.items) > 4 else ""))


class Decomposer:
    """A lead agent that decides the work and produces a validated fan-out plan.

    Parameters
    ----------
    complete:
        `complete(request) -> response`, the same seam `AgentLoop` uses.
    tools:
        A `ToolRegistry`, so the lead explores with the *same* capability gate as the workers. A lead
        that could read files its workers cannot would plan work nobody is allowed to do.
    max_items:
        The ceiling handed to the model and enforced on its answer. A runaway decomposition is caught
        as a mistake rather than scheduled.
    explore_steps:
        How many model calls the lead may make while exploring. Small on purpose: this is orientation.
    """

    def __init__(self, *, complete: Callable[[Any], Any], tools: Any,
                 max_items: int = 12, explore_steps: int = 4,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.complete = complete
        self.tools = tools
        self.max_items = max(2, min(int(max_items), MAX_ITEMS))
        self.explore_steps = max(1, int(explore_steps))
        self.on_event = on_event

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:  # noqa: BLE001 - an observer must not break a decomposition
            pass

    def decompose(self, goal: str, *, authored: str = "") -> Decomposition:
        """Explore if needed, decide, and return a validated plan.

        `authored` is optional text placed *between* the goal and the instruction — an SOP, a house
        rule, a list of files to consider. It sits before the instruction for the same cache reason
        everything else does: it is shared across every decomposition on that skill, and the
        instruction is what varies per goal.
        """
        from .agentloop import AgentLoop
        from .providers.base import ChatRequest

        if not goal.strip():
            raise DecomposeError("a goal is required to decompose")

        instruction = DECOMPOSE_PROMPT.format(goal=goal.strip(), authored=authored.strip(),
                                              max_items=self.max_items)

        def _complete(request: ChatRequest) -> Any:
            return self.complete(request)

        loop = AgentLoop(complete=_complete, tools=self.tools, max_steps=self.explore_steps + 1)
        self._emit("decompose.start", {"goal": goal, "max_items": self.max_items})
        outcome = loop.run(system="You are a lead engineer deciding how work should be divided.",
                           user=instruction)
        self._emit("decompose.decided", {"steps": outcome.steps,
                                         "explored": sorted(set(outcome.paths))})

        plan = self._parse(outcome.text, goal=goal)
        plan.explored = sorted(set(outcome.paths))
        plan.steps = outcome.steps

        if not plan.swarm:
            # A decline made without looking is worth flagging. The prompt explicitly asks the lead to
            # explore first when the goal depends on what the project contains, and a model that
            # skips straight to "this is not separable" has decided from the goal text alone. That is
            # not necessarily wrong — a rename really is one coherent change — but the decision
            # carries less weight, and saying so is more honest than presenting it as considered.
            # Measured on qwen2.5-coder:14b: it declined a security goal at step 1 with nothing read.
            plan.reason += "" if plan.explored else (
                " (decided without reading the project, so the split was not checked against "
                "the actual work)")
            self._emit("decompose.direct", {"reason": plan.reason,
                                            "explored": bool(plan.explored)})
            return plan

        # Validated by the *same* code the executor uses, so a decomposition cannot produce a fan-out
        # the executor would then refuse. A model that emits one item, or two that expand alike, is
        # caught here — before a single worker starts.
        try:
            plan.plan = plan_fanout(plan.prompt_template, plan.items, skill=plan.skill)
        except FanoutError as exc:
            raise DecomposeError(
                f"the decomposition did not produce a runnable fan-out: {exc}. "
                f"Template={plan.prompt_template[:80]!r}, items={plan.items[:4]!r}"
            ) from exc
        self._emit("decompose.planned", {"skill": plan.skill, "items": len(plan.items)})
        return plan

    # ── parsing ─────────────────────────────────────────────────────────────

    def _parse(self, text: str, *, goal: str) -> Decomposition:
        """Read the lead's decision from its reply, refusing rather than guessing."""
        payload = self._extract(text)
        if payload is None:
            raise DecomposeError(
                "the lead did not return a parsable decomposition block. Expected a fenced "
                f"```{DECOMPOSE_FENCE} JSON object. Reply tail: {text[-200:]!r}")

        swarm = bool(payload.get("swarm"))
        reason = str(payload.get("reason") or "").strip()
        skill = str(payload.get("skill") or "").strip()
        template = str(payload.get("prompt_template") or "")
        raw_items = payload.get("items") or []
        if not isinstance(raw_items, list):
            raise DecomposeError(f"`items` must be a list, got {type(raw_items).__name__}")
        items = [str(i).strip() for i in raw_items if str(i).strip()]

        if not swarm:
            return Decomposition(goal=goal, swarm=False,
                                 reason=reason or "the lead judged this work was not separable")

        if not skill:
            raise DecomposeError("a swarm decision must name the skill for its items")
        # A single-brace `{item}` is the same intent and is refused by nothing but the fence parser.
        # Measured on qwen2.5-coder:14b: it wrote `Review the file {item} for bugs.` — unambiguously
        # meaning the placeholder, and refusing that costs a whole round trip to fix a keystroke.
        # The repair is narrow: only the lone brace is rewritten, so `{{other}}` is left alone.
        if "{{item}}" not in template and "{item}" in template:
            template = re.sub(r"(?<!\{)\{item\}(?!\})", "{{item}}", template)
        if len(items) < 2:
            # Caught here as well as in plan_fanout, so the error names the *decision* rather than
            # only the plan: "the lead asked for a swarm of one" is the actionable statement.
            raise DecomposeError(
                f"the lead asked for a swarm but produced {len(items)} item(s). A swarm needs at "
                "least two independent pieces; if the work is not separable it should set "
                "\"swarm\": false.")
        if len(items) > self.max_items:
            # Truncating would silently drop work the lead thought was needed; refusing says so.
            raise DecomposeError(
                f"the lead produced {len(items)} items, over the {self.max_items} ceiling. Raise "
                "max_items deliberately, or ask it to combine related items.")

        # Ground the items against the project. Measured on qwen2.5-coder:14b: asked to review every
        # file in `src/`, it invented `file1.js … file4.js` for a project containing `auth.py`,
        # `api.py`, `db.py`. The plan would have been *valid* — distinct prompts, real template — and
        # entirely fiction, so every worker would have been sent to a file that does not exist.
        #
        # An item naming a path that is not in the project is therefore refused, because a fan-out is
        # only as good as its items. The check is narrow on purpose: an item that is not path-shaped
        # ("the login flow", "every write endpoint") is left alone, since those are legitimate and
        # cannot be verified against a filesystem.
        missing = self._ungrounded_paths(items)
        if missing:
            raise DecomposeError(
                "the lead named file(s) that do not exist in the project: "
                + ", ".join(missing[:6])
                + ". It likely decided without reading the project. The items must refer to real "
                "work; explore the project first, or name the items yourself."
            )
        return Decomposition(goal=goal, swarm=True, reason=reason, skill=skill,
                             prompt_template=template, items=items)

    def _ungrounded_paths(self, items: Iterable[str]) -> list[str]:
        """Items that look like project paths but are not in the project.

        Path-shaped means an item that names a file with an extension, or a path containing a slash,
        and that is not merely a fragment. A path-shaped item that does not exist is a hallucination,
        and catching it here is what stops a well-formed plan full of imaginary work.
        """
        root = getattr(self.tools, "root", None)
        if root is None:
            return []
        missing: list[str] = []
        for item in items:
            text = item.strip()
            candidate = _path_like(text)
            if not candidate:
                continue
            if not (root / candidate).exists():
                missing.append(candidate)
        return missing

    @staticmethod
    def _extract(text: str) -> dict[str, Any] | None:
        """The first JSON object in the tagged fence, then any fence, then a trailing object."""
        for pattern in (_FENCE_RE, _JSON_FENCE_RE):
            match = pattern.search(text or "")
            if match:
                parsed = _first_object(match.group(1))
                if parsed is not None:
                    return parsed
        stripped = (text or "").strip()
        if stripped.startswith("{"):
            return _first_object(stripped)
        return None


def _path_like(text: str) -> str | None:
    """The project-relative path an item names, or None when it does not name one.

    Conservative by design: it returns a path only for something that clearly *is* one — a token with
    a file extension, or a token containing a slash. "the login flow" and "every write endpoint" are
    legitimate items that cannot be checked against a filesystem, and treating them as missing files
    would refuse good plans.
    """
    # The first whitespace-delimited token is where a path appears in practice
    # ("review src/auth.py thoroughly" → "src/auth.py").
    for token in text.split():
        cleaned = token.strip("`'\".,;:()[]")
        if "/" in cleaned or re.search(r"\.[A-Za-z0-9]{1,6}$", cleaned):
            # `..` or an absolute path is not a project path, and `_ungrounded` only compares
            # existence, so those are left to the tool layer's containment to refuse.
            if cleaned.startswith("/") or ".." in cleaned.split("/"):
                continue
            return cleaned
    return None


def _first_object(text: str) -> dict[str, Any] | None:
    """The first balanced JSON object in `text`, or None.

    Balanced rather than greedy, and string-aware, so a brace inside a path does not end the scan —
    the same rule the Ollama tool-call recovery uses, because the failure mode is identical.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None
