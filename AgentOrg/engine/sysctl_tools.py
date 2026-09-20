#!/usr/bin/env python3
"""sysctl_tools.py — the machine itself, one scoped grant at a time.

WHY THIS EXISTS
---------------
`tools.py` confines an agent *inside* a project: every path is resolved against the workspace and a
path that escapes is refused. That answers "what may an agent do to the code". It does not answer
"what may an agent do to my Mac" — and that is the question a person asks the moment they want an
assistant that opens an app, reads the clipboard, or takes a screenshot.

The tempting answer is one switch: a `system:automation` capability that runs any AppleScript, and a
checkbox. That would be full control of the person's account behind a single grant, which is not what
"capabilities to control the system for anything granted" means. So this module is scoped grants, and
the scoping is the whole design:

- **One capability per kind of action.** `system:state` reads the battery; it does not write the
  clipboard. There is no grant that covers the rest.
- **A grant is a scope, not a boolean.** `allow_apps` names which applications may be launched and
  `allow_automation` names which handlers may run. "May open Safari" and "may script Safari" are two
  decisions, because the second can delete your files and the first cannot.
- **The dangerous capability is the narrowest one.** `run_automation` is the only tool here that can
  do *anything*, so it is the only one that requires a config prefix to match before it runs at all.

DESIGN
------
- **Absolute binary paths, never `PATH`.** Every call names `/usr/bin/osascript` and friends
  explicitly. `shutil.which` would resolve through `$PATH`, so an agent that can write a directory
  earlier in `$PATH` — which is exactly what `run_command` in a sandbox can do — could substitute its
  own `pmset`. The path is checked for existence at call time and a missing one is a refusal that
  names the binary, not a `FileNotFoundError`.
- **argv, never a shell string.** The AppleScript snippet is one argv element handed to `osascript`.
  It is never concatenated into a shell line, so no amount of quoting in a snippet reaches a shell.
  (This constrains *transport*, not the snippet's own power — see the ASK ONCE and KNOWN LIMITS
  sections.)
- **Clipboard text goes in on stdin, not argv.** `pbcopy` reads its input from stdin, and argv is
  world-readable through `ps`, so a clipboard write containing a password would be visible to every
  process on the machine for as long as it ran.
- **Bounded, and the bound is reported.** A wall-clock ceiling and an output cap, both from
  `SystemConfig`. A result that hit either says so; a silently clipped one produces a model reasoning
  about output it never saw. The ceiling is also what stops a tool hanging forever on an AppleScript
  dialog nobody is there to dismiss.
- **Nothing raises into a run.** A missing binary, a non-zero exit, a timeout and a refusal are all
  `ToolResult(ok=False, ...)`. An exception here would end the node and discard the work that led to
  the call.
- **`osascript`'s own success is not taken as the truth.** `set volume output volume 200` exits 0 and
  quietly sets 100. Refusing an out-of-range request is the honest alternative to reporting a value
  the machine did not use.
- **One tool is allowed not to wait, and it is named.** `keep_awake` starts `caffeinate` *detached*:
  the sleep assertion lives exactly as long as that process, so a call that waited for it and then hit
  the wall-clock ceiling would drop the assertion at the moment it mattered. The duration is the
  child's own `-t`, the ceiling degrades to a short startup window, and the report says the period was
  requested rather than observed. It is the only tool here with that shape, and `run_detached` exists
  for it alone — anything else that reached for it would be a tool quietly escaping its own bound.
- **A read can still cost something.** `network_status` with a measurement moves tens of megabytes of
  real traffic on a connection that may be metered, so it is bounded to a tighter ceiling than
  `max_seconds` and can be turned off entirely with `measure=false`. "Read-only" describes what it does
  to the machine, not what it costs the person.

ASK ONCE — WHAT THIS MODULE CAN AND CANNOT DO
---------------------------------------------
A read is not a decision. Overwriting the clipboard, changing the volume, launching an application,
running a script, speaking aloud, holding sleep off, putting the Mac to sleep, pulling a Shortcut's
lever, or installing an OS update are: each one destroys or alters something the person already had,
or interrupts them in a way they did not choose, and autonomy does not extend to it. So those **ten
tools** require a **consent decision** in the workspace's own decision ledger
(`engine/org/ledger.py`) before they will act.

The line is drawn on what a person would notice, not on whether bytes changed. `take_screenshot`
writes a file and does *not* ask: what it adds lands inside the workspace and destroys nothing the
person was holding. `say_message` changes no bytes at all and *does* ask, because a voice through the
speakers is audible to anyone in the room and cannot be taken back. `post_notification` sits between
them and does not ask: a banner is addressed to one person at one screen, is dismissed, and leaves
nothing changed. Where the line falls is argued at each tool.

That ledger is not a second approval system. It is the ledger `orchestrator.py` already owns for this
workspace and already writes its gate decisions into; the gate is `system-tool:<tool>:<agent>`, and the
approval is an ordinary `Ledger.record(...)` — the same call the orchestrator's gate machinery makes.
A refusal records the *request* at `system-request:<tool>:<agent>` so "what is my agent asking for" is
answerable from the ledger rather than only from a log line, and `grant_consent` is a named convenience
over `Ledger.record` so the gate naming exists in one place.

**What this cannot do, stated plainly.** An agent's tool call runs in the runner *subprocess*
(`engine/host.py` spawns `workflow-runner.py`, which loads the generated executor plugin), and the
gate machinery — `Orchestrator._auto_pass`, `_release_terminal_gate`, `Orchestrator.decide`, and the
`GateRequest` on the `Run` — lives in the *host* process behind a `Run` object this code has no
reference to. There is no bus in the subprocess worth the name and no way to park the run from here.
Consequences, all of them real:

1. **A destructive call fails fast; it does not block.** The tool is refused with the gate name and
   told not to retry. The run does not park waiting for the Owner, so an unattended goal cannot sit
   blocked on a screenshot of the desktop it will never get.
2. **The Owner approves between calls, not mid-node.** There is no queue and no notification from
   this side; the request sits in the ledger until someone reads it.
3. **A running orchestrator will not see a request written mid-run.** Its `Ledger` loads once at
   construction and holds its entries in memory; the file is append-only so nothing is lost, but the
   console shows the request only after a reload.

Closing (3) properly means a bus that reaches the host, and closing (2) means a real gate — both are
changes to `orchestrator.py` and `serve.py`, which this change deliberately does not touch. Until
then the refusal is the gate, and it says so.

FULL ACCESS — ONE SWITCH, AND EXACTLY WHAT IT MOVES
---------------------------------------------------
`system.allow_full_access` is the mode both reference agents ship (Reasonix's `bypassPermissions`,
Kimi's `--auto`): the person saying "I have handed this machine over". In this module it moves exactly
four things, and the list is closed:

1. `open_app` may launch any installed application, not only those in `allow_apps`.
2. `run_automation` may run any snippet, not only handlers matching `allow_automation`.
3. `run_shortcut` may run any Shortcut by name, not only those in `allow_shortcuts`.
4. a state-changing action no longer needs the one-off Owner consent.

(3) is the same kind of list as the other two, which is why it belongs in the same breath: it is a
*scope* being stepped aside, not a bound. The three allowlists are the three scopes this module keeps,
and a mode that lifted two of them while leaving the third closed would be a `bypassPermissions` that
does not bypass — the sort of gap nobody finds until an unattended run needs a lever pulled at 3am.

It does **not** move `system.enabled`, the per-agent `system:*` grant, `max_seconds`,
`max_output_bytes`, the screenshot directory's containment, the speaking-length cap, or the
shortening this module applies to a network measurement. A mode that lifted those would be "this agent
may do anything on any machine", which is not what either reference agent offers and not what a person
asking for convenience means. A bound is not an approval, and full access is permission to *act*.

KNOWN LIMIT — `allow_automation` IS A PREFIX, NOT A SANDBOX
----------------------------------------------------------
`run_automation` refuses a snippet that does not *start with* an allowlisted handler, which scopes the
grant the way `read:src/` scopes a file grant. It is genuinely narrower than a wildcard, and it is
genuinely **not** a sandbox: AppleScript has no capability model, so an allowlisted prefix can be
extended with more statements by the same script. What defends against that is the ask-once consent
(the Owner sees the tool being granted), the fact that the *person* chose the prefix, and this being
the one tool whose whole purpose is to be powerful. Do not read a prefix match as a guarantee.

PARTLY CLOSED — `system:` IS AN ELEVATED MARKER BY DEFAULT, BUT A FILE CAN OVERRIDE IT
------------------------------------------------------------------------------------
This section used to say a requisition asking for `system:automation` classified **T1** — "a bounded
helper within budget; auto-approved with notification" — and that was true when the markers knew only
about file and deploy capabilities. It is no longer true of the *default*: `DelegationConfig`
`.elevated_markers()` now ships `("write:", "deploy:", "exec:", "admin:", "system:")`, so in a fresh
configuration a `system:` requisition classifies **T3** and reaches the Owner. Measured, not assumed.

**What is still open is the override.** `elevated_markers()` returns the configured list verbatim
whenever `delegation.approval_tiers.elevated_capability_markers` is present and non-empty, so a
`credentials.json` that spells the older four markers out — as this checkout's own file does — silently
keeps the gap for every `system:` capability added since. That is not hypothetical: it is the state of
the file this was developed against, where the default is correct and the effective list is not.

Two readings of that, and both are defensible: the file is explicit, so it wins; or the default is a
*safety floor* and an explicit list should not be able to drop below it. This note does not resolve it,
because the answer belongs in `config.py`. **Until it does, an operator whose file lists markers should
add `system:` by hand:**

    "delegation": {"approval_tiers": {"elevated_capability_markers":
        ["write:", "deploy:", "exec:", "admin:", "system:"]}}

What holds either way: the capability is never *conferred* by hiring — `people._capabilities_for`
grants `read:*` and a scoped write and nothing else — so an auto-created helper holds no `system:`
grant unless a requisition explicitly asked for one. The gap is in the *approval* path, not in the
grant path, and it now affects the six newer capabilities (power, softwareupdate) exactly as it once
affected automation.

Usage:
    from engine.sysctl_tools import SystemTools
    tools = SystemTools(workspace_root=Path("~/code/app"), config=cfg.system, agent_id="ag_alice")
    result = tools.system_state({})
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .state import ENGINE_STATE_DIRNAME
from .tools import ToolResult

__all__ = [
    "SystemTools", "SystemCall", "CatalogEntry", "ConsentError",
    "CATALOGUE", "CONSENT_REQUIRED", "CAPABILITIES", "MUTATING_TOOLS",
    "OSASCRIPT", "SCREENCAPTURE", "PBCOPY", "PBPASTE", "OPEN", "PMSET", "DF",
    "SYSTEM_PROFILER", "LSAPPINFO", "SAY", "CAFFEINATE", "MDFIND", "NETWORK_QUALITY",
    "SHORTCUTS", "SOFTWAREUPDATE", "BINARIES",
    "MAX_SPEECH_CHARS", "MAX_NOTIFICATION_CHARS", "DEFAULT_AWAKE_SECONDS",
    "MAX_AWAKE_SECONDS", "NETWORK_TEST_SECONDS",
    "consent_gate", "request_gate", "grant_consent", "revoke_consent",
    "parse_battery", "parse_disk", "parse_boot_report", "count_running_apps",
    "parse_volume_settings", "parse_network_quality", "parse_update_list",
    "parse_shortcut_names", "run",
]

#: Every binary a tool in this module may invoke, by absolute path.
#:
#: Absolute because `$PATH` is agent-writable the moment the sandbox is on: a directory earlier in the
#: path holding a substituted `pmset` would turn a read-only battery query into arbitrary code
#: execution, and the substitution would be invisible from here. Verified present on the machine this
#: was built against; absence is a refusal that names the path.
OSASCRIPT = "/usr/bin/osascript"
SCREENCAPTURE = "/usr/sbin/screencapture"
PBCOPY = "/usr/bin/pbcopy"
PBPASTE = "/usr/bin/pbpaste"
OPEN = "/usr/bin/open"
PMSET = "/usr/bin/pmset"
DF = "/bin/df"
SYSTEM_PROFILER = "/usr/sbin/system_profiler"
LSAPPINFO = "/usr/bin/lsappinfo"
SAY = "/usr/bin/say"
CAFFEINATE = "/usr/bin/caffeinate"
MDFIND = "/usr/bin/mdfind"
NETWORK_QUALITY = "/usr/bin/networkQuality"
SHORTCUTS = "/usr/bin/shortcuts"
SOFTWAREUPDATE = "/usr/sbin/softwareupdate"
#: The interface read, which is the *fast* half of `system:network`.
#:
#: `networkQuality` is the thorough answer and costs ~10s of real traffic; asking "am I online" is a
#: different question and should not cost 40 MB. `ifconfig` answers it from the kernel in milliseconds
#: and lists which interfaces are up, which is the fact a model actually needs before deciding whether
#: a network failure explains anything else.
IFCONFIG = "/sbin/ifconfig"
#: Where the default route is read from. `route` lives in `/sbin` on macOS, not `/usr/sbin` — the
#: first attempt named the wrong directory, which is exactly the kind of guess the absolute-path rule
#: exists to make visible rather than to paper over with a `$PATH` search.
ROUTE = "/sbin/route"

BINARIES: tuple[str, ...] = (OSASCRIPT, SCREENCAPTURE, PBCOPY, PBPASTE, OPEN, PMSET, DF,
                             SYSTEM_PROFILER, LSAPPINFO, SAY, CAFFEINATE, MDFIND,
                             NETWORK_QUALITY, SHORTCUTS, SOFTWAREUPDATE, IFCONFIG, ROUTE)

#: The capability names this module honours. Held here as well as in `SystemConfig.CAPABILITIES` so a
#: caller can ask this module what it implements; the registry's gate refuses anything outside it.
CAPABILITIES: tuple[str, ...] = (
    "system:state",
    "system:clipboard",
    "system:screenshot",
    "system:media",
    "system:open",
    "system:automation",
    "system:notify",
    "system:search",
    "system:power",
    "system:network",
    "system:shortcuts",
    "system:softwareupdate",
)

#: Tools that change something the person already had. Each one needs a consent decision before it
#: acts — see the ASK ONCE section of the module docstring.
#:
#: `take_screenshot` is deliberately *not* here despite writing a file: it adds a file inside the
#: workspace and destroys nothing the person was holding. It is still `mutates=True`, so a read-only
#: run refuses it like any other write.
#:
#: `post_notification` and `network_status` are likewise *not* here, and for two different reasons.
#: A banner is addressed to one person at one screen, says nothing out loud, and leaves nothing
#: changed once it is dismissed — asking once for it would spend the Owner's attention on a decision
#: with no consequence, which is how a consent prompt becomes a reflex. `network_status` is a read
#: that happens to move bytes: it changes no state on the machine, and the bytes are the measurement.
#: Both are still `mutates=True` where a person would *see* them (the banner) or `False` where they
#: only measure, so a read-only run treats each one the way the person would.
#:
#: `keep_awake` **is** here despite being reversible and bounded. It holds the machine in a state the
#: person did not choose, and on a laptop it spends battery to do it; the ask is once per agent, so
#: the cost is one decision rather than one per call.
CONSENT_REQUIRED: frozenset[str] = frozenset({
    "write_clipboard", "set_volume", "set_mute", "open_app", "run_automation",
    "say_message", "keep_awake", "sleep_now", "run_shortcut", "install_os_updates",
})

#: The largest clipboard payload accepted. Sized like a file write rather than like a tool result
#: because that is what it is — content the agent authored, not content it read.
MAX_CLIPBOARD_BYTES = 512 * 1024

#: The longest message `say_message` will speak, in characters.
#:
#: A length bound rather than a byte bound, because the cost of speech is *time*: `say` blocks until
#: the sentence is finished, so this is what keeps a spoken message inside `max_seconds` instead of
#: dying at the ceiling half-spoken. At a default speaking rate (~175 words/minute) 400 characters is
#: roughly fifteen seconds, which fits the shipped 20s ceiling with room to spare. A model that wants
#: to say more should say less — a spoken summary of a result is a sentence, not a report, and a
#: report read aloud is a thing the person cannot skim.
MAX_SPEECH_CHARS = 400

#: The longest message `post_notification` will put on a banner.
#:
#: Smaller than the speech bound because a banner is *read*, not listened to: macOS truncates a long
#: notification body, so a message past this length would be reported as posted while the person saw
#: only its beginning. Capping here means the report and the banner agree about what was said.
MAX_NOTIFICATION_CHARS = 256

#: How long `keep_awake` will hold sleep off, in seconds, when the caller names no period. One hour:
#: long enough for a build or a download, short enough that a forgotten assertion is not a machine
#: that never sleeps again. `caffeinate -t` is the mechanism, so the ceiling is enforced by the
#: process itself rather than by this code remembering to kill it.
DEFAULT_AWAKE_SECONDS = 3600

#: The longest period `keep_awake` will accept, in seconds. Eight hours — a working day. A request
#: past this is refused rather than clamped, because a clamp would report a period the machine is not
#: holding: `caffeinate -t 999999999` is a machine that never sleeps, which is the failure mode this
#: bound exists to prevent.
MAX_AWAKE_SECONDS = 8 * 3600

#: How long `keep_awake` waits to learn whether `caffeinate` actually started.
#:
#: Not a lifetime — the assertion's lifetime is `-t`'s. This is only long enough for a bad flag or a
#: missing binary to fail loudly, so a "held sleep off" report is not given about a process that died
#: on its first line. Deliberately small, because every second here is a second the tool call blocks
#: for no reason once the child is running.
_AWAKE_STARTUP_WINDOW_S = 3

#: The wall-clock ceiling for a throughput measurement, in seconds, independent of `max_seconds`.
#:
#: `networkQuality` is genuinely slow — it runs a real upload and download — and it takes `-M` as its
#: own maximum runtime. This is smaller than a typical `max_seconds` on purpose: a throughput figure
#: is worth about ten seconds and not worth a minute, so the tool passes this to the binary *and* uses
#: it as the subprocess ceiling. A ceiling that only killed the process would still leave the run
#: waiting; a `-M` that only bounded the test would still let the binary hang on the config request.
#: Both, and the result says which bound was the one that bit.
NETWORK_TEST_SECONDS = 15

#: The ledger gate an approval lives at. Prefixed `system-tool:` rather than `system:` so it cannot
#: be confused with a capability name (`system:open`) in a ledger listing.
_GATE_PREFIX = "system-tool"
_REQUEST_PREFIX = "system-request"

#: How long to wait for a killed process to actually die before escalating to SIGKILL. The same grace
#: the sandbox uses, and for the same reason: a process that ignores SIGTERM must not become a hang.
_KILL_GRACE_S = 5


class ConsentError(RuntimeError):
    """Raised by `grant_consent` on a request that cannot be an Owner's approval.

    Narrow on purpose: every *runtime* refusal in this module is a `ToolResult(ok=False, ...)`, because
    a refusal a model can read is worth more than an exception that ends the node. This is only for a
    caller asking to record an approval that no person could have given.
    """


@dataclass(frozen=True)
class CatalogEntry:
    """One tool this module offers: how it is advertised and which grant it needs.

    Plain data rather than a `tools.Tool` so this module does not import the registry's own types back:
    the machine knowledge and the registry wiring stay in separate files with one direction of
    dependency, and a caller can read the catalogue without a `ToolRegistry`.
    """

    name: str
    capability: str
    description: str
    parameters: dict[str, Any]
    mutates: bool


#: The tool catalogue. Order is the advertisement order, which is stable so the tool block — and
#: therefore the cacheable prefix — does not shift between runs.
CATALOGUE: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        name="system_state",
        capability="system:state",
        description=(
            "Report this machine's state: battery and charging, free disk space, how long it has been "
            "up, the OS version, and how many applications are running. Read-only, and the cheapest "
            "way to find out whether the machine can take more work."),
        parameters={"type": "object", "properties": {}},
        mutates=False,
    ),
    CatalogEntry(
        name="read_clipboard",
        capability="system:clipboard",
        description=(
            "Read the current contents of the system clipboard as text. Use this when the person "
            "says something is 'on my clipboard' — it is the one channel from their desktop into "
            "this run."),
        parameters={"type": "object", "properties": {}},
        mutates=False,
    ),
    CatalogEntry(
        name="write_clipboard",
        capability="system:clipboard",
        description=(
            "Replace the system clipboard with text, so the person can paste it. This DESTROYS "
            "whatever they had copied and asks the Owner once per agent, so use it only when they "
            "asked for something to be on the clipboard."),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the text to place on the clipboard"},
            },
            "required": ["text"],
        },
        mutates=True,
    ),
    CatalogEntry(
        name="take_screenshot",
        capability="system:screenshot",
        description=(
            "Capture the whole screen to a PNG inside the workspace and return its path and size. "
            "For showing the person what the screen looks like; the image itself is not returned as "
            "text, so report the path."),
        parameters={"type": "object", "properties": {}},
        mutates=True,
    ),
    CatalogEntry(
        name="get_volume",
        capability="system:media",
        description="Report the system output volume, the input and alert volumes, and whether output is muted.",
        parameters={"type": "object", "properties": {}},
        mutates=False,
    ),
    CatalogEntry(
        name="set_volume",
        capability="system:media",
        description=(
            "Set the system output volume, 0-100. A change the person will hear, so it asks the Owner "
            "once per agent. A value outside 0-100 is refused rather than clamped — the machine "
            "clamps silently, which would make the report untrue."),
        parameters={
            "type": "object",
            "properties": {
                "level": {"type": "integer", "description": "0-100"},
            },
            "required": ["level"],
        },
        mutates=True,
    ),
    CatalogEntry(
        name="set_mute",
        capability="system:media",
        description=(
            "Mute or unmute the system output. Asks the Owner once per agent, like every other "
            "change to the person's machine."),
        parameters={
            "type": "object",
            "properties": {
                "muted": {"type": "boolean", "description": "true to mute, false to unmute"},
            },
            "required": ["muted"],
        },
        mutates=True,
    ),
    CatalogEntry(
        name="open_app",
        capability="system:open",
        description=(
            "Launch an application by name. Refused unless the name is one of the applications "
            "`system.allow_apps` lists — the list is the scope of this grant, so check it before "
            "asking rather than after."),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "the application's name, e.g. \"Safari\""},
            },
            "required": ["name"],
        },
        mutates=True,
    ),
    CatalogEntry(
        name="run_automation",
        capability="system:automation",
        description=(
            "Run an AppleScript or JXA snippet to drive an application. This is the most powerful "
            "tool here and is refused unless the snippet starts with a handler `system."
            "allow_automation` lists and the Owner has approved it once. Do not use it where a "
            "narrower tool will do."),
        parameters={
            "type": "object",
            "properties": {
                "script": {"type": "string",
                           "description": "the AppleScript or JXA source to run"},
                "language": {"type": "string",
                             "description": "'applescript' (default) or 'javascript'",
                             "enum": ["applescript", "javascript"]},
            },
            "required": ["script"],
        },
        mutates=True,
    ),
    CatalogEntry(
        name="say_message",
        capability="system:notify",
        description=(
            "Speak a short message aloud through the Mac's speakers. Use this only when the person is "
            "in the room and asked to be told something — speech is audible to everyone present, is "
            "not private, and cannot be taken back. It asks the Owner once per agent."),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": f"what to say aloud, at most {MAX_SPEECH_CHARS} characters"},
                "voice": {"type": "string",
                          "description": "optional voice name, e.g. \"Samantha\"; default is the "
                                         "system voice"},
            },
            "required": ["text"],
        },
        mutates=True,
    ),
    CatalogEntry(
        name="post_notification",
        capability="system:notify",
        description=(
            "Post a notification banner on the person's screen. Quieter than speaking and addressed to "
            "them alone: use this when they are at the Mac but not watching this run. It is not "
            "speech, so it does not ask the Owner first."),
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": f"the body text, at most {MAX_NOTIFICATION_CHARS} "
                                           "characters"},
                "title": {"type": "string",
                          "description": "the bold heading, e.g. \"AgentOrg\""},
                "sound": {"type": "boolean",
                          "description": "play the notification sound (default false — a silent "
                                         "banner is not an interruption)"},
            },
            "required": ["message"],
        },
        mutates=True,
    ),
    CatalogEntry(
        name="spotlight_search",
        capability="system:search",
        description=(
            "Search the Mac's Spotlight index for files by name or metadata query. Read-only. NOTE: "
            "this sees filenames across the whole account, including paths the `read:` grant does not "
            "cover — it can tell you a file exists without letting you open it."),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "a Spotlight query, e.g. \"invoice\" or "
                                         "\"kMDItemContentType == 'public.pdf'\""},
                "name_only": {"type": "boolean",
                              "description": "match on the file name only (default false: a full "
                                             "metadata query)"},
                "within": {"type": "string",
                           "description": "optional directory to limit the search to, e.g. "
                                          "\"~/Documents\""},
                "limit": {"type": "integer",
                          "description": "how many paths to return (default 50)"},
            },
            "required": ["query"],
        },
        mutates=False,
    ),
    CatalogEntry(
        name="keep_awake",
        capability="system:power",
        description=(
            "Hold sleep off for a bounded period, so a long download or build is not interrupted. The "
            "assertion ends by itself when the period elapses. Asks the Owner once per agent, and on a "
            "laptop it spends battery to do its job."),
        parameters={
            "type": "object",
            "properties": {
                "seconds": {"type": "integer",
                            "description": f"how long to stay awake, up to {MAX_AWAKE_SECONDS} "
                                           f"(default {DEFAULT_AWAKE_SECONDS})"},
                "display": {"type": "boolean",
                            "description": "also keep the display on (default false — the screen may "
                                           "sleep while the machine stays awake)"},
            },
        },
        mutates=True,
    ),
    CatalogEntry(
        name="sleep_now",
        capability="system:power",
        description=(
            "Put the Mac to sleep immediately. DESTRUCTIVE: this interrupts whatever the person is "
            "doing, may drop a call or a transfer in progress, and cannot be undone from here. It asks "
            "the Owner once per agent — do not use it to save power."),
        parameters={"type": "object", "properties": {}},
        mutates=True,
    ),
    CatalogEntry(
        name="network_status",
        capability="system:network",
        description=(
            "Report the active network interface and, when asked, measure throughput and "
            "responsiveness. The measurement moves real data and takes about ten seconds; the call is "
            "bounded, and a cut measurement says so rather than reporting a partial figure."),
        parameters={
            "type": "object",
            "properties": {
                "measure": {"type": "boolean",
                            "description": "run the throughput test (default true). Set false for a "
                                           "fast interface-only read."},
            },
        },
        mutates=False,
    ),
    CatalogEntry(
        name="run_shortcut",
        capability="system:shortcuts",
        description=(
            "Run one of the person's own Shortcuts by name. Refused unless the name is in "
            "`system.allow_shortcuts`: the person built the automation, so this pulls a lever they "
            "installed rather than writing a new one. Asks the Owner once per agent."),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "the Shortcut's name, exactly as it appears in the app"},
                "input": {"type": "string",
                          "description": "optional text to pass as the Shortcut's input"},
            },
            "required": ["name"],
        },
        mutates=True,
    ),
    CatalogEntry(
        name="list_os_updates",
        capability="system:softwareupdate",
        description=(
            "List the macOS updates that are available. Read-only and safe: it reports what could be "
            "installed without installing anything. Use this before asking to install."),
        parameters={
            "type": "object",
            "properties": {
                "no_scan": {"type": "boolean",
                            "description": "report only already-scanned updates instead of "
                                           "re-scanning (default true — a scan is slow)"},
            },
        },
        mutates=False,
    ),
    CatalogEntry(
        name="install_os_updates",
        capability="system:softwareupdate",
        description=(
            "Install available macOS updates. THE HEAVIEST ACTION HERE: it changes the operating "
            "system, can take half an hour, and may REBOOT the machine — ending this run and anything "
            "else the person had open. It installs only, never reboots by itself, and asks the Owner "
            "once per agent. Prefer `list_os_updates` and let the person install."),
        parameters={
            "type": "object",
            "properties": {
                "labels": {"type": "array", "items": {"type": "string"},
                           "description": "specific update labels from list_os_updates; omit to "
                                          "install recommended updates"},
                "restart": {"type": "boolean",
                            "description": "reboot after installing if required (default false). "
                                           "Setting this ends the run."},
            },
        },
        mutates=True,
    ),
)

#: Every tool name that changes something. Read off the catalogue rather than restated, so the two
#: cannot disagree about whether a tool mutates.
MUTATING_TOOLS: frozenset[str] = frozenset(entry.name for entry in CATALOGUE if entry.mutates)


# ── the transport ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SystemCall:
    """What one system command produced.

    `exit_code` is `None` when the command never reached an exit — it timed out or could not be
    started. `None` rather than `1` because "was stopped at the ceiling" and "failed" call for
    different next moves, and collapsing them would hide the ceiling from the model.
    """

    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool = False
    timed_out: bool = False
    reason: str = ""

    @property
    def combined(self) -> str:
        """stdout and stderr as one block, the way a terminal would have shown them."""
        parts = [self.stdout]
        if self.stderr:
            parts.append(self.stderr if not self.stdout else f"\n[stderr]\n{self.stderr}")
        return "".join(parts)


def _render(raw: bytes, limit: int) -> tuple[str, bool]:
    """Decode one captured stream, cutting it at the cap and saying whether it cut.

    Cut on a byte boundary rather than a line boundary, matching `sandbox._render` and for the same
    reason: a single very long line — a JXA program returning a base64 blob — would otherwise pass the
    cap intact, and the cap is what keeps a runaway command's output out of the model's context.
    """
    cut = len(raw) > limit
    return raw[:limit].decode("utf-8", errors="replace"), cut


def _preflight(argv: Sequence[str]) -> tuple[list[str] | None, SystemCall | None]:
    """Check a command's binary before running it: `(parts, None)` or `(None, refusal)`.

    Split out so both `run` and `run_detached` apply the same two rules — absolute path, and it exists
    and is executable — from one implementation. Two copies of a security preflight is how one of them
    drifts, and the one that drifts is the one nobody reads.
    """
    parts = [str(a) for a in argv]
    if not parts:
        return None, SystemCall(False, None, "", "no command was given",
                                reason="no command was given")
    binary = parts[0]
    if not os.path.isabs(binary):
        return None, SystemCall(False, None, "", "", reason=(
            f"refusing to run {binary!r}: every system command is named by absolute path, because a "
            "name resolved through $PATH can be substituted by anything that can write to a "
            "directory earlier in it."))
    if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        return None, SystemCall(False, None, "", "", reason=(
            f"{binary} is not an executable file on this machine, so this capability is unavailable "
            "here. The engine names absolute paths rather than searching $PATH, so this is a fact "
            "about the machine and not a misconfiguration."))
    return parts, None


def run(argv: Sequence[str], *, stdin_text: str | None = None, timeout_s: int = 20,
        max_output_bytes: int = 40_000) -> SystemCall:
    """Run one system command, bounded in time and output. Never raises.

    The binary is checked first and a missing one is a refusal naming the path: `FileNotFoundError`
    from `Popen` would be reported as a crash in the tool rather than as "this command is not on this
    machine", which is the difference between an operator fixing the machine and an operator
    debugging the engine.

    Its own process group, so the kill on timeout reaches anything the command spawned rather than
    only the command itself. `osascript` can spawn a helper that outlives it, and a helper still
    holding the pipe is how a ceiling turns into a hang the ceiling was supposed to prevent.

    The environment is **inherited, deliberately**. A scrubbed environment would be the tidier-looking
    choice, but `osascript` reaches the person's session through their own agent and `screencapture`
    through the window server, and a command that cannot find its session fails in ways that read like
    a bug in the script rather than in the sandbox around it. The confinement this module relies on is
    the capability gate and the allowlists, not the environment.
    """
    parts, refusal = _preflight(argv)
    if refusal is not None:
        return refusal
    assert parts is not None

    ceiling = max(1, int(timeout_s))
    try:
        process = subprocess.Popen(
            parts, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        return SystemCall(False, None, "", "", reason=f"cannot start {binary}: {exc}")

    payload = stdin_text.encode("utf-8") if stdin_text is not None else b""
    timed_out = False
    try:
        raw_out, raw_err = process.communicate(input=payload, timeout=ceiling)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
        try:
            raw_out, raw_err = process.communicate(timeout=_KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            _kill(process)
            raw_out, raw_err = b"", b""

    out, out_cut = _render(raw_out, int(max_output_bytes))
    err, err_cut = _render(raw_err, int(max_output_bytes))
    notes: list[str] = []
    if timed_out:
        notes.append(
            f"[stopped at the {ceiling}s ceiling — the command had not finished, and anything it "
            "left half-done is half-done. Raise system.max_seconds only if it genuinely needs "
            "longer.]")
    if out_cut or err_cut:
        notes.append(
            f"[output truncated at {int(max_output_bytes)} bytes; what is shown is the beginning.]")
    if notes:
        err = (err + "\n" if err else "") + "\n".join(notes)

    exit_code = None if timed_out else process.returncode
    return SystemCall(
        ok=(exit_code == 0),
        exit_code=exit_code,
        stdout=out,
        stderr=err,
        truncated=bool(out_cut or err_cut),
        timed_out=timed_out,
        reason=("the command exceeded its wall-clock ceiling" if timed_out else ""),
    )


def _terminate(process: "subprocess.Popen[bytes]") -> None:
    """Ask the command to stop, and its whole process group with it."""
    try:
        os.killpg(os.getpgid(process.pid), 15)
    except (OSError, AttributeError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass


def _kill(process: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(os.getpgid(process.pid), 9)
    except (OSError, AttributeError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _reap_later(process: "subprocess.Popen[bytes]") -> None:
    """Wait for a detached child in a daemon thread, so it cannot become a zombie.

    A detached command outlives the call on purpose, but nothing is left waiting to collect it when it
    exits — and an uncollected child stays a zombie process until the parent does. A tool whose whole
    purpose is to leave a process running must not leak one entry per call.

    The thread `wait`s and returns when the child does. It is a daemon because its only job is to
    collect an exit status: it holds no resource the caller needs, and making it non-daemon would let a
    one-hour sleep assertion keep the interpreter alive, which is the opposite of detached.
    """
    def _reap() -> None:
        try:
            process.wait()
        except Exception:  # noqa: BLE001 - a reap that fails must never surface anywhere
            pass

    try:
        threading.Thread(target=_reap, name="sysctl-reaper", daemon=True).start()
    except RuntimeError:
        # No thread can be started (interpreter shutting down). The child is left to be reaped by the
        # process's own exit; that is a leak of one process entry, and it is strictly better than
        # raising into the tool that started it.
        pass


def run_detached(argv: Sequence[str], *, max_seconds: int = 5) -> SystemCall:
    """Start a command that is *meant* to outlive this call, and confirm only that it started.

    `keep_awake` is why this exists, and its shape is the reason: `caffeinate -t N` holds the sleep
    assertion for exactly as long as the *process* lives, so the useful call is one that keeps running
    after the tool returns. Running it through `run` would wait for it and then, past the ceiling, kill
    it — which drops the assertion at the moment the ceiling bites. So the call that holds a machine
    awake cannot be the call that waits.

    What that means, stated plainly because it is a real weakening:

    - **`max_seconds` is not a lifetime here, it is a startup budget.** The child gets a short window
      to fail loudly (a bad flag, an unreadable binary) and after that it is confirmed started and left
      alone. The *duration* is bounded by the child's own `-t`, which is what makes a period limit
      meaningful rather than decorative.
    - **The output is not captured**, because nothing waits to read it. A detached child writing to a
      pipe nobody drains would block on a full buffer and hang a process it was supposed to leave
      running. Its stdout goes to `DEVNULL`; stderr is a pipe only for the startup window, so a bad
      flag's message can be reported, and it is closed from this side once the child is confirmed up.
    - **The child is reaped by a daemon thread.** A child that exits while nobody is waiting becomes a
      zombie, and a tool whose whole purpose is to leave a process running must not leak one every
      time it is called. The thread `wait`s for the child and exits when it does; it is a daemon, so it
      can never hold the interpreter open.

    A refusal here is a refusal to have *started*: an absolute-path violation, a binary that does not
    exist, or a start that failed within the startup window. A non-zero exit inside that window is
    reported as a failure with its message, because a `caffeinate` that died immediately did not hold
    anything and saying "held" would be the lie this whole module is careful not to tell.
    """
    parts, refusal = _preflight(argv)
    if refusal is not None:
        return refusal
    assert parts is not None
    binary = parts[0]
    window = max(1, int(max_seconds))
    try:
        process = subprocess.Popen(parts, shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   start_new_session=True)
    except OSError as exc:
        return SystemCall(False, None, "", "", reason=f"cannot start {binary}: {exc}")
    try:
        raw_err = process.communicate(timeout=window)[1]
    except subprocess.TimeoutExpired:
        # Still running after the startup window, which for a self-limiting command is success. The
        # stderr pipe is closed from this side without waiting: the child keeps the assertion and
        # loses only the reader it was never going to hear from.
        try:
            process.stderr.close()
        except OSError:
            pass
        _reap_later(process)
        return SystemCall(True, None, "", "", reason="")
    stderr = raw_err.decode("utf-8", errors="replace") if raw_err else ""
    code = process.returncode
    if code != 0:
        return SystemCall(False, code, "", stderr, reason=(
            f"{binary} exited {code} within its {window}s startup window, so it is not holding "
            "anything."))
    return SystemCall(True, 0, "", stderr, reason="")


# ── parsers ──────────────────────────────────────────────────────────────────
#
# Split out as plain functions over text so the tool is a thin shell around them. A test can then
# assert the parse against captured output without depending on the machine it runs on, which is what
# keeps this module's suite hermetic while still exercising the real command end to end.


def parse_battery(text: str) -> dict[str, Any] | None:
    """Read `pmset -g batt`: the charge, whether it is charging, and the time remaining.

    `pmset` is not a machine-readable format. Its second line is id, percentage, state and remaining
    time separated by `\t` and `;`, and the field order is not guaranteed: the state field reads
    `discharging` when discharging and `finishing charge` when nearly full. So the percentage is found
    by its own `%` marker, the state by an **exact** match against the words the tool uses, and the time
    by the `remaining` suffix — rather than by field position, which produced
    `69%; discharging; 1:08 present: true remaining`.

    Exact rather than substring for the state, and that is not pedantry: `charging` is a substring of
    `discharging`, so a `in` test reports a discharging battery as charging — the one fact about a
    laptop that changes what an unattended run should do.

    Returns None when no internal battery is reported, which is the honest answer on a desktop Mac —
    not `0%`, and not a fabricated figure.
    """
    if not text:
        return None
    source = ""
    for line in text.splitlines():
        lowered = line.lower()
        if "drawing from" in lowered:
            source = ("battery power" if "battery power" in lowered
                      else "ac power" if "ac power" in lowered else "")
            continue
        if "%" not in line or "InternalBattery" not in line:
            continue
        fields = [f.strip() for f in line.replace("\t", ";").split(";") if f.strip()]
        percent_field = next((f for f in fields if "%" in f), "")
        try:
            percent = int(percent_field.split("%")[0].strip().split()[-1])
        except (ValueError, IndexError):
            continue
        # `present: true` trails the line and is not a state; an exact match against the vocabulary
        # keeps it out, which a substring test could not do for `discharging`.
        state = next((f for f in fields
                      if f in ("charging", "discharging", "charged", "finishing charge",
                               "AC attached")), "")
        remaining = ""
        for field in fields:
            if "remaining" in field:
                remaining = field.split("remaining")[0].strip()
                break
        return {"percent": percent, "state": state, "remaining": remaining, "source": source}
    return None


def parse_disk(text: str) -> dict[str, Any] | None:
    """Read `df -h <mount>`: the size, the free space, the used percentage and the mount point.

    The last non-empty line is used because `df` prints a header above its one row. A line without
    enough fields yields None rather than a guess at which column meant what — a wrong free-space
    figure is worse than a reported failure to parse one.
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    fields = lines[-1].split()
    if len(fields) < 4:
        return None
    mount = fields[-1]
    try:
        used_percent = int(fields[4].rstrip("%")) if len(fields) > 4 and fields[4].endswith("%") else None
    except ValueError:
        used_percent = None
    return {"filesystem": fields[0], "size": fields[1], "used": fields[2], "avail": fields[3],
            "used_percent": used_percent, "mount": mount}


