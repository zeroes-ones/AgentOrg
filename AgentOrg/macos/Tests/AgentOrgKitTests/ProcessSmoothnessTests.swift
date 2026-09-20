//
//  ProcessSmoothnessTests.swift
//  AgentOrgKitTests
//
//  The console never looking stuck. Three separate claims, each asserted where it is measurable.
//
//  WHY THESE ARE SEPARATE FROM THE OTHER CONTROLLER TESTS
//  ----------------------------------------------------
//  Everything here is about *time* — a wait that must be reported, commands that must overlap, a read
//  that must not block the main actor — and the assertions are shaped accordingly: elapsed thresholds
//  are checked at the boundary, concurrency is proven by the *order* commands reach the wire rather than
//  by a stopwatch, and the disk rule is checked by doing the read from the wrong actor on purpose.
//
//  The one thing deliberately *not* asserted is that the UI "feels" smoother. That needs a person
//  looking at a screen, and no test can stand in for it — so what is asserted is the facts that make it
//  smooth: the notice appears at the threshold, the three loads overlap, and no read happens on the main
//  actor.
//

import XCTest
@testable import AgentOrgKit

// MARK: - The launch row

/// `LaunchProgress` is a value with the clock passed in, so every claim about what a person reads while
/// the engine starts is assertable at any elapsed time without sleeping.
final class LaunchProgressTests: XCTestCase {

    private let start = Date(timeIntervalSince1970: 1_000_000)

    private func at(_ seconds: TimeInterval) -> Date {
        start.addingTimeInterval(seconds)
    }

    func testTheElapsedTimeIsOnScreenFromTheFirstMomentOfTheBootstrap() {
        // The complaint in its plainest form: "Working…" with no indication of how long. The clock is
        // in the sentence from the first render, not added once something has gone wrong.
        var progress = LaunchProgress(startedAt: start)
        progress.spawned()
        XCTAssertEqual(progress.summary(now: at(3)),
                       "Starting the engine — 3s, waiting for it to report ready")
    }

    func testTheEnginesOwnWordsAppearWhileItIsStarting() {
        // The engine writes everything before `engine.ready` to stderr. Ignoring it meant the app was
        // reporting silence during the one phase where it had actual evidence of progress.
        var progress = LaunchProgress(startedAt: start)
        progress.spawned()
        progress.noted("loading skill library from /Users/me/Skills")
        let summary = progress.summary(now: at(5)) ?? ""
        XCTAssertTrue(summary.contains("5s"), summary)
        XCTAssertTrue(summary.contains("loading skill library"), summary)
    }

    func testBlankDiagnosticLinesDoNotCountAsProgress() {
        // The engine's stderr is not always a meaningful line. Counting a blank one as "it said
        // something" would let the row claim progress the engine did not make.
        var progress = LaunchProgress(startedAt: start)
        progress.noted("   ")
        XCTAssertEqual(progress.stage, .starting)
        XCTAssertNil(progress.lastLine)
        XCTAssertEqual(progress.lineCount, 0)
    }

    func testNoAdviceIsOfferedWhileTheLaunchIsStillNormal() {
        // The threshold has to mean something. Advice shown at 2 s would be noise on every launch, and
        // noise on the common path is how a warning stops being read.
        var progress = LaunchProgress(startedAt: start)
        progress.spawned()
        XCTAssertNil(progress.advice(now: at(LaunchProgress.stuckAfter - 1)),
                     "one second under the threshold must stay quiet")
    }

    func testTheAdviceAppearsAtTheThresholdAndNamesTheUsualCause() {
        // The case the row exists for: a launch that has crossed the line is almost always macOS
        // waiting on a permission prompt behind the window, and that is worth saying because the person
        // can act on it in one glance.
        var progress = LaunchProgress(startedAt: start)
        progress.spawned()
        let advice = progress.advice(now: at(LaunchProgress.stuckAfter + 5)) ?? ""
        XCTAssertTrue(advice.contains("25s"), advice)
        XCTAssertTrue(advice.contains("permission"), advice)
    }

