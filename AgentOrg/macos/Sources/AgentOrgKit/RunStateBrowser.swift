//
//  RunStateBrowser.swift
//  AgentOrgKit
//
//  Reading the run's own state with the engine down.
//
//  WHY THIS IS WORTH A FILE
//  ------------------------
//  Everything else in the console needs the engine: the panels are rendered from `status`, which is a
//  command, and a command needs a live child. That leaves the most common moment of confusion — *the
//  run stopped and I want to know what it did* — as the one moment the console shows nothing.
//
//  The engine already writes everything needed for that answer to `.agent_state/`: a run checkpoint,
//  one JSON document per handoff, and the cache's own streams. This type reads those files directly.
//
//  Three rules, each for a failure this codebase has already had:
//
//  1. **Every read goes through `WorkspaceWriter`.** The paths here are *data* — a handoff id, a
//     filename out of a jsonl line — and a browser that joined them onto a path itself would be a read
//     anywhere the user can reach. `WorkspaceWriter` already refuses traversal, absolute paths and
//     symlink escapes, and a second implementation would be a second thing to get wrong.
//  2. **A missing file is not an error.** A fresh workspace has no runs and no handoffs; that is a
//     normal answer, and the panel must render it as "nothing has run" rather than as a failure.
//  3. **A torn last line is skipped.** Every durable write in the engine is append-only, so the last
//     line of a jsonl file can be a partial write after a kill. Skipping it is what makes reading the
//     live file safe while the engine is also writing it.

import Foundation

/// One past run, read from `.agent_state/run_state.json`.
///
/// A value type with the fields the UI renders, rather than a raw dictionary: the run-history list is
/// a fixed set of columns, and naming them here means a renamed engine field shows up as a decode
/// problem in one place instead of as a blank cell in a table.
public struct RunSummary: Identifiable, Sendable, Equatable {
    public let id: String
    public let workflow: String
    public let phase: String
    public let node: String
    public let updated: String
    /// Every node's status, in name order.
    public let nodes: [(name: String, status: String, verdict: String, iterations: Int)]
    /// The run's own budget block, as the engine wrote it.
    public let budget: [String: JSONValue]

    public static func == (lhs: RunSummary, rhs: RunSummary) -> Bool {
        lhs.id == rhs.id && lhs.updated == rhs.updated && lhs.phase == rhs.phase
            && lhs.node == rhs.node
            && lhs.nodes.map(\.name) == rhs.nodes.map(\.name)
            && lhs.nodes.map(\.status) == rhs.nodes.map(\.status)
    }

    /// Whether any node ended stopped short, which is the one thing a person scanning the list needs.
    ///
    /// The predicate is the engine's own, mirrored in `BoardStop` so it has one home in this app: a
    /// node that has not finished whose status *or* verdict names a stop (`engine/flow.py:258`,
    /// `is_stuck`, and the sets at `:130-131`). It was `status == "blocked" || verdict ==
    /// "guardrail-blocked"`, which missed the stops that are not called "blocked" — a node stopped by
    /// its completion contract (`needs_review`) or parked at a human gate (`awaiting_owner`) — so this
    /// list read "0 blocked" for a run whose own board read "1 stuck". The two are read together, and
    /// one question with two answers is what a person cannot check.
    ///
    /// The verdict clause is kept as the engine keeps it, and so is the guard in front of it: a
    /// *finished* node is never stopped whatever verdict it still carries, because a released gate
    /// keeps its `awaiting_owner` verdict after its status becomes `done` (see `Orchestrator
    /// ._detect_gate`, and `engine/flow.py:258`'s own note about it).
    public var blockedCount: Int {
        nodes.filter { BoardStop.isStuck(status: $0.status, verdict: $0.verdict) }.count
    }

    /// A one-line description for the list row.
    public var detail: String {
        let done = nodes.filter { $0.status == "done" }.count
        var parts = ["\(done)/\(nodes.count) node(s) done"]
        if blockedCount > 0 { parts.append("\(blockedCount) blocked") }
        if !node.isEmpty { parts.append("last: \(node)") }
        return parts.joined(separator: " · ")
    }
}

/// One handoff, read from `.agent_state/handoffs/<id>.json`.
///
/// The typed handoff is the engine's newest durable record, and it is the one that answers "what
/// exactly crossed between those two nodes" rather than "that something did". The payload's nine
/// registry fields are kept whole, because a browser that summarised them would hide the very thing
/// the contract exists to enforce.
public struct HandoffSummary: Identifiable, Sendable, Equatable {
    public let id: String
    public let kind: String
    public let origin: String
    public let target: String
    public let state: String
    public let attempt: Int
    public let createdAt: String
    public let status: String
    public let summary: String
    public let artifacts: [String]
    public let openQuestions: [String]
    public let budget: [String: JSONValue]

