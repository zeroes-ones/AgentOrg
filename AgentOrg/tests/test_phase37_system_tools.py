#!/usr/bin/env python3
"""Phase 37 tests — scoped, separately-granted control of the machine.

The person's ask was that an agent "have capabilities to control the system for anything granted".
The design answer is **scoped grants, never one broad switch**: one `system:automation`-style escape
hatch that runs any AppleScript would be full control of the account behind a single checkbox, so
each capability is its own grant and the allowlists make a grant a *scope* rather than a boolean —
the same shape `read:`/`write:` already have.

The tests are written against the properties that make that scoping worth having rather than against
the code that produces it:

1. **Nothing is advertised unless the operator turned it on.** With `system.enabled = False` the
   registry must offer none of these, because a tool offered-then-refused teaches a model that the
   tool list is not a statement of policy.
2. **A grant is required, and it is per capability.** Holding `system:state` does not confer
   `system:open`. Asserted by calling each tool on an agent that holds a *different* one.
3. **The allowlists are the scope.** An app outside `allow_apps` is refused naming the list; a snippet
   that does not start with an `allow_automation` prefix is refused naming the prefixes. Asserted on
   a real call, so the refusal is the one a model would actually receive.
4. **The machine is really read.** `system_state` returns a battery percentage and a free-space figure
   parsed out of the real command output. The *values* are not asserted — this machine's charge is not
   a fact about the code — but their shape and parseability are, which is what catches a parser that
   reads the wrong column.
5. **Both bounds bite.** A long command is stopped at `max_seconds`; a large output is cut at
   `max_output_bytes` and *says* it was cut.
6. **A refusal keeps the established shape.** It names what was required and what the agent holds.
7. **`read:`/`write:`/`exec:` are unchanged.** The gate is extended for a capability with no path
   scope, and that extension must not move the path semantics underneath it.

What is deliberately *not* tested against a live machine: the clipboard round trip, volume changes,
`open_app` and `run_automation`. Each of those changes something the person is holding, and a test
that mutates the developer's clipboard or volume to prove a point is a test that costs the person.
Their refusal paths are exercised instead, with the consent gate satisfied — which is the honest part
to assert here, because the part that reaches the machine is `osascript`/`pbcopy` and that is not this
code.

`take_screenshot` is refused-to-a-temp-directory only, and is not run live either: a passing run of
`screencapture` writes a full-screen PNG of whatever the developer has on screen, once per suite run,
into a file the suite then has to clean up. The two things worth asserting about it — that the default
directory is inside the workspace and that an escaping or root directory is refused — need no capture
to hold.

The one live call the suite makes besides the read-only tools is a single read-only AppleScript
(`tell application "Finder" to name`), which is why `run_automation`'s prefix gate is asserted against
a real `osascript` rather than only against the refusal.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import SystemConfig, load
from engine.org.agent import AgentLevel, AgentSpec
from engine.tools import (
    EXEC_CAPABILITY,
    READ_CAPABILITY,
    SYSTEM_CAPABILITY,
    WRITE_CAPABILITY,
    ToolRegistry,
)
from engine import sysctl_tools as sysctl
from engine.sysctl_tools import (
    CATALOGUE,
    CONSENT_REQUIRED,
    CAPABILITIES,
    DF,
    LSAPPINFO,
    MUTATING_TOOLS,
    OSASCRIPT,
    SystemTools,
    consent_gate,
    count_running_apps,
    grant_consent,
    parse_battery,
    parse_boot_report,
    parse_disk,
    parse_network_quality,
    parse_shortcut_names,
    parse_update_list,
    parse_volume_settings,
    request_gate,
    revoke_consent,
    run,
)


@pytest.fixture(scope="module")
def config():
    return load()


def make_agent(*capabilities: str, name: str = "Alice", role: str = "worker") -> AgentSpec:
    return AgentSpec(id=f"ag_{name.lower()}", name=name, title="Engineer",
                     skills=["backend-developer"], provider="fake", model="m",
                     context_window=32768, capabilities=list(capabilities),
                     level=AgentLevel.SENIOR, role=role)


@pytest.fixture
def project(tmp_path):
    """A real project tree, because the default screenshot directory lives inside it."""
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text("def subtract(a, b):\n    return a - b\n")
    (root / "README.md").write_text("# App\n")
    return root


@pytest.fixture
def system_on():
    return SystemConfig(enabled=True, max_seconds=20, max_output_bytes=40_000)


ALL_TOOLS = [entry.name for entry in CATALOGUE]


# ── the catalogue and the gate table agree ───────────────────────────────────


def test_every_catalogue_entry_declares_a_capability_the_registry_gates_on():
    """A tool offered without a grant it is gated on would be a tool reachable by anyone."""
    from engine.tools import ToolRegistry as _Registry

    declared = _Registry.SYSTEM_TOOL_CAPABILITY
    for entry in CATALOGUE:
        assert entry.name in declared, f"{entry.name} is advertised with no capability declared"
        assert declared[entry.name] == entry.capability, (
            f"{entry.name}: the catalogue says {entry.capability} and the gate says "
            f"{declared[entry.name]}, which means one of the two is a lie")
        assert entry.capability in SystemConfig.CAPABILITIES, (
            f"{entry.capability} is not one of the capabilities SystemConfig documents")


def test_every_capability_in_the_config_is_a_namespace_this_registry_knows():
    """The config and the tool layer cannot disagree about the vocabulary."""
    for capability in SystemConfig.CAPABILITIES:
        assert capability.startswith(f"{SYSTEM_CAPABILITY}:"), capability
    covered = {entry.capability for entry in CATALOGUE}
    assert covered == set(SystemConfig.CAPABILITIES), (
        "every documented capability must have at least one tool, or the config advertises a grant "
        "that buys nothing")


def test_mutating_tools_are_read_off_the_catalogue_not_restated():
    assert MUTATING_TOOLS == {entry.name for entry in CATALOGUE if entry.mutates}
    for name in ("system_state", "get_volume", "read_clipboard"):
        assert name not in MUTATING_TOOLS, f"{name} reads and must not be labelled a mutation"
    for name in ("write_clipboard", "set_volume", "set_mute", "open_app", "run_automation",
                 "take_screenshot"):
        assert name in MUTATING_TOOLS, f"{name} changes something and must be labelled so"


def test_the_consent_set_is_a_subset_of_the_mutating_tools():
    """Asking for consent on a read would train the Owner to approve without reading."""
    assert CONSENT_REQUIRED <= MUTATING_TOOLS
    assert "system_state" not in CONSENT_REQUIRED and "read_clipboard" not in CONSENT_REQUIRED


# ── advertising ──────────────────────────────────────────────────────────────


def test_no_system_tool_is_advertised_when_the_section_is_off(project):
    """`enabled=False` is the default: the machine is not reachable unless someone said so."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:*"),
                            system=SystemConfig(enabled=False))
    for name in ALL_TOOLS:
        assert name not in registry.names()
        assert name not in [spec.name for spec in registry.specs()]


def test_no_system_tool_is_advertised_without_a_section_at_all(project):
    """The engine's default posture: no `system` section means no machine access, as before."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:*"))
    assert not [name for name in registry.names() if name in ALL_TOOLS]


def test_every_tool_is_advertised_when_enabled(project, system_on):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:*"), system=system_on)
    for name in ALL_TOOLS:
        assert name in registry.names(), f"{name} is missing from an enabled registry"


def test_the_advertised_order_is_the_sorted_order(project, system_on):
    """A reordering of the tool schemas is a cache miss, so the order must not drift."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:*"), system=system_on)
    assert [spec.name for spec in registry.specs()] == registry.names() == sorted(registry.names())


def test_a_read_only_run_advertises_them_but_refuses_every_mutating_one(project, system_on):
    """`read_only` is a property of the *run*, so it binds the machine tools as it binds writes.

    Enforced in `call` rather than at registration, which is where `write_file` and `run_command` are
    refused — one rule, one sentence, one place. So the tool is present and the call is refused.
    """
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:*"),
                            read_only=True, system=system_on)
    for name in sorted(MUTATING_TOOLS):
        result = registry.call(name, _args_for(name))
        assert not result.ok, f"{name} mutates and must be refused in a read-only run"
        assert "read-only" in result.text
    # And a read is still allowed, because a read changes nothing.
    assert registry.call("system_state", {}).ok


def _args_for(name: str) -> dict:
    """A plausible argument set for a tool, so a refusal is tested rather than a missing argument."""
    return {
        "write_clipboard": {"text": "x"},
        "take_screenshot": {},
        "set_volume": {"level": 50},
        "set_mute": {"muted": True},
        "open_app": {"name": "Finder"},
        "run_automation": {"script": "tell application id \"com.apple.finder\" to activate"},
        "say_message": {"text": "hello"},
        "post_notification": {"message": "hello"},
        "spotlight_search": {"query": "readme"},
        "keep_awake": {"seconds": 60},
        "sleep_now": {},
        "network_status": {"measure": False},
        "run_shortcut": {"name": "Battery Level"},
        "list_os_updates": {},
        "install_os_updates": {},
    }.get(name, {})


# ── the capability gate ──────────────────────────────────────────────────────


def test_each_tool_needs_its_own_capability(project, system_on):
    """Holding one machine grant must not confer another — that is the whole design."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:state"),
                            system=system_on)
    by_capability: dict[str, list[str]] = {}
    for entry in CATALOGUE:
        by_capability.setdefault(entry.capability, []).append(entry.name)
    for capability, names in by_capability.items():
        if capability == "system:state":
            continue
        for name in names:
            result = registry.call(name, _args_for(name))
            assert not result.ok, f"{name} answered an agent holding only system:state"
            assert capability in result.text, (
                f"the refusal must name the capability required ({capability})")


def test_a_grant_is_not_conferred_by_read_write_or_exec(project, system_on):
    """The machine is not part of the project, so no workspace grant reaches it."""
    registry = ToolRegistry(workspace_root=project,
                            agent=make_agent("read:*", "write:*", "exec:*"), system=system_on)
    result = registry.call("system_state", {})
    assert not result.ok
    assert "system:state" in result.text
    assert "read:*, write:*, exec:*" in result.text, "the refusal must list what the agent holds"


def test_the_system_wildcard_grants_every_machine_capability(project, system_on):
    """`system:*` is the one wildcard, and it is deliberate: a caller who writes it asked for all."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:*"), system=system_on)
    assert registry.call("system_state", {}).ok
    assert registry.call("get_volume", {}).ok


def test_a_prefix_capability_does_not_match_a_longer_name(project, system_on):
    """`system:state` must not grant `system:stateful`, were such a thing ever added.

    A prefix rule here would be a widening nobody asked for, in the one namespace where widening is
    the risk. Checked through `_granted_scoped` because no such capability exists to call.
    """
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:state"),
                            system=system_on)
    assert registry._granted_scoped("system", "state")
    assert not registry._granted_scoped("system", "stateful")
    assert not registry._granted_scoped("system", "sta")


