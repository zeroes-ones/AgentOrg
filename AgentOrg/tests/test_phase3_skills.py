#!/usr/bin/env python3
"""Phase 3 tests — frontmatter parsing, skill bundles, and the source.

The emphasis is on extraction that would fail *silently*: a contract that parses as empty,
a checklist that loses its ids, criteria that fall back to nothing. Those produce a node
that looks gated and is not.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine.library import resolve
from engine.skills import (
    FilesystemSkillSource,
    FrontmatterError,
    SkillError,
    Tier,
    parse_frontmatter,
    parse_skill,
)
from engine.skills.frontmatter import safe_load_subset, split_document


# ── frontmatter: the document split ──────────────────────────────────────────


def test_split_document_returns_frontmatter_and_body():
    front, body = split_document("---\nname: x\n---\nReal body\n")
    assert "name: x" in front
    assert body.strip() == "Real body"


def test_split_document_rejects_a_missing_delimiter():
    with pytest.raises(FrontmatterError, match="does not begin"):
        split_document("no frontmatter here")


def test_split_document_rejects_an_unterminated_block():
    """A truncated file must not read as 'no frontmatter'."""
    with pytest.raises(FrontmatterError, match="never closed"):
        split_document("---\nname: x\nno closing delimiter")


# ── frontmatter: scalars ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [("x", "x"), ("42", 42), ("-7", -7), ("1.5", 1.5), ("true", True), ("false", False),
     ("null", None), ("~", None), ('"quoted"', "quoted"), ("'single'", "single"),
     ("2026-07-23", "2026-07-23")],
)
def test_scalar_types(raw, expected):
    parsed = safe_load_subset(f"key: {raw}")
    assert parsed["key"] == expected


def test_flow_list_with_commas_inside_quotes():
    """A criterion containing a comma must not be split into two items."""
    parsed = safe_load_subset('items: ["reduces p95 from 340ms to 120ms, ±15ms", plain]')
    assert parsed["items"] == ["reduces p95 from 340ms to 120ms, ±15ms", "plain"]


def test_comment_is_stripped_but_not_inside_quotes():
    parsed = safe_load_subset('a: value  # trailing\nb: "keeps # inside"')
    assert parsed["a"] == "value"
    assert parsed["b"] == "keeps # inside"


def test_bare_value_containing_a_colon_is_preserved():
    """`url: http://x` must not be truncated at the scheme colon."""
    parsed = safe_load_subset("url: http://example.com/path")
    assert parsed["url"] == "http://example.com/path"


# ── frontmatter: structure ───────────────────────────────────────────────────


def test_nested_mapping_one_level():
    parsed = safe_load_subset(
        "workflow:\n"
        "  artifacts:\n"
        "    inputs: [a, b]\n"
        "    outputs: [c]\n"
        "  completion:\n"
        "    criteria:\n"
        "      - one\n"
        "      - two\n"
        "    evidence: required\n"
    )
    workflow = parsed["workflow"]
    assert workflow["artifacts"]["inputs"] == ["a", "b"]
    assert workflow["completion"]["criteria"] == ["one", "two"]
    assert workflow["completion"]["evidence"] == "required"


def test_sequence_at_the_parent_indent_is_supported():
    """YAML permits `tags:` followed by `- x` at the same indent.

    Requiring a deeper indent here silently drops most real frontmatter blocks.
    """
    parsed = safe_load_subset("tags:\n- a\n- b\nother: 1")
    assert parsed["tags"] == ["a", "b"]
    assert parsed["other"] == 1


def test_sequence_deeper_than_the_parent_is_supported():
    parsed = safe_load_subset("tags:\n  - a\n  - b\n")
    assert parsed["tags"] == ["a", "b"]


def test_mapping_inside_a_sequence_item():
    parsed = safe_load_subset(
        "rows:\n"
        "  - from: a\n"
        "    to: b\n"
        "  - from: c\n"
        "    to: d\n"
    )
    assert parsed["rows"] == [{"from": "a", "to": "b"}, {"from": "c", "to": "d"}]


def test_nested_sequence_under_a_sequence_item_key():
    """`- version: 1` followed by a deeper `changes:` list."""
    parsed = safe_load_subset(
        "changelog:\n"
        "  - version: 1.0.0\n"
        "    date: 2026-01-01\n"
        "    changes:\n"
        "      - first\n"
        "      - second\n"
    )
    entry = parsed["changelog"][0]
    assert entry["version"] == "1.0.0"
    assert entry["changes"] == ["first", "second"]


def test_folded_block_scalar():
    parsed = safe_load_subset("description: >\n  line one\n  line two\n\nmore: 1")
    assert parsed["description"] == "line one line two"
    assert parsed["more"] == 1


def test_literal_block_scalar_keeps_newlines():
    parsed = safe_load_subset("text: |\n  one\n  two\n")
    assert parsed["text"] == "one\ntwo"


def test_plain_multiline_scalar_is_folded():
    parsed = safe_load_subset("description: starts here\n  continues here\nnext: 1")
    assert parsed["description"].startswith("starts here continues here")
    assert parsed["next"] == 1


def test_multiline_quoted_scalar_with_a_detached_close():
    """The library's shape: quote opens on the key line, closes on its own line later."""
    parsed = safe_load_subset(
        "description: 'first part\n"
        "  second part\n"
        "\n"
        "  '\n"
        "license: MIT\n"
    )
    assert "first part" in parsed["description"]
    assert "second part" in parsed["description"]
    assert parsed["license"] == "MIT", "the close must not swallow following keys"


