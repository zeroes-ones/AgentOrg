//
//  SpineTests.swift
//  AgentOrgKitTests
//
//  The spine: "you are here, this needs you, do this next".
//
//  WHY THIS IS THE HEART OF THE REWRITE'S TEST COVERAGE
//  ---------------------------------------------------
//  The audit's central finding was not that a panel was wrong — most were good — it was that **there
//  was no single place saying what needs a person**. A gate awaiting a decision was visible on one of
//  twelve sidebar rows and nowhere else, and three of those rows were different renderings of the same
//  live run.
//
//  The spine is now a value built by a pure function, so the three things it has to get right are all
//  asserted here without a window:
//
//  1. **The decision controls appear only where a decision is actually needed.** `GateLine.canAct`
//     comes from `GateDisposition`, the one rule that decides whether the console may forward a
//     decision — and the test that matters most is the negative: a gate the console is answering
//     itself, or one the engine already answered, must not render a button.
//  2. **"Next" is the engine's own answer, not a Swift-side guess.** It reads `activity.next_action`,
//     which the engine orders by urgency and which can see the ledger and the stop reason this app
//     cannot.
//  3. **A failure outranks everything.** The engine's state can read "idle" after a bootstrap failure,
//     which is the exact "nothing looks broken" trap the whole rewrite is about.
//

import XCTest
@testable import AgentOrgKit

@MainActor
final class SpineTests: XCTestCase {

    private func spine(_ input: SpineInput) -> SpineModel { SpineModel.make(input) }

    private func base(engineState: EngineState = .running) -> SpineInput {
        SpineInput(engineState: engineState,
                   workspace: ["name": .string("my-app"), "attached": .bool(true)],
                   projectPath: "/Users/me/code/my-app")
    }

    // MARK: - The three facts

    func testTheSpineNamesTheEngineTheProjectAndTheGoal() {
        var input = base()
        input.goal = ["objective": .string("harden auth"), "live": .bool(true), "state": .string("armed")]
        let model = spine(input)
        XCTAssertEqual(model.stateWord, "Working on a goal")
        XCTAssertEqual(model.stateTone, .ok)
        XCTAssertEqual(model.projectName, "my-app")
        XCTAssertEqual(model.goalObjective, "harden auth")
        XCTAssertEqual(model.goalTone, .ok)
    }

    func testTheProjectSaysWhetherItIsYoursOrTheEngines() {
        // The distinction decides how much the next run matters — one edits the person's own files —
        // so it is stated rather than left to be inferred from a path.
        var attached = base()
        attached.workspace = ["name": .string("my-app"), "attached": .bool(true)]
        XCTAssertTrue(spine(attached).projectDetail.contains("your folder"))

        var managed = base()
        managed.workspace = ["name": .string("console"), "attached": .bool(false)]
        XCTAssertTrue(spine(managed).projectDetail.contains("engine owns"))
    }

    func testWithNoWorkspaceReportedTheProjectComesFromThePath() {
        // Before the first poll there is no `workspace` block, and a spine that said "no project" while
        // the app was demonstrably pointed at one would be worse than useless.
        var input = base(engineState: .launching)
        input.workspace = [:]
        XCTAssertEqual(spine(input).projectName, "my-app")
    }

    func testAFatalFailureOutranksAHealthyLookingEngineState() {
        // The trap the whole rewrite is about: after a bootstrap failure the engine's state can read
        // `idle`, and an app that said "Engine not running" there would look like a normal idle app
        // rather than a broken one.
        var input = base(engineState: .idle)
        input.engineFailure = "the engine could not start: bad config"
        let model = spine(input)
        XCTAssertEqual(model.stateWord, "Engine failed")
        XCTAssertEqual(model.stateTone, .bad)
    }

    func testEveryEngineStateProducesAWordAndATone() {
        // Never colour alone: the state is always a word as well as a hue.
        for state in [EngineState.idle, .launching, .running, .pausing, .paused,
                      .terminating, .finished, .failed] {
            let model = spine(base(engineState: state))
            XCTAssertFalse(model.stateWord.isEmpty, "\(state) produced no word")
        }
    }

    // MARK: - The gate, and the one rule about buttons

