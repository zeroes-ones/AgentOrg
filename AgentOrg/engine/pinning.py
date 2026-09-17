#!/usr/bin/env python3
"""pinning.py — hold a prefix fixed for the life of a run, and report when it would have moved.

WHY THIS EXISTS
---------------
`engine/prefix.py` makes a prefix *computed* correctly — the cacheable bytes are deterministic and
agent-independent. This module makes it *held*.

The gap it closes is subtle and was measured: the executor loads the skill bundle on **every node**,
and the skill source deliberately re-parses when a `SKILL.md`'s content hash changes. That is right for
editing a skill mid-session and wrong for a cache: an edit during a run silently changes the prefix for
every later node, so the cache goes cold and nothing says why. The symptom is a bigger bill and a
slower run — the two things hardest to attribute.

Reasonix's invariant is *"the prefix is computed once per session, hashed, and pinned"*. The first two
clauses were already true here. This is the third.

DESIGN
------
- **Pinned by identity, not by time.** A prefix is keyed on `(skill, tool-names)` — the things that
  legitimately decide it. The same key returns the *same object*, so re-asking is free and cannot
  drift.
- **A change is detected, not forbidden.** Editing a skill is a legitimate thing to do; what must not
  happen is that it changes a running session's prefix *unnoticed*. The store therefore records the
  original hash, reports every divergence, and lets the caller decide — refuse the change, or accept
  it explicitly for the rest of the run.
- **A refusal names the field.** "The prefix changed" is not actionable; "the procedure changed for
  `code-reviewer`, from d7f6e1e8 to 4a1b…" is.
- **Scoped to one run.** A new run gets a new store, because a new run *should* pick up an edited
  skill. Pinning is per-run stability, not permanent fossilisation.

Usage:
    pins = PrefixPins(run_id="run_1")
    prefix = pins.get_or_pin(skill="code-reviewer", system=sys_text, procedure=sop_text, tools=specs)
    # later, after a skill edit:
    drift = pins.check(skill="code-reviewer", system=sys_text, procedure=changed_sop, tools=specs)
    if drift.changed:
        log(drift.reason)          # names the field and both hashes
    pins.accept_change(...)        # or refuse, and the pinned bytes stay in use
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .prefix import Prefix

__all__ = ["PrefixPins", "PrefixDrift", "PrefixPinError"]


class PrefixPinError(RuntimeError):
    """A prefix that cannot be pinned, or a change the caller chose not to accept."""


@dataclass(frozen=True)
class PrefixDrift:
    """A pinned prefix that no longer matches what the source now produces."""

    changed: bool
    skill: str = ""
    #: Which region moved: `system`, `procedure`, `tools` or `skill`.
    reasons: list[str] = field(default_factory=list)
    #: Both hashes, so the report is checkable rather than a bare claim.
    pinned_hash: str = ""
    current_hash: str = ""

    @property
    def reason(self) -> str:
        """A sentence naming the field and the two hashes."""
        if not self.changed:
            return f"prefix for {self.skill!r} is unchanged ({self.pinned_hash})"
        fields = ", ".join(self.reasons) or "unknown"
        return (f"the prefix for {self.skill!r} changed in {fields}: "
                f"{self.pinned_hash} -> {self.current_hash}. Every node after this one would "
                "re-pay for the whole prefix.")

    def as_dict(self) -> dict[str, Any]:
        return {"changed": self.changed, "skill": self.skill, "reasons": list(self.reasons),
                "pinned_hash": self.pinned_hash, "current_hash": self.current_hash,
                "reason": self.reason}


class PrefixPins:
    """The prefixes held fixed for one run.

    Parameters
    ----------
    run_id:
        Recorded so a drift report names the run it belongs to, and so a caller can tell two runs'
        stores apart in a log.
    strict:
        When True, `check` raises on a change instead of returning a report. Off by default: an edit
        is legitimate, and a run that dies because someone saved a file is a worse outcome than one
        that reports the cache went cold.
    """

    def __init__(self, *, run_id: str = "", strict: bool = False) -> None:
        self.run_id = run_id
        self.strict = strict
        self._pinned: dict[str, Prefix] = {}
        self._drift: list[PrefixDrift] = []
        #: Keys whose change the caller has explicitly accepted for this run.
        self._accepted: set[str] = set()

    # ── the pin ─────────────────────────────────────────────────────────────

    @staticmethod
    def key_for(skill: str, tools: Iterable[Any] = ()) -> str:
        """The identity of a prefix: the skill and the tool names it carries.

        Not the procedure text — that is what gets *pinned*, and keying on it would make a pin
        impossible to match once it changed, which is the case this module is about. The tool *names*
        rather than their schemas, because a reworded description is a change the drift report should
        catch, not a different prefix.
        """
        names = sorted(
            (t.get("name") if isinstance(t, dict) else getattr(t, "name", "")) or ""
            for t in tools)
        return f"{skill}|{','.join(names)}"

    def get_or_pin(self, *, skill: str, system: str, procedure: str,
                   tools: Iterable[Any] = ()) -> Prefix:
        """Return the pinned prefix for this key, computing and pinning it on first use.

        The first caller decides the bytes. Every later caller gets the **same object**, so a skill
        edited mid-run cannot change what a running session sends — which is the whole purpose.
        """
        key = self.key_for(skill, tools)
        pinned = self._pinned.get(key)
        if pinned is not None:
            return pinned
        prefix = Prefix.for_skill(skill=skill, system=system, procedure=procedure, tools=tools)
        self._pinned[key] = prefix
        return prefix

    def pinned(self, *, skill: str, tools: Iterable[Any] = ()) -> Prefix | None:
        return self._pinned.get(self.key_for(skill, tools))

    # ── drift ───────────────────────────────────────────────────────────────

    def check(self, *, skill: str, system: str, procedure: str,
              tools: Iterable[Any] = ()) -> PrefixDrift:
        """Compare what the source now produces against what is pinned.

        Reports rather than silently re-pins, so the caller learns the cache went cold instead of
        discovering it in the bill. `strict` turns the report into a refusal.
        """
        key = self.key_for(skill, tools)
        pinned = self._pinned.get(key)
        if pinned is None:
            return PrefixDrift(changed=False, skill=skill)
        if key in self._accepted:
            return PrefixDrift(changed=False, skill=skill)

        current = Prefix.for_skill(skill=skill, system=system, procedure=procedure, tools=tools)
        change = current.diff(pinned)
        if not change.changed:
            return PrefixDrift(changed=False, skill=skill,
                               pinned_hash=pinned.prefix_hash, current_hash=current.prefix_hash)

        drift = PrefixDrift(changed=True, skill=skill, reasons=change.reasons,
                            pinned_hash=pinned.prefix_hash, current_hash=current.prefix_hash)
        self._drift.append(drift)
        if self.strict:
            raise PrefixPinError(drift.reason)
        return drift

    def accept_change(self, *, skill: str, system: str, procedure: str,
                      tools: Iterable[Any] = ()) -> Prefix:
        """Replace the pinned prefix with the current bytes, deliberately.

        The explicit route. A caller that means to pick up an edited skill calls this; one that does
        not, keeps sending the pinned bytes and the drift stays on the record.
        """
        key = self.key_for(skill, tools)
        prefix = Prefix.for_skill(skill=skill, system=system, procedure=procedure, tools=tools)
        self._pinned[key] = prefix
        self._accepted.add(key)
        return prefix

    # ── reporting ───────────────────────────────────────────────────────────

    def drift(self) -> list[PrefixDrift]:
        """Every change detected this run, in order."""
        return list(self._drift)

    def summary(self, *, cache: dict[str, Any] | None = None) -> dict[str, Any]:
        """The run's pinning picture, for the console and the trace.

        `cache` is the provider's own accounting, folded in so the panel can show a hit rate beside
        the drift that explains it — the two facts are only useful together.
        """
        return {
            "run_id": self.run_id,
            "pinned": {key: prefix.prefix_hash for key, prefix in sorted(self._pinned.items())},
            "drift": [d.as_dict() for d in self._drift],
            "drifted": bool(self._drift),
            "cache": cache or {},
        }