# ── frontmatter: rejections ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "document, match",
    [
        ("just text", "does not begin"),
        ("---\nname: x\nbroken", "never closed"),
        ("---\nname: x\n\tworkflow:\n---\nb", "tab"),
        ("---\nname: &a x\n---\nb", "anchor"),
        ("---\nname: *a\n---\nb", "anchor|alias|construct"),
        ("---\nname: !!str x\n---\nb", "tag"),
        ("---\n\n---\nb", "empty"),
        ("---\n- a\n---\nb", "must be a mapping"),
    ],
)
def test_frontmatter_rejections(document, match):
    with pytest.raises(FrontmatterError, match=match):
        parse_frontmatter(document)


def test_out_of_subset_constructs_are_rejected_whichever_parser_runs():
    """PyYAML resolves anchors silently; the subset check must run regardless.

    Otherwise the fast path would accept a document the fallback interprets differently,
    which defeats the point of having a fallback.
    """
    with pytest.raises(FrontmatterError):
        parse_frontmatter("---\nname: &a x\n---\nbody", prefer_pyyaml=True)
    with pytest.raises(FrontmatterError):
        parse_frontmatter("---\nname: &a x\n---\nbody", prefer_pyyaml=False)


# ── frontmatter: real library agreement ──────────────────────────────────────


@pytest.fixture(scope="module")
def library_root():
    return pathlib.Path(resolve().files.flat_skills)


def test_strict_parser_agrees_with_pyyaml_on_the_whole_library(library_root):
    """The stdlib fallback must be equivalent to PyYAML, not merely close.

    A near-match would mean the engine behaves differently depending on whether PyYAML is
    installed, which is exactly the kind of environment-dependent bug that is hardest to
    diagnose.
    """
    try:
        import yaml  # noqa: F401
    except ImportError:
        pytest.skip("PyYAML is not installed, so there is no second path to compare")

    checked = 0
    for entry in sorted(library_root.iterdir()):
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text()
        with_pyyaml = parse_frontmatter(text, prefer_pyyaml=True)[0]
        strict = safe_load_subset(split_document(text)[0])
        assert with_pyyaml == strict, f"{entry.name} parses differently between paths"
        checked += 1
    assert checked > 200, f"expected the whole corpus, only checked {checked}"


def test_strict_parser_handles_every_library_skill(library_root):
    checked = 0
    for entry in sorted(library_root.iterdir()):
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        parsed = safe_load_subset(split_document(skill_md.read_text())[0])
        assert parsed.get("name"), f"{entry.name} lost its name"
        checked += 1
    assert checked > 200


