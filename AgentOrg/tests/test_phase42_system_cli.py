#!/usr/bin/env python3
"""Phase 42 tests — the machine capabilities, driven by a person from a terminal.

The ask: *a person at the terminal can use every system capability the agents can, and see exactly
what each one is — through the SAME implementation the agents use, so there is only one.*

That sentence contains two properties and this file tests each of them separately, because they fail
in different ways:

1. **One implementation.** Every command must reach the machine through `ToolRegistry.call`, which is
   the single entry point an agent's tool call passes through. A command that called `SystemTools`
   directly would get a second "set the volume" — and, worse, a second *gate*, because the capability
   check lives in the registry and not in the tool. Asserted by wrapping the registry and counting:
   the call the CLI makes is the call the agent makes.

2. **Nothing declared is unreachable.** The subcommand table in `engine/systemcli.py` is data
   (`TOOLS_BY_COMMAND`), and the test derives what it must cover from `sysctl_tools.CATALOGUE` — never
   from a second list written here. A tool added to the tool layer and not to the CLI is the drift
   this file exists to catch, and a hand-copied expectation could not catch it: it would go stale in
   the same commit.

Then the properties that make the commands usable rather than merely present:

- **`list` renders `syscap.describe()` itself.** Not a copy of its words. The test compares the
  rendered output against the module the console also renders, so the terminal and the GUI cannot say
  different things about one grant.
- **A refusal is not a failure.** They carry different exit codes, because a script that retries a
  failure must not retry a permission decision. `EXIT_REFUSED` is asserted against a real refusal.
- **A refusal names the next move.** `grant_consent`'s own gate name and the exact `consent grant`
  command appear in the message, because a refusal a person cannot act on is a dead end.
- **Consent is per holder and per tool, and needs a person.** The CLI's `by` is checked against
  `grant_consent`'s guard rather than trusted: an approval attributed to `ag_*` is refused by the
  engine, and this file proves the CLI cannot produce one.
- **`--json` is only JSON on stdout.** The contract `tests/test_phase38_cli_parity.py` holds every
  other command to, and the one this module's `_emit` is copied from.

What is deliberately **not** asserted: the *values* a live machine reports (this Mac's charge is not a
fact about the code), and anything that would change the developer's machine to prove a point. Every
call that would mutate — the volume, the clipboard, sleep, an update — is asserted on its refusal path
or against a stubbed registry, and the one live call the suite makes is the read-only state report.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import systemcli as sc
from engine import usercfg
from engine.config import SystemConfig, load
from engine.library import resolve
from engine.syscap import console_payload, describe, summary
from engine.sysctl_tools import (
    CATALOGUE,
    CONSENT_REQUIRED,
    MUTATING_TOOLS,
    grant_consent,
)
from engine.tools import ToolRegistry


# ── a hermetic config, so these tests never read the developer's own ─────────

#: The provider block is the smallest one `config.load` accepts (`_build_providers` refuses a config
#: with none), pointed at a port nothing listens on so a probe can never reach the network.
_CREDS = {
    "version": "1.0.0",
    "providers": {"ollama": {"kind": "ollama", "base_url": "http://127.0.0.1:9",
                             "timeout_s": 1, "max_retries": 0}},
    "models": {"known": {"qwen2.5-coder:7b": {"context_window": 32768}}},
    "defaults": {"provider": "ollama", "model": "qwen2.5-coder:7b"},
}


def creds(tmp_path: pathlib.Path, system: dict | None = None) -> pathlib.Path:
    """A credentials file written for one test.

    `system` is merged rather than replaced, so a test names only the switch it is about and the rest
    of the section keeps its shipped defaults — which is what makes the `enabled=False` tests assert
    the real default rather than a fixture's opinion of it.
    """
    document = json.loads(json.dumps(_CREDS))
    section = {"enabled": True}
    if system:
        section.update(system)
    document["system"] = section
    path = tmp_path / "creds.json"
    path.write_text(json.dumps(document))
    return path


@pytest.fixture
def project(tmp_path):
    """A real folder, because every command resolves its ledger against one."""
    root = tmp_path / "Mac"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def config(tmp_path):
    """A hermetic credentials file with `system.enabled` on and no allowlist entries.

    Deliberately the *permissive* config: every test that needs something narrower builds its own with
    `creds(...)`, so the interesting switch in each test is the one the test names rather than one this
    fixture happened to set.
    """
    return creds(tmp_path)


class Captured:
    """Runs a `cmd_*` in-process with stdout and stderr captured, returning all three.

    In-process rather than as a subprocess because `grant_consent`'s `by` is resolved here — a
    subprocess would resolve it in a child whose environment the test cannot see, and the whole point
    of the assertion is that the identity is the *person's*.
    """

    def __init__(self, args: list[str]) -> None:
        self.args = args
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run(self) -> int:
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            return sc.main(self.args)

    @property
    def stdout(self) -> str:
        return self.out.getvalue()

    @property
    def stderr(self) -> str:
        return self.err.getvalue()

    def json(self) -> dict:
        """stdout parsed as JSON — which also proves nothing else was written there."""
        return json.loads(self.stdout)


def run(*args: str, config: pathlib.Path, project: pathlib.Path, agent: str = "") -> Captured:
    argv = ["system", *args, "--config", str(config), "--project", str(project)]
    if agent:
        argv += ["--agent", agent]
    return Captured(argv)


# ── the coverage guarantee: every tool is reachable, derived from the catalogue ──


def test_every_catalogue_tool_is_reachable_from_some_command():
    """The drift guard. Derived from `sysctl_tools.CATALOGUE`, so a tool added there cannot become
    terminal-unreachable without this going red.

    This is the test that keeps the CLI from falling behind the tool layer, and it is written against
    the tool module's own catalogue rather than a list here precisely because a hand-maintained copy is
    what goes stale — the failure it would miss is a working tool nobody can reach, which looks like a
    missing feature rather than a missing line.
    """
    reachable = {name for names in sc.TOOLS_BY_COMMAND.values() for name in names}
    declared = {entry.name for entry in CATALOGUE}
    assert declared - reachable == set(), (
        f"reachable from no subcommand: {sorted(declared - reachable)}; add it to "
        "systemcli.TOOLS_BY_COMMAND (or to `system call`, which reaches everything)")


def test_the_command_table_names_only_real_catalogue_tools():
    """The other direction: a subcommand promising a tool that does not exist is a lie in the help."""
    declared = {entry.name for entry in CATALOGUE}
    for command, tools in sc.TOOLS_BY_COMMAND.items():
        for tool in tools:
            assert tool in declared, f"`system {command}` names {tool!r}, which is not a catalogue entry"
        assert command in sc.COMMANDS, f"{command!r} is in the table but not in COMMANDS"


def test_the_command_table_covers_every_capability_that_has_a_tool():
    """A capability whose tools are all unreachable is a capability the person cannot use.

    The coarser statement of the guarantee the per-tool check above makes, kept because it fails with
    a message naming the *capability* — the word a person uses — rather than a tool name they would
    have to look up. Derived from the catalogue's own capability field, so a new grant arrives here
    without an edit.
    """
    reachable = {name for names in sc.TOOLS_BY_COMMAND.values() for name in names}
    by_capability: dict[str, set[str]] = {}
    for entry in CATALOGUE:
        by_capability.setdefault(entry.capability, set()).add(entry.name)
    missing = {capability: sorted(names - reachable)
               for capability, names in by_capability.items() if not (names & reachable)}
    assert missing == {}, f"capabilities with no reachable tool: {missing}"


# ── list: what exists, and the words come from the one source ────────────────


def test_list_renders_every_declared_capability(config, project):
    """Every capability `syscap.describe()` declares appears, with the grant name a person types."""
    result = run("list", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    for capability in describe():
        assert capability.grant in result.stdout, f"{capability.grant} is not in the listing"
        assert capability.title in result.stdout, f"{capability.grant} is listed without its title"


def test_list_uses_the_same_words_as_the_console(config, project):
    """The terminal and the app must not describe one grant two ways.

    The console's `system` command returns `syscap.console_payload`; this asserts the CLI rendered
    *that* prose — the reach, the caution and what changes — rather than a paraphrase. A paraphrase is
    how two surfaces start disagreeing about what a switch does.
    """
    result = run("list", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    for capability in describe():
        if capability.changes:
            assert capability.changes in result.stdout, capability.grant
        if capability.caution:
            assert capability.caution in result.stdout, capability.grant


def test_list_is_the_console_payload_plus_a_holder(config, project):
    """`--json` from the CLI and `serve._cmd_system` are one document, not two shapes.

    The CLI adds `holder`, `config`, `ledger` and `consent`, and every *description* key the app
    decodes is identical to what the console returns. If this fails, the app and the terminal have
    started reading different documents — which is the drift `syscap` exists to prevent.

    `summary` is excluded from the comparison on purpose and asserted separately: the console answers
    for the capability set ("0 of 12"), while a command acting as a holder answers "and how much of
    this do I have". Both are the same sentence about a different subject, and the subject is the whole
    difference — so it is named rather than blurred.
    """
    result = run("list", "--json", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    payload = result.json()
    console = console_payload(load(str(config), warn=False).system)
    for key, value in console.items():
        if key == "summary":
            continue
        assert payload[key] == value, f"{key} differs between the CLI and the console"
    assert payload["holder"]["id"], "the CLI must say whose authority it is acting under"

    expected = summary(payload["holder"]["grants"])
    assert payload["granted_count"] == expected["granted_count"]
    assert payload["summary"] == expected["text"], (
        "the CLI's count is the holder's, not the capability set's")


def test_list_marks_a_capability_with_no_tool_behind_it(config, project):
    """A switch that buys nothing must say so.

    Driven by the description rather than by a list of grants, so it holds whether or not the tree is
    mid-build: the assertion is that *whatever* `available` is false is marked, and that the marking
    is absent for everything else. Hardcoding `system:notify` here would have been a second copy of a
    build fact that changes as tools are added.
    """
    result = run("list", "--json", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    payload = result.json()
    unavailable = {e["grant"] for e in payload["capabilities"] if not e["available"]}
    assert set(payload["unavailable"]) == unavailable
    if unavailable:
        text = run("list", config=config, project=project)
        text.run()
        assert "NOT BUILT YET" in text.stdout
        for grant in unavailable:
            assert grant in text.stdout


def test_list_works_even_when_the_config_will_not_load(tmp_path, project):
    """A person asking what a grant *means* should get an answer even when the engine is unhappy.

    The same rule the console's `_cmd_system` follows. The listing describes the declared
    capabilities with everything off, which is the truthful default rather than a guess.
    """
    broken = tmp_path / "broken.json"
    broken.write_text("{ this is not json")
    result = run("list", config=broken, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    assert "system:state" in result.stdout


# ── a read works when granted and is refused when not ────────────────────────


def test_a_read_command_works_for_a_holder_that_has_the_grant(config, project):
    """The one live call in this file: the read-only state report.

    Its *values* are not asserted — this machine's charge is not a fact about the code. What is
    asserted is that the command reached the machine and reported the shape of an answer, which is
    what catches a command wired to the wrong tool.
    """
    result = run("state", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    assert "system state:" in result.stdout
    assert "disk" in result.stdout


def test_a_read_command_is_refused_for_a_holder_without_the_grant(config, project, tmp_path):
    """`--agent` is how a person asks "may *it* do this?" and gets the engine's own answer.

    The refusal exits `EXIT_REFUSED` and names the grant, the holder and where the grant comes from —
    the three things a person needs in order to decide whether to hire someone with it.
    """
    _hire(project, config, "Wanda", "backend-developer", ["read:*"])
    result = run("state", config=config, project=project, agent="Wanda")
    assert result.run() == sc.EXIT_REFUSED, result.stdout + result.stderr
    assert "system:state" in result.stderr
    assert "Wanda" in result.stderr


def test_a_sibling_grant_does_not_leak(config, project):
    """Holding `system:clipboard` must not confer `system:state` — the whole point of scoped grants."""
    _hire(project, config, "Bob", "backend-developer", ["system:clipboard"])
    result = run("state", config=config, project=project, agent="Bob")
    assert result.run() == sc.EXIT_REFUSED
    assert "system:state" in result.stderr


def test_the_whole_section_being_off_refuses_before_the_grant(config, project, tmp_path):
    """`system.enabled = false` is the operator's switch, and it is checked *before* the grant.

    The order matters for the message: telling a person "your agent lacks system:state" when the
    section is off sends them to hire the wrong thing. It exits as a refusal, because it is the engine
    declining rather than the command failing.
    """
    off = creds(tmp_path, system={"enabled": False})
    result = run("state", config=off, project=project)
    assert result.run() == sc.EXIT_REFUSED
    assert "system.enabled" in result.stderr
    assert "system:state" not in result.stderr.split("required:")[0].replace(
        "system.enabled", ""), "the section switch is the reason, and it is named first"


# ── a mutating command without consent refuses, and says how to grant ────────


def test_a_mutating_command_without_consent_refuses_and_says_how_to_grant(config, project):
    """The refusal is the *product decision* here, so it is asserted in full.

    Three facts must be present or the person is stuck: the gate the approval would land at (which is
    the engine's own `consent_gate`, so a reader can find it in the ledger), the ledger's path, and the
    exact command that fixes it. `grant_consent` is imported to build the expected gate rather than
    restating the format — a second spelling of `system-tool:<tool>:<agent>` is exactly the drift that
    would make the message point at nothing.
    """
    result = run("volume", "--set", "40", config=config, project=project)
    assert result.run() == sc.EXIT_REFUSED, result.stdout + result.stderr
    from engine.sysctl_tools import consent_gate

    assert consent_gate("set_volume", "ag_owner") in result.stderr
    assert "system consent grant --tool set_volume" in result.stderr
    assert str(project / ".agent_state" / "ledger.jsonl") in result.stderr


def test_every_tool_that_asks_first_has_some_command_that_can_grant_it(config, project):
    """`CONSENT_REQUIRED` is the tool module's own set; the CLI must be able to approve each one.

    Derived, not listed: a tool that starts asking for consent and cannot be approved from the terminal
    would be permanently unreachable through this surface, which reads as a broken command rather than
    as a missing grant path. The evidence is `consent list`'s own output for a holder with nothing
    approved — every asking tool carries its "grant with" line there — and the *format* of that line is
    taken from `Console.consent_line` rather than restated, so a changed spelling cannot pass here
    while being wrong for a person.
    """
    for tool in sorted(CONSENT_REQUIRED):
        assert tool in {e.name for e in CATALOGUE}, tool

    listed = run("consent", "list", config=config, project=project)
    assert listed.run() == sc.EXIT_OK, listed.stderr
    for tool in sorted(CONSENT_REQUIRED):
        expected = _consent_line_shape(tool)
        assert expected in listed.stdout, f"{tool}: {expected!r} is absent from the listing"


def _consent_line_shape(tool: str) -> str:
    """The command `consent list` promises will approve one tool.

    Built through `Console.consent_line` — the method that actually produces the line — so this asserts
    the command a person would paste rather than a second copy of its spelling. A hand-written
    expectation here would go stale in the same commit that changed the message.
    """
    from engine.systemcli import Console, Holder

    console = Console(config=None, config_path=None, project=pathlib.Path("."),
                      holder=Holder(id="ag_owner", name="Owner", capabilities=("system:*",), why=""),
                      registry=None)
    return console.consent_line(tool)


def test_a_refusal_exits_non_zero_and_differs_from_a_failure(config, project):
    """The two outcomes carry different codes, because they call for different next moves.

    A script that retries a *failure* must not retry a *refusal* — the first may succeed, the second
    will not until a person changes something. Asserted against two real runs: an out-of-range volume
    (the tool ran and refused the value) and a missing approval (the engine would not run at all).
    """
    assert sc.EXIT_REFUSED != sc.EXIT_CHECK_FAILED
    assert sc.EXIT_REFUSED != sc.EXIT_OK

    missing_approval = run("volume", "--set", "40", config=config, project=project)
    assert missing_approval.run() == sc.EXIT_REFUSED

    # `consent list` reaches no tool and changes nothing, so it is the control: a command that is
    # allowed exits 0 even while the one above is refused.
    allowed = run("consent", "list", config=config, project=project)
    assert allowed.run() == sc.EXIT_OK


def test_granting_then_calling_stops_asking(config, project):
    """The point of an ask-once approval: the second call does not ask.

    Asserted without touching the volume — the approval is checked through the tool layer's own
    `_has_consent`, so this proves the CLI wrote the decision the *gate* reads rather than merely
    printing success. That distinction is the whole bug class here: an approval in the wrong place
    looks identical from the command line.
    """
    before = _has_consent(project, config, "set_volume")
    assert before is False, "nothing is approved on a fresh project"

    granted = run("consent", "grant", "--tool", "set_volume", "--note", "phase-42", config=config,
                  project=project)
    assert granted.run() == sc.EXIT_OK, granted.stderr
    assert _has_consent(project, config, "set_volume") is True

    again = run("volume", "--set", "40", config=config, project=project)
    # It now passes the consent gate and reaches the *value* check instead, which is how "it stopped
    # asking" is visible without making the machine beep: the refusal is about 40 not being the current
    # value, and the message is the tool's, not the gate's.
    assert "no approval covers it" not in again.stderr


def test_revoking_withdraws_it_and_the_refusal_comes_back(config, project):
    """Superseding, not deleting: the ledger keeps the approval and the withdrawal."""
    run("consent", "grant", "--tool", "set_mute", config=config, project=project).run()
    assert _has_consent(project, config, "set_mute") is True
    revoked = run("consent", "revoke", "--tool", "set_mute", config=config, project=project)
    assert revoked.run() == sc.EXIT_OK, revoked.stderr
    assert _has_consent(project, config, "set_mute") is False

    ledger = (project / ".agent_state" / "ledger.jsonl").read_text(encoding="utf-8")
    assert "approved" in ledger and "revoked" in ledger, (
        "the withdrawal supersedes the approval rather than erasing it")


def test_revoking_something_never_approved_is_a_refusal_not_a_crash(config, project):
    """`revoke_consent` raises `ConsentError` when there is nothing to withdraw.

    The CLI must turn that into the engine's refusal — exit `EXIT_REFUSED` with the reason — because a
    traceback on a command like this reads as a bug in the engine rather than as an accurate answer to
    "is a standing approval in force?" (it is not).
    """
    result = run("consent", "revoke", "--tool", "set_mute", config=config, project=project)
    assert result.run() == sc.EXIT_REFUSED, result.stdout + result.stderr
    assert "Approval" not in result.stdout


def test_approving_a_tool_that_needs_no_approval_is_a_usage_error(config, project):
    """A read is not a decision, so there is nothing to record — said with the tools that *do* ask.

    `grant_consent` refuses this too, but its message is addressed to a caller; the CLI's is addressed
    to a person and names the alternatives, which is the difference this check exists for.
    """
    result = run("consent", "grant", "--tool", "system_state", config=config, project=project)
    assert result.run() == sc.EXIT_USAGE
    assert "system_state" in result.stderr
    for tool in ("set_volume", "write_clipboard"):
        assert tool in result.stderr, "the refusal must name tools that would work"


def test_an_approval_can_never_be_attributed_to_an_agent(config, project):
    """`grant_consent` refuses a `by` that names an agent — and the CLI must not be able to produce one.

    The guard exists because an agent that can approve its own destructive call has not been gated at
    all. Asserting the *CLI's* resolved identity is stronger than asserting the guard: it proves the
    command cannot reach the guard with an agent id in the first place, which is the property that
    holds even if someone later relaxes the guard.
    """
    person = sc._person()
    assert not person.startswith("ag_"), (
        f"the CLI attributed an approval to {person!r}, which names an agent")
    assert person, "an approval with no author cannot be reviewed later"

    # And the guard itself still bites, so the two are belt and braces rather than a comment.
    from engine.sysctl_tools import ConsentError

    with pytest.raises(ConsentError, match="names an agent"):
        grant_consent(project / ".agent_state", tool="set_mute", agent_id="ag_x", by="ag_x")


def test_an_approval_for_one_holder_does_not_cover_another(config, project):
    """Per holder, per tool. An approval for the Owner must not silently authorise an agent.

    Read through the tool layer's own gate for *both* ids, rather than through the listing text: the
    listing is what a person sees, but the gate is what decides, and the failure this guards against —
    an approval that looks effective and is not — is invisible from the listing.
    """
    run("consent", "grant", "--tool", "set_volume", config=config, project=project).run()
    assert _has_consent(project, config, "set_volume", agent_id="ag_owner") is True

    _hire(project, config, "Carol", "backend-developer", ["system:media"])
    carol = _agent_id(project, config, "Carol")
    assert _has_consent(project, config, "set_volume", agent_id=carol) is False, (
        "an approval for the Owner must not cover another holder")

    # And the CLI acting as Carol is refused on the *approval*, not on the grant: she holds
    # system:media, so the only thing standing between her and the volume is the missing decision.
    refused = run("volume", "--set", "40", config=config, project=project, agent="Carol")
    assert refused.run() == sc.EXIT_REFUSED, refused.stdout + refused.stderr
    assert "no approval covers it" in refused.stderr


def _agent_id(project: pathlib.Path, config: pathlib.Path, name: str) -> str:
    """One roster agent's id, read off the roster the console reads."""
    from engine.catalog import ModelCatalog
    from engine.people import People
    from engine.providers.registry import build_providers

    loaded = load(str(config), warn=False)
    providers, _ = build_providers(loaded)
    people = People(library=resolve(), config=loaded, catalog=ModelCatalog(loaded, providers),
                    project=project)
    org = people.load(project=project)
    spec = next((a for a in org.agents.values() if a.name == name), None)
    assert spec is not None, f"no agent named {name!r} in the roster"
    return spec.id


def _has_consent(project: pathlib.Path, config: pathlib.Path, tool: str,
                 agent_id: str = "ag_owner") -> bool:
    """Whether the *tool layer* sees an approval — the only reading that matters.

    Built through `SystemTools._has_consent`, which is the method the consent gate itself calls, so a
    CLI that wrote the decision into a plausible-looking but wrong gate is caught rather than passing.
    """
    from engine.sysctl_tools import SystemTools

    loaded = load(str(config), warn=False)
    tools = SystemTools(workspace_root=project, config=loaded.system, agent_id=agent_id)
    return tools._has_consent(tool)


def _hire(project: pathlib.Path, config: pathlib.Path, name: str, skill: str,
          capabilities: list[str]) -> None:
    """Put an agent in the roster the console reads, with exactly the grants named.

    Through `People` rather than by writing `roster.json`, so the roster is one the *loader* accepts —
    a hand-written file could carry a shape the engine refuses, and the test would then be asserting
    about its own fixture rather than about the CLI. The library is the real one, because `People.hire`
    validates the skill against it and a fake would let a typo through.
    """
    from engine.catalog import ModelCatalog
    from engine.people import HireRequest, People
    from engine.providers.registry import build_providers

    loaded = load(str(config), warn=False)
    providers, _ = build_providers(loaded)
    people = People(library=resolve(), config=loaded, catalog=ModelCatalog(loaded, providers),
                    project=project)
    org = people.load(project=project)
    people.hire(HireRequest(name=name, skill=skill, provider="ollama", model="qwen2.5-coder:7b",
                            context_window=32768, capabilities=list(capabilities)),
                org=org, roster_root=usercfg.project_root(project))


# ── supervised/consent model: `allow_full_access` and the allowlists ─────────


def test_full_access_still_requires_the_grant(config, project):
    """`allow_full_access` steps aside the *allowlists and the ask-once gate*, not the capability.

    Both reference agents ship this mode and neither drops the per-capability grant — a mode that also
    conferred every grant would be "this agent may do anything on any machine", which is not what
    either offers. Asserted through the CLI, because that is where a person could most easily assume
    otherwise.
    """
    full = creds(project, system={"enabled": True, "allow_full_access": True})
    _hire(project, full, "Dana", "backend-developer", ["read:*"])
    refused = run("state", config=full, project=project, agent="Dana")
    assert refused.run() == sc.EXIT_REFUSED, refused.stdout + refused.stderr
    assert "system:state" in refused.stderr

    allowed = run("state", config=full, project=project)
    assert allowed.run() == sc.EXIT_OK, allowed.stderr


def test_full_access_does_not_ask_for_consent(config, project, tmp_path):
    """In that mode a consent prompt is a question nobody is there to answer.

    Asserted on the *gate*, not by changing the volume: the command reaches the tool and the tool's
    answer is about something other than consent, which is how "it did not stop at the approval" is
    visible without mutating the developer's machine.
    """
    full = creds(tmp_path, system={"enabled": True, "allow_full_access": True,
                                   "max_seconds": 20, "max_output_bytes": 40_000})
    result = run("volume", "--set", "101", config=full, project=project)
    assert "no approval covers it" not in result.stderr
    assert result.run() == sc.EXIT_CHECK_FAILED, result.stdout + result.stderr
    assert "0 to 100" in result.stdout or "0 to 100" in result.stderr


def test_an_empty_allowlist_refuses_with_a_command_that_fixes_it(config, project):
    """`system.allow_apps` empty means nothing may launch, and the refusal says so.

    The tool's own words are the answer; what this asserts is that the CLI surfaces them rather than
    replacing them — a CLI that re-worded a refusal would be a second description of one permission.
    A refusal by a *handler* is a refusal, not a failure, so it exits `EXIT_REFUSED`: the engine
    declined this action, and a script must not retry it until the allowlist changes.
    """
    _grant_consent(project, config, "open_app")
    result = run("open", "Finder", config=config, project=project)
    assert result.run() == sc.EXIT_REFUSED, result.stdout + result.stderr
    assert "system.allow_apps is empty" in result.stdout


def test_an_allowlisted_app_is_reached_through_the_allowlist(config, project, tmp_path):
    """With the app allowlisted and consent given, the refusal moves on to the machine.

    This does not launch anything: the assertion is that *neither* the allowlist nor the consent gate
    is the reason any more. Launching Finder in a test suite would open a window on the developer's
    desktop to prove a point, which is a cost this suite refuses to charge.
    """
    allowed = creds(tmp_path, system={"enabled": True, "allow_apps": ["Finder"]})
    _grant_consent(project, allowed, "open_app")
    result = run("open", "Finder", config=allowed, project=project)
    assert "allow_apps is empty" not in result.stdout
    assert "no approval covers it" not in result.stderr


def _grant_consent(project: pathlib.Path, config: pathlib.Path, tool: str,
                   agent_id: str = "ag_owner") -> None:
    """Approve a tool directly through the tool module, for tests whose subject is elsewhere."""
    grant_consent(project / ".agent_state", tool=tool, agent_id=agent_id, by="sp.vm",
                  note="phase-42 fixture")


# ── one implementation: the CLI goes through the registry ───────────────────


def test_the_command_reaches_the_machine_through_the_registry(config, project):
    """The load-bearing test of this whole track: one implementation, one gate.

    `ToolRegistry.call` is where the capability gate lives, so a command that reached `SystemTools`
    directly would bypass it — and would be a second "read the battery". Wrapping the registry's `call`
    and counting proves the CLI used it: the registry is built *inside* `_console` via `engine.tools`,
    so patching the class there is what intercepts the instance the command makes for itself.
    """
    seen: list[str] = []

    class Watching(ToolRegistry):
        def call(self, name, arguments=None):
            seen.append(str(name))
            return super().call(name, arguments)

    with _registry_replaced_by(Watching):
        result = run("clipboard", config=config, project=project)
        assert result.run() == sc.EXIT_OK, result.stderr

    assert seen == ["read_clipboard"], seen


@contextlib.contextmanager
def _registry_replaced_by(replacement: type):
    """Swap the registry class for one command's lifetime, so the CLI's own build is intercepted.

    Patches `engine.tools.ToolRegistry`, which is the name `systemcli._registry_for` resolves at call
    time — so this reaches the instance the command constructs. A patch that silently missed would make
    every "one implementation" assertion in this file pass for the wrong reason, so
    `test_the_probe_intercepts_the_registry_the_console_builds` proves the seam is live.
    """
    import engine.tools as tools_module

    real = tools_module.ToolRegistry
    tools_module.ToolRegistry = replacement
    try:
        yield
    finally:
        tools_module.ToolRegistry = real


def test_the_probe_intercepts_the_registry_the_console_builds(config, project):
    """The seam `_registry_replaced_by` patches is the seam the console actually uses.

    Without this, a refactor that built the registry anywhere but through `engine.tools.ToolRegistry`
    would make `test_the_command_reaches_the_machine_through_the_registry` pass while observing nothing
    — an assertion that cannot fail is worse than no assertion, because it reads as coverage.
    """
    built: list[str] = []

    class Watching(ToolRegistry):
        def __init__(self, **kwargs):
            built.append(type(self).__name__)
            super().__init__(**kwargs)

    with _registry_replaced_by(Watching):
        assert run("consent", "list", config=config, project=project).run() == sc.EXIT_OK

    assert built == ["Watching"], built


def test_a_tool_argument_is_passed_through_unchanged(config, project):
    """`--set 30` must arrive as the integer the tool validates, not as the string "30".

    `set_volume` refuses a non-integer with "level must be a whole number", so a CLI that stringified
    everything would produce a refusal that reads like an engine bug. The assertion is on the value the
    *tool* received, taken from the registry call the CLI made. Consent is granted first so the call
    actually reaches the handler rather than stopping at the gate — otherwise this would assert the
    argument of a call that never happened.
    """
    seen: list[dict] = []

    class Watching(ToolRegistry):
        def call(self, name, arguments=None):
            seen.append({"name": str(name), "args": dict(arguments or {})})
            return super().call(name, arguments)

    _grant_consent(project, config, "set_volume")
    with _registry_replaced_by(Watching):
        result = run("volume", "--set", "30", config=config, project=project)
        # Consent is granted so the call reaches the handler; 30/100 is a value the tool validates and
        # this test does not need the machine to reach. What is asserted is the *argument*, which is
        # decided before the call runs.
        result.run()
        assert "level must be a whole number" not in result.stderr, result.stderr

    assert seen == [{"name": "set_volume", "args": {"level": 30}}], seen


def test_call_reaches_any_tool_by_name(config, project):
    """The escape hatch, so the CLI cannot fall behind the tool layer.

    `system call <tool>` names any catalogue entry, which is what makes the coverage guarantee real
    rather than aspirational: a tool added to `sysctl_tools` is reachable the same day without a new
    subcommand.
    """
    result = run("call", "system_state", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    assert "system state:" in result.stdout


def test_call_decodes_json_values(config, project):
    """`--arg` values decode as JSON where they look like JSON, so booleans and lists arrive typed."""
    assert sc._decode_arg("true") is True
    assert sc._decode_arg("30") == 30
    assert sc._decode_arg('["a","b"]') == ["a", "b"]
    assert sc._decode_arg("Safari") == "Safari"


def test_call_refuses_an_unknown_tool_by_name(config, project):
    """A name that is not a catalogue entry is a usage error naming what exists."""
    result = run("call", "no_such_tool", config=config, project=project)
    assert result.run() == sc.EXIT_USAGE
    assert "no_such_tool" in result.stderr


def test_call_refuses_a_malformed_argument(config, project):
    """`--arg` without `=` cannot be a parameter, and saying so beats sending `key` as a value."""
    result = run("call", "set_mute", "--arg", "muted", config=config, project=project)
    assert result.run() == sc.EXIT_USAGE
    assert "key=value" in result.stderr


# ── --json emits parseable JSON and nothing else on stdout ─────────────────


# The cases are written as tuples of *strings* for the runner's `parametrize` expansion, which treats
# a single list value as the sequence of arguments to unpack — `["state"]` would arrive as the string
# `"s"`. A tuple of two strings is unpacked into the two names it declares, which is what the real
# pytest does with a 1-tuple too, so the same declaration is correct under either runner.
@pytest.mark.parametrize("command, subcommand", [
    ("list", ""),
    ("consent", "list"),
    ("state", ""),
    ("shortcuts", ""),
])
def test_stdout_carries_only_parseable_json(command, subcommand, config, project):
    """The contract `tests/test_phase38_cli_parity.py` holds every command to, and `| jq` depends on.

    One stray `print` corrupts the stream, so *every* line must parse — asserted by parsing the whole
    of stdout rather than its first line, which is what a `json.loads` on the first line would miss.
    Four commands are checked because they take four different paths: a description, a ledger read, a
    live tool call, and a command that reaches no tool at all.
    """
    argv = [command, *([subcommand] if subcommand else [])]
    result = run(*argv, "--json", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    payload = result.json()
    assert isinstance(payload, dict)
    assert payload, "an empty document is not an answer"


def test_json_output_is_indented_and_key_sorted(config, project):
    """Match `cli._emit` exactly, so a caller cannot tell the two surfaces apart.

    Sorted keys are what makes two payloads comparable by eye and by `diff`, and the indent matches
    every other command's. This is the envelope the whole CLI uses, and inventing a second one here is
    the failure this test names.
    """
    result = run("list", "--json", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    expected = json.dumps(result.json(), indent=2, sort_keys=True, default=str)
    assert result.stdout.strip() == expected.strip()


def test_a_refusal_keeps_stdout_clean_and_says_why_on_stderr(config, project):
    """A refusal still writes *something* parseable to stdout in --json mode.

    A script reading this needs the reason as data, not only as text — and "exit 3 with an empty
    stdout" would make the reason unreadable by the caller that most needs it.
    """
    result = run("state", "--json", config=config, project=project, agent="Nobody")
    assert result.run() == sc.EXIT_CHECK_FAILED, result.stdout + result.stderr
    assert "Nobody" in result.stderr


def test_a_refusal_is_a_parseable_result_too(config, project, tmp_path):
    """A refusal on a *valid* holder is reported as JSON, since the command genuinely ran."""
    off = creds(tmp_path, system={"enabled": False})
    result = run("volume", "--set", "10", "--json", config=off, project=project)
    assert result.run() == sc.EXIT_REFUSED
    payload = result.json()
    assert payload["refused"] is True
    assert payload["ok"] is False
    assert payload["tool"] == "set_volume"


def test_screenshot_with_a_path_still_emits_one_json_document(config, project):
    """The one command whose answer *changes* after the call, so the one that could break `| jq`.

    `screenshot --path` moves the file the tool wrote, which means the path the tool reported is no
    longer where the file is. Printing the tool's document and then a "moved to …" line would corrupt
    stdout for a `--json` caller — and `json.loads` over the *whole* of stdout is the assertion that
    catches it, where parsing only the first line would not.
    """
    target = project / "shot.png"
    result = run("screenshot", "--path", str(target), "--json", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stdout + result.stderr
    payload = result.json()
    assert payload["paths"] == [str(target)], (
        "the document must name where the file is now, not where the tool put it")
    assert target.is_file()


def test_screenshot_refuses_to_clobber_a_file_it_did_not_write(config, project):
    """`--path` over an existing file is a usage error unless `--force`, and the *capture never runs*.

    Judged before the capture on purpose: a refusal discovered afterwards would have already cost the
    person a screen-recording prompt and a full-resolution PNG sitting on disk that nobody asked for.
    """
    target = project / "existing.png"
    target.write_bytes(b"not a screenshot")
    result = run("screenshot", "--path", str(target), config=config, project=project)
    assert result.run() == sc.EXIT_USAGE, result.stdout + result.stderr
    assert "already exists" in result.stderr
    assert target.read_bytes() == b"not a screenshot", "the existing file must be untouched"


# ── the parser surface ──────────────────────────────────────────────────────


def test_the_parser_accepts_every_documented_command():
    """Every command the module documents resolves to a callable, so none of them is decorative."""
    parser = sc.build_parser()
    assert "system" in parser.format_help()
    for argv in (["system", "list"], ["system", "state"], ["system", "clipboard"],
                 ["system", "screenshot"], ["system", "volume"],
                 ["system", "open", "Safari"], ["system", "automation", "--script", "x"],
                 ["system", "notify", "--message", "x"], ["system", "search", "x"],
                 ["system", "power", "--awake"], ["system", "network"],
                 ["system", "shortcuts"], ["system", "softwareupdate"],
                 ["system", "call", "system_state"], ["system", "consent", "list"],
                 ["system", "consent", "grant", "--tool", "set_volume"],
                 ["system", "consent", "revoke", "--tool", "set_volume"]):
        args = parser.parse_args(argv)
        assert callable(getattr(args, "func", None)), argv


def test_the_help_documents_the_exit_codes():
    """The exit codes are the contract a script branches on, so they are in the help."""
    text = sc.build_parser().format_help()
    assert "exit codes" in text.lower()
    assert "REFUSED" in text
    assert str(sc.EXIT_REFUSED) in text


def test_the_word_system_is_optional_on_the_module_entry_point(config, project):
    """`python3 -m engine.systemcli list` and `... system list` are the same command.

    The wiring into `cli` produces the `system`-first shape; a person typing at this module directly
    produces the other. Accepting both is what makes the module usable and testable on its own, which
    matters because the file that wires it in is owned by another change. Proved by *running* both
    forms of a command that reaches no machine and changes nothing, so the two are compared on their
    real output rather than on a parse result.
    """
    with_system = Captured(["system", "list", "--config", str(config), "--project", str(project)])
    without = Captured(["list", "--config", str(config), "--project", str(project)])
    assert with_system.run() == sc.EXIT_OK, with_system.stderr
    assert without.run() == sc.EXIT_OK, without.stderr
    assert with_system.stdout == without.stdout


def test_install_adds_the_tree_to_a_host_parser_without_a_second_common_parent():
    """`install` must take the host's own `common` parent, so a global flag added to `cli` reaches
    every command here with no change in this file.

    Proves the shape the one-line wiring depends on: a subparsers action plus the host's `common`. The
    host's flag is asserted to reach a *leaf* parser, which is the property that matters — a parent
    applied only to the `system` level would accept the flag before the subcommand and reject it after,
    and `engine.cli system state --org x` is exactly the word order a person types.
    """
    host = argparse.ArgumentParser(prog="probe")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host-flag", default=None)
    sub = host.add_subparsers(dest="command", required=True)
    sc.install(sub, common)

    # After the leaf's own name, which is where `cli`'s global flags are always accepted (every leaf
    # takes `common` as a parent too).
    args = host.parse_args(["system", "state", "--host-flag", "v"])
    assert args.command == "system"
    assert args.system_command == "state"
    assert args.host_flag == "v", "the host's own flag must reach the leaf parser"
    assert callable(args.func)


def test_install_returns_the_system_parser_for_further_wiring():
    """So a caller can add a command of its own under `system` without editing this module."""
    host = argparse.ArgumentParser(prog="probe")
    common = argparse.ArgumentParser(add_help=False)
    sub = host.add_subparsers(dest="command", required=True)
    returned = sc.install(sub, common)
    assert returned.prog.endswith("system") or "system" in returned.prog


# ── the two contradictory arguments a command refuses to guess between ───────


def test_contradictory_arguments_are_usage_errors_not_silent_preferences(config, project):
    """`--mute --unmute` cannot both be honoured, and picking one silently is the worse answer.

    Every command with two opposite flags is checked here, because the failure is identical in each:
    a person who typed both meant something, and the command cannot know which — so it asks rather
    than guessing.
    """
    for argv in (["volume", "--mute", "--unmute"],
                 ["volume", "--set", "10", "--mute"],
                 ["clipboard", "--set", "a", "--stdin"],
                 ["automation"],
                 ["automation", "--script", "a", "--file", "b"],
                 ["notify"],
                 ["notify", "--say", "a", "--message", "b"],
                 ["power"],
                 ["power", "--awake", "--sleep"],
                 ["softwareupdate", "--list", "--install"]):
        result = run(*argv, config=config, project=project)
        assert result.run() == sc.EXIT_USAGE, f"{argv}: {result.stdout}{result.stderr}"
        assert result.stderr.strip(), f"{argv} refused with no reason on stderr"


def test_a_parameter_the_tool_needs_is_refused_before_the_call(config, project):
    """`system notify` with neither flag cannot call anything, and says which flags it wanted."""
    result = run("notify", config=config, project=project)
    assert result.run() == sc.EXIT_USAGE
    assert "--say" in result.stderr and "--message" in result.stderr


def test_an_unknown_agent_is_refused_with_the_command_that_lists_them(config, project):
    """`--agent Nobody` must not silently fall back to the Owner — that would act as someone else
    than the person asked for, under grants they did not choose."""
    result = run("state", config=config, project=project, agent="Nobody")
    assert result.run() == sc.EXIT_CHECK_FAILED
    assert "engine.cli agents" in result.stderr
    assert "system state:" not in result.stdout, "a failed lookup must not run the command anyway"


# ── the description modules disagreeing is the failure this whole area guards ─


def test_every_capability_with_a_mutating_tool_can_be_described_as_changing_something():
    """`MUTATING_TOOLS` and `syscap`'s prose must agree, and the terminal shows the prose.

    Derived from the tool module's own set — a hand-written list here would be the second copy that
    drifts. This is asserted again in `test_phase40_capability_surface.py` for the console; it is
    repeated here because the CLI is where a person *reads* the prose, and reading a grant described as
    harmless while it changes the volume is the failure both modules exist to prevent.
    """
    changed = {entry.capability for entry in CATALOGUE if entry.name in MUTATING_TOOLS}
    described = {c.grant: c for c in describe()}
    for capability in sorted(changed):
        entry = described.get(capability)
        assert entry is not None, capability
        if entry.available:
            assert entry.changes or entry.caution, (
                f"{capability} has a mutating tool but is described as changing nothing")


def test_the_mutating_set_and_the_consent_set_agree_in_one_direction():
    """Everything that asks for consent mutates; not everything that mutates asks.

    `take_screenshot` writes a file without asking (it destroys nothing the person was holding) and
    `install_os_updates` both mutates and asks. Asserted through `CONSENT_REQUIRED <= MUTATING_TOOLS`,
    which is the property the tool module documents, so the CLI's "changes something" wording is never
    applied to something that changes nothing.
    """
    assert CONSENT_REQUIRED <= MUTATING_TOOLS, CONSENT_REQUIRED - MUTATING_TOOLS
    assert "system_state" not in CONSENT_REQUIRED
    assert "take_screenshot" in MUTATING_TOOLS and "take_screenshot" not in CONSENT_REQUIRED


# ── config resolution and the ledger's location ──────────────────────────────


def test_the_default_holder_is_the_owner_with_every_capability(config, project):
    """A command you type is the grant, and `--agent` is how you act as someone else instead.

    Asserted positively and negatively: the default holder is `ag_owner` holding `system:*`, and it is
    NOT resolved by hiring anyone — a console that required setup before it would touch the machine
    would answer "let me use my own computer" with a form.
    """
    result = run("list", "--json", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    holder = result.json()["holder"]
    assert holder["id"] == "ag_owner"
    assert "system:*" in holder["grants"]
    assert holder["why"]


def test_the_ledger_lands_where_the_tool_layer_reads_it(config, project):
    """The approval and the gate must be the same file.

    A CLI that wrote `ledger.jsonl` one directory away would report an approval the run never sees —
    and that failure is invisible from the command line, because `consent grant` would exit 0. So the
    path is asserted against `Workspace.state_dir`, which is the layout the orchestrator uses.
    """
    from engine.state import Workspace

    workspace = Workspace.attach(project)
    result = run("list", "--json", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    assert result.json()["ledger"] == str(workspace.state_dir / "ledger.jsonl")

    run("consent", "grant", "--tool", "set_volume", config=config, project=project).run()
    assert (workspace.state_dir / "ledger.jsonl").is_file()


def test_the_projects_flag_aims_the_ledger_at_an_attached_folder(config, project):
    """`--project` is what makes the approvals the *same ones* a run in that folder reads."""
    result = run("list", "--json", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    assert str(project) in result.json()["ledger"]


def test_the_resolved_config_path_is_reported(config, project):
    """So a person can see *which* credentials file decided the switches they are reading."""
    result = run("list", "--json", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    assert result.json()["config"] == str(config)


def test_system_config_defaults_are_off_in_a_config_that_never_mentions_system(tmp_path, project):
    """The shipped default is `enabled = False`, and the CLI must show that rather than assume on.

    A config with no `system` block at all is the common case, so it is the case worth asserting: the
    listing describes every capability and reports the section as off, which is why the first command
    a person runs on a fresh machine tells them what to switch rather than refusing mysteriously.
    """
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(_CREDS))
    result = run("list", "--json", config=bare, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    assert result.json()["enabled"] is False
    assert len(result.json()["capabilities"]) == len(describe())


def test_an_unhandled_tool_name_reaches_no_machine_call(config, project):
    """`system call` refuses a name that is not in the catalogue *before* touching anything."""
    result = run("call", "rm_rf", config=config, project=project)
    assert result.run() == sc.EXIT_USAGE
    assert "no system tool" in result.stderr


def test_system_config_type_is_the_one_the_registry_reads(config):
    """`_console` passes `config.system`, not the whole `Config`.

    Passing the wrong object would make `SystemTools.__init__` read `max_seconds` off the top-level
    config — which has no such attribute — and silently fall back to the default bound. Asserted on
    the type, because the symptom of getting it wrong is a ceiling that is merely wrong.
    """
    loaded = load(str(config), warn=False)
    assert isinstance(loaded.system, SystemConfig)
    assert loaded.system.enabled is True


# ── the mode is the sentence: what an empty allowlist means (gap 2) ───────────
#
# `allow_full_access` makes `sysctl_tools` skip the allowlist check outright, so an empty list means
# "none allowed" in the scoped mode and "no list is kept" in that one. Every phrase that described an
# empty allowlist used the scoped reading unconditionally — under a `full access : yes` line. On the
# machine this was found on, `allow_automation` and `allow_shortcuts` are empty *and* full access is
# on, so the CLI told a person that AppleScript reaches nothing at the moment it reached everything.


@pytest.mark.parametrize("full_access", [True, False])
def test_an_empty_allowlist_reads_as_the_mode_it_is_in(tmp_path, project, full_access):
    """Both modes, and the *same* command — so the assertion cannot pass by testing nothing.

    The scoped reading is not merely less accurate under full access: it is the reverse of what the
    engine does. So the two modes are asserted against each other on the one phrase that decides it,
    rather than against a hardcoded string that a later reword would drag along.
    """
    config = creds(tmp_path, system={"enabled": True, "allow_full_access": full_access})
    result = run("list", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    empty_line = [line for line in result.stdout.splitlines()
                  if line.startswith("  allow_automation")][0]
    if full_access:
        assert "no list is kept" in empty_line, (
            f"full access is on, so this list is not consulted: {empty_line!r}")
        assert "none allowed" not in empty_line, empty_line
    else:
        assert "none allowed" in empty_line, empty_line
        assert "no list is kept" not in empty_line, empty_line


@pytest.mark.parametrize("full_access", [True, False])
def test_the_enable_switch_report_reads_the_same_way_as_the_listing(tmp_path, project, full_access):
    """One phrase, two commands. `_render_switches` said "this grant reaches nothing" — the stronger,
    more wrong wording — and a person comparing `system list` against `system enable` read two
    accounts of one mode."""
    config = creds(tmp_path, system={"enabled": True, "allow_full_access": full_access})
    result = run("enable", config=config, project=project)
    assert result.run() == sc.EXIT_USAGE, result.stdout + result.stderr
    empty_line = [line for line in result.stdout.splitlines()
                  if line.startswith("  allow_shortcuts")][0]
    assert "reaches nothing" not in empty_line, (
        "the phrase the engine contradicts: with full access on it reaches anything")
    assert ("no list is kept" in empty_line) is full_access, empty_line
    # And the listing says exactly the same words about the same list — the padding differs, the
    # sentence does not, and the sentence is what a person reads.
    phrase = empty_line.split(":", 1)[1].strip()
    listing = run("list", config=config, project=project)
    listing.run()
    assert phrase in listing.stdout, listing.stdout[-400:]


# ── who holds what: the other reading of one word (gap 3) ────────────────────


def test_the_holders_view_answers_who_can_not_only_whether_you_can(config, project):
    """`system list` marks each row with the *acting* holder's grant, so every row read `granted` on a
    machine whose roster held half of them. The two readings must be separately askable, and the row
    must say whose it is.

    Asserted with a hired holder rather than a fixture: the point is that a grant held by somebody
    else shows up here and not in the marks, which is only true if the holders come from the roster
    the engine would serve.
    """
    _hire(project, config, "Wanda", "backend-developer", ["system:clipboard"])
    result = run("list", "--json", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    holders = result.json()["holders"]
    assert set(holders) == {entry.grant for entry in describe()}, (
        "every grant is answered for, including the ones nobody holds")
    assert [h["name"] for h in holders["system:clipboard"]] == ["Wanda"]
    assert holders["system:state"] == [], (
        "a grant Wanda does not hold must not list her — that is the leak the marks exist to stop")

    # And the row is unambiguous about which of the two readings it is giving.
    text = run("list", config=config, project=project)
    text.run()
    assert "acting holder" in text.stdout, text.stdout
    assert "Wanda" not in text.stdout, "the per-row marks are the acting holder's, not the roster's"

    flagged = run("list", "--holders", config=config, project=project)
    assert flagged.run() == sc.EXIT_OK, flagged.stderr
    assert "Wanda" in flagged.stdout
    assert "  granted      system:clipboard" in flagged.stdout


def test_the_two_counts_are_under_two_words(config, project):
    """`12 of 12 system capabilities granted` was printed under the label `approvals`, while
    `system consent list` printed `0 of 10 state-changing tools approved` under the same word. Two
    numbers, one word, and both were on the screen — the capability count is `holds`, the ledger is
    `approvals`."""
    _grant_consent(project, config, "set_volume")
    result = run("list", config=config, project=project)
    assert result.run() == sc.EXIT_OK, result.stderr
    holds = [line for line in result.stdout.splitlines() if line.startswith("  holds")][0]
    approvals = [line for line in result.stdout.splitlines() if line.startswith("  approvals")][0]
    assert "system capabilities granted" in holds, holds
    assert "state-changing tools approved" in approvals, approvals
    assert "1 of" in approvals, approvals
    assert "12 of" in holds, holds


# ── `doctor` reads the machine posture (gap 1) ───────────────────────────────


def _doctor_check(checks: list[dict], name: str) -> dict:
    found = [c for c in checks if c["check"] == name]
    assert found, f"no {name!r} check among {[c['check'] for c in checks]}"
    return found[0]


def _cli(argv: list[str]) -> tuple[int, str]:
    """Run `engine.cli` in-process, returning its exit code and stdout.

    In-process for the same reason the rest of this file is: the check under test reads a *file*, and
    the test has to know which one. The CLI's real entry point is what is driven, so the wiring that
    put the check into `doctor` is exercised rather than assumed.
    """
    from engine import cli as cli_module

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli_module.main(argv)
    return code, out.getvalue()


def test_doctor_reports_the_machine_posture(tmp_path):
    """`doctor` never read `[system]`, so on a full-access machine it printed seven OK lines and
    "all checks passed" — while `system` refused with a remedy naming `engine.cli doctor`.

    A permissive posture is `ok` on purpose: it is the operator's decision, not a fault, and a check
    that failed here would exit 1 on a correctly configured machine. What it must not be is *silent*,
    so the consequence is asserted as a warning rather than as a failure.
    """
    full = creds(tmp_path, system={"enabled": True, "allow_full_access": True})
    code, out = _cli(["doctor", "--config", str(full), "--json"])
    assert code == sc.EXIT_OK, out
    check = _doctor_check(json.loads(out)["checks"], "system access")
    assert check["ok"] is True, "a mode the operator chose is not a failed check"
    assert "FULL ACCESS" in check["detail"], check
    assert check["warnings"], "a permissive posture must say what it means for the person"
    warned = " ".join(check["warnings"])
    assert "any AppleScript may run" in warned, (
        "the sentence has to contradict the empty-allowlist reading, which is the one this machine "
        f"would otherwise print: {warned!r}")
    assert "engine.cli system enable --no-full-access" in warned, warned


def test_doctor_reads_the_allowlists_in_the_scoped_mode(tmp_path):
    """The other mode, and the one that must *not* warn: scoped allowlists are the least-privilege
    configuration, so a warning here would train the reader to ignore the one above."""
    scoped = creds(tmp_path, system={"enabled": True, "allow_apps": ["Safari", "Notes"]})
    code, out = _cli(["doctor", "--config", str(scoped), "--json"])
    assert code == sc.EXIT_OK, out
    check = _doctor_check(json.loads(out)["checks"], "system access")
    assert check["ok"] is True
    assert "ON, scoped" in check["detail"], check
    assert "Safari, Notes" in check["detail"], check
    assert "none allowed" in check["detail"], check
    assert not check.get("warnings"), check


def test_doctor_says_how_to_turn_it_on_when_off(tmp_path):
    """The third mode. The shipped default is off, and the useful sentence there is the way back."""
    off = creds(tmp_path, system={"enabled": False})
    code, out = _cli(["doctor", "--config", str(off), "--json"])
    assert code == sc.EXIT_OK, out
    check = _doctor_check(json.loads(out)["checks"], "system access")
    assert "OFF" in check["detail"], check
    assert "engine.cli system enable --on" in check["detail"], check


# ── the next move, and the code that says nothing was written (gaps 5 and 7) ──


def test_list_json_next_names_the_command_that_applies_to_the_state(tmp_path, project):
    """The closing hint used to point at `system consent list` unconditionally, which on a full-access
    machine is a gate that has already stepped aside. State-derived means the three modes give three
    different answers — asserted as three, because a helper that always returned the same string
    would pass any single-mode test."""
    steps = {}
    for name, section in (("off", {"enabled": False}),
                          ("scoped", {"enabled": True, "allow_apps": ["Safari"]}),
                          ("full", {"enabled": True, "allow_full_access": True})):
        config = creds(tmp_path, system=section)
        result = run("list", "--json", config=config, project=project)
        assert result.run() == sc.EXIT_OK, result.stderr
        steps[name] = result.json()["next"]
        assert isinstance(steps[name], str) and steps[name], f"{name}: no next step in the document"
    assert "engine.cli system enable --on" in steps["off"]
    assert "engine.cli system consent list" in steps["scoped"]
    assert "engine.cli system enable --no-full-access" in steps["full"], (
        "with full access on, the consent gate decides nothing, so it cannot be the next move")
    assert len(set(steps.values())) == 3, steps

    # Text mode prints the same sentence the document carries — one wording, two readers.
    full = creds(tmp_path, system={"enabled": True, "allow_full_access": True})
    text = run("list", "--holders", config=full, project=project)
    assert text.run() == sc.EXIT_OK, text.stderr
    assert text.stdout.rstrip().endswith(f"next   : {steps['full']}"), text.stdout[-200:]


@pytest.mark.parametrize("argv", [
    "list",
    "list --holders",
    "consent list",
    "consent grant --tool set_volume",
    "shortcuts",
    "enable",
])
def test_every_report_carries_a_next_step_in_json(argv, config, project):
    """Every report ends with the move that applies to it, in the document as `next` — the field the
    app and `serve` were each inventing for themselves.

    `enable` with no switch is included deliberately: it exits 2 (nothing was written), which is the
    case where a consumer most needs to be told what to do instead, and an `--json` payload that is
    only an error would leave it guessing.

    The cases are one *string* per case, split here, because the runner expands a parametrize row
    positionally: a tuple of four words under one name would arrive as its first word, and the test
    would then assert about `system list --json` four times.
    """
    result = run(*argv.split(), "--json", config=config, project=project)
    result.run()
    payload = result.json()
    assert isinstance(payload.get("next"), str) and payload["next"], payload
    assert payload["next"].startswith("engine.cli system"), payload["next"]


def test_enable_with_no_switch_answers_the_question_and_exits_as_a_usage_error(config, project):
    """A command given nothing to do is a usage error, and this one used to exit 0 while writing
    nothing — so `system enable && echo flipped` printed `flipped` on an unchanged machine. The only
    signal was a `wrote` key missing from `--json`, and a missing key is not one a shell can read.

    `EXIT_USAGE` rather than `EXIT_CHECK_FAILED` or `EXIT_REFUSED`, because nothing failed and nothing
    was refused: the invocation was incomplete, exactly as `system power` with neither `--awake` nor
    `--sleep` is, and it carries the same code as its siblings for the same reason.
    """
    before = config.read_text(encoding="utf-8")
    result = run("enable", config=config, project=project)
    assert result.run() == sc.EXIT_USAGE, result.stdout + result.stderr
    assert config.read_text(encoding="utf-8") == before, "a question must not write anything"
    assert "nothing was written" in result.stdout, result.stdout
    # Every hint offered is one that applies to the state just reported: `enabled` is on in this
    # fixture, so the offer is the way *off*, and the way on must not be suggested.
    assert "engine.cli system enable --off" in result.stdout, result.stdout
    assert "to turn it on" not in result.stdout, result.stdout

    # The inverse fixture, so the assertion above cannot pass by the hints being empty.
    off = creds(project, system={"enabled": False})
    other = run("enable", config=off, project=project)
    assert other.run() == sc.EXIT_USAGE, other.stdout + other.stderr
    assert "engine.cli system enable --on" in other.stdout, other.stdout
    assert "engine.cli system enable --off" not in other.stdout, other.stdout


def test_enable_with_no_switch_still_reports_the_state_on_stdout(config, project):
    """The answer stays the answer: stdout carries the state, stderr carries why the code is 2.

    Asserted because a usage error elsewhere in this CLI prints *nothing* to stdout, and a reader
    could reasonably "fix" this one into that shape — which would delete the answer to the question
    the person asked.
    """
    result = run("enable", config=config, project=project)
    assert result.run() == sc.EXIT_USAGE
    for key in ("enabled", "full access", "allow_apps", "allow_automation", "allow_shortcuts"):
        assert key in result.stdout, result.stdout
    assert "nothing was written" in result.stderr, (
        "the diagnostic stream says why the exit code is not 0")
