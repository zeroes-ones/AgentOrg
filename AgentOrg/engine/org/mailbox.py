#!/usr/bin/env python3
"""mailbox.py — per-agent append-only message log.

WHY THIS EXISTS
---------------
An agent needs to be told things: that a task was assigned, that a review was rejected, that
the Owner injected a constraint, that a peer handed off work. Those messages must be durable
and ordered because they are the agent's *context* — and because after a crash the run must be
able to reconstruct what each agent had been told.

A mailbox rather than shared mutable state, for the reason the library gives: state
corruption across handoffs is the expensive failure, so each agent owns its own log and
nothing else writes to it.

DESIGN
------
- **Append-only.** Messages are never edited or deleted, so the record of what an agent was
  told cannot be rewritten after the fact.
- **Idempotent by id.** A message carries a caller-supplied id, and re-delivering one already
  present is a no-op. This is what makes delivery safe to retry after a crash.
- **Bounded reads.** `recent()` returns the tail, because an agent's prompt only needs its
  unread or recent context, not its whole history.
- **Unread tracking is explicit.** The Owner's UI needs to show what an agent has not seen,
  and a resumed run needs to know what to re-inject.

Usage:
    box = Mailbox(path=state_dir / "agents" / "ag_1" / "mailbox.jsonl")
    box.send(Message(id="m1", kind=MessageKind.ASSIGN, body="Fix the auth bug"))
    for message in box.unread():
        ...
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

__all__ = ["Mailbox", "MailboxError", "Message", "MessageKind"]


def _iso_now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"



class MailboxError(RuntimeError):
    """Raised when a mailbox cannot be read or written."""


class MessageKind(str, Enum):
    """What a message is for. The kind drives how the prompt frames it."""

    ASSIGN = "assign"                # a task was assigned
    HANDOFF = "handoff"              # work arrived from a peer
    REWORK = "rework"                # a review rejection with findings
    INSTRUCTION = "instruction"      # guidance from the Owner
    CONSTRAINT = "constraint"        # a non-negotiable injected mid-run
    REVIEW = "review"                # a review verdict
    APPROVAL = "approval"            # a gate was approved
    REJECTION = "rejection"          # a gate or route was rejected, with reason
    NOTICE = "notice"                # informational: health, policy, routing
    ROTATION = "rotation"            # a session rotation occurred
    DELEGATION = "delegation"        # a helper/specialist was spawned or destroyed


@dataclass
class Message:
    """One message in an agent's mailbox."""

    id: str
    kind: MessageKind
    body: str
    sender: str = "system"
    task_id: str | None = None
    node_id: str | None = None
    # Structured payload for kinds that carry data (findings, requisitions, verdicts).
    data: dict[str, Any] = field(default_factory=dict)
    sent_at: str = ""
    read: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise MailboxError("a message requires an id so redelivery can be idempotent")
        if not self.sent_at:
            self.sent_at = _iso_now()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "body": self.body,
            "sender": self.sender,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "data": self.data,
            "sent_at": self.sent_at,
            "read": self.read,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        """Rebuild a message, tolerating an unknown kind from a newer build.

        An unrecognised kind becomes NOTICE rather than raising: a run that encounters a
        message type it does not know should continue, not fail to resume.
        """
        try:
            kind = MessageKind(str(data.get("kind", "notice")))
        except ValueError:
            kind = MessageKind.NOTICE
        return cls(
            id=str(data.get("id") or ""),
            kind=kind,
            body=str(data.get("body") or ""),
            sender=str(data.get("sender") or "system"),
            task_id=data.get("task_id"),
            node_id=data.get("node_id"),
            data=data.get("data") if isinstance(data.get("data"), dict) else {},
            sent_at=str(data.get("sent_at") or _iso_now()),
            read=bool(data.get("read", False)),
        )


