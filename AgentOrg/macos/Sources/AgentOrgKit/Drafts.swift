//
//  Drafts.swift
//  AgentOrgKit
//
//  What the user is *composing* in a form, before the engine has accepted it.
//
//  WHY THESE ARE SEPARATE TYPES
//  ----------------------------
//  A form's state and a command's payload are different things, and conflating them is how a UI ends
//  up sending fields the user never filled in. A `ProviderDraft` holds exactly what the editor shows;
//  `payload()` decides what crosses the wire, and drops anything empty so "leave this alone" is
//  expressible rather than being sent as an empty string that overwrites a value.
//
//  These are plain value types with no engine knowledge, so they are unit-testable without a running
//  process — which matters because payload construction is exactly the part that fails silently.

import Foundation

/// A provider the user is adding or editing.
public struct ProviderDraft: Sendable, Equatable {
    public var id: String
    public var kind: String
    public var baseURL: String
    /// The key itself. Held in memory only long enough to send it; never read back from the engine.
    public var apiKey: String
    /// A variable *name* to read the key from instead. Preferred over a literal, and what the UI nudges
    /// toward, because the environment is not read into logs or traces.
    public var apiKeyEnv: String
    /// Extra request headers, one `Name: value` per entry.
    public var headers: [String: String]

    public init(id: String = "", kind: String = "openai", baseURL: String = "",
                apiKey: String = "", apiKeyEnv: String = "", headers: [String: String] = [:]) {
        self.id = id
        self.kind = kind
        self.baseURL = baseURL
        self.apiKey = apiKey
        self.apiKeyEnv = apiKeyEnv
        self.headers = headers
    }

    /// Build a draft from an entry the engine already has, for editing.
    ///
    /// The key is deliberately **not** populated: the engine never returns it, so the field starts
    /// empty and an untouched save leaves the stored key alone rather than blanking it.
    public init(existing: [String: JSONValue]) {
        self.init(
            id: existing["id"]?.stringValue ?? "",
            kind: existing["kind"]?.stringValue ?? "openai",
            baseURL: existing["base_url"]?.stringValue ?? "",
            apiKey: "",
            apiKeyEnv: existing["api_key_env"]?.stringValue ?? "",
            headers: [:])
    }

    /// Whether there is enough here to attempt a test or a save.
    public var isComplete: Bool {
        !id.trimmingCharacters(in: .whitespaces).isEmpty
            && !baseURL.trimmingCharacters(in: .whitespaces).isEmpty
    }

    /// The command payload. Empty fields are omitted, so a save cannot blank a value it did not set.
    public func payload() -> [String: JSONValue] {
        var out: [String: JSONValue] = [
            "provider_id": .string(id.trimmingCharacters(in: .whitespaces)),
            "kind": .string(kind),
            "base_url": .string(baseURL.trimmingCharacters(in: .whitespaces)),
        ]
        let key = apiKey.trimmingCharacters(in: .whitespaces)
        let env = apiKeyEnv.trimmingCharacters(in: .whitespaces)
        if !env.isEmpty { out["api_key_env"] = .string(env) }
        // Both are sent when both were filled, and the *engine* decides which one wins — it writes
        // both and `resolve_key` reads the variable first, so the safer source still takes precedence.
        //
        // Sending the literal only when the variable field was empty is what made "paste a key and
        // also name the variable you intend to use" a save with no usable key at all: the person's
        // input was discarded before it left the machine, and the engine then reported "has no API
        // key" to someone who had just supplied one. The engine-side writer was fixed to keep both
        // (`serve._cmd_provider_add`), but this builder dropped the key first, so that fix could never
        // take effect from the app. An empty field still means "unchanged" — only presence is sent.
        if !key.isEmpty { out["api_key"] = .string(key) }
        if !headers.isEmpty {
            out["extra_headers"] = .object(headers.mapValues { JSONValue.string($0) })
        }
        return out
    }
}

/// An agent the user is hiring or editing.
public struct AgentDraft: Sendable, Equatable {
    public var name: String
    public var skill: String
    public var provider: String
    public var model: String
    public var level: String
    public var team: String
    public var title: String
    /// What this agent may reach, as `<kind>:<scope>` grants.
    ///
    /// Empty means "use the skill's default" — the engine's own least-privilege rule — so a hire that
    /// says nothing is never silently widened. This is the field that lets a person actually grant
    /// something from the app: capabilities existed only in the roster file before it, so there was no
    /// way to give an agent `exec:` or a `system:` grant without hand-editing JSON.
    public var capabilities: [String]

