#!/usr/bin/env python3
"""Phase 43 tests — shell completion, generated from the argument parser.

Both reference agents ship this: Reasonix documents `reasonix completion bash|zsh|fish`, and Kimi
carries the same idea in its shell integration. The value of the feature rests entirely on the script
describing the *current* tree, so almost every test here compares the emitted script against
`build_parser()` rather than against a list written in this file.

The one test that does not is the live bash exercise, which sources the script and drives its
completion function — because "the script parses" and "the script completes" are different claims,
and only the second is the feature. That test found a real defect: the position check ran before the
flag check, so `engine.cli --js<TAB>` completed to nothing.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.cli import build_parser
from engine.completion import SUPPORTED_SHELLS, command_tree, script_for, top_level_names


@pytest.fixture(scope="module")
def parser():
    return build_parser()


@pytest.fixture(scope="module")
def tree(parser):
    return command_tree(parser)


# ── the tree is read off the parser ──────────────────────────────────────────


def test_every_top_level_command_in_the_script_is_one_the_parser_knows(parser, tree):
    """The staleness guard, in the direction that matters most.

    A completion script is advisory, so a name it offers that the parser does not accept produces no
    error at all — the shell completes, the command is run, and it fails. That silence is why this is
    asserted rather than eyeballed.
    """
    from engine.completion import _bash, _option_strings

    script = _bash("engine.cli", tree, " ".join(sorted(_option_strings(parser))))
    known = set(top_level_names(parser))
    # Every real command appears.
    for name in known:
        assert name in script, f"the script omits the real command {name!r}"
    # And the emitted first-position list is exactly the parser's own commands — nothing invented.
    # Read from the generated function's own `first` assignment rather than by splitting on the first
    # `compgen -W`, because the options arm now precedes it and a positional split silently picked up
    # the flag list instead (which is how this assertion first failed, usefully).
    marker = f'COMP_CWORD" -eq 1 ]; then\n        COMPREPLY=( $(compgen -W "'
    assert marker in script, "the script has no first-position word list"
    offered = set(script.split(marker, 1)[1].split('"', 1)[0].split())
    assert offered == known, (
        f"the script's first-word list disagrees with the parser: "
        f"invented={sorted(offered - known)}, missing={sorted(known - offered)}")


def test_the_nested_commands_are_reachable_from_the_tree(parser, tree):
    """Two-level trees must be walked, not just the first level.

    `system consent grant` is three deep, and a walk that stopped at one level would offer `system`
    and nothing under it — an inconsistency a person meets as "some commands complete and some do
    not", which is worse than none completing.
    """
    paths = {" ".join(path) for path in tree}
    for expected in ("system", "system consent", "system consent grant", "agent update",
                     "goal set", "mission add", "providers add", "session export"):
        assert expected in paths, f"{expected!r} is missing from the walked tree"


def test_the_walked_tree_matches_the_parser_at_every_level(parser, tree):
    """Nothing invented, nothing dropped: the set of paths is exactly the parser's own."""
    from engine.completion import _subparsers_of

    walked = {" ".join(path) for path in tree}
    expected: set[str] = set()

    def walk(node, prefix=()):
        for name, sub in _subparsers_of(node).items():
            path = (*prefix, name)
            expected.add(" ".join(path))
            walk(sub, path)

    walk(parser)
    assert walked == expected


# ── every supported shell emits something valid ──────────────────────────────


def test_each_supported_shell_emits_a_script_with_a_shebang_or_directive(parser):
    for shell in SUPPORTED_SHELLS:
        script = script_for(shell, parser)
        assert script.strip(), f"{shell} produced nothing"
        if shell == "zsh":
            assert script.startswith("#compdef"), "zsh needs the compdef directive to be loaded"
        if shell == "bash":
            assert "complete -F" in script, "bash needs the complete registration"
        if shell == "fish":
            assert script.lstrip().startswith("#"), "fish scripts open with a comment"
            assert "complete -c" in script, "fish needs at least one complete line"


