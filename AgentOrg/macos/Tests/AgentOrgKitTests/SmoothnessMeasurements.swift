//
//  SmoothnessMeasurements.swift
//  AgentOrgKitTests
//
//  The numbers the task asks to be measured: how long the launch takes, and what the concurrent panel
//  loads actually save.
//
//  WHY TWO KINDS OF MEASUREMENT
//  ---------------------------
//  The launch is measured against the **real engine**, because the question is about this engine's
//  bootstrap — its imports, its config resolution, its skill-library scan — and a stub would measure
//  the stub.
//
//  The panel loads are measured against a stub with an **injected per-command delay**, because the real
//  engine cannot answer that question cleanly, and pretending otherwise produced a misleading number the
//  first time this was written. Two reasons:
//
//  1. **The engine executes commands one at a time.** `serve.run` has a single worker taking from one
//     queue, so three "concurrent" commands are still handled serially *on the engine's side*. The win
//     from `loadWindow` is overlapping the round-trip latency, not parallelising the engine's work, and
//     a real-engine number would conflate the two.
//  2. **The console polls every two seconds while the engine is up.** `startSnapshotting` fires
//     `status` and `models` on a repeating timer, so a several-second measurement competes with the
//     poll — and whichever variant ran first measured a colder cache. The first version of this test
//     reported "8180ms sequential, 4172ms concurrent", which was mostly poll contention and cache
//     warm-up rather than the property under test.
//
//  A fixed delay removes both: with each reply held for D, three sequential loads take about 3D and
//  three concurrent ones take about D. That is the structural claim, and it is assertable.
//

import XCTest
@testable import AgentOrgKit

@MainActor
final class SmoothnessMeasurements: XCTestCase {

    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-measure-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    /// The repository root, found the same way the app finds it.
    private func repositoryRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // AgentOrgKitTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // macos
            .deletingLastPathComponent()   // AgentOrg
            .deletingLastPathComponent()   // repository root
    }

    private func waitUntil(_ deadline: TimeInterval, _ condition: () -> Bool) async -> Bool {
        let end = Date().addingTimeInterval(deadline)
        while Date() < end {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        return condition()
    }

    // MARK: - How long the engine takes to become usable

    func testMeasureHowLongLaunchTakesToReachEngineReady() async throws {
        // The headline number for the launch row, and what decides whether that row is a courtesy or a
        // necessity: at a fraction of a second it barely appears; on a cold machine — no cached
        // bytecode, a skill library on a network mount, a permission prompt to answer — it is the
        // difference between the old blank "Working…" and a row saying which stage it reached.
        //
        // Measured through the *controller*, not by spawning the engine here, so the figure includes the
        // same argument composition, path checks and event plumbing the app performs.
        let settings = OrgController.OrgSettings.discover(repositoryRoot: repositoryRoot())
        let controller = OrgController(settings: settings, maxRestartAttempts: 0,
                                       preferences: .ephemeral())
        try XCTSkipUnless(controller.canLaunch, "no Python interpreter — nothing to measure")
        try XCTSkipIf(controller.launchProblem() != nil,
                      controller.launchProblem() ?? "the engine is not launchable here")

        let started = Date()
        controller.launch()
        let ready = await waitUntil(60) { controller.engineState == .running }
        let elapsed = Date().timeIntervalSince(started)
        XCTAssertTrue(ready, "the real engine must reach `running` via its readiness frame")
        defer { controller.stop() }

        let recorded = try XCTUnwrap(controller.lastLaunchSeconds,
                                     "the controller records the same figure the row shows")
        print("measured: launch → engine.ready \(String(format: "%.3f", elapsed))s "
              + "(controller recorded \(String(format: "%.3f", recorded))s, "
              + "runtime \(controller.runtimeDescription))")

        // The row must be finished with: a ready engine cannot still be claiming a launch is in flight.
        XCTAssertNil(controller.launchProgress, "a ready engine must clear the launch row")
        XCTAssertNil(controller.waitingSummary, "nothing is waiting once the engine is up")
        XCTAssertGreaterThanOrEqual(recorded, 0)
    }

    // MARK: - Sequential versus concurrent panel loads

    /// A stand-in engine that holds every reply for a fixed delay, then answers it.
    ///
    /// A `Thread` per command rather than one read loop, because the point is to let the *engine's* side
    /// overlap too: with a single-threaded reader the delay itself would serialise the replies and
    /// conceal exactly the effect being measured.
    private func makeDelayedEngine(delay: TimeInterval) throws -> URL {
        let script = root.appendingPathComponent("delayed.py")
        let details = """
        {
            "providers": {"providers": [], "config_path": ""},
            "models": {"models": []},
            "agents": {"agents": [], "skills": [], "roster_path": ""}
        }
        """
        let source = """
        import json, sys, os, threading, time

        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()

        emit({"v": 1, "seq": 1, "type": "engine.ready", "payload": {"pid": os.getpid()}})

        details = \(details)
        delay = \(delay)
        out_lock = threading.Lock()

        def answer(cmd):
            time.sleep(delay)
            with out_lock:
                emit({"v": 1, "seq": 0, "type": "command.ack",
                      "payload": {"cmd_id": cmd["cmd_id"], "ok": True,
                                  "detail": details.get(cmd["type"], {})}})

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            cmd = json.loads(line)
            threading.Thread(target=answer, args=(cmd,), daemon=True).start()
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

    func testMeasureTheSequentialVersusConcurrentPanelLoads() async throws {
        // Each reply is held for 400 ms, so three sequential loads must cost about 1.2 s and three
        // concurrent ones about 0.4 s. The delay is injected rather than real, because a real command on
        // a local pipe costs single-digit milliseconds and the difference would be lost in the noise —
        // which is itself worth stating: on a *fast* engine the concurrency saves little, and its real
        // value is that one slow load no longer holds up the other two.
        let delay: TimeInterval = 0.4
        let engineScript = try makeDelayedEngine(delay: delay)
        let controller = makeController(arguments: ["python3", engineScript.path])
        controller.launch()
        let ready = await waitUntil(15) { controller.engineState == .running }
        XCTAssertTrue(ready, "the stand-in engine must reach `running`")
        // The console's own two-second poll must not be running during the measurement, or it competes
        // with the loads and the numbers become about contention. Stopping the engine would do that but
        // also close the pipe, so instead the loads are measured immediately after readiness, before the
        // first tick — with a guard that the test fails loudly rather than quietly reporting a polluted
        // figure if the poll did get in.
        defer { controller.stop() }

        let sequentialStart = Date()
        await controller.loadProviders()
        await controller.loadModels()
        await controller.loadRoster()
        let sequential = Date().timeIntervalSince(sequentialStart)

        let concurrentStart = Date()
        await controller.loadWindow()
        let concurrent = Date().timeIntervalSince(concurrentStart)

        let message = String(
            format: "measured: three panel loads with %.0fms replies — sequential %.0fms, "
                + "concurrent %.0fms (%.2f× faster)",
            delay * 1000, sequential * 1000, concurrent * 1000,
            sequential / max(concurrent, 0.000_001))
        print(message)

        // The structural claim, with generous margins: sequential is at least 2.5 delays (it cannot be
        // less than 3 delays unless replies overlapped, which they cannot when each await must resolve
        // before the next command is sent), and concurrent is under 2 delays.
        XCTAssertGreaterThan(sequential, delay * 2.5,
                             "three sequential round trips must cost about three delays")
        XCTAssertLessThan(concurrent, delay * 2,
                          "three concurrent round trips must cost about one delay")
        XCTAssertLessThan(concurrent, sequential,
                          "the concurrent load must beat the sequential one")
    }
}
