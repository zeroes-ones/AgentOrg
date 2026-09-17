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
                               grace: TimeInterval = 2.0) throws -> AgentProcessService {
        let config = EngineLaunchConfig(
            runtime: .system(URL(fileURLWithPath: "/bin/sh")),
            engineRoot: URL(fileURLWithPath: FileManager.default.temporaryDirectory.path),
            projectPath: URL(fileURLWithPath: FileManager.default.temporaryDirectory.path),
            arguments: ["-c", script])
        let service = AgentProcessService(config: config, terminationGrace: grace)
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
        XCTAssertEqual(info["state"], "running")
        XCTAssertNotNil(info["python"])
        XCTAssertNotNil(info["project"])
        XCTAssertNotNil(info["pid"])
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
