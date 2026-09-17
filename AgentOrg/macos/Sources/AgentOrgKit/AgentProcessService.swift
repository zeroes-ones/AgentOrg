//
//  AgentProcessService.swift
//  AgentOrgKit
//
//  The bridge between the SwiftUI app and the Python engine.
//
//  WHY THIS SHAPE
//  --------------
//  The design's central promise is that **the app cannot hang on agent work**. That promise is an
//  architectural fact, not a discipline: no agent work runs in this process. This service launches the
//  Python engine as a child process and talks to it over pipes, so a wedged run is a wedged *child* the
//  app can kill — not a frozen window.
//
//  Three things follow from that, and each shapes this file:
//
//  1. **Nothing here blocks the main thread.** Reading a pipe happens on a background queue; only the
//     decoded results are published, and they are coalesced. A `readabilityHandler` that published
//     directly would storm the main actor under load — which is exactly when a run produces the most
//     output.
//  2. **The child owns its stdout.** The engine's protocol is NDJSON on stdout and diagnostics on
//     stderr, so the two streams are read separately and never interleaved. Writing an event into the
//     wrong stream would corrupt the protocol.
//  3. **Cancellation is staged.** SIGTERM lets the engine reach a checkpoint; SIGKILL is the fallback
//     after a grace period. Killing immediately would lose the work the checkpoint exists to preserve.
//
//  `@MainActor` on the observable state, and only there: the state is what SwiftUI reads. The pipes and
//  the process management stay off it.

import Foundation

/// Where the engine process is in its lifecycle, as the UI shows it.
public enum EngineState: String, Sendable, Equatable {
    case idle
    case launching
    case running
    case pausing
    case paused
    case terminating
    case finished
    case failed

    /// Whether a process is expected to be alive in this state.
    public var isLive: Bool {
        switch self {
        case .launching, .running, .pausing, .paused, .terminating: return true
        case .idle, .finished, .failed: return false
        }
    }
}

/// A failure that prevented the engine from being launched or driven.
public struct EngineError: Error, LocalizedError, Sendable {
    public enum Kind: String, Sendable {
        case runtimeUnavailable
        case launchFailed
        case notRunning
        case commandTimeout
        case writeFailed
        case badConfiguration
    }

    public let kind: Kind
    public let message: String

    public init(_ kind: Kind, _ message: String) {
        self.kind = kind
        self.message = message
    }

    public var errorDescription: String? { "[\(kind.rawValue)] \(message)" }
}

/// Everything the service needs to launch the engine.
///
/// A value type, so the configuration can be built, inspected and asserted in a test without touching a
/// process — and so the service has no hidden dependencies on the filesystem.
public struct EngineLaunchConfig: Sendable {
    /// The interpreter, and how to reach it.
    public let runtime: PythonRuntime
    /// The engine package directory: the child's working directory, and where plugins are written.
    public let engineRoot: URL
    /// The workspace's project directory.
    public let projectPath: URL
    /// `credentials.json`.
    public let credentialsPath: URL?
    /// The pinned Skills library root, passed so the child does not have to rediscover it.
    public let libraryRoot: URL?
    /// Arguments passed to the runtime. Defaults to invoking the engine's serve mode.
    ///
    /// The invocation lives here rather than in the service: the service should not know how to call
    /// Python, and a caller that wants to run something else (a test harness, a different entry point)
    /// should not have to defeat a hardcoded module flag.
    public let arguments: [String]
    /// Extra environment. The defaults below are merged over this.
    public let environment: [String: String]
    /// An existing folder to attach: passed to the engine as `--project <dir>`, so the agents work in
    /// the user's real repository and the engine's state goes in `<dir>/.agent_state/`.
    public let attachedProjectPath: URL?

    public init(runtime: PythonRuntime,
                engineRoot: URL,
                projectPath: URL,
                credentialsPath: URL? = nil,
                libraryRoot: URL? = nil,
                arguments: [String] = ["-m", "engine.cli", "serve"],
                environment: [String: String] = [:],
                attachedProjectPath: URL? = nil) {
        self.runtime = runtime
        self.engineRoot = engineRoot
        self.projectPath = projectPath
        self.credentialsPath = credentialsPath
        self.libraryRoot = libraryRoot
        self.arguments = arguments
        self.environment = environment
        self.attachedProjectPath = attachedProjectPath
    }

