#!/usr/bin/env python3
"""ledger.py — the decision gate ledger.

WHY THIS EXISTS
---------------
Every architectural or strategic choice an agent makes is a constraint on everything that
follows. A ledger makes those choices visible to later agents, and — critically — makes an
*override* of one explicit. Without it, two contradictory decisions coexist and a downstream
agent may re-adopt the wrong one.

The library's rules R5 and R8 depend on this ledger existing: an irreversible decision requires
an entry, and an override requires a SUPERSEDED marker. Both are unenforceable without a place
to record decisions.

DESIGN
------
- **Append-only.** A decision is never edited, only superseded — which is what preserves the
  history of *why* the current choice won.
- **Superseding is explicit and requires a rationale.** R8 exists because an unmarked override
  is indistinguishable from a contradiction.
- **Non-negotiable constraints live here too**, because they are decisions whose whole point is
  to survive.
- **Querying is by gate, node and agent**, so the UI can answer "why is it like this?" and a
  later agent can check whether it is about to reverse something irreversible.

Usage:
    ledger = Ledger()
    entry = ledger.record(gate="auth-strategy", choice="argon2id",
                          rationale="memory hardness", reversible=False, by="ag_7f3a")
    ledger.supersede("auth-strategy", choice="bcrypt", rationale="…", by="ag_9c1d")
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = ["DecisionGate", "Ledger", "LedgerError", "Constraint"]


class LedgerError(RuntimeError):
    """Raised on an illegal ledger operation, such as an unmarked override."""


@dataclass
class Constraint:
    """A constraint recorded alongside the decision that produced it.

    `non_negotiable` is the flag rule R2 protects: a constraint marked so must survive every
    handoff untouched, and the validator refuses a handoff that drops one.
    """

    value: str
    type: str = "general"
    source: str = ""
    non_negotiable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "type": self.type,
                "source": self.source, "non_negotiable": self.non_negotiable}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Constraint":
        return cls(
            value=str(data.get("value") or ""),
            type=str(data.get("type") or "general"),
            source=str(data.get("source") or ""),
            non_negotiable=bool(data.get("non_negotiable", False)),
        )


@dataclass
class DecisionGate:
    """One decision, recorded with enough context to be understood later.

    `rejected_alternatives` is kept because it is the field that makes a decision reviewable: a
    choice with no alternatives listed cannot be evaluated, only obeyed.
    """

    gate: str
    choice: str
    rationale: str
    by: str = ""
    node_id: str = ""
    attempt: int = 1
    confidence: str = "medium"        # low | medium | high
    reversible: bool = True
    rejected_alternatives: list[str] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    superseded: bool = False
    superseded_by: str = ""
    superseded_rationale: str = ""
    at: str = ""

    def __post_init__(self) -> None:
        if not self.at:
            self.at = _iso_now()

    @property
    def marker(self) -> str:
        """The SUPERSEDED marker rule R8 requires on an override."""
        return "SUPERSEDED" if self.superseded else ""

    def key(self) -> tuple[str, str]:
        """The (gate, at) tuple identifying one entry."""
        return (self.gate, self.at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "choice": self.choice,
            "rationale": self.rationale,
            "by": self.by,
            "node_id": self.node_id,
            "attempt": self.attempt,
            "confidence": self.confidence,
            "reversible": self.reversible,
            "rejected_alternatives": list(self.rejected_alternatives),
            "constraints": [c.as_dict() for c in self.constraints],
            "superseded": self.superseded,
            "marker": self.marker,
            "superseded_by": self.superseded_by,
            "superseded_rationale": self.superseded_rationale,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionGate":
        return cls(
            gate=str(data.get("gate") or ""),
            choice=str(data.get("choice") or ""),
            rationale=str(data.get("rationale") or ""),
            by=str(data.get("by") or ""),
            node_id=str(data.get("node_id") or ""),
            attempt=int(data.get("attempt", 1)),
            confidence=str(data.get("confidence") or "medium"),
            reversible=bool(data.get("reversible", True)),
            rejected_alternatives=[str(a) for a in (data.get("rejected_alternatives") or [])],
            constraints=[Constraint.from_dict(c) for c in (data.get("constraints") or [])
                         if isinstance(c, dict)],
            superseded=bool(data.get("superseded", False)),
            superseded_by=str(data.get("superseded_by") or ""),
            superseded_rationale=str(data.get("superseded_rationale") or ""),
            at=str(data.get("at") or ""),
        )


class Ledger:
    """An append-only decision ledger, optionally persisted to disk.

    Parameters
    ----------
    path:
        When given, every change is appended to this JSONL file so the ledger survives a crash
        and can be reviewed afterwards.
    """

    def __init__(self, path: os.PathLike | str | None = None) -> None:
        self.path = Path(path) if path else None
        self._entries: list[DecisionGate] = []
        self._lock = threading.RLock()
        self._fh: Any = None
        if self.path is not None:
            self.load()

    # ── persistence ─────────────────────────────────────────────────────────

    def load(self) -> int:
        """Replay the journal, tolerating a torn final line.

        The journal is append-only for audit purposes, which means a *reload* must replay
        operations rather than append one entry per line: a `supersede` writes a second record
        for the same decision, and naively appending would duplicate the gate's history on every
        restart.

        Returns the number of live decisions reconstructed.
        """
        with self._lock:
            self._entries.clear()
            if self.path is None or not self.path.is_file():
                return 0
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # Expected when the process was killed mid-write.
                        continue
                    if not isinstance(data, dict):
                        continue
                    record = data.get("record")
                    if not isinstance(record, dict):
                        continue
                    entry = DecisionGate.from_dict(record)
                    operation = str(data.get("op") or "record")
                    self._replay(entry, operation)
            return len([e for e in self._entries if not e.superseded])

    def _replay(self, entry: DecisionGate, operation: str) -> None:
        """Apply one journal operation to the in-memory ledger.

        A `record` for a gate that already ends its history is a genuine new decision (the
        supersede case rewrites the *previous* entry and then records the new one); a
        `supersede`/`constraint` line updates the existing entry in place rather than appending.
        """
        if operation in ("supersede", "constraint"):
            for existing in self._entries:
                if existing.gate == entry.gate and existing.at == entry.at:
                    existing.constraints = list(entry.constraints)
                    existing.superseded = entry.superseded
                    existing.superseded_by = entry.superseded_by
                    existing.superseded_rationale = entry.superseded_rationale
                    return
            # No matching entry (the write landed before the original was replayed): treat it
            # as a plain record so the decision is not lost.
        self._entries.append(entry)

    def _persist(self, payload: dict[str, Any]) -> None:
        if self.path is None:
            return
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        self._fh.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        """Flush and close the file handle. Idempotent."""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                finally:
                    self._fh = None

    # ── recording ───────────────────────────────────────────────────────────

    def record(self, *, gate: str, choice: str, rationale: str = "", by: str = "",
               node_id: str = "", attempt: int = 1, reversible: bool = True,
               confidence: str = "medium",
               rejected_alternatives: Iterable[str] = (),
               constraints: Iterable[Constraint] = ()) -> DecisionGate:
        """Record a decision.

        Raises
        ------
        LedgerError
            When the gate or choice is empty, or when the gate already holds a *live*
            irreversible decision — such a decision cannot be silently re-made, because rule
            R5 exists to stop exactly that.
        """
        if not (gate or "").strip():
            raise LedgerError("a decision requires a gate name")
        if not (choice or "").strip():
            raise LedgerError(f"decision {gate!r} requires a choice")
        with self._lock:
            live = self.current(gate)
            if live is not None and live.reversible is False:
                raise LedgerError(
                    f"gate {gate!r} already holds an irreversible decision "
                    f"({live.choice!r} by {live.by or 'unknown'}). An irreversible decision "
                    "cannot be re-made silently — supersede it explicitly with a rationale."
                )
            entry = DecisionGate(
                gate=str(gate).strip(),
                choice=str(choice).strip(),
                rationale=str(rationale).strip(),
                by=by, node_id=node_id, attempt=attempt,
                reversible=bool(reversible),
                confidence=str(confidence),
                rejected_alternatives=[str(a) for a in rejected_alternatives],
                constraints=list(constraints),
            )
            self._entries.append(entry)
            self._persist({"op": "record", "record": entry.as_dict()})
            return entry

    def supersede(self, gate: str, *, choice: str, rationale: str, by: str = "",
                  rejected_alternatives: Iterable[str] = (), reversible: bool = True,
                  confidence: str = "medium") -> DecisionGate:
        """Override a previous decision, marking the old one SUPERSEDED.

        Raises
        ------
        LedgerError
            When there is no live decision at the gate, or the rationale is empty. The rationale
            is mandatory: rule R8's purpose is that an override explains itself, so a later agent
            can judge whether it was justified rather than merely observing that the answer
            changed.
        """
        if not (rationale or "").strip():
            raise LedgerError(
                f"superseding {gate!r} requires a rationale. An unmarked override leaves two "
                "contradictory decisions visible with no way to tell which won."
            )
        with self._lock:
            previous = self.current(gate)
            if previous is None:
                raise LedgerError(
                    f"cannot supersede gate {gate!r}: no live decision found. Record it first."
                )
            previous.superseded = True
            previous.superseded_by = choice
            previous.superseded_rationale = rationale
            self._persist({"op": "supersede", "gate": gate, "record": previous.as_dict()})
            entry = DecisionGate(
                gate=gate, choice=choice, rationale=rationale, by=by,
                reversible=bool(reversible), confidence=confidence,
                rejected_alternatives=[str(a) for a in rejected_alternatives],
                superseded=False,
            )
            entry.constraints = list(previous.constraints)
            self._entries.append(entry)
            self._persist({"op": "record", "record": entry.as_dict()})
            return entry

    def add_constraint(self, gate: str, constraint: Constraint) -> DecisionGate:
        """Attach a constraint to the live decision at a gate.

        Raises
        ------
        LedgerError
            When the gate has no live decision, because a constraint with no decision behind it
            has no provenance.
        """
        with self._lock:
            live = self.current(gate)
            if live is None:
                raise LedgerError(
                    f"cannot add a constraint to gate {gate!r}: no live decision. Record the "
                    "decision first so the constraint has provenance."
                )
            live.constraints.append(constraint)
            self._persist({"op": "constraint", "gate": gate, "record": live.as_dict()})
            return live

    # ── querying ────────────────────────────────────────────────────────────

    def current(self, gate: str) -> DecisionGate | None:
        """The live (non-superseded) decision at a gate, if any."""
        with self._lock:
            for entry in reversed(self._entries):
                if entry.gate == gate and not entry.superseded:
                    return entry
        return None

    def history(self, gate: str) -> list[DecisionGate]:
        """Every decision at a gate, oldest first, including superseded ones."""
        with self._lock:
            return [e for e in self._entries if e.gate == gate]

    def all(self) -> list[DecisionGate]:
        """Every entry, in recording order."""
        with self._lock:
            return list(self._entries)

    def live(self) -> list[DecisionGate]:
        """Every live decision, oldest first."""
        with self._lock:
            return [e for e in self._entries if not e.superseded]

    def irreversible(self) -> list[DecisionGate]:
        """Every live irreversible decision — the set later agents must not silently reverse."""
        with self._lock:
            return [e for e in self._entries if not e.superseded and e.reversible is False]

    def constraints(self, *, non_negotiable_only: bool = False) -> list[Constraint]:
        """Every live constraint, optionally only the non-negotiable ones.

        The non-negotiable subset is what the prompt pins to the primacy zone and what rule R2
        protects across handoffs.
        """
        out: list[Constraint] = []
        with self._lock:
            for entry in self._entries:
                if entry.superseded:
                    continue
                for constraint in entry.constraints:
                    if non_negotiable_only and not constraint.non_negotiable:
                        continue
                    out.append(constraint)
        return out

    def by_node(self, node_id: str) -> list[DecisionGate]:
        """Every decision made by one node."""
        with self._lock:
            return [e for e in self._entries if e.node_id == node_id]

    def by_agent(self, agent_id: str) -> list[DecisionGate]:
        """Every decision made by one agent."""
        with self._lock:
            return [e for e in self._entries if e.by == agent_id]

    def as_handoff_block(self) -> list[dict[str, Any]]:
        """The live decisions rendered for a handoff payload.

        Only live decisions travel: a superseded one would contradict the current choice, and
        the receiving agent has no way to know which of the two won.
        """
        return [
            {
                "gate": e.gate,
                "choice": e.choice,
                "rationale": e.rationale,
                "rejected_alternatives": list(e.rejected_alternatives),
                "confidence": e.confidence,
                "reversible": e.reversible,
                "by": e.by,
                "timestamp": e.at,
            }
            for e in self.live()
        ]

    def constraints_block(self) -> list[dict[str, Any]]:
        """Live constraints rendered for a handoff payload, for rule R2's comparison."""
        return [c.as_dict() for c in self.constraints()]

    def stats(self) -> dict[str, Any]:
        """Counts for the UI, including how many overrides occurred."""
        with self._lock:
            superseded = sum(1 for e in self._entries if e.superseded)
            irreversible = sum(1 for e in self._entries if e.reversible is False and not e.superseded)
            gates = {e.gate for e in self._entries}
            return {
                "entries": len(self._entries),
                "gates": len(gates),
                "superseded": superseded,
                "irreversible": irreversible,
                "non_negotiable_constraints": len(self.constraints(non_negotiable_only=True)),
            }

    def summary(self) -> str:
        """A readable ledger, for the CLI inspector."""
        if not self._entries:
            return "The decision ledger is empty."
        lines = ["gate                          choice                    rev   by"]
        for entry in self.live():
            marker = "" if entry.reversible else "✗"
            lines.append(
                f"{entry.gate:29s} {entry.choice[:24]:24s} {marker:5s} {entry.by[:16]}"
            )
        superseded = [e for e in self._entries if e.superseded]
        if superseded:
            lines.append("")
            lines.append(f"({len(superseded)} superseded entries retained in history)")
        return "\n".join(lines)


def _iso_now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
