//
//  ProtocolModels.swift
//  AgentOrgKit
//
//  The Swift mirror of `engine/protocol.py`.
//
//  WHY THIS FILE EXISTS
//  --------------------
//  The Python engine and this app talk over one NDJSON stream. That stream is the only thing they
//  share, so it has to be a *contract* rather than a convention — and a contract written twice in two
//  languages drifts unless something checks it.
//
//  Two decisions keep it honest:
//
//  1. **A test decodes a `trace.jsonl` recorded by the real engine.** The fixtures are not hand-written
//     for Swift; they are produced by Python. A field renamed on one side therefore fails the build
//     rather than surfacing as a silent nil at runtime.
//  2. **An unknown event type is preserved, not dropped.** The engine may emit an event this build has
//     never seen; the app must keep running and show it as unrecognised rather than crash or ignore it.
//     This mirrors the engine's own forward-compatibility rule.
//
//  Deliberately NOT modelled here: the payload's inner shape. A payload is decoded as a JSON object and
//  read by key at the point of use. Modelling every payload as a nested Swift type would mean the app
//  breaks on any additive engine change, which is precisely what the protocol's versioning exists to
//  avoid.

import Foundation

/// One engine event, as it crosses the socket.
public struct EngineEvent: Codable, Sendable, Identifiable, Equatable {
    /// The protocol version the frame declared.
    public let v: Int
    /// A monotonic sequence number, assigned by the engine's bus.
    public let seq: Int
    /// The event type, e.g. `node.enter`. Kept as a `String` rather than an enum so an unknown type
    /// from a newer engine decodes instead of throwing.
    public let type: String
    public let payload: [String: JSONValue]
    public let runId: String?
    public let agentId: String?
    public let nodeId: String?
    public let sessionId: String?
    public let phase: String?
    public let ts: String?

    /// `seq` is the stable identity: it is assigned once and never reused, so it is the right key for
    /// a SwiftUI `ForEach` over a growing event list.
    public var id: Int { seq }

    enum CodingKeys: String, CodingKey {
        case v, seq, type, payload, ts, phase
        case runId = "run_id"
        case agentId = "agent_id"
        case nodeId = "node_id"
        case sessionId = "session_id"
    }

    /// Whether this build understands the event type.
    ///
    /// The UI uses this to render an unrecognised event distinctly — showing it as raw is honest;
    /// hiding it would make a newer engine's behaviour invisible.
    public var isKnown: Bool { EventType.isKnown(type) }

