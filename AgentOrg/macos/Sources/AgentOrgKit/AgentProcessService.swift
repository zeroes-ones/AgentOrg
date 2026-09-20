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
    /// When the command went on the wire. Kept so the UI can say how long *this* command has been
    /// outstanding rather than falling back to the age of the oldest one, which is a different number
    /// and the one a person would act on.
    let sentAt: Date
    /// What was sent, so a slow command can be named. A spinner with no noun on it is the thing this
    /// whole mechanism exists to remove.
    let type: String
}

/// A command that is on the wire and has not been acknowledged yet.
///
/// A value, published to the UI rather than the service's own dictionary: the dictionary is guarded by a
/// lock and read from process queues, and handing a view a live view of it would be handing it the lock.
public struct OutstandingCommand: Equatable, Sendable, Identifiable {
    public let type: String
    public let sentAt: Date
    /// The deadline this command was sent with, so the UI can show the budget as well as the elapsed
    /// time — "waiting 8s of 60s" is answerable, "waiting" is not.
    public let deadline: Date

    public var id: String { "\(type)@\(sentAt.timeIntervalSince1970)" }

    public func elapsed(now: Date) -> TimeInterval { max(0, now.timeIntervalSince(sentAt)) }
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

    /// Whether the child has sent `engine.ready` — the proof it is actually usable.
    ///
    /// A process that is *spawned* is not an engine that *works*: a configuration or bootstrap failure
    /// is a process that exists for a moment and exits. Without this flag the app called that moment
    /// "running", printed a pid, and left the UI looking healthy while nothing ran. So `.running` is
    /// only published once this is true; before it, the state stays `.launching`.
    private var _ready = false
    /// The reason reported by a fatal `error` frame, kept so the exit can say *why* rather than only
    /// "status 1". Set from the frame, which the child writes to stdout precisely so the app can read
    /// it before the exit.
    private var _fatalReason: String?
    /// Whether **we** asked this process to stop, rather than it dying on its own.
    ///
    /// **A deliberate stop must not be reported as a crash.** `terminate()` sends SIGTERM, and a signal
    /// is not reliably caught: measured on this machine, a SIGTERM landing in the engine's first ~0.2 s
    /// — before `serve.py` installs its handler — takes the default action, so the process is killed
    /// *by* the signal and Foundation reports that as status 15 instead of a clean 0 (a SIGTERM to a
    /// ready engine exits 0; three of three runs each way). The termination handler treated any
    /// non-zero status as a failure, and the console treats a failure as news: a prominent refusal
    /// banner, an interrupt notification, and an automatic relaunch four seconds later that overrules
    /// the person who pressed Stop.
    ///
    /// Seen end to end in the running app's own log, at this bridge's expense:
    ///
    ///     12:13:52.282  engine stopping…
    ///     12:13:54.644  engine failed: the engine exited with status 15
    ///     12:13:54.645  auto-restart 1/3 in 4s
    ///     12:13:58.894  launching the engine…
    ///     12:13:59.214  engine ready (pid 93225)
    ///
    /// — every pane empty for the 6.9 s in between, which is the "going off blank and coming back"
    /// report. A process we asked to stop that stopped is a *finish*, whatever its exit status says.
    ///
    /// Set only where a live process is being asked to stop. A `terminate()` that finds the process
    /// already gone does **not** set it: that process may have crashed, and calling a crash a finish
    /// would hide it.
    private var _stopRequested = false

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
    /// Whether the engine has confirmed it is usable (sent `engine.ready`).
    public var isReady: Bool { stateLock.withLock { _ready } }

    /// The commands currently on the wire, so the UI can name a slow one.
    ///
    /// A snapshot, not a live view: the real dictionary is guarded by `pendingLock` and mutated from the
    /// process queues, and handing that to a view would be handing it the lock. A copy of the four
    /// fields the UI needs is small, and there is at most a handful of commands in flight at a time.
    public var outstandingCommands: [OutstandingCommand] {
        pendingLock.withLock {
            pending.values.map {
                OutstandingCommand(type: $0.type, sentAt: $0.sentAt, deadline: $0.deadline)
            }
        }
    }

    // MARK: - Process state