def parse_boot_report(text: str) -> dict[str, str]:
    """Read `system_profiler SPSoftwareDataType`: the OS version and time since boot.

    `system_profiler` rather than `uptime` or `sysctl` because neither of those is on the verified
    binary list this module is allowed to call, and this one also carries the OS version. Measured at
    ~0.13s for the software-data type, so the cost of the extra report is not worth a second call.
    """
    found: dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "system version":
            found["version"] = value.strip()
        elif key == "time since boot":
            found["uptime"] = value.strip()
    return found


def count_running_apps(text: str) -> int:
    """Count the applications `lsappinfo processlist` reports.

    Called on `processlist` rather than `list` because `list` is ~113 KB on a working machine and the
    default output cap is 40 KB — so the count would be made from a truncated listing, which is a
    number that is quietly wrong rather than absent. `processlist` is one line of `ASN:0x…-"Name":`
    items, ~7 KB, and carries the same application array.

    Counted by the ASN-name token rather than by splitting on whitespace: an application name contains
    spaces and is quoted, and `System Settings` is one application, not two.
    """
    return len(re.findall(r'ASN:0x[0-9a-f]+(?:-0x[0-9a-f]+)*-"', text or ""))


def parse_volume_settings(text: str) -> dict[str, Any]:
    """Read AppleScript's `get volume settings` record.

    The record is `output volume:32, input volume:29, alert volume:100, output muted:false`. Only the
    keys actually present are returned, so a machine that omits one is not reported as zero for it.
    """
    found: dict[str, Any] = {}
    for chunk in (text or "").split(","):
        key, _, value = chunk.partition(":")
        key = key.strip().lower().replace(" ", "_")
        value = value.strip()
        if not key or not value:
            continue
        if key == "output_muted":
            found["muted"] = value.lower() == "true"
            continue
        try:
            found[key] = int(value)
        except ValueError:
            found[key] = value
    return found


