//
//  SystemCapabilities.swift
//  AgentOrgKit
//
//  What the agents may do on this machine, in the engine's own words.
//
//  WHY NOTHING HERE IS A LIST OF CAPABILITIES, TOOLS OR SENTENCES
//  --------------------------------------------------------------
//  The engine answers this question three times over: `engine.config.SystemConfig.CAPABILITIES`
//  declares which grants exist, `engine.sysctl_tools.CATALOGUE` and `CONSENT_REQUIRED` say which tools
//  sit behind them and which ask the Owner first, and `engine.syscap.describe()` writes the prose a
//  person reads. `syscap.console_payload` — which `serve._cmd_system` returns — sends all of it,
//  including the tool table; a payload that did not carry one is why a hand-written mirror of the
//  catalogue lived at the bottom of this file, and it is gone now that it does.
//
//  So this file contains **no grant names, no tool names, no descriptions and no ordering of its own**.
//  It decodes what the engine sent, and its own docstring says why: "the prose living in Swift as well —
//  drifts the first time a capability changes, and the failure mode is a console confidently describing
//  a grant it does not enforce".
//
//  The one thing it does add is the question the engine cannot answer for a single panel — *which agent
//  holds this* — and it derives that from the roster with the same rule the tool registry enforces
//  (`system:*` or an exact match), so the panel cannot show a grant as held while `ToolRegistry.call`
//  would refuse it.
//
//  `available == false` is carried, not filtered. **All twelve declared capabilities have a tool
//  behind them today**, so nothing is in that state right now — the flag stays because the config and
//  the catalogue are two files that drift while work is in progress, and a panel that dropped an
//  unbuilt grant would collapse "not built yet" into "not granted".
//
//  A SECOND GAP, AND IT IS THE ONE THE PANEL'S FIRST FIGURE RESTS ON. `serve._cmd_system` answers with
//  `syscap.console_payload`, which describes the capability *set*: it names no holder, so its own
//  `summary` reads "0 of 12 system capabilities granted" on every machine (measured, `syscap.py:324`
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
///   (`engine/syscap.py:324` calls `summary([])`);
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

    /// Decoded from one entry of the payload's `tools`.
    ///
    /// Total by construction, like `SystemCapability`: only `name` is required, because a tool row with
    /// no name is not a row — `system_invoke` takes the name and nothing else identifies one. A field
    /// the payload omits reads as the conservative value rather than failing to decode, so an older
    /// engine's reply draws a shorter row instead of an empty panel.
    public init?(payload: [String: JSONValue]) {
        guard let name = payload["name"]?.stringValue, !name.isEmpty else { return nil }
        self.name = name
        self.grant = payload["grant"]?.stringValue ?? ""
        self.mutates = payload["mutates"]?.boolValue ?? false
        // The engine derives this from the entry's own JSON schema, which is what the tool layer
        // enforces; re-deriving it here would be a second rule to keep true.
        self.runsWithoutArguments = payload["runs_without_arguments"]?.boolValue ?? false
        self.consentRequired = payload["consent_required"]?.boolValue ?? false
    }

    /// Every tool the engine's reply carries, in the catalogue's order.
    ///
    /// Order is the engine's and is not re-sorted: `sysctl_tools.CATALOGUE`'s order is the grant order,
    /// so a panel that groups by grant reads in the order `syscap` deliberately chose (reads, then
    /// state changes, then the powerful ones).
    public static func list(from payload: [String: JSONValue]) -> [SystemTool] {
        (payload["tools"]?.arrayValue ?? [])
            .compactMap { $0.objectValue }
            .compactMap(SystemTool.init(payload:))
    }
}

/// The tool catalogue and the ask-once set, **decoded** from the engine's reply.
///
/// `syscap.console_payload` carries `tools` for exactly this: every `CatalogEntry` reduced to the four
/// facts a control surface acts on. The two questions an *actionable* panel asks are answered from it —
/// `system_invoke` takes a tool name, and the ask-once gate applies to tools (`CONSENT_REQUIRED`)
/// rather than to grants.
///
/// This type used to be an 18-entry hand-written list, with a docstring calling itself "a known defect,
/// not a design" and reporting that the fix belonged in the engine. It did, and it is there now; the
/// list is deleted rather than kept in step, which is the only version of it that cannot go stale.
///
/// An empty catalog is a real state, not a failure: before the first reply, and on a machine whose
/// `system` read failed, there is nothing to offer rather than a guessed list.
public struct SystemToolCatalog: Sendable, Equatable {

    /// Every catalogue entry, in the engine's own order.
    public let all: [SystemTool]

    public init(_ payload: [String: JSONValue]) {
        self.all = SystemTool.list(from: payload)
    }

    /// Whether the engine has told this console about any tools yet.
    public var isEmpty: Bool { all.isEmpty }

    /// The tools that act under one grant, in catalogue order.
    public func forGrant(_ grant: String) -> [SystemTool] {
        all.filter { $0.grant == grant }
    }

    /// The tools the engine refuses until the Owner approves them once, for one agent.
    public var consentRequired: [SystemTool] {
        all.filter(\.consentRequired)
    }

    /// The one tool worth offering a "try it" for, or nil where every tool needs an argument or
    /// changes something. Nil is the common case and is not an error: most of these grants are
    /// decisions, and a decision has no safe demonstration.
    public func tryable(_ grant: String) -> SystemTool? {
        all.first { $0.grant == grant && $0.isSafeToTry }
    }

    /// The grants the catalogue backs, in catalogue order, each appearing once.
    ///
    /// Note that this is *not* what the hire form offers any more. That form is built from the
    /// capabilities the engine **declares** (`SystemCapability.list`), so a grant added to the config
    /// with no tool yet still appears there; this answers the narrower question "which grants have
    /// something behind them", which is what a panel offering to run one needs.
    public var grants: [String] {
        var seen = Set<String>()
        return all.compactMap { seen.insert($0.grant).inserted ? $0.grant : nil }
    }
}

