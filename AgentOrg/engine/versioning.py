#!/usr/bin/env python3
"""versioning.py — schema versions, safe migration, and refusal to guess.

WHY THIS EXISTS
---------------
A workspace outlives the code that wrote it. The Owner upgrades AgentOrg, opens a
project started last month, and the engine must decide what to trust. Two wrong
answers are common and both are damaging:

- **Silently read a newer schema.** Fields the old build does not know about are
  dropped on the next write, quietly destroying data the new build relied on.
- **Refuse everything unfamiliar.** A patch upgrade that added an optional field would
  make every existing workspace unopenable.

So the rule is: *migrate forward what we understand, refuse what we do not, and never
guess*. A workspace written by a newer schema is opened read-only at best, and the
refusal names the versions so the Owner can act.

DESIGN
------
- Every persisted document carries a `*_version` field (the library does the same:
  `handoff_version`, `memory_version`), so a version is a property of the artifact,
  not a filename convention.
- Versions are `MAJOR.MINOR.PATCH` semver-ish: a MAJOR bump is a breaking shape change
  and requires a registered migration; MINOR/PATCH are additive and readable as-is.
- Migrations are explicit, ordered functions, each moving one step, and each recorded
  in the document so a migration is auditable rather than mysterious.
- A missing version means "v0" for documents that predate versioning, which is a real
  case worth handling rather than crashing on.

Usage:
    reg = Registry()
    reg.register("run_state", "1.1.0", lambda d: {**d, "new_field": None})
    doc = reg.load(path, kind="run_state", current="1.2.0")
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "SchemaError",
    "SchemaVersionError",
    "SchemaTooNewError",
    "parse_version",
    "is_compatible",
    "Registry",
    "Migration",
]

_VERSION_RE = None


class SchemaError(RuntimeError):
    """Base class for schema problems."""


class SchemaVersionError(SchemaError):
    """A version string could not be parsed."""


class SchemaTooNewError(SchemaError):
    """The document was written by a schema newer than this build understands."""

    def __init__(self, kind: str, found: str, current: str) -> None:
        self.kind, self.found, self.current = kind, found, current
        super().__init__(
            f"{kind} schema {found} was written by a newer AgentOrg than this build "
            f"(which speaks {current}).\n"
            "  Refusing to open it: reading and re-writing would drop fields this "
            "build does not know about.\n"
            "  Upgrade AgentOrg, or open a copy in a newer build and downgrade it."
        )


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse ``MAJOR.MINOR.PATCH`` into a tuple.

    Tolerant of a leading ``v``, a missing patch, and non-numeric suffixes such as
    ``1.2.0-beta``, because those all appear in real files and none of them warrant
    refusing to open a workspace.

    Raises
    ------
    SchemaVersionError
        When the string cannot be interpreted at all.
    """
    if text is None:
        raise SchemaVersionError("version is missing")
    raw = str(text).strip().lstrip("vV")
    if not raw:
        raise SchemaVersionError("version is empty")
    # Drop any pre-release/build metadata before parsing the numeric core.
    core = raw.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if not (1 <= len(parts) <= 3):
        raise SchemaVersionError(f"cannot parse version {text!r}; expected MAJOR[.MINOR[.PATCH]]")
    numbers: list[int] = []
    for part in parts:
        if not part.isdigit():
            raise SchemaVersionError(f"cannot parse version {text!r}: {part!r} is not numeric")
        numbers.append(int(part))
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)  # type: ignore[return-value]


def is_compatible(found: str, current: str) -> bool:
    """True when a document at `found` can be read by a build speaking `current`.

    Compatibility is defined on MAJOR only: additive changes within a major series are
    readable, while a major bump means the shape changed incompatibly.
    """
    f_major, _, _ = parse_version(found)
    c_major, _, _ = parse_version(current)
    return f_major <= c_major


@dataclass(frozen=True)
class Migration:
    """One ordered step from ``from_version`` to ``to_version`` for a document kind."""

    kind: str
    from_version: str
    to_version: str
    apply: Callable[[dict[str, Any]], dict[str, Any]]
    note: str = ""

    def __call__(self, doc: dict[str, Any]) -> dict[str, Any]:
        return self.apply(doc)


