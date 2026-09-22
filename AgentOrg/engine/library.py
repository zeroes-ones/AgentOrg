#!/usr/bin/env python3
"""library.py — locate, pin and hash-verify the Skills library.

WHY THIS EXISTS
---------------
AgentOrg does not merely *read* the zeroes-ones/Skills library: it treats it as a
**dependency**. Skill bodies become system-prompt content and `workflow:` frontmatter
supplies the completion criteria that gate every phase transition. A floating path is
therefore a supply-chain hole: modify a `SKILL.md` on disk and you have silently changed
what every agent is instructed to do.

Only what is read is registered here. The library also ships `workflow/templates/`,
`.skills-compiled/`, `evals/golden/` and `evals/tier3-behavioral/`, but those belong to the
library's *own* consumers — its `iterative-task-execution` skill and its eval toolchain —
and this engine never opens them: the boundary protocol is implemented in
:mod:`engine.prompts`, and skills are parsed from raw markdown. Registering them anyway
would put a resolved path in every diagnostic and, for the required ones, refuse startup
over a directory no line of code reads.

DESIGN
------
- **Pin by commit SHA when the library is a git checkout.** A mismatch is a hard failure,
  not a warning, because the alternative is running a prompt you did not review.
- **Verify a content manifest.** Every consumed file is hashed and checked against a
  recorded manifest, so tampering is detectable even without git.
- **Report which check actually ran, never a single "verified".** `capabilities_verified`
  means the paths resolved and the runner advertises the flags we call; `commit_pinned` and
  `manifest_pinned` mean a *supplied* pin was compared and matched. With no pin there is no
  comparison, so both pin flags stay false — collapsing the two facts into one boolean is
  how a reader comes to believe content was hashed when nothing was.
- **A pin is opt-in, but real.** A recorded pin is discovered at
  :func:`default_pin_path` (or named explicitly), compared on every resolve, and refused
  loudly on mismatch. Absent by default, so a first run against an unpinned checkout works.
- **Fail loud, fail early.** A missing runner or a hash mismatch raises
  :class:`LibraryError` at startup rather than surfacing as a confusing failure three hours
  into a run.
- **Never import from the library.** We read its files as data. Nothing in it is
  executed as code, and its content is confined to the prompt slot it belongs to.

Usage:
    from engine.library import resolve
    lib = resolve()                      # uses config/defaults
    lib.files.runner                     # .../scripts/workflow-runner.py
    lib.verification_summary()           # what was actually checked
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["LibraryError", "LibraryFiles", "Library", "resolve", "sha256_file", "sha256_text",
           "load_pin", "default_pin_path", "default_search_paths", "unpinned_search_paths", "PIN_ENV"]


class LibraryError(RuntimeError):
    """Raised when the pinned Skills library is missing, mismatched or unverifiable."""


# Files the engine genuinely depends on. Each entry is (attribute, relative path); an absent one
# refuses startup, so an entry earns its place by having a reader.
_REQUIRED = (
    ("runner", "scripts/workflow-runner.py"),
    ("validator", "scripts/validate-workflows.py"),
    ("skill_sli_report", "scripts/skill-sli-report.py"),
    ("export_traces", "scripts/export-traces.py"),
    ("safe_yaml", "scripts/lib/safe_yaml.py"),
    ("lint_workflow", "scripts/lib/lint-workflow.py"),
)
# Directories the engine reads. `schema` is required even though no AgentOrg line opens it:
# `scripts/validate-workflows.py` — which this engine does invoke — is documented to validate
# manifests against `workflow/schema/workflow-manifest.schema.yaml`, so the directory is a
# precondition of a component we delegate to. The four directories that are *not* here
# (`workflow/templates`, `.skills-compiled`, `evals/golden`, `evals/tier3-behavioral`) have no
# such delegate: they serve the library's own skill and eval toolchain.
_REQUIRED_DIRS = (
    ("schema", "workflow/schema"),
    ("flat_skills", "skills-flat"),
    ("nested_skills", "skills"),
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
    "--contract-rework",
    "--max-steps",
)
# Directories pruned from manifest walks: never hash git objects or caches.
_MANIFEST_PRUNE = {".git", "__pycache__", ".DS_Store", ".pytest_cache"}

#: Environment variable naming a recorded pin document, so a CI checkout can be pinned without
#: writing into the repository.
PIN_ENV = "AGENTORG_LIBRARY_PIN"
#: The recorded pin's conventional name, beside this module's repository root. Absent in a fresh
#: checkout, which is what keeps pin enforcement opt-in rather than a refusal on every run.
_PIN_FILENAME = ".library-pin.json"


def default_pin_path() -> Path:
    """Where a recorded pin is looked for when the caller names none.

    The environment first, so a machine that checks out the library somewhere unusual can point at
    its own pin; otherwise the engine repository's `.library-pin.json`. A path is returned whether
    or not it exists — the caller decides what an absent pin means, and on a first run it means
    "nothing to compare against", not a failure.
    """
    override = os.environ.get(PIN_ENV)
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / _PIN_FILENAME


def load_pin(path: os.PathLike | str) -> tuple[str | None, dict[str, str], str | None]:
    """Read a pin document written by :meth:`Library.write_manifest`.

    Returns `(commit, files, library_root)`. A malformed document — or one carrying no file
    hashes — raises rather than returning an empty pin, because an empty manifest compares
    nothing and would then report a match: the same overstatement this module exists to remove.
    """
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LibraryError(f"cannot read library pin {target}: {exc}") from exc
    except ValueError as exc:
        raise LibraryError(f"library pin {target} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LibraryError(f"library pin {target} is not an object, so it records no comparison")
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise LibraryError(
            f"library pin {target} records no file hashes, so it cannot verify anything.\n"
            "  record one with: python3 -m engine.cli skills pin"
        )
    commit = payload.get("commit")
    root = payload.get("library_root")
    return (str(commit) if commit else None,
            {str(key): str(value) for key, value in files.items()},
            str(root) if root else None)


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
    """Resolved absolute paths into the parts of the pinned library the engine reads.

    Every field here has a reader in `engine/`. A path that nothing opens is not a fact about a
    dependency, it is a liability: it shows up in diagnostics as if it mattered, and when it is
    required a missing directory refuses startup over something no code would have touched.
    """

    root: Path
    runner: Path
    validator: Path
    skill_sli_report: Path
    export_traces: Path
    safe_yaml: Path
    lint_workflow: Path
    schema: Path
    flat_skills: Path
    nested_skills: Path

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
            "schema": str(self.schema),
            "flat_skills": str(self.flat_skills),
            "nested_skills": str(self.nested_skills),
        }


@dataclass
class Library:
    """A resolved view of the Skills library, with the two integrity facts kept apart.

    Attributes
    ----------
    files:
        Resolved paths.
    commit:
        The git commit SHA of the checkout, when the library is a git repo. Recorded whether or
        not it was compared against anything.
    manifest:
        Mapping of path-relative-to-root -> sha256, populated only by a manifest comparison or by
        :meth:`build_manifest`.
    capabilities_verified:
        The paths resolved *and* the runner advertises every flag this engine calls. True on any
        successful :func:`resolve`, and the only fact a run with no pin can honestly claim.
    commit_pinned:
        A supplied `expected_commit` was compared and matched.
    manifest_pinned:
        A supplied (or recorded) content manifest was compared and matched, file by file.
    pin_source:
        The pin document that was loaded, when one was.
    pin_note:
        Why an available pin was *not* compared — a pin recorded for a different checkout must
        not refuse this one, but the skip has to be visible rather than silently relaxing the
        check.
    """

    files: LibraryFiles
    commit: str | None = None
    manifest: dict[str, str] = field(default_factory=dict)
    capabilities_verified: bool = False
    commit_pinned: bool = False
    manifest_pinned: bool = False
    pin_source: str | None = None
    pin_note: str | None = None

    @property
    def pinned(self) -> bool:
        """True only when a recorded pin was actually compared and matched.

        Deliberately not named `verified`: it says which question was answered, and the two flags
        behind it say whether the evidence was a commit or file hashes.
        """
        return self.commit_pinned or self.manifest_pinned

    def verification_report(self) -> dict[str, Any]:
        """The integrity facts, separately, for a JSON consumer.

        `doctor --json` and the session's `/doctor` render this, so an operator sees "capabilities
        checked; content unpinned" instead of a boolean that would be true either way.
        """
        return {
            "capabilities_verified": self.capabilities_verified,
            "pinned": self.pinned,
            "commit_pinned": self.commit_pinned,
            "manifest_pinned": self.manifest_pinned,
            "pin_source": self.pin_source,
            "pin_note": self.pin_note,
            "manifest_files": len(self.manifest),
        }

    def verification_summary(self) -> str:
        """One honest sentence about what was checked, for a human-readable line."""
        if self.manifest_pinned:
            checked = f"content pin matched ({len(self.manifest)} files)"
        elif self.commit_pinned:
            checked = "commit pin matched; content not hash-checked"
        elif self.capabilities_verified:
            checked = "capabilities checked; content unpinned"
        else:
            checked = "nothing checked"
        if self.pin_note:
            checked += f"; pin not applied ({self.pin_note})"
        return checked

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

        Success is recorded as `capabilities_verified` — one of the two integrity facts this class
        reports, and the only one that is true without a pin to compare against.
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
        self.capabilities_verified = True

    def build_manifest(self) -> dict[str, str]:
        """Hash every file under the consumed directories.

        The manifest is the tamper-evidence record: recorded as a pin by `skills pin`, and
        re-checked on every startup against it, so a modified `SKILL.md` or a swapped
        `workflow-runner.py` is detected before a single token is spent. It covers `scripts/`,
        `workflow/` and `evals/` including the material this engine does not read, because a pin
        that skipped those directories would leave the library's own toolchain outside the
        tamper boundary — and that toolchain is invoked.
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
        """Compare the library against a recorded commit and/or manifest.

        What this method does *not* do is claim a comparison that never happened. With no pin it
        asserts capabilities and returns with both pin flags still false, because the honest answer
        to "did the content match a recorded pin?" is "there was no pin to match" — and a single
        flag set on both paths reads to every later reader as "hash checked" on a checkout where
        not one hash was compared.

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
            self.commit_pinned = True

        if expected_manifest is None:
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
        self.manifest_pinned = True

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


def unpinned_search_paths() -> list[str]:
    """The roots probed when nothing is pinned, in order of preference.

    **A function of its own, and the reason is that a surface must not keep its own copy of this
    list.** `default_search_paths()` returns this list *plus* a leading override, which is the shape
    :func:`resolve` needs; a reader answering "where would the engine look if I pinned nothing?" wants
    exactly what this returns, and spelling the three paths anywhere else is how the list in a console
    comes to disagree with the list the engine searches. `serve._cmd_library` reports this, so the app
    shows these rather than its own.

    Order matters: the documented sibling checkout first (the common layout), then the installer's
    conventional locations.
    """
    home = Path.home()
    return [
        str(home / "Documents" / "Projects" / "Skills"),
        str(home / ".zeroes-ones" / "skills"),
        str(home / ".agentorg" / "skills"),
    ]


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
    candidates += unpinned_search_paths()
    return candidates


def _pin_from(pin_path: os.PathLike | str | None, root: Path,
              lib: "Library") -> tuple[str | None, dict[str, str] | None]:
    """Load the recorded pin, unless it describes a different checkout.

    A pin names the root it was recorded from. Applying one to another checkout would refuse every
    run on a second machine over a pin that was never about that tree, so the mismatch is recorded
    on the handle as `pin_note` and nothing is enforced — visible in `doctor`, never silent. A pin
    with no recorded root is applied, because omitting the root is a deliberate statement that the
    pin describes whatever library it is pointed at.
    """
    target = Path(pin_path).expanduser() if pin_path else default_pin_path()
    if not target.is_file():
        return None, None
    commit, manifest, recorded_root = load_pin(target)
    lib.pin_source = str(target)
    if recorded_root is not None and Path(recorded_root).expanduser() != root:
        lib.pin_note = f"recorded for {recorded_root}, not {root}"
        return None, None
    return commit, manifest


def resolve(root: os.PathLike | str | None = None, *,
            expected_commit: str | None = None,
            expected_manifest: dict[str, str] | None = None,
            pin_path: os.PathLike | str | None = None,
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
    pin_path:
        Where a recorded pin is looked for when one is not passed directly. Defaults to
        :func:`default_pin_path` — `$AGENTORG_LIBRARY_PIN`, then `<repo>/.library-pin.json`. A path
        that does not exist means "no pin", not an error: an unpinned checkout is the normal first
        state.
    verify:
        When False, only path resolution and capability assertions run, and a recorded pin is not
        loaded. Used by tooling that wants to *build* a manifest for the first time: a tree being
        pinned cannot be checked against the pin it is about to write.
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

    files = LibraryFiles(root=resolved, **attrs)
    # The commit is a fact about the checkout, not a result of checking it, so it is recorded even
    # on the unverified path — that is what a pin document needs to carry.
    lib = Library(files=files, commit=None)
    lib.commit = lib._git_commit()

    if not verify:
        lib.assert_capabilities()
        return lib

    if expected_commit is None and expected_manifest is None:
        expected_commit, expected_manifest = _pin_from(pin_path, resolved, lib)
    lib.verify(expected_commit=expected_commit, expected_manifest=expected_manifest)
    return lib