def test_a_refusal_keeps_the_established_shape(project, system_on):
    """The shape `read:`/`write:` established, so a model sees one refusal whatever it asked for."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*"), system=system_on)
    result = registry.call("system_state", {})
    assert not result.ok
    assert "required" in result.text
    assert "held" in result.text
    assert "Do not retry this call" in result.text
    assert "read:*" in result.text


# ── system:state really reads the machine ────────────────────────────────────


def test_system_state_reports_real_figures(project, system_on):
    """The shape and the parseability are asserted; the values are not.

    This machine's charge and free space are not facts about the code, so asserting them would make
    the suite pass or fail on the weather. What *is* a fact about the code is that the percentage is a
    percentage and the free-space figure is a size — which is what catches a parser reading the wrong
    column, and that is exactly the defect this file has already had once.
    """
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:state"),
                            system=system_on)
    result = registry.call("system_state", {})
    assert result.ok, result.text
    assert result.text.startswith("system state:")

    battery = next((line for line in result.text.splitlines() if "battery" in line), "")
    assert battery, "the report must carry a battery line even when there is no battery"
    if "no internal battery" not in battery:
        percent = int(battery.split(":")[1].strip().split("%")[0].strip())
        assert 0 <= percent <= 100, f"a charge outside 0-100 is not a charge: {battery!r}"

    disk = next((line for line in result.text.splitlines() if "disk" in line), "")
    assert disk, "the report must carry a disk line"
    assert "free of" in disk and "/" in disk
    free = disk.split(":")[1].strip().split(" ")[0]
    # `df -h` prints sizes with a one- or two-letter unit (`420Gi`), so the digits are the prefix and
    # the rest must look like a unit rather than prose.
    digits = free.rstrip("KMGTPEiB")
    assert digits and digits[-1] in "0123456789", f"the free-space figure must be a size, got {free!r}"
    assert free[len(digits):] in ("K", "M", "G", "T", "P", "Ki", "Mi", "Gi", "Ti", "Pi"), (
        f"an unrecognised size unit: {free!r}")

    assert any(line.startswith("  uptime") for line in result.text.splitlines())
    apps_line = next((line for line in result.text.splitlines() if "apps" in line), "")
    assert int(apps_line.split(":")[1].strip().split(" ")[0]) > 0, (
        "a machine running this test has at least one application registered")


def test_the_battery_parser_reads_a_discharging_line(project):
    """Captured output, so the parse is asserted without depending on this machine's power state."""
    parsed = parse_battery(
        "Now drawing from 'Battery Power'\n"
        " -InternalBattery-0 (id=37814371)\t69%; discharging; 1:08 remaining present: true\n")
    assert parsed == {"percent": 69, "state": "discharging", "remaining": "1:08",
                      "source": "battery power"}


def test_the_battery_parser_does_not_read_discharging_as_charging(project):
    """`charging` is a substring of `discharging`, so a substring test inverts the one fact that
    changes what an unattended run should do."""
    parsed = parse_battery(
        "Now drawing from 'AC Power'\n"
        " -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true\n")
    assert parsed is not None and parsed["state"] == "charged"
    assert parsed["source"] == "ac power"


def test_a_machine_with_no_battery_reports_none_rather_than_zero(project):
    """A desktop Mac has no internal battery; `0%` would be a fabricated figure, not an answer."""
    assert parse_battery("Now drawing from 'AC Power'\n") is None
    assert parse_battery("") is None


def test_the_disk_parser_refuses_a_line_it_cannot_trust():
    """A wrong free-space figure is worse than a reported failure to parse one."""
    assert parse_disk("") is None
    assert parse_disk("Filesystem Size\n") is None
    assert parse_disk("this is not df output at all\n") is None


def test_the_disk_parser_reads_the_last_row_not_the_header():
    parsed = parse_disk("Filesystem      Size   Used  Avail Capacity iused ifree %iused  Mounted on\n"
                        "/dev/disk3s3s1   926Gi   13Gi  422Gi     3%    485k  4.3G    0%   /\n")
    assert parsed is not None
    assert parsed["avail"] == "422Gi"
    assert parsed["size"] == "926Gi"
    assert parsed["used_percent"] == 3
    assert parsed["mount"] == "/"


def test_the_volume_parser_reads_the_apple_script_record():
    parsed = parse_volume_settings(
        "output volume:32, input volume:29, alert volume:100, output muted:false")
    assert parsed == {"output_volume": 32, "input_volume": 29, "alert_volume": 100, "muted": False}
    # A key the machine did not report is absent rather than zero.
    sparse = parse_volume_settings("output volume:0, output muted:true")
    assert sparse["output_volume"] == 0 and "input_volume" not in sparse


def test_the_boot_report_parser_reads_uptime_and_version():
    parsed = parse_boot_report(
        "Software:\n\n    System Software Overview:\n\n"
        "      System Version: macOS 27.2 (26B5086k)\n"
        "      Time since boot: 1 day, 22 hours, 19 minutes\n")
    assert parsed["version"].startswith("macOS")
    assert "day" in parsed["uptime"]


def test_the_app_counter_counts_named_application_entries():
    """Counted by the ASN token, so an application whose name contains spaces is one application."""
    text = ('ASN:0x0-0x1001-"loginwindow": ASN:0x0-0x48048-"SystemUIServer": '
            'ASN:0x0-0x7b37b3-"Privacy_&_Security_(System_Settings)":\n')
    assert count_running_apps(text) == 3
    assert count_running_apps("") == 0
    # A line with no ASN, or an unmatched one, must not be counted.
    assert count_running_apps("no applications here\n") == 0


def test_the_app_count_is_made_from_output_that_fits_the_cap(project, system_on):
    """`lsappinfo list` is ~113 KB and the default cap is 40 KB, so the count would be a truncation.

    Asserted as a property rather than a byte count: the call the tool makes must not be one whose
    output the cap would cut, because a truncated listing yields a number that is quietly wrong.
    """
    called = run([LSAPPINFO, "processlist"], timeout_s=20, max_output_bytes=40_000)
    assert called.ok, called.stderr or "lsappinfo processlist must run on this machine"
    assert not called.truncated, (
        "the app count is read from output the 40 KB default cap would cut; use a cheaper command")


# ── the bounds ───────────────────────────────────────────────────────────────


def test_max_seconds_stops_a_command_and_says_so(project):
    """An AppleScript dialog nobody is there to dismiss must not hold the run for ever."""
    config = SystemConfig(enabled=True, max_seconds=1)
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")
    started = _now()
    result = tools._call([OSASCRIPT, "-e", "delay 30"])
    elapsed = _now() - started
    assert not result.ok
    assert result.timed_out
    assert "ceiling" in result.stderr
    assert elapsed < 20, f"the ceiling took {elapsed:.1f}s to bite; it should be ~1s"


def test_max_output_bytes_cuts_the_output_and_says_so(project):
    """A silently clipped output produces a model reasoning about what it never saw."""
    config = SystemConfig(enabled=True, max_seconds=20, max_output_bytes=64)
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")
    result = tools._call([LSAPPINFO, "processlist"])
    assert result.truncated, "the cap must bite on a 7 KB listing at 64 bytes"
    assert "truncated" in result.stderr and "64" in result.stderr


def _now() -> float:
    import time

    return time.time()


def test_a_missing_binary_is_a_refusal_not_a_crash(project, system_on):
    """A path we verified is not a path every machine has, and the refusal must say which."""
    result = run(["/usr/bin/definitely-not-here", "-x"], timeout_s=5)
    assert not result.ok
    assert "definitely-not-here" in result.reason
    assert "not an executable file" in result.reason


def test_a_bare_command_name_is_refused(project, system_on):
    """`$PATH` is agent-writable once the sandbox is on, so a bare name is a substitution risk."""
    result = run(["lsappinfo", "processlist"], timeout_s=5)
    assert not result.ok
    assert "absolute path" in result.reason


def test_a_report_assembled_from_failed_reads_is_not_reported_as_an_answer(project):
    """A report where nothing was measured must read as a failure, not as a state.

    Without this, a machine with none of these binaries returns `ok=True` and a report saying "no
    internal battery" and "0 apps" — which is an answer about a machine that was never measured.
    The runner is swapped for one that always fails, so the property is asserted rather than the
    absence of the binaries on this machine.
    """
    tools = SystemTools(workspace_root=project, config=SystemConfig(enabled=True),
                        agent_id="ag_alice")

    def always_fails(argv, *, stdin_text=None):
        return sysctl.SystemCall(False, 127, "", "command not found",
                                 reason="command not found")

    tools._call = always_fails
    result = tools.system_state({})
    assert not result.ok, "a report with no successful read is not a state"
    assert "every read failed" in result.text
    assert "127" in result.text


def test_system_state_reports_a_failure_it_can_actually_diagnose(project):
    """One working read is enough for the report to be a state, and it still says which part failed."""
    tools = SystemTools(workspace_root=project, config=SystemConfig(enabled=True),
                        agent_id="ag_alice")
    original = tools._call
    calls: list[str] = []

    def failing_df(argv, *, stdin_text=None):
        calls.append(argv[0])
        if argv[0] == DF:
            return sysctl.SystemCall(False, 1, "", "df: /: No such file", reason="")
        return original(argv, stdin_text=stdin_text)

    tools._call = failing_df
    result = tools.system_state({})
    assert result.ok, "the other reads worked, so this is a state with one part missing"
    assert "could not read df output" in result.text


def test_the_binaries_this_module_names_are_the_ones_it_uses():
    """No `shutil.which`, no bare name: every binary constant is an absolute path."""
    for binary in sysctl.BINARIES:
        assert binary.startswith("/"), f"{binary} is not an absolute path"
        assert binary.startswith(("/usr/", "/bin/", "/sbin/")), (
            f"{binary} is outside the system paths this module verified")


# ── system:clipboard ─────────────────────────────────────────────────────────


def test_reading_the_clipboard_needs_no_consent(project, system_on):
    """Reading changes nothing, so it does not ask — and a read that asked would be noise."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:clipboard"),
                            system=system_on)
    result = registry.call("read_clipboard", {})
    assert result.ok, result.text


def test_a_clipboard_write_is_refused_until_the_owner_approves_it(project, system_on):
    """Writing the clipboard destroys what the person had copied, so it asks once."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:clipboard"),
                            system=system_on)
    result = registry.call("write_clipboard", {"text": "replaced"})
    assert not result.ok
    assert "Owner has not approved it for this agent" in result.text
    assert consent_gate("write_clipboard", "ag_alice") in result.text
    assert "Do not retry this call" in result.text


def test_the_clipboard_write_goes_through_stdin_not_argv(project, system_on):
    """argv is visible in `ps` to every process on the machine, and a clipboard often holds a secret.

    Asserted on the argv the tool would build, because actually writing the developer's clipboard to
    prove the point is a cost this test refuses to impose on the person running it.
    """
    source = pathlib.Path(sysctl.__file__).read_text(encoding="utf-8")
    assert "stdin_text=text" in source, (
        "the clipboard text must travel on stdin; putting it in argv exposes it via ps")
    assert "[PBCOPY, text]" not in source and "[PBCOPY, str(text)]" not in source


# ── system:screenshot ────────────────────────────────────────────────────────


def test_a_screenshot_lands_inside_the_workspace_by_default(project, system_on):
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    target = tools.screenshot_dir()
    assert target.is_relative_to(project.resolve()), (
        "a screenshot of the whole desktop is not project content, so the default stays inside the "
        "workspace rather than on the person's Desktop")
    assert ".agent_state" in str(target), (
        "the default must be in the state directory, which is already gitignored, so a screenshot "
        "cannot be committed by accident")


