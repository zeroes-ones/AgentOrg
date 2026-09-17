#!/usr/bin/env python3
"""guardrail.py — the `classify` plugin: what may not cross a graph edge.

WHY THIS EXISTS
---------------
The runner offers one hook per edge: after a node produces its payload and *before* the handoff
advances, a classifier runs. If it refuses, the payload never reaches the next node and the runner
records the trip.

That position is the point. It is the last moment at which something harmful can be stopped without
having already been acted on — and the library's own guidance is that agent output is the exact vector
an OWASP #1 injection arrives through, because a handoff payload carries *content an agent produced*
into the context of *another agent*.

So this module does three jobs:

1. **Secret detection.** A key that reaches a payload reaches a trace, a span, a diagnostics bundle and
   possibly a repository.
2. **Injection defence.** Text arriving from an agent is data. Text that *instructs* the receiver is an
   attempt to redirect it, and it must not be treated as a system instruction.
3. **Structural sanity.** A payload that is empty, oversized or missing its verification evidence is
   refused before it becomes the next node's problem.

DESIGN
------
- **Refuse, do not sanitise.** A classifier that rewrote a payload would hide the attempt. The verdict
  is `allow: false` with a reason, so the trip is visible in the log and in the UI.
- **The reason names the category and the location, never the secret.** A guardrail that echoed a key
  into its own reason would leak it a second time.
- **A trip is always sampled at 100%.** The library's policy, and the reason is obvious: a blocked
  payload is exactly the event worth having a record of.
- **False positives are cheap; false negatives are not.** An over-eager refusal costs one node's work
  and is visible. A missed key is permanent.

Usage:
    guardrail = EdgeGuardrail(config)
    verdict = guardrail.classify("reviewer", result, state)
    if not verdict["allow"]:
        ...  # the runner records the trip and does not advance
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .config import redact

__all__ = ["EdgeGuardrail", "GuardrailVerdict", "classify", "classify_payload"]

#: Key-shaped patterns, matched anywhere in a payload. Deliberately broader than the redactor's list:
#: the redactor protects the *log*, while this protects a *handoff*, so it favours catching over
#: precision. A false positive costs a node; a false negative is permanent.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google_key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]{20,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("password_literal", re.compile(
        # `api[ _-]?key` accepts the spaced phrasing ("api key"), which is the most natural wording and
        # therefore the most likely to appear.
        r"(?i)\b(password|passwd|secret|api[ _-]?key)\b\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']")),
)

#: Phrases that attempt to redirect the receiving agent. Matched case-insensitively against the
#: *payload text* only — never the system prompt, which is where legitimate instructions live.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instruction_override", re.compile(
        r"(?i)\b(ignore|disregard|forget)\s+(all\s+)?(your\s+)?(previous|prior|above|earlier)\s+"
        r"(instructions?|rules?|prompts?|directions?)")),
    ("role_hijack", re.compile(
        r"(?i)\b(you\s+are\s+now|from\s+now\s+on\s+you|act\s+as\s+if\s+you|pretend\s+to\s+be)\b")),
    ("system_impersonation", re.compile(
        r"(?i)(^|\n)\s*(system|assistant)\s*:\s*\S")),
    ("policy_bypass", re.compile(
        r"(?i)\b(bypass|disable|turn\s+off|skip)\s+(the\s+)?(safety|security|guardrail|filter|"
        r"review|checklist|verification)\b")),
    ("exfiltration", re.compile(
        # `api[ _-]?key` rather than `api[_-]?key`: the natural phrasing is "api key" with a space, and
        # a pattern that only matched the underscore form would miss the most likely wording.
        r"(?i)\b(send|post|upload|exfiltrate|email|curl|wget|leak)\b[^\n]{0,60}?"
        r"(api[ _-]?key|token|credential|secret|password|\.env)")),
    ("data_marker", re.compile(r"<<<[^>]{0,20}>>>|\[/?INST\]|<\|im_(start|end)\|>")),
)

#: Fields a payload must carry to be a valid handoff. Checked here as well as in the handoff
#: validators, because this hook runs on payloads the runner built from a raw result dict.
_REQUIRED_PAYLOAD = ("status", "summary")

#: Above this many characters, a payload is almost certainly an inlined artifact body rather than a
#: summary — which is what rule R1 caps because it bloats the receiver.
_MAX_SUMMARY_CHARS = 4000


class GuardrailError(RuntimeError):
    """Raised on a guardrail misconfiguration. A payload problem is a verdict, not an error."""


@dataclass
class GuardrailVerdict:
    """The classifier's answer.

    `allow: False` is the whole point: it stops the payload at the edge. `reason` names the category and
    the field, never the offending content.
    """

    allow: bool
    reason: str = ""
    category: str = ""
    # Named `at_field` rather than `field`, which would shadow `dataclasses.field` inside the class body.
    at_field: str = ""
    severity: str = "warning"        # warning | critical
    redacted_preview: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """The dict shape the runner expects from a guardrail."""
        payload: dict[str, Any] = {"allow": self.allow}
        if not self.allow:
            payload["reason"] = self.reason
            payload["category"] = self.category
            payload["severity"] = self.severity
            if self.detail:
                payload["detail"] = self.detail
        return payload


def _flatten(value: Any, *, path: str = "", depth: int = 0) -> list[tuple[str, str]]:
    """Flatten a JSON-ish structure into (path, text) pairs.

    Paths are carried so a refusal can name *where* the problem is — "the payload" is not actionable,
    "findings[2].fix" is.
    """
    if depth > 12:
        # A pathological structure: refuse to walk further rather than recursing without bound.
        return [(path or "<root>", "<deeply nested: not scanned>")]
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        out.append((path or "<root>", value))
    elif isinstance(value, dict):
        for key, item in value.items():
            out.extend(_flatten(item, path=f"{path}.{key}" if path else str(key), depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            out.extend(_flatten(item, path=f"{path}[{index}]", depth=depth + 1))
    elif value is not None and not isinstance(value, bool):
        out.append((path or "<root>", str(value)))
    return out


def _find_secret(text: str) -> tuple[str, str] | None:
    """Return (category, matched text) for the first key-shaped string, or None."""
    for category, pattern in _SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            return category, match.group(0)
    return None


def _find_injection(text: str) -> tuple[str, str] | None:
    """Return (category, matched phrase) for the first injection attempt, or None."""
    for category, pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return category, match.group(0)
    return None


def classify_payload(payload: Any, *, node_id: str = "", skip_injection: bool = False
                     ) -> GuardrailVerdict:
    """Classify one payload.

    Order matters: a secret is checked first, because leaking one is unrecoverable while a suspicious
    phrase is merely a refusal.

    `skip_injection` exists for the one node that legitimately discusses injection attempts — a
    security reviewer analysing one. It never skips the secret check, because no node has a legitimate
    reason to hand a credential forward.
    """
    if payload is None:
        return GuardrailVerdict(
            allow=False, category="malformed",
            reason=f"node {node_id!r} produced no payload to hand off",
            severity="critical",
        )

    fields = _flatten(payload)

    # 1. Secrets. Unrecoverable if missed, so this runs before anything else.
    for path, text in fields:
        found = _find_secret(text)
        if found is None:
            continue
        category, matched = found
        return GuardrailVerdict(
            allow=False, category="secret", at_field=path, severity="critical",
            reason=(
                f"a {category} value is present in the handoff payload at {path}. A secret that "
                "crosses a node boundary reaches the trace, the telemetry, the diagnostics bundle and "
                "possibly a repository. Remove it before the payload advances."
            ),
            # A preview with the secret already redacted, so the reason is diagnosable without leaking.
            redacted_preview=redact(matched)[:24],
            detail={"pattern": category, "field": path},
        )

    # 2. Injection. Agent output is data; text that instructs the receiver is an attempt to redirect it.
    for path, text in (fields if not skip_injection else []):
        found = _find_injection(text)
        if found is None:
            continue
        category, matched = found
        return GuardrailVerdict(
            allow=False, category="injection", at_field=path, severity="critical",
            reason=(
                f"the handoff payload contains an instruction-shaped phrase at {path} "
                f"({category}: {redact(matched)[:60]!r}). Agent output is data, never instruction: a "
                "payload that directs the receiving agent is attempting to redirect it, and must not "
                "advance."
            ),
            detail={"pattern": category, "field": path},
        )

    # 3. Structural sanity.
    if not isinstance(payload, dict):
        # A payload that is not a mapping has no fields to check, so its structure cannot be
        # verified — and the runner expects a dict, so a non-dict means the executor produced
        # something the contract cannot describe.
        return GuardrailVerdict(
            allow=False, category="malformed", at_field="<root>", severity="warning",
            reason=(
                f"the payload is a {type(payload).__name__}, not a mapping. A handoff payload must "
                "carry named fields so the receiver can tell what it was given."
            ),
            detail={"type": type(payload).__name__},
        )
    missing = [name for name in _REQUIRED_PAYLOAD if not payload.get(name)]
    if missing:
        return GuardrailVerdict(
            allow=False, category="malformed", at_field=missing[0], severity="warning",
            reason=(
                f"the payload is missing {', '.join(missing)}. A receiver that cannot see the "
                "status or a summary cannot tell what it was given."
            ),
            detail={"missing": missing},
        )
    summary = payload.get("summary") or ""
    if len(str(summary)) > _MAX_SUMMARY_CHARS:
        return GuardrailVerdict(
            allow=False, category="oversized", at_field="summary", severity="warning",
            reason=(
                f"the payload summary is {len(str(summary))} characters. An inlined artifact body "
                "bloats the receiver's context; reference it by path instead."
            ),
            detail={"chars": len(str(summary)), "limit": _MAX_SUMMARY_CHARS},
        )

    return GuardrailVerdict(allow=True)


@dataclass
class EdgeGuardrail:
    """The runner's `classify(node_id, result, state)` plugin.

    Parameters
    ----------
    config:
        Not required for correctness — the patterns are fixed — but a caller may pass it so the
        guardrail can consult policy (for example a per-node exemption).
    allow_nodes:
        Node ids exempt from the injection check. Exists because a node whose *job* is to discuss
        prompt-injection would otherwise block itself. Never exempts the secret check: no node has a
        legitimate reason to hand a credential forward.

    A trip is always sampled at 100% by the telemetry policy, so a blocked payload is never a sampled
    statistic.
    """

    config: Any = None
    allow_nodes: tuple[str, ...] = ()
    # Recorded trips, so the orchestrator and the UI can report them without parsing the log.
    trips: list[dict[str, Any]] = field(default_factory=list)

    def classify(self, node_id: str, result: Any, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Judge whether a node's result may advance along its edge.

        Returns the dict the runner expects: `{"allow": bool, "reason": str}`. A refusal is recorded in
        `self.trips` so it is visible to the orchestrator as well as to the runner's log.
        """
        payload = self._payload_of(result)
        verdict = classify_payload(
            payload, node_id=node_id, skip_injection=node_id in self.allow_nodes,
        )

        if not verdict.allow:
            record = {"node_id": node_id, **verdict.as_dict()}
            self.trips.append(record)
            self._record(node_id, verdict)
        return verdict.as_dict()

    def _payload_of(self, result: Any) -> Any:
        """The part of a node result that would cross the edge.

        Only the handoff-relevant fields. Including the whole result would flag the executor's own
        bookkeeping (`_agent`, `_rotated`) as payload content, which it is not.
        """
        if not isinstance(result, dict):
            return result
        keys = ("status", "verdict", "summary", "diagnostics", "findings", "evidence",
                "artifacts", "decisions", "open_questions", "delegation_request")
        payload = {key: result[key] for key in keys if key in result}
        return payload or result

    def _record(self, node_id: str, verdict: GuardrailVerdict) -> None:
        """Log the trip, redacted.

        A guardrail that wrote the offending content into its own log would leak it a second time — so
        the reason is stored as already-redacted text.
        """
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is None:
            return
        try:
            diagnostics.log(
                "guardrail.blocked", level="warning", node_id=node_id,
                message=verdict.reason,
                detail={"category": verdict.category, "field": verdict.at_field,
                        "severity": verdict.severity, **verdict.detail},
            )
        except Exception:  # noqa: BLE001 - a guardrail must never break the run
            pass

    # ── reporting ───────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """Trip counts by category, for the health view and the eval suite."""
        by_category: dict[str, int] = {}
        for trip in self.trips:
            category = str(trip.get("category") or "unknown")
            by_category[category] = by_category.get(category, 0) + 1
        return {"trips": len(self.trips), "by_category": dict(sorted(by_category.items())),
                "records": list(self.trips[-20:])}

    def blocked(self) -> bool:
        """Whether anything has been blocked. A non-zero count is a signal, not a statistic."""
        return bool(self.trips)


#: A module-level callable so `--guardrail guardrail.py` works when the runner loads this file by path.
def classify(node_id: str, result: Any, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Module-level classifier for the runner's `--guardrail <path>` flag.

    Uses a module-level instance so trips accumulate across the run when the runner loads this file
    directly rather than being handed an instance.
    """
    global _DEFAULT
    try:
        _DEFAULT
    except NameError:
        _DEFAULT = EdgeGuardrail()
    return _DEFAULT.classify(node_id, result, state)


def trips() -> list[dict[str, Any]]:
    """Every trip the module-level guardrail has recorded."""
    try:
        return list(_DEFAULT.trips)
    except NameError:
        return []