    private func gateInput(_ gate: [String: JSONValue],
                           posture: OrgController.Posture,
                           decidedByGoal: Bool = false) -> SpineInput {
        var input = base()
        input.goal = ["objective": .string("ship it"), "posture": .string(posture.rawValue)]
        input.pendingGate = gate
        input.gateDisposition = OrgController.gateDisposition(
            gate: gate, posture: posture, decidedByGoal: decidedByGoal)
        return input
    }

    func testAGateTheEngineLeftToTheGoalIsShown() {
        // The console forwards this one itself, so the person is *told* what is happening rather than
        // shown a button that would race the console's own approval.
        let model = spine(gateInput(["gate_id": .string("reroute"), "kind": .string("agent"),
                                     "reason": .string("the org gate wants to reroute")],
                                    posture: .unattended))
        XCTAssertNotNil(model.gate)
        XCTAssertEqual(model.gate?.reason, "the org gate wants to reroute")
        XCTAssertFalse(model.gate?.canAct ?? true,
                       "the console is answering this gate, so no button may be offered")
        XCTAssertFalse(model.needsAPerson)
    }

    func testAGateTheEngineRefusedIsTheOneThatOffersButtons() {
        // The engine's own refusal (`waiting_on: owner`) with its reason. This is the only case where
        // the console may act, and therefore the only case where a button renders.
        let model = spine(gateInput([
            "gate_id": .string("release"), "kind": .string("human"),
            "reason": .string("Owner release approval"),
            "waiting_on": .string("owner"),
            "why": .string("a safety control fired (guardrail); the goal may not release this"),
        ], posture: .unattended))
        XCTAssertEqual(model.gate?.canAct, true)
        XCTAssertTrue(model.needsAPerson)
        XCTAssertTrue(model.gate?.why.contains("safety control") ?? false, model.gate?.why ?? "nil")
    }

    func testASupervisedGoalAlwaysOffersButtonsWhateverTheGateLooksLike() {
        // `GateDisposition`'s rule, reached through the spine: a supervised goal parks at every gate.
        let model = spine(gateInput(["gate_id": .string("x"), "reason": .string("anything")],
                                    posture: .supervised))
        XCTAssertEqual(model.gate?.canAct, true)
        XCTAssertTrue(model.gate?.why.contains("supervised") ?? false, model.gate?.why ?? "nil")
    }

    func testAnUnknownPostureNeverOffersTheForwardsCase() {
        // The safe reading of an unknown authority is "I do not know", never "it may act". `unknown`
        // is what a posture this build cannot parse resolves to.
        let model = spine(gateInput(["gate_id": .string("x"), "reason": .string("anything")],
                                    posture: .unknown))
        XCTAssertEqual(model.gate?.canAct, true,
                       "an unreadable posture is treated as supervised, so the gate is the person's")
        XCTAssertTrue(model.gate?.why.contains("does not know") ?? false, model.gate?.why ?? "nil")
    }

    func testAGateTheEngineAlreadyAnsweredIsNotAQuestionForAnyone() {
        // The third disposition. Leaving `pendingGate` set would keep asking a person to confirm a
        // decision already recorded, and `canAct` must be false so no button appears.
        let model = spine(gateInput(["gate_id": .string("reroute"), "reason": .string("reroute")],
                                    posture: .unattended, decidedByGoal: true))
        XCTAssertEqual(model.gate?.canAct, false)
        XCTAssertFalse(model.needsAPerson)
    }

    func testNoGateMeansNoGateRowAtAll() {
        let model = spine(base())
        XCTAssertNil(model.gate, "a gate row with nothing in it is a control that cannot act")
        XCTAssertFalse(model.needsAPerson)
    }

    func testTheGatesEvidenceIsReportedAndWhatIsMissingIsNamed() {
        // A release gate is decidable only against its evidence, so the counts travel with it — and
        // what is absent is named rather than left as a difference the reader has to compute.
        let model = spine(gateInput([
            "gate_id": .string("release"), "reason": .string("Owner release approval"),
            "waiting_on": .string("owner"),
            "requires": .array([.string("tests"), .string("review")]),
            "present": .array([.string("tests")]),
        ], posture: .supervised))
        XCTAssertEqual(model.gate?.requires.count, 2)
        XCTAssertEqual(model.gate?.present.count, 1)
        XCTAssertEqual(model.gate?.missing, ["review"])
        XCTAssertEqual(model.gate?.evidence, "2 required, 1 present — missing review")
    }