    /// A short summary for a one-line terminal entry, chosen per event type.
    public var summary: String {
        switch type {
        case "run.start": return "run started"
        case "run.end": return "run finished: \(payload["outcome"]?.stringValue ?? "unknown")"
        case "manifest.proposed":
            let nodes = payload["nodes"]?.arrayValue?.count ?? 0
            return "graph proposed with \(nodes) node(s)"
        case "manifest.approved": return "graph approved"
        case "node.enter": return "entering \(nodeId ?? "node")"
        case "node.exit": return "left \(nodeId ?? "node")"
        case "agent.spawn":
            return "agent \(payload["name"]?.stringValue ?? agentId ?? "?") ready"
        case "agent.log": return payload["text"]?.stringValue ?? ""
        case "llm.request":
            return "\(payload["provider"]?.stringValue ?? "?") → \(payload["model"]?.stringValue ?? "?")"
        case "llm.response":
            let tokens = payload["usage"]?["total_tokens"]?.intValue
                ?? ((payload["usage"]?["prompt_tokens"]?.intValue ?? 0)
                    + (payload["usage"]?["completion_tokens"]?.intValue ?? 0))
            return "reply in \(payload["latency_ms"]?.intValue ?? 0)ms, \(tokens) token(s)"
        case "artifact.written":
            let bytes = payload["bytes"]?.intValue ?? 0
            return "wrote \(payload["path"]?.stringValue ?? "?") (\(bytes) bytes)"
        case "checklist.result":
            let items = payload["items"]?.arrayValue?.count ?? 0
            return "checklist: \(items) item(s) reported"
        case "review.rejected": return "review rejected: \(payload["summary"]?.stringValue ?? "")"
        case "review.approved": return "review approved"
        case "human.gate": return "waiting on you: \(payload["reason"]?.stringValue ?? "")"
        case "human.decision":
            let approved = payload["approved"]?.boolValue ?? false
            return "you \(approved ? "approved" : "rejected") \(payload["gate_id"]?.stringValue ?? "")"
        case "session.compact":
            return "compacted \(payload["recovered"]?.intValue ?? 0) token(s)"
        case "session.saturation":
            let band = payload["band"]?.stringValue ?? "?"
            let percent = Int((payload["saturation"]?.doubleValue ?? 0) * 100)
            return "context \(percent)% (\(band))"
        case "session.rotate", "session.rotate.requested":
            return "rotating: \(payload["trigger"]?.stringValue ?? "?")"
        case "handoff.verified":
            return "handoff verified \(payload["from"]?.stringValue ?? "?") → \(payload["to"]?.stringValue ?? "?")"
        case "route.decided", "route.proposed":
            let chosen = payload["chosen"]?.stringValue
            let proposed = payload["proposed"]?.boolValue ?? false
            if proposed {
                return "route proposed — waiting on you"
            }
            return "routed to \(chosen ?? "?") (\(payload["autonomy"]?.stringValue ?? "?"))"
        case "agent.spawn.requested":
            return "hire requested (\(payload["tier"]?.stringValue ?? "?") tier) — needs your approval"
        case "agent.slo.breach":
            return "SLO breach: \(payload["objective"]?.stringValue ?? "?")"
        case "command.ack":
            return (payload["ok"]?.boolValue ?? false) ? "command acknowledged" : "command refused"
        case "engine.ready":
            let providers = payload["providers"]?.arrayValue?.count ?? 0
            return "engine ready (\(providers) provider(s))"
        case "guardrail.blocked", "guardrail.block":
            return "guardrail blocked a payload"
        case "cost.ceiling": return "budget ceiling reached — the run parked"
        case "cost.reconciled":
            return "cost estimate drifted \(payload["error_pct"]?.doubleValue ?? 0)%"
        case "guardrail.blocked": return "guardrail blocked a payload"
        case "agent.health.changed":
            return "health \(payload["current"]?.stringValue ?? "?")"
        case "goal.armed":
            return "goal armed — the run will continue until it is done"
        case "goal.resumed":
            return "goal resumed — a fresh slice of budget granted"
        case "goal.progress":
            return "goal round \(payload["round"]?.stringValue ?? "?") — still working"
        case "goal.paused":
            return "goal paused (\(payload["reason"]?.stringValue ?? "manual"))"
        case "goal.completed":
            return "goal complete: \(payload["summary"]?.stringValue ?? "done")"
        case "goal.blocked":
            return "goal blocked: \(payload["reason"]?.stringValue ?? "needs you")"
        case "goal.cleared": return "goal cleared"
        case "subagent.spawned":
            return "subagent \(payload["child_id"]?.stringValue ?? "?") started"
        case "subagent.done":
            return "subagent \(payload["child_id"]?.stringValue ?? "?") finished"
        case "subagent.failed":
            return "subagent \(payload["child_id"]?.stringValue ?? "?") failed: "
                + "\(payload["error"]?.stringValue ?? "unknown")"
        case "subagent.read":
            return "read \(payload["returned_bytes"]?.stringValue ?? "?") bytes of "
                + "\(payload["child_id"]?.stringValue ?? "?")"
        case "model.catalog.refreshed":
            let provider = payload["provider_id"]?.stringValue ?? "?"
            let reason = payload["reason"]?.stringValue ?? "refreshed"
            return provider == "?" ? "model catalog \(reason)" : "\(provider): catalog \(reason)"
        case "error": return payload["message"]?.stringValue ?? "an error occurred"
        default: return type
        }
    }
}