def test_an_unsupported_shell_is_refused_by_name(parser):
    with pytest.raises(ValueError) as excinfo:
        script_for("powershell", parser)
    message = str(excinfo.value)
    assert "powershell" in message
    # The refusal must name the alternatives, or the person has to read the source to recover.
    for shell in SUPPORTED_SHELLS:
        assert shell in message


# ── the scripts really are valid for their shells ────────────────────────────


def test_the_bash_script_passes_bash_syntax_checking(parser):
    """`bash -n` is the shell's own parser, so this is not a re-implementation of the check."""
    script = script_for("bash", parser)
    with tempfile.NamedTemporaryFile("w", suffix=".bash", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        result = subprocess.run(["bash", "-n", path], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f"bash refused the script: {result.stderr}"
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def test_the_zsh_script_passes_zsh_syntax_checking(parser):
    """Skipped rather than failed when zsh is absent: an unavailable shell is not a broken script."""
    if not pathlib.Path("/bin/zsh").exists():
        pytest.skip("zsh is not installed here")
    script = script_for("zsh", parser)
    with tempfile.NamedTemporaryFile("w", suffix=".zsh", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        result = subprocess.run(["/bin/zsh", "-n", path], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f"zsh refused the script: {result.stderr}"
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


# ── the feature, exercised rather than parsed ────────────────────────────────


def _complete(script: str, words: list[str], index: int) -> list[str]:
    """Source the script in a real bash and call its completion function. Returns COMPREPLY.

    Driving the generated function is the only way to test what a person experiences; asserting on
    the script's text would pass even if the function returned nothing at all.
    """
    driver = (
        f"source {script}\n"
        f"COMP_WORDS=({' '.join(words)}); COMP_CWORD={index}\n"
        "_engine.cli_complete\n"
        'printf "%s\\n" "${COMPREPLY[@]}"\n'
    )
    result = subprocess.run(["bash", "-c", driver], capture_output=True, text=True, timeout=30)
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_a_real_bash_completes_a_partial_command_and_a_partial_flag(parser):
    """The behaviour, driven through bash. This is the test that caught the flag-ordering defect."""
    if not pathlib.Path("/bin/bash").exists():
        pytest.skip("bash is not installed here")
    script = script_for("bash", parser)
    with tempfile.NamedTemporaryFile("w", suffix=".bash", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        # A partial top-level command narrows to it.
        assert _complete(path, ["engine.cli", "sys"], 1) == ["system"]
        # A partial flag completes, at the *first* position. Checking the command position before the
        # flag arm made this return nothing, which is how the defect was found.
        assert "--json" in _complete(path, ["engine.cli", "--js"], 1)
        # A nested command is offered one level down.
        assert "consent" in _complete(path, ["engine.cli", "system", "con"], 2)
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def test_the_cli_exposes_the_completion_command_for_every_supported_shell(parser):
    """The command exists in the tree and accepts exactly the shells this module implements."""
    completion = None
    for action in parser._actions:  # noqa: SLF001
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "completion" in choices:
            completion = choices["completion"]
    assert completion is not None, "the parser has no `completion` command"
    shells: set[str] = set()
    for action in completion._actions:  # noqa: SLF001
        if action.dest == "shell":
            shells = {str(c) for c in (action.choices or [])}
    assert shells == set(SUPPORTED_SHELLS), (
        f"the command offers {sorted(shells)} but the module implements {sorted(SUPPORTED_SHELLS)}")


def test_the_command_prints_the_script_and_nothing_else():
    """stdout is the redirect target, so a banner would corrupt the installed file.

    Run as a subprocess because that is how it is actually used: `engine.cli completion bash > file`.
    """
    result = subprocess.run(
        [sys.executable, "-m", "engine.cli", "completion", "bash"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("#"), f"stdout does not open with the script: {result.stdout[:60]!r}"
    assert "complete -F" in result.stdout
    # A diagnostic on stdout would be written into the sourced file.
    assert "warning" not in result.stdout.lower()
