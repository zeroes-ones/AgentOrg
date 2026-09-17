#!/usr/bin/env python3
"""bundle.py — turn a SKILL.md into an enforceable bundle.

WHY THIS EXISTS
---------------
A raw SKILL.md is 18,000 tokens of prose, tables and examples. The orchestrator needs four
specific things out of it, and needs them to be reliable:

1. The **contract** — inputs, outputs, completion criteria, evidence requirement, escalation
   target. These gate every phase transition, so a missing one is a correctness bug.
2. The **Production Checklist** — the `[CR1]`-style items the prompt must make the model
   tick off. These are what turn "review the code" into a checkable claim.
3. The **anti-rationalization rules** — the excuses the skill itself forbids.
4. The **research steps** (RP1–RP8) — the hard gate every skill declares.

Plus the body, split into disclosure tiers so the prompt can load only what the task needs.

DESIGN
------
- **Missing criteria fall back to the Verification section**, which the library documents as
  the default criteria source when a skill declares no `workflow:` block. Returning "no
  criteria" instead would make the node ungated, which is the failure the contract exists
  to prevent.
- **Checklist IDs are extracted, not the whole table.** The prompt needs to name every item
  so the model cannot quietly skip one, and the orchestrator needs a stable id per item to
  verify the response against.
- **Section classification is explicit** about what is Tier 1 (always loaded), Tier 2
  (loaded on intent match) and Tier 3 (lazily loaded). Getting this backwards is what makes
  a prompt either miss its ground rules or cost 18k tokens.
- **A content hash is carried on the bundle**, so every span can record which prompt
  produced which output.

Usage:
    bundle = parse_skill("code-reviewer", text)
    bundle.criteria            # the completion criteria, verbatim
    bundle.checklist           # [ChecklistItem(id="CR1", text=...), ...]
    bundle.system_body(tier=1) # the token-budgeted SOP text
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .frontmatter import FrontmatterError, parse_frontmatter

__all__ = [
    "ChecklistItem",
    "SkillContract",
    "SkillBundle",
    "SkillError",
    "Tier",
    "Section",
    "parse_skill",
]


class SkillError(RuntimeError):
    """Raised when a skill cannot be turned into an enforceable bundle."""


class Tier(int, Enum):
    """Progressive-disclosure tier, per the library's own compaction guidance."""

    ROUTE = 1      # always loaded: route, headline, ground rules
    CORE = 2       # loaded on intent match: workflow, decision trees, criteria
    DETAIL = 3     # lazily loaded: examples, references, gotchas, practice


# Headings mapped to a tier. Matching is on a normalised prefix so "## Production Checklist
# **(STANDARD)**" and "## Production Checklist" classify identically.
#
# The assignments follow the library's own rule: what changes an agent's *decision* loads
# early; what merely illustrates or motivates loads late.
_TIER_RULES: tuple[tuple[tuple[str, ...], Tier], ...] = (
    (("route the request", "auto-route", "intent route", "when to use", "when not to use",
      "ground rules", "core workflow", "verification"), Tier.ROUTE),
    (("anti-rationalization", "decision tree", "workflow", "severity grading",
      "error recovery", "production checklist", "research prerequisite",
      "anti-patterns", "best practices", "operating at different levels"), Tier.CORE),
    (("gotchas", "error decoder", "deliberate practice", "what good looks like", "references",
      "cross-skill", "proactive triggers", "state log", "examples", "the expert's mindset",
      "mental models", "cognitive biases", "changelog", "compatibility"), Tier.DETAIL),
)


@dataclass(frozen=True)
class ChecklistItem:
    """One Production Checklist row.

    `id` is the bracketed marker the library uses (`CR1`, `CR2`, …). It must be stable,
    because the orchestrator verifies the model's response against these ids — an
    unverifiable checklist is decoration.
    """

    id: str
    text: str
    tier: Tier = Tier.CORE

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text}


