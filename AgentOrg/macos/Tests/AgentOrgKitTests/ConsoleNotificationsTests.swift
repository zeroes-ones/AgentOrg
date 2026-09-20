//
//  ConsoleNotificationsTests.swift
//  AgentOrgKitTests
//
//  When the console is allowed to interrupt a person.
//
//  WHY THIS IS TESTED AS A PURE FUNCTION
//  -------------------------------------
//  `UNUserNotificationCenter` cannot be observed in a headless test: there is no banner to look at,
//  and a test process has no application bundle to ask. So the part that *can* be wrong in a way that
//  matters — whether an event is worth interrupting for, and which events are not — is a value the
//  planner returns, and it is asserted exhaustively here. The delivery half is exercised through the
//  `ConsoleNotifier` protocol with a recording fake, which is the same protocol the controller talks to.
//

import XCTest
@testable import AgentOrgKit

final class ConsoleNotificationsTests: XCTestCase {

    private func event(_ type: String, _ payload: [String: JSONValue] = [:]) -> EngineEvent {
        EngineEvent(v: Protocol.version, seq: 1, type: type, payload: payload,
                    runId: "run_1", agentId: nil, nodeId: nil, sessionId: nil,
                    phase: nil, ts: nil)
    }

    // MARK: - The events that always notify

