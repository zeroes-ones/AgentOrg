#!/usr/bin/env python3
"""authoring.py — writing your own skills, in the library's own format.

WHY THIS EXISTS
---------------
The 327 library skills are procedures, and a procedure you cannot write is a tool you can only borrow.
"Train skills" in the Owner's words means authoring one: a markdown file with a contract, completion
criteria and a checklist, which the engine then enforces exactly as it enforces a library skill.

The whole design goal is that an authored skill is **indistinguishable from a library one**. Same
frontmatter, same headings, same checklist grammar — so the same parser reads both, and a custom skill
gets the checklist-enforcement, the evidence requirement and the gate treatment for free. A second
format would drift; there is deliberately only one.

DESIGN
------
- **Generated, then editable.** `new` writes a complete, valid, enforceable skill with the sections
  the parser looks for, so the first round-trip works before you have written a word. The file is
  plain markdown: you edit it afterwards.
- **Criteria are required and named.** A skill with no completion criteria cannot gate a node, so the
  scaffold insists on at least one and the loader refuses a file that has none. The refusal names the
  file and the line, because "this skill silently has no criteria" is the failure worth preventing.
- **Checklist ids are explicit.** The prompt requires every id to be reported with evidence, so the ids
  are written as `[XX1]` in a `## Verification` section — the form the parser's own regex accepts.
- **Nothing is written into the library.** Authored skills go to the layered user roots, because the
  library is commit-pinned and hash-verified; writing there would break the pin.

Usage:
    from engine.authoring import scaffold, write_skill, SkillTemplate
    path = write_skill(SkillTemplate(name="db-migrator", purpose="…"),
                       criteria=["…"], checklist=["…"], root=Path(".agentorg"))
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import usercfg

__all__ = ["SkillTemplate", "AuthoringError", "scaffold", "write_skill", "slugify"]


class AuthoringError(RuntimeError):
    """An authored skill that could not be written, named so the reason is actionable."""


#: A skill name must be a slug: the directory name *is* the name, and the parser keys on it.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass
class SkillTemplate:
    """What the Owner asked to create.

    The fields mirror the library's frontmatter where they matter to the engine — `name` (the
    identity), `purpose` (the description) and `tags` — and omit the rest rather than inventing
    values for fields nothing reads.
    """

    name: str
    purpose: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    escalate_to: list[str] = field(default_factory=lambda: ["human-gate"])
    author: str = "Owner"


def slugify(text: str) -> str:
    """Turn a phrase into a valid skill name.

    Offered so `skills new "Database Migration"` works without the user having to know the naming rule
    — and so the *same* rule is applied whether the name was typed or derived.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:64].strip("-")


def _yaml_list(items: list[str], indent: int = 2) -> str:
    pad = " " * indent
    return "\n".join(f"{pad}- {item}" for item in items)


def _yaml_block_scalar(text: str, indent: int = 0) -> str:
    """A folded scalar for `key: >-`, with the indicator on the **key's own line**.

    The indicator must follow the key on the same line. This used to return the `>-` on a line of its
    own — `description:` then `  >-` — which is still valid YAML and PyYAML reads it, but the engine's
    own stdlib parser (the one a machine without PyYAML uses) refuses it: *"line is neither a
    'key: value' mapping nor a sequence item: '>-' (frontmatter line 3)"*.

    So `skills new` wrote a skill the engine could only read where PyYAML happened to be installed.
    Five tests in `tests/test_phase8_authoring.py` passed on a developer machine and failed in CI for
    exactly that reason — the clean environment was the only one telling the truth. The output is now
    the same shape every library skill uses, and both parsers read it.
    """
    pad = " " * (indent + 2)
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return ">-"
    return ">-\n" + "\n".join(f"{pad}{line}" for line in lines)