# ── bundle: contract ─────────────────────────────────────────────────────────


MINIMAL_SKILL = """---
name: demo
version: 1.0.0
description: A demo skill.
token_budget: 1000
tags: [demo, test]
workflow:
  artifacts:
    inputs: [brief]
    outputs: [spec]
  completion:
    criteria:
      - Every requirement has a section
      - Open questions are recorded
    evidence: required
  escalate_to: [human-gate]
---
# Demo

## Ground Rules
| # | Rule | Trigger |
|---|---|---|
| R1 | Must NOT invent facts | always |

## Production Checklist

- [ ] **[CR1]** First check
- [ ] **[CR2]** Second check

## Anti-Rationalization
| # | Hard Rule |
|---|-----------|
| AR1 | "It's small" is not evidence |

## Verification
- [ ] All criteria evidenced
"""


def test_bundle_extracts_the_contract():
    bundle = parse_skill("demo", MINIMAL_SKILL)
    assert bundle.name == "demo"
    assert bundle.version == "1.0.0"
    assert bundle.contract.inputs == ("brief",)
    assert bundle.contract.outputs == ("spec",)
    assert bundle.contract.criteria == ("Every requirement has a section",
                                       "Open questions are recorded")
    assert bundle.contract.evidence_required is True
    assert bundle.contract.escalate_to == ("human-gate",)
    assert not bundle.contract.criteria_from_fallback


def test_bundle_extracts_checklist_ids():
    bundle = parse_skill("demo", MINIMAL_SKILL)
    assert bundle.checklist_ids() == ["CR1", "CR2"]
    assert bundle.checklist[0].text == "First check"


def test_bundle_extracts_anti_rationalization():
    bundle = parse_skill("demo", MINIMAL_SKILL)
    assert any("AR1" in rule for rule in bundle.anti_rationalization)


def test_bundle_extracts_ground_rules_and_tiers():
    bundle = parse_skill("demo", MINIMAL_SKILL)
    titles = {section.title for section in bundle.sections}
    assert "Ground Rules" in titles
    assert "Production Checklist" in titles


def test_bundle_hash_is_content_addressed():
    a = parse_skill("demo", MINIMAL_SKILL)
    b = parse_skill("demo", MINIMAL_SKILL)
    c = parse_skill("demo", MINIMAL_SKILL.replace("First check", "Other check"))
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


def test_bundle_rejects_a_skill_with_no_name():
    with pytest.raises(SkillError, match="no 'name'"):
        parse_skill("x", "---\nversion: 1\n---\n# body\n")


def test_bundle_rejects_unparsable_frontmatter():
    with pytest.raises(SkillError, match="unparsable frontmatter"):
        parse_skill("x", "no frontmatter at all")


def test_bundle_rejects_a_skill_with_no_criteria_at_all():
    """A node that cannot be gated must be refused, not silently ungated."""
    with pytest.raises(SkillError, match="no completion criteria"):
        parse_skill("x", "---\nname: x\n---\n# X\n\n## Notes\nSome prose.\n")


# ── bundle: criteria fallbacks ───────────────────────────────────────────────

VERIFICATION_ONLY = """---
name: vonly
description: No workflow block.
---
# Vonly

## Verification

| # | Check | Pass condition |
|---|---|---|
| **V1** | Sources | All claims tagged |
| **V2** | Scope | Requirements covered |

## Production Checklist

- [ ] First item with no id
- [ ] Second item with no id
"""


def test_criteria_fall_back_to_the_verification_section():
    bundle = parse_skill("vonly", VERIFICATION_ONLY)
    assert bundle.contract.criteria_from_fallback
    assert any(c.startswith("V1") for c in bundle.contract.criteria)
    assert len(bundle.contract.criteria) == 2


def test_plain_bullet_checklist_items_get_stable_positional_ids():
    """An item the model cannot be held to is worse than no item — it looks like coverage."""
    bundle = parse_skill("vonly", VERIFICATION_ONLY)
    assert bundle.checklist_ids() == ["PC1", "PC2"]
    assert bundle.checklist[0].text == "First item with no id"


