//
//  ContextReadingTests.swift
//  AgentOrgKitTests
//
//  "How full are the agents' contexts?" — with a real number, or an honest "not recorded".
//
//  WHY THE OLD PANEL NEEDED REPLACING
//  ----------------------------------
//  The Context panel asked that question in its own subtitle and never answered it. It showed the
//  model's *window size* and a static 70/85/95 legend, and no current saturation anywhere — the figure
//  existed and the panel simply did not read it.
//
//  Where it exists is what these tests pin, because getting this wrong produces the two failure modes
//  the app's honesty rule forbids:
//
//  - **An unmeasured session must not read as empty.** The engine computes
//    `payload.budget.session_saturation` at each node boundary and writes it into the handoff; it is
//    `0.0` when it could not be computed, so a naive read would draw a healthy empty bar for a session
//    nobody measured. `hasContextReading` is the guard, and the test for it is the important one.
//  - **A node that crossed several boundaries must not appear several times** with progressively older
//    figures, which reads as a trend without saying it is one. Newest wins, one row per node.
//

import XCTest
@testable import AgentOrgKit

final class ContextReadingTests: XCTestCase {

    private func handoff(origin: String, target: String = "reviewer",
                         saturation: Double?, window: Int?,
                         createdAt: String = "2026-09-18T10:00:00Z",
                         id: String = UUID().uuidString) -> HandoffSummary {
        var budget: [String: JSONValue] = [:]
        if let saturation { budget["session_saturation"] = .double(saturation) }
        if let window { budget["context_window"] = .int(window) }
        return HandoffSummary(
            id: id, kind: "handoff", origin: origin, target: target, state: "FULFILLED",
            attempt: 1, createdAt: createdAt, status: "done", summary: "",
            artifacts: [], openQuestions: [], budget: budget)
    }

    // MARK: - The band thresholds mirror the engine's own ladder

    func testTheBandsAreTheEnginesOwnThresholds() {
        // `engine/context/session.py::Band` classifies at 70/85/95. The bar's colours mean what the
        // compactor does, and a legend that disagreed with the behaviour would teach the wrong thing.
        XCTAssertEqual(Band.forSaturation(0.0), .healthy)
        XCTAssertEqual(Band.forSaturation(0.69), .healthy)
        XCTAssertEqual(Band.forSaturation(0.70), .warning)
        XCTAssertEqual(Band.forSaturation(0.84), .warning)
        XCTAssertEqual(Band.forSaturation(0.85), .critical)
        XCTAssertEqual(Band.forSaturation(0.94), .critical)
        XCTAssertEqual(Band.forSaturation(0.95), .overflow)
        XCTAssertEqual(Band.forSaturation(1.0), .overflow)
    }

    func testTheSegmentsMatchTheBandBoundaries() {
        // The bar is drawn from the bands' own thresholds rather than hardcoded fractions, so the
        // picture cannot drift from the rule. This is that property, asserted.
        XCTAssertEqual(Band.healthy.lowerBound, 0.0)
        XCTAssertEqual(Band.warning.lowerBound, 0.70)
        XCTAssertEqual(Band.critical.lowerBound, 0.85)
        XCTAssertEqual(Band.overflow.lowerBound, 0.95)
    }

    func testEveryBandCarriesAWordAndASymbolNotJustAColour() {
        // Never colour alone: a band has a tone (which carries the symbol) and a sentence saying what
        // it means for the run.
        for band in Band.allCases {
            XCTAssertFalse(band.meaning.isEmpty, "\(band) does not say what it means")
            XCTAssertFalse(band.tone.symbol.isEmpty)
            XCTAssertFalse(band.tone.word.isEmpty)
        }
        let meanings = Band.allCases.map(\.meaning)
        XCTAssertEqual(Set(meanings).count, meanings.count, "duplicate meaning: \(meanings)")
    }

    // MARK: - Unmeasured is not zero

    func testAHandoffWithNoSaturationRecordedIsNotReadAsEmpty() {
        // The load-bearing case. `session_saturation` absent means "not recorded", and drawing it as
        // 0% would report a healthy session nobody measured — the same mistake as rendering an
        // unmeasured cost as free.
        let unmeasured = handoff(origin: "developer", saturation: nil, window: 32768)
        XCTAssertFalse(unmeasured.hasContextReading)
        XCTAssertNil(unmeasured.sessionSaturation)
        XCTAssertTrue(ContextReading.latestPerNode(from: [unmeasured]).isEmpty,
                      "an unmeasured session must not appear as a reading at all")
    }