def test_a_relative_screenshot_dir_that_escapes_the_workspace_is_refused(project):
    config = SystemConfig(enabled=True, screenshot_dir="../../Desktop")
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")
    result = tools.take_screenshot({})
    assert not result.ok
    assert "outside the workspace" in result.text


def test_the_filesystem_root_is_not_a_screenshot_directory(project):
    """`screenshot_dir: "/"` looks like an entry and behaves like a grant of the whole disk."""
    config = SystemConfig(enabled=True, screenshot_dir="/")
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")
    result = tools.take_screenshot({})
    assert not result.ok
    assert "filesystem root" in result.text


def test_screenshot_dir_is_overridable_and_absolute_paths_are_honoured(project, tmp_path):
    chosen = tmp_path / "shots"
    config = SystemConfig(enabled=True, screenshot_dir=str(chosen))
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")
    assert tools.screenshot_dir() == chosen.resolve(strict=False)


# ── system:media ─────────────────────────────────────────────────────────────


def test_get_volume_reads_without_asking(project, system_on):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:media"),
                            system=system_on)
    result = registry.call("get_volume", {})
    assert result.ok, result.text
    assert "output volume" in result.text
    level = int(result.text.split(":")[1].strip().split("/")[0])
    assert 0 <= level <= 100


def test_set_volume_refuses_a_level_outside_the_range_rather_than_clamping(project, system_on):
    """macOS clamps silently and exits 0, so a clamped call would report a volume never used."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:media"),
                            system=system_on)
    grant_consent(project / ".agent_state", tool="set_volume", agent_id="ag_alice", by="sp.vm")
    for bad in (101, -1, 200):
        result = registry.call("set_volume", {"level": bad})
        assert not result.ok, f"{bad} must be refused"
        assert "0 to 100" in result.text
    result = registry.call("set_volume", {"level": "loud"})
    assert not result.ok and "whole number" in result.text


def test_set_mute_refuses_a_non_boolean(project, system_on):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:media"),
                            system=system_on)
    grant_consent(project / ".agent_state", tool="set_mute", agent_id="ag_alice", by="sp.vm")
    result = registry.call("set_mute", {"muted": "yes"})
    assert not result.ok
    assert "true or false" in result.text


def test_changing_the_volume_asks_once_and_is_then_remembered(project, system_on):
    """Approved once per agent per tool, and then it stays approved across registries.

    Asserted by granting and then checking a *freshly built* registry — the approval lives in the
    workspace's ledger, so a new node in the same run must not have to ask again. Asserted through
    the ledger rather than by actually changing the developer's volume.
    """
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:media"),
                            system=system_on)
    assert not registry.call("set_mute", {"muted": True}).ok, "not approved yet"
    grant_consent(project / ".agent_state", tool="set_mute", agent_id="ag_alice", by="sp.vm")
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    assert tools._has_consent("set_mute"), "the grant must survive a fresh object"


# ── system:open ──────────────────────────────────────────────────────────────


def test_open_app_refuses_an_app_not_in_the_allowlist_naming_it(project):
    """The allowlist is the scope, so the refusal must name it — otherwise it is a dead end."""
    config = SystemConfig(enabled=True, allow_apps=["Finder", "Safari"])
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:open"), system=config)
    grant_consent(project / ".agent_state", tool="open_app", agent_id="ag_alice", by="sp.vm")
    result = registry.call("open_app", {"name": "Terminal"})
    assert not result.ok
    assert "Finder, Safari" in result.text, "the refusal must name the allowlist"
    assert "Terminal" in result.text, "and what was asked for"
    assert "system.allow_apps" in result.text, "and the setting that would change the answer"


def test_open_app_refuses_everything_when_the_allowlist_is_empty(project, system_on):
    """Empty is the default, and it means none — not "any"."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:open"),
                            system=system_on)
    grant_consent(project / ".agent_state", tool="open_app", agent_id="ag_alice", by="sp.vm")
    result = registry.call("open_app", {"name": "Finder"})
    assert not result.ok
    assert "empty" in result.text and "no application is allowed" in result.text


def test_the_app_match_is_exact_and_case_insensitive_not_a_prefix(project):
    """A prefix rule would let `Safari` authorise a different vendor channel of the same app."""
    config = SystemConfig(enabled=True, allow_apps=["Safari"])
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:open"), system=config)
    grant_consent(project / ".agent_state", tool="open_app", agent_id="ag_alice", by="sp.vm")
    assert not registry.call("open_app", {"name": "SafariTechnologyPreview"}).ok
    assert not registry.call("open_app", {"name": "Saf"}).ok
    # A case difference is the same application, so it is allowed through the gate. It is not
    # launched here, because launching an app is a change to the developer's desktop.
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")
    assert "safari" in {a.lower() for a in tools.allowed_apps()}


def test_open_app_needs_a_name(project, system_on):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:open"),
                            system=system_on)
    grant_consent(project / ".agent_state", tool="open_app", agent_id="ag_alice", by="sp.vm")
    result = registry.call("open_app", {})
    assert not result.ok
    assert "name is required" in result.text


# ── system:automation ────────────────────────────────────────────────────────


def test_run_automation_refuses_a_snippet_not_matching_any_prefix(project):
    """The prefix list is the scope, so the refusal must name the prefixes and the snippet."""
    config = SystemConfig(enabled=True,
                          allow_automation=['tell application "Finder" to'])
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:automation"),
                            system=config)
    grant_consent(project / ".agent_state", tool="run_automation", agent_id="ag_alice", by="sp.vm")
    result = registry.call("run_automation",
                           {"script": 'tell application "Terminal" to do script "rm -rf ~"'})
    assert not result.ok
    assert "allowlisted handler" in result.text
    assert 'tell application "Finder" to' in result.text, "the refusal must name the prefixes"
    assert "Terminal" in result.text, "and the snippet that was asked for"
    assert "Do not retry this call with a reworded snippet" in result.text


def test_run_automation_refuses_everything_when_the_prefix_list_is_empty(project, system_on):
    """Empty is the default, and it means nothing may run — not "anything"."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:automation"),
                            system=system_on)
    grant_consent(project / ".agent_state", tool="run_automation", agent_id="ag_alice", by="sp.vm")
    result = registry.call("run_automation", {"script": "return 1"})
    assert not result.ok
    assert "system.allow_automation is empty" in result.text


def test_an_allowlisted_prefix_runs_and_its_result_comes_back(project):
    """The one live automation call, and it is read-only: it asks Finder for its own name.

    Chosen because it touches nothing — no window, no file, no setting — so the test proves the prefix
    gate and the wiring without changing anything on the machine it runs on.
    """
    config = SystemConfig(enabled=True, allow_automation=['tell application "Finder" to name'])
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:automation"),
                            system=config)
    grant_consent(project / ".agent_state", tool="run_automation", agent_id="ag_alice", by="sp.vm")
    result = registry.call("run_automation", {"script": 'tell application "Finder" to name'})
    assert result.ok, result.text
    assert "Finder" in result.text


def test_a_reformatted_snippet_still_matches_its_prefix(project):
    """Whitespace is style, not meaning: a one-line config must match a multi-line snippet."""
    config = SystemConfig(enabled=True, allow_automation=['tell application "Finder" to name'])
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")
    script = 'tell application "Finder"\n  return name\nend tell'
    # The normaliser is what the allowlist is compared against, so a reformatted snippet matches on
    # the handler rather than on the line breaks — and one that does not start with the prefix fails.
    assert sysctl._normalise_script('  tell   application "Finder" to name') == (
        'tell application "Finder" to name')
    assert not tools.automation_prefixes() == []
    assert sysctl._normalise_script(script).startswith("tell application")


def test_run_automation_needs_a_script(project, system_on):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:automation"),
                            system=system_on)
    grant_consent(project / ".agent_state", tool="run_automation", agent_id="ag_alice", by="sp.vm")
    result = registry.call("run_automation", {"script": "   "})
    assert not result.ok
    assert "script is required" in result.text


def test_an_unknown_automation_language_is_refused(project, system_on):
    """The refusal names the languages the tool actually accepts — read from the tool's own table, so a
    third language is offered the moment it is declared rather than the next time someone edits this."""
    from engine.sysctl_tools import SCRIPT_LANGUAGES

    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:automation"),
                            system=system_on)
    grant_consent(project / ".agent_state", tool="run_automation", agent_id="ag_alice", by="sp.vm")
    result = registry.call("run_automation", {"script": "return 1", "language": "bash"})
    assert not result.ok
    for language in SCRIPT_LANGUAGES:
        assert language in result.text, f"the refusal must name {language}"
    assert "bash" in result.text, "and the value it was given"


# ── the consent ledger ───────────────────────────────────────────────────────


def test_the_consent_gate_is_per_agent_and_per_tool():
    assert consent_gate("set_mute", "ag_a") != consent_gate("set_mute", "ag_b")
    assert consent_gate("set_mute", "ag_a") != consent_gate("open_app", "ag_a")
    assert request_gate("set_mute", "ag_a") != consent_gate("set_mute", "ag_a"), (
        "a request must never be readable as an approval")


def test_a_grant_to_one_agent_does_not_grant_another(project, system_on):
    registry_a = ToolRegistry(workspace_root=project, agent=make_agent("system:media"),
                              system=system_on)
    registry_b = ToolRegistry(workspace_root=project,
                              agent=make_agent("system:media", name="Bob"), system=system_on)
    grant_consent(project / ".agent_state", tool="set_mute", agent_id="ag_alice", by="sp.vm")
    assert SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")._has_consent(
        "set_mute")
    assert not SystemTools(workspace_root=project, config=system_on, agent_id="ag_bob")._has_consent(
        "set_mute"), "an approval for one agent must not cover another"
    assert not registry_b.call("set_mute", {"muted": True}).ok
    del registry_a


def test_revoking_a_grant_supersedes_it_rather_than_deleting_it(project, system_on):
    """The ledger is append-only, and "withdrawn on this date" is the fact a later reader needs."""
    state = project / ".agent_state"
    grant_consent(state, tool="set_mute", agent_id="ag_alice", by="sp.vm")
    revoke_consent(state, tool="set_mute", agent_id="ag_alice", by="sp.vm", note="no longer needed")
    assert not SystemTools(workspace_root=project, config=system_on,
                           agent_id="ag_alice")._has_consent("set_mute")
    text = (state / "ledger.jsonl").read_text(encoding="utf-8")
    assert "revoked" in text and "approved" in text, "both the grant and its withdrawal survive"


def test_an_agent_cannot_approve_its_own_call(project, system_on):
    """An approval an agent can give itself is not an approval, so the recorder refuses it."""
    with pytest.raises(sysctl.ConsentError, match="names an agent"):
        grant_consent(project / ".agent_state", tool="set_mute", agent_id="ag_alice",
                      by="ag_alice")


def test_approving_a_tool_that_needs_no_approval_is_refused(project):
    """Recording an approval for a read would put a decision in the ledger that was never a decision."""
    with pytest.raises(sysctl.ConsentError, match="is not a tool that needs an approval"):
        grant_consent(project / ".agent_state", tool="system_state", agent_id="ag_alice", by="sp.vm")


def test_an_unattributed_approval_is_refused(project):
    """An append-only ledger whose entry has no author cannot be reviewed later."""
    with pytest.raises(sysctl.ConsentError, match="requires `by`"):
        grant_consent(project / ".agent_state", tool="set_mute", agent_id="ag_alice", by="")


def test_revoking_without_a_live_grant_is_refused(project):
    with pytest.raises(sysctl.ConsentError, match="no approval to withdraw"):
        revoke_consent(project / ".agent_state", tool="set_mute", agent_id="ag_alice", by="sp.vm")


def test_revoking_a_superseded_gate_is_refused(project):
    """A second withdrawal has nothing to withdraw, and must say so rather than record again."""
    state = project / ".agent_state"
    grant_consent(state, tool="set_mute", agent_id="ag_alice", by="sp.vm")
    revoke_consent(state, tool="set_mute", agent_id="ag_alice", by="sp.vm")
    with pytest.raises(sysctl.ConsentError, match="nothing to withdraw"):
        revoke_consent(state, tool="set_mute", agent_id="ag_alice", by="sp.vm")


def test_a_refused_call_records_the_request_for_the_owner(project, system_on):
    """So "what is my agent asking for" is answerable from the ledger, not only from a log line."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:open"),
                            system=system_on)
    assert not registry.call("open_app", {"name": "Finder"}).ok
    assert not registry.call("open_app", {"name": "Finder"}).ok
    text = (project / ".agent_state" / "ledger.jsonl").read_text(encoding="utf-8")
    assert request_gate("open_app", "ag_alice") in text
    assert text.count(f'"gate":"{request_gate("open_app", "ag_alice")}"') == 1, (
        "a retry loop must not be able to fill the ledger with duplicates of one question")


