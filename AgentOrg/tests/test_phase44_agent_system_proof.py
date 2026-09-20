#!/usr/bin/env python3
"""Phase 44 tests — an agent *inside a run* actually using a system tool.

WHY THIS EXISTS
---------------
`tools.py` builds a `SystemTools` into the registry, and a previous session fixed `system=` not being
passed at the executor's call site — before that, every system tool was **inert in a live run**. The
fix is one keyword argument, and a keyword argument is exactly the kind of claim that can be true in
one call site and false in the next. Nobody had shown an agent, mid-node, calling a machine tool and
getting a real result out of the loop.

So this file drives the whole path for real: an org, a bound agent, `tools: true`, the agentic loop,
a model that issues the call, and the `ToolResult` text read back out of the conversation. Nothing is
stubbed except the provider, and the provider is stubbed because the alternative is a network call.

WHAT IT PINS, AND WHY EACH LAYER IS ASSERTED SEPARATELY
-------------------------------------------------------
A system tool is offered and then allowed only when four separate things are true. They are four
because each is a different person's decision, and collapsing any two would make one of them
unanswerable:

1. **`system.enabled`** — the operator's switch. Off means the tool is not in the model's list at
   all, so the agent is not tempted by something it would only be refused.
2. **The agent's own `system:<capability>` grant** — the roster's. `allow_full_access` does **not**
   lift this, and the test proves it by running full access without the grant.
3. **`allow_full_access`** — which lifts the allowlists and the ask-once consent, and nothing else.
   Asserted *positively* (a consent-requiring tool proceeds), because a switch proved only by its
   refusals is a switch that could be doing nothing.
4. **The per-agent consent** — for the ten state-changing tools, keyed on `(tool, agent)`, recorded in
   the workspace's ledger.

Both halves of the load-bearing claim are here. A positive result alone cannot tell a working grant
from a missing check: an agent that is *refused* proves the gate fires, and one that is *allowed*
proves the gate was satisfied rather than bypassed. The negative controls name what was required, so
the refusal is the one a model would actually read — the same standard the rest of the suite holds
refusals to.

WHAT IS DELIBERATELY NOT DONE HERE
----------------------------------
No mutating call is executed against the machine. `set_volume` is driven only as far as a refusal the
*value* check produces, and `install_os_updates` is never asked for. A test that changes the
developer's volume or installs an OS update to prove a point is a test that costs the person, and the
gate that matters — does the call reach the tool with the right grant — is fully visible without it.
The one live call the suite makes is the read-only state report, and its *values* are never asserted:
this Mac's charge is not a fact about the code.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.artifacts import ArtifactStore
from engine.config import BudgetConfig, Config, ExecutorConfig, SystemConfig
from engine.diagnostics import Diagnostics
from engine.executor import ExecutorContext, NodeExecutor
from engine.gateway import Gateway
from engine.library import resolve
from engine.org import default_company
from engine.providers.base import (
    ChatResponse,
    FinishReason,
    ProviderCapabilities,
    ToolCall,
    Usage,
)
from engine.skills import FilesystemSkillSource
from engine.sysctl_tools import CAPABILITIES, CATALOGUE, SystemTools, consent_gate, grant_consent
from engine.tokens import TokenEstimator
from engine.tools import ToolRegistry

#: The capability whose grant this file drives. `system:state` is a read, so it needs no consent and
#: the positive case can be a real call against the machine rather than a refusal — which is the only
#: way to see a genuine `ToolResult` come back through the loop.
STATE_GRANT = "system:state"
#: The tool that reaches it, read off the catalogue rather than restated.
STATE_TOOL = next(entry.name for entry in CATALOGUE if entry.capability == STATE_GRANT)
#: A tool that asks the Owner first, and whose *value* check refuses before anything reaches the
#: machine. Used to prove the consent layer without changing the developer's volume.
CONSENT_TOOL = "set_volume"
CONSENT_GRANT = next(entry.capability for entry in CATALOGUE if entry.name == CONSENT_TOOL)


@pytest.fixture(scope="module")
def library():
    return resolve()


@pytest.fixture(scope="module")
def skills(library):
    return FilesystemSkillSource(library)


@pytest.fixture(scope="module")
def criteria(skills):
    """The skill's own completion criteria, so the fake trailer covers what the contract checks."""
    return list(skills.load("backend-developer").contract.criteria)


