#!/usr/bin/env python3
"""run_tests.py — run the test suite with no dependency on pytest.

WHY THIS EXISTS
---------------
The engine is stdlib-only by design, and the environment may forbid installing pytest. Rather than
documenting a test command that does not work, this runner makes the suite runnable with a bare
Python interpreter.

It is a real runner, not a shim: it discovers `test_*.py` files, executes every `test_*` function,
honours `pytest.raises`-style assertions written as `pytest.raises`, supports the fixtures the suite
uses, and reports failures with their traceback. The suite is also valid pytest, so either runner
works — this one just has no prerequisites.

DESIGN
------
- **Fixtures are resolved in the order the test asks for them**, with `module`-scoped fixtures cached
  so an expensive one (loading 327 skills) runs once per module rather than per test.
- **Failures are reported with the assertion, not just a count**, because the useful part of a
  failure is what the assertion said.
- **A missing optional import is skipped, never failed**, matching pytest's `skip` semantics for the
  tests that need a second parser to compare against.

Usage:
    python3 run_tests.py                 # everything
    python3 run_tests.py tests/test_phase4_org.py
    python3 run_tests.py -k router       # only tests whose name matches
    python3 run_tests.py -q              # summary only
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import pathlib
import sys
import traceback
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Install the pytest shim before any test module is imported, so `import pytest` inside a test file
# resolves. When the real pytest is present this is a no-op and the real one is used.
import pytest_shim  # noqa: E402

_SHIM_ACTIVE = pytest_shim.install_if_missing()

__all__ = ["main", "run_file", "discover"]


class _Skip(Exception):
    """Raised by a fixture or a test to mark it skipped."""


def discover(targets: list[str] | None) -> list[pathlib.Path]:
    """Find the test files to run, in a stable order."""
    if targets:
        return [pathlib.Path(t) for t in targets]
    tests_dir = ROOT / "tests"
    return sorted(tests_dir.glob("test_*.py"))


def _load_module(path: pathlib.Path) -> Any:
    """Import a test file as a module, by path rather than by package name."""
    spec = importlib.util.spec_from_file_location(f"_agentorg_test_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixtures(module: Any) -> dict[str, Any]:
    """Collect module-level fixture functions, keyed by name."""
    out: dict[str, Any] = {}
    for name, value in vars(module).items():
        if callable(value) and hasattr(value, "_pytestfixturefunction"):
            out[name] = value
        elif callable(value) and getattr(value, "__name__", "") in {}:
            continue
    return out


def _fixture_cache_for(module: Any) -> dict[str, Any] | None:
    """Per-module fixture cache, so a module-scoped fixture runs once."""
    cache = getattr(module, "_agentorg_fixture_cache", None)
    if cache is None:
        cache = {}
        setattr(module, "_agentorg_fixture_cache", cache)
    return cache


def _resolve(name: str, module: Any, fixtures: dict[str, Any], tmp_root: pathlib.Path,
             monkeypatch: Any = None, per_test: dict[str, Any] | None = None) -> Any:
    """Resolve one fixture argument for a test function.

    `per_test` is a cache created once per test. It exists so `tmp_path` is the *same* directory for
    the test body and for every fixture it requests — resolving it fresh each time meant a fixture
    that wrote and a body that read were looking at two different directories, which made a test fail
    with `FileNotFoundError` or, worse, pass while inspecting an empty one.
    """
    if name == "tmp_path":
        cache = per_test if per_test is not None else {}
        if "__tmp_path__" not in cache:
            import tempfile

            cache["__tmp_path__"] = pathlib.Path(
                tempfile.mkdtemp(prefix=f"agentorg-{tmp_root.name}-"))
        return cache["__tmp_path__"]
    if name == "monkeypatch":
        return monkeypatch
    fixture = fixtures.get(name)
    if fixture is None:
        raise _Skip(f"no fixture named {name!r}")
    scope = getattr(fixture, "_pytestfixturefunction", None)
    is_module_scoped = bool(scope and getattr(scope, "scope", "function") == "module")
    cache = _fixture_cache_for(module)
    if is_module_scoped and name in cache:
        return cache[name]
    # A function-scoped fixture is built once *per test*, not once per reference to it. Without this
    # a test that asked for `project` twice — directly and through another fixture — built it twice
    # into the same directory, which fails on `mkdir` or silently re-creates what it just wrote.
    if not is_module_scoped and per_test is not None and name in per_test:
        return per_test[name]
    kwargs = {
        arg: _resolve(arg, module, fixtures, tmp_root, monkeypatch, per_test=per_test)
        for arg in inspect.signature(fixture).parameters
    }
    value = fixture(**kwargs)

    # A generator fixture yields its value and then runs teardown on resume — the form pytest uses
    # for a resource that must be cleaned up (the RPC tests bind a socket and stop the server this
    # way). Returning the generator itself would hand the test a generator instead of the resource.
    if inspect.isgenerator(value):
        generator = value
        try:
            resolved = next(generator)
        except StopIteration:
            resolved = None
        finalizers = getattr(module, "_agentorg_finalizers", None)
        if finalizers is None:
            finalizers = []
            setattr(module, "_agentorg_finalizers", finalizers)
        finalizers.append(generator)
        value = resolved

    if is_module_scoped:
        cache[name] = value
    elif per_test is not None:
        per_test[name] = value
    return value


def _run_finalizers(module: Any) -> None:
    """Exhaust every generator fixture so its teardown runs.

    Called after each test file finishes, which is where module-scoped teardown belongs.
    """
    for generator in reversed(getattr(module, "_agentorg_finalizers", []) or []):
        try:
            next(generator)
        except StopIteration:
            continue
        except Exception:  # noqa: BLE001 - teardown must not mask a test result
            continue
    if hasattr(module, "_agentorg_finalizers"):
        module._agentorg_finalizers = []


class _MonkeyPatch:
    """A minimal `monkeypatch` replacement.

    Only the operations the suite uses: setting an environment variable and deleting one, and
    setting an attribute with restoration.
    """

    def __init__(self) -> None:
        self._env: dict[str, str | None] = {}
        self._attrs: list[tuple[Any, str, Any, bool]] = []

    def setenv(self, name: str, value: str) -> None:
        import os

        self._env.setdefault(name, os.environ.get(name))
        os.environ[name] = value

    def delenv(self, name: str, raising: bool = True) -> None:
        import os

        if name in os.environ:
            self._env.setdefault(name, os.environ.get(name))
            del os.environ[name]
        elif raising:
            raise KeyError(name)

    def setattr(self, target: Any, name: str, value: Any, raising: bool = True) -> None:
        had = hasattr(target, name)
        if not had and raising:
            raise AttributeError(name)
        self._attrs.append((target, name, getattr(target, name, None), had))
        setattr(target, name, value)

    def undo(self) -> None:
        import os

        for name, original in self._env.items():
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original
        self._env.clear()
        for target, name, original, had in reversed(self._attrs):
            if had:
                setattr(target, name, original)
            else:
                try:
                    delattr(target, name)
                except AttributeError:
                    pass
        self._attrs.clear()


def run_file(path: pathlib.Path, *, quiet: bool = False,
             name_filter: str | None = None) -> tuple[int, int, list[str]]:
    """Run every test in one file. Returns ``(passed, failed, failure_reports)``."""
    module = _load_module(path)
    fixtures = _fixtures(module)
    tests = [
        (name, value) for name, value in vars(module).items()
        if name.startswith("test_") and callable(value)
    ]
    tests.sort(key=lambda kv: kv[0])
    if name_filter:
        tests = [(n, f) for n, f in tests if name_filter.lower() in n.lower()]

    passed = failed = 0
    reports: list[str] = []
    parametrize = getattr(module, "pytest", None) is not None

    for name, fn in tests:
        # Expand pytest.mark.parametrize, which the suite uses heavily.
        cases = _expand_parametrize(fn)
        for case in cases:
            label = f"{path.name}::{name}" + (f"[{case['id']}]" if case.get("id") else "")
            monkeypatch = _MonkeyPatch()
            try:
                kwargs = dict(case.get("kwargs") or {})
                # One `tmp_path` per *test*, shared by the test body and every fixture it uses.
                # A fresh cache per test (and per parametrised case) is what makes each get its own
                # directory while a fixture and the body agree on the same one.
                per_test: dict[str, Any] = {}
                for arg in inspect.signature(fn).parameters:
                    if arg in kwargs:
                        continue
                    kwargs[arg] = _resolve(arg, module, fixtures, path.with_suffix(""), monkeypatch,
                                           per_test=per_test)
                fn(**kwargs)
                passed += 1
                if not quiet:
                    print(f"  PASS {label}")
            except _Skip as exc:
                passed += 1
                if not quiet:
                    print(f"  SKIP {label}: {exc}")
            except Exception as exc:  # noqa: BLE001 - a failed test is data, not a crash
                failed += 1
                report = (
                    f"FAIL {label}\n"
                    f"  {type(exc).__name__}: {exc}\n"
                    + "".join(f"    {line}\n" for line in traceback.format_exc().splitlines()[-6:])
                )
                reports.append(report.rstrip())
                print(f"  FAIL {label}: {type(exc).__name__}: {exc}")
            finally:
                monkeypatch.undo()
    # Module-scoped generator fixtures tear down once per file, not per test.
    _run_finalizers(module)
    return passed, failed, reports


def _expand_parametrize(fn: Callable[..., Any]) -> list[dict[str, Any]]:
    """Expand a `pytest.mark.parametrize` decorator into concrete cases.

    The suite uses parametrize in several places, so honouring it here is what makes this runner a
    real alternative rather than a subset that silently skips coverage.
    """
    marks = getattr(fn, "pytestmark", None) or []
    cases: list[dict[str, Any]] = [{}]
    for mark in marks:
        if getattr(mark, "name", "") != "parametrize":
            continue
        args = list(mark.args)
        if len(args) != 2:
            continue
        names = [n.strip() for n in str(args[0]).split(",")]
        values = list(args[1])
        expanded: list[dict[str, Any]] = []
        for base in cases:
            for value in values:
                entry = dict(base)
                kwargs = dict(entry.get("kwargs") or {})
                row = value if isinstance(value, (tuple, list)) else (value,)
                for index, name in enumerate(names):
                    kwargs[name] = row[index] if index < len(row) else None
                entry["kwargs"] = kwargs
                entry["id"] = "-".join(str(v)[:20] for v in row)
                expanded.append(entry)
        cases = expanded
    return cases


def main(argv: list[str] | None = None) -> int:
    """Run the suite. Returns a process exit code."""
    parser = argparse.ArgumentParser(description="Run the AgentOrg test suite without pytest.")
    parser.add_argument("targets", nargs="*", help="test files (default: tests/test_*.py)")
    parser.add_argument("-k", dest="filter", help="only run tests whose name contains this")
    parser.add_argument("-q", "--quiet", action="store_true", help="summary only")
    args = parser.parse_args(argv)

    files = discover(args.targets)
    if not files:
        print("no test files found", file=sys.stderr)
        return 2

    total_passed = total_failed = 0
    all_reports: list[str] = []
    for path in files:
        if not path.is_file():
            print(f"not a file: {path}", file=sys.stderr)
            return 2
        if not args.quiet:
            print(f"\n{path}")
        passed, failed, reports = run_file(path, quiet=args.quiet, name_filter=args.filter)
        total_passed += passed
        total_failed += failed
        all_reports.extend(reports)

    print()
    if all_reports:
        print("=" * 72)
        for report in all_reports:
            print(report)
            print("-" * 72)
    print(f"{total_passed} passed, {total_failed} failed")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
