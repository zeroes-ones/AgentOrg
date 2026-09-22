//
//  AgentProcessServiceTests.swift
//  AgentOrgKitTests
//
//  The bridge, tested against a *real* subprocess rather than a mock.
//
//  WHY REAL PROCESSES
//  ------------------
//  This type's whole job is process and pipe handling, and every bug it can have is a bug in how a pipe
//  actually behaves: a handler that fires on a closed handle, a continuation left suspended when the
//  child dies, a chunk that arrives mid-line. A mock would confirm the code calls the calls it calls and
//  prove nothing about any of that.
//
//  So the tests launch `/bin/sh` and `/bin/echo` — cheap, always present, and enough to exercise
//  framing, exit codes, and failure resolution.
//

import XCTest
@testable import AgentOrgKit

final class AgentProcessServiceTests: XCTestCase {

    private func makeConfig(script: String,
                            arguments: [String] = []) -> EngineLaunchConfig {
        EngineLaunchConfig(
            runtime: .system(URL(fileURLWithPath: "/bin/sh")),
            engineRoot: URL(fileURLWithPath: FileManager.default.temporaryDirectory.path),
            projectPath: URL(fileURLWithPath: FileManager.default.temporaryDirectory.path),
            arguments: arguments)
    }

    /// A service that runs a shell script instead of the engine, so framing can be tested directly.
    ///
    /// The invocation is in the config rather than the service, so a test can point the bridge at any
    /// program — which is exactly how these tests exercise pipes without Python in the loop.
    private func launchService(_ script: String,
                               grace: TimeInterval = 2.0,
                               launchTimeout: TimeInterval = AgentProcessService.defaultLaunchTimeout)
                               throws -> AgentProcessService {
        let config = EngineLaunchConfig(
            runtime: .system(URL(fileURLWithPath: "/bin/sh")),
            engineRoot: URL(fileURLWithPath: FileManager.default.temporaryDirectory.path),
            projectPath: URL(fileURLWithPath: FileManager.default.temporaryDirectory.path),
            arguments: ["-c", script])
        let service = AgentProcessService(config: config, terminationGrace: grace,
                                          launchTimeout: launchTimeout)
        try service.launch()
        return service
    }

    // MARK: - Environment

    func testTheChildEnvironmentSetsUnbufferedOutput() {
        // Without PYTHONUNBUFFERED a piped stdout is block-buffered, so the terminal would see a run's
        // output arrive in one burst at the end instead of live.
        let config = makeConfig(script: "")
        let environment = config.resolvedEnvironment
        XCTAssertEqual(environment["PYTHONUNBUFFERED"], "1")
        XCTAssertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        XCTAssertNotNil(environment["AGENTORG_PROJECT"])
        XCTAssertNotNil(environment["PATH"], "a GUI launch has a minimal PATH")
    }

    func testTheChildEnvironmentCarriesTheLibraryAndCredentialsPaths() {
        let config = EngineLaunchConfig(
            runtime: .system(URL(fileURLWithPath: "/bin/sh")),
            engineRoot: URL(fileURLWithPath: "/tmp/engine"),
            projectPath: URL(fileURLWithPath: "/tmp/proj"),
            credentialsPath: URL(fileURLWithPath: "/tmp/creds.json"),
            libraryRoot: URL(fileURLWithPath: "/tmp/skills"))
        let environment = config.resolvedEnvironment
        XCTAssertEqual(environment["AGENTORG_CREDENTIALS"], "/tmp/creds.json")
        XCTAssertEqual(environment["AGENTORG_SKILLS_ROOT"], "/tmp/skills")
        XCTAssertEqual(environment["AGENTORG_PROJECT"], "/tmp/proj")
    }

    func testNoPinnedLibraryMeansNoVariableAndThereforeDiscovery() {
        // **The variable's absence is the app's half of "let the engine look for one".** The engine
        // treats an *empty* value as a candidate too, so exporting `""` would not be the same
        // statement as exporting nothing — this pins that an unpinned launch leaves the key out
        // entirely, which is how the console's Clear button restores the pre-pin behaviour.
        let config = EngineLaunchConfig(
            runtime: .system(URL(fileURLWithPath: "/bin/sh")),
            engineRoot: URL(fileURLWithPath: "/tmp/engine"),
            projectPath: URL(fileURLWithPath: "/tmp/proj"))
        XCTAssertNil(config.resolvedEnvironment["AGENTORG_SKILLS_ROOT"])
    }

    // MARK: - Attaching a project

    func testNoAttachedProjectMeansUnchangedArguments() {
        let config = makeConfig(script: "", arguments: ["-m", "engine.cli", "serve"])
        XCTAssertEqual(config.resolvedArguments, ["-m", "engine.cli", "serve"])
    }

