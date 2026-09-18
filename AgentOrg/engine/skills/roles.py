#!/usr/bin/env python3
"""roles.py — what *kind* of work a skill is, read from the skill itself.

WHY THIS EXISTS
---------------
Several places in the engine need to answer one question about a skill: **does this node produce a
change, or does it judge one?** The answer decides real behaviour —

- a reviewer must be bound to a *different* agent than the producer (`binding.assert_independent`),
- a reviewer gets `phase=REVIEW` and is denied the write tools,
- the planner puts verifiers in the fan-out and the rework loop rather than the build chain.

Each of those sites had grown its own hardcoded list, and the lists disagreed:

```python
# binding.py
{"code-reviewer", "security-reviewer", "qa-engineer",
 "accessibility-auditor", "performance-engineer", "contract-completeness-review"}
# planner.py — a *different* set, per domain
```

Two copies of "which skills are reviewers" is how the two silently diverge: a skill added as a
reviewer in one place is a producer in the other, and a plan binds it to its own producer. Worse,
neither list can see a skill the library adds, so a new verifier is invisible until someone edits
both.

This module derives the role from the skill's own metadata, so it is total over the library rather
than a list someone maintains:

- **the name** — `*-reviewer`, `*-auditor`, `*-qa`, `*-checker`, `*-validator`, `*-critic`, and the
  `verification-*` family, which is the library's own naming convention for a judging role;
- **`outputs`** — a skill whose declared output is a `*-report` / `*-audit` / `*-verdict` is
  producing a judgement, not a change;
- **the description** — the explicit "Do NOT use for …" and "verdict"/"review" language the library
  writes into every skill.

DESIGN
------
- **Deterministic and explainable.** `classify` returns the role *and the reason*, because "why is
  this node treated as a reviewer?" is a question an operator will ask and a bare boolean cannot
  answer.
- **The old sets remain the floor.** A skill named in `KNOWN_VERIFIERS` is a verifier even if its
  metadata is thin, so replacing the hardcoded lists cannot *lose* a role that already worked. The
  derived signal only ever adds.
- **Consumed, not duplicated.** `binding.py`, `planner.py` and `executor.py` all call this module,
  so there is exactly one answer to "what kind of work is this?".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "PRODUCER",
    "VERIFIER",
    "RoleVerdict",
    "classify",
    "is_verifier",
    "is_reviewer_name",
    "KNOWN_VERIFIERS",
    "verifier_skills_for",
]


#: The roles a skill can play in a graph. `verifier` judges (and so must be independent of the
#: producer); `producer` changes or creates. Deliberately just two: a "gate" is a graph concept the
#: planner declares explicitly, and guessing it from metadata produced wrong answers.
PRODUCER = "producer"
VERIFIER = "verifier"


#: The floor: skills already treated as verifiers elsewhere in the engine. Kept so replacing the
#: hardcoded lists cannot lose a role that already worked. The derived signal below only *adds*.
KNOWN_VERIFIERS: frozenset[str] = frozenset({
    "code-reviewer", "security-reviewer", "qa-engineer", "accessibility-auditor",
    "performance-engineer", "contract-completeness-review", "verification-before-completion",
    "critical-thinker", "product-analyst", "roi-gate", "ai-safety-health-reviewer",
    "medical-content-reviewer", "smart-contract-auditor",
})

#: Names that read as a judging role. Deliberately narrow: `*-engineer` and `*-developer` build, so
#: `security-engineer` (a producer) is *not* matched by the reviewer pattern the way
#: `security-reviewer` is — the distinction the library makes by name.
_VERIFIER_NAME_RE = re.compile(
    r"(^|-)("
    r"reviewer|review|auditor|audit|critic|checker|validator|verifier"
    r")(-|$)"
)
#: The `verification-*` family: the library's explicit naming for a judging procedure.
_VERIFIER_PREFIX_RE = re.compile(r"^verification(-|$)")
#: `-qa` / `qa-` as a standalone token.
_QA_NAME_RE = re.compile(r"(^|-)qa(-|$)")

#: An output artifact that *is* a judgement. `-report`, `-audit`, `-verdict`, `-findings`.
#: Deliberately excludes `-plan`: a plan is a proposal, not a verdict — including it swept
#: `code-formatting-and-linting` (outputs `enforcement-plan`) into the verifier set by mistake.
_VERDICT_OUTPUT_RE = re.compile(r"(report|audit|verdict|findings|review)$")

#: Phrases in the description that state a judging role explicitly.
_VERIFIER_DESC_RE = re.compile(
    r"(review|audit|verdict|pass or changes_requested|severity|adversarial|"
    r"verif|critique|falsif|quality gate|assess(ment|es)?\b)"
)


@dataclass(frozen=True)
class RoleVerdict:
    """The role of one skill, and why — so a decision is explainable, not just asserted."""

    skill: str
    role: str
    reason: str

    @property
    def is_verifier(self) -> bool:
        return self.role == VERIFIER

    def as_dict(self) -> dict[str, Any]:
        return {"skill": self.skill, "role": self.role, "reason": self.reason,
                "is_verifier": self.is_verifier}


def is_reviewer_name(skill: str) -> bool:
    """Whether a skill's *name* reads as a judging role. Cheap, and does not need the library."""
    name = str(skill or "").strip().lower()
    if not name:
        return False
    if name in KNOWN_VERIFIERS:
        return True
    return bool(_VERIFIER_NAME_RE.search(name)
                or _VERIFIER_PREFIX_RE.search(name)
                or _QA_NAME_RE.search(name))