class Registry:
    """Knows each document kind's current version and how to migrate older documents.

    Parameters
    ----------
    versions:
        Mapping of document kind -> current version string. Seeded from config so the
        single source of truth for versions stays `credentials.json`.
    """

    def __init__(self, versions: dict[str, str] | None = None) -> None:
        self.versions: dict[str, str] = dict(versions or {})
        self._migrations: dict[str, list[Migration]] = {}

    # ── registration ────────────────────────────────────────────────────────

    def set_version(self, kind: str, version: str) -> None:
        """Declare the current version for a document kind (validates the string)."""
        parse_version(version)
        self.versions[kind] = version

    def current(self, kind: str) -> str:
        """Current version for a kind, defaulting to 1.0.0 when undeclared."""
        return self.versions.get(kind, "1.0.0")

    def register(self, kind: str, from_version: str, to_version: str,
                 apply: Callable[[dict[str, Any]], dict[str, Any]], *, note: str = "") -> None:
        """Register a migration step. Steps for a kind are sorted by version on use."""
        parse_version(from_version)
        parse_version(to_version)
        self._migrations.setdefault(kind, []).append(
            Migration(kind=kind, from_version=from_version, to_version=to_version, apply=apply, note=note)
        )
        self._migrations[kind].sort(key=lambda m: parse_version(m.from_version))

    def migrations_for(self, kind: str) -> list[Migration]:
        """Registered migrations for a kind, oldest first."""
        return list(self._migrations.get(kind, []))

    # ── document handling ───────────────────────────────────────────────────

    def version_field(self, kind: str) -> str:
        """The field name carrying this kind's version.

        `run_state` -> `run_state_version`; a document whose own name ends in
        `_version` (a handoff payload) keeps its canonical field.
        """
        if kind.endswith("_version"):
            return kind
        return f"{kind}_version"

    def check(self, doc: dict[str, Any], kind: str) -> str:
        """Validate a document's version against this build, migrating when needed.

        Returns the version the document is at *after* migration. Raises
        :class:`SchemaTooNewError` for a newer MAJOR, and :class:`SchemaError` when a
        required migration step is missing — never silently passing an unmigrated,
        misunderstood document through.
        """
        field = self.version_field(kind)
        found_raw = doc.get(field, "0.0.0")
        found = str(found_raw)
        target = self.current(kind)
        # Keep a handle on the caller's mapping: migrations return new dicts, and the
        # migrated content must end up in the object the caller passed in.
        original = doc

        found_major, _, _ = parse_version(found)
        target_major, _, _ = parse_version(target)
        if found_major > target_major:
            raise SchemaTooNewError(kind, found, target)

        if parse_version(found) == parse_version(target):
            return target

        # Walk the registered steps from where the document is to where we are.
        steps = self.migrations_for(kind)
        applicable = [
            m for m in steps
            if parse_version(m.from_version) >= parse_version(found)
            and parse_version(m.to_version) <= parse_version(target)
        ]
        if not applicable:
            if found_major == target_major:
                # Same major series, additive differences: safe to read as-is.
                return found
            raise SchemaError(
                f"no migration path for {kind} from {found} to {target}. "
                "Add a registered migration rather than reading the document blindly."
            )

        applied: list[str] = []
        for migration in applicable:
            doc = migration.apply(doc)
            applied.append(f"{migration.from_version}->{migration.to_version}")
        doc[field] = target
        # A later migration step may have rebuilt the mapping (migrations return a new
        # dict rather than mutating). Rebind the caller's object in place so the
        # migrated content is actually visible to whoever passed it in — otherwise the
        # migration is computed and silently discarded.
        if original is not doc:
            original.clear()
            original.update(doc)
            doc = original
        # Record what happened so a migrated workspace is auditable.
        history = doc.setdefault("_migrations", [])
        if isinstance(history, list):
            history.append({"kind": kind, "from": found, "to": target, "steps": applied})
        return target

    def load(self, path: os.PathLike | str, *, kind: str, migrate: bool = True) -> dict[str, Any]:
        """Load a JSON document, checking (and optionally migrating) its schema.

        ``migrate=False`` validates the version and raises on incompatibility without
        modifying the file, which is what a read-only inspection wants.

        Raises
        ------
        SchemaError
            When the file is missing, malformed, or not a JSON object.
        """
        target = Path(path)
        if not target.is_file():
            raise SchemaError(f"{kind} document not found: {target}")
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchemaError(f"{kind} document {target} is malformed JSON: {exc.msg}") from exc
        if not isinstance(data, dict):
            raise SchemaError(f"{kind} document {target} must be a JSON object")
        if migrate:
            self.check(data, kind)
        else:
            field = self.version_field(kind)
            found = str(data.get(field, "0.0.0"))
            f_major, _, _ = parse_version(found)
            c_major, _, _ = parse_version(self.current(kind))
            if f_major > c_major:
                raise SchemaTooNewError(kind, found, self.current(kind))
        return data

    def save(self, path: os.PathLike | str, doc: dict[str, Any], *, kind: str) -> Path:
        """Stamp the current version and atomically persist a document.

        Stamping here rather than trusting the caller means a document can never be
        written without a version, which is what makes the refusal-to-guess rule work.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        doc = dict(doc)
        doc[self.version_field(kind)] = self.current(kind)
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
        return target


def default_registry(schemas: dict[str, str] | None = None) -> Registry:
    """A registry seeded with AgentOrg's document kinds and their current versions."""
    defaults = {
        "workspace": "1.0.0",
        "run_state": "1.0.0",
        "org": "1.0.0",
        "session_handoff": "1.0.0",
        "handoff_version": "1.0.0",
        "requisition": "1.0.0",
        "spans": "1.0.0",
        "review_feedback": "1.0.0",
        "effect_journal": "1.0.0",
    }
    if schemas:
        defaults.update(schemas)
    return Registry(defaults)