class SystemCallingModel:
    """A model that calls one tool, then answers with a trailer.

    Scripted as a callable rather than a `FakeProvider` script because the loop's *second* request is
    what carries the tool result, and a scripted-by-order provider cannot be asked what it was shown.
    This records every `tool_result` block it receives, which is where the real `ToolResult` text
    appears — the assertion this file exists for.
    """

    def __init__(self, tool: str, arguments: dict, trailer: dict) -> None:
        self.tool = tool
        self.arguments = arguments
        self.trailer = trailer
        #: Every tool-result block the model was shown, in order.
        self.seen_results: list[str] = []
        #: How many tools each request advertised, so "was this offered at all" is answerable.
        self.offered: list[list[str]] = []
        self.requests = 0

    def __call__(self, request) -> ChatResponse:
        for message in request.messages:
            for block in getattr(message, "content", []) or []:
                if getattr(block, "type", "") == "tool_result":
                    self.seen_results.append(block.text or "")
        self.offered.append([str(getattr(spec, "name", "")) for spec in request.tools])
        self.requests += 1
        usage = Usage(prompt_tokens=10, completion_tokens=5)
        if self.requests == 1:
            return ChatResponse(
                text="", tool_calls=[ToolCall(id="call-1", name=self.tool, arguments=self.arguments)],
                usage=usage, model="fake-model", provider_id="fake",
                finish_reason=FinishReason.TOOL_CALLS)
        return ChatResponse(text=json.dumps(self.trailer), usage=usage, model="fake-model",
                            provider_id="fake", finish_reason=FinishReason.STOP)


class ScriptedProvider:
    """The model behind the provider interface, so the gateway's budget and cost path is real."""

    kind = "fake"
    provider_id = "fake"

    def __init__(self, model: SystemCallingModel) -> None:
        self.model = model

    def complete(self, request) -> ChatResponse:
        return self.model(request)

    def stream(self, request):
        raise NotImplementedError("the proof drives `complete` only")

    def health(self) -> dict:
        return {"provider_id": "fake", "kind": "fake", "base_url": "in-process",
                "locality": "local", "has_key": True, "status": "ok", "model_count": 1}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_streaming=True,
                                    supports_json_mode=True, reports_usage=True,
                                    locality="local", source="probed")


def _config(*, enabled: bool = True, full_access: bool = False, **kw) -> Config:
    return Config(
        defaults={"provider": "fake", "model": "fake-model"},
        executor=ExecutorConfig(), budget=BudgetConfig(),
        system=SystemConfig(enabled=enabled, allow_full_access=full_access,
                            max_seconds=20, max_output_bytes=40_000, **kw),
    )


def drive(tmp_path, skills, criteria, *, capabilities, tool: str, arguments: dict | None = None,
          config: Config | None = None, grant: str = "", project_name: str = "project"):
    """Run one node whose agent calls one system tool, and return what happened.

    Everything here is the real path a live run takes: a seeded org, a binding by skill, a
    `tools: true` node, the agentic loop, and the registry the executor builds. The provider is the
    only substitution.
    """
    project = tmp_path / project_name
    project.mkdir(parents=True, exist_ok=True)
    org = default_company(provider="fake", model="fake-model", context_window=32768)
    alice = next(a for a in org.agents.values() if a.skills == ["backend-developer"])
    alice.capabilities = list(capabilities)
    alice.provider = "fake"
    alice.model = "fake-model"

    trailer = {
        "status": "done", "verdict": "ok", "summary": "reported the machine state",
        "criteria_satisfied": [{"criterion": c, "satisfied": True, "evidence": "the tool result"}
                               for c in criteria],
        "checklist": [{"id": "PC1", "status": "PASS", "evidence": "the tool result came back"}],
    }
    model = SystemCallingModel(tool, dict(arguments or {}), trailer)

    if grant:
        grant_consent(project / ".agent_state", tool=grant, agent_id=alice.id, by="a person")

    cfg = config or _config()
    gateway = Gateway(cfg, {"fake": ScriptedProvider(model)}, estimator=TokenEstimator())
    # A real `Diagnostics` bound to the workspace: `node.tools.start` / `node.tools.end` are the
    # engine's own record that a tool-using node ran and what it saw, and `activity.py` promotes
    # `node.tools.end` into the timeline a person reads. Reading the log back is how the trace half of
    # the claim is asserted, rather than only the model's view of it.
    diagnostics = Diagnostics(run_id="run_proof", state_dir=project / ".agent_state")
    ctx = ExecutorContext(
        org=org, gateway=gateway, skills=skills, workspace=project,
        store=ArtifactStore(workspace_root=project), config=cfg,
        run_id="run_proof", workflow="proof", diagnostics=diagnostics,
        manifest={"nodes": [{"id": "ops", "skill": "backend-developer",
                             "outputs": ["change"], "tools": True}]},
    )
    state = {"nodes": {}, "artifacts": {}, "budget": {"steps_used": 0}, "decisions": [],
             "open_questions": []}
    result = NodeExecutor(ctx).execute_node("ops", state, {"pass": 1})
    return {"result": result, "model": model, "agent": alice, "project": project, "config": cfg,
            "diagnostics": diagnostics}


