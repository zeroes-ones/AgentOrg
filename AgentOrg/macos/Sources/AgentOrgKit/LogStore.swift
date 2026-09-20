//
//  LogStore.swift
//  AgentOrgKit
//
//  The bounded, coalesced log buffer behind the terminal view.
//
//  WHY THIS IS NOT JUST AN ARRAY
//  -----------------------------
//  A run emits an event per node, per model call, per token batch. Appending each one to an array bound
//  to a SwiftUI list does two bad things at once: the array grows without bound over a long run, and
//  every append invalidates the list, so the terminal re-renders thousands of times per minute while the
//  user is trying to read it.
//
//  The design's answer is three properties, and this type exists to provide them:
//
//  1. **A ring buffer.** The most recent `capacity` lines are kept and the rest are counted as dropped
//     — counted, not silently discarded, because a terminal that quietly forgets is lying about history.
//  2. **Coalescing.** Appends accumulate and are published at most once per interval, so a burst becomes
//     one update rather than thousands.
//  3. **Publishing on the main actor.** The views are main-actor state, so the hand-off is explicit
//     rather than incidental.
//
//  It is `@MainActor` because it *is* view state. Decoding happens off the main actor in the process
//  service; only the finished lines arrive here.

import Foundation

/// One line in the terminal.
public struct LogLine: Identifiable, Sendable, Equatable {
    public let id: Int
    public let seq: Int
    public let kind: Kind
    public let text: String
    public let timestamp: String
    public let isKnownEvent: Bool
    /// The correlation fields, kept so the terminal can be filtered by agent or node.
    public let agentId: String?
    public let nodeId: String?
    public let phase: String?

    public enum Kind: String, Sendable, Equatable {
        case event
        case diagnostic
        case unparsable
        case notice
    }

    public init(id: Int, seq: Int, kind: Kind, text: String, timestamp: String,
                isKnownEvent: Bool = true, agentId: String? = nil, nodeId: String? = nil,
                phase: String? = nil) {
        self.id = id
        self.seq = seq
        self.kind = kind
        self.text = text
        self.timestamp = timestamp
        self.isKnownEvent = isKnownEvent
        self.agentId = agentId
        self.nodeId = nodeId
        self.phase = phase
    }
}

/// A bounded, coalescing log buffer.
@MainActor
public final class LogStore: ObservableObject {

    /// The lines currently held, oldest first.
    @Published public private(set) var lines: [LogLine] = []
    /// How many lines were dropped from the front of the buffer.
    ///
    /// Published so the UI can show it. A terminal that silently forgot its oldest lines would make a
    /// long run look like it started later than it did.
    @Published public private(set) var droppedCount: Int = 0
    /// The highest sequence number seen, for a "live" indicator.
    @Published public private(set) var lastSeq: Int = 0
    /// Bumped whenever `lines` changes, so a view can cache something *derived* from the buffer
    /// without re-deriving it on every body evaluation.
    ///
    /// A count is not enough to detect a change here: at `capacity` the buffer stops growing, so an
    /// append that trims the front leaves the count identical while every line has shifted by one.
    /// Nothing else published by this type distinguishes those two states.
    @Published public private(set) var revision: Int = 0

    private let capacity: Int
    private let publishInterval: TimeInterval
    private var buffer: [LogLine] = []
    private var dirty = false
    private var lastPublish = Date()
    private var nextId = 1

    /// - Parameters:
    ///   - capacity: How many lines to retain. 20,000 is roughly a long run's worth at a readable size.
    ///   - publishInterval: The minimum gap between published updates, in seconds. 1/60 keeps the
    ///     terminal at about a display frame without re-rendering per line.
    public init(capacity: Int = 20_000, publishInterval: TimeInterval = 1.0 / 60.0) {
        self.capacity = max(1, capacity)
        self.publishInterval = publishInterval
    }