@dataclass(frozen=True)
class SkillContract:
    """The typed `workflow:` block plus the completion criteria it gates on."""

    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    evidence_required: bool = False
    escalate_to: tuple[str, ...] = ()
    iteration_max: int = 1
    on_exhaustion: str = "escalate"
    # True when the criteria came from the Verification section rather than a workflow block.
    criteria_from_fallback: bool = False

    @property
    def has_contract(self) -> bool:
        """True when the skill declared an explicit `workflow:` block."""
        return bool(self.inputs or self.outputs or self.criteria) and not self.criteria_from_fallback

    def as_dict(self) -> dict[str, Any]:
        return {
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "criteria": list(self.criteria),
            "evidence_required": self.evidence_required,
            "escalate_to": list(self.escalate_to),
            "iteration_max": self.iteration_max,
            "on_exhaustion": self.on_exhaustion,
            "criteria_from_fallback": self.criteria_from_fallback,
        }


@dataclass(frozen=True)
class Section:
    """One `##` section of the body, with its assigned tier."""

    title: str
    body: str
    tier: Tier
    level: int = 2

    @property
    def tokens_est(self) -> int:
        """Rough token cost at the usual four characters per token."""
        return max(1, len(self.body) // 4)


@dataclass
class SkillBundle:
    """Everything the engine needs from one skill, extracted and typed."""

    name: str
    version: str = ""
    description: str = ""
    token_budget: int | None = None
    content_hash: str = ""
    skill_type: str = ""
    status: str = ""
    tags: tuple[str, ...] = ()
    # Chain edges: the static capability graph the library documents.
    consumes_from: tuple[str, ...] = ()
    feeds_into: tuple[str, ...] = ()
    contract: SkillContract = field(default_factory=SkillContract)
    checklist: tuple[ChecklistItem, ...] = ()
    anti_rationalization: tuple[str, ...] = ()
    research_steps: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    sections: tuple[Section, ...] = ()
    body: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""

    # ── derived views ───────────────────────────────────────────────────────

    def checklist_ids(self) -> list[str]:
        """Every checklist id, in order — the prompt must name all of them."""
        return [item.id for item in self.checklist]

    def sections_for_tier(self, tier: Tier) -> list[Section]:
        """Sections at exactly one tier."""
        return [s for s in self.sections if s.tier is tier]

    def system_body(self, *, tier: "Tier | int" = Tier.CORE, max_tokens: int | None = None,
                    include: Iterable[str] | None = None) -> str:
        """Assemble the SOP text up to a tier, optionally capped by token budget.

        Tiers are cumulative: requesting `Tier.CORE` includes `Tier.ROUTE` sections too,
        because a core instruction is meaningless without the ground rules it depends on.
        When `max_tokens` is given, later sections are dropped once the budget is spent —
        which is what keeps a 5,000-token skill inside a local model's 32k window alongside
        a conversation.
        """
        # Coerce so `tier=3` works: `Tier` is an int enum, and comparing a bare int against
        # `t.value` would raise rather than behave as the caller plainly intends.
        try:
            requested = tier if isinstance(tier, Tier) else Tier(int(tier))
        except (TypeError, ValueError):
            requested = Tier.CORE
        tiers = [t for t in (Tier.ROUTE, Tier.CORE, Tier.DETAIL) if t.value <= requested.value]
        selected = [s for s in self.sections if s.tier in tiers]
        if include:
            wanted = {w.lower() for w in include}
            # An explicit include list wins, but always keep the ground rules: dropping them
            # to satisfy a narrow request is how an agent loses its safety constraints.
            selected = [
                s for s in selected
                if any(w in s.title.lower() for w in wanted) or s.tier is Tier.ROUTE
            ]

        parts: list[str] = []
        used = 0
        budget = max_tokens if max_tokens is not None else self.token_budget
        for section in selected:
            cost = section.tokens_est
            if budget is not None and used + cost > budget and parts:
                break
            parts.append(f"## {section.title}\n{section.body}".rstrip())
            used += cost
        if not parts:
            return f"# {self.name}\n\n{self.description}".strip()
        return "\n\n".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """Compact serialisation for events and tracing. Omits the full body."""
        return {
            "name": self.name,
            "version": self.version,
            "content_hash": self.content_hash,
            "token_budget": self.token_budget,
            "contract": self.contract.as_dict(),
            "checklist_ids": self.checklist_ids(),
            "checklist_count": len(self.checklist),
            "anti_rationalization_count": len(self.anti_rationalization),
            "research_step_count": len(self.research_steps),
            "consumes_from": list(self.consumes_from),
            "feeds_into": list(self.feeds_into),
            "section_count": len(self.sections),
            "source_path": self.source_path,
        }


# ── extraction ───────────────────────────────────────────────────────────────

# `## Production Checklist **(STANDARD)**` / `## Verification` / `### Ground Rules`
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
# A checklist row: `- [ ] **[CR1]** text...`  or  `- [ ] [CR1] text...`  or  `1. **[R1]** ...`
_CHECKLIST_RE = re.compile(
    r"^\s*(?:[-*]\s*(?:\[[ xX]\]\s*)?|\d+\.\s*)\*{0,2}\[([A-Za-z][A-Za-z0-9_-]*)\]\*{0,2}\s*(.+?)\s*$"
)
# `| **R1** | Must NOT ... |` — the ground-rule table form.
_RULE_ROW_RE = re.compile(r"^\|\s*\*{0,2}([A-Za-z][A-Za-z0-9_-]*)\*{0,2}\s*\|\s*(.+?)\s*\|")
# `| AR1 | "It's a small change" is a scope decision... |`
_AR_ROW_RE = re.compile(r"^\|\s*\*{0,2}(AR[-\s]?\d+)\*{0,2}\s*\|\s*(.+?)\s*\|")
# `- [ ] text` in a Verification or checklist section.
_VERIFY_RE = re.compile(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?(.+?)\s*$")
# `| **V1** | Check | Pass condition |` — the verification table form. Also `| RP1 | ... |`.
_ID_ROW_RE = re.compile(
    r"^\|\s*\*{0,2}([A-Za-z]{1,4}[-\s]?\d+)\*{0,2}\s*\|\s*(.+?)\s*\|"
)
# A `## RESEARCH_PREREQUISITE` heading, in either space- or underscore-separated spelling.
_RESEARCH_HEADING_RE = re.compile(r"research[\s_-]*prerequisite", re.IGNORECASE)
# A `## Verification` heading.
_VERIFICATION_HEADING_RE = re.compile(r"^\s*verification\b", re.IGNORECASE)
# `Complete when: ...` — the checkpoint criterion the Core Workflow phases carry. The library
# documents this as a completion signal, and 241 skills use it, so it is the last fallback
# before a skill is refused.
_COMPLETE_WHEN_RE = re.compile(r"^\s*>?\s*\**Complete when:?\**\s*(.+?)\s*$", re.IGNORECASE)


def _normalise_heading(title: str) -> str:
    """Strip emphasis, HTML comments and separators so a heading can be classified.

    `Production Checklist **(STANDARD)**` and `Error Recovery **(STANDARD)**` must classify
    the same as their bare forms, or the tier assignment silently misses them. Underscores
    and hyphens are folded to spaces because the library writes some headings in SHOUTY_SNAKE
    (`RESEARCH_PREREQUISITE`), which would otherwise never match a space-separated rule.
    """
    text = re.sub(r"<!--.*?-->", "", title)
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"\([^)]*\)", "", text)
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" :—–").lower()


