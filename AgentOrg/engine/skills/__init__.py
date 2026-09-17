#!/usr/bin/env python3
"""skills — read SKILL.md contracts and turn them into enforceable prompt material.

A skill is the library's unit of standard operating procedure. This package reads one,
extracts its typed contract (`workflow:` frontmatter), its completion criteria, its
Production Checklist and its anti-rationalization rules, and hands back a `SkillBundle`
that the prompt builder can enforce and the orchestrator can gate on.

Nothing here executes library content. SKILL.md bodies are read as *data* and confined to
a prompt slot; they can never redirect control flow.
"""

from .bundle import (
    ChecklistItem,
    SkillBundle,
    SkillContract,
    SkillError,
    Tier,
    parse_skill,
)
from .filesystem import FilesystemSkillSource
from .frontmatter import FrontmatterError, parse_frontmatter
from .source import SkillSource

__all__ = [
    "ChecklistItem",
    "FilesystemSkillSource",
    "FrontmatterError",
    "SkillBundle",
    "SkillContract",
    "SkillError",
    "SkillSource",
    "Tier",
    "parse_frontmatter",
    "parse_skill",
]
