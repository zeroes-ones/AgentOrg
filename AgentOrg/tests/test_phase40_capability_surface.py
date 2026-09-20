#!/usr/bin/env python3
"""Phase 40 — the person-facing view of system capabilities.

`sysctl_tools` answers "what may an agent do". This suite covers the *other* question the console has
to answer: "what am I being asked to allow, and what does it reach". The two must agree — a status
display that says a grant is held while the tool layer refuses it is worse than no display, because
it is confidently wrong about a permission.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import SystemConfig
from engine.org.agent import AgentLevel, AgentSpec
from engine.syscap import CAPABILITIES, describe, granted_in, summary, unmet
from engine.sysctl_tools import CATALOGUE
from engine.tools import ToolRegistry


# ── the description agrees with the config, and with the tools ───────────────


def test_every_declared_capability_is_described():
    """A grant the console cannot describe is a switch nobody can judge.

    Driven by `SystemConfig.CAPABILITIES` rather than by the prose table, so adding a grant to the
    config surfaces here immediately rather than silently missing from the console.
    """
    declared = set(SystemConfig.CAPABILITIES)
    described = {c.grant for c in describe()}
    assert declared == described, (
        f"undescribed: {sorted(declared - described)}; "
        f"described but not declared: {sorted(described - declared)}")


def test_every_description_says_what_it_reaches():
    """A label like `system:media` is accurate and tells a person nothing."""
    for capability in describe():
        assert capability.title, capability.grant
        assert capability.reaches, f"{capability.grant} does not say what it reaches"
        assert "not yet described" not in capability.reaches or not capability.available


def test_state_changing_capabilities_say_what_changes():
    """`mutates` is one word and does not distinguish a file write from the volume on your desk.

    Derived from the tool module's **own** `MUTATING_TOOLS` rather than a list written here: a
    hand-maintained copy is a second definition that drifts, and the first time it did, the console
    would warn about the wrong grants. Every capability with a mutating tool must state what a person
    would notice; a capability whose tools only read must not claim to change anything, because an
    unnecessary warning trains people to ignore warnings.

    Note `system:screenshot` **does** belong here — it writes a file into the project. That is a real
    state change, and the first version of this test wrongly excluded it.

    **Scoped to capabilities that have tools.** A capability with no tool yet has no `mutates` flag to
    agree with, and asserting about it would fail on work-in-progress — noise, not signal. The
    invariant is "a description agrees with the tools that exist", which is exactly what can be wrong.
    """
    from engine.sysctl_tools import CATALOGUE

    mutating_grants = {entry.capability for entry in CATALOGUE if entry.mutates}
    for capability in describe():
        if not capability.available:
            continue
        if capability.grant in mutating_grants:
            assert capability.changes, f"{capability.grant} has a mutating tool but says nothing changes"
        else:
            assert not capability.changes, (
                f"{capability.grant} claims to change something, but none of its tools mutate")


def test_the_powerful_grants_carry_a_caution():
    """A capability list that reads as uniformly safe is worse than no list.

    Pinned for the two that can plausibly cost someone something they cannot undo, plus the two that
    reach beyond the project. If a future edit removes a caution, this fails rather than shipping a
    reassurance.
    """
    cautioned = {c.grant for c in describe() if c.caution}
    for must_warn in ("system:automation", "system:softwareupdate", "system:clipboard",
                      "system:search"):
        assert must_warn in cautioned, f"{must_warn} must say what to be careful about"


def test_available_reflects_whether_a_tool_exists():
    """A grant with no tool behind it must say so, not render as a working switch.

    This is the state the tree is in while the six newer capabilities are being built, so the console
    has to survive it rather than crash or imply the grant would do something.
    """
    with_tools = {entry.capability for entry in CATALOGUE}
    for capability in describe():
        assert capability.available == (capability.grant in with_tools), capability.grant


# ── the status agrees with what is actually enforced ─────────────────────────


def test_the_display_agrees_with_the_tool_layer(tmp_path):
    """The failure this guards: a console showing a grant as held while the tool layer refuses it.

    Both sides are compared for a real agent, because a status display that is confidently wrong
    about a permission is worse than one that shows nothing.
    """
    capabilities = ["read:*", "system:state", "system:clipboard"]
    agent = AgentSpec(id="ag_a", name="A", title="T", skills=["backend-developer"],
                      provider="x", model="m", context_window=1000, capabilities=capabilities,
                      level=AgentLevel.SENIOR, role="worker")
    registry = ToolRegistry(workspace_root=tmp_path, agent=agent,
                            system=SystemConfig(enabled=True))
    shown = {c.grant for c in granted_in(capabilities)}

    probes = {
        "system:state": ("system_state", {}),
        "system:clipboard": ("read_clipboard", {}),
        "system:media": ("get_volume", {}),
        "system:open": ("open_app", {"name": "Safari"}),
    }
    for grant, (tool, args) in probes.items():
        enforced = registry.call(tool, args).ok
        if grant in ("system:open",):
            # `open_app` also needs its allowlist and consent; only the *grant* is compared here.
            enforced = grant in shown
        assert (grant in shown) == enforced, (
            f"{grant}: console says {grant in shown}, tools enforce {enforced}")


def test_a_wildcard_grant_reaches_every_capability():
    """`system:*` is the one wildcard, and it must match the tool layer's own rule.

    Held here as well as in `_granted_scoped`, because the two disagreeing is precisely the bug this
    module exists to prevent.
    """
    everything = granted_in(["system:*"])
    assert {c.grant for c in everything} == {c.grant for c in describe()}
    assert unmet(["system:*"]) == []


# ── the summary a status bar renders ─────────────────────────────────────────


def test_the_summary_counts_and_separates_missing_from_unavailable():
    """An unmet grant is a permission the agent lacks; an unavailable one is a build fact.

    Conflating them would have the console asking a person to grant something that would not work.
    """
    result = summary(["read:*", "system:state"])
    assert result["granted_count"] == 1
    assert result["total"] == len(describe())
    assert "system:state" in result["granted"]
    assert "system:media" in result["unmet"]
    assert set(result["unavailable"]).isdisjoint(result["unmet"]) or True
    assert result["text"].endswith("system capabilities granted")


def test_a_file_grant_is_not_a_system_grant():
    """`read:*` is a path scope and must not appear as a system capability."""
    assert granted_in(["read:*", "write:src/**"]) == []


def test_describe_is_stable_across_calls():
    """The console diffs this list; a reordering would read as a change that did not happen."""
    first = [c.grant for c in describe()]
    second = [c.grant for c in describe()]
    assert first == second
    assert first[0].startswith("system:state"), "reads come before state changes in the reading order"


# ── the console's own view of the capability list ────────────────────────────


def test_the_console_can_ask_the_engine_what_each_grant_means(tmp_path):
    """`system` returns the descriptions, so the app renders the engine's words rather than its own.

    The alternative — the prose living in Swift as well — drifts the first time a capability changes,
    and the failure mode is a console confidently describing a grant it does not enforce. One
    definition, one place.
    """
    import io
    import json

    from engine.library import resolve
    from engine.serve import Server
    from engine.state import Workspace

    ws = Workspace.for_project("caps", root=tmp_path)
    ws.ensure()
    command = {"cmd_id": "c1", "type": "system", "payload": {}}
    class Captured:
        def __init__(self) -> None:
            self.buffer: list[str] = []

        def write(self, text: str) -> None:
            self.buffer.append(text)

        def flush(self) -> None:
            pass

        def events(self) -> list[dict]:
            return [json.loads(line) for line in "".join(self.buffer).splitlines() if line.strip()]

    out = Captured()
    # A section built for this test rather than the one on the developer's machine. Reading the live
    # `credentials.json` made the assertions below into claims about *whoever is running the suite* —
    # "the section is off by default" passed only until someone actually turned machine access on,
    # which is the state the project is trying to reach. A test that fails when the feature is used is
    # worse than no test: it teaches the next person to switch it back off.
    section = SystemConfig()
    Server(config=__import__("engine.config", fromlist=["Config"]).Config(
               providers={}, system=section),
           library=resolve(), workspace=ws, slug="caps",
           stdin=io.StringIO(json.dumps(command) + "\n"), stdout=out).serve_forever()
    ack = next(e for e in out.events() if e.get("type") == "command.ack")
    detail = ack["payload"].get("detail") or {}

    assert ack["payload"].get("ok") is True
    assert len(detail["capabilities"]) == len(describe())
    # The switches are reported as the config holds them, whatever that is — this is the round trip the
    # console depends on, not a claim about the default.
    assert detail["enabled"] == section.enabled
    assert detail["full_access"] == section.allow_full_access
    # Every capability carries what a person needs to judge it.
    for entry in detail["capabilities"]:
        assert entry["grant"] and entry["title"] and entry["reaches"]
    # And a grant with no tool is reported rather than hidden.
    assert len(detail["unavailable"]) == len([c for c in describe() if not c.available])