    func testAReportedZeroSaturationIsAlsoTreatedAsNotRecorded() {
        // The engine's own `_agent_budget` writes `0.0` when the session could not be measured, so a
        // literal zero is indistinguishable from "no figure" — and the safe reading of that is
        // "not recorded" rather than "empty". A real session that is genuinely 0% full would have to
        // have taken no turns, which is not a session worth drawing.
        let zeroed = handoff(origin: "developer", saturation: 0.0, window: 32768)
        XCTAssertFalse(zeroed.hasContextReading)
    }

    func testAHandoffWithNoWindowIsNotAReadingEvenWithASaturation() {
        // A saturation with no window is a fraction of nothing: the panel could not say "43% of what".
        let windowless = handoff(origin: "developer", saturation: 0.43, window: nil)
        XCTAssertFalse(windowless.hasContextReading)
        XCTAssertTrue(ContextReading.latestPerNode(from: [windowless]).isEmpty)
    }

    // MARK: - One row per node, newest wins

    func testEachNodeAppearsOnceEvenAfterSeveralCrossings() {
        // A node that crossed several boundaries would otherwise appear several times with
        // progressively older figures — a list that looks like a trend without saying it is one.
        let older = handoff(origin: "developer", saturation: 0.40, window: 32768,
                            createdAt: "2026-09-18T10:00:00Z", id: "ho_1")
        let newer = handoff(origin: "developer", saturation: 0.88, window: 32768,
                            createdAt: "2026-09-18T11:00:00Z", id: "ho_2")
        let readings = ContextReading.latestPerNode(from: [newer, older])
        XCTAssertEqual(readings.count, 1)
        XCTAssertEqual(readings.first?.saturation ?? 0, 0.88, accuracy: 1e-9,
                       "the newest figure is the one that answers “how full is it now”")
    }

    func testNodesAreReportedSeparatelyAndInAStableOrder() {
        let readings = ContextReading.latestPerNode(from: [
            handoff(origin: "reviewer", saturation: 0.72, window: 200_000),
            handoff(origin: "developer", saturation: 0.31, window: 32_768),
        ])
        XCTAssertEqual(readings.map(\.node), ["developer", "reviewer"],
                       "sorted, so the list does not reshuffle between polls")
    }

    func testTheReadingNamesTheNodeAndTheNodeItHandedTo() {
        let reading = ContextReading.latestPerNode(from: [
            handoff(origin: "developer", target: "reviewer", saturation: 0.91, window: 32_768),
        ]).first
        XCTAssertEqual(reading?.node, "developer")
        XCTAssertEqual(reading?.handedTo, "reviewer")
        XCTAssertEqual(reading?.window, 32_768)
        XCTAssertEqual(reading?.band, .critical)
        XCTAssertEqual(reading?.label, "91%")
    }

    func testASaturationAboveOneIsClampedRatherThanDrawnPastTheBar() {
        // The engine's own `saturation` is bounded to 0...1, but a rounding or a future engine could
        // exceed it — and a bar drawn at 140% would run off its own frame.
        let reading = ContextReading.latestPerNode(from: [
            handoff(origin: "developer", saturation: 1.4, window: 32_768),
        ]).first
        XCTAssertEqual(reading?.saturation ?? 0, 1.0, accuracy: 1e-9)
        XCTAssertEqual(reading?.band, .overflow)
    }

    func testAHandoffWithNoOriginIsLabelledAsSuchRatherThanDropped() {
        // "unknown" is a row a person cannot match to anything, but dropping the record silently would
        // hide that a measurement exists at all — so it is kept and labelled, and the honest emptiness
        // is in `handedTo`.
        let readings = ContextReading.latestPerNode(from: [
            handoff(origin: "", target: "", saturation: 0.5, window: 1024),
        ])
        XCTAssertEqual(readings.count, 1)
        XCTAssertEqual(readings.first?.node, "unknown",
                       "a nameless record is labelled as nameless, not dropped silently")
        XCTAssertTrue(readings.first?.handedTo.isEmpty ?? false)
    }

    func testAMixedSetKeepsOnlyTheMeasuredNodes() {
        let readings = ContextReading.latestPerNode(from: [
            handoff(origin: "developer", saturation: 0.72, window: 32_768),
            handoff(origin: "reviewer", saturation: nil, window: 32_768),
            handoff(origin: "qa", saturation: 0.2, window: nil),
            handoff(origin: "pm", saturation: 0.30, window: 200_000),
        ])
        XCTAssertEqual(readings.map(\.node), ["developer", "pm"],
                       "the two measured nodes, and nothing invented for the other two")
    }

    func testNoHandoffsMeansNoReadingsRatherThanZeroOfThem() {
        // The panel says "nothing has been measured yet" for this, which is true and useful; a row of
        // empty bars would be the same information drawn as a failed measurement.
        XCTAssertTrue(ContextReading.latestPerNode(from: []).isEmpty)
    }
}