    private let config: EngineLaunchConfig
    //: One serial queue **per pipe**, not one shared between them.
    //:
    //: A shared queue was the cause of the app hanging on "launching the engine…" forever, found by
    //: sampling a live process: the stderr handler's `availableData` was parked in `read()` holding
    //: the single queue, so the stdout handler — which is where `engine.ready` arrives — could never
    //: run. The engine was alive and healthy the whole time, and the app could not hear it.
    //:
    //: The reasoning that produced the shared queue was sound as far as it went (the drain must not
    //: race a handler on the *same* pipe) but the conclusion was one queue too few: serialising the
    //: two pipes against each other buys nothing and costs correctness, because a pipe read blocks
    //: until its own stream produces bytes. Each pipe now has its own queue, so a blocked read on one
    //: cannot starve the other, and per-pipe ordering — the property that actually matters — is kept.
    private let queue = DispatchQueue(label: "org.agentorg.engine-stdout", qos: .userInitiated)
    private let stderrQueue = DispatchQueue(label: "org.agentorg.engine-stderr", qos: .utility)
    private let commandQueue = DispatchQueue(label: "org.agentorg.engine-commands", qos: .userInitiated)
    private var process: Process?
    private var stdoutDecoder = LineDecoder()
    private var stderrDecoder = LineDecoder()

    private let pendingLock = NSLock()
    private var pending: [String: PendingCommand] = [:]
    private var commandSequence = 0
    /// Guards the command pipe's write end, and records whether we have closed it.
    ///
    /// **Without this, a command that races a stop crashes the app.** Writing to a file handle whose fd
    /// is already closed raises an `NSFileHandleOperationException` — an Objective-C exception, which
    /// Swift cannot catch, so `try`/`catch` does not help and the process dies. Reproduced by a `status`
    /// poll landing in the moment between `terminate()` closing our end of the pipe and the process
    /// actually exiting: the app vanished instead of stopping, taking any unsaved state with it.
    ///
    /// A lock rather than a plain flag for the same reason: checking the flag and then writing is two
    /// steps, and the close can land between them, which is the race itself. The write is performed
    /// *inside* the lock so the two cannot interleave. The tradeoff this buys, stated plainly: a write
    /// that blocks because the pipe's buffer is full would hold the lock, and `terminate()` would wait
    /// on it. That is acceptable because the engine drains stdin continuously — a full buffer means it
    /// has stopped reading, which means it is about to be SIGKILLed anyway — and it is strictly better
    /// than an uncatchable crash on the ordinary Stop button.
    private let writeLock = NSLock()
    private var stdinClosed = false
    /// The timer that enforces `commandTimeout`.
    ///
    /// **Enforcement, not just a recorded deadline.** `PendingCommand.deadline` documented a
    /// `.commandTimeout` error from the day it was added and nothing ever compared against it — so a
    /// command the engine never answered suspended its continuation for the life of the process. For a
    /// UI that is the worst possible shape: the caller awaiting it never resumes, so anything sequenced
    /// behind it (a panel refresh, a run start, the next command on that path) silently never happens,
    /// and the app is not hung but *stopped*, one await at a time. The timer is what makes the
    /// documented timeout true.
    private var timeoutTimer: DispatchSourceTimer?
    /// Guards `timeoutTimer`, which three different queues create and cancel. See `startTimeoutSweep`.
    private let timeoutLock = NSLock()

    private let terminationGrace: TimeInterval
    /// How long a command may wait for its acknowledgement.
    ///
    /// **A stored value, not a shared constant.** It used to be a 600 s default argument and nothing
    /// else, which made the one number a person might legitimately want to change — how long a slow
    /// engine is worth waiting for — unreachable from outside this initialiser. Now the console owns
    /// the policy (see `OrgController.commandTimeout`) and this is the value it was handed, so there is
    /// exactly one place to change it and a test can inject a short one instead of waiting ten minutes.
    private let commandTimeout: TimeInterval

    /// The deadline the console's own commands are sent with, for the UI to quote.
    ///
    /// A computed property rather than a stored one: the controller needs the number to *describe* the
    /// budget while a command is outstanding, and it must be the same number the timeout uses — two
    /// independently configured copies would let the UI promise a window the bridge does not honour.
    public var timeout: TimeInterval { commandTimeout }

    public init(config: EngineLaunchConfig,
                terminationGrace: TimeInterval = 5.0,
                commandTimeout: TimeInterval = AgentProcessService.defaultCommandTimeout) {
        self.config = config
        self.terminationGrace = terminationGrace
        // Floored here rather than only at the call site: a zero or negative budget would time every
        // command out the instant it was sent, which turns the bridge into one that cannot drive the
        // engine at all. The console floors its own value too, but a guarantee that depends on the
        // caller having done so is not a guarantee.
        self.commandTimeout = max(1, commandTimeout)
    }

