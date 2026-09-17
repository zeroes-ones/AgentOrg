//
//  LogStoreTests.swift
//  AgentOrgKitTests
//
//  The ring buffer and the coalescing, both of which exist to keep a long run's UI alive.
//

import XCTest
@testable import AgentOrgKit

@MainActor
final class LogStoreTests: XCTestCase {

    private func event(seq: Int, type: String = "agent.log", text: String = "line") -> EngineEvent {
        EngineEvent(v: 1, seq: seq, type: type, payload: ["text": .string(text)],
                    runId: "run_1", agentId: "ag_1", nodeId: "fixer", sessionId: nil,
                    phase: "BUILD", ts: "2026-01-01T00:00:00.000Z")
    }

    func testAppendsAndPublishesOnFlush() {
        let store = LogStore(capacity: 100)
        store.append(event(seq: 1))
        store.flush()
        XCTAssertEqual(store.lines.count, 1)
        XCTAssertEqual(store.lines.first?.seq, 1)
        XCTAssertEqual(store.lines.first?.kind, .event)
    }

    func testSummaryIsUsedForTheLineText() {
        let store = LogStore(capacity: 10)
        store.append(event(seq: 1, type: "run.start"))
        store.flush()
        XCTAssertEqual(store.lines.first?.text, "run started")
    }

    func testBoundsTheBufferAndCountsWhatItDropped() {
        // A terminal that silently forgot its oldest lines would make a run look like it started later
        // than it did, so the count is kept and published.
        let store = LogStore(capacity: 5)
        for seq in 1...20 { store.append(event(seq: seq)) }
        store.flush()
        XCTAssertEqual(store.lines.count, 5)
        XCTAssertEqual(store.droppedCount, 15)
        XCTAssertTrue(store.hasDropped)
        XCTAssertEqual(store.lines.first?.seq, 16, "the newest lines are kept")
    }

    func testTracksTheHighestSequence() {
        let store = LogStore(capacity: 10)
        for seq in [3, 1, 7, 2] { store.append(event(seq: seq)) }
        XCTAssertEqual(store.lastSeq, 7)
    }

    func testCoalescesABurstIntoFewPublishes() async {
        // The property that keeps the terminal usable: 500 appends in one tick must not be 500
        // invalidations. The interval is long here so the assertion is unambiguous.
        let store = LogStore(capacity: 1_000, publishInterval: 0.5)
        for seq in 1...500 { store.append(event(seq: seq)) }
        // Nothing published yet, because the burst arrived inside one interval.
        XCTAssertEqual(store.lines.count, 0, "a burst must not publish per line")
        store.flush()
        XCTAssertEqual(store.lines.count, 500, "one flush publishes the whole burst")
    }

    func testDistinguishesLineKinds() {
        let store = LogStore(capacity: 10)
        store.append(event(seq: 1))
        store.append(diagnostic: "a warning from the engine")
        store.append(unparsable: "{bad")
        store.append(notice: "the app is doing something")
        store.flush()
        XCTAssertEqual(store.lines.map(\.kind), [.event, .diagnostic, .unparsable, .notice])
    }

    func testMarksAnUnknownEventTypeAsUnknown() {
        // Visible as unknown rather than hidden: an unrecognised event means the contract drifted.
        let store = LogStore(capacity: 10)
        store.append(event(seq: 1, type: "future.event"))
        store.flush()
        XCTAssertFalse(store.lines.first?.isKnownEvent ?? true)
    }

    func testKeepsTheCorrelationFields() {
        let store = LogStore(capacity: 10)
        store.append(event(seq: 1))
        store.flush()
        let line = store.lines[0]
        XCTAssertEqual(line.agentId, "ag_1")
        XCTAssertEqual(line.nodeId, "fixer")
        XCTAssertEqual(line.phase, "BUILD")
    }

    func testFiltersByKindAgentNodeAndSearch() {
        let store = LogStore(capacity: 50)
        store.append(event(seq: 1))
        store.append(diagnostic: "engine diagnostic")
        store.append(event(seq: 2, text: "a finding about the auth path"))
        store.flush()

        XCTAssertEqual(store.filtered(kind: .diagnostic).count, 1)
        // Two of the four lines came from an agent; a diagnostic has no correlation fields.
        XCTAssertEqual(store.filtered(agentId: "ag_1").count, 2)
        XCTAssertEqual(store.filtered(nodeId: "fixer").count, 2)
        XCTAssertEqual(store.filtered(search: "auth").count, 1)
        XCTAssertEqual(store.filtered(nodeId: "other").count, 0)
    }

    func testClearEmptiesTheBufferButNotTheDropCount() {
        let store = LogStore(capacity: 3)
        for seq in 1...10 { store.append(event(seq: seq)) }
        store.flush()
        XCTAssertGreaterThan(store.droppedCount, 0)
        store.clear()
        XCTAssertTrue(store.lines.isEmpty)
        // What was dropped remains true even after a clear.
        XCTAssertGreaterThan(store.droppedCount, 0)
    }

    func testStatsReportTheBufferState() {
        let store = LogStore(capacity: 4)
        for seq in 1...10 { store.append(event(seq: seq)) }
        store.flush()
        let stats = store.stats
        XCTAssertEqual(stats["capacity"], 4)
        XCTAssertEqual(stats["lines"], 4)
        XCTAssertEqual(stats["dropped"], 6)
        XCTAssertEqual(stats["last_seq"], 10)
    }

    func testIdsAreUniqueSoTheListCanBeKeyed() {
        let store = LogStore(capacity: 100)
        for seq in 1...50 { store.append(event(seq: seq)) }
        store.flush()
        XCTAssertEqual(Set(store.lines.map(\.id)).count, store.lines.count)
    }
}