/// The event types this build knows, mirroring `EventType` in the engine.
///
/// A `Set<String>` rather than an enum: the enum's exhaustiveness would be a liability across a
/// language boundary, where the app cannot be recompiled the moment the engine adds a type.
public enum EventType {
    public static let known: Set<String> = [
        "run.start", "run.queued", "run.admitted", "run.paused", "run.resumed",
        "run.aborted", "run.end", "run.log",
        "manifest.proposed", "manifest.approved", "phase.enter", "phase.exit",
        "node.enter", "node.exit", "loop.pass", "loop.stagnation",
        "agent.spawn", "agent.status", "agent.log",
        "agent.spawn.requested", "agent.spawn.approved", "agent.spawn.denied",
        "agent.spawned", "agent.destroyed", "agent.retirement_review", "agent.org.changed",
        "llm.request", "llm.response", "model.catalog.refreshed",
        "artifact.written", "checklist.result", "review.rejected", "review.approved",
        "run.criteria.satisfied",
        "route.proposed", "route.decided", "route.overridden",
        "handoff.proposed", "handoff.accepted", "handoff.rejected", "handoff.fulfilled",
        "handoff.breached", "handoff.verified", "delegation.rejected",
        "session.open", "session.saturation", "session.compact", "session.rotate",
        "session.rotate.requested", "session.sealed", "session.handoff.verified",
        "session.closed", "context.irreducible_overflow", "attention.decay",
        "agent.health.changed", "agent.slo.breach", "agent.quarantined", "agent.recovered",
        "agent.sprawl.suspected",
        "human.gate", "human.decision", "human.takeover", "human.released",
        "policy.changed", "decision.recorded",
        "goal.armed", "goal.resumed", "goal.progress", "goal.paused",
        "goal.completed", "goal.blocked", "goal.cleared",
        "subagent.spawned", "subagent.progress", "subagent.done",
        "subagent.failed", "subagent.read",
        "backpressure.on", "backpressure.off", "cost.ceiling", "cost.reconciled",
        "budget.burn", "watchdog.restart", "watchdog.stall", "schema.migrated",
        "schema.refused", "effect.applied", "effect.replayed", "leak.detected",
        "span.exported", "diagnostics.exported", "error", "command.ack",
        "guardrail.blocked",
        // The readiness handshake: the console treats this as the proof the engine is usable, so an
        // unknown-event warning here would be exactly backwards.
        "engine.ready",
    ]

    public static func isKnown(_ type: String) -> Bool { known.contains(type) }
}

/// One engine command, as it crosses the socket inbound.
public struct EngineCommand: Codable, Sendable {
    public let cmdId: String
    public let type: String
    public let payload: [String: JSONValue]
    public let ts: String
    public let v: Int

    enum CodingKeys: String, CodingKey {
        case type, payload, ts, v
        case cmdId = "cmd_id"
    }

    public init(cmdId: String, type: String, payload: [String: JSONValue] = [:],
                ts: String = Protocol.timestamp(), v: Int = Protocol.version) {
        self.cmdId = cmdId
        self.type = type
        self.payload = payload
        self.ts = ts
        self.v = v
    }
}

/// Protocol constants, mirroring `engine/protocol.py`.
public enum Protocol {
    public static let version = 1

    /// An ISO-8601 UTC timestamp with millisecond precision, matching the engine's format so the two
    /// halves' traces interleave with a consistent ordering.
    public static func timestamp() -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: Date())
    }

    /// A unique command id.
    ///
    /// Time-prefixed so ids sort chronologically in a trace, random-suffixed so two commands minted in
    /// the same millisecond cannot collide — the same scheme the engine uses.
    public static func newCommandId() -> String {
        let millis = Int(Date().timeIntervalSince1970 * 1000)
        let suffix = String(UInt32.random(in: 0...UInt32.max), radix: 16, uppercase: false)
        return "cmd_\(String(format: "%013d", millis))_\(suffix)"
    }
}

