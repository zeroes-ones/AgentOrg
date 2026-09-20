//
//  GateDispositionTests.swift
//  AgentOrgKitTests
//
//  The safety rule: which gates may the console answer itself?
//
//  WHY THIS FILE IS THE POINT OF THE FEATURE
//  -----------------------------------------
//  "Auto-resume on a gate" is the difference between a console you click and a console you supervise,
//  and the failure it can cause is the one failure this product must not have: **the app deciding
//  something on a person's behalf that the person did not authorise**.
//
//  So the rule is a function — `OrgController.gateDisposition` — rather than a scattering of `if`s in
//  the event handler, and every branch of it is asserted here. The branches that matter most are the
//  *negative* ones: a supervised goal, a gate the engine claimed, and a posture this build cannot read
//  must all resolve to "this is yours", because the safe direction of an unknown is to do nothing.
//

import XCTest
@testable import AgentOrgKit

@MainActor
final class GateDispositionTests: XCTestCase {

    // MARK: - The posture gate

    func testASupervisedGoalNeverForwardsAnything() {
        // The byte-for-byte behaviour of the app before this feature existed. A supervised goal must
        // park exactly as it always did, whatever the gate payload looks like.
        let gate: [String: JSONValue] = ["gate_id": .string("release"), "kind": .string("human")]
        let disposition = OrgController.gateDisposition(gate: gate, posture: .supervised)
        XCTAssertFalse(disposition.shouldForward)
        XCTAssertTrue(disposition.isWaitingForHuman)
        XCTAssertTrue(disposition.why.contains("supervised"), disposition.why)
    }

    func testAnUnknownPostureIsTreatedAsSupervisedRatherThanUnattended() {
        // The load-bearing default. A posture this build does not recognise must not be read as "the
        // goal may act" — the safe reading of an unknown authority is "I do not know".
        let gate: [String: JSONValue] = ["gate_id": .string("release")]
        let disposition = OrgController.gateDisposition(gate: gate, posture: .unknown)
        XCTAssertFalse(disposition.shouldForward)
        XCTAssertTrue(disposition.why.contains("does not know"), disposition.why)
    }

    // MARK: - The engine's own refusal outranks the posture

    func testAGateTheEngineSaidIsTheOwnersIsNeverForwarded() {
        // `waiting_on: owner` is the engine *refusing* to pass the gate, with its reason. It outranks
        // the posture: a goal may be unattended and still be told this particular gate is not its to
        // answer — a release with no evidence, a guardrail that fired, a node that ended blocked.
        let gate: [String: JSONValue] = [
            "gate_id": .string("release"),
            "kind": .string("human"),
            "waiting_on": .string("owner"),
            "why": .string("a safety control fired (guardrail); the goal may not release this"),
        ]
        let disposition = OrgController.gateDisposition(gate: gate, posture: .unattended)
        XCTAssertFalse(disposition.shouldForward, "the engine's refusal must outrank the posture")
        XCTAssertEqual(disposition.why, "a safety control fired (guardrail); the goal may not release this")
    }

    func testAGateTheEngineHandedToSomeoneElseIsNotForwarded() {
        // Forward compatibility: a future engine that names another holder must not have its gate
        // decided by this build. Anything other than us is not ours.
        let gate: [String: JSONValue] = ["gate_id": .string("x"), "waiting_on": .string("policy")]
        let disposition = OrgController.gateDisposition(gate: gate, posture: .unattended)
        XCTAssertFalse(disposition.shouldForward)
        XCTAssertTrue(disposition.why.contains("waiting on policy"), disposition.why)
    }

    // MARK: - The one case that forwards

    func testAnUnattendedGoalForwardsAGateTheEngineLeftUnanswered() {
        // The feature. The engine reached a gate (`executor._gate`) without claiming it and without
        // refusing it; the posture says the goal may answer; so the console supplies the click. The
        // engine still applies its own evidence and safety checks and will report `waiting_on: owner`
        // if one fires — which is the next test's subject.
        let gate: [String: JSONValue] = [
            "gate_id": .string("reroute-gate"),
            "kind": .string("agent"),
            "reason": .string("the org gate wants to reroute"),
        ]
        XCTAssertTrue(OrgController.gateDisposition(gate: gate, posture: .unattended).shouldForward)
    }

    func testAGateTheEngineAlreadyAnsweredIsRecordedRatherThanForwarded() {
        // A decision the engine made itself. Forwarding it would send a second approval for a gate
        // that no longer exists; leaving `pendingGate` set would keep asking a person to confirm a
        // decision already recorded. So it is neither — it is recorded.
        let gate: [String: JSONValue] = ["gate_id": .string("reroute-gate")]
        let disposition = OrgController.gateDisposition(gate: gate, posture: .unattended,
                                                       decidedByGoal: true)
        XCTAssertFalse(disposition.shouldForward)
        XCTAssertFalse(disposition.isWaitingForHuman)
        if case .answeredByGoal = disposition {} else {
            XCTFail("a goal-decided gate must be reported as answered, got \(disposition)")
        }
    }

    func testNoGateMeansNothingIsForwarded() {
        for posture in OrgController.Posture.allCases {
            let disposition = OrgController.gateDisposition(gate: nil, posture: posture)
            XCTAssertFalse(disposition.shouldForward)
            XCTAssertTrue(disposition.isWaitingForHuman)
        }
    }

    // MARK: - The rule as the controller applies it

