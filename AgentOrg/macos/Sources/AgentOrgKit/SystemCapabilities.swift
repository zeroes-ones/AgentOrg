//
//  SystemCapabilities.swift
//  AgentOrgKit
//
//  What the agents may do on this machine, in the engine's own words.
//
//  WHY NOTHING HERE IS A LIST OF CAPABILITIES
//  ------------------------------------------
//  The engine already answers this question twice over: `engine.config.SystemConfig.CAPABILITIES`
//  declares which grants exist, `engine.sysctl_tools.CATALOGUE` declares which of them have a tool
//  behind them, and `engine.syscap.describe()` writes the prose a person reads. `serve._cmd_system`
//  returns all three, and its own docstring says why: "the prose living in Swift as well — drifts the
//  first time a capability changes, and the failure mode is a console confidently describing a grant
//  it does not enforce".
//
//  So this file contains **no grant names, no descriptions and no ordering of its own**. It decodes
//  what the engine sent. The one thing it does add is the question the engine cannot answer for a
//  single panel — *which agent holds this* — and it derives that from the roster with the same rule
//  the tool registry enforces (`system:*` or an exact match), so the panel cannot show a grant as
//  held while `ToolRegistry.call` would refuse it.
//
//  **One exception, at the bottom of the file, and it is an exception because the reply is
//  incomplete.** `SystemTools` mirrors `sysctl_tools.CATALOGUE` and `CONSENT_REQUIRED`, because
//  `syscap.console_payload` sends no tool names at all — so "run this capability once" and "approve
//  this tool once" have nowhere to read one from. It is documented there as a stopgap with the engine
//  change that deletes it, rather than being quietly presented as decoded data.
//
//  `available == false` is carried, not filtered. **All twelve declared capabilities have a tool
//  behind them today**, so nothing is in that state right now — the flag stays because the config and
//  the catalogue are two files that drift while work is in progress, and a panel that dropped an
//  unbuilt grant would collapse "not built yet" into "not granted".
//
//  A SECOND GAP, AND IT IS THE ONE THE PANEL'S FIRST FIGURE RESTS ON. `serve._cmd_system` answers with
//  `syscap.console_payload`, which describes the capability *set*: it names no holder, so its own
//  `summary` reads "0 of 12 system capabilities granted" on every machine (measured, `syscap.py:281`
//  passes `[]`). The CLI's `system list` can say "12 of 12 granted" for the same machine because it
//  adds a holder itself (`systemcli.py:628`, the Owner, holding `system:*`), and the served roster can
//  say six, because six are on a named agent's entry. Three numbers, one machine, one phrase, no
//  subject. `SystemHolder` and `SystemReach` below print each figure with its subject; the field that
//  would let the panel state the person's own reach is `holder` on the `system` reply, added at
//  `engine/serve.py:1919` — until then the panel says the fact is missing instead of inventing it.

import Foundation

/// One machine capability, as the engine describes it.
///
/// Total by construction: a payload missing a field renders as an empty string rather than failing to
/// decode, because a panel that disappeared when the engine added a field would be worse than one
/// showing a short row. Only `grant` is required — without it there is nothing to key a row on.
public struct SystemCapability: Sendable, Equatable, Identifiable {

    /// The grant an agent holds: `system:state`, `system:clipboard`, …
    public let grant: String
    /// A person-facing name — "Control volume and media", not `system:media`.
    public let title: String
    /// What it reaches, in one sentence, written for someone who does not know what AppleScript is.
    public let reaches: String
    /// What the person would notice changing. Empty means nothing does — a read is not a decision.
    public let changes: String
    /// Present only where there is something real to be cautious about. Empty means there is not.
    public let caution: String
    /// Whether at least one tool exists for this grant today.
    public let available: Bool

    public var id: String { grant }

    public init?(payload: [String: JSONValue]) {
        guard let grant = payload["grant"]?.stringValue, !grant.isEmpty else { return nil }
        self.grant = grant
        self.title = payload["title"]?.stringValue ?? grant
        self.reaches = payload["reaches"]?.stringValue ?? ""
        self.changes = payload["changes"]?.stringValue ?? ""
        self.caution = payload["caution"]?.stringValue ?? ""
        // Read from the engine rather than inferred from `changes.isEmpty` here: the engine sets both
        // in `Capability.as_dict`, and re-deriving one from the other would be a second rule that can
        // disagree with the first. A payload that omits it falls back to the same reading.
        self.available = payload["available"]?.boolValue ?? true
    }

