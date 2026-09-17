#!/usr/bin/env python3
"""library.py — locate, pin and hash-verify the Skills library.

WHY THIS EXISTS
---------------
AgentOrg does not merely *read* the zeroes-ones/Skills library: it treats it as a
**dependency**. Skill bodies become system-prompt content, `workflow:` frontmatter
supplies the completion criteria that gate every phase transition, `workflow/templates/`
supply the boundary prompts, and `evals/golden/` supplies the health-probe corpus.
A floating path is therefore a supply-chain hole: modify a `SKILL.md` on disk and you
have silently changed what every agent is instructed to do.

DESIGN
------
- **Pin by commit SHA when the library is a git checkout.** The SHA is recorded in the
  config; a mismatch is a hard failure, not a warning, because the alternative is
  running a prompt you did not review.
- **Verify a content manifest.** Every consumed file is hashed and checked against a
  recorded manifest, so tampering is detectable even without git.
- **Fail loud, fail early.** A missing runner, a missing schema directory or a hash
  mismatch raises :class:`LibraryError` at startup rather than surfacing as a confusing
  failure three hours into a run.
- **Never import from the library.** We read its files as data. Nothing in it is
  executed as code, and its content is confined to the prompt slot it belongs to.

Usage:
    from engine.library import resolve
    lib = resolve()                      # uses config/defaults
    lib.paths.runner                     # .../scripts/workflow-runner.py
    lib.verify()                         # raises LibraryError on mismatch
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["LibraryError", "LibraryFiles", "Library", "resolve", "sha256_file", "sha256_text"]


class LibraryError(RuntimeError):
    """Raised when the pinned Skills library is missing, mismatched or unverifiable."""


# Files the engine genuinely depends on. Each entry is (attribute, relative path).
# `required` entries raise if absent; `optional` entries are recorded when present.
_REQUIRED = (
    ("runner", "scripts/workflow-runner.py"),
    ("validator", "scripts/validate-workflows.py"),
    ("skill_sli_report", "scripts/skill-sli-report.py"),
    ("export_traces", "scripts/export-traces.py"),
    ("safe_yaml", "scripts/lib/safe_yaml.py"),
    ("lint_workflow", "scripts/lib/lint-workflow.py"),
)
_REQUIRED_DIRS = (
    ("templates", "workflow/templates"),
    ("schema", "workflow/schema"),
    ("flat_skills", "skills-flat"),
    ("nested_skills", "skills"),
)
_OPTIONAL_DIRS = (
    ("compiled", ".skills-compiled"),
    ("golden", "evals/golden"),
    ("behavioral", "evals/tier3-behavioral"),
)
# Substrings that must appear in the runner's argparse surface. If a future library
# version drops one of these, our host integration silently degrades — so we assert
# the capabilities we actually call, not just that the file exists.
_RUNNER_CAPABILITIES = (
    "--manifest",
    "--executor",
    "--guardrail",
    "--state",
    "--memory",
    "--enforce-contracts",
    "--max-steps",
)
# Directories pruned from manifest walks: never hash git objects or caches.
_MANIFEST_PRUNE = {".git", "__pycache__", ".DS_Store", ".pytest_cache"}


def sha256_file(path: os.PathLike | str) -> str:
    """Return the hex sha256 of a file, streamed so large files stay cheap."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    """Return the hex sha256 of a string, UTF-8 encoded."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LibraryFiles:
    """Resolved absolute paths into the pinned library."""

    root: Path
    runner: Path
    validator: Path
    skill_sli_report: Path
    export_traces: Path
    safe_yaml: Path
    lint_workflow: Path
    templates: Path
    schema: Path
    flat_skills: Path
    nested_skills: Path
    compiled: Path | None
    golden: Path | None
    behavioral: Path | None

    def as_dict(self) -> dict[str, str | None]:
        """Serialise to plain strings for events and diagnostics."""
        return {
            "root": str(self.root),
            "runner": str(self.runner),
            "validator": str(self.validator),
            "skill_sli_report": str(self.skill_sli_report),
            "export_traces": str(self.export_traces),
            "safe_yaml": str(self.safe_yaml),
            "lint_workflow": str(self.lint_workflow),
            "templates": str(self.templates),
            "schema": str(self.schema),
            "flat_skills": str(self.flat_skills),
            "nested_skills": str(self.nested_skills),
            "compiled": str(self.compiled) if self.compiled else None,
            "golden": str(self.golden) if self.golden else None,
            "behavioral": str(self.behavioral) if self.behavioral else None,
        }