    func testAGateWithNoDeclaredEvidenceSaysNothingRatherThanZeroOfZero() {
        // An `agent` gate declares no artifacts. Rendering "0 required, 0 present" there would read as
        // a failed check when in fact no check applies.
        let model = spine(gateInput(["gate_id": .string("x"), "reason": .string("reroute")],
                                    posture: .supervised))
        XCTAssertNil(model.gate?.evidence)
        XCTAssertTrue(model.gate?.missing.isEmpty ?? false)
    }

    // MARK: - Next is the engine's answer

    func testTheNextLineUsesTheEnginesOwnLabelAndOrder() {
        // The engine orders a gate above a staffing gap above a stop above a resume, and it can see the
        // ledger and the run's `stop_reason` — which this app cannot. Recomputing that order in Swift
        // would be a second implementation of a rule already resolved, and the two would disagree.
        var input = base()
        input.activity = ["next_action": .object([
            "kind": .string("hire"),
            "label": .string("Hire for 2 unstaffed capabilities"),
            "detail": .string("devops-engineer; sre"),
            "command": .string("engine.cli hire --skill devops-engineer"),
        ])]
        let model = spine(input)
        XCTAssertEqual(model.next?.kind, "hire")
        XCTAssertEqual(model.next?.label, "Hire for 2 unstaffed capabilities")
        XCTAssertEqual(model.next?.detail, "devops-engineer; sre")
        XCTAssertEqual(model.next?.command, "engine.cli hire --skill devops-engineer")
    }

    func testNoNextActionMeansNoNextRowRatherThanAnEmptyOne() {
        var input = base()
        input.activity = ["next_action": .object(["kind": .string("none"),
                                                  "label": .string("Nothing needs you")])]
        XCTAssertNil(spine(input).next,
                     "“nothing needs you” is the absence of a next step, not a step")
    }

    func testWithNoEngineReportAGateStillBecomesTheNextThing() {
        // The engine may be down, or nothing may have run. If a gate is on screen it is still the next
        // thing, and saying so is better than an empty line.
        let model = spine(gateInput([
            "gate_id": .string("release"), "reason": .string("Owner release approval"),
            "waiting_on": .string("owner"),
        ], posture: .supervised))
        XCTAssertEqual(model.next?.kind, "decide")
        XCTAssertTrue(model.next?.label.contains("Owner release approval") ?? false,
                      model.next?.label ?? "nil")
    }

    func testTheAppOffersExactlyWhatTheEngineSaidItCouldPerform() {
        // A button that silently does nothing is worse than a line telling you what to run, so the app
        // must not decide for itself what it can do — it reads `performable` and `needs`, which the
        // engine sends with every action (`engine/activity.py::_action`).
        //
        // This test used to construct `NextLine`s from *kind* names and assert the app's own answer for
        // each. That is the enumeration this change removes: the engine's kinds were a list in Swift,
        // and the day `retry` was added the app's opinion about it was stale until someone widened the
        // switch. So the input here is the *payload*, exactly as `_next_action` writes it.
        func line(_ action: [String: JSONValue]) -> SpineModel.NextLine? {
            var input = base()
            input.activity = ["next_action": .object(action)]
            return spine(input).next
        }

        // An action the engine marks performable is offerable, whatever its kind is — including a kind
        // this build has never seen, which is the property the whole change exists for.
        let hire = line(["kind": .string("hire"), "label": .string("Hire"),
                         "detail": .string(""), "command": .string("engine.cli hire"),
                         "performable": .bool(true), "needs": .string("")])
        XCTAssertEqual(hire?.canPerform(gateIsWaitingForHuman: false), true)
        let unknown = line(["kind": .string("a-kind-from-a-later-engine"),
                            "label": .string("Something new"), "detail": .string(""),
                            "command": .string("engine.cli whatever"),
                            "performable": .bool(true), "needs": .string("")])
        XCTAssertEqual(unknown?.canPerform(gateIsWaitingForHuman: false), true,
                       "the app must not need an edit to offer a kind the engine added")

        // The one conditional the engine names: the gate is the person's only while the gate still is.
        // The engine cannot answer it — whether it has since decided the gate itself is not in the
        // checkpoint it reads — so it says what performing the action needs and the app supplies the
        // state from `GateDisposition`.
        let decide = line(["kind": .string("decide"), "label": .string("Decide gate 'release'"),
                           "detail": .string(""), "command": .string("engine.cli decide"),
                           "performable": .bool(false), "needs": .string("gate")])
        XCTAssertEqual(decide?.canPerform(gateIsWaitingForHuman: true), true)
        XCTAssertEqual(decide?.canPerform(gateIsWaitingForHuman: false), false,
                       "a gate that is not the person's must not be offerable")

        // `retry` is the engine's own `false`: no `serve` command re-runs a graph, so the command is
        // shown instead. A payload from a build that predates these two fields reads as not-performable
        // rather than as a dead button — the safe direction for a field the engine did not send.
        let notOfferable: [[String: JSONValue]] = [
            ["kind": .string("retry"), "label": .string("Re-run the graph"),
             "detail": .string(""), "command": .string("engine.cli run"),
             "performable": .bool(false), "needs": .string("")],
            ["kind": .string("decide"), "label": .string("Decide")],
        ]
        for action in notOfferable {
            XCTAssertEqual(line(action)?.canPerform(gateIsWaitingForHuman: true), false,
                           "\(action["kind"]?.stringValue ?? "?") is not offerable")
        }
        // `none` is the absence of a next step, so there is no line at all — asserted by
        // `testNoNextActionMeansNoNextRowRatherThanAnEmptyOne` above, and repeated here because this
        // test's own list must not depend on that one staying.
        XCTAssertNil(line(["kind": .string("none"), "label": .string("Nothing needs you")]))
    }

