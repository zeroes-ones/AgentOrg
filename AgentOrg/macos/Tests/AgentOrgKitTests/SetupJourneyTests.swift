//
//  SetupJourneyTests.swift
//  AgentOrgKitTests
//
//  The whole first-run path: is it complete, is it ordered, and does it agree with the gate?
//
//  WHY THIS FILE EXISTS
//  --------------------
//  The HIG audit failed the onboarding on four rules, and two of them are properties of the *model*
//  rather than of a view:
//
//  - "Whole journey visible" — a rail can only show every step if there is a model of every step.
//  - "Explains how it works" — each step has to carry what it is for and what it unlocks.
//
//  A view that draws a rail is not testable; the model behind it is, and the failure this file guards
//  against is the one the audit actually found: a checklist that tells a person a step is done while
//  the wizard still blocks on it. That is why the ticks are derived from `SetupGate` and asserted here
//  rather than computed in the view.
//
//  `SetupSection` is asserted too: the wizard and the Setup destination cover the same ground, and the
//  audit found they had drifted. Every journey step must have a section, and that is a test rather than
//  a comment because it is exactly the kind of agreement that rots silently.
//

import XCTest
@testable import AgentOrgKit

final class SetupJourneyTests: XCTestCase {

    private let readyDefaults: [String: JSONValue] = [
        "provider": .string("olla"), "model": .string("deepseek-v4.1-flash"),
        "context_window": .int(131072), "window_source": .string("catalog"),
    ]

    private func gate(engineUp: Bool = true, providers: [[String: JSONValue]]? = nil,
                      projectConfirmed: Bool = false, postureChosen: Bool = false,
                      defaults: [String: JSONValue]? = nil) -> SetupGate {
        SetupReadiness.gate(
            engineIsRunning: engineUp,
            defaults: defaults ?? readyDefaults,
            providers: providers ?? [["id": .string("olla")]],
            projectConfirmed: projectConfirmed,
            postureChosen: postureChosen)
    }

    // MARK: - The path is complete

    func testThePathNamesEveryPreconditionAndEachStepExplainsItself() {
        // The audit's two model-level findings. A step with no purpose is a step a person cannot weigh,
        // and a step with no unlock is a step with no reason to do it — which is what "only the current
        // step is shown" reduced the whole screen to.
        let steps = SetupJourney.steps(for: gate())
        XCTAssertGreaterThanOrEqual(steps.count, 4, "the path must be complete, not just the next step")
        for step in steps {
            XCTAssertFalse(step.id.isEmpty, "a step with no id cannot be matched by the rail")
            XCTAssertFalse(step.title.isEmpty, "\(step.id) has no title")
            XCTAssertFalse(step.purpose.isEmpty, "\(step.id) does not say what it is for")
            XCTAssertFalse(step.unlocks.isEmpty, "\(step.id) does not say what finishing it unlocks")
            XCTAssertFalse(step.symbol.isEmpty, "\(step.id) has no glyph, so it is invisible without colour")
        }
    }

    func testEveryStepHasASectionInTheSetupDestination() {
        // The two surfaces must cover the same ground. This is the drift the audit found between the
        // wizard and the destination, asserted rather than commented.
        let resolvable = Set(SetupSection.allCases.compactMap(\.resolvesStep))
        for step in SetupJourney.steps(for: gate()) {
            // The engine step has no section because nothing in this window can start the engine from a
            // form; its control is the wizard's own Start button. Every other step must have one.
            if step.id == "engine" { continue }
            XCTAssertTrue(resolvable.contains(step.id),
                          "journey step \(step.id) has no SetupSection, so the destination cannot "
                          + "answer it")
        }
    }

    func testStepNumbersAreOneBasedAndContiguous() {
        let steps = SetupJourney.steps(for: gate())
        XCTAssertEqual(steps.map(\.number), Array(1...steps.count))
    }

    // MARK: - The ticks agree with the gate