/// A JSON value, so a payload whose shape this build does not model can still be read by key.
///
/// Writing this by hand rather than using `AnyCodable` avoids a dependency, and the recursive enum is
/// the idiomatic Swift representation of JSON — every case is explicit, so a value that cannot be
/// represented is a compile error rather than a runtime surprise.
/// `Hashable` as well as `Equatable` so a payload value can be a SwiftUI `ForEach` identity — the org
/// roster and node table are lists of payload objects, and a `ForEach` over them needs a stable key.
public enum JSONValue: Codable, Sendable, Equatable, Hashable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null; return }
        if let value = try? container.decode(Bool.self) { self = .bool(value); return }
        if let value = try? container.decode(Int.self) { self = .int(value); return }
        if let value = try? container.decode(Double.self) { self = .double(value); return }
        if let value = try? container.decode(String.self) { self = .string(value); return }
        if let value = try? container.decode([JSONValue].self) { self = .array(value); return }
        if let value = try? container.decode([String: JSONValue].self) { self = .object(value); return }
        throw DecodingError.dataCorruptedError(in: container,
                                               debugDescription: "unrepresentable JSON value")
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }

    public var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    public var intValue: Int? {
        switch self {
        case .int(let value): return value
        case .double(let value): return Int(value)
        default: return nil
        }
    }

    public var doubleValue: Double? {
        switch self {
        case .double(let value): return value
        case .int(let value): return Double(value)
        default: return nil
        }
    }

    public var boolValue: Bool? {
        if case .bool(let value) = self { return value }
        return nil
    }

    public var arrayValue: [JSONValue]? {
        if case .array(let value) = self { return value }
        return nil
    }

    public var objectValue: [String: JSONValue]? {
        if case .object(let value) = self { return value }
        return nil
    }
}

extension JSONValue {
    /// Look a key up inside an object value, returning nil for a non-object.
    ///
    /// A subscript on `JSONValue` rather than only on the dictionary, so a nested payload can be walked
    /// with `payload["usage"]?["total_tokens"]` — the shape most engine payloads have, and one the
    /// dictionary-only version could not express.
    public subscript(key: String) -> JSONValue? {
        objectValue?[key]
    }
}

extension Dictionary where Key == String, Value == JSONValue {
    /// Look a key up by a dotted path, e.g. `payload["usage.total_tokens"]`.
    public subscript(path path: String) -> JSONValue? {
        let parts = path.split(separator: ".").map(String.init)
        guard let first = parts.first else { return nil }
        var current: JSONValue? = self[first]
        for part in parts.dropFirst() {
            current = current?[part]
        }
        return current
    }
}

/// A decoded line from the engine's stream.
public enum DecodedFrame: Sendable {
    case event(EngineEvent)
    case unparsable(String)
}

/// The line decoder: partial lines, split UTF-8, and a bounded buffer.
///
/// A pipe hands over arbitrary byte chunks, so a line arrives across several reads and a multi-byte
/// character can be split across two of them. Decoding naively at each read would corrupt text and lose
/// events — and the loss would be invisible, which is why this is a type with its own tests rather than
/// a few lines inside the service.
public final class LineDecoder {
    private var buffer = Data()
    private let maxLineBytes: Int

    /// - Parameter maxLineBytes: A single line above this is dropped rather than buffered, so a
    ///   runaway payload cannot grow the app's memory without bound.
    public init(maxLineBytes: Int = 8 << 20) {
        self.maxLineBytes = maxLineBytes
    }

    /// Feed bytes, returning every complete line now available.
    public func append(_ data: Data) -> [DecodedFrame] {
        buffer.append(data)
        var frames: [DecodedFrame] = []
        while let newline = buffer.firstIndex(of: 0x0A) {
            let lineData = buffer[buffer.startIndex..<newline]
            buffer.removeSubrange(buffer.startIndex...newline)
            guard !lineData.isEmpty else { continue }
            frames.append(LineDecoder.decode(lineData))
        }
        if buffer.count > maxLineBytes {
            // Discard the partial line rather than let it grow: it is already unusable, and reporting it
            // is better than exhausting memory for a frame that can never complete.
            let dropped = buffer.count
            buffer.removeAll(keepingCapacity: false)
            frames.append(.unparsable("<line exceeded \(maxLineBytes) bytes after \(dropped) buffered>"))
        }
        return frames
    }

    /// Decode one complete line.
    static func decode(_ data: Data) -> DecodedFrame {
        let decoder = JSONDecoder()
        if let event = try? decoder.decode(EngineEvent.self, from: data) {
            return .event(event)
        }
        let text = String(data: data, encoding: .utf8) ?? "<non-utf8 line>"
        return .unparsable(text)
    }

    /// Bytes held for an incomplete line. Exposed so a test can assert the buffer drains.
    public var pendingBytes: Int { buffer.count }
}
