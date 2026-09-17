//
//  ProtocolContractTests.swift
//  AgentOrgKitTests
//
//  The cross-language contract test.
//
//  WHY THIS IS THE MOST IMPORTANT SWIFT TEST
//  -----------------------------------------
//  `ProtocolModels.swift` is a second implementation of `engine/protocol.py`. Two implementations of one
//  contract drift, and the drift is silent: a renamed field decodes to `nil`, the UI shows a blank where
//  a value should be, and nothing fails.
//
//  The fixture is not hand-written for Swift. It is recorded by running the **real Python engine** and
//  dumping its trace — so a field renamed on the Python side fails this test on the next build, which is
//  the only arrangement that keeps the two halves honest.
//
//  Regenerate it with:
//
//      python3 macos/Tests/AgentOrgKitTests/Fixtures/record-engine-trace.py
//
//  The test also asserts coverage: every event type in the fixture must be one this build claims to
//  know, so adding an engine event without teaching the app about it fails here rather than in the UI.

import XCTest
@testable import AgentOrgKit

final class ProtocolContractTests: XCTestCase {

    /// The engine's own trace, recorded from Python.
    private func fixtureData() throws -> Data {
        // `#filePath` rather than a bundle resource: a package test can read the source tree directly,
        // and the fixture stays visible in the repository rather than hidden inside a bundle.
        let here = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        let fixture = here.appendingPathComponent("Fixtures/engine-trace.jsonl")
        return try Data(contentsOf: fixture)
    }

    private func fixtureEvents() throws -> [EngineEvent] {
        let decoder = LineDecoder()
        let frames = decoder.append(try fixtureData())
        return frames.compactMap { frame in
            if case .event(let event) = frame { return event }
            return nil
        }
    }

    // MARK: - Decoding

    func testDecodesEveryEventTheRealEngineRecorded() throws {
        let events = try fixtureEvents()
        XCTAssertGreaterThan(events.count, 15, "the fixture should cover a representative run")

        let unparsable = LineDecoder().append(try fixtureData()).compactMap { frame -> String? in
            if case .unparsable(let text) = frame { return text }
            return nil
        }
        XCTAssertTrue(unparsable.isEmpty,
                      "every recorded engine event must decode: \(unparsable.prefix(3))")
    }

    func testSequenceNumbersAreMonotonic() throws {
        // The bus assigns these, so a gap or a repeat means the stream was misread.
        let sequences = try fixtureEvents().map(\.seq)
        XCTAssertEqual(sequences, sequences.sorted(), "sequence numbers must be ordered")
        XCTAssertEqual(Set(sequences).count, sequences.count, "sequence numbers must be unique")
    }

    func testEveryRecordedTypeIsKnownToThisBuild() throws {
        // Adding an engine event without teaching the app about it must fail here, not in the UI.
        let unknown = Set(try fixtureEvents().map(\.type).filter { !EventType.isKnown($0) })
        XCTAssertTrue(unknown.isEmpty,
                      "the engine emitted types this build does not know: \(unknown.sorted())")
    }

    func testEveryEventCarriesAProtocolVersion() throws {
        for event in try fixtureEvents() {
            XCTAssertEqual(event.v, Protocol.version,
                           "\(event.type) declared protocol v\(event.v)")
        }
    }

    func testCorrelationFieldsSurviveTheCrossing() throws {
        let events = try fixtureEvents()
        let request = try XCTUnwrap(events.first { $0.type == "llm.request" })
        XCTAssertEqual(request.agentId, "ag_7f3a")
        XCTAssertEqual(request.nodeId, "developer")
        XCTAssertEqual(request.sessionId, "ses_001")
        XCTAssertEqual(request.runId, "run_fixture")
    }

    // MARK: - The load-bearing payloads

    func testTheGatePayloadCarriesWhatTheOwnerNeedsToDecide() throws {
        // The gate is where the run stops for a person, so its fields are the ones a decision depends on.
        let gate = try XCTUnwrap(try fixtureEvents().first { $0.type == "human.gate" })
        XCTAssertEqual(gate.payload["gate_id"]?.stringValue, "release")
        XCTAssertEqual(gate.payload["kind"]?.stringValue, "human")
        XCTAssertNotNil(gate.payload["reason"]?.stringValue)
        XCTAssertEqual(gate.payload["requires"]?.arrayValue?.count, 1)
        XCTAssertEqual(gate.payload["present"]?.arrayValue?.count, 1)
        XCTAssertEqual(gate.payload["missing"]?.arrayValue?.count, 0)
    }

