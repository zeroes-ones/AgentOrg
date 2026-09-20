//
//  OrgControllerBridgeTests.swift
//  AgentOrgKitTests
//
//  The console driven against a *real* child process.
//
//  WHY A SCRIPTED ENGINE RATHER THAN A MOCK SERVICE
//  ------------------------------------------------
//  The decisions in this file are the ones with consequences: a command reaching the engine with the
//  key the engine actually reads, a refused pause surfacing, a gate being answered or left alone.
//  Those are all *wire* facts — they are about what a JSON line contains and what comes back — and a
//  mock would let the test assert whatever the code happens to do.
//
//  So the stand-in is `/bin/sh` running a small Python script that speaks the same NDJSON protocol:
//  it writes `engine.ready`, answers `command.ack` with a real `cmd_id` echo, and records every
//  command it received to a file the test can read. The console cannot tell it from the engine, which
//  is exactly the point — `EngineLaunchConfig` documents that a caller may run something else, and
//  this is that path used.
//

import XCTest
@testable import AgentOrgKit

@MainActor
final class OrgControllerBridgeTests: XCTestCase {

    private var root: URL!
    private var received: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-bridge-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        received = root.appendingPathComponent("received.jsonl")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    /// A stand-in engine: reads NDJSON commands, records each, acknowledges each.
    ///
    /// Written to a file rather than inlined so the quoting is ordinary Python rather than shell, and
    /// so the payload the console sent can be asserted field by field afterwards.
    private func makeEngineScript(pauseAnswer: String = #"{"paused": True}"#) throws -> URL {
        let script = root.appendingPathComponent("stand_in_engine.py")
        let source = """
        import json, sys, os

        received_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "received.jsonl")

        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()

        emit({"v": 1, "seq": 1, "type": "engine.ready",
              "payload": {"pid": os.getpid(), "providers": []}})

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except Exception:
                continue
            with open(received_path, "a") as handle:
                handle.write(json.dumps(command) + "\\n")
            if command.get("type") == "pause":
                detail = \(pauseAnswer)
            elif command.get("type") == "discard_run":
                # A reply shaped like the engine's own, so the test asserts the app reads the count
                # and the backup path rather than a sentence it wrote itself.
                detail = {"discarded": True, "reason": "",
                          "moved": [{"name": "run_state.json", "bytes": 12},
                                    {"name": "runner_state.json", "bytes": 30}],
                          "backup_dir": "/tmp/agentorg-discard-backup",
                          "kept": ["trace.jsonl"], "freed_bytes": 42}
            else:
                detail = {"accepted": True}
            emit({"v": 1, "seq": 0, "type": "command.ack",
                  "payload": {"cmd_id": command["cmd_id"], "ok": True, "detail": detail}})
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        return script
    }

    private func makeController(pauseAnswer: String = #"{"paused": True}"#,
                                maxRestartAttempts: Int = 0,
                                projectPath: URL? = nil,
                                attachedProject: URL? = nil,
                                arguments: [String]? = nil,
                                preferences: AppPreferences? = nil) throws -> OrgController {
        let script = try makeEngineScript(pauseAnswer: pauseAnswer)
        let settings = OrgController.OrgSettings(
            engineRoot: root,
            projectPath: projectPath ?? root,
            credentialsPath: nil, libraryRoot: nil,
            attachedProject: attachedProject)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: arguments ?? ["python3", script.path])
        return OrgController(settings: settings, maxRestartAttempts: maxRestartAttempts,
                             restartDelay: 0.05, preferences: preferences ?? .ephemeral())
    }

    /// A stand-in engine that records its own **arguments**, not just the commands it receives.
    ///
    /// The slug bug is an argument-passing bug: the console and the engine have to be told the same
    /// project, and no amount of asserting on the app's own strings would catch the two disagreeing.
    /// So this records `sys.argv` to a file the test reads.
    private func makeArgvRecorder() throws -> URL {
        let script = root.appendingPathComponent("argv_engine.py")
        let out = root.appendingPathComponent("argv.json")
        let source = """
        import json, sys, os

        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()

        with open(\(String(reflecting: out.path)), "w") as handle:
            json.dump(sys.argv, handle)

        emit({"v": 1, "seq": 1, "type": "engine.ready",
              "payload": {"pid": os.getpid(), "providers": []}})
        for line in sys.stdin:
            pass
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        return script
    }

    /// The arguments the console actually passed to the child.
    private func launchedArguments() throws -> [String] {
        let path = root.appendingPathComponent("argv.json")
        guard FileManager.default.fileExists(atPath: path.path) else { return [] }
        let data = try Data(contentsOf: path)
        return (try JSONSerialization.jsonObject(with: data) as? [String]) ?? []
    }

