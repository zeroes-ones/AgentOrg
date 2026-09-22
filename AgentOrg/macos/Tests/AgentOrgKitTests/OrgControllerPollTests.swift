//
//  OrgControllerPollTests.swift
//  AgentOrgKitTests
//
//  What the controller *publishes*, and what it must not.
//
//  WHY THIS IS ITS OWN FILE
//  ------------------------
//  Every claim here is about an invalidation: the two-second poll, the one-second launch tick, the
//  status bar's notice, and the liveness timestamp. `@Published` fires `objectWillChange` on every
//  assignment regardless of equality and there is no per-property granularity, so "this poll changed
//  nothing" is not a nicety — it is the difference between a window that re-renders twice a second on
//  an idle engine and one that does not. Asserting it needs a subscriber, which is why these are
//  separate from the derived-state tests in `OrgControllerTests`.
//
//  The two engine-facing tests at the end run a *real* child process, because the two behaviours they
//  pin are about state transitions — a relaunch that has to follow a stop, and a launch that has to stop
//  repainting — and neither is observable from a mock.
//

import Combine
import XCTest
@testable import AgentOrgKit

@MainActor
final class OrgControllerPollTests: XCTestCase {

    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-poll-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    // MARK: - Fixtures

    /// A controller over a temporary root, with the developer's own defaults untouched.
    ///
    /// The dismissal store is injected for the same reason `AppPreferences.ephemeral()` exists: a test
    /// that dismisses a proposal must not write into the developer's real `UserDefaults`, and the
    /// assertions must not depend on what an earlier run left there. In memory, not a suite — a suite
    /// of this test's own was still a real plist in the developer's preferences directory.
    private func makeController(arguments: [String]? = nil,
                                dismissals: PreferenceStore? = nil) -> OrgController {
        var settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
        if let arguments {
            settings = settings.with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                                     arguments: arguments)
        }
        return OrgController(settings: settings, maxRestartAttempts: 0, restartDelay: 0.05,
                             preferences: .ephemeral(),
                             proposalDismissals: dismissals ?? InMemoryPreferenceStore())
    }

    private func event(_ type: String, _ payload: [String: JSONValue] = [:]) -> EngineEvent {
        EngineEvent(v: 1, seq: 1, type: type, payload: payload,
                    runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil,
                    ts: "2026-09-19T00:00:00.000Z")
    }

    /// A full `status` snapshot, shaped the way `serve._cmd_status` shapes it.
    private func statusPayload(activity: String = "idle") -> [String: JSONValue] {
        [
            "phase": .string("idle"),
            "running": .bool(false),
            "org": .array([.object(["name": .string("Owner"), "state": .string("healthy")])]),
            "gate": .null,
            "outcome": .object([:]),
            "cost": .object([:]),
            "cache": .object([:]),
            "swarm": .object([:]),
            "workspace": .object(["path": .string(root.path), "name": .string("stand-in"),
                                  "attached": .bool(false)]),
            "goal": .object(["objective": .string("ship it")]),
            "mission": .object(["statement": .string("be useful")]),
            "subagents": .object(["children": .array([])]),
            "proposals": .object(["proposals": .array([]), "refused_count": .int(0),
                                  "refused": .array([]), "directory": .string("")]),
            "activity": .object(["summary": .string(activity)]),
            "flow": .object(["rows": .array([])]),
            "defaults": .object(["provider": .string("ollama"), "model": .string("qwen")]),
            "portfolio": .object(["orgs": .array([]), "active_org_id": .string("")]),
        ]
    }

    // MARK: - The poll is differential

    func testAnUnchangedStatusPollPublishesNothing() {
        // The defect in one assertion. Nineteen fields were assigned straight from the payload on every
        // two-second tick, and `@Published` has no equality check — so an engine that had said nothing
        // new still invalidated every view holding this controller, twice a second, for as long as the
        // window was open.
        let controller = makeController()
        let payload = statusPayload()
        controller.applyStatus(payload)

        var publishes = 0
        let cancellable = controller.objectWillChange.sink { publishes += 1 }
        controller.applyStatus(payload)
        XCTAssertEqual(publishes, 0,
                       "an unchanged poll must not invalidate the window")
        withExtendedLifetime(cancellable) {}
    }

    func testAPollThatChangesOneFieldPublishesOnlyForThatField() {
        // The counterpart, so the guard above cannot be satisfied by "applyStatus does nothing": one
        // changed value must still reach the UI. Two publishes, not one, because `runStatus` holds the
        // whole snapshot — a payload with anything different in it is necessarily a different snapshot.
        let controller = makeController()
        controller.applyStatus(statusPayload())

        var publishes = 0
        let cancellable = controller.objectWillChange.sink { publishes += 1 }
        controller.applyStatus(statusPayload(activity: "running"))
        XCTAssertEqual(publishes, 2,
                       "the changed field and the snapshot that carries it, and nothing else")
        XCTAssertEqual(controller.activity["summary"]?.stringValue, "running")
        withExtendedLifetime(cancellable) {}
    }

    func testAnUnchangedAttentionReplyPublishesNothing() {
        // `attention` is fetched on the slow cadence and whenever Now appears, and it holds a list —
        // `@Published` has no equality check, so assigning it straight from the reply would invalidate
        // every view twice a second worth of a list that had not moved.
        let controller = makeController()
        let reply: [String: JSONValue] = [
            "count": .int(1),
            "workspaces": .array([.object(["slug": .string("elsewhere"),
                                           "path": .string("/tmp/elsewhere")])]),
        ]
        controller.applyAttention(reply)

        var publishes = 0
        let cancellable = controller.objectWillChange.sink { publishes += 1 }
        controller.applyAttention(reply)
        XCTAssertEqual(publishes, 0, "an unchanged attention reply must not invalidate the window")
        controller.applyAttention(["count": .int(0), "workspaces": .array([])])
        XCTAssertEqual(publishes, 1, "a changed one must still reach the navigation's count")
        withExtendedLifetime(cancellable) {}
    }

    func testAKeyThePayloadDropsIsClearedRatherThanLeftOnScreen() {
        // **Absent means absent.** These fields used to keep whatever the engine last said about them
        // for the rest of the session, so a workspace that had gone, or a proposal queue that had
        // emptied, went on rendering as though it were still there. `status` carries every one of these
        // keys in both of its shapes, so an absent key means an engine that stopped sending it.
        let controller = makeController()
        controller.applyStatus(statusPayload())

        controller.applyStatus(["phase": .string("idle")])
        XCTAssertEqual(controller.goal, [:], "a dropped `goal` must not leave the old one on screen")
        XCTAssertEqual(controller.workspace, [:])
        XCTAssertEqual(controller.activity, [:])
        XCTAssertEqual(controller.flow, [:])
        XCTAssertEqual(controller.defaults, [:])
        XCTAssertEqual(controller.portfolio, [:])
        XCTAssertEqual(controller.mission, [:])
        XCTAssertTrue(controller.subagents.isEmpty)
        XCTAssertTrue(controller.proposals.isEmpty)
        XCTAssertNil(controller.journey)
    }

    // MARK: - The notice lapses

    func testANoticeLapsesOnItsOwnAndCanBeDismissed() {
        // `notice` was assigned by thirty call sites and cleared by none, so "run finished: …" sat in
        // the status bar for the rest of the session. The clock is passed in, so the lapse is asserted
        // at any elapsed time rather than by waiting out the production value.
        let controller = makeController()
        controller.notice = "run finished: ok"

        controller.expireNoticeIfStale(now: Date().addingTimeInterval(OrgController.defaultNoticeLifetime - 1))
        XCTAssertEqual(controller.notice, "run finished: ok", "a notice inside its lifetime stays")

        controller.expireNoticeIfStale(now: Date().addingTimeInterval(OrgController.defaultNoticeLifetime + 1))
        XCTAssertNil(controller.notice, "a notice past its lifetime must go")

        controller.notice = "waiting on you: a gate"
        controller.dismissNotice()
        XCTAssertNil(controller.notice, "the explicit dismiss clears it regardless of the clock")
    }

    func testTheEngineBecomingReadyClearsAStaleNotice() {
        // The resolving event: everything the status bar could still be saying is a statement about a
        // state that no longer holds once the engine is up.
        let controller = makeController()
        controller.notice = "engine failed: no interpreter"
        controller.handle(event("engine.ready"))
        XCTAssertNil(controller.notice)
    }

    // MARK: - The liveness clock

    func testARoutineAckDoesNotStampTheLivenessClock() async {
        // The app polls `status` every two seconds and every poll is answered by a `command.ack`, so
        // stamping `lastEventAt` before the routine filter made the poll itself the event — a `@Published`
        // change, and so a whole-window invalidation, twice a second for a heartbeat.
        let controller = makeController()
        controller.handle(event("run.end", ["outcome": .string("ok")]))
        let stamped = try? XCTUnwrap(controller.lastEventAt)

        controller.handle(event("command.ack", ["cmd_id": .string("c1"), "ok": .bool(true)]))
        XCTAssertEqual(controller.lastEventAt, stamped, "the poll's own ack is not news")

        // A *refused* ack is not routine — it is the reason a button did nothing — so it still counts.
        try? await Task.sleep(nanoseconds: 5_000_000)
        controller.handle(event("command.ack", ["cmd_id": .string("c2"), "ok": .bool(false)]))
        XCTAssertGreaterThan(controller.lastEventAt ?? .distantPast, stamped ?? .distantFuture)
    }

    // MARK: - A dismissed proposal stays dismissed

    func testADismissedProposalSurvivesTheNextPollAndARelaunch() async {
        // `dismissProposal` was local-only: the row was removed from `proposals`, and the engine — which
        // re-globs the directory on every `status` — put it back within two seconds, under the hand that
        // had just removed it.
        let dismissals = InMemoryPreferenceStore()
        let controller = makeController(dismissals: dismissals)
        await controller.dismissProposal(id: "prop_0001")

        let payload: [String: JSONValue] = [
            "proposals": .object([
                "proposals": .array([
                    .object(["proposal_id": .string("prop_0001"), "state": .string("drafted")]),
                    .object(["proposal_id": .string("prop_0002"), "state": .string("drafted")]),
                ]),
                "refused_count": .int(0), "refused": .array([]), "directory": .string(""),
            ]),
        ]
        controller.applyStatus(payload)
        XCTAssertEqual(controller.proposals.compactMap { $0["proposal_id"]?.stringValue },
                       ["prop_0002"], "the next poll must not re-offer what was dismissed")

        // "Survives a relaunch": a second controller over the same store is the relaunch, without
        // launching anything.
        let relaunched = makeController(dismissals: dismissals)
        relaunched.applyStatus(payload)
        XCTAssertEqual(relaunched.proposals.compactMap { $0["proposal_id"]?.stringValue },
                       ["prop_0002"], "a dismissal that a relaunch undid would be no dismissal at all")
    }

    func testADismissalPutsNothingInTheRealPreferencesDirectory() async throws {
        // The dismissal store is the second place a test wrote into the developer's own preferences: the
        // suite was `org.agentorg.tests.poll.<UUID>`, one real plist per run of the test above, never
        // removed. Injecting a store is only half of the claim — that it does not persist is a statement
        // about the disk, so it is measured there.
        let directory = URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/Preferences", isDirectory: true)
        let before = try preferenceFiles(in: directory)

        let controller = makeController()
        await controller.dismissProposal(id: "prop_0001")

        // A `UserDefaults` suite reaches the preference daemon out of process, so a write that should
        // not happen is given time to land: a pass then means none was made, not that none had arrived.
        Thread.sleep(forTimeInterval: 1)
        let after = try preferenceFiles(in: directory)
        XCTAssertEqual(after, before,
                       "a test dismissal must leave no file behind; it added "
                        + "\(after.subtracting(before).sorted())")
    }

    /// Every test-domain preference file, by name — the whole family this test and `AppPreferences` used
    /// to write into the real directory.
    private func preferenceFiles(in directory: URL) throws -> Set<String> {
        Set(try FileManager.default.contentsOfDirectory(atPath: directory.path)
            .filter { $0.hasPrefix("org.agentorg.") })
    }

    // MARK: - The engine's own stderr

    func testClearingTheDiagnosticsKeepsTheRuntimeLine() {
        // The terminal's clear button only cleared `LogStore`, so the Now pane's diagnostics block and
        // Setup's engine line kept the top of the session's stderr until the app was quit. The runtime
        // line is not engine output: Setup reads it *positionally* and drops the `"runtime: "` prefix.
        let controller = makeController()
        XCTAssertEqual(controller.engineDiagnostics.count, 1)
        controller.clearEngineDiagnostics()
        XCTAssertEqual(controller.engineDiagnostics.count, 1)
        XCTAssertTrue(controller.engineDiagnostics[0].hasPrefix("runtime: "),
                      "clearing must leave the header Setup reads by position: "
                        + "\(controller.engineDiagnostics)")
    }

    // MARK: - A real child process

    /// A stand-in engine that speaks enough of the protocol to be indistinguishable from the real one
    /// for what these tests assert, plus one deliberate behaviour a mock could not supply: it can
    /// *drain* before exiting.
    ///
    /// `drainSeconds` is what the app's stop handshake is written against. `terminate()` closes the
    /// command pipe first and then signals, precisely so the engine "finishes the command in flight,
    /// writes its checkpoint, and exits" — and `serve._drain(timeout_s=10.0)` is that wait, which runs
    /// on EOF. A child doing that ignores SIGTERM while it drains, which is why this stand-in does too.
    /// (The real `engine/serve.py` installs no SIGTERM handler, so whether the drain window actually
    /// opens in production depends on that; this fixes the app to be right either way.)
    ///
    /// The window matters because `.terminating` lasts until the process is *really* gone, `launch()`
    /// refuses while any live state is set, and a clean exit schedules no retry — so a relaunch issued
    /// on a fixed sleep is a no-op whenever the drain outlasts it.
    private func writeStandIn(name: String, emitsReady: Bool,
                              drainSeconds: Double) throws -> URL {
        let script = root.appendingPathComponent("\(name).py")
        let source = """
        import json, os, signal, sys, time

        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "argv-%d.json" % os.getpid()), "w") as handle:
            json.dump(sys.argv, handle)

        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()

        if \(emitsReady ? "True" : "False"):
            emit({"v": 1, "seq": 1, "type": "engine.ready",
                  "payload": {"pid": os.getpid(), "providers": []}})

        def idle_status():
            return {"phase": "idle", "running": False, "org": [], "gate": None, "outcome": {},
                    "workspace": {"path": os.getcwd(), "name": "stand-in", "attached": False},
                    "goal": {}, "mission": {}, "subagents": {"children": []},
                    "proposals": {"proposals": [], "refused_count": 0, "refused": [],
                                  "directory": ""},
                    "activity": {}, "flow": {}, "defaults": {}, "cost": {}, "cache": {},
                    "swarm": {}, "portfolio": {"orgs": [], "active_org_id": ""}}

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except Exception:
                continue
            detail = idle_status() if command.get("type") == "status" else {"accepted": True}
            emit({"v": 1, "seq": 0, "type": "command.ack",
                  "payload": {"cmd_id": command["cmd_id"], "ok": True, "detail": detail}})

        time.sleep(\(drainSeconds))
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        return script
    }

    /// Every argv the stand-in engines recorded, one file per process.
    private func launchedArgvs() -> [[String]] {
        let files = (try? FileManager.default.contentsOfDirectory(at: root,
                                                                  includingPropertiesForKeys: nil)) ?? []
        return files.filter { $0.lastPathComponent.hasPrefix("argv-") }.compactMap { url in
            guard let data = try? Data(contentsOf: url) else { return nil }
            return (try? JSONSerialization.jsonObject(with: data)) as? [String]
        }
    }

    private func waitUntil(_ deadline: TimeInterval = 10,
                           _ condition: () -> Bool) async -> Bool {
        let end = Date().addingTimeInterval(deadline)
        while Date() < end {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    private func terminalText(_ controller: OrgController) -> String {
        controller.logs.flush()
        return controller.logs.lines.map(\.text).joined(separator: "\n")
    }

    func testAttachingAProjectWhileTheEngineIsLiveRelaunchesIt() async throws {
        // The defect: `setProject` terminated the engine, slept a fixed 600 ms, and called `launch()` —
        // which refuses while *any* live state is set. `.terminating` lasts until the process is really
        // gone, and nothing relaunches on a clean exit (`.finished` does not go through the bounded
        // auto-restart), so an attach whose stop outlasted the sleep left the engine down for good.
        //
        // The stand-in drains for 1.5 s before exiting, so the transition lands long after the old
        // 600 ms sleep: this test fails on the fixed-sleep code and passes on the state-driven relaunch.
        let attached = root.appendingPathComponent("attached-repo")
        try FileManager.default.createDirectory(at: attached, withIntermediateDirectories: true)
        let script = try writeStandIn(name: "relaunch", emitsReady: true, drainSeconds: 1.5)
        let controller = makeController(arguments: ["python3", script.path])
        controller.launch()
        defer { controller.stop() }
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready, "the stand-in must reach `running` via its readiness frame")

        await controller.setProject(attached)
        XCTAssertTrue(controller.engineState.isLive,
                      "the engine is still stopping when the attach returns — which is why the launch "
                        + "cannot be issued from here")

        let relaunched = await waitUntil(12) {
            controller.engineState == .running
                && self.launchedArgvs().contains { $0.contains("--project") }
        }
        XCTAssertTrue(relaunched,
                      "attaching during a live engine must start it again once it has stopped")
        XCTAssertEqual(controller.projectPath, attached.path)
        let relaunchArgv = try XCTUnwrap(launchedArgvs().first { $0.contains("--project") })
        let index = try XCTUnwrap(relaunchArgv.firstIndex(of: "--project"))
        XCTAssertEqual(relaunchArgv[index + 1], attached.path,
                       "the relaunch must be pointed at the folder just attached, not the previous one")
    }

    func testAStuckLaunchIsReportedRatherThanRepaintedForever() async throws {
        // The engine is silent for the whole bootstrap, and this stand-in never reports ready at all —
        // the shape of a launch wedged behind a permission prompt. The row used to be cleared only by
        // readiness, a reported failure or a stop, so on this machine it sent `objectWillChange` every
        // second for the life of the process, re-evaluating every view holding the controller.
        let script = try writeStandIn(name: "never-ready", emitsReady: false, drainSeconds: 0)
        let controller = makeController(arguments: ["python3", script.path])
        controller.launch()
        defer { controller.stop() }
        XCTAssertNotNil(controller.launchProgress)

        var publishes = 0
        let cancellable = controller.objectWillChange.sink { publishes += 1 }

        let expired = await waitUntil(LaunchProgress.stuckAfter + 8) { controller.launchProgress == nil }
        XCTAssertTrue(expired,
                      "a launch past `stuckAfter` must be closed out rather than repainted for ever")
        XCTAssertLessThanOrEqual(publishes, Int(LaunchProgress.stuckAfter) + 4,
                                 "the row advances its clock once a second at most")
        // Reported, not silent: the advice names the usual cause, which is the whole reason the row
        // exists — a person watching a spinner cannot act on it.
        XCTAssertTrue(controller.notice?.contains("Still starting after") ?? false,
                      "the stuck launch must be *reported*: \(controller.notice ?? "no notice")")
        XCTAssertTrue(terminalText(controller).contains("did not report ready"))

        // And the repainting really has stopped, which is the point of an expiry rather than a longer
        // tick: after the row is gone there is nothing left to invalidate the window for.
        let afterExpiry = publishes
        try? await Task.sleep(nanoseconds: 1_500_000_000)
        XCTAssertEqual(publishes, afterExpiry, "nothing may repaint once the launch row is closed out")
        withExtendedLifetime(cancellable) {}
    }
}
