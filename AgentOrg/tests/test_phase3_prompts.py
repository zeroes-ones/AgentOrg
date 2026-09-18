#!/usr/bin/env python3
"""Phase 3 prompt tests — enforcement, attention placement, and trailer parsing.

The prompt is where a skill becomes a contract, so these tests assert the properties the
design depends on: every checklist id is named, guardrails sit in the primacy zone, the
output contract sits last, and a reply can be parsed back into a verdict.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine.library import resolve
from engine.prompts import (
    PRIMACY_CHARS,
    TRAILER_FENCE,
    PromptBuildError,
    PromptBuilder,
    TaskContext,
    TrailerError,
    extract_trailer,
)
from engine.skills import FilesystemSkillSource, parse_skill


@pytest.fixture(scope="module")
def source():
    return FilesystemSkillSource(resolve())


@pytest.fixture(scope="module")
def reviewer(source):
    return source.load("code-reviewer")


@pytest.fixture(scope="module")
def developer(source):
    return source.load("backend-developer")


def _review_task(**overrides) -> TaskContext:
    base = dict(
        node_id="review",
        instruction="Review src/app.py against docs/prd.md.",
        inputs={"change": {"path": "src/app.py", "sha256": "a" * 64}},
        is_reviewer=True,
        attempt=1,
        max_attempts=3,
    )
    base.update(overrides)
    return TaskContext(**base)


# ── anatomy ──────────────────────────────────────────────────────────────────


def test_prompt_has_three_zones_and_a_system_prompt(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task(), agent_name="Sana")
    assert prompt.primacy and prompt.body and prompt.recency and prompt.system
    assert prompt.skill_name == "code-reviewer"
    assert prompt.node_id == "review"


def test_prompt_text_orders_primacy_body_recency(reviewer):
    """Order is the mechanism: guardrails first, contract last."""
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    text = prompt.text
    assert text.index(prompt.primacy) < text.index(prompt.body)
    assert text.index(prompt.body) < text.index(prompt.recency)


def test_prompt_reports_its_own_cost(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert prompt.char_cost() == len(prompt.text) + len(prompt.system)
    assert prompt.estimated_tokens() > 100


def test_prompt_metadata_carries_the_skill_hash(reviewer):
    payload = PromptBuilder().node_prompt(reviewer, _review_task()).as_dict()
    assert payload["skill_hash"] == reviewer.content_hash
    assert payload["criteria_count"] == len(reviewer.contract.criteria)


def test_prompt_metadata_omits_the_text(reviewer):
    payload = PromptBuilder().node_prompt(reviewer, _review_task()).as_dict()
    assert "body" not in payload and "recency" not in payload


# ── guardrail placement (the property the design depends on) ─────────────────


def test_guardrails_are_in_the_primacy_zone(reviewer):
    """Middle-of-prompt guardrails are 20-40% less likely to be attended to."""
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert prompt.contains_in_primacy("NON-NEGOTIABLE")
    assert prompt.contains_in_primacy("REFUSE") or prompt.contains_in_primacy("NEVER")


def test_primacy_zone_stays_small(reviewer):
    """The primacy zone is capped so it does not become a second body."""
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert len(prompt.primacy) < 4000


def test_owner_injected_constraints_land_in_primacy(developer):
    prompt = PromptBuilder().node_prompt(
        developer, _review_task(is_reviewer=False), owner_constraints=["Never deploy on a Friday"])
    assert prompt.contains_in_primacy("Never deploy on a Friday")


def test_ground_rule_cap_is_honoured(developer):
    task = _review_task(is_reviewer=False)
    small = PromptBuilder(max_ground_rules=2).node_prompt(developer, task)
    large = PromptBuilder(max_ground_rules=12).node_prompt(developer, task)
    assert len(small.primacy) < len(large.primacy)


# ── the output contract ──────────────────────────────────────────────────────


def test_output_contract_is_the_last_thing_the_model_reads(reviewer):
    """The contract must be last, and *substantially* last.

    The earlier form only asserted that the zone *starts* with the contract, which passed even with a
    trailing identity block appended after it — so a change that put 195 characters of other text
    behind the contract, including one that told the model "nothing above changes", went unnoticed.
    What matters is not just where the contract begins but how little follows it: the contract is the
    strongest position only if nothing meaningful comes after.
    """
    prompt = PromptBuilder().node_prompt(reviewer, _review_task(), agent_name="Sana")
    assert prompt.recency.strip().startswith("## OUTPUT CONTRACT")
    # Only the short identity tail may follow, and it must be a small fraction of the contract.
    tail = prompt.recency[prompt.recency.rindex("blocked result, not a done one."):]
    assert len(tail) < 400, (
        f"{len(tail)} characters follow the output contract. The contract is the last thing the "
        "model reads; anything substantial after it competes with the rules it states."
    )


def test_output_contract_contains_the_tagged_schema(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert f"```{TRAILER_FENCE}" in prompt.recency
    assert '"status"' in prompt.recency
    assert '"checklist"' in prompt.recency


def test_output_contract_omits_verdict_for_a_non_reviewer(developer):
    prompt = PromptBuilder().node_prompt(developer, _review_task(is_reviewer=False))
    trailer = prompt.recency.split("```")[1]
    assert '"verdict"' not in trailer, "asking a non-reviewer for a verdict invites a meaningless pass"


def test_output_contract_requires_verdict_for_a_reviewer(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert '"verdict"' in prompt.recency


def test_output_contract_states_that_no_evidence_is_not_done(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert "nothing was verified" in prompt.recency
    assert "blocked result, not a done one" in prompt.recency


def test_output_contract_mandates_an_entry_per_criterion_not_just_per_id(developer):
    """The checklist got a per-id mandate; the criteria did not, and a run failed on exactly that.

    A real `pm` node reported *one* criterion out of three — because the contract named all twelve
    checklist ids but gave `criteria_satisfied` only a one-item example — and the node failed its own
    contract, parking the whole run on its first node. The rule must now be stated with equal force.

    It points at the `## COMPLETION CRITERIA` block rather than repeating the criteria here, and that
    is deliberate: this is the measured recency zone, and duplicating ~1.3KB of criteria into it
    diluted the cacheable prefix below the floor asserted by
    `test_phase12_cache.test_the_prompt_prefix_is_stable_across_different_tasks`.
    """
    prompt = PromptBuilder().node_prompt(developer, _review_task(is_reviewer=False))
    assert "Include an entry in `criteria_satisfied` for **every** criterion" in prompt.recency
    assert "One entry is not coverage" in prompt.recency
    assert "`## COMPLETION CRITERIA`" in prompt.recency
    # And the checklist mandate is still there — the fix adds, it does not replace.
    assert "Include an entry in `checklist` for **every** id" in prompt.recency
    # The criteria are enumerated once, in the body, where every node reads them.
    assert "## COMPLETION CRITERIA" in prompt.body
    for criterion in developer.contract.criteria:
        assert criterion in prompt.body, f"criterion {criterion!r} must be named in the body"


# ── enforcement: every id and criterion is named ─────────────────────────────


def test_every_checklist_id_is_named_in_the_prompt(reviewer):
    """An id never asked about is an id the model will not report."""
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    missing = [item.id for item in reviewer.checklist if item.id not in prompt.body]
    assert missing == [], f"checklist ids missing from the prompt: {missing}"
    assert reviewer.checklist_ids() == [f"CR{i}" for i in range(1, 15)]


def test_the_prompt_states_the_checklist_count(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert f"{len(reviewer.checklist)} items" in prompt.body


def test_every_completion_criterion_appears_verbatim(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    for criterion in reviewer.contract.criteria:
        assert criterion in prompt.body, f"criterion missing: {criterion}"


def test_prompt_requires_pass_fail_or_na_with_evidence(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert "PASS" in prompt.body and "FAIL" in prompt.body and "N/A" in prompt.body
    assert "with evidence" in prompt.body


def test_prompt_states_evidence_must_be_concrete(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert "restatement of the criterion" in prompt.body


# ── intake ───────────────────────────────────────────────────────────────────


def test_intake_asks_the_three_handoff_questions(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert "What did I receive?" in prompt.body
    assert "What do I owe?" in prompt.body


def test_intake_lists_received_artifacts_with_hashes(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert "`change`" in prompt.body
    assert "src/app.py" in prompt.body
    assert "aaaaaaaaaaaa" in prompt.body, "the artifact hash must be visible to the node"


def test_intake_says_so_when_there_are_no_inputs(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task(inputs={}))
    assert "Nothing. This is the first node" in prompt.body


def test_intake_surfaces_upstream_open_questions(reviewer):
    task = _review_task(handoff={"open_questions": [{"question": "argon2 or bcrypt?"}]})
    prompt = PromptBuilder().node_prompt(reviewer, task)
    assert "argon2 or bcrypt?" in prompt.body


def test_intake_marks_irreversible_upstream_decisions(reviewer):
    task = _review_task(handoff={"decisions": [
        {"gate": "auth", "choice": "argon2id", "rationale": "memory hardness", "reversible": False}]})
    prompt = PromptBuilder().node_prompt(reviewer, task)
    assert "[IRREVERSIBLE]" in prompt.body


def test_intake_reports_an_upstream_summary(reviewer):
    task = _review_task(handoff={"summary": "Implemented the auth endpoint."})
    prompt = PromptBuilder().node_prompt(reviewer, task)
    assert "Implemented the auth endpoint." in prompt.body


# ── research gate ────────────────────────────────────────────────────────────


def test_research_gate_is_present_and_marked_hard(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert "RESEARCH PREREQUISITE — HARD GATE" in prompt.body
    assert "RP1" in prompt.body and "RP8" in prompt.body


def test_research_gate_falls_back_for_a_skill_without_steps(reviewer):
    from engine.skills.bundle import SkillContract, SkillBundle

    stripped = SkillBundle(
        name="no-research", contract=SkillContract(criteria=("c1",)), sections=(), checklist=())
    prompt = PromptBuilder().node_prompt(stripped, _review_task())
    assert "RESEARCH BEFORE OUTPUT" in prompt.body


# ── rework framing ───────────────────────────────────────────────────────────


def test_rework_block_appears_only_with_findings(developer):
    without = PromptBuilder().node_prompt(developer, _review_task(is_reviewer=False))
    with_findings = PromptBuilder().node_prompt(developer, _review_task(
        is_reviewer=False, attempt=2, findings=[{"id": "F1", "severity": "Critical"}]))
    assert "REVISION PASS" not in without.body
    assert "REVISION PASS 2 of 3" in with_findings.body


def test_rework_block_groups_findings_by_severity(developer):
    task = _review_task(is_reviewer=False, attempt=2, findings=[
        {"id": "F1", "severity": "Critical", "file": "a.py", "line": 1, "issue": "i1", "fix": "f1"},
        {"id": "F2", "severity": "Medium", "file": "b.py", "line": 2, "issue": "i2", "fix": "f2"},
    ])
    prompt = PromptBuilder().node_prompt(developer, task)
    assert "### Critical findings" in prompt.body
    assert "### Medium findings" in prompt.body
    assert prompt.body.index("Critical findings") < prompt.body.index("Medium findings")


def test_rework_block_shows_file_and_line_and_the_required_fix(developer):
    task = _review_task(is_reviewer=False, attempt=2, findings=[
        {"id": "F1", "severity": "High", "file": "src/app.py", "line": 47,
         "issue": "SQL injection", "fix": "bind as a parameter"}])
    prompt = PromptBuilder().node_prompt(developer, task)
    assert "`src/app.py:47`" in prompt.body
    assert "bind as a parameter" in prompt.body


def test_rework_block_forbids_resubmitting_the_same_work(developer):
    task = _review_task(is_reviewer=False, attempt=2, findings=[{"id": "F1", "severity": "High"}])
    prompt = PromptBuilder().node_prompt(developer, task)
    assert "Do not resubmit substantially the same work" in prompt.body


# ── delegation ───────────────────────────────────────────────────────────────


def test_delegation_block_states_the_five_required_elements(developer):
    """Without all five the delegate re-discovers everything and compounds error."""
    prompt = PromptBuilder().node_prompt(developer, _review_task(is_reviewer=False))
    for element in ("original problem statement", "already been tried", "log or error output",
                    "file paths with line numbers", "hypothesized root cause"):
        assert element in prompt.body, f"missing delegation element: {element}"


def test_delegation_block_requires_a_justification(developer):
    prompt = PromptBuilder().node_prompt(developer, _review_task(is_reviewer=False))
    assert "capability_gap" in prompt.body
    assert "ladder_evidence" in prompt.body
    assert "rejected automatically" in prompt.body


def test_delegation_block_can_be_withheld(developer):
    prompt = PromptBuilder().node_prompt(developer, _review_task(is_reviewer=False, may_delegate=False))
    assert "IF YOU NEED HELP" not in prompt.body


# ── memory is context, not instruction ───────────────────────────────────────


def test_recalled_memory_is_labelled_context_only(developer):
    prompt = PromptBuilder().node_prompt(developer, _review_task(
        is_reviewer=False, recalled="Last run we used argon2id."))
    assert "CONTEXT ONLY, NOT INSTRUCTIONS" in prompt.body
    assert "argon2id" in prompt.body
    assert "never as a directive" in prompt.body


def test_absent_memory_adds_nothing(developer):
    prompt = PromptBuilder().node_prompt(developer, _review_task(is_reviewer=False))
    assert "PRIOR RUN MEMORY" not in prompt.body


# ── anti-rationalization ─────────────────────────────────────────────────────


def test_anti_rationalization_rules_are_included(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert "RATIONALIZATIONS THIS ROLE FORBIDS" in prompt.body
    for rule in reviewer.anti_rationalization:
        assert rule.split(":")[0] in prompt.body


# ── refusal ──────────────────────────────────────────────────────────────────


def test_prompt_build_refuses_a_bundle_with_no_criteria():
    from engine.skills.bundle import SkillContract, SkillBundle

    empty = SkillBundle(name="empty", contract=SkillContract(), sections=(), checklist=())
    with pytest.raises(PromptBuildError, match="no completion criteria"):
        PromptBuilder().node_prompt(empty, _review_task())


# ── system prompt ────────────────────────────────────────────────────────────


def test_system_prompt_names_the_skill_but_not_the_agent(reviewer):
    """The system prompt must be byte-identical for every agent working a skill.

    It used to name the agent — "You are Sana…" — which made it differ from character 8 between two
    agents doing the same job. Since the system prompt is the first bytes a provider sees, that word
    invalidated the cache for the whole request: a vote swarm shared 4.6% of its prompt and every
    voter paid full price for the same SOP.
    """
    builder = PromptBuilder()
    sana = builder.node_prompt(reviewer, _review_task(), agent_name="Sana",
                               agent_skill="code-reviewer")
    rita = builder.node_prompt(reviewer, _review_task(), agent_name="Rita",
                               agent_skill="code-reviewer")
    assert "code-reviewer" in sana.system
    assert sana.system == rita.system, "the system prompt must not carry the agent's name"
    # The identity is still stated — just later, where it costs no cache.
    assert "Sana" in sana.text and "Rita" in rita.text


def test_system_prompt_forbids_fabrication(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert "never fabricate" in prompt.system.lower()


def test_system_prompt_requires_the_trailer(reviewer):
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    assert "trailer" in prompt.system.lower()


# ── trailer extraction ───────────────────────────────────────────────────────


def test_extract_trailer_from_the_tagged_fence():
    text = 'Work.\n\n```agentorg\n{"status": "changes_requested", "findings": [{"id": "F1"}]}\n```'
    payload = extract_trailer(text)
    assert payload["status"] == "changes_requested"
    assert payload["findings"][0]["id"] == "F1"


def test_extract_trailer_from_a_bare_json_fence():
    """A model that forgets the tag has still done the work."""
    payload = extract_trailer('prose\n```json\n{"status": "done"}\n```')
    assert payload["status"] == "done"


def test_extract_trailer_from_an_unfenced_trailing_object():
    payload = extract_trailer('prose only, no fence\n{"status": "needs_review"}')
    assert payload["status"] == "needs_review"


def test_extract_trailer_repairs_a_trailing_comma():
    """A common model slip; refusing the whole result would waste the tokens it cost."""
    payload = extract_trailer('x\n```agentorg\n{"status": "done", "findings": [],}\n```')
    assert payload["status"] == "done"


def test_extract_trailer_prefers_the_tagged_fence_over_prose_braces():
    text = ('Some {incidental} braces in prose.\n'
            '```agentorg\n{"status": "blocked", "summary": "real"}\n```')
    assert extract_trailer(text)["status"] == "blocked"


def test_extract_trailer_raises_when_required_and_absent():
    with pytest.raises(TrailerError, match="no parsable JSON trailer"):
        extract_trailer("just prose, no json")


def test_extract_trailer_rejects_a_json_object_that_is_not_a_trailer():
    """An arbitrary JSON blob is not the result trailer, and accepting one hid a real failure.

    A node answered with a JSON object shaped like its own *intake* block — `task`, `received`, `owed`,
    `assumptions`, `open_questions` — and nothing else. The old extractor returned it because it was a
    dict, so the engine recorded a parsed trailer with zero criteria coverage and failed the node's
    contract, while the log claimed the trailer had parsed. The reply must be refused as *not a
    trailer*, which is the honest diagnosis and the one a repair can act on.
    """
    intake_shaped = '```json\n{"task": "x", "received": {}, "owed": {}, "assumptions": {}}\n```'
    with pytest.raises(TrailerError, match="no parsable JSON trailer"):
        extract_trailer(intake_shaped)
    assert extract_trailer(intake_shaped, require=False) is None
    # A genuine trailer — even a minimal one — is still accepted.
    assert extract_trailer('```json\n{"status": "done"}\n```')["status"] == "done"
    assert extract_trailer('```agentorg\n{"checklist": []}\n```')["checklist"] == []


def test_extract_trailer_returns_none_when_lenient():
    assert extract_trailer("just prose", require=False) is None


def test_extract_trailer_handles_an_empty_reply():
    with pytest.raises(TrailerError, match="empty"):
        extract_trailer("")
    assert extract_trailer("", require=False) is None


def test_extract_trailer_ignores_a_non_object_fence():
    assert extract_trailer("[1, 2, 3]", require=False) is None


def test_extract_trailer_reads_a_full_realistic_trailer():
    payload = {
        "status": "changes_requested",
        "verdict": "changes_requested",
        "summary": "Auth middleware is bypassable.",
        "criteria_satisfied": [
            {"criterion": "Findings reference concrete files and lines", "satisfied": True,
             "evidence": "src/app.py:47"}],
        "checklist": [{"id": "CR1", "status": "FAIL", "evidence": "1 Critical unresolved"}],
        "findings": [{"id": "F1", "severity": "Critical", "dimension": "security",
                      "owasp": "A03:2021 — Injection", "file": "src/app.py", "line": 47,
                      "issue": "userId interpolated into SQL", "fix": "bind as a parameter"}],
        "artifacts": [{"type": "change", "path": "src/app.py", "sha256": "b" * 64}],
        "decisions": [{"gate": "auth", "choice": "argon2id", "rationale": "memory hardness",
                       "reversible": False}],
        "open_questions": [{"question": "bcrypt for legacy?"}],
        "context": {"files_read": ["src/app.py:1-120"], "tried_and_failed": [],
                    "assumptions": ["postgres 15"]},
        "budget": {"tokens_used": 1200, "steps_used": 2},
    }
    text = f"Review below.\n\n```{TRAILER_FENCE}\n{json.dumps(payload)}\n```"
    parsed = extract_trailer(text)
    assert parsed == payload
    assert parsed["checklist"][0]["status"] == "FAIL"
    assert parsed["findings"][0]["severity"] == "Critical"


def test_prompt_and_parser_agree_on_the_schema_keys(reviewer):
    """The prompt text and the parser must not drift: a mismatch fails as 'no verdict'."""
    prompt = PromptBuilder().node_prompt(reviewer, _review_task())
    fenced = prompt.recency.split(f"```{TRAILER_FENCE}")[1].split("```")[0]
    schema = json.loads(fenced)
    for key in ("status", "summary", "criteria_satisfied", "checklist", "findings",
                "artifacts", "decisions", "open_questions"):
        assert key in schema, f"the prompt's schema omits {key}, which the parser expects"


def test_extract_trailer_handles_a_nested_fence_inside_the_json():
    """A trailer whose `artifacts[].content` embeds ``` fences must still parse.

    This is the defect behind a real, reproduced failure. A model returned a complete, valid 53KB
    trailer whose markdown artifact contained its own Gherkin fence. The extractor used a non-greedy
    `(.*?)` up to the next ```, so it stopped *inside* the JSON, the parse failed, and the engine
    reported "no parsable JSON trailer" — failing the node's contract on work that was actually
    correct. The fence body is now delimited by balanced braces.
    """
    body = {
        "status": "done",
        "criteria_satisfied": [{"criterion": "c1", "satisfied": True, "evidence": "x"}],
        "artifacts": [{
            "type": "product-spec",
            "path": "specs/prd.md",
            "content": "## Stories\n\n```gherkin\nScenario: a user logs in\n  Given a user\n```\n\nDone.",
        }],
    }
    text = "Prose first.\n\n```agentorg\n" + json.dumps(body) + "\n```\n"
    payload = extract_trailer(text)
    assert payload["status"] == "done"
    assert payload["artifacts"][0]["path"] == "specs/prd.md"
    assert "gherkin" in payload["artifacts"][0]["content"]


def test_extract_trailer_handles_braces_inside_a_json_string():
    """Brace counting must respect string literals, or a `}` in content closes the object early."""
    body = {"status": "done", "summary": "a dict literal like {\"k\": 1} and a closing } brace"}
    text = "```agentorg\n" + json.dumps(body) + "\n```\n"
    assert extract_trailer(text)["summary"].endswith("brace")