def parse_network_quality(text: str) -> dict[str, Any]:
    """Read `networkQuality -c` JSON down to the handful of fields worth reporting.

    Only the scalar summary fields are kept. The raw document is ~4 KB and most of it is timing arrays
    (`il_h2_req_resp`, `lud_foreign_*`) whose individual entries say nothing a model can act on; what
    is useful is the interface, the idle latency, the responsiveness scores and the throughputs.

    `dl_throughput` and `ul_throughput` are in **bits per second**, which is what the man page says and
    what the figures (190225248 for a ~190 Mbps link) confirm. They are converted to Mbps here so the
    report is readable — the conversion is named in the returned `unit` field rather than left for the
    reader to guess from the magnitude.

    Returns None when the text is not the JSON this expects. That is the honest answer when
    `networkQuality` was killed at the ceiling mid-test and the JSON is truncated: a partial document
    is not a measurement, and reporting a throughput parsed from half a run would be a figure invented
    from a cut. The caller distinguishes "no JSON" from "JSON with no test in it" by the keys present.
    """
    import json

    try:
        document = json.loads(text or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    out: dict[str, Any] = {}
    if document.get("interface_name"):
        out["interface"] = str(document["interface_name"])
    if isinstance(document.get("base_rtt"), (int, float)):
        out["idle_latency_ms"] = round(float(document["base_rtt"]), 1)
    for key, label in (("dl_throughput", "down_mbps"), ("ul_throughput", "up_mbps")):
        value = document.get(key)
        if isinstance(value, (int, float)):
            out[label] = round(float(value) / 1_000_000, 1)
    for key, label in (("dl_responsiveness", "down_rpm"), ("ul_responsiveness", "up_rpm")):
        value = document.get(key)
        if isinstance(value, (int, float)):
            out[label] = round(float(value))
    if document.get("test_endpoint"):
        out["endpoint"] = str(document["test_endpoint"])
    if document.get("os_version"):
        out["os"] = str(document["os_version"])
    return out


def parse_update_list(text: str) -> list[str]:
    """Read `softwareupdate -l` output for the update labels it offers.

    `softwareupdate` is the one command here whose *success* is reported on stderr: with nothing to
    install it prints `No new software available.` on stderr and only a banner on stdout, and with
    updates it prints `* Label: <name>` lines, each followed by Title/Version/Size indented beneath.
    So the labels are read by their own `* Label:` marker rather than by position, and the caller
    unions both streams before parsing.

    Returns an empty list both for "no updates" and for unparseable output, which is why the caller
    reports the raw text alongside the count rather than only the number: "0 updates" and "I could not
    read the answer" are different facts, and this function cannot tell them apart on its own.
    """
    labels: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("* Label:"):
            continue
        label = stripped.split(":", 1)[1].strip()
        if label:
            labels.append(label)
    return labels


def parse_shortcut_names(text: str) -> list[str]:
    """Read `shortcuts list` into the names it printed, one per line.

    The command prints a bare name per line and nothing else, so there is no marker to key on — the
    whole parse is "non-empty lines, trimmed". Kept separate from the tool because a test can then
    assert it against captured output, and because the day the format changes this is the one function
    that has to change.
    """
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def parse_say_voices(text: str) -> list[str]:
    """Read `say -v ?` into the voice names it offers. Used to check a requested voice exists.

    A voice that is not installed makes `say` exit non-zero with a message rather than falling back to
    the default, so naming a real one is required for the call to speak at all. The lines are
    `Name              en_US    # sample`, so the name is everything before the first run of two or
    more spaces — matching on whitespace rather than on a fixed column, because the column widths are
    language-dependent.
    """
    names: list[str] = []
    for line in (text or "").splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        head = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0].strip()
        if head:
            names.append(head)
    return names