    /// Whether this grant changes something the person already had.
    public var changesState: Bool { !changes.isEmpty }

    /// The consequence, in one phrase, for a row: the engine's sentence, or an explicit "nothing".
    ///
    /// The negative case is stated rather than left blank because a blank line in a column headed
    /// "changes" reads as "unknown", and "unknown" is how someone ends up refusing a harmless read.
    public var effect: String {
        changesState ? changes : "nothing — this only reads"
    }

    /// How the row should be drawn. Colour never carries this alone; the word and glyph are beside it.
    ///
    /// An unavailable grant is `neutral` rather than a warning: nothing is wrong, it is simply not
    /// built yet, and colouring it like a problem would send a person looking for a cause.
    public var tone: StatusTone {
        if !available { return .neutral }
        return changesState ? .attention : .ok
    }

    /// The word the row shows beside the tone, so the state survives without colour.
    public var stateWord: String {
        if !available { return "no tool yet" }
        return changesState ? "changes state" : "reads only"
    }

    /// The agents in this roster that hold the grant.
    ///
    /// The same rule `tools.ToolRegistry._granted_scoped` enforces — an exact `system:<scope>` match
    /// or the `system:*` wildcard, and deliberately **no prefix matching**, because a prefix rule
    /// would let `system:state` reach a hypothetical `system:stateful`. `syscap.granted_in` mirrors it
    /// too, which is why the panel and the model-facing refusal agree.
    ///
    /// `name` is used rather than `id`: a person reading "who holds this" wants the colleague's name.
    public static func holders(of grant: String, in roster: [[String: JSONValue]]) -> [String] {
        roster.filter { holds(grant, agent: $0) }
            .compactMap { $0["name"]?.stringValue }
            .sorted()
    }

    /// Whether one roster entry holds the grant, by the same exact-or-wildcard rule.
    ///
    /// Split out because the panel now asks this per *agent* as well: an approval is per holder
    /// (`sysctl_tools.grant_consent`), so the consent picker has to know which agents a grant reaches,
    /// and re-deriving the rule there would be a second copy of it — the one thing that could make the
    /// picker and the row above it disagree about who holds what.
    public static func holds(_ grant: String, agent: [String: JSONValue]) -> Bool {
        reached(grant, by: (agent["capabilities"]?.arrayValue ?? []).compactMap { $0.stringValue })
    }

    /// Whether a *list* of grants reaches one grant — the same rule, asked of the other shape.
    ///
    /// `holds(_:agent:)` above reads the rule out of a roster entry; this reads it out of a plain grant
    /// list, which is the shape the engine reports the **acting holder** in (`systemcli.Holder.grants`,
    /// `engine/systemcli.py:115` — `["system:*"]` for the Owner). One home for the rule, so the figure
    /// for an agent and the figure for the person cannot drift apart: a prefix rule here would let
    /// `system:state` be read as `system:stateful` in one place and not the other.
    public static func reached(_ grant: String, by grants: [String]) -> Bool {
        grants.contains(grant) || grants.contains("system:*")
    }

    /// The capabilities the engine reported, in the order it sent them.
    ///
    /// Order is the engine's and is not re-sorted: `syscap.CAPABILITIES` deliberately runs from the
    /// harmless reads to the powerful grants, and a panel that alphabetised them would put
    /// `system:automation` third from the top of a list a person is skim-reading for what is risky.
    public static func list(from payload: [String: JSONValue]) -> [SystemCapability] {
        (payload["capabilities"]?.arrayValue ?? [])
            .compactMap { $0.objectValue }
            .compactMap(SystemCapability.init(payload:))
    }
}

// MARK: - Who can use what