    func testNothingIsTickedWhileTheEngineIsDown() {
        // Not even the engine step: the question after it cannot be asked until it is answered, and a
        // rail that showed "engine: done" beside a stopped engine would be the checklist contradicting
        // the screen.
        let steps = SetupJourney.steps(for: gate(engineUp: false))
        XCTAssertEqual(SetupJourney.completedCount(for: gate(engineUp: false)), 0)
        XCTAssertTrue(steps.allSatisfy { !$0.satisfied })
    }

    func testTheTicksFollowTheGatesOwnOrder() {
        // Each row is compared against the same gate the wizard obeys. With the gate on the autonomy
        // step, the three before it are done and the autonomy one is not.
        let settled = gate(projectConfirmed: true, postureChosen: false)
        let satisfied = SetupJourney.steps(for: settled).filter(\.satisfied).map(\.id)
        XCTAssertEqual(satisfied, ["engine", "model", "project"])
        XCTAssertEqual(SetupJourney.currentId(for: settled), "autonomy")
    }

    func testAReadyGateTicksEverything() {
        let done = gate(projectConfirmed: true, postureChosen: true)
        XCTAssertEqual(done, .ready)
        let steps = SetupJourney.steps(for: done)
        XCTAssertTrue(steps.allSatisfy(\.satisfied), "a ready gate must leave no step looking unfinished")
        XCTAssertEqual(SetupJourney.completedCount(for: done), steps.count)
        XCTAssertNil(SetupJourney.currentId(for: done))
    }

    func testTheGateAndTheJourneyAgreeByIdNotByPosition() {
        // The gate names its step, and the rail looks the name up — so a step inserted into the middle
        // of the path moves every number without any view needing to change. This asserts the lookup
        // rather than the numbers, which is the property that survives a reorder.
        let gate = gate(providers: [], defaults: [:])
        guard case .needsModel = gate else { return XCTFail("expected needsModel, got \(gate)") }
        XCTAssertEqual(gate.stepId, "model")
        XCTAssertTrue(SetupJourney.order.contains("model"))
        XCTAssertFalse(gate.isPast(id: "model"), "the step in hand is not behind the person")
        XCTAssertTrue(gate.isPast(id: "engine"), "the step before it is")
    }

    func testAnUnknownStepIdIsTreatedAsNotDoneRatherThanDone() {
        // The safe direction. A step id this build does not know cannot be proven finished, and a tick
        // shown for it would be the checklist claiming something it cannot check.
        XCTAssertFalse(gate().isPast(id: "a-step-from-a-newer-engine"))
    }

    // MARK: - The engine's own report

    func testAnEngineReportWithNoJourneyFieldIsNilRatherThanEmpty() {
        // "The engine has no opinion" and "the engine says there are no steps" are different facts, and
        // only the first should fall back to this app's own copy. An empty report that was accepted
        // would render a first-run screen with no steps at all.
        XCTAssertNil(SetupJourneyReport(status: ["phase": .string("idle")]))
        XCTAssertNil(SetupJourneyReport(status: ["journey": .array([])]))
        XCTAssertNil(SetupJourneyReport(status: ["journey": .string("nonsense")]))
    }

    func testTheEngineReportDecodesTheDocumentedShape() throws {
        // The contract `engine/onboarding.py::journey()` is being built to: every step with its title,
        // purpose, whether it is satisfied, the one command that resolves it, and what it unlocks.
        let status: [String: JSONValue] = [
            "journey": .array([
                .object([
                    "id": .string("model"),
                    "title": .string("A model answers"),
                    "purpose": .string("the endpoint and the model everyone runs on"),
                    "unlocks": .string("hiring agents"),
                    "satisfied": .bool(true),
                    "resolves": .string("engine.cli defaults set --provider P --model M"),
                ]),
            ]),
        ]
        let report = try XCTUnwrap(SetupJourneyReport(status: status))
        XCTAssertEqual(report.steps.count, 1)
        XCTAssertEqual(report.steps.first?.id, "model")
        XCTAssertEqual(report.steps.first?.satisfied, true)
        XCTAssertEqual(report.steps.first?.resolves,
                       "engine.cli defaults set --provider P --model M")
    }