def _tier_for(title: str) -> Tier:
    """Classify a section heading into a disclosure tier."""
    normalised = _normalise_heading(title)
    for prefixes, tier in _TIER_RULES:
        for prefix in prefixes:
            if normalised.startswith(prefix) or prefix in normalised:
                return tier
    # Unclassified sections are loaded late: an unknown section is more likely to be
    # illustrative than decision-critical, and loading it early costs tokens on every call.
    return Tier.DETAIL


def _split_sections(body: str) -> list[Section]:
    """Split a body into `##`-level sections, preserving order.

    Only level-2 headings start a section; deeper headings stay inside their parent, which
    keeps a section's content together rather than fragmenting tables across tiers.
    """
    sections: list[Section] = []
    current_title: str | None = None
    current_level = 2
    buffer: list[str] = []

    def flush() -> None:
        if current_title is None:
            return
        sections.append(Section(
            title=current_title,
            body="\n".join(buffer).strip(),
            tier=_tier_for(current_title),
            level=current_level,
        ))

    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 2:
            flush()
            current_title = match.group(2).strip()
            current_level = 2
            buffer = []
            continue
        if current_title is None:
            continue
        buffer.append(line)
    flush()
    return sections


def _section_text(sections: Iterable[Section], *needles: str) -> str:
    """Concatenated body of every section whose title contains a needle."""
    wanted = [n.lower() for n in needles]
    return "\n".join(
        s.body for s in sections if any(w in s.title.lower() for w in wanted)
    )