    /// Whether this crossing needs a person: the two terminal states that mean it did not complete.
    public var needsAttention: Bool { state == "REJECTED" || state == "BREACHED" || state == "ESCALATED" }
}

/// One pinned prefix, read from `.agent_state/cache/prefix/<hash>.json`.
public struct PrefixSummary: Identifiable, Sendable, Equatable {
    public let id: String
    public let skill: String
    public let tools: [String]
    public let chars: Int
    public let observations: Int
    public let source: String
    public let lastSeen: String
}

/// The offline reader for `.agent_state/`.
///
/// Holds a `WorkspaceWriter` rather than a path so containment is the *same* check the rest of the app
/// uses — see the file header. The engine state directory name is repeated here from
/// `engine/state.py` (`ENGINE_STATE_DIRNAME`) as the one place in the app that names it.
public final class RunStateBrowser: @unchecked Sendable {

    /// `.agent_state/` — the engine's own directory name, mirrored from `engine/state.py`.
    public static let stateDirectory = ".agent_state"

    private let writer: WorkspaceWriter

    public init(writer: WorkspaceWriter) {
        self.writer = writer
    }

    // MARK: - The run checkpoint

    /// The last run's checkpoint, or nil when this workspace has never run.
    ///
    /// `run_state.json` is the runner's per-node checkpoint, which is the file that says what each
    /// node actually reached. It is read rather than `status` because it survives the engine: this is
    /// the answer that is still available after the process is gone.
    public func lastRun() -> RunSummary? {
        guard let object = try? writer.readJSON("\(Self.stateDirectory)/run_state.json") else {
            return nil
        }
        // `workflow` is the runner's own name for the project; a checkpoint without it is not a run
        // this build understands, and guessing one would put a fabricated row in the history list.
        guard let workflow = object["workflow"] as? String, !workflow.isEmpty else { return nil }

        let nodesObject = object["nodes"] as? [String: Any] ?? [:]
        let nodes = nodesObject.map { name, value -> (String, String, String, Int) in
            let record = value as? [String: Any] ?? [:]
            return (name,
                    record["status"] as? String ?? "",
                    record["verdict"] as? String ?? "",
                    record["iterations"] as? Int ?? 0)
        }
        .sorted { $0.0 < $1.0 }
        .map { (name: $0.0, status: $0.1, verdict: $0.2, iterations: $0.3) }

        return RunSummary(
            id: object["manifest_sha"] as? String ?? workflow,
            workflow: workflow,
            phase: object["phase"] as? String ?? "",
            node: object["node"] as? String ?? "",
            updated: object["updated"] as? String ?? "",
            nodes: nodes,
            budget: JSONValue.fromAny(object["budget"])?.objectValue ?? [:])
    }

    /// The run's goal document, when one has been set.
    ///
    /// Read offline because the goal is what says whether a stopped run *meant* to stop — a paused
    /// goal and an abandoned run look identical in the checkpoint.
    public func goal() -> [String: JSONValue] {
        guard let object = try? writer.readJSON("\(Self.stateDirectory)/goal.json") else { return [:] }
        return JSONValue.fromAny(object)?.objectValue ?? [:]
    }

    // MARK: - Handoffs

    /// Every persisted handoff, newest first.
    ///
    /// Sorted by the file's own `created_at` where present, falling back to the id, so the order is a
    /// property of the record rather than of the filesystem's mtime — which a git checkout or a copy
    /// rewrites wholesale.
    public func handoffs(limit: Int = 200) -> [HandoffSummary] {
        let directory = "\(Self.stateDirectory)/handoffs"
        guard let names = try? writer.list(under: directory) else { return [] }
        var result: [HandoffSummary] = []
        for name in names where name.hasSuffix(".json") {
            guard let object = try? writer.readJSON(name) else { continue }
            guard let handoff = Self.decodeHandoff(object, fallbackId: name) else { continue }
            result.append(handoff)
        }
        result.sort { $0.createdAt == $1.createdAt ? $0.id > $1.id : $0.createdAt > $1.createdAt }
        return Array(result.prefix(limit))
    }

