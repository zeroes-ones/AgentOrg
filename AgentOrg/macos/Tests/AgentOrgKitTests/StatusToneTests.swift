//
//  StatusToneTests.swift
//  AgentOrgKitTests
//
//  The shared status vocabulary.
//
//  WHY THIS IS WORTH TESTING
//  -------------------------
//  The same four states appear in six views. Before this type existed each had its own switch, which is
//  how "failed" ends up orange in one panel and red in another — a status the app contradicts itself
//  about, and the kind of inconsistency nobody notices until it matters.
//
//  Two properties are load-bearing: the mapping must be **total** (an unknown status must not be guessed
//  at), and a tone must carry a **word and a symbol** so the state survives without colour, which is what
//  the accessibility skill requires and what a colour-blind or VoiceOver user depends on.

import XCTest
@testable import AgentOrgKit

final class StatusToneTests: XCTestCase {

    func testKnownStatusesMapToTheToneTheirWordsImply() {
        XCTAssertEqual(StatusTone.forStatus("done"), .ok)
        XCTAssertEqual(StatusTone.forStatus("pass"), .ok)
        XCTAssertEqual(StatusTone.forStatus("healthy"), .ok)
        XCTAssertEqual(StatusTone.forStatus("needs_review"), .attention)
        XCTAssertEqual(StatusTone.forStatus("blocked"), .attention)
        XCTAssertEqual(StatusTone.forStatus("failed"), .bad)
        XCTAssertEqual(StatusTone.forStatus("quarantined"), .bad)
        XCTAssertEqual(StatusTone.forStatus("running"), .active)
    }

    func testTheMappingIsCaseInsensitive() {
        // The engine emits lowercase, but a provider or a future field may not.
        XCTAssertEqual(StatusTone.forStatus("DONE"), .ok)
        XCTAssertEqual(StatusTone.forStatus("Failed"), .bad)
    }

    func testAnUnknownStatusIsNeutralRatherThanGuessed() {
        // Inventing a state for a value this build does not recognise is how a UI confidently shows the
        // wrong thing. Neutral is the honest answer, and it renders as a plain dot with no claim.
        XCTAssertEqual(StatusTone.forStatus("something_new"), .neutral)
        XCTAssertEqual(StatusTone.forStatus(""), .neutral)
    }

    func testEveryToneHasASymbolAndAWord() {
        // The state must be legible without colour: `.green`/`.red` are indistinguishable to a
        // colour-blind reader and invisible to VoiceOver, so the symbol and the word carry it.
        for tone in [StatusTone.ok, .attention, .bad, .neutral, .active] {
            XCTAssertFalse(tone.symbol.isEmpty, "\(tone) has no symbol")
            XCTAssertFalse(tone.word.isEmpty, "\(tone) has no word")
        }
    }

    func testTheSymbolsAreDistinct() {
        let symbols = [StatusTone.ok, .attention, .bad, .neutral, .active].map(\.symbol)
        XCTAssertEqual(Set(symbols).count, symbols.count, "two tones share a glyph: \(symbols)")
    }

    func testAStatusLabelRendersTheStatusWordItself() {
        // The label shows the engine's own word rather than the tone's generic one, so a reader sees
        // "needs_review" and not "attention" — the specific state is the useful part.
        let view = StatusLabel(status: "needs_review")
        XCTAssertEqual(view.status, "needs_review")
        XCTAssertEqual(view.tone, nil, "an override should be absent unless one is passed")

        let overridden = StatusLabel(status: "custom", tone: .bad)
        XCTAssertEqual(overridden.tone, .bad)
    }
}