    public init(name: String = "", skill: String = "", provider: String = "", model: String = "",
                level: String = "senior", team: String = "", title: String = "",
                capabilities: [String] = []) {
        self.name = name
        self.skill = skill
        self.provider = provider
        self.model = model
        self.level = level
        self.team = team
        self.title = title
        self.capabilities = capabilities
    }

    /// Build a draft from an agent the engine already has, for editing.
    public init(existing: [String: JSONValue]) {
        self.init(
            name: existing["name"]?.stringValue ?? "",
            skill: existing["skills"]?.arrayValue?.first?.stringValue ?? "",
            provider: existing["provider"]?.stringValue ?? "",
            model: existing["model"]?.stringValue ?? "",
            level: existing["level"]?.stringValue ?? "senior",
            team: existing["team"]?.stringValue ?? "",
            title: existing["title"]?.stringValue ?? "",
            capabilities: (existing["capabilities"]?.arrayValue ?? []).compactMap { $0.stringValue })
    }

    /// Whether there is enough here to attempt a hire.
    public var isComplete: Bool {
        !name.trimmingCharacters(in: .whitespaces).isEmpty
            && !skill.trimmingCharacters(in: .whitespaces).isEmpty
    }

    public func hirePayload() -> [String: JSONValue] {
        var out: [String: JSONValue] = [
            "name": .string(name.trimmingCharacters(in: .whitespaces)),
            "skill": .string(skill),
            "level": .string(level),
        ]
        if !provider.isEmpty { out["provider"] = .string(provider) }
        if !model.isEmpty { out["model"] = .string(model) }
        if !team.trimmingCharacters(in: .whitespaces).isEmpty { out["team"] = .string(team) }
        if !title.trimmingCharacters(in: .whitespaces).isEmpty { out["title"] = .string(title) }
        // Sent only when non-empty, so an untouched hire keeps the engine's least-privilege default
        // rather than being sent an empty list that would mean "grant nothing".
        if !capabilities.isEmpty {
            out["capabilities"] = .array(capabilities.map { JSONValue.string($0) })
        }
        return out
    }

    /// The edit payload. The *name* is included only when it changed, because the engine refuses a
    /// rename onto a name another agent already holds — and re-sending an unchanged name would make a
    /// model-only edit fail for a reason that has nothing to do with the edit.
    public func updatePayload(original: AgentDraft? = nil) -> [String: JSONValue] {
        var out: [String: JSONValue] = [:]
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        if !trimmed.isEmpty, trimmed != original?.name { out["name"] = .string(trimmed) }
        if !provider.isEmpty { out["provider"] = .string(provider) }
        if !model.isEmpty { out["model"] = .string(model) }
        if !level.isEmpty { out["level"] = .string(level) }
        if !team.trimmingCharacters(in: .whitespaces).isEmpty { out["team"] = .string(team) }
        if !title.trimmingCharacters(in: .whitespaces).isEmpty { out["title"] = .string(title) }
        return out
    }
}

/// The capabilities an agent can be granted, as a list the UI renders and tests can assert on.
///
/// Kept in the Kit rather than in a view so the vocabulary has one definition: the toggle, the help
/// text, and any test all read from here.
///
/// **The machine half is derived; the project half is written out, and the difference is deliberate.**
/// The six `system:` grants this used to list by hand were half the twelve the engine declares —
/// `system:notify`, `:search`, `:power`, `:network`, `:shortcuts` and `:softwareupdate` could not be
/// granted from the app at all, and nothing caught it, because a hand-written list is the only thing
/// that can decide which grants it omits. So the system group now comes from `SystemTools.grants` —
/// the grants the tool catalogue backs, which is exactly the twelve `SystemConfig.CAPABILITIES`
/// declares — and the wording below is a lookup *keyed by* those grants rather than the source of
/// them. A grant with no wording still appears, with a placeholder, which is louder than the silence
/// a missing list entry produced. The project grants stay written out: `read:`/`write:`/`exec:` are
/// the sandbox's vocabulary, there is no catalogue to derive them from, and there are four of them.
///
/// The residual gap, stated rather than left to be discovered: the derivation source is the *tool*
/// catalogue, so a grant added to `SystemConfig.CAPABILITIES` with **no tool yet** would still be
/// absent from this form. Closing it needs the form to take the engine's live list —
/// `CapabilityChoice.groups(syscap:)` plus a one-line change where the hire form renders it — which is
/// a change in a file this one does not own. Today all twelve have tools, so the two lists are equal.
///
/// Grouped by *what it reaches*, because that is what a person is deciding. File grants stay inside the
/// project; system grants act on the machine, and the ones that change state (`open`, `automation`,
/// `media`, `power`, `shortcuts` and the installing half of `softwareupdate`) are described as such
/// rather than presented as equally safe as reading the battery.
public enum CapabilityChoice {
    public struct Choice: Sendable, Identifiable, Hashable {
        public let grant: String
        public let label: String
        public let detail: String
        public var id: String { grant }
    }