    func testAReadyLaunchHasNothingLeftToSay() {
        // Once the engine is up the row must vanish, or it becomes a permanent claim that something is
        // still happening.
        var progress = LaunchProgress(startedAt: start)
        progress.ready()
        XCTAssertNil(progress.summary(now: at(30)))
        XCTAssertNil(progress.advice(now: at(300)))
        XCTAssertFalse(progress.isLive)
    }

    func testSpawningIsNotSomethingAReadyLaunchCanGoBackFrom() {
        // The spawn callback and the readiness frame arrive from different queues, so a late `spawned()`
        // must not undo the readiness the row depends on — it would put the spinner back on screen
        // after the engine was usable.
        var progress = LaunchProgress(startedAt: start)
        progress.ready()
        progress.spawned()
        XCTAssertEqual(progress.stage, .ready)
    }

    func testTheElapsedTimeNeverGoesNegative() {
        // A clock adjustment mid-launch must not produce "-3s". The value is a report about the app,
        // and a nonsense figure in it would be worse than a stale one.
        let progress = LaunchProgress(startedAt: start)
        XCTAssertEqual(progress.elapsed(now: at(-10)), 0)
    }
}

// MARK: - The slow command

/// The command timeout and the slow-command notice, driven against a real child process.
///
/// A real process rather than a fake, for the same reason `AgentOrgControllerBridgeTests` uses one: the
/// claim is about a command that goes on a wire and never comes back, and that is a fact about the
/// bridge.
@MainActor
final class SlowCommandTests: XCTestCase {

    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-slow-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    /// A stand-in engine that answers everything **except** `flow`.
    ///
    /// The shape of the failure being tested: the process is alive and healthy, it simply never replies
    /// to one command. A mock that returned an error would test the error path, which is not the path
    /// that leaves a person staring at an unchanged window.
    ///
    /// It answers the rest deliberately. The console fires `loadWindow` the moment the engine reports
    /// ready, so a stub that answered *nothing* would put three other commands in the slow list and the
    /// assertion about which command is named could not distinguish "the row works" from "the row is
    /// listing the launch's own loads".
    private func makeStallingEngine(stalling: String = "flow") throws -> URL {
        let script = root.appendingPathComponent("stalling.py")
        let source = """
        import json, sys, os

        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()

        emit({"v": 1, "seq": 1, "type": "engine.ready", "payload": {"pid": os.getpid()}})

        stalls = \(String(reflecting: stalling))
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            cmd = json.loads(line)
            # The one command that never comes back. Everything else is answered, so the slow list has
            # exactly one entry and the test is about the row rather than about the stub.
            if cmd["type"] == stalls:
                continue
            emit({"v": 1, "seq": 0, "type": "command.ack",
                  "payload": {"cmd_id": cmd["cmd_id"], "ok": True, "detail": {}}})
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        return script
    }

    private func makeController(commandTimeout: TimeInterval = 600,
                                slowCommandAfter: TimeInterval = 2) throws -> OrgController {
        let script = try makeStallingEngine()
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: ["python3", script.path])
        return OrgController(settings: settings, maxRestartAttempts: 0,
                             commandTimeout: commandTimeout, slowCommandAfter: slowCommandAfter,
                             preferences: .ephemeral())
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

    func testASlowCommandIsNamedRatherThanLeftInvisible() async throws {
        // The requirement: a command outstanding for more than a couple of seconds must be *said*, with
        // its name. This is the difference between a spinner and a diagnosis.
        let controller = try makeController(slowCommandAfter: 0.4)
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready)
        defer { controller.stop() }

        // Fired without awaiting: the command never returns, so awaiting it would be the very hang
        // being tested. The controller records it on the way out.
        Task { await controller.refreshFlow() }

        let appeared = await waitUntil(5) { !controller.slowCommands.isEmpty }
        XCTAssertTrue(appeared, "a command past the threshold must appear in the slow list")
        XCTAssertEqual(controller.slowCommands.first?.type, "flow",
                       "the notice must name the command, not just say something is slow")
        let summary = controller.waitingSummary ?? ""
        XCTAssertTrue(summary.contains("flow"), summary)

        // The advice names the budget as well as the command, so a person knows what will happen.
        let advice = controller.waitingAdvice ?? ""
        XCTAssertTrue(advice.contains("flow"), advice)
        XCTAssertTrue(advice.contains("600"), "the advice must quote the configured budget: \(advice)")
    }

    func testACommandAnsweredQuicklyNeverProducesANotice() async throws {
        // The other half, and the one that keeps the row worth reading: a healthy engine must never
        // make it appear. If a fast `status` produced a notice, every poll would flash one.
        let script = root.appendingPathComponent("fast.py")
        let source = """
        import json, sys, os
        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
        emit({"v": 1, "seq": 1, "type": "engine.ready", "payload": {"pid": os.getpid()}})
        for line in sys.stdin:
            line = line.strip()
            if not line: continue
            cmd = json.loads(line)
            emit({"v": 1, "seq": 0, "type": "command.ack",
                  "payload": {"cmd_id": cmd["cmd_id"], "ok": True, "detail": {}}})
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: ["python3", script.path])
        let controller = OrgController(settings: settings, maxRestartAttempts: 0,
                                       slowCommandAfter: 0.3, preferences: .ephemeral())
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready)
        defer { controller.stop() }

        await controller.refreshFlow()
        // Well past the threshold the notice would have appeared at.
        try? await Task.sleep(nanoseconds: 800_000_000)
        XCTAssertTrue(controller.slowCommands.isEmpty,
                      "an answered command must leave nothing in the slow list")
        XCTAssertNil(controller.waitingSummary)
    }

    /// A stand-in engine that emits readiness and then answers nothing at all.
    ///
    /// Distinct from `makeStallingEngine`: this drives the *bridge* directly rather than through the
    /// console, so there is no `loadWindow` firing three other commands and a wholly silent child is
    /// exactly the shape wanted.
    private func makeSilentEngine() throws -> URL {
        let script = root.appendingPathComponent("silent.py")
        let source = """
        import json, sys, os, time
        sys.stdout.write(json.dumps({"v": 1, "seq": 1, "type": "engine.ready",
                                     "payload": {"pid": os.getpid()}}) + "\\n")
        sys.stdout.flush()
        time.sleep(600)
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        return script
    }

    func testACommandRacingAStopIsRefusedRatherThanCrashingTheApp() async throws {
        // **This was a real crash, found while measuring.** Writing to the engine's stdin after
        // `terminate()` closed it raises `NSFileHandleOperationException` — an Objective-C exception,
        // which Swift cannot catch, so the whole app died. The reproduction is a `status` poll landing
        // in the window between the close and the child's exit, which is a moment the running app is
        // in constantly: it polls every two seconds, and a person pressing Stop is exactly when one is
        // most likely to be in flight.
        //
        // Asserted by firing commands straight into that window: each must come back as an error, and
        // the test process must still be here to check it.
        let script = try makeStallingEngine()
        let service = AgentProcessService(
            config: EngineLaunchConfig(
                runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                engineRoot: root, projectPath: root,
                arguments: ["python3", script.path]),
            commandTimeout: 5)
        try service.launch()
        let ready = Date().addingTimeInterval(5)
        while !service.isReady && Date() < ready { try? await Task.sleep(nanoseconds: 50_000_000) }
        XCTAssertTrue(service.isReady)

        // One command in flight, then stop while it is outstanding, then more commands into the gap.
        let inFlight = Task { try await service.send("status") }
        service.terminate()
        for type in ["status", "providers", "models"] {
            do {
                _ = try await service.send(type)
                XCTFail("`\(type)` sent after the pipe closed must be refused")
            } catch {
                XCTAssertEqual((error as? EngineError)?.kind, .notRunning,
                               "expected a refusal, got \(error)")
            }
        }
        // The one that was outstanding when the stop landed is failed by `failPendingCommands` rather
        // than left suspended.
        do {
            _ = try await inFlight.value
            XCTFail("a command outstanding at termination must be failed, not left hanging")
        } catch {
            XCTAssertEqual((error as? EngineError)?.kind, .notRunning)
        }
        XCTAssertFalse(service.canSendCommands, "the pipe is closed, and the bridge must say so")
    }

    func testTheTimeoutIsEnforcedRatherThanOnlyRecorded() async throws {
        // **This was a real defect.** `PendingCommand.deadline` was computed and documented as producing
        // a `.commandTimeout`, and nothing ever compared against it — so a command the engine never
        // answered suspended its continuation for the life of the process, and every caller awaiting it
        // stopped for good. A short timeout is injected precisely so this can be asserted in a second
        // rather than by waiting out the production ten minutes.
        let script = try makeSilentEngine()
        let service = AgentProcessService(
            config: EngineLaunchConfig(
                runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                engineRoot: root, projectPath: root,
                arguments: ["python3", script.path]),
            commandTimeout: 0.5)
        try service.launch()
        defer { service.terminate() }
        // Wait for readiness so the send is not refused as `notRunning`.
        let deadline = Date().addingTimeInterval(5)
        while !service.isReady && Date() < deadline {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTAssertTrue(service.isReady)

        do {
            _ = try await service.send("status")
            XCTFail("an unanswered command must time out rather than suspend forever")
        } catch {
            XCTAssertEqual((error as? EngineError)?.kind, .commandTimeout)
            XCTAssertTrue(error.localizedDescription.contains("status"),
                          "the error must name the command: \(error.localizedDescription)")
        }
    }

    func testTheTimeoutIsNotAMagicNumberInOnePlace() throws {
        // The requirement in its literal form: the timeout is injected, the bridge honours what it was
        // given, and there is exactly one default. Asserting the *injected* value came back is what
        // proves it is configured rather than hardcoded, and a zero is floored so a bad value cannot
        // make every command fail instantly.
        let config = EngineLaunchConfig(
            runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
            engineRoot: root, projectPath: root, arguments: ["python3", "-c", ""])
        XCTAssertEqual(AgentProcessService(config: config, commandTimeout: 42).timeout, 42)
        XCTAssertEqual(AgentProcessService(config: config).timeout,
                       AgentProcessService.defaultCommandTimeout)
        XCTAssertEqual(OrgController.defaultCommandTimeout,
                       AgentProcessService.defaultCommandTimeout,
                       "the console and the bridge must agree, or the UI quotes a budget the "
                       + "bridge does not honour")
        XCTAssertEqual(AgentProcessService(config: config, commandTimeout: 0).timeout, 1,
                       "a zero budget would time out every command instantly")
    }

    func testTheOutstandingListIsEmptyBeforeAnythingIsSent() {
        let config = EngineLaunchConfig(
            runtime: .system(URL(fileURLWithPath: "/bin/sh")),
            engineRoot: root, projectPath: root, arguments: ["-c", "sleep 5"])
        let service = AgentProcessService(config: config)
        XCTAssertTrue(service.outstandingCommands.isEmpty)
    }
}

// MARK: - The panel loads overlap

/// The three window loads, asserted **concurrently by their arrival order on the wire**.
///
/// Why not a stopwatch: a wall-clock comparison would be flaky on a loaded machine and would still not
/// distinguish "they overlapped" from "they were fast". The stand-in engine instead *records the order
/// commands arrive* and answers nothing until it has seen all three — which cannot happen if the loads
/// are sequential, because each await would block on a reply that is deliberately withheld.
@MainActor
final class ConcurrentPanelLoadTests: XCTestCase {

    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-concurrent-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    /// A stand-in engine that withholds every acknowledgement until all three loads have arrived.
    ///
    /// The barrier is the assertion. A sequential caller sends one, waits, and — because the reply never
    /// comes until the other two arrive — simply stops; only a caller that sends all three without
    /// waiting in between can trip the barrier.
    private func makeBarrierEngine(expected: Int) throws -> URL {
        let script = root.appendingPathComponent("barrier.py")
        let received = root.appendingPathComponent("received.jsonl")
        let source = """
        import json, sys, os, threading

        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()

        emit({"v": 1, "seq": 1, "type": "engine.ready", "payload": {"pid": os.getpid()}})

        #: What each command answers with, so the loaders have something real to apply — an empty detail
        #: would let a test that claims "the window is populated" pass without any field being set.
        details = {
            "providers": {"providers": [{"id": "local", "kind": "ollama", "status": "ready"}],
                          "config_path": "/tmp/credentials.json"},
            "models": {"models": [{"name": "llama3.1:8b", "provider": "local"}]},
            "agents": {"agents": [{"id": "owner", "name": "Owner", "role": "owner"}],
                       "skills": ["backend-developer"], "roster_path": "/tmp/roster.json"},
        }

        received_path = \(String(reflecting: received.path))

        lock = threading.Lock()
        held = []
        expected = \(expected)

        def release(batch):
            for cmd in batch:
                emit({"v": 1, "seq": 0, "type": "command.ack",
                      "payload": {"cmd_id": cmd["cmd_id"], "ok": True,
                                  "detail": details.get(cmd["type"], {})}})

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            cmd = json.loads(line)
            with open(received_path, "a") as handle:
                handle.write(json.dumps(cmd) + "\\n")
            with lock:
                held.append(cmd)
                # Released only once `expected` have *arrived without any having been answered* — which
                # is the whole test. A caller that awaits each reply in turn never gets here.
                if len(held) >= expected:
                    batch, held = held, []
                    release(batch)
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        return script
    }

    private func makeController(arguments: [String]) -> OrgController {
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")), arguments: arguments)
        return OrgController(settings: settings, maxRestartAttempts: 0, preferences: .ephemeral())
    }

    private func waitUntil(_ deadline: TimeInterval = 15,
                           _ condition: () -> Bool) async -> Bool {
        let end = Date().addingTimeInterval(deadline)
        while Date() < end {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 25_000_000)
        }
        return condition()
    }

    /// What the stand-in engine actually received, in order.
    private func receivedTypes() -> [String] {
        let path = root.appendingPathComponent("received.jsonl")
        guard let text = try? String(contentsOf: path, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap { line in
            (try? JSONSerialization.jsonObject(with: Data(line.utf8)))
                .flatMap { ($0 as? [String: Any])?["type"] as? String }
        }
    }

    /// Run `loadWindow` and answer whether it finished before `seconds` elapsed.
    ///
    /// The race *is* the assertion. Against the barrier engine, a sequential implementation never
    /// finishes at all — its first await is on a reply the stub withholds until two more commands
    /// arrive — so "did it return, within a bound generous enough to exclude a slow machine?" is exactly
    /// the question, and a bare `await` would hang the suite instead of failing it.
    private func loadsFinish(_ controller: OrgController, within seconds: TimeInterval) async -> Bool {
        await withTaskGroup(of: Bool.self) { group in
            group.addTask { await controller.loadWindow(); return true }
            group.addTask {
                try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                return false
            }
            let first = await group.next() ?? false
            group.cancelAll()
            return first
        }
    }

    func testTheThreeWindowLoadsAreSentTogetherRatherThanOneAtATime() async throws {
        // The engine holds every reply until all three commands have *arrived*, so this passes only if
        // the console put all three on the wire without waiting for any of them. Against the old
        // sequential `loadWindow` this stub deadlocks by construction — which is why the assertion is
        // "it returned", not "it was fast".
        let script = try makeBarrierEngine(expected: 3)
        let controller = makeController(arguments: ["python3", script.path])
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready, "the stand-in engine must reach `running`")
        defer { controller.stop() }

        let finished = await loadsFinish(controller, within: 10)
        XCTAssertTrue(finished,
                      "all three commands must be in flight together, or the barrier never trips")
        // And they really were the three panel loads, in whatever order the group ran them.
        let seen = Set(receivedTypes())
        XCTAssertTrue(seen.isSuperset(of: ["providers", "models", "agents"]),
                      "the three window loads must be what went out: \(seen)")
    }

    func testAllThreeLoadsApplyTheirRepliesSoTheWindowIsFullyPopulated() async throws {
        // The barrier engine answers all three at once, so after `loadWindow` returns every field it
        // touches must be set — which is what "the window fills in one round trip" means in state terms.
        // A group that fired the commands but dropped their replies would trip the barrier and still
        // leave the window empty, so this is a distinct claim from the test above.
        let script = try makeBarrierEngine(expected: 3)
        let controller = makeController(arguments: ["python3", script.path])
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready)
        defer { controller.stop() }

        let finished = await loadsFinish(controller, within: 10)
        XCTAssertTrue(finished)
        XCTAssertEqual(controller.providers.count, 1)
        XCTAssertEqual(controller.providers.first?["id"]?.stringValue, "local")
        XCTAssertEqual(controller.models.count, 1)
        XCTAssertEqual(controller.models.first?["name"]?.stringValue, "llama3.1:8b")
        XCTAssertEqual(controller.roster.count, 1)
        XCTAssertEqual(controller.roster.first?["name"]?.stringValue, "Owner")
        XCTAssertEqual(controller.skills, ["backend-developer"])
    }

    // MARK: - Nothing reads the disk on the main actor