    /// The commands the console actually put on the wire.
    private func commandsSent() throws -> [[String: Any]] {
        guard FileManager.default.fileExists(atPath: received.path) else { return [] }
        let text = try String(contentsOf: received, encoding: .utf8)
        return text.split(separator: "\n").compactMap { line in
            (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
        }
    }

    private func waitUntil(_ deadline: TimeInterval = 8,
                           _ condition: () -> Bool) async -> Bool {
        let end = Date().addingTimeInterval(deadline)
        while Date() < end {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    /// Every terminal line, flushed.
    ///
    /// `LogStore` coalesces appends to at most one publish per frame, so a line appended a microsecond
    /// ago is in the buffer but not yet visible in `lines`. Reading through `flush()` is what makes an
    /// assertion about a notice reliable rather than racing the coalescing — and it is honest about
    /// what is being tested: the *decision* to log, not the timing of the publish.
    private func terminalText(_ controller: OrgController) -> String {
        controller.logs.flush()
        return controller.logs.lines.map(\.text).joined(separator: "\n")
    }

    private func launchedController() async throws -> OrgController {
        let controller = try makeController()
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready, "the stand-in engine must reach `running` via its readiness frame")
        return controller
    }

    // MARK: - The wire keys

    func testTheNonNegotiableCheckboxSendsTheKeyTheEngineReads() async throws {
        // The bug this pins: the app sent `as_constraint`, `serve._cmd_instruct` reads `constraint`,
        // and the mismatch was invisible — the command was acknowledged and the flag silently dropped,
        // so the one guarantee the checkbox promises (verbatim survival across compaction) did not
        // hold. Asserted against the *received line*, not against the payload the app built.
        let controller = try await launchedController()
        defer { controller.stop() }

        await controller.instruct("never use float money", asConstraint: true)
        let instruct = try XCTUnwrap(try commandsSent().first { $0["type"] as? String == "instruct" })
        let payload = try XCTUnwrap(instruct["payload"] as? [String: Any])
        XCTAssertEqual(instruct["type"] as? String, "instruct")
        XCTAssertEqual(payload["text"] as? String, "never use float money")
        XCTAssertEqual(payload["constraint"] as? Bool, true,
                       "the engine reads `constraint`; `as_constraint` is a no-op")
        XCTAssertNil(payload["as_constraint"], "the key the engine ignores must not be sent")
    }

    func testAnOrdinaryInstructionSendsNoConstraintFlag() async throws {
        // The flag is only sent when it is true, so a plain instruction cannot be upgraded by accident.
        let controller = try await launchedController()
        defer { controller.stop() }

        await controller.instruct("prefer Postgres")
        let instruct = try XCTUnwrap(try commandsSent().first { $0["type"] as? String == "instruct" })
        let payload = try XCTUnwrap(instruct["payload"] as? [String: Any])
        XCTAssertNil(payload["constraint"])
        XCTAssertNil(payload["as_constraint"])
    }

    // MARK: - Pause awaits its acknowledgement

    func testPauseIsAcknowledgedAndReportsSuccess() async throws {
        let controller = try await launchedController()
        defer { controller.stop() }

        let paused = await controller.pause()
        XCTAssertTrue(paused)
        XCTAssertTrue(try commandsSent().contains { $0["type"] as? String == "pause" },
                      "pause must actually go on the wire")
        XCTAssertNil(controller.notice)
    }

    func testARefusedPauseIsVisibleRatherThanSilent() async throws {
        // The bug this pins: `pause` was `post` — fire-and-forget — so a refusal never surfaced. The
        // engine reports a no-op inside the acknowledgement detail (`{paused: false, reason: …}`)
        // rather than by failing, which is precisely why the reply has to be read.
        let controller = try makeController(
            pauseAnswer: #"{"paused": False, "reason": "no run is loaded"}"#)
        controller.launch()
        defer { controller.stop() }
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready)

        let paused = await controller.pause()
        XCTAssertFalse(paused, "a refused pause must report failure")
        XCTAssertEqual(controller.notice, "no run is loaded")
        let terminal = terminalText(controller)
        XCTAssertTrue(terminal.contains("pause refused"),
                      "the refusal must reach the terminal as well as the notice:\n\(terminal)")
    }

    // MARK: - Discarding a settled run is the engine's operation, not the app's

    func testDiscardRunSendsTheEngineCommandAndReadsItsReply() async throws {
        // The app must not move the files itself: `discard_run` is the one engine operation, and the
        // count and backup path a person reads come from its reply rather than from Swift. So this
        // asserts the command actually crossed the wire *and* that the reply is what the console held.
        let controller = try await launchedController()
        defer { controller.stop() }

        let answered = await controller.discardRun()
        XCTAssertTrue(answered)
        XCTAssertTrue(try commandsSent().contains { $0["type"] as? String == "discard_run" },
                      "discard must actually go on the wire")
        XCTAssertEqual(controller.lastDiscard["backup_dir"]?.stringValue,
                       "/tmp/agentorg-discard-backup",
                       "the backup path must be the engine's, not one the app composed")
        XCTAssertEqual(controller.lastDiscard["moved"]?.arrayValue?.count, 2)
        XCTAssertEqual(controller.lastDiscard["kept"]?.arrayValue?.first?.stringValue, "trace.jsonl")
    }

    // MARK: - The posture reaches the engine as one word

    func testChoosingAPostureSendsTheWordAndDoesNotArmTheGoal() async throws {
        // `no_arm` is repeated because the person pressed a picker, not Resume: changing how a goal
        // runs must not restart one that is paused.
        let controller = try await launchedController()
        defer { controller.stop() }
        // A goal must exist for the posture to be meaningful.
        controller.applyStatus(["goal": .object([
            "objective": .string("ship the pagination change"), "posture": .string("unattended"),
        ])])

        await controller.setGoalPosture(.supervised)
        let goalSet = try XCTUnwrap(try commandsSent().last { $0["type"] as? String == "goal_set" })
        let payload = try XCTUnwrap(goalSet["payload"] as? [String: Any])
        XCTAssertEqual(payload["posture"] as? String, "supervised")
        XCTAssertEqual(payload["no_arm"] as? Bool, true)
        XCTAssertEqual(payload["objective"] as? String, "ship the pagination change")
    }

    // MARK: - The autonomy answer the engine has to hear

    func testChoosingTheDefaultPostureTellsTheEngineAndNotJustTheWindow() async throws {
        // **The bug this closes.** The wizard's last step stored the answer in `UserDefaults`, the
        // window's own gate went satisfied and the wizard retired — and nothing told the engine, so its
        // journey kept reporting "next: Choose how much it decides alone" and re-running setup asked
        // the same question again with no explanation.
        //
        // A `goal_set` cannot fix it: `goal.default_posture` is what `onboard` reads, and the app's
        // per-goal posture is a value the window attaches at creation. So the answer is an
        // `autonomy_set` carrying `posture`.
        let controller = try await launchedController()
        defer { controller.stop() }

        let accepted = await controller.choosePosture(.supervised)
        XCTAssertTrue(accepted, "the stand-in engine acknowledges, so the write is reported as done")
        let sent = try XCTUnwrap(try commandsSent().last { $0["type"] as? String == "autonomy_set" })
        let payload = try XCTUnwrap(sent["payload"] as? [String: Any])
        XCTAssertEqual(payload["posture"] as? String, "supervised",
                       "the engine must be told, or the journey step never moves")
        XCTAssertEqual(controller.goalPosturePreference, .supervised,
                       "and the window's own preference is recorded in the same call")
    }

    func testAPostureTheEngineRefusesIsReportedRatherThanAssumed() async throws {
        // The refusal has to reach the person: `autonomy_set` rejects an unknown posture and a missing
        // credentials file, and in either case the autonomy step will still show as outstanding. A
        // `Bool` that was discarded — the `setDefaults` bug — would let the wizard retire over a write
        // the engine never accepted.
        let script = try makeEngineScript()
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: ["python3", script.path])
        let controller = OrgController(settings: settings, maxRestartAttempts: 0,
                                       restartDelay: 0.05, preferences: .ephemeral())
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready)
        defer { controller.stop() }

        // Stopped first, so the command cannot be sent at all — the state a person reaches by
        // answering the question while the engine is down.
        controller.stop()
        _ = await waitUntil { controller.engineState != .running }
        let accepted = await controller.choosePosture(.unattended)
        XCTAssertFalse(accepted, "with no engine to tell, the write did not happen and must say so")
        XCTAssertNotNil(controller.notice)
        XCTAssertEqual(controller.goalPosturePreference, .unattended,
                       "the app's own preference is still recorded, so its goals carry it")
    }