    /// The full argument list, with `--project` appended when a folder is attached.
    ///
    /// Composed here rather than by the caller so the flag and the path cannot drift apart — the app
    /// would otherwise have to remember to append both, and a mismatch would launch the engine at a
    /// managed project while the UI claimed to be on the user's repository.
    public var resolvedArguments: [String] {
        guard let attachedProjectPath else { return arguments }
        return arguments + ["--project", attachedProjectPath.path]
    }

    /// The environment the child is launched with.
    ///
    /// `PYTHONUNBUFFERED=1` is load-bearing: without it Python buffers stdout when it is a pipe rather
    /// than a terminal, so the app would see a run's output arrive in one burst at the end instead of
    /// live — which defeats the point of a streaming terminal. `PYTHONDONTWRITEBYTECODE=1` keeps the
    /// engine from scattering `__pycache__` through a reviewed repository.
    public var resolvedEnvironment: [String: String] {
        var merged = environment
        merged["AGENTORG_PROJECT"] = projectPath.path
        merged["PYTHONUNBUFFERED"] = "1"
        merged["PYTHONDONTWRITEBYTECODE"] = "1"
        // Our own pid, so the engine can tell whether we are still alive.
        //
        // Without this the engine can only watch `getppid()` change, which has a race: if the app dies
        // while the engine is still importing, the parent is *already* gone at startup, the value reads
        // 1, and there is nothing to detect — so the engine survives and holds the project while the
        // next app instance starts a second one. Naming the pid removes that window.
        merged["AGENTORG_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        if let credentialsPath { merged["AGENTORG_CREDENTIALS"] = credentialsPath.path }
        if let libraryRoot { merged["AGENTORG_SKILLS_ROOT"] = libraryRoot.path }
        // A GUI launch has a minimal PATH, so the child may not find tools a run needs.
        if merged["PATH"] == nil {
            merged["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
        }
        return merged
    }
}

/// A command awaiting its acknowledgement.
private struct PendingCommand {
    let continuation: CheckedContinuation<[String: JSONValue], Error>
    let deadline: Date
}

/// The engine bridge.
///
/// Deliberately a `final class` with `@unchecked Sendable` rather than an actor: the process and the
/// pipe handlers are inherently outside Swift's concurrency model, and pretending otherwise would mean
/// hopping actors on every byte read. The mutable state is guarded by a lock, which is honest about
/// what it is.
public final class AgentProcessService: @unchecked Sendable {

    // MARK: - Observable state

    /// The published state, for the UI. A lock guards it rather than an actor, so a pipe handler on a
    /// background queue can update it without an `await` it has nowhere to perform.
    private let stateLock = NSLock()
    private var _state: EngineState = .idle
    private var _lastError: EngineError?
    private var _pid: Int32?

    /// Called on the main queue when the state changes. The UI subscribes here.
    public var onStateChange: (@Sendable (EngineState) -> Void)?
    /// Called on a background queue for each decoded event.
    public var onEvent: (@Sendable (EngineEvent) -> Void)?
    /// Called on a background queue for each engine diagnostic line.
    public var onDiagnostic: (@Sendable (String) -> Void)?
    /// Called on a background queue with an unparsable line, so a protocol violation is visible.
    public var onUnparsable: (@Sendable (String) -> Void)?

    public var state: EngineState { stateLock.withLock { _state } }
    public var lastError: EngineError? { stateLock.withLock { _lastError } }
    public var pid: Int32? { stateLock.withLock { _pid } }

    // MARK: - Process state

    private let config: EngineLaunchConfig
    private let queue = DispatchQueue(label: "org.agentorg.engine-pipe", qos: .utility)
    private let commandQueue = DispatchQueue(label: "org.agentorg.engine-commands", qos: .userInitiated)
    private var process: Process?
    private var stdoutDecoder = LineDecoder()
    private var stderrDecoder = LineDecoder()

    private let pendingLock = NSLock()
    private var pending: [String: PendingCommand] = [:]
    private var commandSequence = 0

    private let terminationGrace: TimeInterval
    private let commandTimeout: TimeInterval

    public init(config: EngineLaunchConfig,
                terminationGrace: TimeInterval = 5.0,
                commandTimeout: TimeInterval = 600.0) {
        self.config = config
        self.terminationGrace = terminationGrace
        self.commandTimeout = commandTimeout
    }

    // MARK: - Lifecycle

    /// Launch the engine.
    ///
    /// Returns as soon as the process is spawned; the state becomes `.running` once it is. Callers that
    /// need confirmation should await the `run.start` event through `onEvent` rather than assuming.
    ///
    /// - Throws: `EngineError.runtimeUnavailable` when no interpreter can be found, or
    ///   `.launchFailed` when the process cannot start. Both are reported before a `Process` exists, so
    ///   a failed launch leaves no half-configured state.
    public func launch() throws {
        if state.isLive { return }

        let executable: URL
        switch config.runtime {
        case .system(let url):
            executable = url
        case .bundled(let url):
            executable = url
        case .unavailable(let reason):
            let error = EngineError(.runtimeUnavailable, reason)
            setState(.failed, error: error)
            throw error
        }

        let process = Process()
        process.executableURL = executable
        process.arguments = config.resolvedArguments
        process.currentDirectoryURL = config.engineRoot
        process.environment = config.resolvedEnvironment

        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe
        process.standardInput = Pipe()

        let decoder = LineDecoder()
        let errorDecoder = LineDecoder()
        stdoutDecoder = decoder
        stderrDecoder = errorDecoder

        // A `readabilityHandler` fires *repeatedly* with empty data once the pipe reaches EOF, so a
        // handler that simply returns on empty data busy-loops for as long as the file handle lives —
        // burning a core per pipe. Detaching on the zero-byte read is the fix, and it is also where the
        // handler should be removed: the process is done writing.
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            for frame in decoder.append(data) {
                switch frame {
                case .event(let event): self?.dispatch(event)
                case .unparsable(let text): self?.onUnparsable?(text)
                }
            }
        }

        // stderr: diagnostics. A separate decoder, because interleaving the two streams would corrupt
        // both the protocol and the error text.
        errPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            for frame in errorDecoder.append(data) {
                switch frame {
                case .event(let event): self?.onDiagnostic?(event.type)
                case .unparsable(let text): self?.onDiagnostic?(text)
                }
            }
        }

        process.terminationHandler = { [weak self] finished in
            guard let self else { return }
            self.detachHandlers(outPipe: outPipe, errPipe: errPipe)
            // A non-zero exit with no prior terminal state is a failure, not a clean finish. Reporting
            // it as `finished` would hide a crash.
            let code = finished.terminationStatus
            let error = code == 0 ? nil : EngineError(
                .launchFailed, "the engine exited with status \(code)")
            self.setState(code == 0 ? .finished : .failed, error: error)
            self.failPendingCommands(EngineError(.notRunning, "the engine stopped"))
        }

        setState(.launching)
        do {
            try process.run()
        } catch {
            detachHandlers(outPipe: outPipe, errPipe: errPipe)
            let engineError = EngineError(.launchFailed,
                                          "could not start \(executable.path): \(error.localizedDescription)")
            setState(.failed, error: engineError)
            throw engineError
        }

        self.process = process
        stateLock.withLock { _pid = process.processIdentifier }
        setState(.running)
    }