/// The acting holder, in the engine's own account of itself.
///
/// Mirrors `systemcli.Holder.as_dict` (`engine/systemcli.py:118-120`) — the shape the CLI's
/// `system list` puts under `holder` (`engine/systemcli.py:628`). It carries the name, the grants in
/// force and the engine's sentence for where they came from, which is what lets the panel say what the
/// person may do *in the engine's words* instead of restating a rule of its own.
///
/// **`serve._cmd_system` does not send it today** (see the gap note at the top of the file), so this
/// decodes to nil and the panel reports the missing fact rather than asserting one. "The Owner holds
/// `system:*`" is a fact about the engine's console holder (`engine/systemcli.py:501-504`, mirrored at
/// `engine/serve.py:2033`); writing it here would be a console describing a rule it does not enforce,
/// which is the failure this whole file is arranged against. Kept — and unit-tested against a synthetic
/// payload — for the same reason `SystemCapability.available == false` is kept: the branch exists for a
/// state the engine can be in, not for a state it happens to be in today.
public struct SystemHolder: Sendable, Equatable {

    /// The holder's id — `ag_owner` for the person.
    public let id: String
    /// The holder's name, as the roster spells it.
    public let name: String
    /// The grants in force, exactly as the engine listed them — `["system:*"]` for the Owner.
    public let grants: [String]
    /// The engine's own account of where those grants came from.
    public let why: String

    public init?(payload: [String: JSONValue]) {
        guard let id = payload["id"]?.stringValue, !id.isEmpty else { return nil }
        self.id = id
        self.name = payload["name"]?.stringValue ?? id
        self.grants = (payload["grants"]?.arrayValue ?? []).compactMap { $0.stringValue }
        self.why = payload["why"]?.stringValue ?? ""
    }

    /// The holder a `system` reply names, or nil when it names none.
    public static func from(_ system: [String: JSONValue]) -> SystemHolder? {
        system["holder"]?.objectValue.flatMap(SystemHolder.init(payload:))
    }
}

/// One set of declared capabilities, read **two ways**: what an agent holds, and what the person can use.
///
/// **Why both readings are here rather than one number.** The panel's headline figure was one number
/// under one phrase — "granted" — and it meant three things on one machine:
///
/// - the engine's own `summary`, computed for no holder at all, so it reads "0 of 12 … granted"
///   (`engine/syscap.py:281` calls `summary([])`);
/// - the roster's answer, which is **six** — six of the twelve are on a named agent's entry;
/// - the console holder's answer, which is **twelve** — the owner of the machine acts with `system:*`,
///   which is why `engine.cli system list` says "12 of 12 granted".
///
/// All three are true and none of them said whose. This type computes the two the panel can answer
/// from data it holds and keeps the subject attached to each: `heldByAnAgent` is the roster's reading,
/// `ownerReaches` is the acting holder's, and `owner == nil` means the engine did not report one — which
/// is not the same as zero, and is why the panel says so in words rather than printing "0".
public struct SystemReach: Sendable, Equatable {

    /// Every declared capability, in the engine's order.
    public let capabilities: [SystemCapability]
    /// Grant → the roster's agents that hold it, sorted. An empty list means nobody does — the fact
    /// that decides whether a run can reach the capability at all.
    public let holders: [String: [String]]
    /// The acting holder the engine named, when it named one.
    public let owner: SystemHolder?

    public init(capabilities: [SystemCapability], roster: [[String: JSONValue]],
                system: [String: JSONValue]) {
        self.capabilities = capabilities
        var byGrant: [String: [String]] = [:]
        for capability in capabilities {
            byGrant[capability.grant] = SystemCapability.holders(of: capability.grant, in: roster)
        }
        self.holders = byGrant
        self.owner = SystemHolder.from(system)
    }

    /// The agents that hold one grant. Empty is an answer, not an error.
    public func holders(of grant: String) -> [String] { holders[grant] ?? [] }

    /// The capabilities at least one agent holds — what the panel's first figure counts, and the only
    /// ones a *run* can reach.
    public var heldByAnAgent: [SystemCapability] {
        capabilities.filter { !holders(of: $0.grant).isEmpty }
    }

    /// The complement, in the engine's order: nothing an agent can use until a roster edit grants one.
    public var heldByNobody: [SystemCapability] {
        capabilities.filter { holders(of: $0.grant).isEmpty }
    }

    /// The capabilities the acting holder's own grants reach. Empty when no holder was reported — read
    /// `owner` to tell that state from "reported, and it reaches none".
    public var ownerReaches: [SystemCapability] {
        guard let owner else { return [] }
        return capabilities.filter { SystemCapability.reached($0.grant, by: owner.grants) }
    }