NUMBERED_CHECKLIST = """---
name: numsk
description: Numbered checklist.
workflow:
  completion:
    criteria: [done]
---
# Numsk

## Production Checklist

Before deployment, verify ALL of:

1. All tests pass
2. Linter reports zero issues
3. Type checker reports zero errors
"""


def test_numbered_checklist_items_are_extracted():
    """Several library skills use an ordered list for their Production Checklist."""
    bundle = parse_skill("numsk", NUMBERED_CHECKLIST)
    assert bundle.checklist_ids() == ["PC1", "PC2", "PC3"]
    assert bundle.checklist[0].text == "All tests pass"
    assert "Before deployment" not in " ".join(i.text for i in bundle.checklist)


COMPLETE_WHEN_ONLY = """---
name: conly
description: Only Complete when checkpoints.
---
# Conly

## Core Workflow

### Phase 1
Do the work.
  Complete when: The inventory is complete and signed off.
Complete when: The plan is reviewed by a peer.
"""


def test_criteria_fall_back_to_complete_when_checkpoints():
    """The third documented criteria source; 241 library skills use it."""
    bundle = parse_skill("conly", COMPLETE_WHEN_ONLY)
    assert bundle.contract.criteria_from_fallback
    assert any("inventory is complete" in c for c in bundle.contract.criteria)
    assert any("reviewed by a peer" in c for c in bundle.contract.criteria)


def test_verification_is_preferred_over_complete_when():
    text = VERIFICATION_ONLY.replace("## Production Checklist",
                                     "Complete when: fallback line.\n\n## Production Checklist")
    bundle = parse_skill("vonly", text)
    assert any(c.startswith("V1") for c in bundle.contract.criteria), (
        "the Verification section is the higher-authority source"
    )


def test_complete_when_checkpoints_are_deduplicated():
    text = "---\nname: d\n---\n# D\n\n## Workflow\nComplete when: same line\nComplete when: same line\n"
    bundle = parse_skill("d", text)
    assert len(bundle.contract.criteria) == 1


# ── bundle: tiered body assembly ─────────────────────────────────────────────


def test_system_body_is_cumulative_across_tiers():
    bundle = parse_skill("demo", MINIMAL_SKILL)
    route = bundle.system_body(tier=Tier.ROUTE)
    core = bundle.system_body(tier=Tier.CORE)
    detail = bundle.system_body(tier=Tier.DETAIL)
    assert len(core) >= len(route)
    assert len(detail) >= len(core)


def test_system_body_accepts_a_bare_int_tier():
    bundle = parse_skill("demo", MINIMAL_SKILL)
    assert bundle.system_body(tier=3) == bundle.system_body(tier=Tier.DETAIL)


def test_system_body_respects_a_token_cap():
    bundle = parse_skill("demo", MINIMAL_SKILL)
    capped = bundle.system_body(tier=Tier.DETAIL, max_tokens=40)
    assert len(capped) // 4 <= 60, "the cap must actually bound the assembled text"


def test_system_body_keeps_ground_rules_when_including_a_subset():
    """Dropping the ground rules to satisfy a narrow request loses safety constraints."""
    bundle = parse_skill("demo", MINIMAL_SKILL)
    body = bundle.system_body(tier=Tier.DETAIL, include=["Production Checklist"])
    assert "Ground Rules" in body
    assert "Production Checklist" in body


def test_system_body_falls_back_to_a_summary_when_no_sections_match():
    bundle = parse_skill("demo", MINIMAL_SKILL)
    body = bundle.system_body(tier=Tier.ROUTE, include=["Nonexistent Section"])
    assert body, "an empty prompt body would silently send no instructions"


# ── source: filesystem ───────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def source():
    return FilesystemSkillSource(resolve())


def test_source_enumerates_the_library(source):
    names = source.names()
    assert len(names) > 200
    assert "code-reviewer" in names
    assert names == sorted(names)


