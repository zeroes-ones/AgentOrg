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
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        XCTAssertEqual(plan.title, "Goal complete")
        XCTAssertEqual(plan.body, "cursor pagination added")
        // Inform, not interrupt: the work finished, so nothing is waiting on the person.
        XCTAssertEqual(plan.urgency, .inform)
    }

    func testGoalBlockedInterruptsWithTheReasonAndWhereToAct() throws {
        // A block is the one state where work has stopped and only a person can move it — so the banner
        // carries the engine's own reason **and** names the surface that holds the goal's controls. The
        // second half is the one this used to leave out: "Goal blocked" with no destination is a state a
        // person has to go hunting for.
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("goal.blocked", ["reason": .string("no route to a provider")]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        XCTAssertEqual(plan.title, "Goal blocked")
        XCTAssertTrue(plan.body.contains("no route to a provider"), plan.body)
        XCTAssertTrue(plan.body.contains("open Now"), plan.body)
        XCTAssertEqual(plan.urgency, .interrupt)
    }

    func testGoalPausedForBudgetSaysSo() throws {
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("goal.paused", ["reason": .string("budget_spend")]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        XCTAssertEqual(plan.title, "Goal paused: budget reached")
        XCTAssertEqual(plan.urgency, .inform)
    }

    func testARunThatEndedAtAGateIsReportedAsWaitingNotFinished() throws {
        // The most misleading message the app could send: "the run finished" for a run that parked and
        // needs a decision. The engine's own outcome says which it was, so it is read rather than
        // guessed.
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("run.end", ["outcome": .string("awaiting_human")]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        XCTAssertEqual(plan.title, "Run waiting on you")
        XCTAssertEqual(plan.urgency, .interrupt)
    }

    func testAnOrdinaryRunEndIsOnlyInformative() throws {
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("run.end", ["outcome": .string("complete")]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        XCTAssertEqual(plan.title, "Run finished")
        XCTAssertEqual(plan.body, "Outcome: complete.")
        XCTAssertEqual(plan.urgency, .inform)
    }

    // MARK: - run.end, read the way the engine writes it

    func testARunThatParkedIsReadFromTheStateTheEngineActuallyWrites() throws {
        // **The bug this fixes, stated as the frame the engine sends.** `run.end` carries
        // `RunOutcome.as_dict()`: `state` is `finished` / `failed` / `gated`, `gated` and `broken` are
        // the booleans behind it, and `outcome` is the *runner's summary word*, absent whenever there
        // was no summary. The planner read `outcome` alone, so its gate branch could not match a real
        // payload — every run parked at a gate reached the person as "the run finished", which is the
        // most misleading sentence this app can send.
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("run.end", ["state": .string("gated"), "gated": .bool(true),
                                   "phase": .string("awaiting_human")]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        XCTAssertEqual(plan.title, "Run waiting on you")
        XCTAssertEqual(plan.urgency, .interrupt)
    }

    func testARunThatBrokeInterruptsWithTheEnginesOwnError() throws {
        // A run that broke is work that stopped with nobody watching — the case this app exists for —
        // and the body is the host's own `error`, not a sentence composed here.
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("run.end", ["state": .string("failed"), "broken": .bool(true),
                                   "exit_code": .int(1),
                                   "error": .string("the runner exited 1")]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        XCTAssertEqual(plan.title, "Run failed")
        XCTAssertTrue(plan.body.contains("the runner exited 1"), plan.body)
        // The engine's sentence is followed by where to read the wreckage — `Runs` is the destination
        // that holds the checkpoint's nodes, their verdicts and every handoff.
        XCTAssertTrue(plan.body.contains("open Runs"), plan.body)
        XCTAssertEqual(plan.urgency, .interrupt)
    }

    func testAStopTheOwnerAskedForDoesNotInterrupt() throws {
        // The engine's own `termination` word decides this, rather than a guess from `killed`: a run
        // the person aborted, and a run the engine reaped on its way down, both arrive as
        // `state: failed` with `killed` set. Interrupting someone for a stop they pressed themselves
        // is the noise that trains them to ignore the banner that matters.
        for word in ["aborted", "shutdown"] {
            let plan = try XCTUnwrap(NotificationPlanner.plan(
                for: event("run.end", ["state": .string("failed"), "broken": .bool(true),
                                       "killed": .bool(true), "termination": .string(word)]),
                gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false), word)
            XCTAssertEqual(plan.title, "Run stopped", word)
            XCTAssertEqual(plan.urgency, .inform, word)
        }
    }

    // MARK: - What became of the attempt

    func testTheUnavailableOutcomeDoesNotSendAnyoneToSystemSettings() {
        // A process with no application bundle cannot be granted anything: it is not listed in
        // System Settings at all. Saying "denied" there — which is what the console did, because it
        // could only see two booleans — is a wrong instruction, and the advice names the build that
        // *can* notify instead.
        let outcome = NotificationOutcome.unavailable()
        XCTAssertEqual(outcome.kind, .unavailable)
        XCTAssertFalse(outcome.sentence.contains("System Settings"))
        XCTAssertTrue(outcome.advice?.contains("AgentOrg.app") ?? false)
        XCTAssertTrue(outcome.needsAttention)
    }

    func testEachOutcomeCarriesItsOwnAdviceAndOnlyADeliveryIsUnremarkable() {
        XCTAssertEqual(NotificationOutcome.delivered("Goal complete").sentence,
                       "notified: Goal complete")
        XCTAssertNil(NotificationOutcome.delivered("Goal complete").advice)
        XCTAssertFalse(NotificationOutcome.delivered("Goal complete").needsAttention)

        // The denial is changeable, so its advice says where — and its sentence is kept word for word
        // from what this console has always shown, so a state a person already recognises does not
        // change wording under them.
        let denied = NotificationOutcome.denied("Run failed")
        XCTAssertEqual(denied.kind, .denied)
        XCTAssertEqual(denied.sentence, "notifications are off (denied in System Settings)")
        XCTAssertTrue(denied.advice?.contains("System Settings") ?? false)
        XCTAssertTrue(denied.needsAttention)

        let failed = NotificationOutcome.failed("Run failed")
        XCTAssertEqual(failed.kind, .failed)
        XCTAssertEqual(failed.title, "Run failed")
        XCTAssertTrue(failed.needsAttention)
    }

    // MARK: - The gate, and the one thing it must not do

    func testAGateWaitingOnAHumanInterruptsAndCarriesTheEngineWhyAndWhereToDecideIt() throws {
        // The engine's own reason travels in `why`, and it is what makes the banner actionable: a
        // person reads *which* refusal fired, not just "a gate" — and then where in this app the
        // decision is made. The gate row's Approve/Reject is that control, and it is on Now.
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("human.gate", [
                "gate_id": .string("release"),
                "reason": .string("Owner release approval"),
                "waiting_on": .string("owner"),
                "why": .string("a safety control fired (guardrail); the goal may not release this"),
            ]),
            gateIsWaitingOnAHuman: true, planIsAwaitingApproval: false))
        XCTAssertEqual(plan.urgency, .interrupt)
        XCTAssertTrue(plan.title.contains("safety control"), plan.title)
        XCTAssertTrue(plan.body.contains("guardrail"), plan.body)
        XCTAssertTrue(plan.body.contains("on Now"), plan.body)
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
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
    }

    func testAGateWithNoEvidenceNamesThatRefusal() throws {
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("human.gate", [
                "reason": .string("release"),
                "waiting_on": .string("owner"),
                "why": .string("the gate's evidence is not present, so there is nothing for the goal to release"),
            ]),
            gateIsWaitingOnAHuman: true, planIsAwaitingApproval: false))
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
            XCTAssertNil(NotificationPlanner.plan(for: event(type), gateIsWaitingOnAHuman: true, planIsAwaitingApproval: false),
                         "\(type) must not interrupt anyone")
        }
    }

    func testTheEventsThatAreSilentOnPurposeSaySoInACommentAndInATest() {
        // **Silence that is a decision, not an oversight.** These four are all states a person may
        // *read* — the console writes a notice for each — but none of them is a stop only a person can
        // move, which is the test this planner applies. They are listed here so that "why is there no
        // banner for a guardrail block" has an answer in the code rather than in somebody's memory.
        //
        // A node stopped by its edge guardrail (`guardrail.blocked`) or its completion contract is a
        // node inside a run that is still going; when it is the *run* that stops, the stop arrives as
        // `run.end` and is announced there. `leak.detected` and `cost.reconciled` are findings and
        // measurements, not stoppages, and the rest are decisions the engine recorded after the fact.
        for type in ["guardrail.blocked", "guardrail.block", "leak.detected", "cost.reconciled",
                     "review.rejected", "handoff.rejected", "agent.slo.breach",
                     "manifest.approved", "human.decision", "policy.changed"] {
            XCTAssertNil(NotificationPlanner.plan(for: event(type, ["reason": .string("guardrail")]),
                                                  gateIsWaitingOnAHuman: false,
                                                  planIsAwaitingApproval: false),
                         "\(type) is a state to read, not a stop only a person can move")
        }
    }

    func testAGatePauseDoesNotDoubleUpWithTheGateItself() {
        // **`goal.paused(reason: gate)` and `human.gate` describe one stop, and this planner used to
        // post a banner for each.** The pause was de-escalated to `.inform` rather than dropped — but a
        // separate banner with its own identifier still *arrives*, which is the "two banners for one
        // stop" the header of this file names as the noise that trains a person to dismiss without
        // reading. The rest of the console already reads it this way: `OrgController`'s `goal.paused`
        // case sets a status-bar notice for every reason *except* a gate, because the gate's own row is
        // what says it.
        XCTAssertNil(NotificationPlanner.plan(
            for: event("goal.paused", ["reason": .string("gate")]),
            gateIsWaitingOnAHuman: true, planIsAwaitingApproval: false),
                     "a gate pause is the gate's own banner; a second one for the same stop is noise")
        // The pause that is *not* a gate is still said — a stop that was not the person's doing is news.
        let own = NotificationPlanner.plan(
            for: event("goal.paused", ["reason": .string("budget_spend")]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false)
        XCTAssertEqual(own?.urgency, .inform)
        XCTAssertTrue(own?.body.contains("budget_spend") ?? false, own?.body ?? "")
    }

    func testARunEndingAtAGateDoesNotAnnounceTheStopTheGateAlreadyAnnounced() throws {
        // **The other half of the same rule, for the frame pair the engine actually sends.** A run that
        // parks at a gate emits `human.gate` (which carries the engine's `why`) and then `run.end` with
        // `state: gated` (which carries no reason at all). Both used to post — under two different
        // identifiers, so they did not even replace each other — and the second banner was the poorer
        // one. With the gate still on screen the run's ending is the same stop seen again, and says
        // nothing.
        let runEnd: [String: JSONValue] = ["state": .string("gated"), "gated": .bool(true)]
        XCTAssertNil(NotificationPlanner.plan(
            for: event("run.end", runEnd),
            gateIsWaitingOnAHuman: true, planIsAwaitingApproval: false))
        // **And the same for the plan stop**, which is the third frame of this shape: a run parked at
        // `awaiting_approval` announces itself once, through `manifest.proposed`.
        XCTAssertNil(NotificationPlanner.plan(
            for: event("run.end", runEnd),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: true))
        // With neither stop on screen — a console relaunched mid-run, a frame dropped — nothing has been
        // announced about this ending, so it speaks. A missing banner is recoverable; a person who
        // learns to ignore banners is not.
        let alone = try XCTUnwrap(NotificationPlanner.plan(
            for: event("run.end", runEnd),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        XCTAssertEqual(alone.title, "Run waiting on you")
        XCTAssertEqual(alone.urgency, .interrupt)
    }

    // MARK: - The stops that notified nobody

    func testAPlanParkedForApprovalNotifiesWithTheEnginesOwnVerdict() throws {
        // **The engine state that asks a person for something and reached them as silence.**
        // `manifest.proposed` is what `_cmd_start` and `Orchestrator.prepare` emit before parking the
        // run at `awaiting_approval`; the planner had no case for it, so a run that could not proceed
        // without an approval produced no banner at all — while the sidebar quietly grew a badge.
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("manifest.proposed", [
                "slug": .string("harden-auth"),
                "validated": .bool(true),
                "nodes": .array([.string("pm"), .string("dev"), .string("reviewer")]),
                "gates": .array([.string("release")]),
                "staffing_gaps": .array([]),
                "approvable": .bool(true),
            ]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: true))
        XCTAssertEqual(plan.identifier, NotificationIdentifier.plan)
        XCTAssertEqual(plan.urgency, .interrupt)
        XCTAssertTrue(plan.title.contains("needs your approval"), plan.title)
        // The engine's own field decides the sentence — the same field the plan card renders.
        XCTAssertTrue(plan.body.contains("3 steps"), plan.body)
        XCTAssertTrue(plan.body.contains("approve it on Now"), plan.body)
    }

    func testAPlanTheEngineWillNotAcceptCarriesItsOwnReasonRatherThanABlanketRefusal() throws {
        // The engine sends `reason` whenever `approvable` is false, naming the thing that blocks it. A
        // banner that reported "a plan needs approval" for a plan the engine has already refused would
        // send a person to press a button the app does not even offer.
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("manifest.proposed", [
                "nodes": .array([.string("pm")]),
                "approvable": .bool(false),
                "reason": .string("no plan file is on disk for this run"),
            ]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: true))
        XCTAssertTrue(plan.title.contains("cannot be approved"), plan.title)
        XCTAssertTrue(plan.body.contains("no plan file is on disk"), plan.body)

        // And the fourth state: a refusal the engine gave *without* a reason. Said as what it is rather
        // than filled in with a cause this build cannot see.
        let silent = try XCTUnwrap(NotificationPlanner.plan(
            for: event("manifest.proposed", ["approvable": .bool(false)]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: true))
        XCTAssertTrue(silent.title.contains("cannot be approved"), silent.title)
        XCTAssertTrue(silent.body.contains("without saying why"), silent.body)
    }

    func testAPlanPayloadWithNoVerdictIsNotReportedAsARefusalTheEngineNeverMade() throws {
        // Three states, not two: a build that carries neither `approvable` nor `reason` has said
        // nothing about whether the plan can be approved, and this planner does not decide that for it
        // — the same rule `NowPane.notApprovableReason` follows for the card.
        let plan = try XCTUnwrap(NotificationPlanner.plan(
            for: event("manifest.proposed", ["nodes": .array([.string("pm")])]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: true))
        XCTAssertTrue(plan.title.contains("waiting for your decision"), plan.title)
        XCTAssertFalse(plan.body.contains("cannot"), plan.body)
        XCTAssertTrue(plan.body.contains("1 step"), plan.body)
    }

    func testAFatalEngineErrorNotifiesAndAnOrdinaryOneDoesNot() throws {
        // The engine's own `fatal` flag is what the console already reads to decide the engine is about
        // to die (it sets the failure banner from the same field). The notification path saw only the
        // *bridge* failing afterwards — and an engine that dies without the bridge noticing reached
        // nobody at all. Everything else in this frame type is ordinary traffic: the recorded trace's
        // own `error` is a retryable rate limit.
        let ordinary = NotificationPlanner.plan(
            for: event("error", ["kind": .string("rate_limit"), "message": .string("slow down"),
                                 "retryable": .bool(true)]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false)
        XCTAssertNil(ordinary, "a retryable rate limit is ordinary traffic, not a stop")
        let fatal = try XCTUnwrap(NotificationPlanner.plan(
            for: event("error", ["fatal": .bool(true),
                                 "message": .string("the port is already in use")]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        XCTAssertEqual(fatal.identifier, NotificationIdentifier.engineFailed,
                       "one death, one banner: the same name the bridge's report uses, so whichever "
                       + "frame arrives second replaces the first rather than joining it")
        XCTAssertEqual(fatal.urgency, .interrupt)
        XCTAssertTrue(fatal.body.contains("the port is already in use"), fatal.body)
    }

    func testTheBridgesOwnFailureNamesTheButtonThatFixesIt() throws {
        // The engine process stopping is the one stop that is not an engine event, so it is planned by
        // its own factory — and the reason it is not composed at the call site is that the call site had
        // no test: the body said the reason and stopped there, leaving a person to work out that the
        // console's "Try again" button is what starts it again.
        let plan = NotificationPlanner.engineStopped(reason: "the engine exited with status 1")
        XCTAssertEqual(plan.identifier, NotificationIdentifier.engineFailed)
        XCTAssertEqual(plan.urgency, .interrupt)
        XCTAssertTrue(plan.body.contains("exited with status 1"), plan.body)
        XCTAssertTrue(plan.body.contains("Try again"), plan.body)
    }

    // MARK: - Identity and threading

    func testNotificationsAboutOneRunShareAThread() throws {
        // So a run's banners group in Notification Centre instead of scattering.
        let first = try XCTUnwrap(NotificationPlanner.plan(
            for: event("goal.completed", ["summary": .string("done")]), gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        let second = try XCTUnwrap(NotificationPlanner.plan(
            for: event("run.end", ["outcome": .string("complete")]), gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        XCTAssertEqual(first.thread, second.thread)
        XCTAssertTrue(first.thread.contains("run_1"))
    }

    func testTheIdentifierIsStablePerStopSoBannersReplaceRatherThanStack() throws {
        let first = try XCTUnwrap(NotificationPlanner.plan(
            for: event("human.gate", ["reason": .string("one")]), gateIsWaitingOnAHuman: true, planIsAwaitingApproval: false))
        let second = try XCTUnwrap(NotificationPlanner.plan(
            for: event("human.gate", ["reason": .string("two")]), gateIsWaitingOnAHuman: true, planIsAwaitingApproval: false))
        XCTAssertEqual(first.identifier, second.identifier,
                       "two gates must replace each other rather than stack up")
    }

    func testTheTwoFramesThatDescribeOneGateStopShareAnIdentifier() throws {
        // **One stop, one identifier.** The gate's own frame and the run ending at that gate are two
        // event kinds describing one stop; with an identifier each they were two banners that did not
        // replace each other. Sharing one name is also what makes the stop cleanable: the console
        // withdraws `NotificationIdentifier.gate` when the decision is made, and both frames' banners go.
        let gate = try XCTUnwrap(NotificationPlanner.plan(
            for: event("human.gate", ["reason": .string("release"), "waiting_on": .string("owner")]),
            gateIsWaitingOnAHuman: true, planIsAwaitingApproval: false))
        let ending = try XCTUnwrap(NotificationPlanner.plan(
            for: event("run.end", ["state": .string("gated")]),
            gateIsWaitingOnAHuman: false, planIsAwaitingApproval: false))
        XCTAssertEqual(gate.identifier, ending.identifier)
        XCTAssertEqual(gate.identifier, NotificationIdentifier.gate)
    }

    func testEveryBannerThisBuildCanPostCanAlsoBeWithdrawn() throws {
        // **The vocabulary has to be complete, or a banner stays in Notification Centre for ever.**
        // `clearDeliveredNotifications()` withdraws by name — `removeAllDeliveredNotifications()` would
        // take back anything a future build posted, but the name list is what makes "which of my banners
        // are still out there" an answerable question. That only holds if the list is the whole
        // vocabulary, so it is held to the plans this suite can produce rather than trusted to agree.
        var produced: Set<String> = []
        let samples: [(String, [String: JSONValue], Bool, Bool)] = [
            ("goal.completed", ["summary": .string("done")], false, false),
            ("goal.blocked", ["reason": .string("stuck")], false, false),
            ("goal.paused", ["reason": .string("budget_spend")], false, false),
            ("manifest.proposed", ["nodes": .array([.string("pm")])], false, true),
            ("error", ["fatal": .bool(true), "message": .string("boom")], false, false),
            ("run.end", ["outcome": .string("complete")], false, false),
            ("run.end", ["state": .string("failed"), "broken": .bool(true)], false, false),
            ("run.end", ["state": .string("gated")], false, false),
            ("human.gate", ["reason": .string("release")], true, false),
        ]
        for (type, payload, gate, plan) in samples {
            if let plan = NotificationPlanner.plan(for: event(type, payload),
                                                   gateIsWaitingOnAHuman: gate,
                                                   planIsAwaitingApproval: plan) {
                produced.insert(plan.identifier)
            }
        }
        produced.insert(NotificationPlanner.engineStopped(reason: "gone").identifier)
        XCTAssertEqual(produced, Set(NotificationIdentifier.all),
                       "every identifier the planner can post under must be in the withdraw list")
    }

    func testAFactIsJoinedToItsActionWithOneSeparatorWhicheverWayTheEngineWroteIt() {
        // The bodies are the engine's sentence plus this app's clause about where to act, and engines
        // are not consistent about the full stop: the recorded trace's `why` ends in one, its `error`
        // strings often do not. Two separators for the two cases, so neither reads as a run-on.
        XCTAssertEqual(NotificationPlanner.andWhereToAct("it stopped.", "open Runs"),
                       "it stopped. Open Runs.")
        XCTAssertEqual(NotificationPlanner.andWhereToAct("it stopped", "open Runs"),
                       "it stopped — open Runs.")
        // An empty fact leaves the clause alone rather than an empty sentence and a dash.
        XCTAssertEqual(NotificationPlanner.andWhereToAct("   ", "open Now"), "Open Now.")
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