def classify(skill: str, bundle: Any = None) -> RoleVerdict:
    """The role of one skill: producer, verifier, or gate.

    `bundle` is optional — with only a name the classification uses the naming convention and the
    known set, which is enough for `binding` (it binds from a manifest, not from bundles). Given a
    bundle, the declared `outputs` and the description strengthen the answer.
    """
    name = str(skill or "").strip().lower()
    if name in KNOWN_VERIFIERS:
        return RoleVerdict(skill, VERIFIER, "in the known-verifier set")

    outputs: list[str] = []
    description = ""
    if bundle is not None:
        try:
            contract = getattr(bundle, "contract", None)
            outputs = [str(o).lower() for o in (getattr(contract, "outputs", ()) or ())]
            description = str(getattr(bundle, "description", "") or "")
        except Exception:  # noqa: BLE001 - a partial bundle still classifies by name
            outputs, description = [], ""

    if is_reviewer_name(name):
        return RoleVerdict(skill, VERIFIER, f"the name {skill!r} reads as a judging role")

    # An output that is a judgement rather than an artifact. This catches a verifier whose *name* is
    # not obviously a reviewer — `contract-completeness-review` is caught by name, but a future
    # `schema-conformance` whose output is `conformance-report` is caught here.
    for output in outputs:
        if _VERDICT_OUTPUT_RE.search(output):
            return RoleVerdict(
                skill, VERIFIER,
                f"its declared output {output!r} is a judgement, not a change")

    # Description language. Weaker, so it is checked last and only when it is unambiguous about a
    # verdict. `skill_type == quality` is a supporting signal, not sufficient on its own (some
    # quality skills, like a test-suite builder, do produce).
    if description and _VERIFIER_DESC_RE.search(description.lower()):
        # Require a verdict-ish noun AND a judging verb, so a producer that merely mentions a
        # "review step" is not swept in.
        strong = re.search(r"(verdict|review-report|severity grading|adversarial|"
                           r"confirm none|pass or changes_requested)", description.lower())
        if strong:
            return RoleVerdict(
                skill, VERIFIER,
                f"its description states a verdict role ({strong.group(0)!r})")

    # No signal that it judges: it produces. The default is deliberately `producer` rather than a
    # guess at `gate` — a node wrongly treated as a verifier is bound to a different agent and denied
    # write tools, which breaks real work, whereas a gate is a narrower claim this cannot make
    # reliably from metadata alone.
    return RoleVerdict(skill, PRODUCER, "no signal that it judges rather than produces")


def is_verifier(skill: str, bundle: Any = None) -> bool:
    """Whether a skill owes a verdict rather than a change."""
    return classify(skill, bundle).is_verifier


def verifier_skills_for(candidates: Iterable[str], source: Any = None) -> list[str]:
    """Filter a set of candidate skill names to those that judge.

    `source` is an optional skill source; when given, each candidate is classified with its bundle
    so `outputs` and the description contribute. When omitted, only the name convention is used —
    which is enough for the reviewer-skill check the binder needs.
    """
    out: list[str] = []
    for name in candidates:
        bundle = None
        if source is not None:
            try:
                bundle = source.load(name)
            except Exception:  # noqa: BLE001 - an unloadable candidate is classified by name
                bundle = None
        if is_verifier(name, bundle):
            out.append(str(name))
    return out
