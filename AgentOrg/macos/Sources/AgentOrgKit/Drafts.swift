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
        // Only send a literal when there is no variable and the field was actually filled. An empty
        // field means "unchanged", which for a local endpoint also means "no key" — both are honest.
        if !key.isEmpty && env.isEmpty { out["api_key"] = .string(key) }
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

    public init(name: String = "", skill: String = "", provider: String = "", model: String = "",
                level: String = "senior", team: String = "", title: String = "") {
        self.name = name
        self.skill = skill
        self.provider = provider
        self.model = model
        self.level = level
        self.team = team
        self.title = title
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
            title: existing["title"]?.stringValue ?? "")
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