class Mailbox:
    """An append-only, idempotent message log for one agent.

    Parameters
    ----------
    path:
        The JSONL file. Created on first write; a missing file is an empty mailbox, which is
        the correct state for a newly hired agent.
    """

    def __init__(self, path: os.PathLike | str, *, agent_id: str = "") -> None:
        self.path = Path(path)
        self.agent_id = agent_id
        self._lock = threading.RLock()
        self._messages: list[Message] = []
        self._index: dict[str, Message] = {}
        self._fh: Any = None
        self._loaded = False

    # ── persistence ─────────────────────────────────────────────────────────

    def load(self) -> int:
        """Read the mailbox into memory, tolerating a torn final line.

        Returns the number of messages. A crash mid-append is expected, so a malformed last
        line is skipped rather than invalidating the whole history.
        """
        with self._lock:
            self._messages.clear()
            self._index.clear()
            if self.path.is_file():
                try:
                    with open(self.path, "r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(data, dict) or not data.get("id"):
                                continue
                            message = Message.from_dict(data)
                            # A later record for the same id wins: that is how the read flag
                            # is persisted without rewriting history.
                            self._index[message.id] = message
                except OSError as exc:
                    raise MailboxError(f"cannot read mailbox {self.path}: {exc}") from exc
            self._messages = list(self._index.values())
            self._loaded = True
            return len(self._messages)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _append(self, message: Message) -> None:
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        self._fh.write(json.dumps(message.as_dict(), separators=(",", ":"), sort_keys=True) + "\n")
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

    # ── writing ─────────────────────────────────────────────────────────────

    def send(self, message: Message) -> bool:
        """Deliver a message. Returns False when it was already present.

        Idempotent by id so a retried delivery after a crash cannot duplicate an instruction
        the agent would then act on twice.
        """
        with self._lock:
            self._ensure_loaded()
            if message.id in self._index:
                return False
            self._index[message.id] = message
            self._messages.append(message)
            self._append(message)
            return True

    def post(self, kind: MessageKind, body: str, *, message_id: str, sender: str = "system",
             task_id: str | None = None, node_id: str | None = None,
             data: dict[str, Any] | None = None) -> Message:
        """Convenience constructor + send. Returns the message either way."""
        message = Message(id=message_id, kind=kind, body=body, sender=sender,
                          task_id=task_id, node_id=node_id, data=data or {})
        self.send(message)
        return message

    def mark_read(self, message_id: str) -> bool:
        """Record a message as read, persisting the flag as a new record.

        The log stays append-only: the read state is a later record for the same id, so the
        original delivery is never rewritten.
        """
        with self._lock:
            self._ensure_loaded()
            message = self._index.get(message_id)
            if message is None or message.read:
                return False
            message.read = True
            self._append(message)
            return True

    def mark_all_read(self) -> int:
        """Mark every unread message read. Returns how many changed."""
        with self._lock:
            self._ensure_loaded()
            changed = [m for m in self._index.values() if not m.read]
            for message in changed:
                message.read = True
                self._append(message)
            return len(changed)

    # ── reading ─────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        with self._lock:
            self._ensure_loaded()
            return len(self._messages)

    def all(self) -> list[Message]:
        """Every message, oldest first."""
        with self._lock:
            self._ensure_loaded()
            return list(self._messages)

    def unread(self) -> list[Message]:
        """Unread messages, oldest first — what the prompt should re-inject."""
        with self._lock:
            self._ensure_loaded()
            return [m for m in self._messages if not m.read]

    def recent(self, limit: int = 20, *, kinds: tuple[MessageKind, ...] | None = None) -> list[Message]:
        """The most recent messages, oldest first, optionally filtered by kind."""
        with self._lock:
            self._ensure_loaded()
            selected = [m for m in self._messages if kinds is None or m.kind in kinds]
            return selected[-limit:]

    def for_task(self, task_id: str) -> list[Message]:
        """Every message about one task, oldest first."""
        with self._lock:
            self._ensure_loaded()
            return [m for m in self._messages if m.task_id == task_id]

    def constraints(self) -> list[str]:
        """Every non-negotiable constraint this agent has been given.

        Surfaced as a list of strings because that is exactly what the prompt's primacy zone
        needs — a constraint an agent has been told must survive every later handoff.
        """
        with self._lock:
            self._ensure_loaded()
            out: list[str] = []
            for message in self._messages:
                if message.kind is MessageKind.CONSTRAINT and message.body.strip():
                    out.append(message.body.strip())
            return out

    def __iter__(self) -> Iterator[Message]:
        return iter(self.all())

    def stats(self) -> dict[str, Any]:
        """Counts by kind and read state, for the inbox view."""
        with self._lock:
            self._ensure_loaded()
            by_kind: dict[str, int] = {}
            unread = 0
            for message in self._messages:
                by_kind[message.kind.value] = by_kind.get(message.kind.value, 0) + 1
                if not message.read:
                    unread += 1
            return {"total": len(self._messages), "unread": unread,
                    "by_kind": dict(sorted(by_kind.items()))}