    func testAnAttachedProjectAppendsTheProjectFlag() {
        // The flag and the path are composed together so they cannot drift: a mismatch would launch the
        // engine at a managed project while the UI claimed to be on the user's repository.
        let config = EngineLaunchConfig(
            runtime: .system(URL(fileURLWithPath: "/bin/sh")),
            engineRoot: URL(fileURLWithPath: "/tmp/engine"),
            projectPath: URL(fileURLWithPath: "/Users/me/code/my-app"),
            arguments: ["-m", "engine.cli", "serve"],
            attachedProjectPath: URL(fileURLWithPath: "/Users/me/code/my-app"))
        XCTAssertEqual(config.resolvedArguments,
                       ["-m", "engine.cli", "serve", "--project", "/Users/me/code/my-app"])
    }

    // MARK: - Lifecycle

    func testLaunchesAndReportsRunning() throws {
        let service = try launchService("echo hello")
        defer { service.terminate() }
        XCTAssertTrue(service.state.isLive)
        XCTAssertNotNil(service.pid)
    }

    func testReachesFinishedAfterACleanExit() throws {
        XCTAssertTrue(serviceStateAfterExit("exit 0") == .finished,
                      "a clean exit must report finished")
    }

    func testReportsFailedAfterANonZeroExit() throws {
        // Reporting a crash as `finished` would hide it.
        XCTAssertEqual(serviceStateAfterExit("exit 3"), .failed)
    }

    func testUnavailableRuntimeIsReportedAndThrown() {
        let config = EngineLaunchConfig(
            runtime: .unavailable("no python found"),
            engineRoot: URL(fileURLWithPath: "/tmp"),
            projectPath: URL(fileURLWithPath: "/tmp"))
        let service = AgentProcessService(config: config)
        XCTAssertThrowsError(try service.launch()) { error in
            XCTAssertEqual((error as? EngineError)?.kind, .runtimeUnavailable)
        }
        XCTAssertEqual(service.state, .failed)
        XCTAssertNotNil(service.lastError, "the reason must be available to show a person")
    }

    func testACommandWithNoProcessIsRefused() async {
        let config = makeConfig(script: "")
        let service = AgentProcessService(config: config)
        do {
            _ = try await service.send("start")
            XCTFail("a command with no process must be refused")
        } catch {
            XCTAssertEqual((error as? EngineError)?.kind, .notRunning)
        }
    }

    // MARK: - Frames

    func testDecodesEventsFromTheChildStdout() throws {
        let expectation = expectation(description: "events arrive")
        var received: [String] = []
        let service = try launchService(
            "echo '{\"v\":1,\"seq\":1,\"type\":\"run.start\",\"payload\":{}}'")
        defer { service.terminate() }
        service.onEvent = { event in
            received.append(event.type)
            if received.count == 1 { expectation.fulfill() }
        }
        wait(for: [expectation], timeout: 5)
        XCTAssertEqual(received, ["run.start"])
    }

    func testSeparatesDiagnosticsFromEvents() throws {
        // Interleaving the streams would corrupt both the protocol and the error text.
        let eventsExpectation = expectation(description: "event")
        let diagnosticsExpectation = expectation(description: "diagnostic")
        var seenEvents = 0
        var seenDiagnostics = 0
        let service = try launchService(
            "echo '{\"v\":1,\"seq\":1,\"type\":\"run.start\",\"payload\":{}}'; echo 'a diagnostic' >&2")
        defer { service.terminate() }
        service.onEvent = { _ in seenEvents += 1; eventsExpectation.fulfill() }
        service.onDiagnostic = { _ in seenDiagnostics += 1; diagnosticsExpectation.fulfill() }
        wait(for: [eventsExpectation, diagnosticsExpectation], timeout: 5)
        XCTAssertEqual(seenEvents, 1)
        XCTAssertEqual(seenDiagnostics, 1)
    }

    func testReportsAnUnparsableLine() throws {
        let expectation = expectation(description: "unparsable")
        var text: String?
        let service = try launchService("echo 'not json at all'")
        defer { service.terminate() }
        service.onUnparsable = { value in text = value; expectation.fulfill() }
        wait(for: [expectation], timeout: 5)
        XCTAssertTrue(text?.contains("not json") ?? false, "\(text ?? "nil")")
    }

    func testDecodesALineSplitAcrossWrites() throws {
        // The realistic case for a pipe: printf emits two writes with no newline between them.
        let expectation = expectation(description: "one event")
        var count = 0
        let service = try launchService(
            "printf '{\"v\":1,\"seq\":1,\"type\":\"run.'; sleep 0.2; echo 'start\",\"payload\":{}}'")
        defer { service.terminate() }
        service.onEvent = { _ in count += 1; expectation.fulfill() }
        wait(for: [expectation], timeout: 5)
        XCTAssertEqual(count, 1, "a split line must decode as one event")
    }

