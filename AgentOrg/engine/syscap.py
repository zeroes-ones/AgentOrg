#!/usr/bin/env python3
"""syscap.py — the system capabilities, described for the person who grants them.

WHY THIS EXISTS
---------------
`sysctl_tools.py` answers "what may an agent do"; this answers "what am I being asked to allow, and
what does it reach". The two are different questions, written for different readers, and the console
cannot answer the second from the first.

The tool module's catalogue is shaped for a *model*: a name, a JSON schema, a capability string. A
person looking at a list of switches needs to know what each one lets loose on their machine, whether
it can change something, whether it will ask them first, and what happens if they say no. Three of
those facts are not in the catalogue at all, and the fourth is a single word (`mutates`) that does not
distinguish "writes a file in the project" from "changes the volume on your desk".

DESIGN
------
- **One definition, three surfaces.** The CLI prints this, the app renders it, and both agree because
  neither invents its own wording. The engine-side grants stay in `sysctl_tools.CATALOGUE`; this adds
  only the description a person needs, keyed by the same capability string.
- **Consequence before capability.** Each entry leads with what the person would notice — "changes
  the volume on your desk" — rather than the mechanism. A label like `system:media` is accurate and
  tells a person nothing.
- **It states what is *not* protected.** `run_automation` is the most powerful grant here and the
  honest description says so. An onboarding that lists capabilities without saying which one is
  dangerous is a list that reads as uniformly safe, which is worse than no list.
- **Refusals and consents are reported, not decided.** This module describes and summarises; it never
  grants. Granting is `people.hire --capability`, or `grant_consent` for a one-off action, and both
  already exist. A describe-layer that could also authorise would be a second authority.
- **A missing tool is a fact, not an error.** A capability declared in `SystemConfig` with no tool yet
  is reported as `available=False` rather than raising: the config and the catalogue drift while work
  is in progress, and a console that crashed on that would be unusable exactly then.

Usage:
    from .syscap import describe, pending, summary
    for entry in describe():          # every capability, with its consequence
        ...
    print(summary(roster_capabilities))   # "2 of 12 granted"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = ["Capability", "CAPABILITIES", "describe", "summary", "granted_in", "unmet",
           "console_payload"]

#: What each capability lets loose, in the order a person would want to read it: the harmless reads
#: first, the state changes next, the powerful ones last. Order is deliberate and load-bearing — a
#: list that opens with `run_automation` reads as a threat, and one that buries it reads as a
#: reassurance. Neither is honest.
#:
#: `reaches` is one sentence, written for someone who does not know what AppleScript is.
#: `changes` names what the person would notice, so "it changes something" is never the whole answer.
#: `caution` is present only where there is something real to be cautious about; its absence carries
#: meaning, so it is not set to a filler string on the safe entries.
CAPABILITIES: tuple[Capability, ...] = ()  # replaced below; see _build()


@dataclass(frozen=True)
class Capability:
    """One grant, described for the person deciding it."""

    grant: str
    title: str
    reaches: str
    #: What the person would notice changing, or "" when nothing does. A read is not a decision.
    changes: str = ""
    #: Set only where there is a real caution. Absent means "there is nothing to warn about", which
    #: is why it is not filled in with a reassuring phrase on the safe entries.
    caution: str = ""
    #: Whether at least one tool exists for this grant yet. False is not an error — the config and the
    #: catalogue drift while work is in progress, and a console must survive that.
    available: bool = True

    @property
    def changes_state(self) -> bool:
        return bool(self.changes)

    def as_dict(self) -> dict[str, Any]:
        return {"grant": self.grant, "title": self.title, "reaches": self.reaches,
                "changes": self.changes, "caution": self.caution,
                "changes_state": self.changes_state, "available": self.available}


#: The prose for each grant. Keyed by the same string `SystemConfig.CAPABILITIES` declares, so a
#: mismatch is a missing description rather than a wrong one — and `_build` reports the missing ones
#: instead of hiding them.
_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "system:state": {
        "title": "Read machine state",
        "reaches": "the battery, free disk space, how long the Mac has been up, and how many "
                   "applications are running",
    },
    "system:clipboard": {
        "title": "Use the clipboard",
        "reaches": "the text you last copied, and setting new text onto it",
        "changes": "replaces whatever you had copied",
        "caution": "An agent reading your clipboard can see whatever was there — a password you "
                   "copied, a private message. Grant it to assistants you would let read over "
                   "your shoulder.",
    },
    "system:screenshot": {
        "title": "Take screenshots",
        "reaches": "a picture of your screen, saved into the project folder",
        "changes": "adds a file to the project",
    },
    "system:media": {
        "title": "Control volume and media",
        "reaches": "the output volume and whether the sound is muted",
        "changes": "the volume on your desk, right now",
    },
    "system:open": {
        "title": "Open applications",
        "reaches": "launching an application — only ones you list, unless full access is on",
        "changes": "a window appearing on your screen",
    },
    "system:automation": {
        "title": "Run AppleScript",
        "reaches": "any scriptable application on this Mac",
        "changes": "almost anything the applications allow",
        "caution": "The most powerful grant here, and the allowlist is a name check rather than a "
                   "sandbox — an allowlisted script can still do whatever AppleScript can. Grant "
                   "this only where you would also hand over an unlocked terminal.",
    },
    "system:notify": {
        "title": "Speak and notify",
        "reaches": "speaking text aloud through the speakers, and posting a notification",
        "changes": "sound from your speakers, or a banner on your screen",
        "caution": "Speaking is audible to anyone in the room. It is not a private channel.",
    },
    "system:search": {
        "title": "Search the Mac",
        "reaches": "Spotlight's index — filenames and metadata across your whole account",
        "caution": "Search sees filenames outside the project folder, which the read grant does "
                   "not. Grant it only where knowing what is on the machine is the point.",
    },
    "system:power": {
        "title": "Control sleep",
        "reaches": "keeping the Mac awake for a bounded period, and putting it to sleep",
        "changes": "whether your Mac sleeps — including putting it to sleep now",
        "caution": "Sleeping interrupts whatever you are doing. Keeping awake drains the battery "
                   "on a laptop.",
    },
    "system:network": {
        "title": "Check the network",
        "reaches": "whether the Mac is online, and a throughput measurement",
        "caution": "A throughput test moves real data and can take up to a minute.",
    },
    "system:shortcuts": {
        "title": "Run your Shortcuts",
        "reaches": "Shortcuts you have already built — only the ones you list",
        "changes": "whatever that Shortcut does, which you chose when you wrote it",
        "caution": "This is the narrowest useful grant: you built the automation, so the agent is "
                   "pulling a lever rather than writing one. What the lever does is still whatever "
                   "you made it do.",
    },
    "system:softwareupdate": {
        "title": "Software updates",
        "reaches": "listing available macOS updates, and installing them",
        "changes": "your operating system, and possibly reboots the Mac",
        "caution": "The heaviest grant here. An update can take half an hour and cannot be undone "
                   "in the ordinary sense. Most agents should hold the listing half at most.",
    },
}


def _build() -> tuple[Capability, ...]:
    """Every declared capability with its description, in the reading order above.

    Driven by `SystemConfig.CAPABILITIES` rather than by `_DESCRIPTIONS`, so a capability added to the
    config appears here immediately — with a placeholder title until someone writes its prose, which
    is louder than a missing entry nobody notices. `available` comes from the tool catalogue, so a
    grant that buys nothing today says so.
    """
    declared = _declared_grants()
    have_tool = _grants_with_tools()
    out: list[Capability] = []
    for grant in declared:
        prose = _DESCRIPTIONS.get(grant)
        if prose is None:
            out.append(Capability(
                grant=grant,
                title=grant.split(":", 1)[-1].replace("_", " ").title(),
                reaches="(not yet described — this grant was added to the config without prose)",
                available=grant in have_tool))
            continue
        out.append(Capability(
            grant=grant,
            title=prose.get("title", grant),
            reaches=prose.get("reaches", ""),
            changes=prose.get("changes", ""),
            caution=prose.get("caution", ""),
            available=grant in have_tool))
    return tuple(out)


def _declared_grants() -> tuple[str, ...]:
    """The capability names the config declares, in its own order.

    Imported lazily: this module is read by surfaces that may not have a full config to hand, and a
    missing config should cost the descriptions rather than the whole console.
    """
    try:
        from .config import SystemConfig

        return tuple(SystemConfig.CAPABILITIES)
    except Exception:  # noqa: BLE001 - a config that will not import is not a reason to crash
        return tuple(_DESCRIPTIONS)


def _grants_with_tools() -> set[str]:
    """Which grants have at least one tool behind them today."""
    try:
        from .sysctl_tools import CATALOGUE

        return {e.capability for e in CATALOGUE}
    except Exception:  # noqa: BLE001
        return set()


# Built once at import. Cheap (two small tuples), and every caller wants the same list.
CAPABILITIES = _build()


def describe() -> list[Capability]:
    """Every capability, with what it reaches and what it changes."""
    return list(CAPABILITIES)


def granted_in(capabilities: Iterable[str]) -> list[Capability]:
    """The capabilities an agent's grant list actually reaches.

    Uses the same equality-with-`*` rule the tool registry enforces (`_granted_scoped`), so the
    console cannot show a grant as held when the tool layer would refuse it. That disagreement is the
    exact failure a status display must not have.
    """
    held = {str(c).strip() for c in (capabilities or [])}
    out: list[Capability] = []
    for entry in CAPABILITIES:
        if entry.grant in held or "system:*" in held:
            out.append(entry)
    return out


def unmet(capabilities: Iterable[str]) -> list[Capability]:
    """The declared capabilities this grant list does not reach — what an agent is *asking* for.

    Exists so a console can answer "what is it missing" without the person diffing two lists by eye.
    """
    held = {str(c).strip() for c in (capabilities or [])}
    return [e for e in CAPABILITIES if e.grant not in held and "system:*" not in held]


def console_payload(section: Any) -> dict[str, Any]:
    """The whole description as one document: every capability and the switches.

    **Why this is here and not in a surface.** The console's `system` command and the CLI's
    `system list` answer the same question, and the one failure this module exists to prevent is two
    surfaces saying different things about one grant. So the assembly lives with the descriptions, and
    each surface only decides how to *print* it.

    Deliberately says nothing about a *holder*: this is the description of the capability set, which is
    the same document whoever is reading it. A caller that wants to report "and this holder has these
    of them" adds `summary(grants)` itself — `systemcli.cmd_list` does exactly that — so the counts for
    a named holder never leak into the description an unrelated reader compares against.

    Shape is fixed by the app's decoder (`macos/Sources/AgentOrgKit/OrgController.swift` reads
    `capabilities`, `enabled`, `full_access`, `unavailable`, `summary`), so the keys are frozen.
    """
    entries = [c.as_dict() for c in describe()]
    return {
        "capabilities": entries,
        "enabled": bool(getattr(section, "enabled", False)),
        "full_access": bool(getattr(section, "allow_full_access", False)),
        "allow_apps": list(getattr(section, "allow_apps", None) or []),
        "allow_automation": list(getattr(section, "allow_automation", None) or []),
        "allow_shortcuts": list(getattr(section, "allow_shortcuts", None) or []),
        "unavailable": [e["grant"] for e in entries if not e["available"]],
        "summary": summary([])["text"],
    }


def summary(capabilities: Iterable[str]) -> dict[str, Any]:
    """A one-line summary a status bar can render, plus the detail behind it.

    Reports `unavailable` separately from `unmet`: a capability with no tool yet is a *build* fact,
    not a permission the agent lacks, and conflating them would have the console asking a person to
    grant something that would not work.
    """
    held = granted_in(capabilities)
    missing = unmet(capabilities)
    return {
        "granted": [e.grant for e in held],
        "unmet": [e.grant for e in missing],
        "granted_count": len(held),
        "total": len(CAPABILITIES),
        "unavailable": [e.grant for e in CAPABILITIES if not e.available],
        "text": f"{len(held)} of {len(CAPABILITIES)} system capabilities granted",
    }