    func testTheReviewRejectionCarriesFindingsWithFileAndLine() throws {
        // The developer's rework prompt is built from exactly these fields.
        let rejection = try XCTUnwrap(try fixtureEvents().first { $0.type == "review.rejected" })
        XCTAssertEqual(rejection.payload["attempt"]?.intValue, 1)
        let finding = try XCTUnwrap(rejection.payload["findings"]?.arrayValue?.first)
        XCTAssertEqual(finding["severity"]?.stringValue, "Critical")
        XCTAssertEqual(finding["file"]?.stringValue, "src/app.py")
        XCTAssertEqual(finding["line"]?.intValue, 47)
        XCTAssertNotNil(finding["fix"]?.stringValue)
        XCTAssertNotNil(finding["owasp"]?.stringValue)
    }

    func testUsageDecodesWithTheHonestyFlags() throws {
        // An unmeasured figure must stay distinguishable from a free one.
        let response = try XCTUnwrap(try fixtureEvents().first { $0.type == "llm.response" })
        let usage = try XCTUnwrap(usagePayload(response))
        XCTAssertEqual(usage["prompt_tokens"]?.intValue, 4100)
        XCTAssertEqual(usage["completion_tokens"]?.intValue, 260)
        XCTAssertEqual(usage["measured"]?.boolValue, true)
        // A local model is a known zero rather than a missing figure.
        let cost = try XCTUnwrap(response.payload["cost"]?.objectValue)
        XCTAssertEqual(cost["source"]?.stringValue, "free")
        XCTAssertEqual(cost["known"]?.boolValue, true)
        XCTAssertEqual(cost["usd"]?.doubleValue ?? -1, 0.0, accuracy: 1e-9)
    }

    func testCommandAckCorrelatesByCmdId() throws {
        // Without this field the app could not tell its own reply from any other event.
        let ack = try XCTUnwrap(try fixtureEvents().first { $0.type == "command.ack" })
        XCTAssertEqual(ack.payload["cmd_id"]?.stringValue, "cmd_0000000000001_ab12")
        XCTAssertEqual(ack.payload["ok"]?.boolValue, true)
    }

    func testErrorPayloadDrivesRetryPolicy() throws {
        let error = try XCTUnwrap(try fixtureEvents().first { $0.type == "error" })
        XCTAssertEqual(error.payload["kind"]?.stringValue, "rate_limit")
        XCTAssertEqual(error.payload["retryable"]?.boolValue, true)
        XCTAssertEqual(error.payload["status"]?.intValue, 429)
    }

    func testSessionSaturationCarriesTheBand() throws {
        let saturation = try XCTUnwrap(
            try fixtureEvents().first { $0.type == "session.saturation" })
        XCTAssertEqual(saturation.payload["band"]?.stringValue, "warning")
        XCTAssertEqual(saturation.payload["saturation"]?.doubleValue ?? 0, 0.72, accuracy: 1e-9)
    }

    func testChecklistResultCarriesEveryItemWithItsEvidence() throws {
        let checklist = try XCTUnwrap(try fixtureEvents().first { $0.type == "checklist.result" })
        let items = try XCTUnwrap(checklist.payload["items"]?.arrayValue)
        XCTAssertEqual(items.count, 2)
        XCTAssertEqual(items[0]["id"]?.stringValue, "CR1")
        XCTAssertEqual(items[0]["status"]?.stringValue, "PASS")
        XCTAssertEqual(items[1]["status"]?.stringValue, "FAIL")
        XCTAssertNotNil(items[1]["evidence"]?.stringValue)
    }