def consent_gate(tool: str, agent_id: str) -> str:
    """The ledger gate a tool's approval lives at: one per agent, per tool.

    Per agent because two agents in one org are two different principals — approving the volume change
    for an assistant is not approving it for a reviewer. Per tool because the approval is about the
    *kind of action*, not the argument: "may change the volume", never "may set it to 50".
    """
    return f"{_GATE_PREFIX}:{tool}:{agent_id}"


def request_gate(tool: str, agent_id: str) -> str:
    """The ledger gate a refused call's *request* is recorded at. Kept distinct from the approval so
    the audit trail never confuses "someone asked" with "someone said yes"."""
    return f"{_REQUEST_PREFIX}:{tool}:{agent_id}"


def _ledger_for(state_dir: Path) -> Any:
    """The workspace's decision ledger, or None when there is not one to read.

    Imported here rather than at module scope because `engine.org` pulls in the whole organisation
    layer — roster, policy, router — and this module is loaded on the tool path of every system call.
    """
    from .org.ledger import Ledger

    path = state_dir / "ledger.jsonl"
    if not path.is_file():
        return None
    try:
        return Ledger(path=path)
    except Exception:  # noqa: BLE001 - an unreadable ledger means "not approved", never a crash
        return None


def grant_consent(state_dir: Path | str, *, tool: str, agent_id: str, by: str,
                  note: str = "") -> Path:
    """Record the Owner's approval for one tool, for one agent, in the run's decision ledger.

    A named convenience over `Ledger.record` rather than a store of its own: the approval *is* a
    ledger decision, which is what makes it reviewable, attributable and impossible to re-make
    silently. This is where a grant comes from — the tool path only ever reads.

    Refuses a `by` that names an agent. An approval an agent can give itself is not an approval, and
    the guard is what makes that a property of the code rather than a convention: the tool layer never
    calls this, and a caller that reaches for it on an agent's behalf is told no.
    """
    from .org.ledger import Ledger

    name = str(tool or "").strip()
    if name not in CONSENT_REQUIRED:
        raise ConsentError(
            f"{name!r} is not a tool that needs an approval. Only {', '.join(sorted(CONSENT_REQUIRED))} "
            "change something the person already had; a read is not a decision.")
    principal = str(by or "").strip()
    if not principal:
        raise ConsentError(
            f"approving {name} requires `by` to name who approved it: an unattributed decision in an "
            "append-only ledger cannot be reviewed later.")
    if principal.startswith("ag_"):
        raise ConsentError(
            f"`by={principal!r}` names an agent. An approval must come from a person: an agent that "
            "can approve its own destructive call has not been gated at all.")
    if not str(agent_id or "").strip():
        raise ConsentError(
            f"approving {name} requires an agent id, because the approval is per agent: a grant with "
            "no holder would read as a grant to everyone.")

    path = Path(state_dir) / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    Ledger(path=path).record(
        gate=consent_gate(name, agent_id),
        choice="approved",
        rationale=(note or f"{principal} approved {name} for {agent_id}"),
        by=principal,
        reversible=True,
        confidence="high",
        rejected_alternatives=["leaving the agent to retry a call that can never succeed"],
    )
    return path


def revoke_consent(state_dir: Path | str, *, tool: str, agent_id: str, by: str,
                   note: str = "") -> Path:
    """Supersede a standing approval, so the next call is refused again.

    Superseding rather than deleting, because the ledger is append-only and "the approval was
    withdrawn, on this date, for this reason" is exactly the fact a later reader needs.
    """
    from .org.ledger import Ledger

    gate = consent_gate(str(tool or "").strip(), agent_id)
    path = Path(state_dir) / "ledger.jsonl"
    if not path.is_file():
        raise ConsentError(f"no ledger at {path}, so there is no approval to withdraw")
    ledger = Ledger(path=path)
    live = ledger.current(gate)
    if live is None or str(live.choice) != "approved":
        # A gate holding `revoked` (or a pending request) has nothing to withdraw. Saying so is what
        # keeps the ledger from filling with withdrawals of a withdrawal, each one implying a live
        # approval that does not exist.
        raise ConsentError(
            f"no live approval for {gate}; nothing to withdraw"
            + (f" (the live decision is {str(live.choice)!r})" if live is not None else ""))
    ledger.supersede(gate, choice="revoked",
                     rationale=(note or f"{by} withdrew the approval for {tool}"),
                     by=by, reversible=True, confidence="high")
    return path