    private static func decodeHandoff(_ object: [String: Any],
                                      fallbackId: String) -> HandoffSummary? {        guard let payload = object["payload"] as? [String: Any] else { return nil }
        // The id names the file and is what a person uses to find it again, so a document that
        // carries none is keyed by its own filename rather than dropped — the record is still real.
        let id = object["handoff_id"] as? String ?? Self.stem(fallbackId)
        let openQuestions = (payload["open_questions"] as? [Any] ?? []).map { entry -> String in
            if let text = entry as? String { return text }
            let record = entry as? [String: Any] ?? [:]
            return record["question"] as? String ?? record["text"] as? String ?? ""
        }
        .filter { !$0.isEmpty }
        return HandoffSummary(
            id: id,
            kind: object["kind"] as? String ?? "handoff",
            origin: object["origin"] as? String ?? "",
            target: object["target"] as? String ?? "",
            state: object["state"] as? String ?? "",
            attempt: object["attempt"] as? Int ?? 1,
            createdAt: object["created_at"] as? String ?? "",
            status: payload["status"] as? String ?? "",
            summary: payload["summary"] as? String ?? "",
            artifacts: (payload["artifacts"] as? [Any] ?? []).map { entry -> String in
                if let text = entry as? String { return text }
                return (entry as? [String: Any])?["path"] as? String ?? ""
            }
            .filter { !$0.isEmpty },
            openQuestions: openQuestions,
            budget: JSONValue.fromAny(payload["budget"])?.objectValue ?? [:])
    }

    // MARK: - The session archive

    /// One archived session: which agent it belonged to, its id, and how much is on disk for it.
    public struct SessionArchive: Identifiable, Sendable, Equatable {
        public let id: String
        public let agent: String
        public let session: String
        /// How many files the archive holds, and its total size — the honest measure of "is there
        /// anything here", because the engine's session directory is a folder rather than one file.
        public let files: Int
        public let bytes: Int
        /// When the directory was last written, as the filesystem reports it.
        public let updated: Date?
    }

    /// Every session the engine has archived under `.agent_state/sessions/`.
    ///
    /// This is the **disk** answer to "what have I run here", as opposed to the live one. The engine
    /// archives a session when it seals one — a rotation at the top of the context ladder, or a
    /// handoff — into `sessions/<agent_id>/<session_id>/`, so an empty result means the engine has not
    /// rotated anything *yet*, not that the app failed to look. Saying that plainly is the whole point
    /// of this read, which is why an empty list is returned rather than an error.
    ///
    /// Containment holds as it does everywhere else: `list` refuses a path that escapes the workspace,
    /// and every name here is resolved through it rather than joined onto a string.
    public func sessions() -> [SessionArchive] {
        let root = "\(Self.stateDirectory)/sessions"
        guard let paths = try? writer.list(under: root) else { return [] }
        // The listing is flat and recursive, so the agent and session are recovered from the path:
        // `.agent_state/sessions/<agent>/<session>/<file>`. A path that does not have that depth is
        // skipped rather than guessed at — a fabricated row would be worse than an absent one.
        var grouped: [String: (agent: String, session: String, files: Int, bytes: Int)] = [:]
        for path in paths {
            // `.agent_state/sessions/<agent>/<session>/<file>` — five components exactly, so a file
            // dropped directly in `sessions/` is skipped rather than attributed to a made-up agent.
            let parts = path.split(separator: "/").map(String.init)
            guard parts.count >= 5, parts[0] == Self.stateDirectory else { continue }
            let agent = parts[2], session = parts[3]
            let key = "\(agent)/\(session)"
            var entry = grouped[key] ?? (agent: agent, session: session, files: 0, bytes: 0)
            entry.files += 1
            entry.bytes += writer.size(of: path) ?? 0
            grouped[key] = entry
        }
        return grouped.map { key, entry in
            SessionArchive(id: key, agent: entry.agent, session: entry.session,
                           files: entry.files, bytes: entry.bytes,
                           updated: lastWrite(of: "\(root)/\(key)"))
        }
        .sorted { $0.id < $1.id }
    }

    /// When a directory was last written, or nil when the filesystem will not say.
    private func lastWrite(of relative: String) -> Date? {
        guard let url = try? writer.resolve(relative) else { return nil }
        let values = try? url.resourceValues(forKeys: [.contentModificationDateKey])
        return values?.contentModificationDate
    }

    // MARK: - The cache store