def _verification_sections(sections: Iterable[Section]) -> list[Section]:
    """Sections that are a Verification section, by explicit heading match.

    Matched on the heading rather than by substring because `Verification` also appears
    inside tables and coordination rows; a substring search would pull in unrelated bodies
    and silently inflate the criteria.
    """
    out: list[Section] = []
    for section in sections:
        title = _normalise_heading(section.title)
        if _VERIFICATION_HEADING_RE.match(title) or title.startswith("verification"):
            out.append(section)
    return out


def _extract_complete_when(sections: Iterable[Section]) -> tuple[str, ...]:
    """Extract `Complete when:` checkpoint criteria from the workflow sections.

    This is the third documented criteria source, after a `workflow:` block and a
    Verification section. 241 of the library's skills carry these instead of either, so
    without it a large fraction of the corpus would be refused as ungated.
    """
    items: list[str] = []
    seen: set[str] = set()
    for section in sections:
        for line in section.body.splitlines():
            match = _COMPLETE_WHEN_RE.match(line)
            if not match:
                continue
            cleaned = _clean_text(match.group(1))
            # Deduplicate: many skills repeat the same boilerplate checkpoint verbatim.
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                items.append(cleaned)
    return tuple(items)


def _extract_verification(sections: Iterable[Section]) -> tuple[str, ...]:
    """Extract the Verification checklist — the fallback criteria source.

    Three shapes appear in the library, and all are collected because this section is the
    documented default criteria source:

    - `| **V1** | check | pass condition |` id tables
    - `| ☐ | Complete when ... | Verify ... |` checkbox tables
    - `- [ ] text` bullets
    """
    items: list[str] = []
    for section in _verification_sections(sections):
        for line in section.body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("<!--"):
                continue
            if set(stripped) <= set("|-: "):
                continue  # a table separator row

            row = _ID_ROW_RE.match(line)
            if row:
                identifier, rest = row.group(1), row.group(2)
                items.append(f"{identifier}: {_clean_text(rest)}")
                continue

            # A `| ☐ | Complete when ... | Verify ... |` row: the middle cell is the
            # criterion and the third is the evidence that satisfies it. Keeping both makes
            # the criterion checkable rather than merely stated.
            if stripped.startswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if len(cells) >= 2:
                    marker, condition = cells[0], cells[1]
                    lowered = condition.lower()
                    # A table header row (`| # | Complete when... | Verify |`) is a label,
                    # not a criterion. Its tell is the trailing ellipsis the library uses.
                    is_header = lowered.rstrip(".").endswith(("complete when", "check", "#")) or \
                        marker in ("#", "check", "id")
                    if is_header:
                        continue
                    if marker in ("☐", "[ ]", "x", "X", "☑") or "complete when" in lowered:
                        evidence = cells[2] if len(cells) > 2 else ""
                        clean_condition = _clean_text(condition)
                        if clean_condition:
                            entry = clean_condition
                            if evidence:
                                entry = f"{clean_condition} — {_clean_text(evidence)[:140]}"
                            items.append(entry)
                continue

            if stripped.startswith("#") or stripped.startswith(">"):
                continue
            bullet = _VERIFY_RE.match(line)
            if bullet:
                cleaned = _clean_text(bullet.group(1))
                if cleaned and not cleaned.endswith(":") and not cleaned.startswith("|"):
                    items.append(cleaned)
    return tuple(items)


def _extract_research_steps(sections: Iterable[Section]) -> tuple[str, ...]:
    """Extract the RP1–RP8 research rows, which every skill declares as a hard gate.

    Two shapes appear: the rich `| **RP1** | **Verify domain currency.** | ... |` table used
    by most skills, and the compact `| RP1 | topic |` table used by some. Both are
    collected; a skill's research gate is the reason the prompt insists on research before
    output, so missing it removes a guardrail.
    """
    steps: list[str] = []
    seen: set[str] = set()
    for section in sections:
        if not _RESEARCH_HEADING_RE.search(_normalise_heading(section.title)):
            continue
        for line in section.body.splitlines():
            stripped = line.strip()
            if set(stripped) <= set("|-: ") or not stripped:
                continue
            row = _ID_ROW_RE.match(line)
            if not row:
                continue
            identifier = row.group(1).replace(" ", "").replace("-", "").upper()
            if not identifier.startswith("RP"):
                continue
            if identifier in seen:
                continue
            seen.add(identifier)
            steps.append(f"{identifier}: {_clean_text(row.group(2))}")
    return tuple(steps)