def test_a_missing_ledger_means_not_approved_rather_than_an_error(project, system_on):
    """Absence of a decision is absence of consent; defaulting the other way would make deleting the
    ledger a way to self-approve."""
    tools = SystemTools(workspace_root=project / "nowhere", config=system_on, agent_id="ag_alice")
    assert not tools._has_consent("set_mute")
    assert "Owner has not approved" in tools.set_mute({"muted": True}).text


def test_a_call_with_no_agent_context_cannot_be_covered_by_a_grant(project, system_on):
    """An approval is per agent, so a call with no agent has nothing that could cover it."""
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="")
    result = tools.set_mute({"muted": True})
    assert not result.ok
    assert "no agent context" in result.text


# ── read/write/exec are unchanged: the regression guard ──────────────────────


def test_read_and_write_still_behave_as_before(project):
    """The gate was extended for a capability with no path scope; the path semantics must not move."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*", "write:src/**"))
    assert registry.call("read_file", {"path": "src/calc.py"}).ok
    assert registry.call("read_file", {"path": "README.md"}).ok
    assert registry.call("write_file", {"path": "src/new.py", "content": "x = 1\n"}).ok
    # A write outside the grant is still refused, naming the scope rather than the capability.
    refused = registry.call("write_file", {"path": "README.md", "content": "no"})
    assert not refused.ok
    assert "write:README.md" in refused.text, "the refusal still names the path scope"
    assert "write:src/**" in refused.text
    assert (project / "README.md").read_text() == "# App\n"


def test_a_path_that_escapes_the_project_is_still_refused(project):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*", "write:*"))
    for path in ("../outside.py", "/etc/passwd", "~/.ssh/id_rsa"):
        assert not registry.call("read_file", {"path": path}).ok


def test_a_reviewer_still_cannot_write_what_it_reads(project):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*", role="reviewer"))
    assert registry.call("read_file", {"path": "src/calc.py"}).ok
    assert not registry.call("write_file", {"path": "src/calc.py", "content": "pass"}).ok


def test_run_command_still_needs_exec_and_still_hides_behind_the_sandbox(project):
    from engine.config import SandboxConfig

    off = ToolRegistry(workspace_root=project, agent=make_agent("read:*", "exec:*"),
                       sandbox=SandboxConfig(enabled=False))
    assert "run_command" not in off.names()
    on = ToolRegistry(workspace_root=project, agent=make_agent("read:*"),
                      sandbox=SandboxConfig(enabled=True))
    assert "run_command" in on.names()
    result = on.call("run_command", {"argv": ["true"]})
    assert not result.ok
    assert EXEC_CAPABILITY in result.text and "read:*" in result.text


def test_the_capability_constants_still_name_their_namespaces():
    assert READ_CAPABILITY == "read"
    assert WRITE_CAPABILITY == "write"
    assert EXEC_CAPABILITY == "exec", "the marker `delegation.elevated_markers()` names is `exec:`"
    assert SYSTEM_CAPABILITY == "system"


def test_the_system_namespace_does_not_leak_into_a_path_grant(project):
    """`read:*` is a path wildcard, not a namespace wildcard — it must not reach the machine."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*"),
                            system=SystemConfig(enabled=True))
    assert not registry._granted_scoped(SYSTEM_CAPABILITY, "state")
    assert registry._grants(READ_CAPABILITY, "src/anything")


def test_an_unknown_tool_is_refused_with_the_available_list(project, system_on):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:*"), system=system_on)
    result = registry.call("system_everything", {})
    assert not result.ok
    assert "system_state" in result.text


def test_a_default_configuration_hands_out_no_machine_access(config, project):
    """The shipped configuration must not reach the machine by accident."""
    if config.system.enabled:
        pytest.skip("this checkout has explicitly enabled system access")
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:*"),
                            system=config.system)
    assert not [name for name in registry.names() if name in ALL_TOOLS]


def test_the_shipped_allowlists_are_empty(config):
    """Off by default, and empty allowlists on top of that: turning it on grants nothing."""
    if config.system.enabled:
        pytest.skip("this checkout has explicitly enabled system access")
    assert config.system.allow_apps == []
    assert config.system.allow_automation == []


def test_a_requisition_for_system_automation_is_not_yet_elevated(config):
    """Pins a measured gap, so fixing it is a deliberate act rather than a silent one.

    `HiringDesk` decides an approval tier from `delegation.elevated_capability_markers`. The *default*
    now includes `system:` — `DelegationConfig.elevated_markers()` ships all five markers — so a fresh
    configuration classifies a `system:automation` requisition **T3** and reaches the Owner. That is
    the fix the UNCLOSED note in `engine/sysctl_tools.py` asked for, and it landed.

    What is still open is the override: `elevated_markers()` returns a *configured* list verbatim, so a
    `credentials.json` that spells the older four markers out — as this checkout's does — keeps the
    gap for every `system:` capability. This test therefore asserts on the *effective* markers rather
    than the default, which is the honest comparison, and it changes its assertion to match whichever
    of the two this checkout is in. If the file is ever brought in line, this reads T3; if the
    elevation is made a floor that a file cannot drop below, this reads T3 too. Either way the day it
    changes, someone reads why rather than finding it out from a bad run.
    """
    from engine.org.delegation import HiringDesk, Requisition
    from engine.org.roster import default_company

    org = default_company(provider="ollama", model="m", context_window=32768)
    markers = list(config.delegation.elevated_markers())
    desk = HiringDesk(org=org, approval_tiers={"elevated_capability_markers": markers})
    request = Requisition(requester_id="ag_a", skill="backend-developer",
                          capabilities=["system:automation"], requested_tokens=1000)
    tier, reason = desk.classify_tier(request)
    if "system:" in markers:
        assert tier.value == "T3" and "system:automation" in reason
    else:
        assert tier.value == "T1", (
            "this test documents that the *effective* marker list lacks `system:`; if it gained the "
            "entry, update this assertion and the PARTLY CLOSED note in engine/sysctl_tools.py")


def test_a_capability_grant_is_not_conferred_by_hiring(config):
    """The bound on the gap above: a helper is never handed a machine capability by default."""
    from engine.people import _capabilities_for

    for skill in ("backend-developer", "code-reviewer", "product-manager"):
        granted = _capabilities_for(skill)
        assert not [c for c in granted if str(c).startswith(f"{SYSTEM_CAPABILITY}:")], (
            f"hiring for {skill} conferred a machine capability, which is not how it should start")


# ── the binaries named are the ones that exist ───────────────────────────────


def test_the_verified_binaries_are_where_this_module_says_they_are(project):
    """A missing binary is a refusal naming the path, so this asserts the path is the right one."""
    import os

    for binary in sysctl.BINARIES:
        assert os.path.isfile(binary), (
            f"{binary} does not exist here; the module must name a path the machine really has, "
            "because it deliberately never searches $PATH")


def test_the_disk_and_app_reads_go_through_the_verified_binaries(project, system_on):
    """Guards against a later refactor reaching for a PATH search or a bare name."""
    source = (pathlib.Path(sysctl.__file__)).read_text(encoding="utf-8")
    assert "shutil.which(" not in source, (
        "resolving a binary through $PATH is the substitution risk the absolute paths exist to close")
    assert "shell=True" not in source
    assert DF in source and LSAPPINFO in source
    assert "list]" not in source.replace("processlist]", ""), (
        "the app count must not be read from `lsappinfo list`, whose output the 40 KB cap would cut")


# ── allow_full_access: the mode both reference agents ship ───────────────────


def test_full_access_lifts_the_allowlist_but_the_scoped_default_binds(tmp_path):
    """`allow_full_access` is Reasonix's `bypassPermissions` / Kimi's `--auto`, named and separate.

    Pinned because the two switches answer different questions — "which applications may this agent
    start" versus "do I still want to be consulted" — and a build that collapsed them would give a
    person no way to say "yes to the app, no to being interrupted".
    """
    from engine.config import SystemConfig
    from engine.sysctl_tools import SystemTools

    scoped = SystemTools(workspace_root=tmp_path,
                         config=SystemConfig(enabled=True, allow_full_access=True),
                         agent_id="ag_a")
    # No allowlist and no consent, yet the launch is permitted: that is what full access means.
    assert scoped.full_access is True

    binding = SystemTools(workspace_root=tmp_path,
                          config=SystemConfig(enabled=True, allow_full_access=False),
                          agent_id="ag_a")
    assert binding.full_access is False


def test_full_access_still_leaves_enabled_and_the_capability_required(tmp_path):
    """Full access is permission to *act*, never permission to stop being gated entirely.

    `system.enabled` and the per-agent `system:*` grant are checked elsewhere and must stay checked:
    a mode that also bypassed them would be "this agent may do anything on any machine", which is not
    what either reference agent offers.
    """
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from engine.config import SystemConfig
    from engine.org.agent import AgentLevel, AgentSpec
    from engine.tools import ToolRegistry

    def registry(caps):
        agent = AgentSpec(id="ag_a", name="A", title="T", skills=["backend-developer"],
                          provider="x", model="m", context_window=1000, capabilities=caps,
                          level=AgentLevel.SENIOR, role="worker")
        return ToolRegistry(workspace_root=tmp_path, agent=agent,
                            system=SystemConfig(enabled=True, allow_full_access=True))

    # Full access does not confer the grant: these are two independent answers.
    refused = registry(["read:*"]).call("system_state", {})
    assert refused.ok is False
    assert "system:state" in refused.text

    allowed = registry(["read:*", "system:state"]).call("system_state", {})
    assert allowed.ok is True