/// A counter the main-actor ticker can bump.
///
/// A class rather than a captured `var`, because the mutation happens inside a `@MainActor` task from a
/// method that is itself main-actor isolated — a captured `var` in a concurrent closure would be an
/// exclusivity violation under the Swift 6 language mode.
@MainActor
private final class TickCounter {
    private(set) var value = 0
    func increment() { value += 1 }
}


    func testTheOfflineReloadLeavesTheMainActorFreeWhileItReads() async throws {
        // **The rule the task asks to audit for.** `refreshOfflineState` reads a tree of files whose
        // size is set by a run — hundreds of handoffs, multi-megabyte cache streams — so a synchronous
        // read here would stall the window in proportion to how much work the run has done.
        //
        // Asserted by making the read *observably slow* and checking the main actor kept running while
        // it happened. A ticker is spun on the main actor for the duration; if the read were inline, the
        // ticks could not interleave with it at all, because nothing on the main actor can run while the
        // main actor is inside a synchronous file read. The count is compared against the number of
        // *awaits* a detached read must involve, so a read that finished too fast to interleave cannot
        // pass this by accident.
        let stateDir = root.appendingPathComponent(".agent_state/handoffs")
        try FileManager.default.createDirectory(at: stateDir, withIntermediateDirectories: true)
        for index in 0..<200 {
            try Data("""
            {"handoff_id":"h\(index)","kind":"handoff","origin":"a","target":"b",
             "state":"VERIFIED","created_at":"2026-09-17T14:0\(index % 10):00Z",
             "payload":{"status":"ok","summary":"s\(index)","artifacts":[],"open_questions":[]}}
            """.utf8).write(to: stateDir.appendingPathComponent("h\(index).json"))
        }

        let controller = makeController(arguments: ["/bin/cat"])
        let ticks = TickCounter()
        let ticker = Task { @MainActor in
            while !Task.isCancelled {
                ticks.increment()
                await Task.yield()
            }
        }
        // Let the ticker establish itself before the read starts, so the count is about the read rather
        // than about how long it took to schedule the ticker.
        for _ in 0..<8 { await Task.yield() }
        let beforeRead = ticks.value

        let started = Date()
        await controller.refreshOfflineState()
        let elapsed = Date().timeIntervalSince(started)
        ticker.cancel()

        XCTAssertEqual(controller.offlineHandoffs.count, 200,
                       "the read must actually have happened")
        XCTAssertGreaterThan(ticks.value, beforeRead + 1,
                             "the main actor must keep running while the read is in flight; it ran "
                             + "\(ticks.value - beforeRead) times during a "
                             + "\(String(format: "%.1f", elapsed * 1000))ms read")
        // Reported rather than asserted: the number is the measurement the task asks for, and a
        // threshold on it would be a flaky claim about this machine's disk.
        print("measured: offline reload of 200 handoffs took "
              + "\(String(format: "%.1f", elapsed * 1000))ms, "
              + "\(ticks.value - beforeRead) main-actor ticks interleaved")
    }

    func testTheOfflineGoalIsCarriedWithTheSnapshotRatherThanReadOnDemand() async throws {
        // `offlineGoal` used to be a computed property calling `offline.goal()` — a synchronous disk
        // read, and the only thing that reads it is a SwiftUI view body, which is the main actor. It is
        // now stored, so reading it cannot touch the filesystem at all.
        try WorkspaceWriter(root: root).writeJSON(".agent_state/goal.json", [
            "objective": "harden the auth path",
            "state": "paused",
        ])
        let controller = makeController(arguments: ["/bin/cat"])
        XCTAssertTrue(controller.offlineGoal.isEmpty,
                      "nothing has been read yet, so the stored value is empty rather than a read")

        await controller.refreshOfflineState()
        XCTAssertEqual(controller.offlineGoal["objective"]?.stringValue, "harden the auth path")
        XCTAssertEqual(controller.offlineGoalDocument["state"]?.stringValue, "paused")
    }
}
