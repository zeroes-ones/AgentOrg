#!/usr/bin/env python3
"""Phase 32 tests — the confined shell, and the tool that reaches it.

`engine/tools.py` refused to ship a command tool, and the refusal explained itself: an unconstrained
shell in a real repository is a bigger risk than a file write and deserved its own decision. This is
that decision, and it is **confinement**. The tests are written against the properties that make the
confinement worth having rather than against the code that produces it:

1. **A command runs, and its output comes back.** The whole reason for the feature: an agent that
   cannot run the test suite it just wrote is limited to guessing whether the code works.
2. **A write outside the workspace is refused by the kernel, not by a string check.** The test attempts
   the write and then asserts the file is not there. A Python-level path check would pass this test
   while failing to contain a command that spawns a child, which is the shape that actually matters.
3. **The home directory's contents stay unreadable.** This is the assertion that keeps the confinement
   honest: `(literal "/")` is required for any process to start at all, and the test proves that rule
   buys the runtime a directory entry rather than read access to `~/.ssh`.
4. **Network is denied unless it was asked for.** Off by default, and the on-case is checked too, so
   the denial is a policy rather than a profile that happens to break sockets.
5. **The two bounds bite.** A long command is stopped at the ceiling, and output is cut and *says* it
   was cut — a silently clipped test log is a model reasoning about output it never saw.
6. **No backend means no command.** The preflight refuses rather than running unconfined, which is the
   one failure this module exists to prevent: a sandbox that degrades silently is worse than none.
7. **The tool is not advertised unless it can be honoured.** Absent when `sandbox.enabled` is false,
   absent in a read-only run, and refused with an explanatory message to an agent holding no `exec:`
   grant — because a tool that is offered and then refused teaches a model to distrust the tool list.
8. **The processes and files a run creates do not outlive it.** The last section is about resource
   lifetime rather than confinement, and it lives here because it is the same question — what does
   this engine leave running behind it — for the two other resources that had no bound: the event
   trace, which grew until someone noticed, and the runner subprocess, which survived its engine.
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# `ROOT` has to be on `sys.path` before these resolve, which is why the whole block below the
# insert is here rather than at the top of the file — the same ordering every test module in this
# suite uses.
from engine.bus import MAX_TRACE_BYTES, EventBus, load_trace  # noqa: E402
from engine.config import SandboxConfig, load  # noqa: E402
from engine.host import RunnerHost  # noqa: E402
from engine.org.agent import AgentLevel, AgentSpec
from engine.sandbox import (
    BASE_OPERATIONS,
    SANDBOX_EXEC,
    SandboxedCommand,
    SandboxError,
    backend_available,
    detect_backend,
)
from engine.tools import EXEC_CAPABILITY, ToolRegistry
from engine.state import ENGINE_STATE_DIRNAME


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
    """A real project tree to run commands against."""
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text("def subtract(a, b):\n    return a - b\n")
    (root / "README.md").write_text("# App\n")
    return root


@pytest.fixture
def sandbox_config():
    return SandboxConfig(enabled=True, max_seconds=30, max_output_bytes=4096)


@pytest.fixture
def runner(project, sandbox_config):
    return SandboxedCommand(workspace_root=project, config=sandbox_config)


def _python(*lines: str) -> list[str]:
    """An argv that runs an inline Python program, so a test does not need a fixture file."""
    return [sys.executable, "-c", "\n".join(lines)]


# ── the backend ──────────────────────────────────────────────────────────────


def test_the_backend_is_reported_by_name_not_a_bare_boolean():
    """A refusal has to be able to say *what* it looked for; a boolean cannot."""
    detected = detect_backend()
    assert detected in (None, "seatbelt")
    assert backend_available() == (detected is not None)
    if sys.platform == "darwin":
        # If Seatbelt is present, its name is the one every other message quotes.
        assert detected == "seatbelt", "macOS with the binary present must report seatbelt"


def test_the_profile_denies_by_default_and_names_every_exception():
    """DENY first is the property the whole module rests on: everything else is a named exception."""
    profile = SandboxedCommand(
        workspace_root=pathlib.Path("/tmp/does-not-matter"),
        config=SandboxConfig(enabled=True)).profile()
    text = profile.text()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    assert lines[0] == "(version 1)"
    assert "(deny default)" in lines
    assert lines.index("(deny default)") < lines.index(
        next(line for line in lines if line.startswith("(allow file-read*")))
    for operation in BASE_OPERATIONS:
        assert f"(allow {operation})" in text
    # Nothing broad is granted: the read rule names paths, and `subpath "/"` is never one of them.
    for line in lines:
        if line.startswith("(allow file-read*"):
            assert '(subpath "/")' not in line, (
                "a subpath grant on / would make the whole filesystem readable and empty the "
                "confinement of its meaning")
            assert '(literal "/")' in line, (
                "the literal root entry is what lets a process start at all; without it every "
                "command dies before main")
    assert "network" not in text


def test_allow_write_entries_widen_the_grant_and_nothing_else(tmp_path, project):
    extra = tmp_path / "build-out"
    extra.mkdir()
    runner = SandboxedCommand(
        workspace_root=project,
        config=SandboxConfig(enabled=True, allow_write=[str(extra)], max_seconds=20))
    writes = runner.profile().write_paths
    # A caller reads `read_paths` / `write_paths` to see what was granted, so those have to be the
    # *resolved* paths the profile actually carries — on macOS `/var` is a symlink to `/private/var`,
    # and a grant expressed against the unresolved spelling is not the path the kernel checks.
    assert str(extra.resolve()) in writes
    assert str(project) in runner.profile().text()
    # The named root may be written; a path that was not named may not. The negative case deliberately
    # avoids the temp directory, which is granted by design and would make the check pass for free.
    inside = runner.run(_python(f"open({str(extra / 'ok.txt')!r}, 'w').write('x'); print('WROTE')"))
    assert inside.ok, inside.stderr
    assert (extra / "ok.txt").exists()
    blocked_target = _ungranted_path(runner, "not-named.txt")
    blocked = runner.run(_python(f"open({str(blocked_target)!r}, 'w').write('x')"))
    assert blocked.exit_code != 0
    assert not blocked_target.exists()


def test_the_filesystem_root_is_refused_as_an_allow_write_entry(project):
    """`allow_write: ["/"]` looks like an entry and is really "no sandbox". Refuse it, loudly."""
    with pytest.raises(SandboxError) as excinfo:
        SandboxedCommand(workspace_root=project,
                         config=SandboxConfig(enabled=True, allow_write=["/"]))
    assert "root" in str(excinfo.value)
    assert "sandbox.enabled off" in str(excinfo.value) or "enabled off" in str(excinfo.value)


def test_a_relative_allow_write_entry_is_refused(project):
    """A relative entry would be resolved against something other than the workspace."""
    with pytest.raises(SandboxError) as excinfo:
        SandboxedCommand(workspace_root=project,
                         config=SandboxConfig(enabled=True, allow_write=["build/"]))
    assert "absolute" in str(excinfo.value)


# ── running ──────────────────────────────────────────────────────────────────


def test_a_command_runs_and_returns_its_output(runner):
    """The feature's whole point: an agent can check its own work instead of guessing."""
    result = runner.run([sys.executable, "-c", "print('sandboxed hello')"])
    assert result.ok
    assert result.exit_code == 0
    assert "sandboxed hello" in result.stdout
    assert not result.refused and not result.timed_out and not result.truncated