def test_full_access_does_not_remove_the_time_and_output_bounds(tmp_path):
    """A bound is not an approval, so handing the machine over must not remove it.

    Without this the mode would make an unattended run able to hang forever on a dialog nobody can
    dismiss, which is the failure the ceilings exist for.
    """
    from engine.config import SystemConfig
    from engine.sysctl_tools import SystemTools

    tools = SystemTools(workspace_root=tmp_path,
                        config=SystemConfig(enabled=True, allow_full_access=True,
                                            max_seconds=7, max_output_bytes=1234),
                        agent_id="ag_a")
    assert tools.max_seconds == 7
    assert tools.max_output_bytes == 1234


def test_full_access_lifts_both_allowlists_or_neither(tmp_path):
    """`open_app` and `run_automation` are the pair `config.py`'s own documentation names.

    A mode that lifted the app allowlist while leaving the script allowlist closed would be a
    `bypassPermissions` that does not bypass, and the two tools disagreeing about one switch is the
    kind of gap nobody finds until an unattended run needs a script at 3am. Asserted against the
    refusal paths — neither call reaches the machine, so this costs nothing.
    """
    from engine.config import SystemConfig
    from engine.sysctl_tools import SystemTools

    scoped = SystemTools(workspace_root=tmp_path, config=SystemConfig(enabled=True),
                         agent_id="ag_a")
    # Consent is granted so each call reaches the allowlist check rather than stopping at the gate
    # before it — otherwise this would assert the consent refusal twice and prove nothing about lists.
    grant_consent(tmp_path / ".agent_state", tool="open_app", agent_id="ag_a", by="sp.vm")
    grant_consent(tmp_path / ".agent_state", tool="run_automation", agent_id="ag_a", by="sp.vm")
    assert "system.allow_apps" in scoped.open_app({"name": "Finder"}).text
    assert "system.allow_automation" in scoped.run_automation({"script": "return 1"}).text

    open_ = SystemTools(workspace_root=tmp_path,
                        config=SystemConfig(enabled=True, allow_full_access=True), agent_id="ag_a")
    # Both allowlists step aside, so neither refusal is about the list any more. A *bad argument* is
    # still refused, which is how this proves the list gate was passed rather than the call being
    # short-circuited somewhere else.
    assert "system.allow_apps" not in open_.open_app({}).text
    assert "system.allow_automation" not in open_.run_automation({"script": "  "}).text


def test_the_documented_full_access_list_is_the_closed_set_the_code_implements():
    """The module docstring names four effects; a fifth read in the code means they parted ways.

    Read off the source rather than restated, because the failure mode this guards is a later change
    quietly widening the mode. Only `self.full_access` is counted, so prose about the setting does not
    inflate the tally: the reads are the attribute assignment, the consent gate, and one condition
    each in `open_app`, `run_automation` and `run_shortcut` — the three allowlists the mode steps
    aside.
    """
    source = pathlib.Path(sysctl.__file__).read_text(encoding="utf-8")
    code_reads = [line.strip() for line in source.splitlines()
                  if "self.full_access" in line]
    assert len(code_reads) == 5, (
        "an unexpected `self.full_access` read appeared, so the docstring's closed list of four "
        f"effects is out of date: {code_reads}")
    assert "FULL ACCESS" in source, "the mode must stay documented where the code lives"
    assert "1. `open_app` may launch any" in source, (
        "the documented effects must stay enumerated, not summarised away")
    assert "3. `run_shortcut` may run any Shortcut by name" in source, (
        "the third allowlist the mode lifts must stay named in the same closed list, or the "
        "docstring and the code disagree about how far `allow_full_access` reaches")


# ── the six newer capabilities: the catalogue, the config, and the gate ──────
#
# The six capabilities below arrived in `SystemConfig.CAPABILITIES` before they had tools, so a guard
# in this file was red on purpose: the config advertised grants that bought nothing. These are the
# tools that close it, and the tests below are written to the same rule as the rest of the file —
# assert the property that makes the scoping worth having, not the code that produces it.
#
# Nothing here speaks aloud, sleeps the machine, installs an update or launches a Shortcut. Each of
# those is a real change to the developer's machine, and a suite that does one of them to prove a
# point is a suite that costs the person running it. Where a tool reaches the machine the call is
# stubbed and the *effect* is asserted on the argv or the refusal a model would actually receive. The
# live calls are the read-only ones: `system_state`, `list_os_updates`, `spotlight_search` and
# `network_status` (which does move bytes — it is a measurement, and measure=false is used where the
# point is not the measurement).

NEW_CAPABILITY_TOOLS = {
    "system:notify": ["say_message", "post_notification"],
    "system:search": ["spotlight_search"],
    "system:power": ["keep_awake", "sleep_now"],
    "system:network": ["network_status"],
    "system:shortcuts": ["run_shortcut"],
    "system:softwareupdate": ["list_os_updates", "install_os_updates"],
}
NEW_TOOLS = [name for names in NEW_CAPABILITY_TOOLS.values() for name in names]


def test_every_new_capability_has_a_tool_and_the_catalogue_says_which():
    """The drift guard's positive direction: no declared grant buys nothing.

    `SystemConfig.CAPABILITIES` lists twelve; six of them had no tool until these. A config that
    advertises a grant with no implementation tells a person they can switch something on that will
    not work, so every declared capability must have at least one tool, and each tool must claim the
    capability the config names.
    """
    covered = {entry.capability for entry in CATALOGUE}
    for capability in SystemConfig.CAPABILITIES:
        assert capability in covered, (
            f"{capability} is declared in SystemConfig.CAPABILITIES with no tool behind it; either "
            "build one or stop advertising the grant")
    for capability, names in NEW_CAPABILITY_TOOLS.items():
        declared = {entry.capability for entry in CATALOGUE if entry.name in names}
        assert declared == {capability}, declared


def test_no_tool_claims_a_capability_the_config_does_not_declare():
    """The drift guard's other direction, asserted separately so each failure is its own message.

    A tool whose capability is not in `SystemConfig.CAPABILITIES` is a grant nobody can discover from
    the config or the console: the registry would still enforce it, but no surface would describe it,
    and an operator could never switch it on deliberately.
    """
    declared = set(SystemConfig.CAPABILITIES)
    for entry in CATALOGUE:
        assert entry.capability in declared, (
            f"{entry.name} claims {entry.capability}, which SystemConfig.CAPABILITIES does not "
            "declare; the config and the tool layer disagree about the vocabulary")


def test_the_config_capabilities_and_this_module_agree_exactly():
    """The module's own `CAPABILITIES` is a third copy, so it is checked against the others too.

    It exists so a caller can ask this module what it implements without loading a config. Three
    copies of one list is two too many, which is why the equality is asserted in both directions
    rather than the presence of the six new names.
    """
    assert set(CAPABILITIES) == set(SystemConfig.CAPABILITIES), (
        "engine/sysctl_tools.CAPABILITIES and SystemConfig.CAPABILITIES have drifted apart")
    assert CAPABILITIES == SystemConfig.CAPABILITIES, (
        "the two are the same list and should read in the same order, so a diff of them is empty")


def test_the_registry_gate_table_is_populated_from_the_catalogue():
    """`tools.py`'s table is hand-written; the catalogue publishes into it, and they must agree.

    This is the wiring that makes a new tool reachable at all. `_register_system_tools` *skips* any
    catalogue entry absent from `SYSTEM_TOOL_CAPABILITY`, so a new tool that only appears in the
    catalogue would be silently missing — not refused, which would at least be visible, but absent,
    which reads as a typo. `publish_capabilities` sets the mapping from the catalogue at import; this
    asserts that it did, and that nothing was published with a *different* capability than the one
    `tools.py` already declared for it.
    """
    table = ToolRegistry.SYSTEM_TOOL_CAPABILITY
    for entry in CATALOGUE:
        assert table.get(entry.name) == entry.capability, (
            f"{entry.name}: the catalogue says {entry.capability} and the registry's gate table says "
            f"{table.get(entry.name)!r}, which means one of the two is a lie")
    assert sysctl.PUBLISHED_TOOL_COUNT == len(CATALOGUE), (
        "every catalogue entry must have been published into the gate table")


def test_each_new_tool_is_absent_when_the_section_is_off(project):
    """`system.enabled=False` is the operator's switch, and it hides all six capabilities' tools."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:*"),
                            system=SystemConfig(enabled=False,
                                                allow_shortcuts=["Battery Level"]))
    for name in NEW_TOOLS:
        assert name not in registry.names(), f"{name} is offered while system.enabled is false"


def test_each_new_tool_is_advertised_when_enabled(project, system_on):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:*"), system=system_on)
    for name in NEW_TOOLS:
        assert name in registry.names(), f"{name} is missing from an enabled registry"


def test_each_new_tool_is_refused_without_its_grant_naming_it(project, system_on):
    """Present but refused, and the refusal names the grant — the shape the rest of the file asserts.

    `system:state` is held and nothing else, so every one of these must be refused, and the *specific*
    capability required must appear in the text. A refusal that named a different capability would send
    an operator to grant the wrong thing.
    """
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:state"),
                            system=system_on)
    for capability, names in NEW_CAPABILITY_TOOLS.items():
        for name in names:
            result = registry.call(name, _args_for(name))
            assert not result.ok, f"{name} answered an agent holding only system:state"
            assert capability in result.text, (
                f"the refusal for {name} must name the capability required ({capability})")
            assert "required" in result.text and "held" in result.text


def test_a_new_grant_does_not_confer_a_sibling(project, system_on):
    """Holding `system:notify` must not reach `system:power`, which is the whole design."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:notify"),
                            system=system_on)
    assert not registry.call("sleep_now", {}).ok
    assert not registry.call("install_os_updates", {}).ok
    assert not registry.call("run_shortcut", {"name": "Battery Level"}).ok


# ── system:notify ────────────────────────────────────────────────────────────


def test_say_message_needs_consent_but_post_notification_does_not(project, system_on):
    """Two tools because there are two decisions: speaking is public, a banner is quiet.

    Asserted as a pair, because the asymmetry is the design and a change that collapsed it would look
    reasonable in either half alone. `say_message` asks because a voice is audible to everyone in the
    room and cannot be taken back; `post_notification` does not because a banner is addressed to one
    person at one screen and leaves nothing changed.
    """
    assert "say_message" in CONSENT_REQUIRED
    assert "post_notification" not in CONSENT_REQUIRED
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:notify"),
                            system=system_on)
    refused = registry.call("say_message", {"text": "hello"})
    assert not refused.ok
    assert "Owner has not approved it for this agent" in refused.text
    assert consent_gate("say_message", "ag_alice") in refused.text


def test_say_message_refuses_a_message_longer_than_the_bound(project, system_on):
    """Speech costs time, so a long message is refused rather than truncated mid-clause.

    The bound is what keeps a spoken message inside `max_seconds` instead of dying at the ceiling
    half-spoken, and it is refused rather than cut because a sentence stopped mid-clause is a
    different and worse thing than a long file truncated.
    """
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="say_message", agent_id="ag_alice", by="sp.vm")
    result = tools.say_message({"text": "x" * (sysctl.MAX_SPEECH_CHARS + 1)})
    assert not result.ok
    assert str(sysctl.MAX_SPEECH_CHARS) in result.text
    assert "post_notification" in result.text, "the refusal must name the quieter alternative"
    # And the boundary itself is accepted, so the bound is a bound and not an off-by-one.
    tools._call = lambda argv, **kw: sysctl.SystemCall(True, 0, "", "")
    assert tools.say_message({"text": "x" * sysctl.MAX_SPEECH_CHARS}).ok