def _extract_checklist(sections: Iterable[Section]) -> tuple[ChecklistItem, ...]:
    """Extract Production Checklist items with their bracketed ids.

    The id is what the orchestrator verifies the model's response against. Where the library
    supplies one (`[CR1]`) it is used verbatim; where it does not, a stable positional id
    (`PC1`, `PC2`, …) is assigned. Assigning one matters: an item the model cannot be held to
    is worse than no item, because it looks like coverage that does not exist.
    """
    text = _section_text(sections, "production checklist", "checklist")
    items: list[ChecklistItem] = []
    seen: set[str] = set()
    positional = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        if set(stripped) <= set("|-: "):
            continue
        # The bullet form: `- [ ] **[CR1]** text`
        match = _CHECKLIST_RE.match(line)
        if match:
            item_id, item_text = match.group(1), match.group(2)
            if item_id.upper().startswith("RP"):
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            items.append(ChecklistItem(id=item_id, text=_clean_text(item_text)))
            continue
        # The plain bullet form: `- [ ] text` with no bracketed id, and the numbered form
        # `1. text`. Both appear in the library's Production Checklist sections; missing the
        # numbered form silently drops the whole checklist for skills that use it.
        is_bullet = stripped.startswith(("- [", "* [", "- ", "* "))
        is_numbered = bool(re.match(r"^\d+[.)]\s+\S", stripped))
        if is_bullet or is_numbered:
            body = re.sub(r"^([-*]\s*(\[[ xX]\]\s*)?|\d+[.)]\s+)", "", stripped)
            cleaned = _clean_text(body)
            if not cleaned or cleaned.endswith(":"):
                continue
            positional += 1
            items.append(ChecklistItem(id=f"PC{positional}", text=cleaned))
    return tuple(items)


def _extract_anti_rationalization(frontmatter: dict[str, Any],
                                  sections: Iterable[Section]) -> tuple[str, ...]:
    """Extract the anti-rationalization rules.

    Two forms appear: a table with `AR1` ids, and a bolded `**AR-01 No Unbounded Skills:**
    ...` paragraph. Both are collected — these are the excuses the skill explicitly forbids,
    so dropping them removes a guardrail.
    """
    rules: list[str] = []
    text = _section_text(sections, "anti-rationalization")
    for line in text.splitlines():
        match = _AR_ROW_RE.match(line)
        if match:
            rules.append(f"{match.group(1)}: {_clean_text(match.group(2))}")
            continue
        para = re.match(r"^\s*\*{0,2}(AR[-\s]?\d+)\*{0,2}\s+(.+?)\s*$", line)
        if para:
            rules.append(f"{para.group(1)}: {_clean_text(para.group(2))}")
    return tuple(rules)


def _extract_ground_rules(sections: Iterable[Section]) -> tuple[str, ...]:
    """Extract the R1–R8 mechanical constraint rows from Ground Rules.

    These are the negative constraints whose violation the skill expects an explicit refusal
    for, so they are pinned into the prompt's primacy zone.
    """
    text = _section_text(sections, "ground rules")
    rules: list[str] = []
    for line in text.splitlines():
        match = _RULE_ROW_RE.match(line)
        if not match:
            continue
        rule_id, body = match.group(1), match.group(2)
        if rule_id.upper().startswith("RP") or rule_id.upper().startswith("AR"):
            continue
        cleaned = _clean_text(body)
        if cleaned:
            rules.append(f"{rule_id}: {cleaned}")
    return tuple(rules)