def _org_with(capabilities: list[str]):
    """The seeded company with the developer's grants replaced, so one agent differs by one grant."""
    org = default_company(provider="fake", model="fake-model", context_window=32768)
    alice = next(a for a in org.agents.values() if a.skills == ["backend-developer"])
    alice.capabilities = list(capabilities)
    alice.provider = "fake"
    alice.model = "fake-model"
    return org


def the_registry_sees(project, org, config: Config) -> list[str]:
    """What the executor's own registry would advertise to this org's developer.

    Built through `ExecutorContext` and `NodeExecutor._tool_registry` rather than by calling the
    registry constructor with hand-picked arguments, because the failure being guarded against — a
    keyword missing at the executor's call site — is invisible in a test that constructs the object
    itself.
    """
    gateway = Gateway(config, {"fake": ScriptedProvider(SystemCallingModel("", {}, {}))},
                      estimator=TokenEstimator())
    ctx = ExecutorContext(org=org, gateway=gateway, skills=skills, workspace=project,
                          config=config, run_id="probe", workflow="probe")
    alice = next(a for a in org.agents.values() if a.skills == ["backend-developer"])
    return NodeExecutor(ctx)._tool_registry(alice, {"tools": True}).names()


# ── the positive half: the call reaches the machine and comes back ───────────


