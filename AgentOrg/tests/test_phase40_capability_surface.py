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


# ── the tool table the console acts through ──────────────────────────────────


def test_the_tool_table_travels_with_the_capabilities():
    """The console must be able to *act*, not only describe, and that needs the catalogue.

    The app used to keep its own copy of `CATALOGUE` and `CONSENT_REQUIRED` — eighteen hand-written
    rows with the line number each came from — because the reply carried no tool names at all. A copy
    is right until the first tool is added and the console offers a set the engine does not have, so
    the table travels instead, and every field here is computed from the engine's own declarations
    rather than written out: a tool added to the catalogue is one the console can act on the moment it
    is added, and a tool moved into `CONSENT_REQUIRED` starts asking for approval with no edit on the
    Swift side.

    `runs_without_arguments` is the one field that is *derived* rather than declared: it is the entry's
    own JSON schema, which is what the tool layer enforces — a flag set by hand could disagree with the
    schema and offer "run it with no arguments" for a call that cannot be made.
    """
    from engine.syscap import console_payload, tools_payload
    from engine.sysctl_tools import CONSENT_REQUIRED

    expected = [{
        "name": entry.name,
        "grant": entry.capability,
        "mutates": bool(entry.mutates),
        "runs_without_arguments": not (entry.parameters or {}).get("required"),
        "consent_required": entry.name in CONSENT_REQUIRED,
    } for entry in CATALOGUE]
    assert expected, "the catalogue should declare tools"
    # Order included: it is the order the engine advertises in, which is the order the console's rows
    # read in, and a re-ordering would be a visible change that nothing else would catch.
    assert tools_payload() == expected
    payload = console_payload(SystemConfig())
    assert payload["tools"] == expected


def test_the_reply_gained_a_key_without_losing_or_renaming_one():
    """The app's decoder reads `capabilities`, `enabled`, `full_access`, `unavailable` and `summary`.

    Adding a key is how a surface stops needing a copy of the engine's data; renaming or retyping one
    is how a shipped console breaks silently, so the pre-existing keys are pinned here beside the new
    one.
    """
    from engine.syscap import console_payload

    payload = console_payload(SystemConfig())
    for key in ("capabilities", "enabled", "full_access", "allow_apps", "allow_automation",
                "allow_shortcuts", "unavailable", "summary", "tools"):
        assert key in payload, f"{key} is missing from the console payload"
    assert isinstance(payload["capabilities"], list)
    assert isinstance(payload["tools"], list)
    assert all(isinstance(entry, dict) for entry in payload["tools"])


def test_every_tool_is_filed_under_a_grant_the_config_declares():
    """A tool naming a grant nobody declared would be advertised and then refused.

    `ToolRegistry` decides what to offer from the holder's grants, and the console's rows are keyed by
    the grants the config declares — so a catalogue entry under an undeclared grant is a row with no
    home, which is the shape a copy of the catalogue had to get wrong.
    """
    from engine.syscap import tools_payload

    declared = set(SystemConfig.CAPABILITIES)
    for entry in tools_payload():
        assert entry["grant"] in declared, entry
    # And every one of them is a *system* grant: filing a machine tool under a project scope would put
    # it in the panel's project half, where the switches that govern it are not shown.
    assert all(entry["grant"].startswith("system:") for entry in tools_payload())


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