    func testGoalCompletionNotifiesWithItsSummary() throws {
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("goal.completed", ["summary": .string("cursor pagination added")]),
            gateIsWaitingOnAHuman: false))
        XCTAssertEqual(plan.title, "Goal complete")
        XCTAssertEqual(plan.body, "cursor pagination added")
        // Inform, not interrupt: the work finished, so nothing is waiting on the person.
        XCTAssertEqual(plan.urgency, .inform)
    }

    func testGoalBlockedInterruptsWithTheReason() throws {
        // A block is the one state where work has stopped and only a person can move it.
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("goal.blocked", ["reason": .string("no route to a provider")]),
            gateIsWaitingOnAHuman: false))
        XCTAssertEqual(plan.title, "Goal blocked")
        XCTAssertEqual(plan.body, "no route to a provider")
        XCTAssertEqual(plan.urgency, .interrupt)
    }

    func testGoalPausedForBudgetSaysSo() throws {
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("goal.paused", ["reason": .string("budget_spend")]),
            gateIsWaitingOnAHuman: false))
        XCTAssertEqual(plan.title, "Goal paused: budget reached")
        XCTAssertEqual(plan.urgency, .inform)
    }

    func testARunThatEndedAtAGateIsReportedAsWaitingNotFinished() throws {
        // The most misleading message the app could send: "the run finished" for a run that parked and
        // needs a decision. The engine's own outcome says which it was, so it is read rather than
        // guessed.
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("run.end", ["outcome": .string("awaiting_human")]),
            gateIsWaitingOnAHuman: false))
        XCTAssertEqual(plan.title, "Run waiting on you")
        XCTAssertEqual(plan.urgency, .interrupt)
    }

    func testAnOrdinaryRunEndIsOnlyInformative() throws {
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("run.end", ["outcome": .string("complete")]),
            gateIsWaitingOnAHuman: false))
        XCTAssertEqual(plan.title, "Run finished")
        XCTAssertEqual(plan.body, "Outcome: complete.")
        XCTAssertEqual(plan.urgency, .inform)
    }

    // MARK: - The gate, and the one thing it must not do

    func testAGateWaitingOnAHumanInterruptsAndCarriesTheEngineWhy() throws {
        // The engine's own reason travels in `why`, and it is what makes the banner actionable: a
        // person reads *which* refusal fired, not just "a gate".
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("human.gate", [
                "gate_id": .string("release"),
                "reason": .string("Owner release approval"),
                "waiting_on": .string("owner"),
                "why": .string("a safety control fired (guardrail); the goal may not release this"),
            ]),
            gateIsWaitingOnAHuman: true))
        XCTAssertEqual(plan.urgency, .interrupt)
        XCTAssertTrue(plan.title.contains("safety control"), plan.title)
        XCTAssertTrue(plan.body.contains("guardrail"), plan.body)
    }

    func testAGateTheConsoleIsNotTreatingAsTheHumansDoesNotNotify() {
        // The load-bearing negative. When the console is forwarding the decision itself, a banner
        // asking a person to decide would be a lie — and it would be the exact noise that trains
        // someone to ignore the next one.
        XCTAssertNil(NotificationPlanner.plan(
            for: event("human.gate", [
                "gate_id": .string("reroute-gate"),
                "kind": .string("agent"),
                "reason": .string("bounded reroute"),
            ]),
            gateIsWaitingOnAHuman: false))
    }

    func testAGateWithNoEvidenceNamesThatRefusal() throws {
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("human.gate", [
                "reason": .string("release"),
                "waiting_on": .string("owner"),
                "why": .string("the gate's evidence is not present, so there is nothing for the goal to release"),
            ]),
            gateIsWaitingOnAHuman: true))
        XCTAssertTrue(plan.title.contains("no evidence"), plan.title)
    }

    // MARK: - The events that must stay quiet

    func testOrdinaryTrafficNeverNotifies() {
        // A run emits an event per node, per model call, per token batch. A notifier that fired on any
        // of these would make the app unusable within a minute of starting a run.
        for type in ["node.enter", "node.exit", "llm.request", "llm.response", "agent.log",
                     "goal.progress", "goal.armed", "goal.resumed", "session.saturation",
                     "artifact.written", "checklist.result", "route.decided", "run.start",
                     "command.ack", "engine.ready", "handoff.verified"] {
            XCTAssertNil(NotificationPlanner.plan(for: event(type), gateIsWaitingOnAHuman: true),
                         "\(type) must not interrupt anyone")
        }
    }

    func testAGatePauseDoesNotDoubleUpWithTheGateItself() {
        // `goal.paused(reason: gate)` and `human.gate` describe one stop. Two banners for one event is
        // the noise this planner exists to prevent, so the pause is de-escalated rather than dropped —
        // the person still sees that the loop stopped, without being asked twice.
        let plan = NotificationPlanner.plan(for: event("goal.paused", ["reason": .string("gate")]),
                                            gateIsWaitingOnAHuman: false)
        XCTAssertEqual(plan?.urgency, .inform)
        XCTAssertEqual(plan?.body, "Waiting at a gate.")
    }

    // MARK: - Identity and threading

    func testNotificationsAboutOneRunShareAThread() throws {
        // So a run's banners group in Notification Centre instead of scattering.
        let first = try XCTUnwrap(NotificationPlanner.plan(
            for: event("goal.completed", ["summary": .string("done")]), gateIsWaitingOnAHuman: false))
        let second = try XCTUnwrap(NotificationPlanner.plan(
            for: event("run.end", ["outcome": .string("complete")]), gateIsWaitingOnAHuman: false))
        XCTAssertEqual(first.thread, second.thread)
        XCTAssertTrue(first.thread.contains("run_1"))
    }

    func testTheIdentifierIsStablePerEventKindSoBannersReplaceRatherThanStack() throws {
        let first = try XCTUnwrap(NotificationPlanner.plan(
            for: event("human.gate", ["reason": .string("one")]), gateIsWaitingOnAHuman: true))
        let second = try XCTUnwrap(NotificationPlanner.plan(
            for: event("human.gate", ["reason": .string("two")]), gateIsWaitingOnAHuman: true))
        XCTAssertEqual(first.identifier, second.identifier,
                       "two gates must replace each other rather than stack up")
    }

    // MARK: - The delivery seam

    func testADeniedNotifierReceivesNothingAndReportsIt() async {
        let notifier = RecordingNotifier(isAuthorized: false, willGrant: false)
        let plan = NotificationPlan(identifier: "x", title: "t", body: "b",
                                    urgency: .interrupt, thread: "th")
        let delivered = await notifier.deliver(plan)
        XCTAssertFalse(delivered, "a denied notifier must report the failure rather than pretend")
        let authorized = await notifier.isAuthorized
        XCTAssertFalse(authorized)
    }

    func testAGrantedNotifierRecordsThePlan() async {
        let notifier = RecordingNotifier(isAuthorized: true)
        _ = await notifier.requestAuthorization()
        let plan = NotificationPlan(identifier: "gate", title: "Waiting on you", body: "why",
                                    urgency: .interrupt, thread: "th")
        let delivered = await notifier.deliver(plan)
        XCTAssertTrue(delivered)
        XCTAssertEqual(notifier.delivered.map(\.identifier), ["gate"])
    }
}