def test_an_agent_holding_the_grant_gets_a_real_result_back_through_the_loop(
        tmp_path, skills, criteria):
    """The load-bearing claim: an agent mid-node calls a system tool and the result is real.

    The assertion is on the text the *model* was shown, not on the function's return value. That is
    deliberate: a `ToolResult` a model never saw is not a working tool path, and reading it back out
    of the conversation is the only way to tell the two apart.
    """
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", "write:src/**", STATE_GRANT],
                tool=STATE_TOOL)
    assert len(run["model"].seen_results) == 1, "the loop must have fed exactly one tool result back"
    text = run["model"].seen_results[0]
    assert not text.startswith(f"{STATE_TOOL} refused"), text
    # The real command output, in the shape `system_state` assembles it. Values are not asserted —
    # this machine's charge is not a fact about the code — but the labels are, because they are what
    # separates a report from a refusal.
    for line in ("system state:", "  battery   :", "  disk      :", "  uptime    :", "  apps      :"):
        assert line in text, f"{line!r} is missing from the live report:\n{text}"


def test_the_system_tool_is_actually_offered_to_the_model(tmp_path, skills, criteria):
    """Offered *and* allowed. A tool hidden from the list produces "no such tool", which a model reads
    as a typo rather than as a permission it lacks — the worse refusal, in the one namespace where the
    model needs to know a grant exists."""
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", STATE_GRANT], tool=STATE_TOOL)
    first = run["model"].offered[0]
    assert STATE_TOOL in first, f"the model was offered {first}"


def test_the_run_records_the_tool_path_it_took(tmp_path, skills, criteria):
    """The engine's own trace must show the tool-using node and the tools it held.

    `node.tools.start` / `node.tools.end` are the two records the engine emits around a tool loop, and
    `activity.py` promotes the second into the timeline. A run that used the machine with no record of
    it would be exactly the audit gap the ask-once design exists to close.
    """
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", STATE_GRANT], tool=STATE_TOOL)
    records = run["diagnostics"].tail()
    events = {record["event"]: record for record in records}
    assert "node.tools.start" in events, sorted(events)
    assert STATE_TOOL in events["node.tools.start"]["detail"]["tools"]
    assert events["node.tools.end"]["detail"]["steps"] >= 1
    assert run["result"]["status"] == "done", run["result"].get("summary")


# ── negative control: the same agent without the grant ───────────────────────


def test_the_same_agent_without_the_grant_is_refused(tmp_path, skills, criteria):
    """A positive result alone cannot tell a working grant from a missing check.

    The agent here differs by exactly one capability, and the tool is still offered — the refusal is
    about the grant, not about the tool being absent.
    """
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", "write:src/**"], tool=STATE_TOOL)
    assert len(run["model"].seen_results) == 1
    text = run["model"].seen_results[0]
    assert text.startswith(f"{STATE_TOOL} refused"), text


def test_the_refusal_names_what_was_required_and_what_is_held(tmp_path, skills, criteria):
    """A refusal a model cannot act on is one it repeats until the step bound runs out."""
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", "write:src/**"], tool=STATE_TOOL)
    text = run["model"].seen_results[0]
    assert f"required: {STATE_GRANT}" in text
    assert "held    : read:*, write:src/**" in text
    assert "Do not retry this call" in text, "the shape `_denied` established"


def test_no_workspace_grant_reaches_the_machine(tmp_path, skills, criteria):
    """`read:*`, `write:*` and `exec:*` together still buy nothing: the machine is not the project."""
    run = drive(tmp_path, skills, criteria,
                capabilities=["read:*", "write:*", "exec:*", "admin:*"], tool=STATE_TOOL)
    text = run["model"].seen_results[0]
    assert text.startswith(f"{STATE_TOOL} refused"), text
    assert STATE_GRANT in text


# ── layer 1: `system.enabled` is the operator's switch ───────────────────────


def test_with_the_section_off_the_tool_is_not_offered_at_all(tmp_path, skills, criteria):
    """Registration, not refusal: an absent tool list is the honest statement of policy."""
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", STATE_GRANT], tool=STATE_TOOL,
                config=_config(enabled=False))
    offered = run["model"].offered[0]
    assert STATE_TOOL not in offered, f"{STATE_TOOL} was advertised with the section off: {offered}"
    text = run["model"].seen_results[0]
    assert f"no tool named {STATE_TOOL!r}" in text, text


def test_full_access_does_not_lift_the_section(tmp_path, skills, criteria):
    """`allow_full_access` widens *permission*; it is not a way to reach a machine the operator shut.

    The two are separate switches answering separate questions, and a mode that quietly turned the
    other one on would make "full access" mean more than the config documents.
    """
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", STATE_GRANT], tool=STATE_TOOL,
                config=_config(enabled=False, full_access=True))
    assert STATE_TOOL not in run["model"].offered[0]
    assert f"no tool named {STATE_TOOL!r}" in run["model"].seen_results[0]


# ── layer 2: the agent's own grant survives full access ──────────────────────


def test_full_access_does_not_confer_the_agent_grant(tmp_path, skills, criteria):
    """The single most important negative here: full access is not a capability.

    An operator handing the machine over has said "stop consulting me", not "give every agent every
    grant" — so an agent that holds nothing about the machine is still refused.
    """
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", "write:src/**"],
                tool=CONSENT_TOOL, arguments={"level": 200},
                config=_config(full_access=True))
    text = run["model"].seen_results[0]
    assert text.startswith(f"{CONSENT_TOOL} refused"), text
    assert f"required: {CONSENT_GRANT}" in text


# ── layer 3: full access lifts the ask-once consent ──────────────────────────


def test_full_access_lifts_the_consent_prompt(tmp_path, skills, criteria):
    """Proved *positively*: the call proceeds past the consent gate to the tool's own value check.

    A switch proved only by what it refuses is a switch that could be doing nothing. With consent
    outstanding, a scoped call is refused *by the gate*; with full access the same call reaches the
    value check, which refuses 200 for its own reason. The two refusals are different sentences, and
    that difference is the evidence.
    """
    scoped = drive(tmp_path, skills, criteria, capabilities=["read:*", CONSENT_GRANT],
                   tool=CONSENT_TOOL, arguments={"level": 200}, project_name="scoped")
    assert "no approval covers it" not in scoped["model"].seen_results[0]
    assert consent_gate(CONSENT_TOOL, scoped["agent"].id) in scoped["model"].seen_results[0], (
        "without full access the refusal must name the gate the Owner's approval would land at")

    wide = drive(tmp_path, skills, criteria, capabilities=["read:*", CONSENT_GRANT],
                 tool=CONSENT_TOOL, arguments={"level": 200}, config=_config(full_access=True),
                 project_name="wide")
    reached = wide["model"].seen_results[0]
    assert "no approval covers it" not in reached
    assert "level must be from 0 to 100" in reached, (
        "with full access the call must pass the consent gate and reach the tool's own check — this "
        f"is the refusals' difference; got:\n{reached}")


def test_full_access_leaves_the_state_changing_tool_gated_by_its_grant(tmp_path, skills, criteria):
    """The other half of the same layer: full access widens scope, never the capability gate."""
    run = drive(tmp_path, skills, criteria, capabilities=["read:*"], tool=CONSENT_TOOL,
                arguments={"level": 200}, config=_config(full_access=True))
    assert f"required: {CONSENT_GRANT}" in run["model"].seen_results[0]


# ── layer 4: consent is per tool, per agent, and lands in the ledger ─────────


def test_a_state_changing_call_without_consent_names_the_ledger(tmp_path, skills, criteria):
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", CONSENT_GRANT],
                tool=CONSENT_TOOL, arguments={"level": 200})
    text = run["model"].seen_results[0]
    assert consent_gate(CONSENT_TOOL, run["agent"].id) in text
    assert "ledger.jsonl" in text, "the refusal must say where the decision would be recorded"


def test_a_refused_call_records_the_request_in_the_ledger(tmp_path, skills, criteria):
    """So "what is my agent asking for" is answerable from the ledger rather than from a log line."""
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", CONSENT_GRANT],
                tool=CONSENT_TOOL, arguments={"level": 200})
    ledger = (run["project"] / ".agent_state" / "ledger.jsonl").read_text(encoding="utf-8")
    assert f"system-request:{CONSENT_TOOL}:{run['agent'].id}" in ledger
    assert "requested" in ledger


def test_a_standing_approval_moves_the_call_past_the_gate(tmp_path, skills, criteria):
    """The approval is read through the tool path, so an approval written to the wrong place would
    show up here as the consent refusal coming back."""
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", CONSENT_GRANT],
                tool=CONSENT_TOOL, arguments={"level": 200}, grant=CONSENT_TOOL)
    text = run["model"].seen_results[0]
    assert "no approval covers it" not in text
    assert "level must be from 0 to 100" in text, text


def test_an_approval_for_the_agent_does_not_cover_a_different_holder(tmp_path, skills, criteria):
    """Per holder: approving the volume change for Alice is not approving it for anyone else.

    Asserted through the tool layer's own gate, because the failure this guards against — an approval
    that looks effective and is not — is indistinguishable from the outside.
    """
    run = drive(tmp_path, skills, criteria, capabilities=["read:*", CONSENT_GRANT],
                tool=CONSENT_TOOL, arguments={"level": 200}, grant=CONSENT_TOOL)
    state_dir = run["project"] / ".agent_state"
    other = SystemTools(workspace_root=run["project"], config=run["config"].system,
                        agent_id="ag_somebody_else", state_dir=state_dir)
    assert other._has_consent(CONSENT_TOOL) is False
    holder = SystemTools(workspace_root=run["project"], config=run["config"].system,
                         agent_id=run["agent"].id, state_dir=state_dir)
    assert holder._has_consent(CONSENT_TOOL) is True


def test_an_approval_cannot_be_attributed_to_an_agent(tmp_path, skills, criteria):
    """The guard that makes "an approval must come from a person" a property of the code.

    `grant_consent` refuses a `by` naming an agent, and this file asserts it from the run's own holder
    rather than trusting the guard's existence: an agent that can approve its own destructive call has
    not been gated at all.
    """
    from engine.sysctl_tools import ConsentError

    run = drive(tmp_path, skills, criteria, capabilities=["read:*", CONSENT_GRANT],
                tool=CONSENT_TOOL, arguments={"level": 200})
    holder = run["agent"].id
    assert holder.startswith("ag_"), "the roster's ids are the `ag_` shape the guard keys on"
    with pytest.raises(ConsentError, match="names an agent"):
        grant_consent(run["project"] / ".agent_state", tool=CONSENT_TOOL, agent_id=holder,
                      by=holder)


# ── the registry the executor builds, without a run ─────────────────────────


def test_the_executor_registry_advertises_exactly_the_catalogue(tmp_path, skills, criteria):
    """The other direction of the same claim: it is not only this file's node that sees them.

    `_tool_registry` is the one place every node's registry is built, so a missing `system=` there
    would make every system tool inert in every run. Derived from the catalogue, never a hand-written
    list — a second copy is the drift this assertion exists to catch.
    """
    project = tmp_path / "probe"
    project.mkdir(parents=True, exist_ok=True)
    names = the_registry_sees(project, _org_with([STATE_GRANT]), _config())
    for entry in CATALOGUE:
        assert entry.name in names, f"{entry.name} is not advertised by the executor's own registry"


def test_the_capabilities_the_registry_gates_on_are_the_modules_own():
    """The vocabulary check, in one line, against the constant rather than a copy of it."""
    from engine.tools import ToolRegistry as _Registry

    assert set(_Registry.SYSTEM_TOOL_CAPABILITY.values()) <= set(CAPABILITIES)
    assert {entry.capability for entry in CATALOGUE} == set(CAPABILITIES)
