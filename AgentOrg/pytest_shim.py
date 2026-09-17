#!/usr/bin/env python3
"""pytest_shim.py — the minimal `pytest` surface the suite uses, with no dependency.

WHY THIS EXISTS
---------------
The test files are written for pytest (`import pytest`, `pytest.raises`, `pytest.fixture`,
`pytest.mark.parametrize`). Requiring pytest would mean either installing a dependency the engine
otherwise does not need, or documenting a test command that does not work.

This shim provides exactly the surface the suite exercises, and nothing more. It is loaded by
`run_tests.py` into `sys.modules["pytest"]` only when the real pytest is absent, so when pytest *is*
installed the genuine article is used.

It is deliberately not a general pytest replacement: a shim that pretended to support the whole
framework would be a source of confusing failures. It supports what is used, and anything else fails
visibly.

Usage:
    # in run_tests.py, before importing any test module:
    install_if_missing()
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Iterable, Sequence

__all__ = ["install_if_missing", "raises", "fixture", "mark", "approx", "skip"]


class Skipped(Exception):
    """Raised by :func:`skip` to mark a test skipped."""


class _RaisesContext:
    """Context manager implementing `pytest.raises`.

    Supports the two forms the suite uses: `pytest.raises(Error)` and
    `pytest.raises(Error, match="substring")`, and exposes the caught exception so a test can
    inspect its attributes (which several do, to assert an invariant id or an error kind).
    """

    def __init__(self, expected: type[BaseException] | tuple[type[BaseException], ...],
                 match: str | None = None) -> None:
        self.expected = expected
        self.match = match
        self.value: BaseException | None = None

    def __enter__(self) -> "_RaisesContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            names = getattr(self.expected, "__name__", None) or ", ".join(
                e.__name__ for e in self.expected)  # type: ignore[union-attr]
            raise AssertionError(f"DID NOT RAISE {names}")
        if not issubclass(exc_type, self.expected):  # type: ignore[arg-type]
            return False
        self.value = exc
        if self.match is not None:
            import re

            if not re.search(self.match, str(exc)):
                raise AssertionError(
                    f"raised {exc_type.__name__}({str(exc)!r}) which does not match "
                    f"{self.match!r}"
                )
        return True


def raises(expected: type[BaseException] | tuple[type[BaseException], ...],
           match: str | None = None) -> _RaisesContext:
    """Implementation of `pytest.raises`."""
    return _RaisesContext(expected, match=match)


def skip(reason: str = "") -> None:
    """Implementation of `pytest.skip`."""
    raise Skipped(reason)


class _FixtureMarker:
    """The object `@pytest.fixture` attaches to a function, carrying its scope."""

    def __init__(self, scope: str = "function") -> None:
        self.scope = scope


def fixture(func: Callable[..., Any] | None = None, *, scope: str = "function",
            **kwargs: Any) -> Any:
    """Implementation of `@pytest.fixture`, in bare and called form.

    `run_tests.py` reads the attached marker to decide whether to cache the fixture per module,
    which is what keeps the expensive library-loading fixtures from running 147 times.
    """
    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        setattr(target, "_pytestfixturefunction", _FixtureMarker(scope=scope))
        return target

    if func is not None:
        return decorate(func)
    return decorate


class _Mark:
    """Implementation of `pytest.mark.<name>`.

    A parametrize mark records its arguments on `pytestmark`, which is where `run_tests.py` reads
    them from. Any other mark is accepted and ignored — a marker that does nothing is harmless,
    unlike a marker that raises.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.args: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not args or not callable(args[0]):
            # Called with arguments, e.g. `mark.parametrize("a, b", [...])`: return a decorator
            # that records them.
            def decorator(target: Callable[..., Any]) -> Callable[..., Any]:
                mark = _Mark(self.name)
                mark.args = args
                mark.kwargs = kwargs
                existing = list(getattr(target, "pytestmark", []) or [])
                existing.append(mark)
                setattr(target, "pytestmark", existing)
                return target

            return decorator
        # Used bare, e.g. `@mark.slow`: record an empty mark.
        target = args[0]
        mark = _Mark(self.name)
        existing = list(getattr(target, "pytestmark", []) or [])
        existing.append(mark)
        setattr(target, "pytestmark", existing)
        return target


class _MarkFactory:
    """`pytest.mark` — attribute access yields a marker for that name."""

    def __getattr__(self, name: str) -> _Mark:
        return _Mark(name)


class _Approx:
    """Implementation of `pytest.approx` for float comparison.

    Compares within a relative tolerance by default, which is what the suite relies on for cost
    arithmetic where an exact equality would be brittle.
    """

    def __init__(self, expected: float, rel: float = 1e-6, abs: float = 1e-12) -> None:
        self.expected = expected
        self.rel = rel
        self.abs = abs

    def __eq__(self, other: Any) -> bool:
        try:
            return abs(float(other) - float(self.expected)) <= max(
                self.abs, self.rel * abs(float(self.expected))
            )
        except (TypeError, ValueError):
            return NotImplemented

    def __repr__(self) -> str:
        return f"approx({self.expected!r})"


def approx(expected: float, rel: float = 1e-6, abs: float = 1e-12) -> _Approx:
    """Implementation of `pytest.approx`."""
    return _Approx(expected, rel=rel, abs=abs)


class _PytestModule:
    """The shim module object, so `import pytest` inside a test file resolves to this."""

    def __init__(self) -> None:
        self.raises = raises
        self.fixture = fixture
        self.mark = _MarkFactory()
        self.approx = approx
        self.skip = skip
        self.Skipped = Skipped
        self.__version__ = "0-shim"

    @staticmethod
    def fail(reason: str = "") -> None:
        """Implementation of `pytest.fail`."""
        raise AssertionError(reason)


_SHIM = _PytestModule()


def install_if_missing() -> bool:
    """Install the shim as `pytest` when the real pytest is not importable.

    Returns True when the shim was installed, so the caller can say which runner is active rather
    than leaving the reader to guess.
    """
    try:
        import pytest  # noqa: F401

        return False
    except ImportError:
        sys.modules["pytest"] = _SHIM  # type: ignore[assignment]
        return True