    // MARK: - Cancellation

    func testTerminateStopsTheProcess() throws {
        let service = try launchService("sleep 30")
        XCTAssertTrue(service.isRunning)
        service.terminate()
        let deadline = Date().addingTimeInterval(6)
        while service.isRunning && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        XCTAssertFalse(service.isRunning, "the process must stop")
    }

    func testTerminateWithNoProcessIsHarmless() {
        let service = AgentProcessService(config: makeConfig(script: ""))
        service.terminate()
        XCTAssertEqual(service.state, .idle)
    }

    // MARK: - Introspection

    func testDescribesItsLaunchForDiagnostics() throws {
        let service = try launchService("sleep 30")
        defer { service.terminate() }
        let info = service.describe()
        // A process that has not sent `engine.ready` is `launching`, not `running`: readiness is the
        // handshake, not the spawn. `sleep 30` never sends it, so this is the honest state.
        XCTAssertEqual(info["state"], "launching")
        XCTAssertEqual(info["ready"], "no")
        XCTAssertNotNil(info["python"])
        XCTAssertNotNil(info["project"])
        XCTAssertNotNil(info["pid"])
    }

    // MARK: - Readiness

    func testRunningIsGatedOnTheReadinessFrame() throws {
        // The bug: the app reported "engine running" the instant the process spawned, so an engine that
        // died during bootstrap looked healthy. Readiness is the `engine.ready` frame, not the spawn.
        let ready = expectation(description: "ready")
        let service = try launchService("sleep 30")
        defer { service.terminate() }
        // No readiness frame yet: not running.
        XCTAssertEqual(service.state, .launching)
        XCTAssertFalse(service.isReady)
        service.onStateChange = { state in if state == .running { ready.fulfill() } }
        // Now send the handshake, as the engine does once its config and providers resolve.
        // (A second service, because the frame must arrive on the live pipe.)
        let service2 = try launchService(
            "echo '{\"v\":1,\"seq\":1,\"type\":\"engine.ready\",\"payload\":{\"providers\":[]}}'; sleep 30")
        defer { service2.terminate() }
        service2.onStateChange = { state in if state == .running { ready.fulfill() } }
        wait(for: [ready], timeout: 5)
        XCTAssertTrue(service2.isReady)
        XCTAssertEqual(service2.state, .running)
    }

    func testReadinessArrivesWhileStderrIsStillWriting() throws {
        // The bug this pins: both pipe handlers ran on ONE serial queue, so the stderr handler's
        // blocking `availableData` held the queue the stdout handler needed, and `engine.ready` sat
        // unread forever. The app stayed on "launching the engine…" while the engine was alive and
        // healthy — found by sampling the live process and seeing the stderr read parked at
        // `AgentProcessService.swift` in `read()`.
        //
        // The child therefore writes a steady stream to **stderr** and only then emits readiness on
        // stdout, which is the real engine's shape: it logs provider warnings to stderr before it
        // announces itself. On the shared queue this could not pass.
        let ready = expectation(description: "ready despite stderr traffic")
        let service = try launchService(
            "i=0; while [ $i -lt 40 ]; do echo \"provider skipped: deepseek has no API key\" >&2; "
            + "i=$((i+1)); done; "
            + "echo '{\"v\":1,\"seq\":1,\"type\":\"engine.ready\",\"payload\":{\"providers\":[]}}'; "
            + "while true; do echo \"still busy\" >&2; sleep 0.2; done")
        defer { service.terminate() }
        service.onStateChange = { state in if state == .running { ready.fulfill() } }
        wait(for: [ready], timeout: 8)
        XCTAssertTrue(service.isReady, "readiness must survive concurrent stderr output")
        XCTAssertEqual(service.state, .running)
    }

    func testReadinessIsAlsoProvenByAReply() throws {
        // **The frame was the only trigger, and that made the whole console depend on one line being
        // observed on one pipe at one moment.** If it was missed, `.launching` was permanent: no poll
        // starts, no reply is applied, and every pane shows a placeholder for an engine that is running
        // and answering. The engine reads its command stream only *after* the handshake, so a reply is
        // the same fact by a second route — this child never sends `engine.ready` at all.
        let ready = expectation(description: "ready from a reply")
        let service = try launchService(
            "while read -r line; do echo '{\"v\":1,\"seq\":0,\"type\":\"command.ack\","
            + "\"payload\":{\"cmd_id\":\"c1\",\"ok\":true,\"detail\":{}}}'; done")
        defer { service.terminate() }
        XCTAssertEqual(service.state, .launching, "no frame has been sent")
        service.onStateChange = { state in if state == .running { ready.fulfill() } }
        // The console always has commands in flight during a launch: the window loads its providers,
        // models, roster and capabilities as it opens.
        service.post("status")
        wait(for: [ready], timeout: 8)
        XCTAssertTrue(service.isReady)
        XCTAssertEqual(service.state, .running)
    }