    func testAStepWithNoIdIsDroppedRatherThanMisPlaced() {
        // An id is how the rail matches a row to the gate's answer. A step without one cannot be placed,
        // and placing it by position would silently tick the wrong row.
        let status: [String: JSONValue] = [
            "journey": .object([
                "steps": .array([
                    .object(["title": .string("no id here")]),
                    .object(["id": .string("model"), "title": .string("A model answers")]),
                ]),
                "summary": .string("four steps to a run"),
            ]),
        ]
        let report = SetupJourneyReport(status: status)
        XCTAssertEqual(report?.steps.map(\.id), ["model"])
        XCTAssertEqual(report?.summary, "four steps to a run")
    }

    func testTheEnginesWordsAreUsedButTheLiveGateStillDecidesTheTicks() throws {
        // The reconciliation that matters: titles come from the engine (so the terminal and this window
        // say one thing), and *satisfied* comes from the live gate. An engine report claiming a step is
        // done must not tick a row the wizard is still blocking on.
        let status: [String: JSONValue] = [
            "journey": .array([
                .object([
                    "id": .string("model"),
                    "title": .string("Which model everyone runs on"),
                    "purpose": .string("engine words"),
                    "unlocks": .string("engine words"),
                    "satisfied": .bool(true),
                ]),
            ]),
        ]
        let report = SetupJourneyReport(status: status)
        let steps = SetupJourney.steps(for: gate(providers: [], defaults: [:]), report: report)
        let model = try XCTUnwrap(steps.first { $0.id == "model" })
        XCTAssertEqual(model.title, "Which model everyone runs on")
        XCTAssertEqual(model.number, 1)
        // The gate is blocked on the model step, so the row is NOT ticked, whatever the report said.
        XCTAssertFalse(model.satisfied)
    }

    func testAStepTheEngineKnowsAndThisBuildDoesNotIsStillShown() throws {
        // A newer engine's extra precondition must be visible rather than invisible. It gets no local
        // words and no glyph, but it gets a row — because a step silently dropped is a step the person
        // is blocked by with nothing on screen explaining it.
        let status: [String: JSONValue] = [
            "journey": .array([
                .object(["id": .string("billing"), "title": .string("Billing is set up"),
                         "satisfied": .bool(false)]),
            ]),
        ]
        let report = SetupJourneyReport(status: status)
        let steps = SetupJourney.steps(for: gate(), report: report)
        let billing = try XCTUnwrap(steps.first { $0.id == "billing" })
        XCTAssertEqual(billing.title, "Billing is set up")
        XCTAssertFalse(billing.satisfied)
        XCTAssertFalse(billing.symbol.isEmpty, "a row with no glyph is invisible without colour")
    }

    // MARK: - The sections

    func testEverySectionHasMetadataForItsRow() {
        for section in SetupSection.allCases {
            XCTAssertFalse(section.symbol.isEmpty, "\(section.rawValue) has no glyph")
            XCTAssertFalse(section.summary.isEmpty, "\(section.rawValue) does not say what it is for")
        }
    }

    func testTheStepSectionsCannotBeCollapsedAway() {
        // Hiding the control a person was just told to use is the failure the wizard exists to avoid —
        // so the step sections are always open, and only the reading sections may collapse.
        for section in SetupSection.allCases where section.resolvesStep != nil {
            XCTAssertTrue(section.isAlwaysOpen, "\(section.rawValue) must not be collapsible")
        }
    }

    func testTheReadingSectionsAskNothingAndSoAreNotJourneySteps() {
        // "What actually happens", the skills list and the engine's paths are not preconditions. If one
        // of them were a journey step, the wizard's progress would count a screen that asks nothing.
        let journeyIds = Set(SetupJourney.order)
        for section in SetupSection.allCases where section.resolvesStep == nil {
            XCTAssertFalse(journeyIds.contains(section.rawValue.lowercased()))
        }
        XCTAssertEqual(SetupSection.howItWorks.resolvesStep, nil)
        XCTAssertEqual(SetupSection.journey.resolvesStep, nil)
    }
}
