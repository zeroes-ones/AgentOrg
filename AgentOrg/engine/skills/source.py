#!/usr/bin/env python3
"""source.py — the interface a skill provider implements.

The engine consumes skills from the filesystem by default and from the library's MCP server
optionally. Both are behind this one protocol so nothing upstream cares where a skill's text
came from, and so the MCP backend can be enabled without touching the prompt builder.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .bundle import SkillBundle

__all__ = ["SkillSource"]


class SkillSource(ABC):
    """Where skills come from.

    Deliberately narrow: list the names, fetch one bundle, and — for a source that holds the Owner's
    own skills — say where those live. A source that needs more is doing work that belongs to the
    caller.
    """

    @abstractmethod
    def names(self) -> list[str]:
        """Every skill name this source can provide, sorted."""

    @abstractmethod
    def has(self, name: str) -> bool:
        """Whether a skill by this name exists."""

    @abstractmethod
    def load(self, name: str) -> "SkillBundle":
        """Load one skill.

        Raises
        ------
        SkillError
            When the skill is absent or its contract cannot be parsed. Never returns a
            partial bundle: a skill with no criteria would let a node claim completion
            without evidence.
        """

    @abstractmethod
    def text_of(self, name: str) -> str:
        """Raw SKILL.md text, for hashing and for a verbatim prompt slot."""

    def fingerprint(self) -> str:
        """A stable identifier for the source's current contents.

        Used to pin what a run actually read. The default is an empty string so a source
        that cannot cheaply fingerprint itself (a remote one) is not forced to lie; the
        filesystem source overrides it with real content hashes.
        """
        return ""

    def authored_dirs(self) -> dict[str, Path]:
        """The Owner's own skills, name -> directory; empty for a source that has none.

        Not abstract, and not part of loading. It answers "where do these names live", which the
        engine needs in exactly one place — presenting the catalogue to the pinned runner, whose own
        validator resolves skills relative to its own checkout (:mod:`engine.runner_view`). A source
        that reads only the pinned library answers with nothing, which is what keeps that view opt-in
        per project rather than a step every source has to implement.
        """
        return {}
