#!/usr/bin/env python3
"""Phase 15 tests — decomposition: a goal becomes a swarm, decided by a lead agent.

This is the bridge the feature set was missing. The fan-out executor, the capability-gated tools and
the cache-first prefix discipline were all built and tested; what did not exist was the step where a
*goal* becomes the item list. Without it, "improve this project" could not produce a swarm, because
the items had to be typed by hand — and deciding the work is most of the job.

The tests guard the three things that make a lead agent useful rather than dangerous:

1. **It looks before it splits.** A decomposition made without reading the project is guesswork.
2. **It can decline.** A swarm on one coherent change multiplies cost and fragments accountability, so
   refusing is a real outcome rather than a failure.
3. **Its output is validated, not trusted.** A model that emits one item, or two that expand alike, is
   refused before a single worker starts.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.decompose import DECOMPOSE_PROMPT, DecomposeError, Decomposer
from engine.org.agent import AgentSpec
from engine.providers.base import ChatResponse, FinishReason, ToolCall, Usage
from engine.tools import ToolRegistry


def make_lead_tools(root: pathlib.Path) -> ToolRegistry:
    agent = AgentSpec(id="ag_lead", name="Arjun", title="Lead", skills=["code-reviewer"],
                      provider="fake", model="m", context_window=32768,
                      capabilities=["read:*"])
    return ToolRegistry(workspace_root=root, agent=agent)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    for name in ("auth.py", "api.py", "db.py"):
        (root / "src" / name).write_text(f"# {name}\n")
    return root


def scripted(payload: dict | str, *, explore: list[list[ToolCall]] | None = None):
    """A model that explores for N calls, then returns the decomposition payload."""
    calls = {"n": 0}

    def complete(request):
        calls["n"] += 1
        if explore and calls["n"] <= len(explore):
            return ChatResponse(text="", tool_calls=explore[calls["n"] - 1],
                                usage=Usage(prompt_tokens=5, completion_tokens=5,
                                            reported_cost_usd=0.0),
                                model="m", provider_id="fake", finish_reason=FinishReason.STOP)
        text = payload if isinstance(payload, str) else "```decompose\n" + json.dumps(payload) + "\n```"
        return ChatResponse(text=text, usage=Usage(prompt_tokens=5, completion_tokens=5,
                                                   reported_cost_usd=0.0),
                            model="m", provider_id="fake", finish_reason=FinishReason.STOP)

    return complete


# ── it explores before it splits ─────────────────────────────────────────────


def test_the_lead_explores_the_project_before_deciding(project):
    """A decomposition made without looking is guesswork."""
    tools = make_lead_tools(project)
    explore = [[ToolCall(id="1", name="list_dir", arguments={"path": "src"})],
               [ToolCall(id="2", name="read_file", arguments={"path": "src/auth.py"})]]
    decision = Decomposer(complete=scripted({
        "swarm": True, "reason": "three independent modules", "skill": "code-reviewer",
        "prompt_template": "Review {{item}}.", "items": ["src/auth.py", "src/api.py", "src/db.py"],
    }, explore=explore), tools=tools).decompose("harden the auth flow")

    assert decision.swarm is True
    assert decision.steps == 3, "two exploration turns plus the decision"
    assert "src" in decision.explored and "src/auth.py" in decision.explored


def test_the_lead_uses_the_same_capability_gate_as_a_worker(project):
    """A lead that could read files its workers cannot would plan work nobody may do."""
    agent = AgentSpec(id="ag", name="Lead", title="Lead", skills=["code-reviewer"],
                      provider="fake", model="m", context_window=32768, capabilities=[])
    tools = ToolRegistry(workspace_root=project, agent=agent)
    exploration = [[ToolCall(id="1", name="read_file", arguments={"path": "src/auth.py"})]]
    decision = Decomposer(complete=scripted({
        "swarm": True, "skill": "code-reviewer", "prompt_template": "R {{item}}",
        "items": ["a", "b"], "reason": "x",
    }, explore=exploration), tools=tools).decompose("goal")
    # The read was refused, so nothing was explored — the gate applied to the lead too.
    assert decision.explored == []


# ── it can decline ───────────────────────────────────────────────────────────


def test_declining_is_a_real_outcome_with_a_reason(project):
    """Splitting one coherent change multiplies cost and fragments accountability."""
    decision = Decomposer(complete=scripted({
        "swarm": False, "reason": "one coherent refactor; splitting would fragment ownership",
        "items": [],
    }), tools=make_lead_tools(project)).decompose("rename the User model field")

    assert decision.swarm is False
    assert "coherent" in decision.reason
    assert decision.plan is None
    assert decision.summary().startswith("direct (no swarm)")


def test_a_decline_without_a_reason_still_says_something(project):
    decision = Decomposer(complete=scripted({"swarm": False, "items": []}),
                          tools=make_lead_tools(project)).decompose("g")
    assert decision.reason, "a decline must explain itself, even if the model did not"


# ── its output is validated, not trusted ─────────────────────────────────────


def test_a_swarm_of_one_is_refused(project):
    with pytest.raises(DecomposeError, match="at least two"):
        Decomposer(complete=scripted({
            "swarm": True, "skill": "code-reviewer", "prompt_template": "R {{item}}",
            "items": ["only one"],
        }), tools=make_lead_tools(project)).decompose("g")


def test_a_template_without_the_placeholder_is_refused(project):
    with pytest.raises(DecomposeError, match=r"\{\{item\}\}"):
        Decomposer(complete=scripted({
            "swarm": True, "skill": "code-reviewer", "prompt_template": "Review the files",
            "items": ["a", "b"],
        }), tools=make_lead_tools(project)).decompose("g")


def test_items_that_expand_alike_are_refused(project):
    """The duplicate check runs in plan_fanout, so the decomposition cannot produce a bad plan."""
    with pytest.raises(DecomposeError, match="same prompt|runnable fan-out"):
        Decomposer(complete=scripted({
            "swarm": True, "skill": "code-reviewer", "prompt_template": "R {{item}}",
            "items": ["a", "a"],
        }), tools=make_lead_tools(project)).decompose("g")


def test_a_swarm_decision_must_name_a_skill(project):
    with pytest.raises(DecomposeError, match="skill"):
        Decomposer(complete=scripted({
            "swarm": True, "prompt_template": "R {{item}}", "items": ["a", "b"],
        }), tools=make_lead_tools(project)).decompose("g")


def test_the_item_ceiling_is_enforced_by_refusal_not_truncation(project):
    """Truncating would silently drop work the lead thought was needed."""
    with pytest.raises(DecomposeError, match="ceiling"):
        Decomposer(complete=scripted({
            "swarm": True, "skill": "code-reviewer", "prompt_template": "R {{item}}",
            "items": [f"item {i}" for i in range(5)],
        }), tools=make_lead_tools(project), max_items=3).decompose("g")


def test_an_unparsable_reply_is_refused_with_its_tail(project):
    """A model that answers in prose gives a diagnosable error, not a silent empty plan."""
    with pytest.raises(DecomposeError, match="parsable"):
        Decomposer(complete=scripted("I think you should maybe split it into parts"),
                   tools=make_lead_tools(project)).decompose("g")


def test_an_empty_goal_is_refused(project):
    with pytest.raises(DecomposeError, match="goal is required"):
        Decomposer(complete=scripted({}), tools=make_lead_tools(project)).decompose("   ")


# ── the decision is a real, runnable plan ────────────────────────────────────


def test_a_valid_decomposition_produces_a_runnable_fanout(project):
    decision = Decomposer(complete=scripted({
        "swarm": True, "reason": "three files, one per item", "skill": "code-reviewer",
        "prompt_template": "Review {{item}} for regressions.",
        "items": ["src/auth.py", "src/api.py", "src/db.py"],
    }), tools=make_lead_tools(project)).decompose("review the project")

    assert decision.plan is not None
    assert len(decision.plan) == 3
    prompts = [i.prompt for i in decision.plan.items]
    assert prompts[0] == "Review src/auth.py for regressions."
    assert len(set(prompts)) == 3, "each item must get its own prompt"


def test_the_decomposition_prompt_requires_the_placeholder_and_a_refusal(project):
    """The instruction has to ask for both, or a model splits a one-line change into five items."""
    assert "{{item}}" in DECOMPOSE_PROMPT
    assert '"swarm": false' in DECOMPOSE_PROMPT.lower() or 'swarm": false' in DECOMPOSE_PROMPT
    assert "overlap" in DECOMPOSE_PROMPT.lower() or "non-overlapping" in DECOMPOSE_PROMPT.lower()


def test_the_decision_is_serialisable_for_the_trace(project):
    decision = Decomposer(complete=scripted({
        "swarm": True, "reason": "r", "skill": "code-reviewer",
        "prompt_template": "R {{item}}", "items": ["a", "b"],
    }), tools=make_lead_tools(project)).decompose("g")
    payload = decision.as_dict()
    assert payload["swarm"] is True
    assert payload["planned"] == 2
    assert json.dumps(payload)  # must not raise


# ── a decision made without looking is flagged ───────────────────────────────


def test_a_decline_without_exploring_says_so(project):
    """Measured on qwen2.5-coder:14b: it declined a security goal at step 1 having read nothing.

    Declining can be right — a rename really is one coherent change — but a decline reached from the
    goal text alone carries less weight, and presenting it as considered would overstate it.
    """
    decision = Decomposer(complete=scripted({
        "swarm": False, "reason": "this looks like one coherent change", "items": [],
    }), tools=make_lead_tools(project)).decompose("rename the User model field")

    assert decision.swarm is False
    assert decision.explored == []
    assert "without reading the project" in decision.reason


def test_a_decline_after_exploring_is_not_flagged(project):
    """The flag is about how the decision was reached, not about declining."""
    explore = [[ToolCall(id="1", name="list_dir", arguments={"path": "src"})]]
    decision = Decomposer(complete=scripted({
        "swarm": False, "reason": "the files share one auth design; splitting would fragment it",
        "items": [],
    }, explore=explore), tools=make_lead_tools(project)).decompose("harden auth")

    assert decision.swarm is False
    assert decision.explored, "it did look"
    assert "without reading the project" not in decision.reason


# ── the two real-model findings ──────────────────────────────────────────────


def test_a_single_brace_placeholder_is_repaired(project):
    """Measured on qwen2.5-coder:14b: it wrote `Review the file {item} for bugs.`

    Unambiguously the placeholder, and refusing it costs a whole round trip to fix a keystroke. The
    repair is narrow, so a genuine `{{other}}` is left alone.
    """
    decision = Decomposer(complete=scripted({
        "swarm": True, "reason": "r", "skill": "code-reviewer",
        "prompt_template": "Review the file {item} for bugs.",
        "items": ["src/auth.py", "src/api.py"],
    }), tools=make_lead_tools(project)).decompose("g")
    assert "{{item}}" in decision.prompt_template
    assert decision.plan is not None


def test_invented_files_are_refused(project):
    """The serious finding: the model produced a *valid* plan naming files that do not exist.

    Asked to review every file in `src/`, it emitted `file1.js … file4.js` for a project containing
    `auth.py`, `api.py`, `db.py`. Distinct prompts, a real template — and entirely fiction, so every
    worker would have been sent to a file that is not there.
    """
    with pytest.raises(DecomposeError, match="do not exist"):
        Decomposer(complete=scripted({
            "swarm": True, "reason": "r", "skill": "code-reviewer",
            "prompt_template": "Review {{item}} for bugs.",
            "items": ["src/file1.js", "src/file2.js", "src/file3.js"],
        }), tools=make_lead_tools(project)).decompose("review every file in src")


def test_real_files_pass_grounding(project):
    decision = Decomposer(complete=scripted({
        "swarm": True, "reason": "r", "skill": "code-reviewer",
        "prompt_template": "Review {{item}}.", "items": ["src/auth.py", "src/api.py"],
    }), tools=make_lead_tools(project)).decompose("g")
    assert decision.plan is not None


def test_a_non_path_item_is_not_treated_as_a_missing_file(project):
    """'the login flow' is a legitimate item and cannot be checked against a filesystem.

    A grounding check that refused everything path-shaped *or vague* would reject good plans, so it
    only judges items that clearly name a file.
    """
    decision = Decomposer(complete=scripted({
        "swarm": True, "reason": "r", "skill": "code-reviewer",
        "prompt_template": "Handle {{item}}.",
        "items": ["the login flow", "every write endpoint", "src/auth.py"],
    }), tools=make_lead_tools(project)).decompose("g")
    assert decision.plan is not None


def test_one_invented_file_among_real_ones_is_still_refused(project):
    """A partly-real plan is the dangerous case: it looks grounded until a worker fails."""
    with pytest.raises(DecomposeError, match="src/invented.py"):
        Decomposer(complete=scripted({
            "swarm": True, "reason": "r", "skill": "code-reviewer",
            "prompt_template": "Review {{item}}.", "items": ["src/auth.py", "src/invented.py"],
        }), tools=make_lead_tools(project)).decompose("g")


def test_grounding_is_skipped_when_the_tools_have_no_root():
    """A registry without a project root cannot check existence, and must not refuse everything."""
    class NoRoot:
        def specs(self): return []
        def call(self, name, args): raise AssertionError("not called")

    decision = Decomposer(complete=scripted({
        "swarm": True, "reason": "r", "skill": "code-reviewer",
        "prompt_template": "Review {{item}}.", "items": ["src/imagination.py", "src/more.py"],
    }), tools=NoRoot()).decompose("g")
    assert decision.plan is not None