class SystemTools:
    """The machine-facing tools, bound to one workspace and one agent.

    Parameters
    ----------
    workspace_root:
        The project the agent works in. The default screenshot directory is inside it, and a relative
        `screenshot_dir` is resolved against it — so a misconfigured path cannot quietly point the
        engine at the person's desktop folder.
    config:
        The `SystemConfig` section. Read only: this class never writes configuration.
    agent_id:
        The agent holding the grants, used to key the per-agent approval. Empty means no agent
        context, and an approval cannot be satisfied without a holder.
    state_dir:
        Where the decision ledger lives. Defaults to `<workspace_root>/.agent_state`, which is where
        the orchestrator puts it.
    """

    def __init__(self, *, workspace_root: Path | str, config: Any, agent_id: str = "",
                 state_dir: Path | str | None = None) -> None:
        self.root = Path(workspace_root).resolve()
        self.config = config
        self.agent_id = str(agent_id or "")
        self.state_dir = (Path(state_dir) if state_dir is not None
                          else self.root / ENGINE_STATE_DIRNAME)
        self.max_seconds = int(getattr(config, "max_seconds", 20) or 20)
        self.max_output_bytes = int(getattr(config, "max_output_bytes", 40_000) or 40_000)
        #: Whether the per-action allowlists and the ask-once gate step aside.
        #:
        #: Read once from config, like every other bound here, so nothing a tool call passes can
        #: widen it — the same rule that makes the sandbox profile non-negotiable from the child's
        #: side.
        self.full_access = bool(getattr(config, "allow_full_access", False))

    # ── the catalogue ───────────────────────────────────────────────────────

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], ToolResult]]:
        """Tool name -> bound handler, so the registry can advertise this object without knowing how
        any of it works."""
        return {
            "system_state": self.system_state,
            "read_clipboard": self.read_clipboard,
            "write_clipboard": self.write_clipboard,
            "take_screenshot": self.take_screenshot,
            "get_volume": self.get_volume,
            "set_volume": self.set_volume,
            "set_mute": self.set_mute,
            "open_app": self.open_app,
            "run_automation": self.run_automation,
            "say_message": self.say_message,
            "post_notification": self.post_notification,
            "spotlight_search": self.spotlight_search,
            "keep_awake": self.keep_awake,
            "sleep_now": self.sleep_now,
            "network_status": self.network_status,
            "run_shortcut": self.run_shortcut,
            "list_os_updates": self.list_os_updates,
            "install_os_updates": self.install_os_updates,
        }

    def _call(self, argv: Sequence[str], *, stdin_text: str | None = None,
              timeout_s: int | None = None) -> SystemCall:
        """Run a command under this instance's bounds.

        `timeout_s` may *tighten* the ceiling for one call — `network_status` runs a measurement that
        is worth ten seconds and not worth the configured minute — but it can only tighten it: a
        caller asking for longer than `system.max_seconds` gets the configured bound, because the
        ceiling is a property of the deployment and not an argument a call reaches around. The same
        rule as `full_access` being read once at construction: nothing a tool call passes may widen a
        bound.
        """
        ceiling = self.max_seconds
        if timeout_s is not None:
            ceiling = max(1, min(int(timeout_s), self.max_seconds))
        return run(argv, stdin_text=stdin_text, timeout_s=ceiling,
                   max_output_bytes=self.max_output_bytes)

    def _start_detached(self, argv: Sequence[str]) -> SystemCall:
        """Start a command that is meant to outlive the call. See `run_detached`.

        A method as well as a module function so a test can swap one attribute on an instance — the
        same seam `_call` gives for every other tool — and so `keep_awake` is exercised without
        anything actually holding the developer's machine awake.
        """
        return run_detached(argv, max_seconds=_AWAKE_STARTUP_WINDOW_S)

    def _failure(self, tool: str, called: SystemCall) -> ToolResult:
        """Turn a non-zero exit into a result the model can act on."""
        body = called.combined.strip() or "(the command produced no output)"
        return ToolResult(
            False,
            f"{tool} failed: exit {called.exit_code if called.exit_code is not None else '(killed)'}"
            f"\n{body}",
            truncated=called.truncated,
        )

    # ── consent ─────────────────────────────────────────────────────────────

    def _has_consent(self, tool: str) -> bool:
        """Whether the Owner has approved this tool for this agent, ever.

        Reads the ledger. An unreadable or absent ledger means *not approved* rather than an error:
        the absence of a decision is the absence of consent, and defaulting the other way would make
        a deleted file a way to self-approve.
        """
        if not self.agent_id:
            return False
        ledger = _ledger_for(self.state_dir)
        if ledger is None:
            return False
        try:
            decision = ledger.current(consent_gate(tool, self.agent_id))
        except Exception:  # noqa: BLE001
            return False
        return bool(decision is not None and str(decision.choice) == "approved")

    def _record_request(self, tool: str) -> None:
        """Note in the ledger that this agent asked, so the Owner can see the ask.

        A request is not a decision and lives at its own gate, so the audit trail never reads as
        though the call was approved. Recorded once per tool per agent: a loop that retries a refused
        call must not be able to fill the ledger with duplicates of one question.
        """
        if not self.agent_id:
            return
        try:
            from .org.ledger import Ledger

            path = self.state_dir / "ledger.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            # A path that does not exist yet is not an error: `Ledger.load` tolerates a missing file
            # and the first `record` creates it. So the engine's own ledger file is the one store,
            # rather than a second one appearing whenever the request happens to arrive first.
            ledger = Ledger(path=path)
            gate = request_gate(tool, self.agent_id)
            if ledger.current(gate) is None:
                ledger.record(
                    gate=gate, choice="requested",
                    rationale=f"{self.agent_id} asked to use {tool}",
                    by=self.agent_id, reversible=True, confidence="low",
                )
        except Exception:  # noqa: BLE001 - a request that cannot be recorded must not fail the call
            pass

    def _needs_consent(self, tool: str) -> ToolResult | None:
        """The ask-once gate, or None when the call may proceed.

        Returns a refusal naming the exact gate the Owner's approval would land at, because a refusal
        the person cannot act on is a dead end for them and a retry loop for the agent.
        """
        # `allow_full_access` is the mode both reference agents ship (Reasonix's
        # `bypassPermissions`, Kimi's `--auto`), and it is the person saying "I have handed this
        # machine over". A consent prompt in that mode is not a safety property, it is a question
        # nobody is there to answer — the run would park forever.
        #
        # What it does *not* do: skip the record. The call proceeds and the ledger still learns of it
        # through the tool-step log, so "full access" widens permission without blinding the audit.
        if tool not in CONSENT_REQUIRED or self.full_access or self._has_consent(tool):
            return None
        self._record_request(tool)
        if not self.agent_id:
            return ToolResult(False, (
                f"{tool} refused: it changes something on the person's machine, and this call has no "
                "agent context, so there is no one whose approval could cover it.\n"
                "  required: a run bound to an agent holding this capability\n"
                "  held    : (no agent)\n"
                "Do not retry this call. Record why you needed it as an open question instead."))
        return ToolResult(False, (
            f"{tool} refused: it changes something on the person's machine, and the Owner has not "
            "approved it for this agent. Destructive actions are approved once per agent per tool —\n"
            f"  required: a live 'approved' decision at gate {consent_gate(tool, self.agent_id)!r} in\n"
            f"            {self.state_dir / 'ledger.jsonl'}\n"
            "  held    : no decision at that gate\n"
            "Your request has been recorded in that ledger so the Owner can see it. Do not retry this "
            "call; complete what you can without it, and record what you could not do as an open "
            "question."))

    # ── system:state ────────────────────────────────────────────────────────

    def system_state(self, args: dict[str, Any]) -> ToolResult:
        """Battery, disk, uptime, running apps — four reads, one report.

        One tool rather than four because they answer one question ("what is this machine doing"), and
        a model that has to spend four steps to learn it will often spend none.
        """
        battery_call = self._call([PMSET, "-g", "batt"])
        disk_call = self._call([DF, "-h", "/"])
        boot_call = self._call([SYSTEM_PROFILER, "SPSoftwareDataType"])
        apps_call = self._call([LSAPPINFO, "processlist"])

        battery = parse_battery(battery_call.stdout)
        disk = parse_disk(disk_call.stdout)
        boot = parse_boot_report(boot_call.stdout)
        apps = count_running_apps(apps_call.stdout)

        lines = ["system state:"]
        if battery is None:
            missing = "not reported" if battery_call.ok else "unavailable"
            lines.append(f"  battery   : no internal battery ({missing})")
        else:
            bits = [f"{battery['percent']}%"]
            if battery["state"]:
                bits.append(battery["state"])
            if battery["remaining"]:
                bits.append(f"{battery['remaining']} remaining")
            if battery["source"]:
                bits.append(f"on {battery['source']}")
            lines.append("  battery   : " + ", ".join(bits))
        if disk is None:
            lines.append(f"  disk      : could not read df output ({disk_call.exit_code})")
        else:
            percent = f", {disk['used_percent']}% used" if disk["used_percent"] is not None else ""
            lines.append(f"  disk      : {disk['avail']} free of {disk['size']} on "
                         f"{disk['mount']}{percent}")
        lines.append(f"  uptime    : {boot.get('uptime') or 'not reported'}")
        if boot.get("version"):
            lines.append(f"  software  : {boot['version']}")
        lines.append(f"  apps      : {apps} running (applications and background agents)")

        calls = (battery_call, disk_call, boot_call, apps_call)
        truncated = any(called.truncated for called in calls)
        if truncated:
            lines.append(f"  [a read was cut at {self.max_output_bytes} bytes; raise "
                         "system.max_output_bytes to see all of it]")
        # `ok` reflects whether any *read* actually worked. A report assembled entirely from failed
        # commands reads like an answer — "no internal battery", "0 apps" — when the truth is that
        # nothing was measured, so one working read is what makes it a state and none is a failure.
        if not any(called.ok for called in calls):
            failures = "; ".join(
                f"exit {c.exit_code if c.exit_code is not None else '(killed)'}: "
                f"{c.stderr.strip().splitlines()[0] if c.stderr.strip() else 'no output'}"
                for c in calls)
            return ToolResult(False, "\n".join(lines) + f"\n(every read failed — {failures})")
        return ToolResult(True, "\n".join(lines), truncated=truncated)

    # ── system:clipboard ────────────────────────────────────────────────────

    def read_clipboard(self, args: dict[str, Any]) -> ToolResult:
        """The clipboard as text. Empty is a legitimate answer, not an error."""
        called = self._call([PBPASTE])
        if not called.ok:
            return self._failure("read_clipboard", called)
        text = called.stdout
        if not text.strip():
            return ToolResult(True, "the clipboard is empty (or holds no text).")
        return ToolResult(True, f"clipboard ({len(text)} bytes):\n{text}",
                          truncated=called.truncated)

    def write_clipboard(self, args: dict[str, Any]) -> ToolResult:
        """Replace the clipboard, which is why this one asks first.

        Text travels in on stdin rather than argv: argv is visible in `ps` to every process on the
        machine, and the thing a person asks an assistant to put on their clipboard is often a
        password or a token.
        """
        gate = self._needs_consent("write_clipboard")
        if gate is not None:
            return gate
        if "text" not in args:
            return ToolResult(False, "text is required; a clipboard write with no text would erase "
                                     "what the person had copied")
        text = str(args.get("text") or "")
        if len(text.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
            return ToolResult(False, f"the text is larger than {MAX_CLIPBOARD_BYTES} bytes; the "
                                     "clipboard is for something a person will paste, not for a file")
        called = self._call([PBCOPY], stdin_text=text)
        if not called.ok:
            return self._failure("write_clipboard", called)
        return ToolResult(True, f"clipboard replaced with {len(text.encode('utf-8'))} bytes.")

    # ── system:screenshot ───────────────────────────────────────────────────

    def screenshot_dir(self) -> Path:
        """Where a screenshot may be written, resolved once.

        A relative `screenshot_dir` is resolved against the workspace and must stay inside it — the
        same rule the file tools apply, applied to the one path this module writes. An absolute one is
        honoured because the person chose it, except for the filesystem root, which would make
        `system.screenshot_dir: "/"` look like an entry and behave like a grant of the whole disk.

        The default is `<workspace>/.agent_state/screenshots/`: inside the workspace, and in the one
        directory that is already ignored by git — a screenshot of someone's whole desktop is not
        project content and must not be committable by accident.
        """
        raw = str(getattr(self.config, "screenshot_dir", "") or "").strip()
        if not raw:
            return self.state_dir / "screenshots"
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
            if str(resolved).rstrip("/") in ("", "/"):
                raise ValueError(
                    "system.screenshot_dir is the filesystem root, which is not a directory to put "
                    "screenshots in. Name a folder, or leave it empty for the workspace default.")
            return resolved
        resolved = (self.root / candidate).resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ValueError(
                f"system.screenshot_dir {raw!r} resolves outside the workspace ({resolved}); a "
                "relative screenshot directory must stay inside the project. Use an absolute path if "
                "you really mean somewhere else.") from None
        return resolved

    def take_screenshot(self, args: dict[str, Any]) -> ToolResult:
        """Capture the screen into the workspace and report the path and size."""
        try:
            target_dir = self.screenshot_dir()
        except ValueError as exc:
            return ToolResult(False, str(exc))
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(False, f"cannot create the screenshot directory {target_dir}: {exc}")

        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        target = target_dir / f"screenshot-{stamp}-{os.getpid()}.png"
        # `-x` silences the shutter, so a capture cannot be heard from another room; `-o` omits the
        # window shadow, which is a large transparent border and nothing else. Neither is
        # interactive, which matters: an interactive capture would park on a selection UI nobody is
        # there to complete until the ceiling killed it.
        called = self._call([SCREENCAPTURE, "-x", "-o", str(target)])
        if not called.ok:
            return self._failure("take_screenshot", called)
        if not target.is_file():
            return ToolResult(False, (
                "screencapture exited 0 but wrote no file. On macOS this is the screen-recording "
                "privacy permission being denied rather than an error: grant Screen Recording to the "
                "process running the engine in System Settings > Privacy & Security."))
        size = target.stat().st_size
        return ToolResult(True, f"screenshot written: {target} ({size} bytes)\n"
                                 "The image is not returned as text — report the path.",
                          paths=[str(target)])

    # ── system:media ────────────────────────────────────────────────────────

    def get_volume(self, args: dict[str, Any]) -> ToolResult:
        """Read the volume settings. No consent: reading a setting changes nothing."""
        called = self._call([OSASCRIPT, "-e", "get volume settings"])
        if not called.ok:
            return self._failure("get_volume", called)
        settings = parse_volume_settings(called.stdout)
        if not settings:
            return ToolResult(False, f"could not read the volume settings from {called.stdout.strip()!r}")
        output = settings.get("output_volume")
        muted = settings.get("muted")
        if output is None:
            return ToolResult(False, f"the volume settings carried no output volume: "
                                     f"{called.stdout.strip()!r}")
        state = "muted" if muted else "not muted"
        line = f"output volume: {output}/100 ({state})"
        if "input_volume" in settings:
            line += f"; input {settings['input_volume']}/100"
        if "alert_volume" in settings:
            line += f"; alert {settings['alert_volume']}/100"
        return ToolResult(True, line)

    def set_volume(self, args: dict[str, Any]) -> ToolResult:
        """Set the output volume, refusing a value outside the range rather than clamping it.

        `set volume output volume 200` exits 0 and leaves the volume at 100. Clamping here would
        produce the same lie one layer up; refusing tells the agent the bounds and leaves the reported
        state true.
        """
        gate = self._needs_consent("set_volume")
        if gate is not None:
            return gate
        raw = args.get("level")
        try:
            level = int(raw)
        except (TypeError, ValueError):
            return ToolResult(False, f"level must be a whole number from 0 to 100, got {raw!r}")
        if not 0 <= level <= 100:
            return ToolResult(False, (
                f"level must be from 0 to 100, got {level}. It is refused rather than clamped because "
                "macOS clamps silently and exits 0, so a clamped call would report a volume the "
                "machine never used."))
        called = self._call([OSASCRIPT, "-e", f"set volume output volume {level}"])
        if not called.ok:
            return self._failure("set_volume", called)
        return ToolResult(True, f"output volume set to {level}/100.")

    def set_mute(self, args: dict[str, Any]) -> ToolResult:
        """Mute or unmute the output. Asks first, like every other change."""
        gate = self._needs_consent("set_mute")
        if gate is not None:
            return gate
        raw = args.get("muted")
        if not isinstance(raw, bool):
            return ToolResult(False, f"muted must be true or false, got {raw!r}")
        called = self._call([OSASCRIPT, "-e",
                             f"set volume output muted {'true' if raw else 'false'}"])
        if not called.ok:
            return self._failure("set_mute", called)
        return ToolResult(True, f"output {'muted' if raw else 'unmuted'}.")

    # ── system:open ─────────────────────────────────────────────────────────

    def allowed_apps(self) -> list[str]:
        """The configured application allowlist, trimmed and de-blanked."""
        raw = getattr(self.config, "allow_apps", None) or []
        return [str(a).strip() for a in raw if str(a).strip()]

    def open_app(self, args: dict[str, Any]) -> ToolResult:
        """Launch an allowlisted application.

        The allowlist is the *scope* of the `system:open` grant, so it is checked here rather than
        left to the config's reader to remember. Matching is exact and case-insensitive, never by
        prefix: a prefix rule would let `Safari` authorise `SafariTechnologyPreview`, which is a
        different application from a different vendor channel with its own data.
        """
        gate = self._needs_consent("open_app")
        if gate is not None:
            return gate
        name = str(args.get("name") or "").strip()
        if not name:
            return ToolResult(False, "name is required: which application should be launched?")
        allowed = self.allowed_apps()
        # `allow_full_access` lifts the allowlist, which is what a person means by handing the machine
        # over: Reasonix calls it `bypassPermissions`, Kimi `--auto`. An empty allowlist in the scoped
        # mode means "nothing may be launched"; in this mode it means "no list is kept".
        if not self.full_access:
            if not allowed:
                return ToolResult(False, (
                    f"open_app refused: {name!r} cannot be launched because system.allow_apps is empty, "
                    "so no application is allowed.\n"
                    "  allowed : (none — system.allow_apps is empty)\n"
                    f"  asked   : {name}\n"
                    "Do not retry this call. Ask the Owner to add the application to system.allow_apps, "
                    "or complete the work without launching anything."))
            if name.lower() not in {a.lower() for a in allowed}:
                return ToolResult(False, (
                    f"open_app refused: {name!r} is not in system.allow_apps, which is the scope of this "
                    "grant.\n"
                    f"  allowed : {', '.join(allowed)}\n"
                    f"  asked   : {name}\n"
                "Do not retry this call. Use one of the allowed applications, or ask the Owner to add "
                "this one to system.allow_apps."))
        called = self._call([OPEN, "-a", name])
        if not called.ok:
            return self._failure("open_app", called)
        return ToolResult(True, f"launched {name}.")

    # ── system:automation ───────────────────────────────────────────────────

    def automation_prefixes(self) -> list[str]:
        """The configured handler prefixes, whitespace-normalised so the comparison is not defeated
        by a newline or an extra indent in the config file."""
        raw = getattr(self.config, "allow_automation", None) or []
        return [_normalise_script(str(p)) for p in raw if _normalise_script(str(p))]

    def run_automation(self, args: dict[str, Any]) -> ToolResult:
        """Run an allowlisted AppleScript or JXA snippet.

        Three gates, in this order, and the order is the design: the capability decides whether the
        tool is offered, the ask-once consent decides whether this agent may drive the machine at all,
        and the prefix decides how far. Consent first because the Owner is approving the *kind of
        action*; judging the snippet before consent exists would be answering "how far" about a
        question that has not been asked yet.

        `allow_full_access` steps the last two aside, exactly as it does for `open_app`: an empty
        `allow_automation` means "nothing may run" in the scoped mode and "no list is kept" in that
        one. The two tools are the pair the config's own documentation names, so they have to agree —
        a mode that lifted the app allowlist while leaving the script allowlist closed would be a
        `bypassPermissions` that does not bypass.

        The prefix match is a scope and is honestly not a sandbox — see KNOWN LIMIT in the module
        docstring. It is checked against the whitespace-normalised script so a reformatted snippet
        cannot slip past it, and it is case-sensitive because AppleScript's own vocabulary is.
        """
        gate = self._needs_consent("run_automation")
        if gate is not None:
            return gate
        script = str(args.get("script") or "")
        if not script.strip():
            return ToolResult(False, "script is required: there is nothing to run")
        language = str(args.get("language") or "applescript").strip().lower()
        if language not in ("applescript", "javascript"):
            return ToolResult(False, f"language must be 'applescript' or 'javascript', got "
                                     f"{language!r}")

        if not self.full_access:
            prefixes = self.automation_prefixes()
            if not prefixes:
                return ToolResult(False, (
                    "run_automation refused: nothing may be run because system.allow_automation is "
                    "empty.\n"
                    "  allowed : (none — system.allow_automation is empty)\n"
                    "  asked   : " + _preview(script) + "\n"
                    "Do not retry this call. Ask the Owner to allowlist the specific handler, or do the "
                    "work with a narrower tool."))
            normalised = _normalise_script(script)
            if not any(normalised.startswith(prefix) for prefix in prefixes):
                return ToolResult(False, (
                    "run_automation refused: the snippet does not start with any allowlisted handler.\n"
                    "  allowed : " + " | ".join(prefixes) + "\n"
                    "  asked   : " + _preview(script) + "\n"
                    "Do not retry this call with a reworded snippet. Ask the Owner to allowlist this "
                    "handler, or use a narrower tool."))

        argv = [OSASCRIPT]
        if language == "javascript":
            argv += ["-l", "JavaScript"]
        argv += ["-e", script]
        called = self._call(argv)
        if not called.ok:
            return self._failure("run_automation", called)
        result = called.stdout.strip() or "(the script returned nothing)"
        return ToolResult(True, f"automation ({language}) result:\n{result}",
                          truncated=called.truncated)

    # ── system:notify ───────────────────────────────────────────────────────

    def say_message(self, args: dict[str, Any]) -> ToolResult:
        """Speak a short message aloud.

        Two tools live under `system:notify` because there are two different decisions in it. Speaking
        is **intrusive and public**: it comes out of the speakers, everyone in the room hears it,
        it can land in the middle of someone's sentence, and it cannot be taken back. A banner is
        quiet, addressed to one person at one screen, and leaves nothing behind when it is dismissed.
        Collapsing them under one name would make "may tell me things" also mean "may talk over me".

        So this one asks the Owner once per agent, and `post_notification` does not.

        **The bound is characters, not bytes**, and that is the point of it: `say` blocks until the
        sentence is finished, so length is *time*. A message past `MAX_SPEECH_CHARS` is refused rather
        than truncated — a spoken sentence cut mid-clause is a different and worse thing than a long
        file truncated, and the person hears only the ruin.

        The text travels as one argv element and is never concatenated into anything: argv is visible
        via `ps`, which this accepts (a spoken message is public by definition), but it is not handed
        to a shell, so a message containing quotes is simply spoken with its quotes.
        """
        gate = self._needs_consent("say_message")
        if gate is not None:
            return gate
        text = str(args.get("text") or "")
        if not text.strip():
            return ToolResult(False, "text is required: there is nothing to say")
        if len(text) > MAX_SPEECH_CHARS:
            return ToolResult(False, (
                f"the message is {len(text)} characters; the most this will speak is "
                f"{MAX_SPEECH_CHARS}, because speech costs time rather than bytes and a long message "
                "would be stopped at the wall-clock ceiling half-spoken.\n"
                "  asked   : " + _preview(text) + "\n"
                "Shorten it to the sentence that matters, or use post_notification for something the "
                "person can read at their own pace."))
        argv = [SAY]
        voice = str(args.get("voice") or "").strip()
        if voice:
            # Checked against the installed voices rather than passed through: `say -v <unknown>` on
            # this machine is *silently ignored* and speaks in the default voice, so a requested voice
            # that does not exist would be reported as honoured while the person heard the wrong one.
            # That is the same class of lie as clamping a volume, and it gets the same treatment.
            voices = parse_say_voices(self._call([SAY, "-v", "?"]).stdout)
            if voice.lower() not in {name.lower() for name in voices}:
                return ToolResult(False, (
                    f"there is no voice named {voice!r} installed on this machine.\n"
                    f"  available: {', '.join(voices[:12])}"
                    + (f" … ({len(voices)} in all)" if len(voices) > 12 else "") + "\n"
                    "Omit `voice` to use the system voice, or name one from that list."))
            argv += ["-v", voice]
        argv.append(text)
        called = self._call(argv)
        if not called.ok:
            return self._failure("say_message", called)
        return ToolResult(True, f"spoke {len(text)} characters aloud.",
                          truncated=called.truncated)

    def post_notification(self, args: dict[str, Any]) -> ToolResult:
        """Post a notification banner. Quiet, and therefore no consent.

        The argument for the asymmetry with `say_message` is stated plainly because it is the kind of
        line that looks arbitrary unless it is argued: a banner does not make a sound, does not
        interrupt a spoken conversation, is addressed to the person at this Mac, and is gone when they
        dismiss it. It changes nothing they were holding. What it does do is appear on their screen —
        so it is `mutates=True` and a read-only run refuses it — but "changes what is on your screen
        for a few seconds" is not the same decision as "speaks into your room", and asking once for
        both would spend the Owner's attention where there is nothing to decide.

        The AppleScript is a **fixed program**, and the message is passed to it as a run-handler
        argument rather than interpolated into the source. That is what keeps a message containing a
        quote, a backslash or a line of AppleScript from being anything other than a string: the
        script is a constant in this file, and only its input varies.
        """
        message = str(args.get("message") or "")
        if not message.strip():
            return ToolResult(False, "message is required: there is nothing to post")
        if len(message) > MAX_NOTIFICATION_CHARS:
            return ToolResult(False, (
                f"the message is {len(message)} characters; the most a banner carries without being "
                f"truncated by macOS is {MAX_NOTIFICATION_CHARS}. A longer one would be reported as "
                "posted while the person saw only its beginning.\n"
                "  asked   : " + _preview(message) + "\n"
                "Put the outcome in the banner and the detail in the run's own output."))
        title = str(args.get("title") or "").strip() or "AgentOrg"
        sound = bool(args.get("sound", False))
        # A fixed program with the text arriving as argv. `display notification` is driven through
        # osascript's `-e` block plus `--` and two arguments, so no part of the message is ever parsed
        # as AppleScript. The `sound name` clause is chosen from a constant, never from an argument.
        script = ("on run argv\n"
                  "display notification (item 1 of argv) with title (item 2 of argv)"
                  + (" sound name \"Ping\"" if sound else "") + "\n"
                  "end run")
        called = self._call([OSASCRIPT, "-e", script, "--", message, title])
        if not called.ok:
            return self._failure("post_notification", called)
        return ToolResult(True, f"posted a notification titled {title!r}.")

    # ── system:search ───────────────────────────────────────────────────────

    def spotlight_search(self, args: dict[str, Any]) -> ToolResult:
        """Search the Spotlight index. Read-only, and honest about what read-only does not mean.

        **This can see files the agent's `read:` grant does not cover, and that is a real widening.**
        It is not a leak in the sense of a bug — `mdfind` is doing exactly what Spotlight does, and the
        operator granted `system:search` on purpose — but it *is* a hole in the story that "the agent
        only reaches what its capabilities name", and pretending otherwise would be the dishonest
        choice. What makes it acceptable rather than a mistake is the shape of what escapes:

        - **Filenames and metadata, not contents.** `spotlight_search` returns paths. Reading one is
          still refused by `read_file` unless the `read:` grant covers it, so this answers "does a file
          called X exist on this machine" and not "what is in it". `mdfind` *can* return content
          matches (`kMDItemTextContent`), which is why the docstring says so rather than claiming
          paths-only: the query language reaches further than the paths this returns, and a model
          should know that a content query tells it something was matched without telling it what.
        - **It is one capability, and it is named in the console.** A person granting `system:search`
          is told "Spotlight's index — filenames and metadata across your whole account" before they do
          it. The grant is the disclosure.

        The alternative — confining the search to the workspace — was rejected because it would make
        the tool useless for the question it exists to answer ("where is that file I made last week"),
        while *looking* like a search of the machine. A tool that silently searches only one directory
        is not more conservative, it is misleading.

        `within` narrows the search when the caller does not need the whole account. It is passed
        through as a literal path, with `~` **not** expanded by this module: `mdfind -onlyin ~` treats
        the tilde as a literal directory name and silently finds nothing, which would read as "no such
        file" rather than "the argument was wrong". So a `~` is refused with that explanation and the
        expanded form is named.
        """
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(False, (
                "query is required. A search with no query is not an empty search: `mdfind ''` exits 1 "
                "with 'Failed to create query', so there is nothing to return."))
        # A leading dash is read as a flag in either mode, and `--` cannot save it: mdfind answers
        # `Unknown option --` and exits 1. Measured — `mdfind -name -x`, `mdfind --`, `mdfind -x` all
        # exit 1 with usage text on stdout. That *is* a failure, so the tool would not lie about it;
        # caught here anyway because "Unknown option -x / Usage: mdfind …" tells a model nothing about
        # what to do next, while naming the fix does.
        if query.startswith("-"):
            return ToolResult(False, (
                f"the query {query!r} begins with a dash, which mdfind reads as a command-line option "
                "in both modes (and it rejects `--` as a terminator, so it cannot be escaped).\n"
                "Use a predicate instead — e.g. `kMDItemDisplayName == '*" + query.lstrip("-") +
                "*'` — or drop the leading dash."))
        argv = [MDFIND]
        if bool(args.get("name_only", False)):
            argv += ["-name", query]
        else:
            # A bare query, passed as one argv element so nothing in it reaches a shell.
            argv.append(query)
        within = str(args.get("within") or "").strip()
        if within:
            if within.startswith("~"):
                return ToolResult(False, (
                    f"within={within!r} begins with `~`, which mdfind treats as a literal directory "
                    "name — it does not expand it. Measured: `mdfind -onlyin ~ -name README.md` exits "
                    "**0** with no output, so the caller sees a successful search that found nothing "
                    "rather than a wrong argument. Pass an absolute path instead "
                    "(e.g. /Users/you/Documents)."))
            argv += ["-onlyin", within]
        try:
            limit = int(args.get("limit") or 50)
        except (TypeError, ValueError):
            return ToolResult(False, f"limit must be a whole number, got {args.get('limit')!r}")
        if limit < 1:
            return ToolResult(False, f"limit must be at least 1, got {limit}")
        called = self._call(argv)
        if not called.ok:
            return self._failure("spotlight_search", called)
        # mdfind prints `UserQueryParser` noise on stderr for every query and the paths on stdout, so
        # the paths are read from stdout alone. Reading `combined` would interleave the banner with the
        # first path and produce a file called "…/mdfind[123:456] [UserQueryParser] /Users/…".
        paths = [line.strip() for line in called.stdout.splitlines() if line.strip()]
        if not paths:
            return ToolResult(True, "no matches in the Spotlight index.")
        shown = paths[:limit]
        lines = [f"{len(paths)} match{'es' if len(paths) != 1 else ''} in the Spotlight index:"]
        lines += [f"  {path}" for path in shown]
        if len(paths) > len(shown):
            lines.append(f"  … {len(paths) - len(shown)} more; raise `limit` to see them.")
        lines.append(
            "These are paths only. Reading one still needs the `read:` grant that covers it — this "
            "tool reports that a file exists, not what is in it.")
        if called.truncated:
            lines.append(f"  [the listing was cut at {self.max_output_bytes} bytes, so the count "
                         "above is a lower bound]")
        return ToolResult(True, "\n".join(lines), truncated=called.truncated)

    # ── system:power ────────────────────────────────────────────────────────

    def keep_awake(self, args: dict[str, Any]) -> ToolResult:
        """Hold sleep off for a bounded period. Asks first, and the bound *is* the mechanism.

        `caffeinate -t <seconds>` does the work, and `-t` is what ends it: the assertion lives as long
        as the process, so the period is enforced by the machine rather than by this module remembering
        to clean up. Nothing outlives the call beyond the period that was asked for — which is the
        whole reason to use `caffeinate` rather than a launchd assertion or a `pmset` setting.

        That the assertion is held by the *process* has one consequence worth stating, because it
        shapes the tool: the call has to leave `caffeinate` running rather than wait for it. So the
        command is started detached (`run_detached`) and confirmed only to have started; the duration
        is `-t`'s, not the wall-clock ceiling's. A tool that waited for the process would kill it at
        the ceiling, dropping the assertion exactly when it was most needed.

        The period is bounded at `MAX_AWAKE_SECONDS` and refused rather than clamped past it, for the
        same reason `set_volume` refuses 200: a clamp would report an hour when the request was for a
        week, and the difference is the one a person would care about.

        It asks the Owner once because it is a state the person did not choose, held for a period they
        did not choose, and on a laptop it spends battery to hold it. Bounded and reversible, yes —
        which is why the ask is once per agent rather than per call.

        Returns `ok=False` when `caffeinate` died inside its startup window: a process that exited
        immediately is not holding anything, and "held sleep off" would be a claim the machine is not
        keeping.
        """
        gate = self._needs_consent("keep_awake")
        if gate is not None:
            return gate
        raw = args.get("seconds", DEFAULT_AWAKE_SECONDS)
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            return ToolResult(False, f"seconds must be a whole number, got {raw!r}")
        if seconds < 1:
            return ToolResult(False, f"seconds must be at least 1, got {seconds}")
        if seconds > MAX_AWAKE_SECONDS:
            return ToolResult(False, (
                f"seconds must be at most {MAX_AWAKE_SECONDS} (8 hours), got {seconds}. It is refused "
                "rather than clamped because a clamp would report a period the machine is not holding "
                "— and one call with a week in it is a Mac that never sleeps.\n"
                f"  asked   : {seconds}s\n"
                f"  allowed : up to {MAX_AWAKE_SECONDS}s\n"
                "Ask for the period the work actually needs; the assertion can simply be made again."))
        display = bool(args.get("display", False))
        # `-i` is idle sleep and `-s` is system sleep; neither touches the display, which is the right
        # default — a build does not need the screen on, and keeping it on is brighter and hotter.
        # `-d` is added only when asked. No `-u`, which would simulate user activity and wake the
        # display as a side effect, and no `-w`, which waits on a pid this call does not have.
        argv = [CAFFEINATE, "-i", "-s"]
        if display:
            argv.append("-d")
        argv += ["-t", str(seconds)]
        started = self._start_detached(argv)
        if not started.ok:
            detail = started.reason or "it could not be started"
            body = started.stderr.strip()
            return ToolResult(False, f"keep_awake failed: {detail}"
                                     + (f"\n{body}" if body else ""))
        return ToolResult(True, (
            f"holding sleep off for {seconds}s (display "
            f"{'kept on' if display else 'allowed to sleep'}).\n"
            "The assertion is held by the `caffeinate` process, whose own `-t` ends it; this call did "
            "not wait for it, so the period above is what was requested rather than what was "
            "observed."))

    def sleep_now(self, args: dict[str, Any]) -> ToolResult:
        """Put the Mac to sleep. Destructive, consent-gated, and honest about its reach.

        `pmset sleepnow` is the mechanism and it does not ask anybody: it sleeps the machine, which
        means whatever was mid-sentence in a call, mid-transfer in a download, or mid-run in another
        agent is interrupted. It cannot be undone from here — nothing in this module wakes the machine,
        and an unattended run cannot, because a sleeping Mac runs nothing.

        Treated like `install_os_updates` rather than like `keep_awake`: `keep_awake` holds a state off
        for a bounded period and is undone by waiting, while this takes the machine out of service
        right now. So it needs the Owner's approval, and — more importantly for an unattended run —
        the refusal says plainly that this cannot be part of a goal, because the goal stops here too.

        `pmset sleepnow` on macOS 27 has historically required no root for the sleep action (unlike the
        *settings* this module deliberately never changes); where it does, the exit and message come
        back through `_failure` rather than being predicted here.
        """
        gate = self._needs_consent("sleep_now")
        if gate is not None:
            return gate
        called = self._call([PMSET, "sleepnow"])
        if not called.ok:
            return self._failure("sleep_now", called)
        return ToolResult(True, (
            "the Mac has been told to sleep. This run stops here: nothing will execute until the "
            "machine is woken, and this module cannot wake it."))

    # ── system:network ──────────────────────────────────────────────────────

    def network_status(self, args: dict[str, Any]) -> ToolResult:
        """Report the interface, and optionally measure throughput. Slow, and bounded to say so.

        Two questions are collapsed under one capability and kept apart under one tool: "am I online"
        is answered by `ifconfig` in milliseconds, and "how fast is it" is answered by `networkQuality`
        in about ten seconds of real traffic. `measure=False` gets the first. That matters because the
        expensive half is expensive in a way a read normally is not: it uploads and downloads tens of
        megabytes, on a connection that might be metered, to answer a question the model may not have
        been asking.

        `networkQuality` is bounded **twice and by the same number**: `-M` bounds the test itself, and
        the subprocess ceiling bounds the process. Either alone is insufficient — `-M` does not cover
        the config request the binary makes before the test, and a subprocess kill alone would let a
        test that ignores its own limit run to the wall. `NETWORK_TEST_SECONDS` is smaller than a
        typical `max_seconds` on purpose: a throughput figure is worth ten seconds and not a minute.

        A measurement that was cut is reported as cut, with no throughput figure. Half a test is not a
        slower connection, and reporting the partial number would be a figure invented from a
        truncation.
        """
        interface_call = self._call([IFCONFIG])
        route_call = self._call([ROUTE, "-n", "get", "default"])
        if not interface_call.ok and not route_call.ok:
            return self._failure("network_status", interface_call)

        active = _parse_active_interfaces(interface_call.stdout)
        default = _parse_default_route(route_call.stdout)

        lines = ["network status:"]
        if default.get("interface"):
            bits = [default["interface"]]
            if default.get("gateway"):
                bits.append(f"gateway {default['gateway']}")
            lines.append("  route     : " + ", ".join(bits))
            lines.append("  default   : " + ("up" if default["interface"] in active
                                             else "the default route's interface is not active"))
        else:
            lines.append("  route     : no default route (this machine has no way out)")
        lines.append(f"  interfaces: {', '.join(active) if active else '(none active)'}")

        if not bool(args.get("measure", True)):
            lines.append("  throughput: not measured (measure=false). Pass measure=true for a "
                         "throughput test — it takes about "
                         f"{NETWORK_TEST_SECONDS}s and moves real data.")
            return ToolResult(True, "\n".join(lines), truncated=interface_call.truncated)

        # A measurement with no route would fail after the full `-M` wait, so it is skipped and said
        # aloud rather than spending the ceiling to prove the machine is offline.
        if not default.get("interface"):
            lines.append("  throughput: not measured — there is no default route, so the test would "
                         "fail after its full timeout rather than measure anything.")
            return ToolResult(True, "\n".join(lines))

        called = self._call([NETWORK_QUALITY, "-c", "-s", "-M", str(NETWORK_TEST_SECONDS)],
                            timeout_s=NETWORK_TEST_SECONDS + 5)
        ceiling = min(self.max_seconds, NETWORK_TEST_SECONDS + 5)
        measured = parse_network_quality(called.stdout)
        if called.timed_out:
            lines.append(f"  throughput: NOT measured — the test was stopped at its "
                         f"{ceiling}s ceiling. No figure is "
                         "reported because a cut test is not a slow connection.")
            return ToolResult(True, "\n".join(lines))
        if measured is None:
            lines.append("  throughput: the test produced no readable JSON"
                         + (f" — {_first_line(called.stderr)}" if called.stderr.strip() else "")
                         + ". No figure is reported rather than a guessed one.")
            return ToolResult(True, "\n".join(lines))
        if "down_mbps" not in measured and "up_mbps" not in measured:
            lines.append("  throughput: the test completed without reporting a throughput (this "
                         "happens when the machine is offline or the test host is unreachable).")
            return ToolResult(True, "\n".join(lines))
        if measured.get("down_mbps") is not None:
            lines.append(f"  down      : {measured['down_mbps']} Mbps")
        if measured.get("up_mbps") is not None:
            lines.append(f"  up        : {measured['up_mbps']} Mbps")
        if measured.get("idle_latency_ms") is not None:
            lines.append(f"  latency   : {measured['idle_latency_ms']} ms idle")
        if measured.get("down_rpm") is not None or measured.get("up_rpm") is not None:
            lines.append(f"  responsive: {measured.get('down_rpm', '?')} down / "
                         f"{measured.get('up_rpm', '?')} up RPM (higher is better)")
        if measured.get("endpoint"):
            lines.append(f"  test host : {measured['endpoint']}")
        return ToolResult(True, "\n".join(lines), truncated=called.truncated)

    # ── system:shortcuts ────────────────────────────────────────────────────

    def allowed_shortcuts(self) -> list[str]:
        """The configured Shortcut allowlist, trimmed and de-blanked."""
        raw = getattr(self.config, "allow_shortcuts", None) or []
        return [str(s).strip() for s in raw if str(s).strip()]

    def run_shortcut(self, args: dict[str, Any]) -> ToolResult:
        """Run one of the person's Shortcuts, by name, if the name is allowlisted.

        This is the **narrowest useful grant in the module**, and the reason is worth stating because
        it is why this is not merely a third way to run AppleScript. `run_automation` lets an agent
        *write* the automation — anything the scriptable applications allow, gated by a prefix that is
        a name check rather than a sandbox. `run_shortcut` lets it *pull a lever the person installed*.
        The person built the Shortcut, chose what it does, and decided its side effects when they made
        it; the agent supplies a name and nothing else. So the reach of this tool is exactly the set of
        Shortcuts the person allowlisted, and no more — which is a scope this module can actually
        enforce, unlike the prefix check that `allow_automation` honestly admits is not a sandbox.

        Matching is exact and case-insensitive, like `open_app`, and for the same reason: a prefix rule
        would let `Backup` authorise `BackupAndDeleteEverything`. Unlike `open_app`, a name that is
        allowlisted but does not exist is *not* treated as an error here — `shortcuts run` reports it
        with a non-zero exit and the message comes back through `_failure`, because the allowlist is a
        statement about permission and the existence check belongs to the Shortcuts app.

        `input` is passed via a temporary file rather than as an argument: `shortcuts run` takes
        `-i <input-path>`, so the input has to be a file. It is written inside the workspace's state
        directory, which is gitignored and already the one place this module puts its own files.
        """
        gate = self._needs_consent("run_shortcut")
        if gate is not None:
            return gate
        name = str(args.get("name") or "").strip()
        if not name:
            return ToolResult(False, "name is required: which Shortcut should be run?")
        allowed = self.allowed_shortcuts()
        # `allow_full_access` steps the allowlist aside, exactly as it does for `open_app` and
        # `run_automation`. All three are scopes and the mode lifts all three; see FULL ACCESS.
        if not self.full_access:
            if not allowed:
                return ToolResult(False, (
                    f"run_shortcut refused: {name!r} cannot be run because system.allow_shortcuts is "
                    "empty, so no Shortcut is allowed.\n"
                    "  allowed : (none — system.allow_shortcuts is empty)\n"
                    f"  asked   : {name}\n"
                    "Do not retry this call. Ask the Owner to add the Shortcut to "
                    "system.allow_shortcuts, or complete the work without running one."))
            if name.lower() not in {s.lower() for s in allowed}:
                return ToolResult(False, (
                    f"run_shortcut refused: {name!r} is not in system.allow_shortcuts, which is the "
                    "scope of this grant.\n"
                    f"  allowed : {', '.join(allowed)}\n"
                    f"  asked   : {name}\n"
                    "Do not retry this call. Use one of the allowed Shortcuts, or ask the Owner to add "
                    "this one to system.allow_shortcuts."))
        argv = [SHORTCUTS, "run", name]
        input_text = args.get("input")
        if input_text is not None:
            payload = str(input_text)
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                input_file = self.state_dir / f"shortcut-input-{os.getpid()}.txt"
                input_file.write_text(payload, encoding="utf-8")
            except OSError as exc:
                return ToolResult(False, f"cannot write the Shortcut input file: {exc}")
            argv += ["-i", str(input_file)]
        called = self._call(argv)
        if input_text is not None:
            # Removed whether or not the run succeeded: the input may be anything the person or the
            # agent supplied, and leaving unknown content in the state directory for the next run to
            # find is not something a tool call should do on its way out.
            try:
                (self.state_dir / f"shortcut-input-{os.getpid()}.txt").unlink(missing_ok=True)
            except OSError:
                pass
        if not called.ok:
            return self._failure("run_shortcut", called)
        output = called.stdout.strip()
        detail = f"\noutput:\n{output}" if output else " (it produced no text output)"
        return ToolResult(True, f"ran Shortcut {name!r}.{detail}", truncated=called.truncated)

    # ── system:softwareupdate ───────────────────────────────────────────────

    def list_os_updates(self, args: dict[str, Any]) -> ToolResult:
        """List available macOS updates. Read-only, and the safe half of the heaviest grant.

        Listing is a scan and a lookup; it installs nothing, downloads nothing, and needs no consent.
        That is the whole point of splitting the capability's two tools: an agent that can *see* what
        is pending can report "there is an update and you should install it" without ever being able to
        install it. Most agents should hold this half and not the other.

        `softwareupdate` reports the interesting case on **stderr**: with nothing to install it prints
        `No new software available.` there and a banner on stdout, and exits 0 either way. So the
        result is assembled from both streams, and the raw text is shown rather than only a parsed
        count — "no updates" and "I could not read the answer" look the same to a parser and are
        different facts to a person.

        Defaults to `--no-scan`, because a scan is slow and touches Apple's servers; `no_scan=False`
        asks for one when a fresh answer is wanted.
        """
        argv = [SOFTWAREUPDATE, "-l"]
        if bool(args.get("no_scan", True)):
            argv.append("--no-scan")
        called = self._call(argv)
        # Not `_failure`: softwareupdate exits 0 with "No new software available." on stderr, and a
        # non-zero exit with a parseable body is still worth showing. A hard failure with nothing to
        # show is the only case that returns a refusal.
        combined = called.combined.strip()
        if not called.ok and not combined:
            return self._failure("list_os_updates", called)
        labels = parse_update_list(combined)
        if labels:
            lines = [f"{len(labels)} update{'s' if len(labels) != 1 else ''} available:"]
            lines += [f"  {label}" for label in labels]
            lines.append("Installing any of these needs install_os_updates and the Owner's approval. "
                         "Listing changed nothing.")
            return ToolResult(True, "\n".join(lines), truncated=called.truncated)
        if "no new software" in combined.lower():
            return ToolResult(True, "no updates are available. (softwareupdate said so on stderr, "
                                    "which is where it reports this case.)")
        return ToolResult(True, "no update labels were found in softwareupdate's output:\n"
                                f"{combined or '(no output)'}\n"
                                "This is reported as text rather than as '0 updates' because the two "
                                "are different facts.",
                          truncated=called.truncated)

    def install_os_updates(self, args: dict[str, Any]) -> ToolResult:
        """Install macOS updates. The heaviest action in this module, and it is stated as such.

        Three things make this the most dangerous call here, and all three belong in the docstring
        because they belong in the *decision*:

        1. **It changes the operating system.** Not a file, not a setting — the thing the machine boots
           into. An update that goes wrong is not undone by deleting something.
        2. **It can reboot the machine.** A reboot ends this run, ends whatever else the person had
           open, and cannot be resumed from here.
        3. **It takes a long time.** Half an hour is ordinary, and the wall-clock ceiling that bounds
           every other tool here cannot bound this one — which is precisely why the call does not try
           to run the install to completion inside the ceiling.

        So the tool is built to be *safe to be interrupted* rather than to finish: it passes neither
        `-R` nor `--restart` unless the caller explicitly asks, and it defaults to installing
        recommended updates only. `--agree-to-license` is deliberately **not** passed: agreeing to a
        licence on the person's behalf is a decision that is theirs, not the agent's, and if an update
        needs it the install will fail and say so rather than having the agent consent for them.

        `restart=true` is honest about what it does: it asks `softwareupdate` to reboot if required,
        which ends this run wherever it is. An unattended run should set it false and let the person
        reboot, and the refusal path for a long install says so.

        Consent is required, once per agent, like `sleep_now` — and for the same reason: both take the
        machine out of service in a way the person did not schedule.
        """
        gate = self._needs_consent("install_os_updates")
        if gate is not None:
            return gate
        labels = args.get("labels")
        if labels is not None and not isinstance(labels, list):
            return ToolResult(False, f"labels must be a list of update labels, got {labels!r}")
        argv = [SOFTWAREUPDATE, "-i"]
        if labels:
            # `softwareupdate -i` takes the labels themselves as arguments, so each one is a separate
            # argv element and none is concatenated into a string.
            argv += [str(label) for label in labels if str(label).strip()]
            if not argv[2:]:
                return ToolResult(False, "labels was an empty list; omit it to install recommended "
                                         "updates, or name at least one label from list_os_updates.")
        else:
            argv.append("-r")
        restart = args.get("restart")
        if restart is not None and not isinstance(restart, bool):
            return ToolResult(False, f"restart must be true or false, got {restart!r}")
        if restart:
            argv.append("-R")
        called = self._call(argv)
        combined = called.combined.strip() or "(no output)"
        if called.timed_out:
            # The interesting case, and it is not a failure of the tool: an install is *expected* to
            # outrun the ceiling. softwareupdate keeps running in the daemon after the command it was
            # invoked with is killed, so the honest report is that the install was started and this
            # call stopped watching — not that it failed.
            return ToolResult(True, (
                "the install was started and this call was stopped at the "
                f"{self.max_seconds}s ceiling — installs routinely run longer than any ceiling this "
                "module enforces, and `softwareupdate` hands the work to its daemon, so it may still "
                "be installing.\n"
                "  requested: " + (" ".join(argv[2:]) or "(recommended)") + "\n"
                f"  last output:\n{combined}\n"
                "Do not call this again while an install may be running. Check with "
                "list_os_updates, and tell the person the machine may restart."))
        if not called.ok:
            return self._failure("install_os_updates", called)
        tail = (" The machine may restart to finish; nothing here can prevent that."
                if restart else " Nothing was rebooted by this call.")
        return ToolResult(True, f"softwareupdate finished reporting:\n{combined}{tail}",
                          truncated=called.truncated)


def _normalise_script(text: str) -> str:
    """Collapse whitespace and strip, so a prefix match is not defeated by formatting.

    Leading indentation and the newlines inside a `tell` block are style, not meaning: a config entry
    written on one line must match a snippet the model wrote across four. Normalising both sides is
    what makes the allowlist a statement about the handler rather than about the line breaks.
    """
    return " ".join((text or "").split())


def _preview(text: str, limit: int = 160) -> str:
    """A snippet shortened for a refusal message, with a marker when it was cut."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _first_line(text: str) -> str:
    """The first non-empty line of a command's message, for putting inside a sentence."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _parse_active_interfaces(text: str) -> list[str]:
    """The interfaces `ifconfig` reports as `status: active`, in the order it printed them.

    Read from `status:` rather than from the `UP` flag, and that distinction is the whole point: an
    interface is `UP` when it is administratively enabled, which is true of Wi-Fi that is switched on
    and connected to nothing. `status: active` is the one that means the interface can carry traffic,
    so reading `UP` would report an interface as a path out when nothing is on the other end of it.
    """
    active: list[str] = []
    current = ""
    for line in (text or "").splitlines():
        if line and not line[0].isspace() and ":" in line:
            current = line.split(":", 1)[0].strip()
            continue
        if current and line.strip().lower() == "status: active":
            active.append(current)
    return active


def _parse_default_route(text: str) -> dict[str, str]:
    """Read `route -n get default` for the interface and gateway the machine would use.

    Only the two fields that answer "is there a way out, and over what" are kept. `route` prints
    `interface: en0` and `gateway: 10.0.0.1` among a dozen others (flags, mtu, expire), and those two
    are the ones a model acts on — the rest describe a route whose existence is already established by
    these being present.
    """
    found: dict[str, str] = {}
    for line in (text or "").splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key in ("interface", "gateway") and value:
            found[key] = value
    return found


# ── the catalogue is the single source of grants ─────────────────────────────


def publish_capabilities() -> int:
    """Publish every catalogue entry's capability into the registry's own gate table.

    **Why this exists, stated plainly.** The registry enforces a machine tool's grant by looking its
    name up in `tools.ToolRegistry.SYSTEM_TOOL_CAPABILITY`, and `_register_system_tools` *skips* any
    catalogue entry that is not in that table — deliberately, so a tool can never be advertised
    without a grant. But that table is written out by hand in `tools.py`, while the catalogue is
    written out in this file, so the two are two copies of one fact. A new tool added here and not
    there is not refused: it is **silently absent**, which is the worse failure, because a tool that
    was never offered looks like a tool that does not exist rather than like a permission problem.

    So this module populates the table from its own catalogue, and the catalogue becomes the single
    source it is documented to be. `setdefault` rather than assignment, so a name `tools.py` already
    declares keeps its declaration and the two are *checked* against each other by the test suite
    rather than one silently overwriting the other.

    Called at import, because the import is what the registry triggers (`_register_system_tools`
    imports `CATALOGUE` from here) — so by the time any registration can happen, the table is current.
    Returns the number of entries published, which the tests assert is all of them.
    """
    from .tools import ToolRegistry

    table = ToolRegistry.SYSTEM_TOOL_CAPABILITY
    for entry in CATALOGUE:
        table.setdefault(entry.name, entry.capability)
    return len(CATALOGUE)


#: Applied at import so the registry's gate table cannot be one tool behind this catalogue. See
#: `publish_capabilities` for why the direction of this dependency is the way it is.
PUBLISHED_TOOL_COUNT = publish_capabilities()