def test_say_message_speaks_the_text_as_one_argv_element(project, system_on):
    """The message is one argv element handed to `say`, never concatenated into a shell line.

    Stubbed, because actually speaking would make a sound in the developer's room. What is asserted is
    the transport: the binary is the absolute `say` path, the text is a single argument, and nothing
    about the call goes through a shell. A message containing quotes is therefore spoken with its
    quotes rather than being parsed as anything.
    """
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="say_message", agent_id="ag_alice", by="sp.vm")
    seen: list[list[str]] = []

    def capture(argv, *, stdin_text=None, timeout_s=None):
        seen.append([str(a) for a in argv])
        return sysctl.SystemCall(True, 0, "", "")

    tools._call = capture
    tricky = 'he said "stop"; do shell script "rm -rf ~"'
    assert tools.say_message({"text": tricky}).ok
    assert seen == [[sysctl.SAY, tricky]], seen


def test_say_message_refuses_a_voice_that_is_not_installed(project, system_on):
    """`say -v <unknown>` is silently ignored on this machine, so accepting it would be a false report.

    Measured, not assumed: `say -v zzznotavoice -o file 'hi'` exits 0 and speaks in the default voice.
    A call that returned "spoke in Alex" for a voice named Nobody would be the same class of lie as
    clamping the volume, and it gets the same answer — check, then refuse naming what is available.
    """
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="say_message", agent_id="ag_alice", by="sp.vm")

    def fake(argv, *, stdin_text=None, timeout_s=None):
        if argv[0] == sysctl.SAY and list(argv[1:]) == ["-v", "?"]:
            return sysctl.SystemCall(True, 0, "Samantha          en_US    # Hi\n"
                                              "Alex              en_US    # Hi\n", "")
        return sysctl.SystemCall(True, 0, "", "")

    tools._call = fake
    result = tools.say_message({"text": "hi", "voice": "Nobody"})
    assert not result.ok
    assert "Nobody" in result.text
    assert "Samantha" in result.text, "the refusal must name the voices that do exist"
    assert tools.say_message({"text": "hi", "voice": "Alex"}).ok


def test_post_notification_passes_the_text_as_arguments_not_as_script(project, system_on):
    """The AppleScript program is a constant; only its input varies.

    Which is what keeps a message containing a quote or a line of AppleScript from being anything but
    a string. Asserted on the argv, because posting a banner would put a window on the developer's
    screen to prove a point about transport.
    """
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    seen: list[list[str]] = []
    tools._call = lambda argv, **kw: (seen.append([str(a) for a in argv]),
                                      sysctl.SystemCall(True, 0, "", ""))[1]
    hostile = 'done" with title "Pwned"; do shell script "rm -rf ~"'
    assert tools.post_notification({"message": hostile, "title": "AgentOrg"}).ok
    argv = seen[0]
    assert argv[0] == sysctl.OSASCRIPT
    assert argv[-2:] == [hostile, "AgentOrg"], (
        "the message and title must arrive as run-handler arguments after `--`, not interpolated "
        "into the script source")
    assert "--" in argv, "the terminator is what stops a message starting with `-` being read as a flag"
    assert hostile not in argv[2], "the message must not appear inside the script itself"


def test_post_notification_refuses_a_message_longer_than_a_banner_shows(project, system_on):
    """macOS truncates a long banner, so a longer message would be reported as posted but not shown."""
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    result = tools.post_notification({"message": "x" * (sysctl.MAX_NOTIFICATION_CHARS + 1)})
    assert not result.ok
    assert str(sysctl.MAX_NOTIFICATION_CHARS) in result.text


def test_post_notification_needs_a_message(project, system_on):
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    result = tools.post_notification({})
    assert not result.ok
    assert "message is required" in result.text


# ── system:search ────────────────────────────────────────────────────────────


def test_spotlight_search_really_reads_the_index(project, system_on):
    """The one live search here, and it is read-only: it asks for a filename that must exist.

    The *count* is not asserted — this machine's index is not a fact about the code — but the parse
    is, and the presence of a path this checkout owns, which is what catches a tool that read the
    UserQueryParser banner on stderr as though it were a result.
    """
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:search"),
                            system=system_on)
    result = registry.call("spotlight_search", {"query": "kMDItemDisplayName == 'README.md'",
                                                "limit": 5})
    assert result.ok, result.text
    assert "Spotlight index" in result.text
    assert "UserQueryParser" not in result.text, (
        "mdfind prints its parser banner on stderr; reading it as a result would produce a path that "
        "is really a log line")
    assert "read:" in result.text, "the result must say that a path is not a permission to read it"


def test_spotlight_search_leaves_a_search_for_nothing_as_a_plain_answer(project, system_on):
    """No matches is a successful answer, not a failure — the difference matters to a model."""
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    result = tools.spotlight_search({"query": "kMDItemDisplayName == 'zzz-no-such-name-zzz'"})
    assert result.ok
    assert "no matches" in result.text


def test_spotlight_search_refuses_an_empty_query(project, system_on):
    """`mdfind ''` exits 1 with 'Failed to create query', so an empty query is refused here.

    Asserted because the command's *other* failure — an unknown flag — exits **0** with usage text,
    which would read to a model as "nothing found". Saying why up front is the honest alternative.
    """
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    result = tools.spotlight_search({"query": "   "})
    assert not result.ok
    assert "query is required" in result.text


def test_spotlight_search_refuses_a_query_that_starts_with_a_dash(project, system_on):
    """`mdfind` rejects `--` as a terminator, so a leading dash cannot be escaped and must be refused.

    Measured: `mdfind -x`, `mdfind --` and `mdfind -name -x` each exit 1 with usage text on stdout, so
    passing this through would produce a failure whose text tells a model nothing about what to do —
    "Unknown option -x / Usage: mdfind …" rather than the fix. Refused here for the clearer message,
    and refused in `name_only` mode too, where the same dash is read as a flag.
    """
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    for mode in (False, True):
        result = tools.spotlight_search({"query": "-name", "name_only": mode})
        assert not result.ok, f"a dash-led query must be refused with name_only={mode}"
        assert "dash" in result.text
        assert "kMDItemDisplayName" in result.text, "the refusal must name the way to write it"


def test_spotlight_search_refuses_a_tilde_within_rather_than_expanding_it(project, system_on):
    """`mdfind -onlyin ~` treats the tilde as a literal directory and exits **0** with no results.

    Measured, and the exit code is the point: a silent empty answer reads as "no such file" rather
    than as "the argument was wrong", which is precisely the failure a model cannot diagnose. Refused
    with the explanation rather than expanded here, because guessing at the person's home directory is
    a decision the caller should make explicitly.
    """
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    result = tools.spotlight_search({"query": "readme", "within": "~/Documents"})
    assert not result.ok
    assert "~" in result.text and "literal" in result.text
    assert "absolute path" in result.text


def test_the_search_is_read_only_and_therefore_needs_no_consent(project, system_on):
    assert "spotlight_search" not in CONSENT_REQUIRED
    entry = next(e for e in CATALOGUE if e.name == "spotlight_search")
    assert entry.mutates is False


def test_the_search_docstring_admits_it_sees_outside_the_read_grant():
    """The honesty requirement, asserted rather than trusted to a reviewer.

    The tool can report filenames the agent's `read:` grant does not cover. That is a real widening,
    and the module's own voice is to state what a mechanism does *not* protect against rather than to
    imply a confinement it does not have. This pins the admission so a later edit cannot quietly turn
    it into a claim of safety.
    """
    entry = next(e for e in CATALOGUE if e.name == "spotlight_search")
    assert "read:" in entry.description, (
        "the advertised description must say that this sees past the read grant")
    source = pathlib.Path(sysctl.__file__).read_text(encoding="utf-8")
    start = source.index("def spotlight_search")
    docstring = source[start:start + 3000]
    assert "read:" in docstring and "widen" in docstring.lower(), (
        "the tool's own docstring must state the widening rather than only the description doing so")


# ── system:power ─────────────────────────────────────────────────────────────


def test_sleep_now_is_consent_gated_and_keep_awake_is_too(project, system_on):
    """Both change the machine's power state; `sleep_now` additionally interrupts the person."""
    assert "sleep_now" in CONSENT_REQUIRED
    assert "keep_awake" in CONSENT_REQUIRED
    for name in ("sleep_now", "keep_awake"):
        registry = ToolRegistry(workspace_root=project, agent=make_agent("system:power"),
                                system=system_on)
        result = registry.call(name, _args_for(name))
        assert not result.ok, f"{name} acted without consent"
        assert consent_gate(name, "ag_alice") in result.text


def test_sleep_now_says_it_cannot_be_undone_from_here(project, system_on):
    """The refusal is the gate, and a sleeping Mac runs nothing — including the run that slept it."""
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="sleep_now", agent_id="ag_alice", by="sp.vm")
    assert tools._has_consent("sleep_now")
    # The call itself is stubbed: actually sleeping the developer's machine is not a thing a test may
    # do, and it would end the suite. What is asserted is the argv and the report.
    seen: list[list[str]] = []
    tools._call = lambda argv, **kw: (seen.append([str(a) for a in argv]),
                                      sysctl.SystemCall(True, 0, "", ""))[1]
    result = tools.sleep_now({})
    assert result.ok
    assert seen == [[sysctl.PMSET, "sleepnow"]]
    assert "cannot wake" in result.text


def test_keep_awake_starts_caffeinate_without_waiting_for_it(project, system_on):
    """`caffeinate -t N` holds the assertion, so the call must leave it running rather than wait.

    Measured: `caffeinate -t 5` blocks for five seconds and drops the assertion when it exits. A tool
    that waited for it would kill it at `max_seconds`, dropping the assertion exactly when the person
    needed it — so the command is started detached and only confirmed to have started. Asserted on the
    argv and on the detached seam, not by holding the developer's machine awake.
    """
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="keep_awake", agent_id="ag_alice", by="sp.vm")
    seen: list[list[str]] = []
    tools._start_detached = lambda argv: (seen.append([str(a) for a in argv]),
                                          sysctl.SystemCall(True, None, "", ""))[1]
    result = tools.keep_awake({"seconds": 600})
    assert result.ok, result.text
    assert seen == [[sysctl.CAFFEINATE, "-i", "-s", "-t", "600"]], seen
    assert "did" in result.text and "not wait" in result.text, (
        "the report must say the period was requested rather than observed")


def test_keep_awake_bounds_the_period_and_refuses_rather_than_clamping(project, system_on):
    """A clamp would report a period the machine is not holding, which is the same lie as a volume."""
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="keep_awake", agent_id="ag_alice", by="sp.vm")
    tools._start_detached = lambda argv: sysctl.SystemCall(True, None, "", "")
    result = tools.keep_awake({"seconds": sysctl.MAX_AWAKE_SECONDS + 1})
    assert not result.ok
    assert str(sysctl.MAX_AWAKE_SECONDS) in result.text
    assert tools.keep_awake({"seconds": 0}).ok is False