def _clean_text(text: str) -> str:
    """Normalise emphasis and whitespace out of an extracted string."""
    cleaned = re.sub(r"\*{1,2}(.+?)\*{1,2}", r"\1", text)
    cleaned = re.sub(r"`(.+?)`", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip().strip("|").strip()


def _contract_from_frontmatter(frontmatter: dict[str, Any]) -> SkillContract:
    """Build a contract from a `workflow:` block, tolerating a missing or partial one."""
    block = frontmatter.get("workflow") or {}
    if not isinstance(block, dict):
        return SkillContract()
    artifacts = block.get("artifacts") or {}
    completion = block.get("completion") or {}
    iteration = block.get("iteration") or {}

    def as_tuple(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,) if value.strip() else ()
        if isinstance(value, (list, tuple)):
            return tuple(str(v) for v in value if str(v).strip())
        return ()

    evidence = completion.get("evidence")
    iteration_max = iteration.get("max", 1)
    try:
        iteration_max = int(iteration_max)
    except (TypeError, ValueError):
        iteration_max = 1

    return SkillContract(
        inputs=as_tuple(artifacts.get("inputs")) if isinstance(artifacts, dict) else (),
        outputs=as_tuple(artifacts.get("outputs")) if isinstance(artifacts, dict) else (),
        criteria=as_tuple(completion.get("criteria")) if isinstance(completion, dict) else (),
        evidence_required=str(evidence).lower() == "required" if evidence is not None else False,
        escalate_to=as_tuple(block.get("escalate_to")) if isinstance(block, dict) else (),
        iteration_max=max(1, iteration_max),
        on_exhaustion=str(iteration.get("on_exhaustion", "escalate")) if isinstance(iteration, dict) else "escalate",
    )


def parse_skill(name: str, text: str, *, source_path: str = "") -> SkillBundle:
    """Turn a SKILL.md document into an enforceable :class:`SkillBundle`.

    Raises
    ------
    SkillError
        When the document has no parsable frontmatter, has no `name`, or declares no
        completion criteria at all. The last case is fatal on purpose: a node with no
        criteria can declare itself done without evidence, which is the single most
        expensive failure in agent workflows.
    """
    try:
        frontmatter, body = parse_frontmatter(text)
    except FrontmatterError as exc:
        raise SkillError(f"skill {name!r} has unparsable frontmatter: {exc}") from exc

    declared_name = str(frontmatter.get("name") or "").strip()
    if not declared_name:
        raise SkillError(
            f"skill {name!r} has no 'name' in its frontmatter; the directory name is not "
            "authoritative because chain references use the declared name"
        )

    sections = tuple(_split_sections(body))
    contract = _contract_from_frontmatter(frontmatter)

    verification = _extract_verification(sections)
    complete_when = _extract_complete_when(sections)
    if not contract.criteria:
        # The library documents three criteria sources, tried in order of authority: an
        # explicit `workflow:` block, then the Verification section, then the Core
        # Workflow's `Complete when:` checkpoints. Trying them in order is what keeps
        # nearly every skill runnable while still refusing one that genuinely states no
        # completion condition at all.
        if verification:
            contract = SkillContract(
                inputs=contract.inputs,
                outputs=contract.outputs,
                criteria=verification,
                evidence_required=contract.evidence_required,
                escalate_to=contract.escalate_to,
                iteration_max=contract.iteration_max,
                on_exhaustion=contract.on_exhaustion,
                criteria_from_fallback=True,
            )
        elif complete_when:
            contract = SkillContract(
                inputs=contract.inputs,
                outputs=contract.outputs,
                criteria=complete_when,
                evidence_required=contract.evidence_required,
                escalate_to=contract.escalate_to,
                iteration_max=contract.iteration_max,
                on_exhaustion=contract.on_exhaustion,
                criteria_from_fallback=True,
            )
        else:
            raise SkillError(
                f"skill {name!r} declares no completion criteria: no `workflow:` block, no "
                "Verification section, and no `Complete when:` checkpoint. A node without "
                "criteria cannot be gated, so this skill cannot be run safely."
            )

    chain = frontmatter.get("chain") or {}
    tags = frontmatter.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]

    return SkillBundle(
        name=declared_name,
        version=str(frontmatter.get("version") or ""),
        description=str(frontmatter.get("description") or "").strip(),
        token_budget=_as_int(frontmatter.get("token_budget")),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        skill_type=str(frontmatter.get("type") or ""),
        status=str(frontmatter.get("status") or ""),
        tags=tuple(str(t) for t in tags if str(t).strip()),
        consumes_from=tuple(str(v) for v in (chain.get("consumes_from") or [])) if isinstance(chain, dict) else (),
        feeds_into=tuple(str(v) for v in (chain.get("feeds_into") or [])) if isinstance(chain, dict) else (),
        contract=contract,
        checklist=_extract_checklist(sections),
        anti_rationalization=_extract_anti_rationalization(frontmatter, sections),
        research_steps=_extract_research_steps(sections),
        verification=verification,
        sections=sections,
        body=body,
        frontmatter=frontmatter,
        source_path=source_path,
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
