#!/usr/bin/env python3
"""Phase 25 tests — skill roles, derived from the skill rather than a maintained list.

Several sites need one answer: does this node judge, or produce? The answer decides whether a node is
bound to a *different* agent than its producer, denied write tools, and placed in the plan's verifier
fan-out. Each site had grown its own hardcoded set, so a skill added to one was invisible to the
other. These tests pin the single derived answer.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.library import resolve
from engine.skills import FilesystemSkillSource
from engine.skills.roles import (
    KNOWN_VERIFIERS,
    PRODUCER,
    VERIFIER,
    classify,
    is_reviewer_name,
    is_verifier,
    verifier_skills_for,
)


@pytest.fixture(scope="module")
def source():
    return FilesystemSkillSource(resolve())


# ── naming ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "code-reviewer", "security-reviewer", "contract-completeness-review",
    "accessibility-auditor", "critical-thinker", "verification-before-completion",
    "smart-contract-auditor", "ai-safety-health-reviewer",
])
def test_judging_names_are_recognised(name):
    assert is_reviewer_name(name)


@pytest.mark.parametrize("name", [
    "backend-developer", "system-architect", "ceo-strategist", "growth-engineer",
    "security-engineer", "api-designer", "product-manager", "data-engineer",
])
def test_producing_names_are_not_judging_names(name):
    """`security-engineer` builds; `security-reviewer` judges — the library distinguishes them."""
    assert not is_reviewer_name(name)


# ── classification against the real corpus ───────────────────────────────────


def test_a_producer_is_classified_as_producing(source):
    verdict = classify("backend-developer", source.load("backend-developer"))
    assert verdict.role == PRODUCER
    assert not verdict.is_verifier
    assert verdict.reason


def test_a_verifier_is_classified_as_judging(source):
    verdict = classify("code-reviewer", source.load("code-reviewer"))
    assert verdict.role == VERIFIER
    assert verdict.is_verifier


def test_security_engineer_produces_but_security_reviewer_judges(source):
    """The exact distinction a hardcoded name-substring check would get wrong."""
    assert classify("security-engineer", source.load("security-engineer")).role == PRODUCER
    assert classify("security-reviewer", source.load("security-reviewer")).role == VERIFIER


def test_classification_works_without_a_bundle():
    """The binder classifies from a manifest's skill name, with no bundle to load."""
    assert is_verifier("code-reviewer") is True
    assert is_verifier("backend-developer") is False


def test_a_verdict_output_makes_a_verifier_source():
    """A skill whose output *is* a judgement is a verifier even if its name is not obviously one."""

    class _Bundle:
        class contract:
            outputs = ("conformance-report",)
        description = ""

    assert classify("schema-conformance", _Bundle()).role == VERIFIER


def test_a_plan_output_does_not_make_a_verifier():
    """A `-plan` output is a proposal, not a verdict — including it swept a formatter into review."""

    class _Bundle:
        class contract:
            outputs = ("enforcement-plan",)
        description = ""

    assert classify("code-formatting-and-linting", _Bundle()).role == PRODUCER


def test_a_partial_bundle_still_classifies():
    assert classify("code-reviewer", object()).is_verifier


def test_the_known_set_is_a_floor_not_the_whole_answer(source):
    """Every known verifier still classifies as a verifier, and the corpus adds more."""
    for name in KNOWN_VERIFIERS:
        assert is_verifier(name) is True
    # A skill recognised by name but not in the set proves derivation adds coverage.
    derived = [n for n in source.names()
               if is_verifier(n) and n not in KNOWN_VERIFIERS]
    assert derived, "the corpus has judging skills beyond the frozen set"


def test_verifier_skills_for_filters_a_candidate_set(source):
    filtered = verifier_skills_for(
        ["backend-developer", "code-reviewer", "qa-engineer", "system-architect"], source)
    assert "backend-developer" not in filtered
    assert "code-reviewer" in filtered and "qa-engineer" in filtered


def test_classification_is_deterministic(source):
    assert classify("code-reviewer", source.load("code-reviewer")) == \
        classify("code-reviewer", source.load("code-reviewer"))


# ── the binder uses the shared answer ────────────────────────────────────────


def test_the_planner_gives_a_derived_verifier_the_verify_phase(source):
    """`_phase_for` derives VERIFY from the skill, so a new judging skill needs no table edit."""
    from engine.planner import _phase_for

    assert _phase_for("code-reviewer") == "VERIFY"
    assert _phase_for("backend-developer") == "BUILD"
    # A judging skill the tables never listed still gets VERIFY.
    assert _phase_for("smart-contract-auditor") == "VERIFY"