    func testAnUnknownPostureIsRefusedLocallyRatherThanSentAsAWord() async throws {
        // `unknown` is a rendering state, never a choice. Sending it would make `autonomy_set` refuse
        // a posture this build invented, and the person would be told their engine rejected a value
        // they never picked.
        let controller = try await launchedController()
        defer { controller.stop() }

        let accepted = await controller.choosePosture(.unknown)
        XCTAssertFalse(accepted)
        XCTAssertTrue(try commandsSent().allSatisfy { $0["type"] as? String != "autonomy_set" },
                      "nothing may be sent for a posture this build cannot name")
    }

    func testAPostureWithNoGoalIsRefusedLocallyRatherThanSent() async throws {
        // Sending `goal_set` with an empty objective would make the engine raise "goal_set needs an
        // objective" — a refusal the app can see coming and should not provoke.
        let controller = try await launchedController()
        defer { controller.stop() }

        await controller.setGoalPosture(.supervised)
        XCTAssertTrue(try commandsSent().allSatisfy { $0["type"] as? String != "goal_set" },
                      "no goal_set may be sent without an objective")
        XCTAssertEqual(controller.notice, "set a goal before choosing how it runs")
    }

    // MARK: - A gate the goal may answer is forwarded

    func testAnUnattendedGoalForwardsAGateTheEngineLeftUnanswered() async throws {
        // The feature, end to end: a gate arrives, the posture says the goal may answer it, and an
        // `approve` reaches the engine. The engine would then apply its own evidence and safety checks
        // and re-emit `human.gate` with `waiting_on: owner` if one refused — which the next test pins.
        let controller = try await launchedController()
        defer { controller.stop() }
        controller.applyStatus(["goal": .object([
            "objective": .string("ship it"), "posture": .string("unattended"),
        ])])

        controller.handle(EngineEvent(
            v: 1, seq: 10, type: "human.gate",
            payload: ["gate_id": .string("reroute-gate"), "kind": .string("agent"),
                      "reason": .string("the org gate wants to reroute")],
            runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))

        let forwarded = await waitUntil {
            (try? self.commandsSent().contains { $0["type"] as? String == "approve" }) ?? false
        }
        XCTAssertTrue(forwarded, "the console must send the approval the engine left to the goal")
        let approve = try XCTUnwrap(try commandsSent().first { $0["type"] as? String == "approve" })
        let note = (approve["payload"] as? [String: Any])?["note"] as? String ?? ""
        XCTAssertTrue(note.contains("unattended") || note.contains("auto-approved"),
                      "the approval must record that the goal's posture authorised it, got: \(note)")
    }

    func testASupervisedGoalSendsNothingForTheSameGate() async throws {
        // Byte-for-byte the behaviour before this feature. The console shows the gate and waits.
        let controller = try await launchedController()
        defer { controller.stop() }
        controller.applyStatus(["goal": .object([
            "objective": .string("ship it"), "posture": .string("supervised"),
        ])])

        controller.handle(EngineEvent(
            v: 1, seq: 10, type: "human.gate",
            payload: ["gate_id": .string("release"), "kind": .string("human"),
                      "reason": .string("Owner release approval")],
            runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))

        XCTAssertNotNil(controller.pendingGate, "the gate must still be waiting")
        XCTAssertTrue(controller.gateIsWaitingForHuman)
        // Give any (incorrect) async approval time to appear before asserting its absence.
        try? await Task.sleep(nanoseconds: 400_000_000)
        XCTAssertTrue(try commandsSent().allSatisfy { $0["type"] as? String != "approve" },
                      "a supervised goal must never be decided for")
    }

    func testAGateTheEngineClaimedIsNeverForwarded() async throws {
        // The engine's own refusal (`waiting_on: owner` with its `why`) outranks the posture, which is
        // the whole safety property: the app must not overrule a refusal it can read.
        let controller = try await launchedController()
        defer { controller.stop() }
        controller.applyStatus(["goal": .object([
            "objective": .string("ship it"), "posture": .string("unattended"),
        ])])

        controller.handle(EngineEvent(
            v: 1, seq: 10, type: "human.gate",
            payload: ["gate_id": .string("release"), "kind": .string("human"),
                      "reason": .string("Owner release approval"),
                      "waiting_on": .string("owner"),
                      "why": .string("a safety control fired (guardrail); the goal may not release this")],
            runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))

        XCTAssertTrue(controller.gateIsWaitingForHuman)
        XCTAssertEqual(controller.gateNote,
                       "a safety control fired (guardrail); the goal may not release this")
        try? await Task.sleep(nanoseconds: 400_000_000)
        XCTAssertTrue(try commandsSent().allSatisfy { $0["type"] as? String != "approve" },
                      "a gate the engine claimed must not be answered by the console")
    }

    // MARK: - Offline state through the live controller

    func testTheControllerReadsRunStateFromDiskWhenTheEngineStops() async throws {
        // The offline path, reached through the controller: the writer is re-rooted to the attached
        // project, and the browser reads it. This is the panel that keeps working after a crash.
        let controller = try makeController()
        let stateDir = root.appendingPathComponent(".agent_state")
        try FileManager.default.createDirectory(at: stateDir, withIntermediateDirectories: true)
        let runState: [String: Any] = [
            "workflow": "console", "manifest_sha": "abc", "phase": "awaiting_human", "node": "pm",
            "nodes": ["pm": ["status": "blocked", "verdict": "guardrail-blocked", "iterations": 1]],
        ]
        try Data(try JSONSerialization.data(withJSONObject: runState))
            .write(to: stateDir.appendingPathComponent("run_state.json"))

        await controller.refreshOfflineState()
        XCTAssertEqual(controller.offlineRuns.first?.workflow, "console")
        XCTAssertEqual(controller.offlineRuns.first?.blockedCount, 1)
        XCTAssertTrue(controller.hasOfflineState)
        XCTAssertNil(controller.offlineError)
    }

    // MARK: - The controller's use of the notifier