    /// Every pinned prefix, most recently seen first.
    public func prefixes(limit: Int = 200) -> [PrefixSummary] {
        let directory = "\(Self.stateDirectory)/cache/prefix"
        guard let names = try? writer.list(under: directory) else { return [] }
        var result: [PrefixSummary] = []
        for name in names where name.hasSuffix(".json") {
            guard let object = try? writer.readJSON(name) else { continue }
            result.append(PrefixSummary(
                id: object["prefix_hash"] as? String ?? Self.stem(name),
                skill: object["skill"] as? String ?? "",
                tools: (object["tool_names"] as? [Any] ?? []).compactMap { $0 as? String },
                chars: object["chars"] as? Int ?? 0,
                observations: object["observations"] as? Int ?? 0,
                source: object["source"] as? String ?? "",
                lastSeen: object["last_seen"] as? String ?? ""))
        }
        result.sort { $0.lastSeen == $1.lastSeen ? $0.id < $1.id : $0.lastSeen > $1.lastSeen }
        return Array(result.prefix(limit))
    }

    /// The tail of one of the cache's append-only streams.
    ///
    /// - Parameter stream: `shapes` or `savings`, which are the two files the store keeps. Named
    ///   rather than passed as a path so a caller cannot ask for an arbitrary file through this door.
    public func cacheStream(_ stream: CacheStream, limit: Int = 200) -> [[String: JSONValue]] {
        let path = "\(Self.stateDirectory)/cache/\(stream.filename)"
        guard let text = try? writer.readText(path) else { return [] }
        return RunStateBrowser.jsonlTail(text, limit: limit)
    }

    public enum CacheStream: String, Sendable, CaseIterable {
        case shapes
        case savings
        public var filename: String { "\(rawValue).jsonl" }
    }

    /// A problem with the state directory itself, or nil.
    ///
    /// Everything *inside* the directory is read best-effort — a missing file or a torn line means
    /// "no data", which is the honest thing to render. The directory is different: if `.agent_state`
    /// is a symlink pointing out of the workspace, `WorkspaceWriter.resolve` refuses it, and the
    /// panel would otherwise draw an empty list. Empty and *refused* look identical, and only one of
    /// them is a state the user should be told about.
    public func stateDirectoryProblem() -> String? {
        do {
            _ = try writer.resolve(Self.stateDirectory)
            return nil
        } catch let error as WorkspaceError {
            return "the engine state directory was refused: \(error.message)"
        } catch {
            return "the engine state directory could not be read: \(error.localizedDescription)"
        }
    }

    // MARK: - jsonl

    /// The base name of a workspace-relative path, with any extension removed.
    ///
    /// `WorkspaceWriter.list` returns *relative* paths (`.agent_state/handoffs/orphan.json`), so a
    /// document that carries no id of its own would otherwise be listed under its whole path — a row
    /// no one could match against the file on disk.
    static func stem(_ relative: String) -> String {
        (relative as NSString).lastPathComponent.replacingOccurrences(of: ".json", with: "")
    }

    /// The last `limit` decodable records of a JSONL document, newest last.
    ///
    /// A torn final line — the normal result of a kill mid-write — is skipped rather than fatal, which
    /// is the same rule the engine's own replay applies. Reading only the tail keeps a long run's
    /// multi-megabyte stream from being parsed in full just to draw the last few rows.
    public static func jsonlTail(_ text: String, limit: Int) -> [[String: JSONValue]] {
        var records: [[String: JSONValue]] = []
        for line in text.split(separator: "\n", omittingEmptySubsequences: true) {
            guard let data = line.data(using: .utf8),
                  let value = try? JSONDecoder().decode(JSONValue.self, from: data),
                  let object = value.objectValue else { continue }
            records.append(object)
        }
        return Array(records.suffix(max(0, limit)))
    }
}

extension JSONValue {
    /// Convert a `JSONSerialization` result into a `JSONValue`.
    ///
    /// Needed because `WorkspaceWriter.readJSON` returns `[String: Any]` — it predates this model and
    /// is used by other callers — so the two representations have to meet somewhere. Unrepresentable
    /// values become `.null` rather than throwing: a single odd field in a run checkpoint must not
    /// cost the whole panel.
    public static func fromAny(_ any: Any?) -> JSONValue? {
        switch any {
        case nil, is NSNull: return .null
        case let value as String: return .string(value)
        case let value as Bool: return .bool(value)
        case let value as Int: return .int(value)
        case let value as Double: return .double(value)
        case let value as NSNumber: return .double(value.doubleValue)
        case let value as [Any]: return .array(value.map { JSONValue.fromAny($0) ?? .null })
        case let value as [String: Any]:
            return .object(value.mapValues { JSONValue.fromAny($0) ?? .null })
        default: return nil
        }
    }
}