    /// Append an engine event.
    public func append(_ event: EngineEvent) {
        buffer.append(LogLine(
            id: nextId,
            seq: event.seq,
            kind: .event,
            text: event.summary,
            timestamp: event.ts ?? "",
            isKnownEvent: event.isKnown,
            agentId: event.agentId,
            nodeId: event.nodeId,
            phase: event.phase))
        nextId += 1
        lastSeq = max(lastSeq, event.seq)
        trim()
        schedulePublish()
    }

    /// Append a diagnostic line from the engine's stderr.
    public func append(diagnostic text: String) {
        buffer.append(LogLine(id: nextId, seq: lastSeq, kind: .diagnostic,
                              text: text, timestamp: Protocol.timestamp()))
        nextId += 1
        trim()
        schedulePublish()
    }

    /// Append a line the protocol could not decode.
    ///
    /// Kept as its own kind rather than dropped: an unparsable line means the contract drifted, and
    /// hiding it would make that drift invisible until something else broke.
    public func append(unparsable text: String) {
        buffer.append(LogLine(id: nextId, seq: lastSeq, kind: .unparsable,
                              text: "unparsable: \(text)", timestamp: Protocol.timestamp(),
                              isKnownEvent: false))
        nextId += 1
        trim()
        schedulePublish()
    }

    /// Append a notice the app itself is making, so the terminal shows the app's own actions in sequence
    /// with the engine's.
    public func append(notice text: String) {
        buffer.append(LogLine(id: nextId, seq: lastSeq, kind: .notice,
                              text: text, timestamp: Protocol.timestamp()))
        nextId += 1
        trim()
        schedulePublish()
    }

    /// Force a publish, so a caller that needs the UI current now can have it.
    public func flush() {
        guard dirty else { return }
        publish()
    }

    /// Remove every line. Does not reset `droppedCount`: what was dropped remains true.
    public func clear() {
        buffer.removeAll()
        lines = []
        dirty = false
        revision += 1
    }

    /// Remove just one kind of line.
    ///
    /// A diagnostic flood is what makes a terminal unreadable, and clearing it must not take the
    /// events with it — which is the same argument the kind filter makes, so the two agree about what
    /// "this kind" means. `droppedCount` is left alone for the same reason as `clear()`.
    public func clear(kind: LogLine.Kind) {
        buffer.removeAll { $0.kind == kind }
        lines = buffer
        dirty = false
        revision += 1
    }

    /// Lines matching a filter, for a filtered terminal view.
    public func filtered(kind: LogLine.Kind? = nil, agentId: String? = nil,
                         nodeId: String? = nil, search: String? = nil) -> [LogLine] {
        var result = lines
        if let kind { result = result.filter { $0.kind == kind } }
        if let agentId { result = result.filter { $0.agentId == agentId } }
        if let nodeId { result = result.filter { $0.nodeId == nodeId } }
        if let search, !search.isEmpty {
            result = result.filter { $0.text.localizedCaseInsensitiveContains(search) }
        }
        return result
    }

    // MARK: - Coalescing

    private func trim() {
        if buffer.count > capacity {
            let overflow = buffer.count - capacity
            buffer.removeFirst(overflow)
            droppedCount += overflow
            dirty = true
        }
    }

    /// Publish now if the interval has elapsed, otherwise mark dirty.
    ///
    /// The interval check is what keeps a burst to one update: without it, appending 2,000 events in a
    /// second would publish 2,000 times and the view would spend all its time invalidating.
    private func schedulePublish() {
        let now = Date()
        if now.timeIntervalSince(lastPublish) >= publishInterval {
            publish()
        } else {
            dirty = true
            let delay = publishInterval - now.timeIntervalSince(lastPublish)
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                self?.flush()
            }
        }
    }

    private func publish() {
        lines = buffer
        dirty = false
        lastPublish = Date()
        revision += 1
    }

    // MARK: - Introspection

    /// Buffer state, for the resources panel.
    public var stats: [String: Int] {
        [
            "lines": lines.count,
            "capacity": capacity,
            "dropped": droppedCount,
            "last_seq": lastSeq,
        ]
    }

    /// Whether anything has been dropped, so the UI can show a badge.
    public var hasDropped: Bool { droppedCount > 0 }
}
