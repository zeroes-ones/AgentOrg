//
//  EventSummaryTests.swift
//  AgentOrgKitTests
//
//  The terminal line each event type produces.
//
//  WHY THE SUMMARIES ARE TESTED AT ALL
//  -----------------------------------
//  The terminal is the only place an Owner can read *why* something happened without opening a panel,
//  and a summary that falls through to the raw type is a line that says "handoff.breached" to someone
//  who needs to know what crossed, to whom, and whether it worked. The contract test asserts every
//  recorded type renders something; these assert the *new* types render the right thing, including the
//  distinction the console's gate rule depends on.
//

import XCTest
@testable import AgentOrgKit

final class EventSummaryTests: XCTestCase {

    private func event(_ type: String, _ payload: [String: JSONValue] = [:]) -> EngineEvent {
        EngineEvent(v: 1, seq: 1, type: type, payload: payload,
                    runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil)
    }

    // MARK: - The gate

    func testAGateTheEngineRefusedReadsAsWaitingOnYou() {
        let line = event("human.gate", [
            "reason": .string("Owner release approval"),
            "waiting_on": .string("owner"),
        ]).summary
        XCTAssertTrue(line.contains("waiting on you"), line)
        XCTAssertTrue(line.contains("Owner release approval"), line)
    }

    func testAGateWithNoRefusalReadsAsReachedRatherThanWaiting() {
        // The distinction the console's auto-approve rule turns on, made visible in the terminal: a
        // gate the engine did *not* claim is not shown as waiting for a person, because the console may
        // be about to answer it.
        let line = event("human.gate", ["reason": .string("bounded reroute")]).summary
        XCTAssertTrue(line.contains("gate reached"), line)
        XCTAssertFalse(line.contains("waiting on you"), line)
    }

    func testAGoalDecidedGateDoesNotClaimYouDecidedIt() {
        // "you approved reroute-gate" for a decision the *engine* took would make the terminal lie
        // about who was present — the same distinction the engine's ledger records.
        let line = event("human.decision", [
            "gate_id": .string("reroute-gate"), "approved": .bool(true), "by": .string("goal"),
        ]).summary
        XCTAssertTrue(line.contains("the goal approved"), line)
        XCTAssertFalse(line.contains("you approved"), line)
    }

    func testAPersonsOwnDecisionStillReadsAsYours() {
        let line = event("human.decision", [
            "gate_id": .string("release"), "approved": .bool(true), "by": .string("owner"),
        ]).summary
        XCTAssertTrue(line.contains("you approved"), line)
    }

    // MARK: - The goal's own reasoning

    func testTheGoalGateReleaseShowsTheEngineWhy() {
        // `policy.changed` with `by: goal` is the engine reporting its gate reasoning, which is the
        // whole reason the app does not re-derive the policy: it can read the answer here.
        let line = event("policy.changed", [
            "gate_id": .string("human-gate"), "approved": .bool(true), "by": .string("goal"),
            "why": .string("the goal's posture is unattended and the gate's evidence is present"),
        ]).summary
        XCTAssertTrue(line.contains("passed"), line)
        XCTAssertTrue(line.contains("human-gate"), line)
        XCTAssertTrue(line.contains("evidence is present"), line)
    }

    func testAnOrdinaryPolicyChangeIsNotRenderedAsAGateDecision() {
        // The same event type carries an Owner instruction and an autonomy edit, so a summary that
        // always said "the goal passed gate" would mislabel most of them.
        let line = event("policy.changed", [
            "instruction": .string("prefer Postgres"),
            "as_constraint": .bool(false),
        ]).summary
        XCTAssertFalse(line.contains("gate"), line)
        XCTAssertTrue(line.contains("prefer Postgres"), line)
    }

    // MARK: - The typed handoff

    func testEveryHandoffStateNamesItsEndpoints() {
        // The four keys the flow board keys on are the four the terminal needs: what crossed, and
        // between whom. A state that rendered only its own name would be unreadable in a log.
        for state in ["proposed", "accepted", "fulfilled", "verified", "rejected", "breached",
                      "escalated"] {
            let line = event("handoff.\(state)", [
                "handoff_id": .string("ho_1"),
                "from_node": .string("developer"),
                "to_node": .string("reviewer"),
                "summary": .string("cursor pagination implemented"),
            ]).summary
            XCTAssertTrue(line.contains("developer"), "\(state): \(line)")
            XCTAssertTrue(line.contains("reviewer"), "\(state): \(line)")
            XCTAssertTrue(line.contains(state), "\(state): \(line)")
            XCTAssertTrue(line.contains("cursor pagination"), "\(state): \(line)")
        }
    }

    func testTheOlderHandoffShapeStillRenders() {
        // The engine has emitted `handoff.verified` with `from`/`to` as well as `from_node`/`to_node`.
        // Reading both is what stops half the crossings on the wire rendering as "? → ?".
        let line = event("handoff.verified", [
            "from": .string("developer"), "to": .string("reviewer"),
        ]).summary
        XCTAssertTrue(line.contains("developer"), line)
        XCTAssertTrue(line.contains("reviewer"), line)
        XCTAssertFalse(line.contains("?"), line)
    }

    func testAHandoffWithNoSummaryStillNamesItsEndpoints() {
        // A refused proposal may arrive before any summary exists; the edge is still the fact worth
        // reporting.
        let line = event("handoff.rejected", [
            "from_node": .string("developer"), "to_node": .string("qa"),
        ]).summary
        XCTAssertTrue(line.contains("developer → qa"), line)
        XCTAssertFalse(line.hasSuffix("— "), line)
    }

    // MARK: - The completion and block events

    func testTheGoalCompletionShowsItsSummary() {
        let line = event("goal.completed", [
            "summary": .string("cursor pagination added, 12 tests pass"),
        ]).summary
        XCTAssertTrue(line.contains("cursor pagination added"), line)
    }

    func testTheGoalBlockShowsItsReason() {
        let line = event("goal.blocked", ["reason": .string("no route to a provider")]).summary
        XCTAssertTrue(line.contains("blocked"), line)
        XCTAssertTrue(line.contains("no route to a provider"), line)
    }

    func testEveryGoalStateIsDistinguishable() {
        // The five goal outcomes read differently in the terminal, because a person scanning a log
        // needs to tell "done" from "stopped and needs me" without opening the Work panel.
        let lines = [
            event("goal.completed", ["summary": .string("s")]).summary,
            event("goal.blocked", ["reason": .string("r")]).summary,
            event("goal.paused", ["reason": .string("budget_spend")]).summary,
            event("goal.cleared").summary,
            event("goal.armed", ["objective": .string("o")]).summary,
        ]
        XCTAssertEqual(Set(lines).count, lines.count, "two goal states read identically: \(lines)")
    }
}