    public struct Group: Sendable, Identifiable, Hashable {
        public let title: String
        public let isSystem: Bool
        public let choices: [Choice]
        public var id: String { title }
    }

    /// The grants inside the project. Written out because nothing in the engine declares them:
    /// `read:`/`write:`/`exec:` are the sandbox profile's vocabulary, not a catalogue's.
    private static let projectChoices: [Choice] = [
        Choice(grant: "read:*", label: "Read the project",
               detail: "every file under the workspace"),
        Choice(grant: "write:src/**", label: "Write source",
               detail: "changes under src/ only"),
        Choice(grant: "write:*", label: "Write anywhere in the project",
               detail: "broader than src/ — grant deliberately"),
        Choice(grant: "exec:*", label: "Run commands",
               detail: "confined to the project by the sandbox"),
    ]

    /// The short wording the *form* shows for each machine grant.
    ///
    /// Short on purpose: this sits under a checkbox in a hire dialog, and the full sentence a person
    /// decides with is the engine's own (`syscap.describe`, rendered on the System destination). What
    /// this adds is the one clause that distinguishes a grant from its neighbours at a glance. It is a
    /// lookup keyed by the derived grant list, so a grant the engine adds shows up with a placeholder
    /// label rather than being silently absent from the hire form.
    private static let systemWording: [String: (label: String, detail: String)] = [
        "system:state": ("Read system state",
                         "battery, disk, uptime, running apps — read-only"),
        "system:clipboard": ("Use the clipboard",
                             "reading is safe; writing replaces what you copied"),
        "system:screenshot": ("Take screenshots",
                              "saved into the project, not your whole desktop"),
        "system:media": ("Control volume and media",
                         "changes state on your machine"),
        "system:open": ("Open applications",
                        "only apps you allowlist in configuration"),
        "system:automation": ("Run AppleScript",
                              "the most powerful grant — allowlisted handlers only"),
        "system:notify": ("Speak and notify",
                          "sound from the speakers, or a banner on the screen"),
        "system:search": ("Search the Mac",
                          "Spotlight's index — filenames outside the project too"),
        "system:power": ("Control sleep",
                         "keeps the Mac awake, or puts it to sleep now"),
        "system:network": ("Check the network",
                           "a throughput test that moves real data"),
        "system:shortcuts": ("Run your Shortcuts",
                             "only the ones you list — you wrote what they do"),
        "system:softwareupdate": ("Software updates",
                                  "listing is safe; installing can reboot the Mac"),
    ]

    /// The machine grants, in the catalogue's order, one choice each.
    public static var systemChoices: [Choice] {
        SystemTools.grants.map { grant in
            let wording = systemWording[grant]
            return Choice(grant: grant,
                          label: wording?.label ?? placeholderLabel(grant),
                          detail: wording?.detail
                              ?? "described in full on the System destination")
        }
    }

    /// `system:softwareupdate` -> "Softwareupdate". For a grant added to the engine that no one has
    /// written wording for yet: ugly on purpose, because a tidy invented label would read as a
    /// decision someone made about the grant.
    private static func placeholderLabel(_ grant: String) -> String {
        let scope = grant.contains(":") ? String(grant.split(separator: ":", maxSplits: 1)[1]) : grant
        return scope.replacingOccurrences(of: "_", with: " ").capitalized
    }

    public static var groups: [Group] {
        [
            Group(title: "In the project", isSystem: false, choices: projectChoices),
            Group(title: "On this Mac", isSystem: true, choices: systemChoices),
        ]
    }
}