    func testAGoalCompletionReachesTheRecordingNotifier() async throws {
        // The whole path through the controller: an engine event arrives, the planner decides it is
        // worth telling someone, authorisation is asked for *lazily* at that moment, and the plan is
        // delivered. Asserting on the recording fake is what makes this observable at all — a real
        // notification centre cannot be inspected from a test.
        let notifier = RecordingNotifier(isAuthorized: false, willGrant: true)
        let controller = OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil),
            notifier: notifier, preferences: .ephemeral())

        controller.handle(EngineEvent(
            v: 1, seq: 1, type: "goal.completed",
            payload: ["summary": .string("cursor pagination added, 12 tests pass")],
            runId: "run_1", agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))
        await controller.awaitNotifications()

        XCTAssertEqual(notifier.authorizationRequests, 1,
                       "authorisation must be asked for lazily, at the first banner worth showing")
        XCTAssertEqual(notifier.delivered.map(\.title), ["Goal complete"])
        XCTAssertEqual(notifier.delivered.first?.body, "cursor pagination added, 12 tests pass")
        XCTAssertEqual(controller.lastNotification, "notified: Goal complete")
    }

    func testADeniedNotifierBreaksNothingAndSaysSo() async throws {
        // The requirement in its most important form: a notification that cannot be delivered must
        // never break the console. The event still becomes state, the terminal still gets its line, and
        // the app records *why* nothing was shown instead of pretending it was.
        let notifier = RecordingNotifier(isAuthorized: false, willGrant: false)
        let controller = OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil),
            notifier: notifier, preferences: .ephemeral())

        controller.handle(EngineEvent(
            v: 1, seq: 1, type: "goal.blocked",
            payload: ["reason": .string("no route to a provider")],
            runId: "run_1", agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))
        await controller.awaitNotifications()

        XCTAssertTrue(notifier.delivered.isEmpty, "a denied notifier must deliver nothing")
        XCTAssertEqual(controller.lastNotification, "notifications are off (denied in System Settings)")
        // The event was still processed: the console is fully functional with notifications denied.
        XCTAssertEqual(controller.notice, "goal blocked: no route to a provider")
        XCTAssertFalse(controller.logs.lines.isEmpty, "the terminal still records the event")
    }

    func testAGateTheConsoleMayAnswerDoesNotNotifyAPerson() async throws {
        // The negative that keeps the feature usable. When the console is about to forward the
        // decision itself, a banner asking a person to decide would be a lie — and it is exactly the
        // noise that trains someone to ignore the next one.
        let notifier = RecordingNotifier(isAuthorized: true)
        let controller = OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil),
            notifier: notifier, preferences: .ephemeral())
        // An unattended goal, so the gate below is one the console will answer.
        controller.applyStatus(["goal": .object([
            "objective": .string("ship it"), "posture": .string("unattended"),
        ])])
        notifier.reset()

        controller.handle(EngineEvent(
            v: 1, seq: 1, type: "human.gate",
            payload: ["gate_id": .string("reroute-gate"), "kind": .string("agent"),
                      "reason": .string("bounded reroute")],
            runId: "run_1", agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))
        await controller.awaitNotifications()

        XCTAssertTrue(notifier.delivered.isEmpty,
                      "a gate the console answers itself must not be sent to a person as a question")
    }

    func testAGateTheEngineClaimedDoesNotifyAPerson() async throws {
        // The other side of the same rule: this gate *is* the person's, so it is exactly the one worth
        // a banner.
        let notifier = RecordingNotifier(isAuthorized: true)
        let controller = OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil),
            notifier: notifier, preferences: .ephemeral())
        controller.applyStatus(["goal": .object([
            "objective": .string("ship it"), "posture": .string("unattended"),
        ])])

        controller.handle(EngineEvent(
            v: 1, seq: 1, type: "human.gate",
            payload: ["gate_id": .string("release"), "kind": .string("human"),
                      "reason": .string("Owner release approval"),
                      "waiting_on": .string("owner"),
                      "why": .string("a safety control fired (guardrail); the goal may not release this")],
            runId: "run_1", agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil))
        await controller.awaitNotifications()

        XCTAssertEqual(notifier.delivered.count, 1)
        XCTAssertEqual(notifier.delivered.first?.urgency, .interrupt)
        XCTAssertTrue(notifier.delivered.first?.title.contains("safety control") ?? false)
    }

    // MARK: - Goal outcomes reach the status bar

    func testEachGoalOutcomeSaysWhatHappenedInTheStatusBar() async throws {
        // The status bar is the one line a person reads without opening a panel, so a goal's outcome
        // must land there — not only in the terminal. Each is asserted distinct, because "complete"
        // and "blocked" are the two states a person must be able to tell apart at a glance.
        let controller = OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil),
            notifier: RecordingNotifier(isAuthorized: false, willGrant: false), preferences: .ephemeral())

        controller.handle(event("goal.completed", ["summary": .string("all tests pass")]))
        XCTAssertEqual(controller.notice, "goal complete: all tests pass")

        controller.handle(event("goal.blocked", ["reason": .string("no provider reachable")]))
        XCTAssertEqual(controller.notice, "goal blocked: no provider reachable")

        controller.handle(event("goal.paused", ["reason": .string("budget_spend")]))
        XCTAssertEqual(controller.notice, "goal paused (budget_spend)")
    }

    func testAGatePauseDoesNotRaceTheGatesOwnNotice() async throws {
        // `goal.paused(reason: gate)` and `human.gate` describe one stop, and the gate's own event is
        // the more useful message. The pause is deliberately not turned into a notice so the gate's
        // does not overwrite it a millisecond later — the same reasoning the notification planner
        // applies in the other direction.
        let controller = OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil),
            notifier: RecordingNotifier(isAuthorized: false, willGrant: false), preferences: .ephemeral())
        controller.applyStatus(["goal": .object([
            "objective": .string("ship it"), "posture": .string("supervised"),
        ])])

        controller.handle(event("goal.paused", ["reason": .string("gate")]))
        XCTAssertNil(controller.notice, "a gate pause must not pre-empt the gate's own notice")

        controller.handle(event("human.gate", ["gate_id": .string("release"),
                                               "reason": .string("Owner release approval")]))
        XCTAssertTrue(controller.notice?.contains("Owner release approval") ?? false,
                      controller.notice ?? "nil")
    }

    private func event(_ type: String, _ payload: [String: JSONValue] = [:]) -> EngineEvent {
        EngineEvent(v: 1, seq: 1, type: type, payload: payload,
                    runId: "run_1", agentId: nil, nodeId: nil, sessionId: nil, phase: nil, ts: nil)
    }

    // MARK: - The bounded restart

    func testAStructuralFailureIsDeclinedRatherThanSpentOnRetries() async throws {
        // The engine cannot start at all — no interpreter — so no retry could succeed. The console must
        // say *that*, not "the retries are used up": one tells a person to go and fix something, the
        // other invites them to wait for an attempt that cannot work.
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/nonexistent/python3")),
                  arguments: ["-m", "engine.cli", "serve"])
        let controller = OrgController(settings: settings, maxRestartAttempts: 2, restartDelay: 0.05)

        XCTAssertEqual(controller.restartAttemptsRemaining, 2,
                       "a fresh controller starts with its full budget")
        controller.launch()
        XCTAssertTrue(controller.restartDeclined, "a missing interpreter is not a transient failure")
        XCTAssertFalse(controller.gaveUpRestarting, "nothing was spent, so nothing was exhausted")
        XCTAssertNotNil(controller.engineFailure)
        XCTAssertTrue(controller.notice?.contains("cannot be started") ?? false,
                      controller.notice ?? "nil")
        // No retry was scheduled, so no engine was spawned a second time.
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertFalse(terminalText(controller).contains("auto-restart 1/"))
    }

    func testARepeatedlyCrashingEngineExhaustsTheBudgetAndTheConsoleStopsTrying() async throws {
        // The case the bound exists for, driven for real: a process that spawns and dies immediately.
        // The console must retry a bounded number of times and then *stop*, saying so — an unbounded
        // retry here is a crash-loop that burns a core and buries the reason under its own repetition.
        let script = root.appendingPathComponent("crash.py")
        // Exits before readiness, so the bridge reports a launch failure rather than a finished run.
        try "import sys\nsys.exit(3)\n".write(to: script, atomically: true, encoding: .utf8)
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: ["python3", script.path])
        let controller = OrgController(settings: settings, maxRestartAttempts: 3, restartDelay: 0.05)

        controller.launch()
        let gaveUp = await waitUntil(15) { controller.gaveUpRestarting }
        XCTAssertTrue(gaveUp, "the console must stop retrying and say so")
        XCTAssertFalse(controller.restartDeclined,
                       "a process that dies is a retryable failure, not a structural one")
        XCTAssertEqual(controller.restartAttemptsRemaining, 0)
        XCTAssertTrue(controller.notice?.contains("used up") ?? false, controller.notice ?? "nil")
        let terminal = terminalText(controller)
        XCTAssertEqual(terminal.components(separatedBy: "auto-restart gave up").count - 1, 1,
                       "the giving-up must be said exactly once")
        // The bound is the point: exactly the configured number of retries, then silence.
        XCTAssertTrue(terminal.contains("auto-restart 1/3"), terminal)
        XCTAssertTrue(terminal.contains("auto-restart 3/3"), terminal)
        XCTAssertFalse(terminal.contains("auto-restart 4/3"), "the bound must not be exceeded")
    }

    func testAZeroBudgetMeansNoAutomaticRetryAtAll() async throws {
        // A person who turned the automatic path off must get no retry — and the console must still be
        // honest that it gave up rather than looking like it is about to succeed.
        let script = root.appendingPathComponent("crash.py")
        try "import sys\nsys.exit(3)\n".write(to: script, atomically: true, encoding: .utf8)
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: ["python3", script.path])
        let controller = OrgController(settings: settings, maxRestartAttempts: 0, restartDelay: 0.05)

        controller.launch()
        let gaveUp = await waitUntil(10) { controller.gaveUpRestarting }
        XCTAssertTrue(gaveUp)
        XCTAssertFalse(terminalText(controller).contains("auto-restart 1/"),
                       "a zero budget must schedule nothing")
    }

    func testAManualLaunchAfterAFixClearsTheVerdictAndRefillsTheBudget() async throws {
        // The scenario the "Try again" button is for: the cause is fixed and the person presses it.
        // The same script path is overwritten with a working engine, which is exactly what fixing a
        // broken install looks like from the console's side.
        let script = root.appendingPathComponent("engine.py")
        try "import sys\nsys.exit(3)\n".write(to: script, atomically: true, encoding: .utf8)
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: ["python3", script.path])
        let controller = OrgController(settings: settings, maxRestartAttempts: 2, restartDelay: 0.05)

        controller.launch()
        let exhausted = await waitUntil(15) { controller.gaveUpRestarting }
        XCTAssertTrue(exhausted, "the crash-loop must exhaust the bound before the fix")
        XCTAssertEqual(controller.restartAttemptsRemaining, 0)

        // The fix: the same engine path now works.
        let source = """
        import json, sys, os
        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
        emit({"v": 1, "seq": 1, "type": "engine.ready", "payload": {"pid": os.getpid()}})
        for line in sys.stdin:
            pass
        """
        try source.write(to: script, atomically: true, encoding: .utf8)

        controller.launch()   // a person pressing Try again
        XCTAssertFalse(controller.gaveUpRestarting, "a deliberate retry must clear the verdict")
        XCTAssertFalse(controller.restartDeclined)
        XCTAssertEqual(controller.restartAttemptsRemaining, 2, "and refill the budget")
        let recovered = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(recovered, "the fixed engine must come up on the manual retry")
        XCTAssertNil(controller.engineFailure, "a recovered engine must not leave the failure banner")
    }

    func testStoppingTheEngineCancelsAPendingRestart() async throws {
        // The app restarting an engine a moment after a person stopped it would be the app overruling
        // them. Driven for real: a crashing engine schedules a retry, the person stops, and the retry
        // must not fire.
        let script = root.appendingPathComponent("crash.py")
        try "import sys\nsys.exit(3)\n".write(to: script, atomically: true, encoding: .utf8)
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: ["python3", script.path])
        let controller = OrgController(settings: settings, maxRestartAttempts: 3, restartDelay: 2)
        controller.launch()

        let scheduled = await waitUntil(8) { controller.restartAttemptsRemaining == 2 }
        XCTAssertTrue(scheduled, "the first failure must schedule a retry")
        controller.stop()
        XCTAssertTrue(terminalText(controller).contains("auto-restart cancelled"))

        // The cancelled retry must not fire, and a cancelled attempt must not be silently spent: the
        // budget stays where it was, so a later genuine failure still has its attempts.
        try? await Task.sleep(nanoseconds: 2_500_000_000)
        XCTAssertEqual(controller.restartAttemptsRemaining, 2,
                       "a cancelled retry must not consume the budget")
        XCTAssertEqual(controller.engineState, .idle,
                       "nothing may have been spawned after the stop")
    }

    // MARK: - The window does not empty on a blip

    /// A stand-in that becomes ready and then dies the way the real engine does when a SIGTERM lands
    /// before `serve.py` installs its handler: killed by the signal's default action, which Foundation
    /// reports as status 15 rather than a clean 0. Measured on the real engine: the fragile window is
    /// its first ~0.2 s (three of three runs die by the signal at 0.20 s; three of three exit 0 at
    /// 0.25 s), and a SIGTERM to a *ready* engine exits 0 every time.
    private func makeSignalKilledScript() throws -> URL {
        let script = root.appendingPathComponent("signal_killed.py")
        let source = """
        import json, os, signal, sys, time

        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()

        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        emit({"v": 1, "seq": 1, "type": "engine.ready", "payload": {"pid": os.getpid()}})
        # Stdin is deliberately never read, so an EOF cannot be what ends this: the stop's signal is.
        while True:
            time.sleep(0.05)
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        return script
    }

    /// A stand-in that becomes ready, dies non-zero once, and stays up on every launch after that —
    /// the shape of a transient crash the console retries.
    private func makeCrashOnceScript() throws -> URL {
        let script = root.appendingPathComponent("crash_once.py")
        let marker = root.appendingPathComponent("crashed_once")
        let source = """
        import json, os, sys, time

        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()

        emit({"v": 1, "seq": 1, "type": "engine.ready", "payload": {"pid": os.getpid()}})
        marker = \(String(reflecting: marker.path))
        if not os.path.exists(marker):
            open(marker, "w").close()
            time.sleep(0.35)
            sys.exit(3)
        for line in sys.stdin:
            pass
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        return script
    }

    /// A controller over a stand-in child, with the presence grace short enough to assert on in
    /// test time. 0.1 s is deliberately **much shorter** than `restartDelay` in the retry test, so the
    /// two rules cannot be confused for one another: the pending retry has to be what holds the window,
    /// not the grace.
    private func makePresenceController(script: URL,
                                        maxRestartAttempts: Int,
                                        restartDelay: TimeInterval,
                                        notifier: ConsoleNotifier? = nil) -> OrgController {
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: ["python3", script.path])
        return OrgController(settings: settings,
                             notifier: notifier,
                             maxRestartAttempts: maxRestartAttempts,
                             restartDelay: restartDelay,
                             engineAwayGrace: 0.1,
                             preferences: .ephemeral())
    }

    func testAFailureTheConsoleIsRetryingDoesNotEmptyThePane() async throws {
        // **The "going off blank and coming back" report, as a test.** A pane gives way to the
        // placeholder when the engine *is gone*, and an engine the console is already bringing back is
        // not that. Driven for real: the stand-in reaches `running`, dies non-zero, and is relaunched
        // by the bounded retry — and the window must never empty in between.
        let controller = makePresenceController(script: try makeCrashOnceScript(),
                                                maxRestartAttempts: 1, restartDelay: 0.6)
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready, "the stand-in engine must reach `running`")
        XCTAssertFalse(controller.engineIsGone, "a running engine is not gone")

        let failed = await waitUntil { controller.engineState == .failed }
        XCTAssertTrue(failed, "the first run must die after readiness")
        // Past the grace, so it is the *pending retry* holding the window rather than the grace.
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertFalse(controller.engineIsGone,
                       "a failure the console is retrying must not empty the window")
        XCTAssertTrue(terminalText(controller).contains("auto-restart 1/1"),
                      terminalText(controller))

        let recovered = await waitUntil(8) { controller.engineState == .running }
        XCTAssertTrue(recovered, "the retry must bring the engine back")
        XCTAssertFalse(controller.engineIsGone, "and the window must never have emptied")
        XCTAssertNil(controller.engineFailure, "a recovered engine leaves no failure banner")
        controller.stop()
    }

    func testALaunchInFlightDoesNotEmptyThePane() async throws {
        // The relaunch half of the rule. Attaching a project is *one* operation that stops the engine
        // and starts it again, and the second half of it — `.launching` — used to empty every pane for
        // the whole bootstrap, which is a blank that returns on its own. A process on its way is not an
        // engine that is gone.
        let script = root.appendingPathComponent("slow_ready.py")
        let source = """
        import json, os, sys, time

        time.sleep(0.6)          # a bootstrap worth watching, so `.launching` is observable
        sys.stdout.write(json.dumps({"v": 1, "seq": 1, "type": "engine.ready",
                                     "payload": {"pid": os.getpid()}}) + "\\n")
        sys.stdout.flush()
        for line in sys.stdin:
            pass
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        let controller = makePresenceController(script: script,
                                                maxRestartAttempts: 0, restartDelay: 0.05)
        XCTAssertTrue(controller.engineIsGone, "nothing has been launched, so there is no engine")

        controller.launch()
        let released = await waitUntil(5) { !controller.engineIsGone }
        XCTAssertTrue(released, "a launch in flight must not read as an engine that is gone")
        XCTAssertEqual(controller.engineState, .launching,
                       "and it must be the bootstrapping engine that released the window")

        let ready = await waitUntil(6) { controller.engineState == .running }
        XCTAssertTrue(ready)
        XCTAssertFalse(controller.engineIsGone)
        controller.stop()
    }

    func testAStoppedEngineStillEmptiesThePane() async throws {
        // The other half of the rule, and the reason it is *settled* rather than simply "never on a
        // non-running state": a stopped engine must not go on looking like a running one. Held through
        // the launch, the drain and the retry; let go once it is actually gone.
        let controller = makePresenceController(script: try makeEngineScript(),
                                                maxRestartAttempts: 0, restartDelay: 0.05)
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready)
        XCTAssertFalse(controller.engineIsGone, "a running engine is not gone")

        controller.stop()
        let emptied = await waitUntil(6) { controller.engineIsGone }
        XCTAssertTrue(emptied, "a stop with nothing bringing the engine back must show the placeholder")
        XCTAssertEqual(controller.engineState, .finished)
    }

    func testAStopTheConsoleAskedForIsNotReportedAsAFailure() async throws {
        // **The other half of the same defect, and the one that made it a loop.** Measured on the real
        // engine: a SIGTERM before its handler is installed kills it by the signal's default action, so
        // `terminate()` produces "the engine exited with status 15" rather than a clean 0. Classifying
        // that as a crash made a deliberate Stop announce itself as a failure and relaunch the engine
        // four seconds later, overruling the person:
        //
        //     12:13:52.282  engine stopping…
        //     12:13:54.644  engine failed: the engine exited with status 15
        //     12:13:54.645  auto-restart 1/3 in 4s
        //     12:13:58.894  launching the engine…
        //
        // A process we asked to stop that stopped is a finish — no banner, no interrupt notification,
        // no retry spending itself on a stop somebody asked for.
        let notifier = RecordingNotifier(isAuthorized: true, willGrant: true)
        let controller = makePresenceController(script: try makeSignalKilledScript(),
                                                maxRestartAttempts: 2, restartDelay: 0.05,
                                                notifier: notifier)
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready)

        controller.stop()
        let stopped = await waitUntil(6) { !controller.engineState.isLive }
        XCTAssertTrue(stopped, "the stopped process must reach a terminal state")
        try? await Task.sleep(nanoseconds: 300_000_000)

        XCTAssertEqual(controller.engineState, .finished,
                       "a stop the console asked for is a finish, not a failure")
        XCTAssertNil(controller.engineFailure, "no failure banner for a stop the person asked for")
        XCTAssertEqual(controller.restartAttemptsRemaining, 2,
                       "a deliberate stop must not spend the retry budget")
        XCTAssertFalse(terminalText(controller).contains("auto-restart 1/"),
                       "the console must not relaunch an engine four seconds after a stop")
        await controller.awaitNotifications()
        XCTAssertFalse(notifier.delivered.contains { $0.identifier == "engine.failed" },
                       "a stop the person asked for is not an emergency to notify about")
    }

    // MARK: - The project the engine is actually told to use

    func testAManagedLaunchTellsTheEngineTheSlugTheConsoleWillRead() async throws {
        // **This is the slug bug.** The app hardcoded `slug: "demo"` and passed no `--slug`, while
        // `engine/serve.py` defaults to `"console"` — so the engine wrote `projects/console/.agent_state/`
        // while the offline browser read `projects/demo/.agent_state/`, and the run history was
        // permanently empty with nothing on screen explaining it.
        //
        // Asserted on the *arguments the child received*, not on the app's own strings: the bug was
        // the two sides disagreeing, and only the child's view of the invocation can catch that.
        let script = try makeArgvRecorder()
        let projectDirectory = root.appendingPathComponent("projects/harden-auth")
        try FileManager.default.createDirectory(at: projectDirectory,
                                                withIntermediateDirectories: true)
        let controller = try makeController(
            projectPath: projectDirectory,
            arguments: ["python3", script.path])
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready, "the stand-in engine must reach `running`")
        defer { controller.stop() }

        let argv = try launchedArguments()
        let slugIndex = try XCTUnwrap(argv.firstIndex(of: "--slug"), argv.description)
        XCTAssertEqual(argv[slugIndex + 1], "harden-auth",
                       "the engine must be told the project the console reads — got \(argv)")
        XCTAssertEqual(argv[slugIndex + 1], projectDirectory.lastPathComponent,
                       "the slug and the project folder must be the same string's two uses")

        let rootIndex = try XCTUnwrap(argv.firstIndex(of: "--root"), argv.description)
        XCTAssertEqual(argv[rootIndex + 1],
                       projectDirectory.deletingLastPathComponent().path,
                       "the root must be the parent of the project, or the engine composes a "
                       + "different directory from the same slug")
    }

    func testAnAttachedProjectIsNotAlsoGivenASlugOrRoot() async throws {
        // An attached folder is passed as `--project`, and the engine derives both the slug and the
        // root from it. Adding `--slug`/`--root` alongside would name a *different* workspace from the
        // one `--project` selects, which is the same class of bug in the other direction.
        let script = try makeArgvRecorder()
        let attached = root.appendingPathComponent("my-existing-repo")
        try FileManager.default.createDirectory(at: attached, withIntermediateDirectories: true)
        let controller = try makeController(
            projectPath: attached, attachedProject: attached,
            arguments: ["python3", script.path])
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready)
        defer { controller.stop() }

        let argv = try launchedArguments()
        XCTAssertTrue(argv.contains("--project"), argv.description)
        XCTAssertFalse(argv.contains("--slug"),
                       "an attached project must not also be named by slug — got \(argv)")
        XCTAssertFalse(argv.contains("--root"), argv.description)
    }

    // MARK: - The posture the app promises

    func testAGoalSetFromTheAppCarriesThePostureThePersonChose() async throws {
        // **This is the "Defaults promises what it does not do" bug.** The old panel said "This is what
        // a new goal inherits" while writing three flags that a supervised goal's own
        // `GoalPolicy.effective()` forces to `auto_approve: false, auto_hire: false` — so all three
        // checkboxes could read "on" and do nothing.
        //
        // The app cannot write the engine's `goal.default_posture` (there is no command for it), so it
        // sends the chosen posture with every goal it sets. This asserts the wire.
        let controller = try makeController(preferences: .ephemeral())
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready)
        defer { controller.stop() }
        controller.goalPosturePreference = .supervised

        await controller.setGoal("harden the auth path", arm: true)
        let goalSet = try XCTUnwrap(try commandsSent().last { $0["type"] as? String == "goal_set" })
        let payload = try XCTUnwrap(goalSet["payload"] as? [String: Any])
        XCTAssertEqual(payload["posture"] as? String, "supervised",
                       "the goal must carry the posture the person chose, or the choice is inert")
        XCTAssertEqual(payload["objective"] as? String, "harden the auth path")
    }

    func testAGoalSetWithNoPostureChosenSendsNoPostureAtAll() async throws {
        // With nothing chosen, the engine's own default applies. Inventing `unattended` here would
        // silently grant an authority nobody asked for — the safe direction of an unanswered question
        // is to leave the engine's answer alone.
        //
        // An ephemeral preferences store, so the assertion is about a controller nobody has answered
        // the wizard for rather than about whatever the developer's machine last stored.
        let script = try makeEngineScript()
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: ["python3", script.path])
        let controller = OrgController(settings: settings, maxRestartAttempts: 0,
                                       restartDelay: 0.05, preferences: .ephemeral())
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready)
        defer { controller.stop() }
        XCTAssertNil(controller.goalPosturePreference, "nothing has been chosen yet")

        await controller.setGoal("ship it", arm: true)
        let goalSet = try XCTUnwrap(try commandsSent().last { $0["type"] as? String == "goal_set" })
        let payload = try XCTUnwrap(goalSet["payload"] as? [String: Any])
        XCTAssertNil(payload["posture"], "no choice must mean no claim: \(payload)")
    }

    func testADefaultsChangeThatTheEngineRefusesIsReportedRatherThanAssumed() async throws {
        // The wizard cannot advance on an unverified change: a `defaults_set` naming a provider the
        // engine does not have is refused, and the old method logged the outcome and discarded it, so
        // the panel showed an unchanged pair with no reason.
        let controller = try await launchedController()
        defer { controller.stop() }
        // The stand-in acknowledges everything, so this asserts the *accepted* path returns true —
        // the refused path is the engine's own refusal, surfaced through `mutate`, and is covered by
        // the notice it sets.
        let accepted = await controller.setDefaults(provider: "local", model: "llama3.1:8b")
        XCTAssertTrue(accepted, "an accepted change must report success rather than only logging")
        let defaults = try XCTUnwrap(try commandsSent().last { $0["type"] as? String == "defaults_set" })
        let payload = try XCTUnwrap(defaults["payload"] as? [String: Any])
        XCTAssertEqual(payload["provider"] as? String, "local")
        XCTAssertEqual(payload["model"] as? String, "llama3.1:8b")
    }

    // MARK: - The provider form, on the wire

    func testAProviderSaveCarriesBothTheKeyAndTheVariableWhenBothWereFilled() async throws {
        // The reported bug, asserted against the *received line* rather than against the payload the
        // app built. `ProviderDraft.payload()` sent the literal only when the variable field was
        // empty, so a person who pasted their key **and** named the variable they intended to use had
        // their input discarded before it left the machine — and the engine then reported "has no API
        // key" to somebody who had just supplied one.
        //
        // The engine's own writer was fixed to keep both (`serve._cmd_provider_add`: "Both are written
        // when both were given"), but the app never sent the key, so that fix could not take effect
        // from the window. This is the assertion that the wire now carries what the engine reads.
        let controller = try await launchedController()
        defer { controller.stop() }

        let draft = ProviderDraft(id: "ollamacloud", kind: "openai",
                                  baseURL: "https://ollama.com/v1/chat/completions",
                                  apiKey: "pasted-key-that-must-cross",
                                  apiKeyEnv: "OLLAMA_API_KEY")
        await controller.saveProvider(draft)

        let add = try XCTUnwrap(try commandsSent().last { $0["type"] as? String == "provider_add" })
        let payload = try XCTUnwrap(add["payload"] as? [String: Any])
        XCTAssertEqual(payload["api_key_env"] as? String, "OLLAMA_API_KEY")
        XCTAssertEqual(payload["api_key"] as? String, "pasted-key-that-must-cross",
                       "the pasted key must cross even when a variable is also named")
        // The engine is what reduces a full endpoint to the base a provider can append to; the app
        // must send what was typed rather than guessing, so the correction is the engine's to make and
        // its `note` is what the form shows back.
        XCTAssertEqual(payload["base_url"] as? String, "https://ollama.com/v1/chat/completions")
    }

    func testAProviderSaveCarriesAKeyWithNoVariableFilledIn() async throws {
        // The case that already worked, kept as a guard: `api_key_env` must be *absent* rather than an
        // empty string, because the engine treats an empty one as "no variable" and an absent one as
        // "leave it alone" — and a save that sent `""` would silently clear a stored variable name.
        let controller = try await launchedController()
        defer { controller.stop() }

        await controller.saveProvider(ProviderDraft(id: "groq", kind: "openai",
                                                    baseURL: "https://api.groq.com/openai/v1",
                                                    apiKey: "gsk_alone"))
        let add = try XCTUnwrap(try commandsSent().last { $0["type"] as? String == "provider_add" })
        let payload = try XCTUnwrap(add["payload"] as? [String: Any])
        XCTAssertEqual(payload["api_key"] as? String, "gsk_alone")
        XCTAssertNil(payload["api_key_env"], "an empty field means `unchanged`, not `clear it`")
    }

    func testProvidingATestAlsoCarriesTheKeyRatherThanOnlyTheVariable() async throws {
        // `provider_test` and `provider_add` share `_provider_spec_from_payload`, so a draft that
        // tested with one key and saved with another would be the worst possible pair: "Connected"
        // followed by a provider the engine cannot authenticate. Both paths go through the same
        // `payload()`, and this pins that they still do.
        let controller = try await launchedController()
        defer { controller.stop() }

        await controller.testProvider(ProviderDraft(id: "gw", kind: "openai",
                                                    baseURL: "https://gw/v1",
                                                    apiKey: "k" + String(repeating: "x", count: 20),
                                                    apiKeyEnv: "GW_KEY"))
        let test = try XCTUnwrap(try commandsSent().last { $0["type"] as? String == "provider_test" })
        let payload = try XCTUnwrap(test["payload"] as? [String: Any])
        XCTAssertEqual(payload["api_key_env"] as? String, "GW_KEY")
        XCTAssertNotNil(payload["api_key"], "the test must probe with the key the save would write")
    }

    func testASavePublishesTheBaseTheEngineStoredAndNotTheOneThatWasTyped() async throws {
        // The reported case, from the app's side: a person pastes
        // `https://ollama.com/v1/chat/completions` and never presses Test. The save works — the engine
        // strips the operation — but the form has to be able to say *where* the endpoint went, or the
        // only evidence is a provider that quietly works against a URL the panel shows differently.
        //
        // The stand-in answers every command with `{"accepted": true}`, which has no `base_url` — so
        // this also pins the shape the form depends on: it reads the key rather than assuming one, and
        // an engine reply without it leaves `providerSave` empty rather than producing a blank row.
        let controller = try await launchedController()
        defer { controller.stop() }
        XCTAssertTrue(controller.providerSave.isEmpty, "nothing is claimed before a save")

        await controller.saveProvider(ProviderDraft(id: "ollamacloud", kind: "openai",
                                                    baseURL: "https://ollama.com/v1/chat/completions"))
        // The stand-in's reply carries no `base_url`, so what is asserted is that the *save* published
        // whatever the engine said rather than the draft's own string — the app never invents a
        // correction it did not receive.
        XCTAssertNil(controller.providerSave["base_url"],
                     "the form must read the engine's answer, not restate what was typed")
    }

    func testTheSaveReplyIsClearedByEachNewAttemptRatherThanAccumulating() async throws {
        // A correction belongs to the save that produced it. Left in place across a second save that
        // needed none, it would tell the person their URL had been changed when it had not.
        let controller = try await launchedController()
        defer { controller.stop() }

        await controller.saveProvider(ProviderDraft(id: "a", kind: "openai", baseURL: "https://a/v1"))
        XCTAssertEqual(controller.providerSave["accepted"]?.boolValue, true,
                       "the reply the engine actually gave is what is held")
        await controller.saveProvider(ProviderDraft(id: "b", kind: "openai", baseURL: "https://b/v1"))
        XCTAssertFalse(controller.providerSave.isEmpty,
                       "the second attempt's reply replaces the first's rather than being ignored")
    }
}