    // MARK: - The first-run hint

    func testASetupHintIsCarriedThroughForTheSpineToShow() {
        var input = base()
        input.setupHint = "No default model yet — choose one in Setup"
        XCTAssertEqual(spine(input).setupHint, "No default model yet — choose one in Setup")
    }

    func testWithNoSetupHintTheSpineSaysNothingAboutSetup() {
        XCTAssertNil(spine(base()).setupHint)
    }

    // MARK: - The board's stopped rows and the move that resolves them

    /// The row the engine produces for the state a person reported — `pm` blocked by the edge
    /// guardrail — and the board's `next` line, both captured from the real fold with:
    ///
    ///     engine.cli flow --project /tmp/boardproof --json
    ///
    /// over a checkpoint whose `pm` record is exactly what the runner's `_apply_guardrail` writes
    /// (`{status: blocked, verdict: guardrail-blocked}`, no summary). The transcript is quoted in the
    /// test rather than read from a file so the assertion is about the *shape* the engine sends, which
    /// is what the app is written against.
    private var guardrailBoard: (row: [String: JSONValue], next: String) {
        (row: ["node_id": .string("pm"), "skill": .string("product-manager"),
               "agent_name": .string("Priya"), "status": .string("blocked"),
               "verdict": .string("guardrail-blocked"), "tone": .string("bad"),
               "blocked_by": .string("the work finished, but what it handed on was refused at the "
                                     + "edge — a contract failure, not a crash"),
               "summary": .string("")],
         next: "engine.cli run --slug boardproof --manifest /tmp/boardproof/manifest.yaml   — "
             + "nothing is driving this graph, so re-running it gives pm another attempt at the "
             + "payload the edge refused")
    }

    func testTheStoppedPredicateIsTheEnginesOwn() {
        // `engine/flow.py:247` (`_is_stuck`) and its two sets at `:119-120`. The app mirrors it so the
        // board's rows, the board's header and the offline run list cannot answer one question three
        // ways — they did: the list counted `blocked` and a guardrail verdict, the board counted four
        // statuses, and a step stopped by its completion contract fell between them.
        XCTAssertEqual(BoardStop.stuckStatuses,
                       ["blocked", "failed", "needs_review", "awaiting_owner"])
        XCTAssertEqual(BoardStop.stuckVerdicts, ["guardrail-blocked", "awaiting_owner"])
        for status in BoardStop.stuckStatuses {
            XCTAssertTrue(BoardStop.isStuck(status: status, verdict: ""), status)
        }
        for verdict in BoardStop.stuckVerdicts {
            XCTAssertTrue(BoardStop.isStuck(status: "pending", verdict: verdict), verdict)
        }
        for status in ["done", "pass", "skipped"] {
            XCTAssertFalse(BoardStop.isStuck(status: status, verdict: "guardrail-blocked"),
                           "\(status) is finished whatever verdict it still carries")
        }
        for status in ["running", "working", "pending", "queued", ""] {
            XCTAssertFalse(BoardStop.isStuck(status: status, verdict: ""), status)
        }
    }