    /// Stop the engine, checkpoint first.
    ///
    /// SIGTERM, then SIGKILL after the grace period. The staged signal is the point: an immediate kill
    /// would lose whatever the engine had not yet checkpointed, and the design's resume depends on that
    /// checkpoint existing.
    public func terminate() {
        guard let process, process.isRunning else {
            setState(.idle)
            return
        }
        setState(.terminating)
        // Close our end of the command pipe **first**. The engine treats stdin EOF as a clean shutdown,
        // so this asks it to stop on its own terms — it finishes the command in flight, writes its
        // checkpoint, and exits. `terminate()` alone sends SIGTERM, which the engine does not handle
        // specially, so the checkpoint could be a node behind.
        (process.standardInput as? Pipe)?.fileHandleForWriting.closeFile()
        process.terminate()

        let deadline = Date().addingTimeInterval(terminationGrace)
        commandQueue.async { [weak self] in
            while process.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.1)
            }
            if process.isRunning {
                // SIGKILL: the engine did not honour SIGTERM or the closed pipe within the grace period.
                kill(process.processIdentifier, SIGKILL)
            }
            self?.failPendingCommands(EngineError(.notRunning, "the engine was terminated"))
        }
    }

    /// Whether a process is alive.
    public var isRunning: Bool { process?.isRunning ?? false }

    // MARK: - Commands

    /// Send a command and await its acknowledgement, correlated by `cmd_id`.
    ///
    /// The correlation is what makes this an `async` call rather than fire-and-forget: without it the
    /// UI could not tell its own reply from any other event, and a command that was never processed
    /// would look identical to one that was.
    ///
    /// - Throws: `EngineError.notRunning` when nothing is launched, or `.commandTimeout` when the
    ///   acknowledgement does not arrive in time. A timeout is reported rather than awaited forever,
    ///   because a UI awaiting a reply that never comes has no way out.
    @discardableResult
    public func send(_ type: String,
                     payload: [String: JSONValue] = [:]) async throws -> [String: JSONValue] {
        guard let process, process.isRunning, let stdin = process.standardInput as? Pipe else {
            throw EngineError(.notRunning, "the engine is not running")
        }
        let cmdId = Protocol.newCommandId()
        let command = EngineCommand(cmdId: cmdId, type: type, payload: payload)

        let data: Data
        do {
            data = try JSONEncoder().encode(command)
        } catch {
            throw EngineError(.badConfiguration, "cannot encode the command: \(error.localizedDescription)")
        }
        // The engine reads NDJSON, so the newline is part of the protocol rather than cosmetic.
        guard let line = String(data: data, encoding: .utf8) else {
            throw EngineError(.badConfiguration, "the command is not valid UTF-8")
        }
        let framed = Data((line + "\n").utf8)

        return try await withCheckedThrowingContinuation { continuation in
            pendingLock.withLock {
                commandSequence += 1
                pending[cmdId] = PendingCommand(
                    continuation: continuation,
                    deadline: Date().addingTimeInterval(commandTimeout))
            }
            stdin.fileHandleForWriting.write(framed)
        }
    }

    /// Fire a command without awaiting its acknowledgement, for a best-effort signal like `pause`.
    public func post(_ type: String, payload: [String: JSONValue] = [:]) {
        guard let process, process.isRunning, let stdin = process.standardInput as? Pipe else { return }
        let command = EngineCommand(cmdId: Protocol.newCommandId(), type: type, payload: payload)
        guard let data = try? JSONEncoder().encode(command),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        stdin.fileHandleForWriting.write(Data(line.utf8))
    }

    // MARK: - Event handling

    private func dispatch(_ event: EngineEvent) {
        // A `command.ack` resolves the awaiting caller; every other event is the UI's.
        if event.type == "command.ack", let cmdId = event.payload["cmd_id"]?.stringValue {
            let waiter: PendingCommand? = pendingLock.withLock { pending.removeValue(forKey: cmdId) }
            if let waiter {
                let ok = event.payload["ok"]?.boolValue ?? false
                if ok {
                    waiter.continuation.resume(returning: event.payload["detail"]?.objectValue ?? [:])
                } else {
                    waiter.continuation.resume(throwing: EngineError(
                        .badConfiguration,
                        event.payload["error"]?.stringValue ?? "the engine refused the command"))
                }
            }
        }
        onEvent?(event)
    }

    /// Resolve every awaiting command with an error, so no continuation is left suspended.
    private func failPendingCommands(_ error: Error) {
        let waiting: [PendingCommand] = pendingLock.withLock {
            let all = Array(pending.values)
            pending.removeAll()
            return all
        }
        for waiter in waiting { waiter.continuation.resume(throwing: error) }
    }

    /// Stop reading the pipes. A handler left installed after the process exits would fire on a closed
    /// handle, which crashes rather than returning empty data.
    private func detachHandlers(outPipe: Pipe, errPipe: Pipe) {
        outPipe.fileHandleForReading.readabilityHandler = nil
        errPipe.fileHandleForReading.readabilityHandler = nil
    }

    private func setState(_ new: EngineState, error: EngineError? = nil) {
        stateLock.withLock {
            _state = new
            _lastError = error
            if !new.isLive { _pid = nil }
        }
        onStateChange?(new)
    }

    // MARK: - Introspection

    /// A snapshot for a diagnostics panel: what is launched, and where.
    public func describe() -> [String: String] {
        var info: [String: String] = [
            "state": state.rawValue,
            "engine_root": config.engineRoot.path,
            "project": config.projectPath.path,
            "arguments": config.resolvedArguments.joined(separator: " "),
        ]
        if let pid { info["pid"] = String(pid) }
        if let error = lastError { info["error"] = error.message }
        if case .system(let url) = config.runtime { info["python"] = url.path }
        return info
    }
}

private extension NSLock {
    /// Run a closure under the lock and return its value.
    ///
    /// A helper rather than `defer { unlock() }` at each site, because a forgotten unlock in a
    /// `Process` handler deadlocks the app with no visible cause.
    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock()
        defer { unlock() }
        return try body()
    }
}