    /// How long a command waits for its acknowledgement before it is reported as timed out.
    ///
    /// Ten minutes, and the trade is worth stating because it is not obviously right. An engine command
    /// is *not* meant to be quick — `improve` runs the whole behavioural suite, and a first `status` on
    /// a cold workspace reads and builds an orchestrator — so a short timeout would fail commands that
    /// were working. But 600 s of silence is also indistinguishable from a wedged engine, which is why
    /// the console no longer relies on the timeout alone to communicate: it says in the UI, within a
    /// couple of seconds, that a command is outstanding and names it. The timeout is the backstop for
    /// "no answer ever", not the mechanism for "this is taking a while".
    public nonisolated static let defaultCommandTimeout: TimeInterval = 600

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

        stdoutDecoder = LineDecoder()
        stderrDecoder = LineDecoder()

        // A `readabilityHandler` fires *repeatedly* with empty data once the pipe reaches EOF, so a
        // handler that simply returns on empty data busy-loops for as long as the file handle lives —
        // burning a core per pipe. Detaching on the zero-byte read is the fix, and it is also where the
        // handler should be removed: the process is done writing.
        //
        // **Every read of these pipes is serialised on `queue`, and that is a fix rather than tidiness.**
        // Two threads read them: the `readabilityHandler`, driven by the run loop, and the termination
        // handler's final `drain`. When they interleave, the drain's `readDataToEndOfFile` can consume
        // the bytes the handler was about to read — and, worse, can consume *part* of a line while the
        // handler holds the other part in the decoder's buffer. The decoder is then left holding bytes
        // for a line that can never complete, and the frame is lost.
        //
        // That was a real, intermittent failure: the engine's fatal `error` frame — the one frame that
        // carries *why* it died — occasionally vanished, and the app reported "exited before it
        // finished starting" instead. It reproduced roughly once in a dozen full test runs, and more
        // often under load. Reading and decoding on one serial queue is what makes it impossible rather
        // than unlikely: whichever runs first sees the bytes, and the decoder is only ever touched by
        // one thread at a time.
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.queue.async {
                guard let self else { return }
                let data = handle.availableData
                guard !data.isEmpty else {
                    handle.readabilityHandler = nil
                    return
                }
                self.consumeStdout(data)
            }
        }

        // stderr: diagnostics. A separate decoder *and* a separate queue, because interleaving the two
        // streams would corrupt both the protocol and the error text — and because a blocking read on
        // this pipe must never be able to hold up the one carrying `engine.ready`.
        errPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.stderrQueue.async {
                guard let self else { return }
                let data = handle.availableData
                guard !data.isEmpty else {
                    handle.readabilityHandler = nil
                    return
                }
                self.consumeStderr(data)
            }
        }

        process.terminationHandler = { [weak self] finished in
            guard let self else { return }
            // **Drain before detaching.** The child may have written its fatal `error` frame to stdout
            // and exited in the same instant; a handler removed first would discard that frame and the
            // app would only have "status 1". So the remaining bytes are read and dispatched *before*
            // the handlers are torn down, which is what makes the reason survive the exit — and both
            // Both queues are synchronised before draining, one pipe each, so a drain cannot race the
            // handler servicing the same pipe. Syncing them *in turn* is safe precisely because they
            // are separate: a blocked read on stderr no longer holds the queue stdout needs, which was
            // the bug. Each sync waits only for its own pipe to become idle.
            self.queue.sync {
                self.drainStdout(outPipe: outPipe)
            }
            self.stderrQueue.sync {
                self.drainStderr(errPipe: errPipe)
            }
            self.queue.sync { self.detachStdoutHandler(outPipe: outPipe) }
            self.stderrQueue.sync { self.detachStderrHandler(errPipe: errPipe) }

            // A non-zero exit with no prior terminal state is a failure, not a clean finish. Reporting
            // it as `finished` would hide a crash — **unless we are the reason it stopped**: see
            // `_stopRequested` for why a stop the console asked for arrives looking exactly like one.
            let code = finished.terminationStatus
            let fatal = self.stateLock.withLock { self._fatalReason }
            let stopped = self.stateLock.withLock { self._stopRequested }
            let error: EngineError?
            if code == 0 || stopped {
                error = nil
            } else if let fatal, !fatal.isEmpty {
                // The engine told us why. This is the message a person needs, not "status 1".
                error = EngineError(.launchFailed, fatal)
            } else if !self.isReady {
                // Exited non-zero without ever becoming ready and without a stated reason: it never
                // started successfully. Say that, rather than implying a run had been under way.
                error = EngineError(
                    .launchFailed,
                    "the engine exited (status \(code)) before it finished starting. "
                    + "Run `engine.cli doctor` to see what failed.")
            } else {
                error = EngineError(.launchFailed, "the engine exited with status \(code)")
            }
            self.setState(code == 0 || stopped ? .finished : .failed, error: error)
            self.stopTimeoutSweep()
            self.failPendingCommands(EngineError(.notRunning, "the engine stopped"))
        }

        setState(.launching)
        do {
            try process.run()
        } catch {
            detachStdoutHandler(outPipe: outPipe)
            detachStderrHandler(errPipe: errPipe)
            let engineError = EngineError(.launchFailed,
                                          "could not start \(executable.path): \(error.localizedDescription)")
            setState(.failed, error: engineError)
            throw engineError
        }

        self.process = process
        // A fresh pipe: reopening the writable flag is what lets a relaunch send commands at all after
        // a previous run was stopped, when the close left it false.
        writeLock.lock()
        stdinClosed = false
        writeLock.unlock()
        stateLock.withLock {
            _pid = process.processIdentifier
            // A fresh process is *not* ready: readiness is the `engine.ready` frame, not this spawn.
            _ready = false
            _fatalReason = nil
            // And nothing has been asked of it yet, so a non-zero exit from here on is its own doing.
            _stopRequested = false
        }
        // Deliberately left `.launching`. The `.running` transition happens in `dispatch` when the
        // readiness frame arrives — so a process that dies during bootstrap is never shown as running.
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
        // Recorded *before* the signal, so the termination handler — which may run at any moment after
        // it — cannot mistake the stop we asked for for a crash. See `_stopRequested`.
        stateLock.withLock { _stopRequested = true }
        setState(.terminating)
        // Close our end of the command pipe **first**. The engine treats stdin EOF as a clean shutdown,
        // so this asks it to stop on its own terms — it finishes the command in flight, writes its
        // checkpoint, and exits, through the same drain a signal now goes through (`serve.py`
        // `_install_signal_handlers`, which unwinds the read loop's own way out).
        //
        // The signal is still sent, and the reason is that EOF is not always reachable: a write end
        // inherited by some other process means the pipe never closes, which is the case `serve.py`'s
        // parent watchdog exists for. The two are belt and braces, and SIGTERM is the one that works
        // when the engine is not reading at all.
        //
        // Through `closeCommandPipe` rather than straight to the handle, because this close races every
        // in-flight `send`: a write that lands after it raises an Objective-C exception Swift cannot
        // catch, which crashed the app instead of stopping it. See `writeLock`.
        if let commandPipe = process.standardInput as? Pipe {
            closeCommandPipe(commandPipe)
        }
        process.terminate()

        let deadline = Date().addingTimeInterval(terminationGrace)
        commandQueue.async { [weak self] in
            while process.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.1)
            }
            if process.isRunning {
                // SIGKILL: the engine did not honour SIGTERM or the closed pipe within the grace period.
                // Still a stop we asked for — `_stopRequested` is already set, so the exit is reported as
                // `.finished` rather than as a crash the console would retry.
                kill(process.processIdentifier, SIGKILL)
            }
            self?.stopTimeoutSweep()
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
        // Refused early when the pipe is already closed, so a command sent during a stop reports the
        // reason instead of being written into a dead handle. The `writeCommand` below covers the race
        // this check cannot: the close can still land between here and there.
        guard canSendCommands else {
            throw EngineError(.notRunning, "the engine is stopping")
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
                let sentAt = Date()
                pending[cmdId] = PendingCommand(
                    continuation: continuation,
                    deadline: sentAt.addingTimeInterval(commandTimeout),
                    sentAt: sentAt,
                    type: type)
            }
            let wrote = writeCommand(framed, to: stdin)
            guard wrote else {
                // The pipe closed between the guard at the top of `send` and here — a stop landed in
                // between. The continuation is already registered in `pending` and nothing will ever
                // answer it, so it is removed and resolved rather than left suspended.
                pendingLock.withLock { _ = pending.removeValue(forKey: cmdId) }
                continuation.resume(throwing: EngineError(.notRunning, "the engine is stopping"))
                return
            }
            // Started *after* the write, so the sweep can never fire for a command that is still in
            // this call's stack. Cheap to call repeatedly: it returns immediately when one is running.
            startTimeoutSweep()
        }
    }

    /// Write one framed command to the engine, refusing rather than crashing if the pipe is gone.
    ///
    /// The refusal is the point: a caller sending a command as the engine stops should get the same
    /// `notRunning` error as one sending after it stopped, not an Objective-C exception that takes the
    /// app down. See `writeLock` for why the check and the write are not separable.
    ///
    /// - Returns: true when the bytes went to the pipe.
    private func writeCommand(_ framed: Data, to pipe: Pipe) -> Bool {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard !stdinClosed else { return false }
        pipe.fileHandleForWriting.write(framed)
        return true
    }

    /// Close our end of the command pipe, once.
    ///
    /// Idempotent, and guarded by the same lock as the writes, so a close can never land between a
    /// caller's check and its write. `closeFile()` on an already-closed handle is itself an exception
    /// rather than a no-op, so this also makes a second `terminate()` safe.
    private func closeCommandPipe(_ pipe: Pipe) {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard !stdinClosed else { return }
        stdinClosed = true
        pipe.fileHandleForWriting.closeFile()
    }

    /// Whether the command pipe is still writable.
    ///
    /// A read of the same flag, under the same lock, for callers that need to *decide* before sending
    /// anything — the ones that would rather report "the engine is stopping" than race the write.
    public var canSendCommands: Bool {
        writeLock.withLock { !stdinClosed && (process?.isRunning ?? false) }
    }

    /// Fire a command without awaiting its acknowledgement, for a best-effort signal like `pause`.
    public func post(_ type: String, payload: [String: JSONValue] = [:]) {
        guard let process, process.isRunning, let stdin = process.standardInput as? Pipe else { return }
        let command = EngineCommand(cmdId: Protocol.newCommandId(), type: type, payload: payload)
        guard let data = try? JSONEncoder().encode(command),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        // Through `writeCommand`, not straight to the handle: this is fire-and-forget, so it is the
        // *most* likely caller to be racing a stop, and an unguarded write here is the crash described
        // on `writeLock`.
        _ = writeCommand(Data(line.utf8), to: stdin)
    }

    // MARK: - Event handling

    private func dispatch(_ event: EngineEvent) {
        // The readiness handshake. This is what turns `.launching` into `.running`, so a process that
        // was spawned but never became usable is *never* reported as running — the bug where the app
        // claimed "engine running (pid …)" for an engine that had already died.
        if event.type == "engine.ready" {
            stateLock.withLock { _ready = true }
            setState(.running)
        }
        // A fatal frame carries the reason the engine is about to exit. Recorded here, before the exit,
        // so the termination handler can report *why* instead of a bare status code. The child writes
        // this to stdout for exactly this reason: stderr never reaches the app's state.
        if event.type == "error", event.payload["fatal"]?.boolValue == true {
            let message = event.payload["message"]?.stringValue ?? "the engine reported a fatal error"
            stateLock.withLock { _fatalReason = message }
        }
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

    /// Resolve every command whose deadline has passed.
    ///
    /// Swept on a timer rather than one timer per command: a command's deadline is a plain `Date`, and
    /// cancelling a per-command timer when its ack arrives is a second thing to keep in step with the
    /// dictionary. A sweep also has the property that matters here — if an ack and a deadline race, the
    /// dictionary decides, exactly once, because both paths remove the entry before resuming it.
    ///
    /// The scan is bounded by how many commands are in flight, which is a handful, so a 250 ms tick
    /// costs nothing measurable. The tick is also what makes the timeout *feel* honest: a command is
    /// reported within a quarter second of its budget expiring rather than at the next unrelated event.
    private func sweepTimedOutCommands() {
        let now = Date()
        let expired: [PendingCommand] = pendingLock.withLock {
            let due = pending.filter { $0.value.deadline <= now }
            for key in due.keys { pending.removeValue(forKey: key) }
            return Array(due.values)
        }
        for waiter in expired {
            // Named in the message, because "a command timed out" leaves a person with nothing to act
            // on — the command is the thing they can go and look at.
            waiter.continuation.resume(throwing: EngineError(
                .commandTimeout,
                "the engine did not acknowledge `\(waiter.type)` within "
                + "\(Int(commandTimeout))s"))
        }
        // Nothing left waiting: stop ticking until the next command is sent, so an idle console holds
        // no timer. Reading `pending` under the lock here rather than reusing `expired` keeps this
        // correct when an ack arrived during the loop above.
        let stillPending = pendingLock.withLock { !pending.isEmpty }
        if !stillPending { stopTimeoutSweep() }
    }

    /// Start the sweep if it is not already running.
    ///
    /// `DispatchSourceTimer` rather than `Timer`: the sweep must not be paused by a menu being open or a
    /// window being dragged — the same reason the console's poll timer runs in `.common` mode — and a
    /// dispatch source on its own queue is immune to run-loop mode by construction.
    ///
    /// Guarded by `timeoutLock`, because the three callers are on different threads: `send` runs on
    /// whatever queue the caller is on, the sweep's own handler runs on `commandQueue`, and the
    /// termination handler runs on a Process queue. Two of those creating or cancelling a timer at once
    /// is a data race on one stored property — harmless in the common case and a crash in the rare one,
    /// which is the worst kind of defect to leave in a "the app must never appear stuck" change.
    private func startTimeoutSweep() {
        timeoutLock.lock()
        defer { timeoutLock.unlock() }
        guard timeoutTimer == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: commandQueue)
        timer.schedule(deadline: .now() + 0.25, repeating: 0.25)
        timer.setEventHandler { [weak self] in self?.sweepTimedOutCommands() }
        timeoutTimer = timer
        timer.resume()
    }

    private func stopTimeoutSweep() {
        timeoutLock.lock()
        defer { timeoutLock.unlock() }
        timeoutTimer?.cancel()
        timeoutTimer = nil
    }

    /// Stop reading the pipes. A handler left installed after the process exits would fire on a closed
    /// handle, which crashes rather than returning empty data.
    private func detachStdoutHandler(outPipe: Pipe) {
        outPipe.fileHandleForReading.readabilityHandler = nil
    }

    private func detachStderrHandler(errPipe: Pipe) {
        errPipe.fileHandleForReading.readabilityHandler = nil
    }

    /// Decode and dispatch a chunk from the child's stdout.
    ///
    /// Split out of the `readabilityHandler` so the same decoding runs both for a live read and for the
    /// final drain at exit — one code path, so a frame cannot be handled one way while running and
    /// another way while dying.
    private func consumeStdout(_ data: Data) {
        for frame in stdoutDecoder.append(data) {
            switch frame {
            case .event(let event): dispatch(event)
            case .unparsable(let text): onUnparsable?(text)
            }
        }
    }

    /// Decode and forward a chunk from the child's stderr (diagnostics).
    private func consumeStderr(_ data: Data) {
        for frame in stderrDecoder.append(data) {
            switch frame {
            case .event(let event): onDiagnostic?(event.type)
            case .unparsable(let text): onDiagnostic?(text)
            }
        }
    }

    /// Read whatever remains on both pipes and dispatch it, before the process is torn down.
    ///
    /// The child can write its last frames — most importantly a fatal `error` frame — and exit in the
    /// same instant. The `readabilityHandler` may not have fired for those bytes yet, and the
    /// termination handler used to detach the handlers first, discarding them. This synchronous read
    /// closes that race: whatever the child managed to write is decoded before the pipes are closed.
    ///
    /// `readDataToEndOfFile` is bounded by the child having exited (its write end is closed), so it
    /// cannot block indefinitely.
    /// Drain one pipe, on the queue that already owns it. Split per pipe so each is drained under the
    /// same serial queue its own handler runs on — the property that prevents a drain from racing a
    /// handler — without the two pipes waiting on one another.
    private func drainStdout(outPipe: Pipe) {
        let remainingOut = outPipe.fileHandleForReading.readDataToEndOfFile()
        if !remainingOut.isEmpty { consumeStdout(remainingOut) }
    }

    private func drainStderr(errPipe: Pipe) {
        let remainingErr = errPipe.fileHandleForReading.readDataToEndOfFile()
        if !remainingErr.isEmpty { consumeStderr(remainingErr) }
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
        info["ready"] = isReady ? "yes" : "no"
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