    func testTheReportedRowsStoppedStateIsReadFromTheEnginesPayload() {
        // The row the engine sends for the reported state: `blocked_by` is populated, so the board has
        // the engine's own sentence rather than the token, and the row is stopped by the same predicate
        // the count uses.
        XCTAssertTrue(BoardStop.isStuck(guardrailBoard.row))
        XCTAssertEqual(guardrailBoard.row["blocked_by"]?.stringValue,
                       "the work finished, but what it handed on was refused at the edge — "
                       + "a contract failure, not a crash")
    }

    func testTheBoardsNextMoveIsSplitIntoTheCommandAndTheReason() throws {
        // One line, command first, because the engine makes the `--json` value and the terminal line
        // the same sentence and expects a caller that wants only the command to take the text before
        // the separator (`engine/systemcli.py:40`, `NEXT_SEP` at `:96`).
        let next = try XCTUnwrap(BoardNext.parse(guardrailBoard.next))
        XCTAssertEqual(next.command,
                       "engine.cli run --slug boardproof --manifest /tmp/boardproof/manifest.yaml")
        XCTAssertTrue(next.why.hasPrefix("nothing is driving this graph"), next.why)
        XCTAssertTrue(next.why.hasSuffix("payload the edge refused"), next.why)
        // Round-tripping is exact, so a person copying the line gets the engine's line.
        XCTAssertEqual(next.line, guardrailBoard.next)
    }

    func testANextLineWithoutASeparatorIsKeptWhole() throws {
        // Dropping it would be the app deciding the engine's answer was malformed.
        let next = try XCTUnwrap(BoardNext.parse("engine.cli goal resume"))
        XCTAssertEqual(next.command, "engine.cli goal resume")
        XCTAssertEqual(next.why, "")
        XCTAssertEqual(next.line, "engine.cli goal resume")
    }

    func testNoNextMoveMeansNothingIsShown() {
        // The engine returns "" when it cannot name a resumable run or a manifest (`engine/flow.py:394`),
        // and "nothing to do" must render as nothing rather than as an empty command.
        XCTAssertNil(BoardNext.parse(""))
        XCTAssertNil(BoardNext.parse("   "))
    }

    func testTheSpinesRetryActionIsShownAsTheEnginesCommandNotAButton() {
        // The engine's `retry` kind, whose payload is transcribed from the real report with:
        //
        //     engine.cli activity --project /tmp/boardproof2 --json
        //
        // over the same guardrail-blocked node and *no* `stop_reason`, which is the state where the
        // retry rather than "investigate" is the engine's answer. The two fields at the end are the
        // engine's own verdict on it: `performable: false` — no `serve` command runs a graph
        // (`engine/serve.py:1168`'s `start` plans a new one) — and no condition under which it would
        // be true. So the spine carries it through and the view falls back to the engine's command,
        // which is the honest thing to show rather than a button that would come back "unknown
        // command". Note the *detail* is the engine's own sentence for the stop token, which
        // `flow.stop_words` also sends separately for a report that carries only the token.
        var input = base()
        input.activity = ["next_action": .object([
            "kind": .string("retry"),
            "label": .string("Re-run the graph so pm gets another attempt"),
            "detail": .string("the work finished, but what it handed on was refused at the edge — "
                              + "a contract failure, not a crash"),
            "command": .string("engine.cli run --slug boardproof2 --manifest "
                               + "/tmp/boardproof2/manifest.yaml"),
            "performable": .bool(false),
            "needs": .string("")])]
        let next = spine(input).next
        XCTAssertEqual(next?.kind, "retry")
        XCTAssertEqual(next?.label, "Re-run the graph so pm gets another attempt")
        XCTAssertFalse(next?.canPerform(gateIsWaitingForHuman: false) ?? true,
                       "the console has no command that re-runs a graph")
        XCTAssertEqual(next?.command,
                       "engine.cli run --slug boardproof2 --manifest /tmp/boardproof2/manifest.yaml")
    }
}