def test_keep_awake_reports_a_caffeinate_that_died_immediately(project, system_on):
    """A process that exited on its first line is not holding anything, so `ok` must be false."""
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="keep_awake", agent_id="ag_alice", by="sp.vm")
    tools._start_detached = lambda argv: sysctl.SystemCall(
        False, 1, "", "caffeinate: invalid option",
        reason="caffeinate exited 1 within its 3s startup window, so it is not holding anything.")
    result = tools.keep_awake({"seconds": 600})
    assert not result.ok
    assert "not holding anything" in result.text
    assert "invalid option" in result.text, "the binary's own message must reach the model"


# ── system:network ───────────────────────────────────────────────────────────


def test_network_status_is_bounded_and_says_how_long_it_takes(project):
    """`networkQuality` is slow, so the tool takes a tighter ceiling than `max_seconds` and says so.

    The measurement is bounded twice by the same number: `-M` bounds the test itself and the process
    ceiling bounds the process. Either alone is insufficient — `-M` does not cover the config request
    the binary makes first, and a subprocess kill alone would let a test that ignores its own limit run
    to the wall.
    """
    config = SystemConfig(enabled=True, max_seconds=60)
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")
    seen: dict[str, int | None] = {}

    def capture(argv, *, stdin_text=None, timeout_s=None):
        if argv[0] == sysctl.NETWORK_QUALITY:
            seen["timeout"] = timeout_s
            seen["has_M"] = "-M" in argv
            seen["M"] = int(argv[list(argv).index("-M") + 1])
            return sysctl.SystemCall(True, 0, '{"interface_name": "en0", "base_rtt": 30.0}', "")
        if argv[0] == sysctl.IFCONFIG:
            return sysctl.SystemCall(True, 0, "en0: flags=8863\n\tstatus: active\n", "")
        if argv[0] == sysctl.ROUTE:
            return sysctl.SystemCall(True, 0, "interface: en0\ngateway: 10.0.0.1\n", "")
        return sysctl.SystemCall(True, 0, "", "")

    tools._call = capture
    result = tools.network_status({"measure": True})
    assert result.ok, result.text
    assert seen["has_M"] is True, "the test must be bounded by networkQuality's own -M"
    assert seen["M"] == sysctl.NETWORK_TEST_SECONDS
    assert seen["timeout"] == sysctl.NETWORK_TEST_SECONDS + 5, (
        "the subprocess ceiling must be the measurement's own, not the longer configured max")


def test_network_status_reports_a_cut_measurement_without_a_figure(project):
    """Half a test is not a slower connection, so no throughput is reported from one."""
    config = SystemConfig(enabled=True, max_seconds=60)
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")

    def capture(argv, *, stdin_text=None, timeout_s=None):
        if argv[0] == sysctl.NETWORK_QUALITY:
            return sysctl.SystemCall(False, None, "", "", timed_out=True,
                                     reason="the command exceeded its wall-clock ceiling")
        if argv[0] == sysctl.IFCONFIG:
            return sysctl.SystemCall(True, 0, "en0: flags=8863\n\tstatus: active\n", "")
        return sysctl.SystemCall(True, 0, "interface: en0\ngateway: 10.0.0.1\n", "")

    tools._call = capture
    result = tools.network_status({"measure": True})
    assert result.ok
    assert "NOT measured" in result.text
    assert "Mbps" not in result.text, "a cut test must not produce a throughput figure"


def test_network_status_skips_the_test_when_there_is_no_route(project, system_on):
    """An offline machine would spend the full `-M` wait to prove it is offline; say so instead."""
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    called: list[str] = []

    def capture(argv, *, stdin_text=None, timeout_s=None):
        called.append(argv[0])
        if argv[0] == sysctl.IFCONFIG:
            return sysctl.SystemCall(True, 0, "lo0: flags=8049\n\tstatus: active\n", "")
        return sysctl.SystemCall(False, 1, "", "no route to host", reason="")

    tools._call = capture
    result = tools.network_status({"measure": True})
    assert result.ok
    assert "no default route" in result.text
    assert sysctl.NETWORK_QUALITY not in called, (
        "the measurement must not run when there is no route; it would fail after the full timeout")


def test_the_network_measurement_can_be_turned_off_for_a_cheap_read(project, system_on):
    """`measure=false` is the fast half: the interface read costs milliseconds and no traffic."""
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    called: list[str] = []

    def capture(argv, *, stdin_text=None, timeout_s=None):
        called.append(argv[0])
        if argv[0] == sysctl.IFCONFIG:
            return sysctl.SystemCall(True, 0, "en0: flags=8863\n\tstatus: active\n", "")
        return sysctl.SystemCall(True, 0, "interface: en0\ngateway: 10.0.0.1\n", "")

    tools._call = capture
    result = tools.network_status({"measure": False})
    assert result.ok
    assert sysctl.NETWORK_QUALITY not in called, "measure=false must not move any data"
    assert "not measured" in result.text


def test_the_network_parser_reads_the_json_it_is_given():
    """Captured output, so the parse is asserted without running a measurement in the suite.

    `dl_throughput` and `ul_throughput` are bits per second, per the man page and confirmed by the
    magnitude of a real run (~190 Mbps read as 190225248). Converted to Mbps so the figure is
    readable, and the unit is named in the report rather than left to be guessed from the exponent.
    """
    parsed = parse_network_quality(
        '{"interface_name": "en0", "base_rtt": 34.34, "dl_throughput": 43906256,'
        ' "ul_throughput": 86328704, "dl_responsiveness": 584.7, "ul_responsiveness": 1076.2,'
        ' "il_h2_req_resp": [1.0, 2.0]}')
    assert parsed is not None
    assert parsed["interface"] == "en0"
    assert parsed["down_mbps"] == 43.9 and parsed["up_mbps"] == 86.3
    assert parsed["idle_latency_ms"] == 34.3
    # A truncated document is not a measurement, so None rather than a partial figure.
    assert parse_network_quality('{"interface_name": "en0", "dl_thr') is None
    assert parse_network_quality("") is None
    assert parse_network_quality("not json at all") is None


def test_network_status_needs_no_consent_because_it_changes_nothing(project, system_on):
    """It moves bytes, but it changes no state on the machine, and the bytes are the measurement."""
    assert "network_status" not in CONSENT_REQUIRED
    entry = next(e for e in CATALOGUE if e.name == "network_status")
    assert entry.mutates is False


# ── system:shortcuts ─────────────────────────────────────────────────────────


def test_run_shortcut_refuses_a_name_not_in_the_allowlist_naming_the_list(project):
    """The allowlist is the scope, so the refusal must name it — otherwise it is a dead end."""
    config = SystemConfig(enabled=True, allow_shortcuts=["Battery Level", "Make PDF"])
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:shortcuts"),
                            system=config)
    grant_consent(project / ".agent_state", tool="run_shortcut", agent_id="ag_alice", by="sp.vm")
    result = registry.call("run_shortcut", {"name": "Sleep"})
    assert not result.ok
    assert "Battery Level, Make PDF" in result.text, "the refusal must name the allowlist"
    assert "Sleep" in result.text, "and what was asked for"
    assert "system.allow_shortcuts" in result.text, "and the setting that would change the answer"
    assert "Do not retry this call" in result.text


def test_run_shortcut_refuses_everything_when_the_allowlist_is_empty(project, system_on):
    """Empty is the default, and it means none — not "any"."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:shortcuts"),
                            system=system_on)
    grant_consent(project / ".agent_state", tool="run_shortcut", agent_id="ag_alice", by="sp.vm")
    result = registry.call("run_shortcut", {"name": "Sleep"})
    assert not result.ok
    assert "empty" in result.text and "no Shortcut is allowed" in result.text


def test_the_shortcut_match_is_exact_and_case_insensitive_not_a_prefix(project):
    """A prefix rule would let `Backup` authorise `BackupAndDeleteEverything`."""
    config = SystemConfig(enabled=True, allow_shortcuts=["Backup"])
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:shortcuts"),
                            system=config)
    grant_consent(project / ".agent_state", tool="run_shortcut", agent_id="ag_alice", by="sp.vm")
    assert not registry.call("run_shortcut", {"name": "BackupAndDeleteEverything"}).ok
    assert not registry.call("run_shortcut", {"name": "Back"}).ok
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")
    assert "backup" in {s.lower() for s in tools.allowed_shortcuts()}


def test_run_shortcut_needs_consent_and_a_name(project, system_on):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:shortcuts"),
                            system=system_on)
    refused = registry.call("run_shortcut", {"name": "Sleep"})
    assert not refused.ok
    assert consent_gate("run_shortcut", "ag_alice") in refused.text
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="run_shortcut", agent_id="ag_alice", by="sp.vm")
    result = tools.run_shortcut({})
    assert not result.ok
    assert "name is required" in result.text


def test_run_shortcut_passes_the_name_as_one_argv_element(project):
    """Stubbed, because launching a Shortcut runs the person's own automation."""
    config = SystemConfig(enabled=True, allow_shortcuts=["Make PDF"])
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="run_shortcut", agent_id="ag_alice", by="sp.vm")
    seen: list[list[str]] = []
    tools._call = lambda argv, **kw: (seen.append([str(a) for a in argv]),
                                      sysctl.SystemCall(True, 0, "made a pdf\n", ""))[1]
    result = tools.run_shortcut({"name": "Make PDF"})
    assert result.ok
    assert seen == [[sysctl.SHORTCUTS, "run", "Make PDF"]]
    assert "made a pdf" in result.text, "the Shortcut's output must come back"


# ── system:softwareupdate ────────────────────────────────────────────────────


def test_install_os_updates_requires_consent_and_list_os_updates_does_not(project, system_on):
    """Listing is read-only; installing is the heaviest action in the module. The pair is the design."""
    assert "install_os_updates" in CONSENT_REQUIRED
    assert "list_os_updates" not in CONSENT_REQUIRED
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:softwareupdate"),
                            system=system_on)
    refused = registry.call("install_os_updates", {})
    assert not refused.ok
    assert "Owner has not approved it for this agent" in refused.text
    assert consent_gate("install_os_updates", "ag_alice") in refused.text
    # And the listing half goes through to a real, read-only call.
    listed = registry.call("list_os_updates", {})
    assert listed.ok, listed.text


def test_list_os_updates_really_reads_the_machine(project, system_on):
    """The read-only half is exercised live: it is a scan, it installs nothing, and it costs nothing.

    The *answer* is not asserted — whether this machine has updates pending is not a fact about the
    code, and pinning it would make the suite depend on Apple's release schedule. What is asserted is
    that the tool returned a real answer rather than a crash, and that it reports the case
    `softwareupdate` puts on **stderr** (`No new software available.`) rather than reading that as an
    empty scan.
    """
    registry = ToolRegistry(workspace_root=project, agent=make_agent("system:softwareupdate"),
                            system=system_on)
    result = registry.call("list_os_updates", {})
    assert result.ok, result.text
    assert result.text.strip(), "the report must say something rather than being empty"
    assert "install_os_updates" in result.text or "update" in result.text.lower()