def test_a_failing_command_is_a_result_not_an_exception(runner):
    """A non-zero exit is data the model can act on; an exception would end the node."""
    result = runner.run([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"])
    assert not result.ok
    assert result.exit_code == 3
    assert "boom" in result.stdout
    assert not result.refused


def test_the_command_runs_with_the_workspace_as_cwd(runner, project):
    (project / "marker.txt").write_text("here\n")
    result = runner.run([sys.executable, "-c", "print(open('marker.txt').read().strip())"])
    assert result.ok
    assert "here" in result.stdout


def test_stderr_is_kept_separate_from_stdout(runner):
    """A model reading a test log needs to know which stream a line came from."""
    result = runner.run(_python("import sys", "print('to stdout')",
                                "print('to stderr', file=sys.stderr)"))
    assert result.exit_code == 0
    assert "to stdout" in result.stdout
    assert "to stderr" in result.stderr
    assert "to stderr" not in result.stdout


def test_a_shell_string_is_refused_rather_than_run(runner):
    """A string would need a shell, and a shell is exactly what confinement cannot follow."""
    result = runner.run("echo hi && rm -rf /")
    assert result.refused
    assert not result.ok
    assert result.exit_code is None
    assert "argv" in result.stderr
    assert "shell" in result.stderr


def test_an_empty_argv_is_refused(runner):
    result = runner.run([])
    assert result.refused
    assert "nothing to run" in result.stderr


# ── confinement: the assertions the feature rests on ─────────────────────────


def test_a_write_outside_the_workspace_is_refused_by_the_profile(runner, project):
    """Prove it, do not assert it: attempt the escape and check the file is not there.

    The target is a real writable directory that the profile does *not* grant — the parent of the
    system temp directory, which is mode 0700 and writable by this user — so this is a genuine attempt
    at an escape rather than a symbolic one. A Python-level argv check would pass a weaker version of
    this test while failing to contain a command that spawns a child of its own, which is why the
    assertion is on the filesystem afterwards.
    """
    outside = _ungranted_path(runner, "escape.txt")
    assert not outside.exists()
    result = runner.run(_python(f"open({str(outside)!r}, 'w').write('x'); print('ESCAPED')"))
    assert result.exit_code != 0, "the write was allowed, so the profile is not confining"
    assert "ESCAPED" not in result.stdout
    assert not outside.exists(), "the sandbox let a write escape the workspace"
    assert "Operation not permitted" in result.stderr or "PermissionError" in result.stderr


def _ungranted_path(runner, name: str) -> pathlib.Path:
    """A writable path that is *not* under the workspace and *not* the granted temp directory.

    The distinction matters for the test's honesty: the temp directory is granted, so writing there is
    not an escape and would make an escape test pass for the wrong reason. The parent of `TMPDIR` is a
    real mode-0700 directory this user owns, so a refusal there is the profile doing its job rather
    than the filesystem refusing a path nobody could write anyway.
    """
    candidate = runner.tmpdir().parent / name
    assert candidate.parent != runner.root and not str(candidate).startswith(str(runner.root))
    assert candidate.parent != runner.tmpdir(), "the granted temp dir is not an escape target"
    return candidate


def test_a_write_inside_the_workspace_is_allowed(runner, project):
    """The other half of the previous test: the grant is a boundary, not a blanket refusal."""
    result = runner.run(_python("open('made-by-command.txt', 'w').write('hello')",
                                "print('WROTE')"))
    assert result.ok, result.stderr
    assert (project / "made-by-command.txt").read_text() == "hello"


def test_the_home_directory_cannot_be_read(runner):
    """The literal-root rule must buy a directory entry, not access to the operator's files.

    This is why the root path is granted with `literal` rather than `subpath`: the looser rule also
    makes every command start, and it would make `~/.ssh/id_rsa` readable in the doing. If this test
    ever fails, the confinement has stopped being confinement.
    """
    target = pathlib.Path.home() / ".zshrc"
    assert target.exists(), "this test needs a real file in the home directory to try to read"
    result = runner.run(_python(f"print(open({str(target)!r}).read()[:40])"))
    assert result.exit_code != 0
    assert "Operation not permitted" in result.stderr or "PermissionError" in result.stderr


def test_the_engine_state_directory_cannot_be_written(runner, project):
    """A command that can rewrite the checkpoint can fabricate a resume and defeat idempotency.

    Reads stay allowed — a test run inspecting the checkpoint it is testing is legitimate — so the
    assertion is specifically about the write grant, which is carved around `.agent_state/`.
    """
    state = project / ENGINE_STATE_DIRNAME
    state.mkdir(parents=True, exist_ok=True)
    victim = state / "run_state.json"
    victim.write_text('{"untouched": true}')
    result = runner.run(_python(f"open({str(victim)!r}, 'w').write('tampered'); print('TAMPERED')"))
    assert result.exit_code != 0
    assert victim.read_text() == '{"untouched": true}'
    assert "Operation not permitted" in result.stderr or "PermissionError" in result.stderr


def test_the_temp_directory_is_writable_and_is_not_in_the_workspace(runner, project):
    """`tempfile` must work, or every toolchain using $TMPDIR fails for the wrong reason.

    It must also *not* land inside the workspace. An earlier version staged scratch under
    `.agent_state/`, and `doctor`'s secret-hygiene scan — which walks that whole tree — then found the
    suite's deliberately leaky fixtures and reported leaks the engine never produced. The assertion on
    the path is what stops that regressing.
    """
    result = runner.run(_python("import tempfile",
                                "f = tempfile.NamedTemporaryFile(delete=False)",
                                "f.write(b'x'); f.close()",
                                "print('TMP-OK', f.name)"))
    assert result.ok, result.stderr
    assert "TMP-OK" in result.stdout
    assert str(project) not in result.stdout, (
        "scratch inside the workspace pollutes the tree the engine scans for leaks")
    assert str(pathlib.Path(result.stdout.split("TMP-OK", 1)[1].strip()).resolve()) \
        .startswith(str(runner.tmpdir())), "the temp file must be where the profile says it may go"


def test_an_agent_can_run_the_test_suite_it_just_wrote(project):
    """The scenario the whole feature exists for, end to end.

    The agent's situation is: write a module, write a test for it, and find out whether it works.
    `tools.py` could do the first two and not the third, which left the agent guessing about its own
    output. This writes both files, runs the suite *from inside the sandbox*, and asserts the failure
    comes back as text a model can act on — including the deliberate failing case, because a runner
    that only reports success is not a runner.
    """
    (project / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (project / "test_calc.py").write_text(
        "import unittest\n"
        "from calc import add\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(2, 3), 5)\n"
        "    def test_add_wrong(self): self.assertEqual(add(2, 2), 5)\n")
    runner = SandboxedCommand(workspace_root=project,
                              config=SandboxConfig(enabled=True, max_seconds=60))
    result = runner.run([sys.executable, "-m", "unittest", "test_calc", "-v"])
    assert result.exit_code == 1, "the deliberately failing assertion must reach the caller"
    assert "test_add (test_calc.T.test_add) ... ok" in result.stderr
    assert "AssertionError: 4 != 5" in result.stderr, (
        "the failure detail is the part the model needs; a bare exit code teaches it nothing")
    assert not result.truncated and not result.timed_out


# ── network ──────────────────────────────────────────────────────────────────


def test_network_is_denied_by_default(runner):
    """Off by default: a build that needs to fetch is a decision, not an accident."""
    result = runner.run(_python(
        "import socket",
        "socket.create_connection(('1.1.1.1', 443), timeout=4)",
        "print('CONNECTED')"), timeout_s=20)
    assert result.exit_code != 0
    assert "CONNECTED" not in result.stdout
    assert "Operation not permitted" in result.stderr or "PermissionError" in result.stderr


def test_network_is_granted_when_it_is_asked_for(project):
    """The denial must be a policy, not a profile that happens to break every socket.

    A loopback listener is used rather than a real host: the test needs to prove the *grant* works
    without making the suite depend on the network being reachable.
    """
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    accepted = threading.Event()

    def _accept() -> None:
        try:
            conn, _ = server.accept()
            conn.close()
        except OSError:
            pass
        finally:
            accepted.set()

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    try:
        allowed = SandboxedCommand(
            workspace_root=project,
            config=SandboxConfig(enabled=True, allow_network=True, max_seconds=20))
        result = allowed.run(_python(
            "import socket",
            f"s = socket.create_connection(('127.0.0.1', {port}), timeout=4)",
            "s.close()",
            "print('CONNECTED')"))
        assert result.ok, result.stderr
        assert "CONNECTED" in result.stdout
    finally:
        server.close()
        thread.join(timeout=5)


# ── the bounds ───────────────────────────────────────────────────────────────


def test_max_seconds_actually_kills_a_long_command(project):
    """A ceiling that does not stop the process is not a ceiling."""
    import time

    runner = SandboxedCommand(
        workspace_root=project,
        config=SandboxConfig(enabled=True, max_seconds=2, max_output_bytes=4096))
    started = time.monotonic()
    result = runner.run(_python("import time", "print('started', flush=True)",
                                "time.sleep(60)", "print('never')"))
    elapsed = time.monotonic() - started
    assert result.timed_out
    assert not result.ok
    assert result.exit_code is None, (
        "a command stopped at the ceiling did not exit, and reporting an exit code would invent one")
    assert elapsed < 30, "the ceiling did not stop the command"
    assert "ceiling" in result.stderr


def test_the_ceiling_stops_a_command_that_spawns_children(project):
    """`make` killed alone leaves its compilers running and holding the pipes.

    The kill has to take the whole process group, or the timeout becomes the hang it was supposed to
    prevent. The command here sleeps in a grandchild, which is the shape a build actually has.
    """
    import time

    runner = SandboxedCommand(
        workspace_root=project,
        config=SandboxConfig(enabled=True, max_seconds=2, max_output_bytes=4096))
    started = time.monotonic()
    result = runner.run(["/bin/sh", "-c", "sleep 60"], timeout_s=2)
    assert result.timed_out
    assert time.monotonic() - started < 30


def test_output_is_truncated_and_the_result_says_so(project):
    """A silently clipped test log is a model reasoning about output it never saw."""
    runner = SandboxedCommand(
        workspace_root=project,
        config=SandboxConfig(enabled=True, max_seconds=20, max_output_bytes=500))
    result = runner.run(_python("print('x' * 5000)"))
    assert result.truncated, "the output cap did not bite"
    assert len(result.stdout.encode("utf-8")) <= 600
    assert "truncated" in result.stderr.lower()
    assert "x" * 400 in result.stdout, "truncation keeps the beginning of the stream"


def test_a_command_under_the_cap_is_not_flagged_as_truncated(runner):
    """The flag has to mean something: a normal short result must not cry wolf."""
    result = runner.run([sys.executable, "-c", "print('short')"])
    assert not result.truncated


def test_a_working_directory_outside_the_workspace_is_refused(runner, tmp_path):
    """A command whose cwd is outside the grant fails confusingly; refusing it is clearer.

    Returned as a refusal rather than raised, like every other refusal here: the caller gets a reason
    it can act on instead of an exception that ends the node.
    """
    result = runner.run([sys.executable, "-c", "print('x')"], cwd=tmp_path)
    assert result.refused
    assert not result.ok
    assert "outside the workspace" in result.stderr


# ── the preflight ────────────────────────────────────────────────────────────


def test_the_preflight_refuses_when_no_backend_exists(project, monkeypatch):
    """The refusal that matters most: no confinement must mean no command.

    Degrading to an unconfined `subprocess` here would keep the promise "your command ran" while
    silently dropping the promise "your command was confined" — and only one of those was the point.
    """
    runner = SandboxedCommand(workspace_root=project, config=SandboxConfig(enabled=True))
    monkeypatch.setattr(runner, "backend", None)
    reason = runner.preflight()
    assert reason is not None
    assert "no confinement backend" in reason
    assert SANDBOX_EXEC in reason, "the refusal must name what it looked for"


def test_a_run_without_a_backend_refuses_instead_of_running_unconfined(project, monkeypatch):
    """Asserted at the filesystem: the command must not have executed at all."""
    runner = SandboxedCommand(workspace_root=project, config=SandboxConfig(enabled=True))
    monkeypatch.setattr(runner, "backend", None)
    canary = project / "it-ran.txt"
    result = runner.run(_python(f"open({str(canary)!r}, 'w').write('x'); print('RAN')"))
    assert result.refused
    assert not result.ok
    assert not canary.exists(), "the command ran unconfined after a failed preflight"
    assert "no confinement backend" in result.stderr


def test_the_preflight_passes_where_a_backend_exists(runner):
    if runner.backend is None:
        pytest.skip("no confinement backend on this platform")
    assert runner.preflight() is None


# ── the tool ─────────────────────────────────────────────────────────────────


def test_the_tool_is_absent_when_the_sandbox_is_disabled(project):
    """A model must never be tempted by a tool it would only be refused."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*", "write:*", "exec:*"),
                            sandbox=SandboxConfig(enabled=False))
    assert "run_command" not in registry.names()
    assert "run_command" not in [spec.name for spec in registry.specs()]


def test_the_tool_is_absent_without_a_sandbox_section_at_all(project):
    """The engine's default posture: no config section means no shell, exactly as before."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*", "exec:*"))
    assert "run_command" not in registry.names()


def test_the_tool_is_offered_when_enabled_and_ordered_with_the_rest(project):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*", "exec:*"),
                            sandbox=SandboxConfig(enabled=True))
    assert "run_command" in registry.names()
    assert [spec.name for spec in registry.specs()] == registry.names(), (
        "a reordering of the tool schemas is a cache miss, so the order must not drift")
    spec = next(spec for spec in registry.specs() if spec.name == "run_command")
    assert "argv" in str(spec.parameters), "the schema must ask for an argv list, not a string"


def test_the_tool_runs_a_command_and_returns_the_output(project):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*", "write:*", "exec:*"),
                            sandbox=SandboxConfig(enabled=True, max_seconds=20))
    result = registry.call("run_command", {"argv": [sys.executable, "-c", "print('via tool')"]})
    assert result.ok
    assert "via tool" in result.text
    assert "$ " in result.text, "the result must show what was run, not only what came back"


def test_the_tool_refuses_an_agent_without_the_exec_capability(project):
    """The refusal must name what was required and what the agent holds, so it can adapt."""
    registry = ToolRegistry(workspace_root=project,
                            agent=make_agent("read:*", "write:*"),
                            sandbox=SandboxConfig(enabled=True))
    result = registry.call("run_command", {"argv": [sys.executable, "-c", "print('nope')"]})
    assert not result.ok
    assert "exec" in result.text
    assert "required" in result.text
    assert "held" in result.text
    assert "read:*, write:*" in result.text
    assert "Do not retry this call" in result.text
    assert EXEC_CAPABILITY == "exec", "the marker `delegation.elevated_markers()` names is `exec:`"


def test_exec_is_a_distinct_capability_not_a_read_or_write_grant(project):
    """`exec:` has to be its own namespace, or the gate is decorative.

    The check is over the namespace prefix rather than over a path: a command's reach is not knowable
    from its argv, so a per-path grant would be a specificity the check cannot honestly deliver. What
    it can deliver is "this agent, and not that one, may run things here".
    """
    from engine.tools import ToolRegistry as _Registry

    unprivileged = _Registry(workspace_root=project, agent=make_agent("read:*"),
                             sandbox=SandboxConfig(enabled=True))
    assert not unprivileged.call("run_command", {"argv": ["true"]}).ok
    privileged = _Registry(workspace_root=project, agent=make_agent("exec:*"),
                           sandbox=SandboxConfig(enabled=True, max_seconds=20))
    assert privileged.call("run_command", {"argv": [sys.executable, "-c", "print(1)"]}).ok


def test_read_and_write_grants_do_not_confer_exec(project):
    """Running a command is its own grant. A wildcard read/write must not imply it."""
    registry = ToolRegistry(workspace_root=project,
                            agent=make_agent("read:*", "write:*"),
                            sandbox=SandboxConfig(enabled=True))
    assert not registry.call("run_command", {"argv": [sys.executable, "-c", "print(1)"]}).ok
    granted = ToolRegistry(workspace_root=project, agent=make_agent("read:*", "write:*", "exec:*"),
                           sandbox=SandboxConfig(enabled=True, max_seconds=20))
    assert granted.call("run_command", {"argv": [sys.executable, "-c", "print(1)"]}).ok


def test_a_read_only_run_refuses_the_tool_whatever_the_capability(project):
    """`read_only` is a property of the run, so it has to bind here as it does for writes."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("read:*", "exec:*"),
                            read_only=True, sandbox=SandboxConfig(enabled=True))
    result = registry.call("run_command", {"argv": [sys.executable, "-c", "print('nope')"]})
    assert not result.ok
    assert "read-only" in result.text


def test_the_tool_refuses_a_shell_string_with_an_explanation(project):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("exec:*"),
                            sandbox=SandboxConfig(enabled=True))
    result = registry.call("run_command", {"argv": "make test"})
    assert not result.ok
    assert "argv" in result.text
    assert "shell" in result.text


def test_the_tool_reports_a_failing_command_without_raising(project):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("exec:*"),
                            sandbox=SandboxConfig(enabled=True, max_seconds=20))
    result = registry.call("run_command",
                           {"argv": [sys.executable, "-c", "import sys; sys.exit(7)"]})
    assert not result.ok
    assert "exit 7" in result.text


def test_the_tool_refuses_an_impossible_timeout(project):
    registry = ToolRegistry(workspace_root=project, agent=make_agent("exec:*"),
                            sandbox=SandboxConfig(enabled=True))
    assert not registry.call("run_command", {"argv": ["true"], "timeout_s": 0}).ok
    assert not registry.call("run_command", {"argv": ["true"], "timeout_s": "soon"}).ok


def test_the_tool_confines_a_write_the_model_asks_for(project):
    """End to end through the tool: the escape must fail and the workspace write must land."""
    registry = ToolRegistry(workspace_root=project, agent=make_agent("exec:*"),
                            sandbox=SandboxConfig(enabled=True, max_seconds=20))
    outside = _ungranted_path(
        SandboxedCommand(workspace_root=project, config=SandboxConfig(enabled=True)),
        "escape-via-tool.txt")
    refused = registry.call("run_command", {"argv": _python(
        f"open({str(outside)!r}, 'w').write('x'); print('ESCAPED')")})
    assert not refused.ok
    assert not outside.exists()
    inside = registry.call("run_command", {"argv": _python(
        "open('by-tool.txt', 'w').write('ok'); print('WROTE')")})
    assert inside.ok
    assert (project / "by-tool.txt").read_text() == "ok"


def test_run_command_is_not_advertised_by_the_default_configuration(config):
    """The shipped configuration must not hand out a shell by accident."""
    if config.sandbox.enabled:
        pytest.skip("this checkout has explicitly enabled the sandbox")
    registry = ToolRegistry(workspace_root=pathlib.Path.cwd(), agent=make_agent("read:*"),
                            sandbox=config.sandbox)
    assert "run_command" not in registry.names()


# ── resource lifetime: the trace, and the runner ─────────────────────────────
#
# The two resources this engine had no bound on, and one rule for both: what a run creates must not
# outlive the run, and a file that grows must not grow for ever. The trace is here because the defect
# was invisible — an engine running for days appends to it without limit, and the readers of it are
# not all tail-readers. The runner is here because the defect was expensive — a runner that outlives
# its engine keeps executing and keeps spending against the same checkpoint.


def test_the_trace_is_bounded_and_a_line_never_tears(tmp_path):
    """A long-lived engine's trace stays under its ceiling, and every line of it is whole.

    The bound is the fix; the line-boundary assertion is what makes the fix usable, because a
    compaction that cut mid-record would leave a file whose first line is a fragment — a record the
    engine cannot read back and a person cannot read at all.
    """
    trace = tmp_path / "trace.jsonl"
    bus = EventBus(run_id="run_t", trace_path=trace, trace_max_bytes=64_000)
    for index in range(4_000):
        bus.emit("node.enter", node_id=f"n{index}", payload={"summary": "x" * 200})
    bus.close()

    size = trace.stat().st_size
    assert size <= 64_000, f"the trace grew past its ceiling: {size} bytes"
    assert bus.compacted_events > 0, "nothing was compacted, so the bound was never exercised"
    # And the default the engine actually runs with is a real bound, not "no bound unless asked".
    assert 0 < MAX_TRACE_BYTES <= 64 * 1024 * 1024
    raw = trace.read_bytes()
    assert raw.startswith(b"{"), "the file begins mid-record: the cut was not on a line boundary"
    assert raw.endswith(b"\n")
    lines = raw.decode("utf-8").splitlines()
    assert len(lines) > 1
    for line in lines:
        json.loads(line)                      # a torn line would raise here
    # The events a reader wants are the ones that survive: `load_trace` (what the accounting path
    # reads) and the tail readers both take the most recent records, so the oldest are what goes.
    events = load_trace(trace)
    assert events, "compaction left nothing readable"
    assert "node.enter" in events[-1].type_value


def test_a_trace_written_by_two_threads_keeps_every_line_whole(tmp_path):
    """The same property under concurrency: one line per `write`, and no interleaving.

    This was reported as a torn-write defect and is not one — the bus's reentrant lock already
    serialised writers within the process, and `O_APPEND` serialised them across processes. It is
    pinned here anyway, because the property is now structural (one `os.write` per line under the
    file lock) rather than a consequence of how `TextIOWrapper` happens to buffer, and a regression
    in that would be the kind of thing nobody notices until a trace will not parse.
    """
    import threading

    trace = tmp_path / "trace.jsonl"
    bus = EventBus(run_id="run_t", trace_path=trace, trace_max_bytes=64_000)

    def write(worker: int) -> None:
        for index in range(400):
            bus.emit("node.enter", node_id=f"w{worker}-{index}", payload={"pad": "y" * 120})

    threads = [threading.Thread(target=write, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    bus.close()

    raw = trace.read_bytes()
    assert raw.startswith(b"{") and raw.endswith(b"\n")
    parsed = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    assert parsed, "every line was torn away"
    assert len({record["seq"] for record in parsed}) == len(parsed), "a line was duplicated"


def test_a_compaction_keeps_the_budget_a_round_reads_spend_from(tmp_path):
    """`orchestrator.cost_snapshot` reads the *last* `llm.response`; compaction must not take it.

    The reader contract, not an implementation detail: the whole reason the trace is compacted from
    the head rather than rotated away is that every reader of it wants the recent end.
    """
    trace = tmp_path / "trace.jsonl"
    bus = EventBus(run_id="run_t", trace_path=trace, trace_max_bytes=32_000)
    for index in range(500):
        bus.emit("agent.log", payload={"line": "z" * 300})
    bus.emit("llm.response", payload={"budget": {"total_usd": 1.25, "calls": 7}})
    bus.close()

    events = load_trace(trace)
    budgets = [event.payload["budget"] for event in events
               if event.type_value == "llm.response"]
    assert budgets == [{"total_usd": 1.25, "calls": 7}], "the accounting record was compacted away"


def test_a_trace_over_its_ceiling_is_rewritten_not_rotated(tmp_path):
    """The trace keeps its name and its inode, so a second writer's descriptor stays valid.

    Two processes write one trace: the engine's bus and the runner subprocess's. Rotation
    (`os.replace`, or unlink and recreate) would leave whichever of them wrote second appending to an
    inode nobody reads — silently losing half the run's events, which is worse than a large file.
    """
    trace = tmp_path / "trace.jsonl"
    first = EventBus(run_id="run_a", trace_path=trace, trace_max_bytes=20_000)
    second = EventBus(run_id="run_b", trace_path=trace, trace_max_bytes=20_000)
    inode = trace.stat().st_ino
    for index in range(400):
        first.emit("node.enter", node_id=f"a{index}", payload={"pad": "a" * 200})
    assert first.compacted_events > 0
    for index in range(20):
        second.emit("node.exit", node_id=f"b{index}")      # written after the compaction
    first.close()
    second.close()

    assert trace.stat().st_ino == inode, "the trace was replaced, orphaning the other writer"
    lines = trace.read_bytes().decode("utf-8").splitlines()
    for line in lines:
        json.loads(line)
    assert any(json.loads(line)["type"] == "node.exit" for line in lines), (
        "the second writer's events were lost")


# ── the runner does not outlive the engine ───────────────────────────────────

#: A stand-in for the pinned workflow runner. It does the one thing about the real runner that
#: matters here — it loads the `--executor` plugin the host generated, which is where the
#: parent-death guard lives — then reports its pid and waits.
_FAKE_RUNNER = '''#!/usr/bin/env python3
import argparse, importlib.util, os, signal, sys, time

parser = argparse.ArgumentParser()
parser.add_argument("--manifest"); parser.add_argument("--executor")
parser.add_argument("--guardrail"); parser.add_argument("--state")
parser.add_argument("--enforce-contracts", action="store_true")
parser.add_argument("--contract-rework")
args, _unknown = parser.parse_known_args()
if args.executor:
    spec = importlib.util.spec_from_file_location("generated_executor", args.executor)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
print("RUNNER pid=%d ppid=%d" % (os.getpid(), os.getppid()), file=sys.stderr, flush=True)
while True:
    time.sleep(0.2)
'''

#: An engine process reduced to the part under test: it spawns a runner and then either returns from
#: main (so `atexit` runs) or waits to be killed (the path nothing inside it can cover).
_ENGINE = '''#!/usr/bin/env python3
import pathlib, sys, threading, time
from types import SimpleNamespace

REPO, WORKSPACE, FAKE, MODE, GUARD = sys.argv[1:6]
WORKSPACE, FAKE = pathlib.Path(WORKSPACE), pathlib.Path(FAKE)
sys.path.insert(0, REPO)
from engine.host import RunnerHost

workspace = WORKSPACE.resolve()
workspace.mkdir(parents=True, exist_ok=True)
(workspace / ".agent_state").mkdir(parents=True, exist_ok=True)
(workspace / "m.yaml").write_text("name: probe\\nstart: dev\\nnodes:\\n  - id: dev\\n")
library = SimpleNamespace(files=SimpleNamespace(runner=FAKE, root=FAKE.parent))
# `--executor` last wins, so a plugin without the parent-death guard isolates the *engine's* reaper
# as the only thing that can stop the runner. Without that, the two halves of the fix cover for each
# other and a test cannot tell which one worked.
extra = [] if GUARD == "guard" else ["--executor", str(FAKE.parent / "noguard_executor.py")]
host = RunnerHost(config=None, library=library, workspace=workspace,
                  on_stderr=lambda line: print("CHILD:" + line, flush=True))
threading.Thread(target=host.run, kwargs={"manifest_path": workspace / "m.yaml",
                                          "run_id": "run_p", "extra_args": extra},
                 daemon=True).start()
time.sleep(2.0)
print("ENGINE-READY", flush=True)
if MODE == "hold":
    while True:
        time.sleep(0.5)
print("ENGINE-EXITING", flush=True)
'''

#: An executor plugin with no parent-death guard: what the engine generated before this fix.
_NOGUARD_EXECUTOR = '''CRITERIA = []


def execute_node(node_id, state, ctx):
    return {"status": "done"}


def summary():
    return {}
'''


def _spawn_engine(tmp_path, *, mode: str, guard: str) -> tuple[subprocess.Popen, int]:
    """Start an engine process with a live runner under it. Returns (engine, runner pid).

    `guard` is `"guard"` for the plugin the host generates (with the parent-death guard in it) or
    `"noguard"` for a plugin without one, which is how each half of the fix is tested on its own.
    """
    fake = tmp_path / "fake_runner.py"
    fake.write_text(_FAKE_RUNNER, encoding="utf-8")
    (tmp_path / "noguard_executor.py").write_text(_NOGUARD_EXECUTOR, encoding="utf-8")
    script = tmp_path / "engine_under_test.py"
    script.write_text(_ENGINE, encoding="utf-8")
    engine = subprocess.Popen(
        [sys.executable, str(script), str(ROOT), str(tmp_path / "projects" / "probe"),
         str(fake), mode, guard],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    runner_pid = 0
    deadline = time.time() + 60
    while time.time() < deadline:
        line = engine.stdout.readline()
        if not line:
            break
        if "RUNNER pid=" in line:
            runner_pid = int(line.split("pid=")[1].split()[0])
        if runner_pid and "ENGINE-READY" in line:
            break
    assert runner_pid, "the engine never reported a runner pid"
    assert _pid_alive(runner_pid), "the runner was not alive when the engine was ready"
    return engine, runner_pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_death(pid: int, timeout_s: float = 8.0) -> float | None:
    """Seconds until the pid is gone, or None if it outlived the timeout."""
    started = time.time()
    while time.time() - started < timeout_s:
        if not _pid_alive(pid):
            return time.time() - started
        time.sleep(0.05)
    return None


def test_a_live_runner_does_not_outlive_a_cleanly_exiting_engine(tmp_path):
    """The engine's own way out reaps what it started, with no cooperation from the runner.

    This is the Stop case: the engine returns from main, `atexit` runs `shutdown_runners`, and the
    runner's whole process group is signalled. Without it the run keeps executing and keeps spending
    against the same checkpoint while the app shows it as stopped.

    The runner here carries no parent-death guard of its own, so the engine's reaper is the only thing
    that can stop it — the two halves are proved separately, because a test both halves can satisfy
    proves neither.
    """
    engine, runner_pid = _spawn_engine(tmp_path, mode="exit", guard="noguard")
    try:
        engine.wait(timeout=30)
        assert engine.returncode == 0
        died = _wait_for_death(runner_pid)
        assert died is not None, "the runner outlived a clean engine exit"
        assert died < 5.0, f"the runner took {died:.1f}s to go"
    finally:
        if engine.poll() is None:
            engine.kill()
        if _pid_alive(runner_pid):
            os.kill(runner_pid, signal.SIGKILL)


def test_a_live_runner_does_not_outlive_a_killed_engine(tmp_path):
    """The half no parent-side handler can cover: SIGKILL runs nothing inside the engine.

    So the guard lives in the child — in the executor plugin the host generates, which the runner
    loads at startup — and watches for its parent to change. Proved against a real killed process
    rather than a simulated one, because the failure this replaces was a real orphan.
    """
    engine, runner_pid = _spawn_engine(tmp_path, mode="hold", guard="guard")
    try:
        engine.send_signal(signal.SIGKILL)
        engine.wait(timeout=10)
        died = _wait_for_death(runner_pid)
        assert died is not None, "the runner survived its engine being killed — it is still spending"
        assert died < 5.0, f"the guard took {died:.1f}s to notice"
    finally:
        if engine.poll() is None:
            engine.kill()
        if _pid_alive(runner_pid):
            os.kill(runner_pid, signal.SIGKILL)


def test_the_stderr_tail_is_read_after_the_pipe_readers_have_finished(tmp_path):
    """The last line the runner printed is the explanation, and it arrives on another thread.

    Deterministic on purpose: the drain here is *made* slow, so a supervisor that reads the tail
    before waiting for its readers reports an empty tail every time rather than occasionally. That
    ordering was the defect — the join existed but ran after the slice, which is why it looked right.
    """
    import time as _time

    class _SlowStderrDrainer(RunnerHost):
        """Reads the pipe, then records it late — a reader that has not caught up when the child dies."""

        def _drain_stderr(self, handle):
            lines = [line.rstrip("\n") for line in handle.process.stderr]
            _time.sleep(0.4)
            handle.stderr_lines.extend(lines)

    fake = tmp_path / "fake_runner.py"
    fake.write_text(
        "import sys, time\nprint('the manifest names a node that does not exist', file=sys.stderr)\n"
        "time.sleep(0.05)\nsys.exit(1)\n", encoding="utf-8")
    workspace = tmp_path / "projects" / "probe"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "m.yaml").write_text("name: probe\nstart: dev\nnodes:\n  - id: dev\n")
    library = SimpleNamespace(files=SimpleNamespace(runner=fake, root=fake.parent))
    host = _SlowStderrDrainer(config=None, library=library, workspace=workspace)
    outcome = host.run(manifest_path=workspace / "m.yaml", run_id="run_t", workflow="probe")

    assert outcome.exit_code == 1
    assert "the manifest names a node that does not exist" in outcome.stderr_tail, (
        "the tail was read before the reader that collects it had finished")
    assert "the manifest names a node that does not exist" in outcome.error


def test_the_per_run_plugin_files_do_not_leak(tmp_path):
    """The generated plugins land in the project directory, so they are removed when the run ends.

    They are imported once, at the runner's startup; after the process is gone nothing reads them, and
    a pair per run is how a project accumulates a directory of `executor_run_*.py` nobody will ever
    look at again.
    """
    class _NoopHost(RunnerHost):
        """A host that spawns nothing, so the cleanup is asserted without waiting for a run."""

    fake = tmp_path / "fake_runner.py"
    fake.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    workspace = tmp_path / "projects" / "probe"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "m.yaml").write_text("name: probe\nstart: dev\nnodes:\n  - id: dev\n")
    library = SimpleNamespace(files=SimpleNamespace(runner=fake, root=fake.parent))
    host = _NoopHost(config=None, library=library, workspace=workspace)
    host.run(manifest_path=workspace / "m.yaml", run_id="run_t", workflow="probe")

    leftovers = sorted(path.name for path in workspace.glob("executor_run_t*.py"))
    leftovers += sorted(path.name for path in workspace.glob("guardrail_run_t*.py"))
    leftovers += sorted(path.name for path in workspace.glob("*.tmp.*"))
    assert leftovers == [], f"the run left its plugin files behind: {leftovers}"
    assert not list((workspace / ".agent_state").glob("*.tmp.*"))

# ── the stall window, and the reason a stopped run gives ─────────────────────
#
# Both halves of the same report, reproduced with a real goal:
#
#     AGENTORG_CREDENTIALS=…/ollama-creds.json python3 -u -m engine.cli run \
#         --goal "create a file notes.md …" --root /tmp/finishprobe --slug probe --posture unattended
#
#     outcome : failed   steps : None   phase : aborted   stopped : the run was aborted
#
# 901 seconds against `stall_timeout_s: 900.0`, a window compiled into `engine/host.py` that no
# configuration could raise — so an engine that supports a local model supervised as though every
# model answered in seconds, and a person running one got an abort with no cause and no setting.
#
# The abort itself was correct. What is tested here is that the window is now the person's number and
# that whatever stops a run says *which* of four things it was.

#: A runner that does nothing at all: no output, no checkpoint, no trace. It is the wedge the watchdog
#: exists to catch, and it is what a model reply that never returns looks like from outside — the
#: executor writes a trace entry when a call *returns*, so a call still generating is silence.
_SILENT_RUNNER = '''#!/usr/bin/env python3
import time

while True:
    time.sleep(0.2)
'''


def _library_for(tmp_path, fake: pathlib.Path):
    return SimpleNamespace(files=SimpleNamespace(runner=fake, root=fake.parent))


def _workspace_for(tmp_path, name: str = "stall"):
    workspace = tmp_path / "projects" / name
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "m.yaml").write_text("name: probe\nstart: dev\nnodes:\n  - id: dev\n")
    (workspace / ".agent_state").mkdir(parents=True, exist_ok=True)
    return workspace


def _config_with(tmp_path, **overrides):
    """The example credentials with the `concurrency` block edited, loaded through the real loader.

    Loaded from the *file* rather than built in memory, because the question is whether the setting
    reaches the host — a `ConcurrencyConfig` constructed by hand would prove the dataclass and not the
    path a person actually uses.
    """
    document = json.loads((ROOT / "credentials.example.json").read_text(encoding="utf-8"))
    document["concurrency"].update(overrides)
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load(path)


def test_the_stall_window_comes_from_the_config_file(tmp_path):
    """The window is a setting, not a constant in `host.py`.

    Before this, `RunnerHost.stall_timeout_s` defaulted to 900 and nothing read the config: a person
    whose local model was slower than that had no way to say so, and the abort they got could not be
    explained by any file they could read. `command_surface()` reports the number actually in force,
    which is the one they need to see.
    """
    config = _config_with(tmp_path, stall_timeout_s=1800)
    assert config.concurrency.stall_timeout_s == 1800

    fake = tmp_path / "fake_runner.py"
    fake.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    host = RunnerHost(config=config, library=_library_for(tmp_path, fake),
                      workspace=_workspace_for(tmp_path, "from-config"))

    assert host.stall_timeout_s == 1800, "the host must take the window from the config"
    assert host.command_surface()["stall_timeout_s"] == 1800


def test_a_host_built_without_a_window_still_honours_the_file(tmp_path):
    """A caller that passes nothing gets the configured window rather than a compiled-in one.

    The two hosts a run is built from pass it explicitly, but `RunnerHost` is also built directly —
    by tests, and by anything that supervises a graph without an orchestrator. A `None` here must mean
    "ask the config", not "fall back to a constant nothing can change".
    """
    config = _config_with(tmp_path, stall_timeout_s=600)
    fake = tmp_path / "fake_runner.py"
    fake.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    host = RunnerHost(config=config, library=_library_for(tmp_path, fake),
                      workspace=_workspace_for(tmp_path, "unset"))

    assert host.stall_timeout_s == 600


def test_the_config_refuses_a_stall_window_that_cannot_be_one():
    """An invalid window is refused at load, with a message naming the field and the value.

    The two ways to get it wrong are both silent if accepted: zero or negative would stop every run at
    the first poll, and a window at or below twice the heartbeat would fire before the monitor had
    even called the run `slow` — so the run would be killed as a stall without ever having been
    reported as one.
    """
    from engine.config import ConfigError, ConcurrencyConfig

    for bad in (0, -1, -900.0):
        with pytest.raises(ConfigError) as excinfo:
            ConcurrencyConfig(stall_timeout_s=bad)
        assert "concurrency.stall_timeout_s" in str(excinfo.value)
        assert str(bad) in str(excinfo.value), "the offending value must be echoed back"

    with pytest.raises(ConfigError) as excinfo:
        ConcurrencyConfig(heartbeat_s=30, stall_timeout_s=45)
    assert "2 × heartbeat_s" in str(excinfo.value)


def test_an_invalid_stall_window_in_the_file_is_refused_by_the_loader(tmp_path):
    """The refusal reaches the person through `load`, not only through the dataclass."""
    from engine.config import ConfigError

    with pytest.raises(ConfigError) as excinfo:
        _config_with(tmp_path, stall_timeout_s=0)
    assert "concurrency.stall_timeout_s" in str(excinfo.value)


def test_a_stall_records_the_watchdog_and_the_window_that_expired(tmp_path):
    """The run stops, and `outcome.termination` says what stopped it and which window ran out.

    A `killed` boolean is all a summary had before, and four different events set it — so this is the
    assertion that the summary can now name a stall rather than reporting the same sentence as an
    Owner's abort. `wedged` is the word `wedged()` already uses for this exact condition.
    """
    from engine.host import TERMINATION_WEDGED, RunnerState

    fake = tmp_path / "silent_runner.py"
    fake.write_text(_SILENT_RUNNER, encoding="utf-8")
    workspace = _workspace_for(tmp_path, "stalls")
    events: list[str] = []
    host = RunnerHost(config=None, library=_library_for(tmp_path, fake), workspace=workspace,
                      heartbeat_s=0.2, grace_s=0.5, stall_timeout_s=2.0,
                      on_event=lambda event, payload: events.append(event))

    outcome = host.run(manifest_path=workspace / "m.yaml", run_id="run_stall", workflow="probe")

    assert outcome.killed and outcome.state is RunnerState.FAILED, "a stall must still stop the run"
    assert outcome.termination == TERMINATION_WEDGED
    assert "stall window" in outcome.termination_detail
    assert "2s" in outcome.termination_detail, (
        "the detail must carry the window that expired, or the reader still cannot act on it")
    assert "concurrency.stall_timeout_s" in outcome.termination_detail
    assert "watchdog.stall" in events
    assert outcome.as_dict()["termination"] == TERMINATION_WEDGED


def test_an_owner_abort_records_the_owner_and_not_the_watchdog(tmp_path):
    """The same `killed` flag, a different cause — which is the whole point of the change.

    A person who pressed Stop and a person whose run was reaped by the watchdog used to read the same
    sentence. The liveness word is on the outcome too, so the two cases cannot be confused even when
    both happened close together.
    """
    from engine.host import TERMINATION_ABORTED

    fake = tmp_path / "silent_runner.py"
    fake.write_text(_SILENT_RUNNER, encoding="utf-8")
    workspace = _workspace_for(tmp_path, "aborted")
    events: list[str] = []
    # A stall window far beyond the test's own patience: if this run is stopped as a stall, the test
    # is asserting the wrong thing rather than merely failing.
    host = RunnerHost(config=None, library=_library_for(tmp_path, fake), workspace=workspace,
                      heartbeat_s=0.2, grace_s=0.5, stall_timeout_s=300.0,
                      on_event=lambda event, payload: events.append(event))
    outcome: list = []
    thread = threading.Thread(target=lambda: outcome.append(host.run(
        manifest_path=workspace / "m.yaml", run_id="run_abort", workflow="probe")), daemon=True)
    thread.start()
    for _ in range(100):
        if host.running:
            break
        time.sleep(0.05)
    assert host.running, "the runner never started"
    assert host.abort()
    thread.join(timeout=20)
    assert not thread.is_alive(), "the abort did not stop the run"

    assert outcome[0].killed
    assert outcome[0].termination == TERMINATION_ABORTED
    assert "watchdog.stall" not in events, "the Owner's own stop is not a stall"


def test_the_shutdown_reaper_records_the_engine_as_the_cause(tmp_path):
    """An engine that stops its runners says so, rather than leaving an unattributable abort behind.

    This is the half of "why was my run stopped" that no policy produced: the process went away and
    took its runners with it. Recorded as a shutdown, because describing it as an abort by the Owner
    would name a decision nobody made.
    """
    from engine.host import TERMINATION_SHUTDOWN, shutdown_runners

    fake = tmp_path / "silent_runner.py"
    fake.write_text(_SILENT_RUNNER, encoding="utf-8")
    workspace = _workspace_for(tmp_path, "shutdown")
    host = RunnerHost(config=None, library=_library_for(tmp_path, fake), workspace=workspace,
                      heartbeat_s=0.2, grace_s=2.0, stall_timeout_s=300.0)
    outcome: list = []
    thread = threading.Thread(target=lambda: outcome.append(host.run(
        manifest_path=workspace / "m.yaml", run_id="run_shutdown", workflow="probe")), daemon=True)
    thread.start()
    for _ in range(100):
        if host.running:
            break
        time.sleep(0.05)
    assert host.running, "the runner never started"

    handle = host._handle
    killed = shutdown_runners(grace_s=1.0)
    thread.join(timeout=20)
    assert not thread.is_alive(), "the reaper did not stop the run"

    assert handle.pid in killed
    assert handle.termination == TERMINATION_SHUTDOWN
    assert "shut down" in handle.termination_detail
    assert outcome[0].termination == TERMINATION_SHUTDOWN


def test_the_first_cause_of_a_stop_is_the_one_recorded(tmp_path):
    """A later stop must not overwrite the earlier one, because the first is the operative cause.

    A shutdown that SIGKILLs a run the watchdog had already stopped is still a stall. Letting the last
    writer win would hide the thing the person needs to act on behind the thing that merely finished it
    off, which is the same failure as having one sentence for four causes.
    """
    from engine.host import (TERMINATION_ABORTED, TERMINATION_WEDGED, RunHandle, _note_termination)

    handle = RunHandle(run_id="r", process=SimpleNamespace(pid=1),
                       manifest_path=tmp_path / "m.yaml", state_path=tmp_path / "s.json")
    _note_termination(handle, TERMINATION_WEDGED, "no work for 1800s")
    _note_termination(handle, TERMINATION_ABORTED)

    assert handle.termination == TERMINATION_WEDGED
    assert handle.termination_detail == "no work for 1800s"