    func testRouteDecisionExplainsItselfForTheTraceView() throws {
        let route = try XCTUnwrap(try fixtureEvents().first { $0.type == "route.decided" })
        XCTAssertEqual(route.payload["decided_by"]?.stringValue, "org")
        XCTAssertEqual(route.payload["proposed"]?.boolValue, false)
        XCTAssertEqual(route.payload["chosen"]?.stringValue, "ag_9c1d")
        let candidate = try XCTUnwrap(route.payload["candidates"]?.arrayValue?.first)
        XCTAssertEqual(candidate["agent_id"]?.stringValue, "ag_9c1d")
        XCTAssertNotNil(candidate["score"]?.doubleValue)
    }

    func testTheSpawnRequestCarriesItsJustification() throws {
        // An Owner gate on a hire is only decidable if the requisition travels with it.
        let request = try XCTUnwrap(
            try fixtureEvents().first { $0.type == "agent.spawn.requested" })
        XCTAssertEqual(request.payload["tier"]?.stringValue, "T2")
        let requisition = try XCTUnwrap(request.payload["requisition"]?.objectValue)
        XCTAssertEqual(requisition["kind"]?.stringValue, "specialist")
        XCTAssertNotNil(requisition["expected_outcome"]?.stringValue)
    }

    func testRunEndRollsUpTheRun() throws {
        let end = try XCTUnwrap(try fixtureEvents().first { $0.type == "run.end" })
        XCTAssertEqual(end.payload["outcome"]?.stringValue, "complete")
        XCTAssertEqual(end.payload["iterations"]?["review-fix-loop"]?.intValue, 2)
    }

    // MARK: - Rendering

    func testEveryRecordedEventRendersASummaryForTheTerminal() throws {
        // A terminal line reading only the raw type would be unreadable; every type needs a summary.
        for event in try fixtureEvents() {
            let summary = event.summary
            XCTAssertFalse(summary.isEmpty, "\(event.type) produced an empty summary")
            XCTAssertNotEqual(summary, event.type,
                              "\(event.type) falls through to the raw type — add a case")
        }
    }

    // MARK: - The goal and subagent events

    func testTheGoalArmedEventCarriesTheObjective() throws {
        let event = try firstEvent("goal.armed")
        XCTAssertEqual(event.payload["objective"]?.stringValue, "add cursor pagination")
        XCTAssertEqual(event.payload["token_budget"]?.intValue, 0)
    }

    func testTheGoalCompletionCarriesTheSummary() throws {
        // Completion is the agent's own verdict, and the summary is what makes it auditable.
        let event = try firstEvent("goal.completed")
        XCTAssertEqual(event.payload["summary"]?.stringValue,
                       "cursor pagination added, 12 tests pass")
        XCTAssertEqual(event.payload["spend"]?.objectValue?["rounds"]?.intValue, 2)
    }

    func testTheGoalPauseNamesItsReason() throws {
        // A pause is only actionable if it says why: manual, gate, budget_spend or restored.
        let event = try firstEvent("goal.paused")
        XCTAssertEqual(event.payload["reason"]?.stringValue, "budget_spend")
    }

    func testTheSubagentReferenceCarriesWhatAParentNeeds() throws {
        let event = try firstEvent("subagent.done")
        XCTAssertEqual(event.payload["child_id"]?.stringValue, "sub_001")
        XCTAssertEqual(event.payload["status"]?.stringValue, "done")
        XCTAssertEqual(event.payload["steps"]?.intValue, 3)
    }

    func testTheSubagentReadReportsTheByteRange() throws {
        // Truncation must be visible: a reader that assumed it saw everything is the failure this
        // reports against.
        let event = try firstEvent("subagent.read")
        XCTAssertEqual(event.payload["returned_bytes"]?.intValue, 8192)
        XCTAssertEqual(event.payload["total_bytes"]?.intValue, 2355)
    }

    func testTheSubagentFailureCarriesTheReason() throws {
        let event = try firstEvent("subagent.failed")
        XCTAssertEqual(event.payload["error"]?.stringValue, "provider exploded")
    }

    // MARK: - Helpers

    private func firstEvent(_ type: String) throws -> EngineEvent {
        let match = try fixtureEvents().first { $0.type == type }
        return try XCTUnwrap(match, "the fixture has no \(type) event; regenerate it")
    }

    private func usagePayload(_ event: EngineEvent) -> [String: JSONValue]? {
        event.payload["usage"]?.objectValue
    }
}