    /// Whether the acting holder's reach is the whole set — the honest form of "you can use all twelve".
    ///
    /// Optional on purpose. Nil is "the engine did not say", and collapsing that into `false` would turn
    /// a missing field into a claim that the person holds nothing.
    public var ownerHasEverything: Bool? {
        guard owner != nil else { return nil }
        return !capabilities.isEmpty && ownerReaches.count == capabilities.count
    }
}

// MARK: - The tools behind the capabilities

/// One entry of `sysctl_tools.CATALOGUE`, reduced to the facts a panel acts on.
///
/// The capability list answers "what may an agent reach"; this answers "which tool would do it", which
/// is the question a console needs once it can *act* rather than only describe. Four facts, each read
/// from the catalogue entry rather than inferred: the tool name (what `system_invoke` takes), the grant
/// it needs, whether it mutates, and whether the engine refuses it until the Owner approves once per
/// agent (`CONSENT_REQUIRED`).
public struct SystemTool: Sendable, Equatable, Identifiable {

    /// The tool name the engine answers to — `open_app`, `read_clipboard`, …
    public let name: String
    /// The grant that decides whether the tool is offered at all.
    public let grant: String
    /// `CatalogEntry.mutates`: whether the call changes something the person already had.
    public let mutates: Bool
    /// Whether the catalogue's schema declares a required argument, so a bare name is a complete call.
    public let runsWithoutArguments: Bool
    /// Whether `sysctl_tools.CONSENT_REQUIRED` names it — the ask-once gate, per agent, per tool.
    public let consentRequired: Bool

    public var id: String { name }

    /// Whether a person can learn what this grant does by running it once from the console.
    ///
    /// **Read-only *and* argument-free, and the conjunction is deliberate.** A state change is a
    /// decision rather than a demonstration: `sleep_now` and `install_os_updates` take no arguments
    /// either, and under `allow_full_access` the engine's consent gate steps aside — so a one-click
    /// "Try it" on those would put the machine to sleep or rewrite the operating system from a button
    /// a person pressed to find out what a row means. `sysctl_tools` already draws this line itself
    /// ("a read is not a decision", `CONSENT_REQUIRED`'s docstring), and the panel follows it rather
    /// than inventing a second one.
    public var isSafeToTry: Bool { runsWithoutArguments && !mutates }

    public init(name: String, grant: String, mutates: Bool,
                runsWithoutArguments: Bool, consentRequired: Bool = false) {
        self.name = name
        self.grant = grant
        self.mutates = mutates
        self.runsWithoutArguments = runsWithoutArguments
        self.consentRequired = consentRequired
    }
}

/// The catalogue and the consent set, mirrored — **and this mirror is a known defect, not a design.**
///
/// Everything else in this file is decoded from the engine's reply. This is not, and it cannot be:
/// `serve._cmd_system` answers with `syscap.console_payload`, whose shape is grant-level prose only.
/// It carries no tool name, no `mutates`, no consent flag. But the two things an *actionable* panel
/// needs are exactly those: `system_invoke` takes a **tool name** (so "try this capability" has
/// nowhere to read one from), and the ask-once gate applies to **tools** (`CONSENT_REQUIRED`), not to
/// grants, so the approvals section has no list to render either.
///
/// The fix belongs in the engine, and it is small: `engine/syscap.py` `console_payload` (the function
/// whose docstring already freezes the key set for this decoder) should carry the catalogue — `name`,
/// `capability`, `mutates`, whether the schema requires an argument, and whether the tool is in
/// `CONSENT_REQUIRED`. Then this type is deleted and the entries are decoded like `SystemCapability`
/// is, which is the only version of this file that cannot go stale.
///
/// Until then the copy is kept honest by naming its source line in every entry, and by a test that
/// should exist and does not: `SystemPanelTests` already parses `engine/sysctl_tools.py` with the
/// small-regex-over-a-stable-block technique (`grantsWithTools`, its lines 88-97), so a drift guard
/// for `all` below is a dozen lines in a file this change does not own. Reported as a gap rather than
/// quietly adding a second hand-maintained list.
public enum SystemTools {