    private func makeController(root: URL) -> OrgController {
        OrgController(settings: OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil),
            preferences: .ephemeral())
    }

    func testTheControllerDerivesItsDispositionFromTheGoalItHas() {
        // The rule reached through the controller, which is what the event handler calls: the goal's
        // posture is read from the status snapshot, not from a local copy, so the picker and the
        // engine's policy cannot disagree.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        controller.applyStatus([
            "goal": .object(["posture": .string("unattended"), "objective": .string("ship it")]),
        ])
        XCTAssertEqual(controller.goalPosture, .unattended)
        // `decides_gates` is absent from this snapshot, so the derived disposition still says the gate
        // is not ours to forward until a gate actually arrives — there is nothing to decide yet.
        XCTAssertFalse(controller.gateDisposition.shouldForward)
        // With no gate yet, there is nothing waiting on anyone.
        XCTAssertFalse(controller.gateIsWaitingForHuman)
    }

    func testASupervisedGoalFromTheSnapshotBlocksForwarding() {
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        controller.applyStatus([
            "goal": .object(["posture": .string("supervised"), "objective": .string("ship it")]),
            "gate": .object([
                "gate_id": .string("release"),
                "kind": .string("human"),
                "reason": .string("Owner release approval"),
            ]),
        ])
        XCTAssertEqual(controller.goalPosture, .supervised)
        XCTAssertTrue(controller.gateIsWaitingForHuman)
        XCTAssertFalse(controller.gateDisposition.shouldForward)
    }

    // MARK: - Reading the posture

    func testTheLegacyFlagIsHonouredWhenTheEngineHasNoPostureField() {
        // An engine from before the posture existed sends only `policy.human_gate`. Rendering that as
        // "unknown" would regress a working control on a perfectly valid engine, so the legacy flag is
        // the fallback — and `human_gate: true` meant supervised.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        controller.applyStatus([
            "goal": .object([
                "objective": .string("legacy"),
                "policy": .object(["human_gate": .bool(true)]),
            ]),
        ])
        XCTAssertEqual(controller.goalPosture, .supervised)

        controller.applyStatus([
            "goal": .object([
                "objective": .string("legacy"),
                "policy": .object(["human_gate": .bool(false)]),
            ]),
        ])
        XCTAssertEqual(controller.goalPosture, .unattended)
    }

    func testAnUnreadablePostureIsReportedAsUnknownRatherThanGuessed() {
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        controller.applyStatus([
            "goal": .object(["posture": .string("cowboy"), "objective": .string("from the future")]),
        ])
        XCTAssertEqual(controller.goalPosture, .unknown)
        XCTAssertNil(controller.goalPosture.wireValue,
                     "an unknown posture must not be sendable back to the engine")
    }

    func testThePickerOffersOnlyTheTwoRealPostures() {
        // `unknown` is a rendering state, not a choice — offering it would let a person pick a value
        // the engine refuses.
        XCTAssertEqual(OrgController.Posture.choices, [.unattended, .supervised])
        XCTAssertFalse(OrgController.Posture.choices.contains(.unknown))
        for choice in OrgController.Posture.choices {
            XCTAssertNotNil(choice.wireValue)
            XCTAssertFalse(choice.label.isEmpty)
            XCTAssertFalse(choice.explanation.isEmpty)
        }
    }

    func testEveryPostureExplainsItselfAndTheCasesAreDistinct() {
        // A control whose options do not say what they do is the ambiguity the picker replaced.
        let labels = OrgController.Posture.allCases.map(\.label)
        XCTAssertEqual(Set(labels).count, labels.count, "duplicate label: \(labels)")
        let explanations = OrgController.Posture.allCases.map(\.explanation)
        XCTAssertEqual(Set(explanations).count, explanations.count, "duplicate explanation")
        XCTAssertTrue(OrgController.Posture.supervised.explanation.contains("waits for you"))
    }

    // MARK: - An engine-decided gate clears the question

    func testAGoalDecisionClearsThePendingGate() {
        // Otherwise the sidebar keeps a badge and the Approve button sits in front of a person for a
        // decision the engine has already taken and recorded.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        controller.handle(EngineEvent(
            v: 1, seq: 1, type: "human.gate",
            payload: ["gate_id": .string("reroute-gate"), "kind": .string("agent"),
                      "reason": .string("reroute")],
            runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))
        XCTAssertNotNil(controller.pendingGate)

        controller.handle(EngineEvent(
            v: 1, seq: 2, type: "policy.changed",
            payload: ["gate_id": .string("reroute-gate"), "approved": .bool(true),
                      "by": .string("goal"), "kind": .string("agent"),
                      "why": .string("the goal authorises the org to decide this gate")],
            runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))
        XCTAssertNil(controller.pendingGate, "a goal-decided gate is no longer a question")
    }

    func testAnOwnerInstructionDoesNotClearAGate() {
        // `policy.changed` is emitted for an Owner instruction and an autonomy edit too, so it must not
        // be read as a gate decision — clearing a live gate because someone typed a constraint would
        // hide the one thing waiting on them.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        controller.handle(EngineEvent(
            v: 1, seq: 1, type: "human.gate",
            payload: ["gate_id": .string("release"), "reason": .string("approve")],
            runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))

        controller.handle(EngineEvent(
            v: 1, seq: 2, type: "policy.changed",
            payload: ["instruction": .string("prefer Postgres"), "as_constraint": .bool(false)],
            runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))
        XCTAssertNotNil(controller.pendingGate, "an instruction is not a gate decision")
    }

    func testAPersonsDecisionClearsTheGate() {
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        controller.handle(EngineEvent(
            v: 1, seq: 1, type: "human.gate",
            payload: ["gate_id": .string("release"), "reason": .string("approve")],
            runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))
        controller.handle(EngineEvent(
            v: 1, seq: 2, type: "human.decision",
            payload: ["gate_id": .string("release"), "approved": .bool(true), "by": .string("owner")],
            runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))
        XCTAssertNil(controller.pendingGate)
    }
}