@dataclass
class Library:
    """A pinned, verified view of the Skills library.

    Attributes
    ----------
    files:
        Resolved paths.
    commit:
        The git commit SHA of the checkout, when the library is a git repo.
    manifest:
        Mapping of path-relative-to-root -> sha256 for every consumed file.
    verified:
        True once :meth:`verify` has passed against a recorded manifest.
    """

    files: LibraryFiles
    commit: str | None = None
    manifest: dict[str, str] = field(default_factory=dict)
    verified: bool = False

    # ── verification ────────────────────────────────────────────────────────

    def _git_commit(self) -> str | None:
        """Return HEAD of the library checkout, or None when it is not a git repo."""
        git_dir = self.files.root / ".git"
        if not git_dir.exists():
            return None
        try:
            out = subprocess.run(
                ["git", "-C", str(self.files.root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        sha = out.stdout.strip()
        return sha or None

    def assert_capabilities(self) -> None:
        """Assert the runner exposes every CLI flag our host integration calls.

        A file existing is not the same as the file supporting what we need. Dropping
        `--guardrail` in a future library release would silently disable handoff
        safety, so we verify the surface we depend on at startup.
        """
        try:
            text = self.files.runner.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - read failure is fatal
            raise LibraryError(f"cannot read runner {self.files.runner}: {exc}") from exc
        missing = [flag for flag in _RUNNER_CAPABILITIES if flag not in text]
        if missing:
            raise LibraryError(
                "workflow-runner.py is missing CLI capabilities AgentOrg depends on: "
                + ", ".join(missing)
                + f"\n  runner: {self.files.runner}\n"
                "  A library version that drops these flags cannot be used safely."
            )
        if "execute_node(node_id, state, ctx)" not in text:
            raise LibraryError(
                "workflow-runner.py no longer documents the "
                "'execute_node(node_id, state, ctx)' executor contract; "
                "AgentOrg's executor plugin would not be loaded."
            )

    def build_manifest(self) -> dict[str, str]:
        """Hash every file under the consumed directories.

        The manifest is the tamper-evidence record: it is written next to the
        workspace and re-checked on every startup, so a modified `SKILL.md` or a
        swapped `workflow-runner.py` is detected before a single token is spent.
        """
        manifest: dict[str, str] = {}
        for sub in ("scripts", "workflow", "evals"):
            base = self.files.root / sub
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                if _MANIFEST_PRUNE.intersection(path.parts):
                    continue
                rel = path.relative_to(self.files.root).as_posix()
                manifest[rel] = sha256_file(path)
        # Skill bodies: the highest-value thing to pin, and the largest. We hash the
        # flat layer only, because every flat entry is a symlink into the nested
        # layer, so hashing both would double the work for identical content.
        flat = self.files.flat_skills
        if flat.is_dir():
            for entry in sorted(flat.iterdir()):
                skill_md = entry / "SKILL.md"
                if skill_md.is_file():
                    rel = skill_md.relative_to(self.files.root).as_posix()
                    try:
                        manifest[rel] = sha256_file(skill_md)
                    except OSError:
                        continue
        return manifest

    def write_manifest(self, path: os.PathLike | str) -> Path:
        """Persist the manifest as JSON, including the pinned commit when known.

        Builds the manifest on demand when :meth:`verify` ran without an expected
        manifest, so the first run of a fresh checkout can record a baseline rather
        than writing an empty file set.
        """
        if not self.manifest:
            self.manifest = self.build_manifest()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "manifest_version": "1.0.0",
            "library_root": str(self.files.root),
            "commit": self.commit,
            "file_count": len(self.manifest),
            "files": self.manifest,
        }
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
        return target

    def verify(self, expected_commit: str | None = None,
               expected_manifest: dict[str, str] | None = None) -> None:
        """Verify the library against a recorded commit and/or manifest.

        Raises
        ------
        LibraryError
            On commit mismatch, missing files, or any content-hash mismatch. The
            message names the differing paths so the failure is actionable.
        """
        self.assert_capabilities()

        if expected_commit:
            if not self.commit:
                raise LibraryError(
                    f"expected library commit {expected_commit} but {self.files.root} "
                    "is not a git checkout"
                )
            if self.commit != expected_commit:
                raise LibraryError(
                    f"library commit mismatch: expected {expected_commit}, found {self.commit}\n"
                    f"  root: {self.files.root}\n"
                    "  Review the change, then re-pin deliberately rather than running "
                    "against unreviewed prompt content."
                )

        if expected_manifest is None:
            self.verified = True
            return

        actual = self.build_manifest()
        missing = sorted(set(expected_manifest) - set(actual))
        changed = sorted(
            rel for rel, digest in expected_manifest.items()
            if rel in actual and actual[rel] != digest
        )
        if missing or changed:
            detail = []
            if missing:
                detail.append(f"  missing ({len(missing)}): " + ", ".join(missing[:10]))
            if changed:
                detail.append(f"  changed ({len(changed)}): " + ", ".join(changed[:10]))
            raise LibraryError(
                "library content manifest mismatch — the pinned library has been modified\n"
                + "\n".join(detail)
                + f"\n  root: {self.files.root}"
            )

        self.manifest = actual
        self.verified = True

    # ── skill lookup ────────────────────────────────────────────────────────

    def skill_roots(self) -> list[Path]:
        """Skill search order: the flat discovery layer first, then the nested one.

        The flat layer (`skills-flat/`) is a directory of symlinks, one level deep,
        which is what every agent CLI expects. The nested layer
        (`skills/<NN-domain>/<name>/`) is the real tree and is the fallback when a
        skill is not linked flat.
        """
        roots = [self.files.flat_skills]
        if self.files.nested_skills.is_dir():
            roots.append(self.files.nested_skills)
        return [r for r in roots if r.is_dir()]

    def find_skill(self, name: str) -> Path | None:
        """Resolve a skill name to its directory, searching both layers.

        Nested search is depth-2 only (`skills/<domain>/<name>`), which is the
        library's own layout. Returning None lets the caller decide whether a missing
        skill is fatal (it usually is, per the delegation design's missing-skill rung).
        """
        if not _SLUG_RE.match(name):
            return None
        for root in self.skill_roots():
            candidate = root / name
            if (candidate / "SKILL.md").is_file():
                return candidate
        nested = self.files.nested_skills
        if nested.is_dir():
            for domain in sorted(nested.iterdir()):
                if not domain.is_dir():
                    continue
                candidate = domain / name
                if (candidate / "SKILL.md").is_file():
                    return candidate
        return None


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _first_existing(paths: list[str]) -> Path | None:
    for raw in paths:
        candidate = Path(raw).expanduser()
        if (candidate / "scripts" / "workflow-runner.py").is_file():
            return candidate
    return None


def default_search_paths() -> list[str]:
    """Candidate library roots, most specific first.

    Order matters: an explicit environment override wins, then the documented
    sibling checkout, then the installer's conventional location. We probe for the
    runner rather than merely for a directory, because a directory that looks right
    but lacks the runner is the exact failure we want to catch early.
    """
    override = os.environ.get("AGENTORG_SKILLS_ROOT")
    candidates: list[str] = []
    if override:
        candidates.append(override)
    home = Path.home()
    candidates += [
        str(home / "Documents" / "Projects" / "Skills"),
        str(home / ".zeroes-ones" / "skills"),
        str(home / ".agentorg" / "skills"),
    ]
    return candidates


def resolve(root: os.PathLike | str | None = None, *,
            expected_commit: str | None = None,
            expected_manifest: dict[str, str] | None = None,
            verify: bool = True) -> Library:
    """Resolve and (optionally) verify the pinned Skills library.

    Parameters
    ----------
    root:
        Explicit library root. When omitted, :func:`default_search_paths` is probed.
    expected_commit:
        Pin. Raises on mismatch when supplied.
    expected_manifest:
        Recorded content manifest. Raises on any missing or changed file.
    verify:
        When False, only path resolution and capability assertions run. Used by
        tooling that wants to *build* a manifest for the first time.
    """
    resolved: Path | None
    if root is not None:
        candidate = Path(root).expanduser()
        resolved = candidate if (candidate / "scripts" / "workflow-runner.py").is_file() else None
    else:
        resolved = _first_existing(default_search_paths())

    if resolved is None:
        searched = [str(root)] if root is not None else default_search_paths()
        raise LibraryError(
            "Skills library not found. AgentOrg requires a checkout of "
            "zeroes-ones/Skills containing scripts/workflow-runner.py.\n"
            "  searched:\n    " + "\n    ".join(searched) + "\n"
            "  set AGENTORG_SKILLS_ROOT=/path/to/Skills to override."
        )

    attrs: dict[str, Path] = {}
    for attr, rel in _REQUIRED:
        path = resolved / rel
        if not path.is_file():
            raise LibraryError(f"library file missing: {path}")
        attrs[attr] = path
    for attr, rel in _REQUIRED_DIRS:
        path = resolved / rel
        if not path.is_dir():
            raise LibraryError(f"library directory missing: {path}")
        attrs[attr] = path
    for attr, rel in _OPTIONAL_DIRS:
        path = resolved / rel
        attrs[attr] = path if path.is_dir() else None

    files = LibraryFiles(root=resolved, **attrs)
    lib = Library(files=files, commit=None)

    if verify:
        lib.commit = lib._git_commit()
        lib.verify(expected_commit=expected_commit, expected_manifest=expected_manifest)
    else:
        lib.assert_capabilities()

    return lib