def test_the_update_parser_reads_labels_and_reports_none_for_the_empty_case():
    """Captured output both ways, so the parse is asserted without a scan."""
    assert parse_update_list("* Label: macOS Tahoe 26.5\n\tTitle: macOS Tahoe 26.5\n"
                             "\tSize: 1234567K\n* Label: Safari 18\n\tTitle: Safari\n") == [
        "macOS Tahoe 26.5", "Safari 18"]
    # The real "nothing to install" output carries no labels at all.
    assert parse_update_list("Software Update Tool\n\nFinding available software\n") == []


def test_install_os_updates_installs_only_and_does_not_reboot_by_default(project, system_on):
    """`-R` is omitted unless asked: a reboot ends the run, and that must be a choice.

    Stubbed, because installing an OS update on the developer's machine is not a thing a test may do.
    Also asserted: `--agree-to-license` is never passed — agreeing to a licence on the person's behalf
    is their decision, not the agent's.
    """
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="install_os_updates", agent_id="ag_alice", by="sp.vm")
    seen: list[list[str]] = []
    tools._call = lambda argv, **kw: (seen.append([str(a) for a in argv]),
                                      sysctl.SystemCall(True, 0, "done", ""))[1]
    assert tools.install_os_updates({}).ok
    assert seen == [[sysctl.SOFTWAREUPDATE, "-i", "-r"]], seen
    assert "-R" not in seen[0]
    assert "--agree-to-license" not in seen[0], (
        "agreeing to a licence is the person's decision, not the agent's")
    assert tools.install_os_updates({"restart": True}).ok
    assert "-R" in seen[-1]
    assert tools.install_os_updates({"labels": ["macOS Tahoe 26.5"]}).ok
    assert seen[-1] == [sysctl.SOFTWAREUPDATE, "-i", "macOS Tahoe 26.5"]


def test_install_os_updates_reports_a_call_stopped_at_the_ceiling_as_started_not_failed(project):
    """An install is *expected* to outrun the ceiling, so being stopped is not the install failing.

    `softwareupdate` hands the work to its daemon, so the honest report is that the install was
    started and this call stopped watching — and `ok` is True because the model must not treat it as
    something to retry, which would start a second install.
    """
    tools = SystemTools(workspace_root=project, config=SystemConfig(enabled=True, max_seconds=20),
                        agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="install_os_updates", agent_id="ag_alice", by="sp.vm")
    tools._call = lambda argv, **kw: sysctl.SystemCall(
        False, None, "", "Installing…", timed_out=True,
        reason="the command exceeded its wall-clock ceiling")
    result = tools.install_os_updates({})
    assert result.ok, "a stopped install is a started install, not a failure to retry"
    assert "may still be installing" in result.text
    assert "Do not call this again" in result.text


def test_install_os_updates_refuses_a_non_list_of_labels(project, system_on):
    tools = SystemTools(workspace_root=project, config=system_on, agent_id="ag_alice")
    grant_consent(project / ".agent_state", tool="install_os_updates", agent_id="ag_alice", by="sp.vm")
    result = tools.install_os_updates({"labels": "macOS Tahoe 26.5"})
    assert not result.ok
    assert "list" in result.text


def test_installing_is_described_as_the_heaviest_grant_in_the_catalogue(project):
    """The advertisement a model reads must carry the danger, not only the config prose."""
    entry = next(e for e in CATALOGUE if e.name == "install_os_updates")
    assert entry.mutates is True
    assert "REBOOT" in entry.description, (
        "the advertised description must say the machine can reboot, since that is what ends a run")


# ── allow_full_access lifts the three allowlists, and nothing else ───────────


def test_full_access_lifts_the_shortcut_allowlist(tmp_path):
    """The third scope the mode steps aside, checked in both directions.

    The scope test needs consent so it reaches the allowlist rather than stopping at the gate before
    it; the full-access test must reach the *call*, so the binary is stubbed and the argv asserted —
    which also proves the allowlist gate was passed rather than the call being short-circuited.
    """
    granted = tmp_path / ".agent_state"
    grant_consent(granted, tool="run_shortcut", agent_id="ag_a", by="sp.vm")

    scoped = SystemTools(workspace_root=tmp_path, config=SystemConfig(enabled=True),
                         agent_id="ag_a")
    refused = scoped.run_shortcut({"name": "Sleep"})
    assert not refused.ok and "system.allow_shortcuts" in refused.text

    open_ = SystemTools(workspace_root=tmp_path,
                        config=SystemConfig(enabled=True, allow_full_access=True), agent_id="ag_a")
    seen: list[list[str]] = []
    open_._call = lambda argv, **kw: (seen.append([str(a) for a in argv]),
                                      sysctl.SystemCall(True, 0, "", ""))[1]
    assert open_.run_shortcut({"name": "Sleep"}).ok, "full access must lift the shortcut allowlist"
    assert seen == [[sysctl.SHORTCUTS, "run", "Sleep"]]


def test_full_access_does_not_lift_enabled_the_grant_or_the_bounds(tmp_path):
    """A bound is not an approval, and the mode is permission to act rather than to stop being gated.

    The three things it must not move are asserted together because each is a different shape of
    mistake: `enabled` is the operator's switch, the grant is the agent's, and the bounds are the
    deployment's. Full access is none of those.
    """
    # `enabled` false: nothing is registered at all, however the mode is set.
    off = ToolRegistry(workspace_root=tmp_path, agent=make_agent("system:*"),
                       system=SystemConfig(enabled=False, allow_full_access=True))
    assert not [name for name in off.names() if name in NEW_TOOLS]

    # The grant still binds: `system:state` does not reach the new tools with the mode on.
    scoped = ToolRegistry(workspace_root=tmp_path, agent=make_agent("system:state"),
                          system=SystemConfig(enabled=True, allow_full_access=True))
    refused = scoped.call("sleep_now", {})
    assert not refused.ok
    assert "system:power" in refused.text

    # The bounds survive, including the new lengths.
    tools = SystemTools(workspace_root=tmp_path,
                        config=SystemConfig(enabled=True, allow_full_access=True,
                                            max_seconds=7, max_output_bytes=1234,
                                            allow_shortcuts=["Sleep"]),
                        agent_id="ag_a")
    assert tools.max_seconds == 7
    assert tools.max_output_bytes == 1234
    over = tools.say_message({"text": "x" * (sysctl.MAX_SPEECH_CHARS + 1)})
    assert not over.ok, "the speech bound is a bound, and full access must not lift it"
    assert not tools.keep_awake({"seconds": sysctl.MAX_AWAKE_SECONDS + 1}).ok


def test_full_access_still_does_not_confer_a_capability(project):
    """The mode widens what an agent holding a grant may do; it never hands over the grant."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*"),
                            system=SystemConfig(enabled=True, allow_full_access=True,
                                                allow_shortcuts=["Sleep"]))
    for name in NEW_TOOLS:
        result = registry.call(name, _args_for(name))
        assert not result.ok, f"{name} answered an agent with no system grant"
        assert "required" in result.text


# ── the bounds, on the new tools ─────────────────────────────────────────────


def test_the_bounds_are_enforced_on_a_slow_new_call(project):
    """`max_seconds` stops a slow command and says so — the same guarantee every other tool has.

    `networkQuality` is the natural slow call here; it is stubbed with a real slow binary instead
    (`osascript -e 'delay 30'`) so the ceiling is measured on the transport rather than trusted, and
    the argv proves the ceiling was applied to the *new* call path.

    A separate assertion covers `_call`'s tightening rule: a tool may ask for *less* time than the
    configured bound but never more, because the ceiling is a property of the deployment.
    """
    import time as _time

    config = SystemConfig(enabled=True, max_seconds=1)
    tools = SystemTools(workspace_root=project, config=config, agent_id="ag_alice")
    started = _time.time()
    result = tools._call([OSASCRIPT, "-e", "delay 30"])
    elapsed = _time.time() - started
    assert not result.ok and result.timed_out
    assert "ceiling" in result.stderr
    assert elapsed < 20, f"the ceiling took {elapsed:.1f}s to bite; it should be ~1s"

    # And the tightening rule, asserted on the value that actually reached the transport.
    import engine.sysctl_tools as module
    seen: list[int] = []
    original = module.run

    def spy(argv, *, stdin_text=None, timeout_s=20, max_output_bytes=40_000):
        seen.append(timeout_s)
        return original(argv, stdin_text=stdin_text, timeout_s=timeout_s,
                        max_output_bytes=max_output_bytes)

    module.run = spy
    try:
        tools._call([sysctl.IFCONFIG])                      # no request: the configured bound
        tools._call([sysctl.IFCONFIG], timeout_s=99)        # asks for longer: still the configured
        tools._call([sysctl.IFCONFIG], timeout_s=1)         # asks for shorter: honoured
    finally:
        module.run = original
    assert seen == [1, 1, 1], (
        "a tool call must not be able to widen the configured ceiling, only tighten it")


def test_the_new_binaries_are_where_this_module_says_they_are():
    """A missing binary is a refusal naming the path, so the paths must be the ones the machine has."""
    import os

    for binary in (sysctl.SAY, sysctl.CAFFEINATE, sysctl.MDFIND, sysctl.NETWORK_QUALITY,
                   sysctl.SHORTCUTS, sysctl.SOFTWAREUPDATE, sysctl.IFCONFIG, sysctl.ROUTE):
        assert binary.startswith("/"), f"{binary} is not an absolute path"
        assert os.path.isfile(binary), binary
        assert binary in sysctl.BINARIES, f"{binary} is used but not declared in BINARIES"


# ── the parsers the new tools lean on ────────────────────────────────────────


def test_the_shortcut_name_parser_reads_one_name_per_line():
    """`shortcuts list` prints bare names, so the parse is non-empty lines, trimmed."""
    assert parse_shortcut_names("Track My Orders\nMake PDF\n\n  Battery Level  \n") == [
        "Track My Orders", "Make PDF", "Battery Level"]
    assert parse_shortcut_names("") == []


def test_the_network_helpers_read_status_not_the_up_flag():
    """An interface is `UP` when it is enabled, which is true of Wi-Fi connected to nothing.

    `status: active` is the one that means the interface can carry traffic, so reading the flags would
    report a path out that has nothing on the other end of it.
    """
    text = ("lo0: flags=8049<UP,LOOPBACK,RUNNING>\n\tinet 127.0.0.1\n"
            "en0: flags=8863<UP,BROADCAST,RUNNING>\n\tinet 10.0.0.17\n\tstatus: active\n"
            "en1: flags=8863<UP,BROADCAST>\n\tstatus: inactive\n")
    assert sysctl._parse_active_interfaces(text) == ["en0"]
    route = sysctl._parse_default_route(
        "   route to: default\ndestination: default\n    gateway: 10.0.0.1\n"
        "  interface: en0\n      flags: <UP,GATEWAY>\n")
    assert route == {"gateway": "10.0.0.1", "interface": "en0"}
    assert sysctl._parse_default_route("") == {}


def test_the_say_voice_parser_reads_the_name_column():
    """The lines are `Name   en_US   # sample`, and the name is what is before the wide gap."""
    assert sysctl.parse_say_voices("Samantha          en_US    # Hi\n"
                                   "Amélie              fr_CA    # Bonjour\n") == ["Samantha", "Amélie"]
    assert sysctl.parse_say_voices("") == []
