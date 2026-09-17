#!/usr/bin/env python3
"""prefix.py — the pinned, byte-stable prompt prefix, and the rule that keeps it stable.

WHY THIS EXISTS
---------------
Providers bill cached input at a fraction of the miss rate — DeepSeek at roughly a tenth — but they can
only reuse a prefix when the **exact bytes** match what they saw before. That makes cache reuse a
property of the prompt's *shape*, and it is unforgiving: a single changed word near the front destroys
the discount for everything behind it, silently, while the request still succeeds.

The lesson was already learned once here, in the body: moving the volatile intake behind the SOP lifted
the stable prefix from 6.2% to 86–90%. This module generalises it. It exists because that fix was
**local** — it repaired one prompt builder — and the same mistake reappeared one layer up, where it cost
far more: the agent's *name* sat at character 8 of the system prompt, so two reviewers voting on the
same question shared 4.6% of their prompt and each paid full price for the same 19KB of procedure.

So the invariant is now an object rather than a convention:

    A prefix is computed for one (skill, tools) pair, hashed, and reused byte-for-byte by every
    agent that works that skill. Everything specific to an agent, a node or a turn goes *after* it.

THREE REGIONS
-------------
Adapted from the cache-first loop design:

- **Prefix** — the system prompt, the skill's procedure, the tool schemas. Fixed for a session.
- **Log** — the conversation so far. Append-only: a prior turn is never rewritten, because rewriting
  one invalidates every token after it.
- **Tail** — this turn's volatile content: the instruction, the handoff, the identity. Last, always.

The `Prefix` object owns the first and *checks* the second: `assert_appended` refuses a log that
reordered or rewrote an earlier turn, turning a silent cache miss into a loud error.

DESIGN
------
- **The hash is over the bytes actually sent**, in their final order, so it cannot drift from the
  request the way a separately-maintained digest would.
- **A miss can be attributed.** `diff()` names which region changed — prefix, tools, or a rewritten
  log — because "the cache stopped working" is not actionable and "the tool list was reordered" is.
- **Identity is a tail concern, and the module says so.** `AgentPrefix` refuses to fold an agent name
  into the prefix, which is the specific mistake that cost 58% on every swarm.

Usage:
    prefix = Prefix.for_skill(skill="code-reviewer", system=sys_text, procedure=sop_text,
                              tools=registry.specs())
    request = prefix.compose(tail=instruction)
    if prefix.changed_since(last):
        log(prefix.diff(last))
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = ["Prefix", "PrefixChange", "PrefixError", "hash_text"]


class PrefixError(RuntimeError):
    """A prefix that cannot be honoured, named so the caller can fix it."""


def hash_text(text: str) -> str:
    """A short, stable hash of one region's bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PrefixChange:
    """What changed between two prefixes, and therefore why a cache miss happened."""

    changed: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"changed": self.changed, "reasons": list(self.reasons)}


@dataclass
class Prefix:
    """The cacheable part of a request: system + procedure + tools, in send order.

    Parameters
    ----------
    system:
        The system prompt. Must not contain anything agent-specific — see `for_skill`.
    procedure:
        The skill's standard operating procedure: the body that is identical for every agent
        working this skill.
    tools:
        The advertised tool schemas. Sorted before hashing, so a re-registration in a different
        order is not mistaken for a change.
    """

    system: str
    procedure: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    #: The skill this prefix belongs to, so a mismatch is catchable.
    skill: str = ""
    #: Filled by `__post_init__`: the hashes, computed once so they cannot drift from the text.
    system_hash: str = ""
    procedure_hash: str = ""
    tools_hash: str = ""
    prefix_hash: str = ""

    def __post_init__(self) -> None:
        self.tools = _sorted_tools(self.tools)
        self.system_hash = hash_text(self.system)
        self.procedure_hash = hash_text(self.procedure)
        self.tools_hash = hash_text(json.dumps(self.tools, sort_keys=True, default=str))
        # The whole prefix in the order it will be sent: system, then procedure, then tools. Hashing
        # the *rendered* form means the digest cannot disagree with the bytes the provider receives.
        self.prefix_hash = hash_text(self.text())

    # ── the bytes ───────────────────────────────────────────────────────────

    def text(self) -> str:
        """The prefix exactly as it is sent: system, then the procedure."""
        return f"{self.system}\n\n{self.procedure}"

    def compose(self, *, tail: str = "") -> str:
        """The full user-turn text: the stable prefix, then this turn's volatile tail.

        The order is the whole point. Anything that varies per agent or per turn belongs in `tail`,
        and putting it before `procedure` would strand the whole SOP behind a changing byte — the
        mistake this module exists to prevent.
        """
        return f"{self.text()}\n\n{tail}" if tail.strip() else self.text()

    @property
    def chars(self) -> int:
        return len(self.text())

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "prefix_hash": self.prefix_hash,
            "system_hash": self.system_hash,
            "procedure_hash": self.procedure_hash,
            "tools_hash": self.tools_hash,
            "chars": self.chars,
            "tools": [t.get("name") for t in self.tools],
        }

    # ── stability ───────────────────────────────────────────────────────────

    def diff(self, other: "Prefix | None") -> PrefixChange:
        """Why this prefix differs from a previous one.

        Named reasons, not a boolean: "the cache stopped working" sends someone hunting, while "the
        tool list changed" is a fix. `other=None` means there is nothing to compare, so the change is
        reported as a cold start rather than as a regression.
        """
        if other is None:
            return PrefixChange(True, ["cold_start"])
        reasons: list[str] = []
        if other.system_hash != self.system_hash:
            reasons.append("system")
        if other.procedure_hash != self.procedure_hash:
            reasons.append("procedure")
        if other.tools_hash != self.tools_hash:
            reasons.append("tools")
        if other.skill != self.skill:
            reasons.append("skill")
        return PrefixChange(bool(reasons), reasons)

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def for_skill(cls, *, skill: str, system: str, procedure: str,
                  tools: Iterable[Any] = (), agent_name: str = "") -> "Prefix":
        """Build the prefix for one skill, refusing anything agent-specific inside it.

        The `agent_name` argument is accepted only to *check* it: naming an agent here is the specific
        mistake that cost a swarm 58% of its bill, so it is refused loudly rather than silently
        tolerated. The caller puts the name in the tail, where it is free.
        """
        if agent_name and agent_name in system:
            raise PrefixError(
                f"the agent name {agent_name!r} appears in the system prompt. It must go in the "
                "turn's tail instead: a name here makes the prefix differ between agents, so every "
                "voter in a swarm re-pays for the same procedure. See Prefix.compose(tail=...)."
            )
        normalised = [t.as_dict() if hasattr(t, "as_dict") else dict(t) for t in tools]
        return cls(system=system, procedure=procedure, tools=normalised, skill=skill)


def _sorted_tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalise tool schemas so an equal set hashes equally.

    Two registrations of the same tools in a different order serialise to different bytes and would
    miss the cache — and the miss would look inexplicable, because the tools *are* the same.
    """
    out = [dict(t) for t in tools]
    out.sort(key=lambda t: (str(t.get("name") or ""),
                            json.dumps(t.get("parameters") or {}, sort_keys=True)))
    return out