    func testABurstIsDeliveredWholeAndReadingContinuesAfterIt() throws {
        // The reader is a loop rather than one read per readability notification. A notification fires
        // and keeps firing while the descriptor is readable, so the shape that read once per fire queued
        // hundreds of reads for bytes that had already been consumed — and every one of them then parked
        // in `read()` waiting for the next write. Both properties are asserted here: nothing in the
        // burst is lost, and the stream after it is still read.
        let ready = expectation(description: "ready after the burst")
        let lock = NSLock()
        var entered = 0
        let service = try launchService(
            "i=0; while [ $i -lt 200 ]; do echo '{\"v\":1,\"seq\":'$i',\"type\":\"node.enter\","
            + "\"payload\":{\"node_id\":\"n\"}}'; i=$((i+1)); done; "
            + "echo '{\"v\":1,\"seq\":1,\"type\":\"engine.ready\",\"payload\":{\"providers\":[]}}'; sleep 30")
        defer { service.terminate() }
        service.onEvent = { event in
            guard event.type == "node.enter" else { return }
            lock.lock(); entered += 1; lock.unlock()
        }
        service.onStateChange = { state in if state == .running { ready.fulfill() } }
        wait(for: [ready], timeout: 8)
        lock.lock()
        let total = entered
        lock.unlock()
        XCTAssertEqual(total, 200)
        XCTAssertEqual(service.state, .running)
    }

    func testALaunchThatNeverReportsReadyIsAbandoned() throws {
        // The console's one unbounded wait: `.launching` was left by the readiness frame and by nothing
        // else, so an engine that never sent one left the window saying "Working…" for the life of the
        // process — no failure, no retry, no end. The deadline is the bound; this pins that it fires,
        // that it says what it was waiting for, and that the stop it performs cannot rewrite the verdict
        // as a clean finish a moment later.
        let failed = expectation(description: "abandoned")
        let service = try launchService("sleep 30", launchTimeout: 1.0)
        service.onStateChange = { state in if state == .failed { failed.fulfill() } }
        wait(for: [failed], timeout: 10)
        XCTAssertEqual(service.state, .failed)
        XCTAssertTrue(service.lastError?.message.contains("did not report ready") ?? false,
                      service.lastError?.message ?? "nil")
        // Let the SIGTERM land and the termination handler run; the verdict must survive it.
        let settled = expectation(description: "settled after the stop")
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { settled.fulfill() }
        wait(for: [settled], timeout: 5)
        XCTAssertEqual(service.state, .failed, "an abandoned launch is not a clean finish")
    }

    func testAFatalErrorFrameIsReportedAsTheFailureReason() throws {        // The engine writes its fatal reason to stdout as a typed frame precisely so the app can read
        // it before the exit — otherwise all the app knew was "exited with status 1".
        let failed = expectation(description: "failed")
        let service = try launchService(
            "echo '{\"v\":1,\"seq\":0,\"type\":\"error\",\"payload\":{\"message\":\"the engine could not start: bad config\",\"fatal\":true}}'; exit 1")
        service.onStateChange = { state in if state == .failed { failed.fulfill() } }
        wait(for: [failed], timeout: 10)
        XCTAssertEqual(service.state, .failed)
        XCTAssertEqual(service.lastError?.message, "the engine could not start: bad config")
    }

    func testAnExitBeforeReadinessSaysItNeverStarted() throws {
        // A non-zero exit with no reason and no readiness: say it never started, rather than implying a
        // run had been under way.
        let failed = expectation(description: "failed")
        let service = try launchService("exit 7")
        service.onStateChange = { state in if state == .failed { failed.fulfill() } }
        wait(for: [failed], timeout: 10)
        XCTAssertEqual(service.lastError?.kind, .launchFailed)
        XCTAssertTrue(service.lastError?.message.contains("before it finished starting") ?? false,
                      service.lastError?.message ?? "nil")
    }

    // MARK: - Helpers

    /// Launch a script and wait for the terminal state, returning it.
    private func serviceStateAfterExit(_ script: String) -> EngineState {
        let expectation = expectation(description: "exit")
        let service: AgentProcessService
        do {
            service = try launchService(script)
        } catch {
            XCTFail("launch failed: \(error)")
            return .failed
        }
        service.onStateChange = { state in
            if !state.isLive { expectation.fulfill() }
        }
        wait(for: [expectation], timeout: 10)
        return service.state
    }
}