def test_source_knows_which_skills_exist(source):
    assert source.has("code-reviewer")
    assert not source.has("not-a-real-skill")


def test_source_loads_a_real_skill_contract(source):
    bundle = source.load("code-reviewer")
    assert bundle.name == "code-reviewer"
    assert bundle.contract.inputs == ("change",)
    assert bundle.contract.outputs == ("review-report",)
    assert bundle.contract.evidence_required
    assert bundle.contract.escalate_to == ("human-gate",)
    assert len(bundle.contract.criteria) == 3


def test_source_extracts_the_code_reviewer_checklist(source):
    """The brief's acceptance bar: CR1–CR14 must be extractable."""
    bundle = source.load("code-reviewer")
    assert bundle.checklist_ids() == [f"CR{i}" for i in range(1, 15)]


def test_source_extracts_a_numbered_checklist(source):
    """backend-developer numbers its checklist; missing the form drops it entirely."""
    bundle = source.load("backend-developer")
    assert len(bundle.checklist) >= 10
    assert bundle.checklist[0].id == "PC1"
    assert "tests pass" in bundle.checklist[0].text.lower()


def test_most_of_the_library_has_a_checklist(source):
    """A skill whose checklist silently fails to extract looks complete and is not."""
    with_checklist = 0
    total = 0
    for name in source.names():
        try:
            bundle = source.load(name)
        except SkillError:
            continue
        total += 1
        if bundle.checklist:
            with_checklist += 1
    assert total == len(source.names()), "every skill must bundle"
    assert with_checklist > total * 0.6, (
        f"only {with_checklist}/{total} skills yielded a checklist; an extraction form is likely missing"
    )


def test_source_extracts_research_steps(source):
    bundle = source.load("code-reviewer")
    assert len(bundle.research_steps) == 8
    assert bundle.research_steps[0].startswith("RP1")


def test_source_extracts_anti_rationalization(source):
    bundle = source.load("code-reviewer")
    assert len(bundle.anti_rationalization) >= 4


def test_source_loads_every_library_skill(source):
    """Every skill must yield a bundle; a silent failure here would be a skill that cannot run."""
    failures: list[str] = []
    for name in source.names():
        try:
            bundle = source.load(name)
            assert bundle.contract.criteria, f"{name} produced no criteria"
        except SkillError as exc:
            failures.append(f"{name}: {exc}")
    assert not failures, "skills failed to bundle:\n" + "\n".join(failures[:10])


def test_every_bundle_carries_a_content_hash(source):
    for name in ("code-reviewer", "product-manager", "backend-developer", "qa-engineer"):
        bundle = source.load(name)
        assert len(bundle.content_hash) == 64


def test_source_reports_a_missing_skill_rather_than_returning_None(source):
    """The delegation design treats a missing skill as a real rung, so it must be detectable."""
    with pytest.raises(SkillError, match="not found"):
        source.load("definitely-not-a-real-skill")


def test_source_caches_by_content_hash(source):
    first = source.load("code-reviewer")
    second = source.load("code-reviewer")
    assert first is second, "an unchanged skill must be served from cache"


def test_source_fingerprint_is_stable(source):
    first = source.fingerprint()
    assert source.fingerprint() == first
    assert len(first) == 64


def test_source_invalidate_clears_the_cache(source):
    source.load("code-reviewer")
    source.invalidate()
    assert source.cache_info()["cached"] == 0
    assert source.load("code-reviewer").name == "code-reviewer"


def test_source_contract_io_types_form_a_chain(source):
    """The handoff graph depends on one skill's outputs being another's inputs."""
    developer = source.load("backend-developer")
    reviewer = source.load("code-reviewer")
    qa = source.load("qa-engineer")
    assert "change" in developer.contract.outputs
    assert "change" in reviewer.contract.inputs
    assert "change" in qa.contract.inputs


def test_bundle_as_dict_is_compact(source):
    payload = source.load("code-reviewer").as_dict()
    assert payload["checklist_ids"][0] == "CR1"
    assert "body" not in payload, "the full body must not be serialised into events"
    assert payload["content_hash"]