    /// Every catalogue entry, in the catalogue's own order — which is also the capability order, so a
    /// panel that groups by grant reads in the order `syscap` deliberately chose (reads, then state
    /// changes, then the powerful ones).
    public static let all: [SystemTool] = [
        // ── system:state ── sysctl_tools.py:362
        SystemTool(name: "system_state", grant: "system:state",
                   mutates: false, runsWithoutArguments: true),
        // ── system:clipboard ── sysctl_tools.py:372, :382
        SystemTool(name: "read_clipboard", grant: "system:clipboard",
                   mutates: false, runsWithoutArguments: true),
        SystemTool(name: "write_clipboard", grant: "system:clipboard",
                   mutates: true, runsWithoutArguments: false, consentRequired: true),
        // ── system:screenshot ── sysctl_tools.py:398
        SystemTool(name: "take_screenshot", grant: "system:screenshot",
                   mutates: true, runsWithoutArguments: true),
        // ── system:media ── sysctl_tools.py:408, :415, :431
        SystemTool(name: "get_volume", grant: "system:media",
                   mutates: false, runsWithoutArguments: true),
        SystemTool(name: "set_volume", grant: "system:media",
                   mutates: true, runsWithoutArguments: false, consentRequired: true),
        SystemTool(name: "set_mute", grant: "system:media",
                   mutates: true, runsWithoutArguments: false, consentRequired: true),
        // ── system:open ── sysctl_tools.py:446
        SystemTool(name: "open_app", grant: "system:open",
                   mutates: true, runsWithoutArguments: false, consentRequired: true),
        // ── system:automation ── sysctl_tools.py:463
        SystemTool(name: "run_automation", grant: "system:automation",
                   mutates: true, runsWithoutArguments: false, consentRequired: true),
        // ── system:notify ── sysctl_tools.py:484, :504
        SystemTool(name: "say_message", grant: "system:notify",
                   mutates: true, runsWithoutArguments: false, consentRequired: true),
        SystemTool(name: "post_notification", grant: "system:notify",
                   mutates: true, runsWithoutArguments: false),
        // ── system:search ── sysctl_tools.py:527
        SystemTool(name: "spotlight_search", grant: "system:search",
                   mutates: false, runsWithoutArguments: false),
        // ── system:power ── sysctl_tools.py:553, :573
        SystemTool(name: "keep_awake", grant: "system:power",
                   mutates: true, runsWithoutArguments: true, consentRequired: true),
        SystemTool(name: "sleep_now", grant: "system:power",
                   mutates: true, runsWithoutArguments: true, consentRequired: true),
        // ── system:network ── sysctl_tools.py:583
        SystemTool(name: "network_status", grant: "system:network",
                   mutates: false, runsWithoutArguments: true),
        // ── system:shortcuts ── sysctl_tools.py:600
        SystemTool(name: "run_shortcut", grant: "system:shortcuts",
                   mutates: true, runsWithoutArguments: false, consentRequired: true),
        // ── system:softwareupdate ── sysctl_tools.py:619, :635
        SystemTool(name: "list_os_updates", grant: "system:softwareupdate",
                   mutates: false, runsWithoutArguments: true),
        SystemTool(name: "install_os_updates", grant: "system:softwareupdate",
                   mutates: true, runsWithoutArguments: true, consentRequired: true),
    ]

    /// The tools that act under one grant, in catalogue order.
    public static func forGrant(_ grant: String) -> [SystemTool] {
        all.filter { $0.grant == grant }
    }

    /// The tools the engine refuses until the Owner approves them once, for one agent.
    public static var consentRequired: [SystemTool] {
        all.filter(\.consentRequired)
    }

    /// The one tool worth offering a "try it" for, or nil where every tool needs an argument or
    /// changes something. Nil is the common case and is not an error: most of these grants are
    /// decisions, and a decision has no safe demonstration.
    public static func tryable(_ grant: String) -> SystemTool? {
        all.first { $0.grant == grant && $0.isSafeToTry }
    }

    /// The grants the catalogue backs, in catalogue order, each appearing once.
    ///
    /// This is what lets the hire form *derive* its list of machine grants instead of restating it:
    /// the twelve grants the engine declares are exactly the twelve its tools name, so a grant added
    /// to the config with no tool yet still appears (with a placeholder), and the six that were
    /// missing from the form before this were the six whose absence nothing caught.
    public static var grants: [String] {
        var seen = Set<String>()
        return all.compactMap { seen.insert($0.grant).inserted ? $0.grant : nil }
    }
}