def scaffold(template: SkillTemplate, *, criteria: list[str], checklist: list[str],
             ground_rules: list[str] | None = None) -> str:
    """Render a complete skill document.

    The output is written to satisfy the engine's own parser, not to look nice: a `workflow:` block
    with `completion.criteria` and `evidence: required`, and a `## Verification` section whose rows
    are `- [ ] [XX1] …` so the ids are extracted exactly as a library skill's are.
    """
    name = slugify(template.name)
    if not _NAME_RE.match(name):
        raise AuthoringError(
            f"{template.name!r} does not reduce to a usable skill name (got {name!r}). A skill name "
            "must start with a letter or digit and contain only letters, digits, dot, dash or "
            "underscore."
        )
    if not criteria:
        raise AuthoringError(
            "a skill with no completion criteria cannot gate a node, so it would never be enforced. "
            "Pass at least one criterion."
        )
    if not checklist:
        raise AuthoringError(
            "a skill with no checklist has nothing to enforce per item. Pass at least one item, or "
            "use the completion criteria alone deliberately."
        )

    prefix = _prefix_for(name)
    description = template.description or template.purpose or f"Use when {name.replace('-', ' ')} is needed."
    tags = template.tags or [t for t in name.split("-") if t]

    front = [
        "---",
        f"name: {name}",
        f"description: {_yaml_block_scalar(description)}",
        "license: MIT",
        "tags:",
        _yaml_list(tags),
        f"author: {template.author}",
        "type: custom",
        "status: draft",
        "version: 1.0.0",
    ]
    if template.inputs or template.outputs:
        front.append("workflow:")
        front.append("  artifacts:")
        front.append("    inputs: " + ("[" + ", ".join(template.inputs or ["task"]) + "]"))
        front.append("    outputs: " + ("[" + ", ".join(template.outputs or ["change"]) + "]"))
    else:
        front.append("workflow:")
        front.append("  artifacts:")
        front.append("    inputs: [task]")
        front.append("    outputs: [change]")
    front.append("  completion:")
    front.append("    criteria:")
    front.extend(f"      - {c}" for c in criteria)
    front.append("    evidence: required")
    front.append("  escalate_to: " + ("[" + ", ".join(template.escalate_to) + "]"))
    front.append("---")

    rules = ground_rules or [
        "MUST NOT claim a result you cannot evidence.",
        "MUST NOT skip a checklist item — mark it FAIL or N/A with a reason instead.",
    ]

    body = [
        "",
        f"# {name.replace('-', ' ').title()}",
        "",
        description,
        "",
        "## Ground Rules",
        "",
        *[f"- {rule}" for rule in rules],
        "",
        "## Core Workflow",
        "",
        "### Phase 1 — Understand",
        "",
        "Read the inputs, restate the task in your own words, and name anything ambiguous before "
        "producing output.",
        "",
        f"> Complete when: the task is restated and the inputs are listed.",
        "",
        "### Phase 2 — Produce",
        "",
        "Do the work. Every claim gets a concrete evidence path, a hash, or command output.",
        "",
        f"> Complete when: {'; '.join(criteria)}.",
        "",
        "### Phase 3 — Verify",
        "",
        "Check the output against each completion criterion above. A criterion you cannot evidence is "
        "an open item, not a pass.",
        "",
        "> Complete when: every criterion is either evidenced or recorded as an open question.",
        "",
        "## Research Prerequisites",
        "",
        "1. **Check currency.** Verify nothing here has been superseded since your knowledge cutoff, "
        "and say so if you cannot.",
        "2. **Read before writing.** Inspect the actual files involved; do not infer their contents.",
        "",
        "## Anti-Hallucination",
        "",
        "- ❌ \"It should work\" — run the check, or say you did not.",
        "- ❌ \"Presumably the convention is…\" — read the file.",
        "- ❌ \"Tests pass\" — quote the output or do not claim it.",
        "",
        "## Production Checklist",
        "",
        "Report every id below as PASS, FAIL or N/A **with evidence**.",
        "",
        *[f"- [ ] **[{prefix}{i}]** {item}" for i, item in enumerate(checklist, start=1)],
        "",
        "## Verification",
        "",
        "Before you report done, confirm each completion criterion above is either evidenced or "
        "recorded as an open question.",
        "",
        "## What Good Looks Like",
        "",
        "- Evidence is a path, a hash or command output — never a restatement of the criterion.",
        "- Open questions are recorded rather than resolved by guessing.",
        "",
    ]
    return "\n".join(front + body)


def _prefix_for(name: str) -> str:
    """A short checklist id prefix derived from the skill name.

    Derived rather than fixed so the ids in a prompt read as this skill's own (`DMP1`) rather than a
    generic `C1` shared by every custom skill, which would make a trace harder to read when two are
    in play.
    """
    letters = "".join(part[0] for part in name.split("-") if part)[:3].upper()
    return (letters or "SK") + ""


def write_skill(template: SkillTemplate, *, criteria: list[str], checklist: list[str],
                root: Path | str | None = None, global_: bool = False,
                ground_rules: list[str] | None = None, overwrite: bool = False) -> Path:
    """Write a scaffolded skill into the chosen root. Returns the path to its `SKILL.md`."""
    name = slugify(template.name)
    if root is not None:
        base = Path(root)
    else:
        base = usercfg.global_root() if global_ else usercfg.project_root()
    directory = base / "skills" / name
    target = directory / "SKILL.md"
    if target.exists() and not overwrite:
        raise AuthoringError(
            f"{target} already exists. Pass overwrite=True (or --force) to replace it — it may have "
            "been edited by hand since it was generated."
        )
    text = scaffold(template, criteria=criteria, checklist=checklist, ground_rules=ground_rules)
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
