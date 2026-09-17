//
//  PythonRuntime.swift
//  AgentOrgKit
//
//  Resolving an interpreter to launch the engine with.
//
//  WHY THIS IS A TYPE RATHER THAN A LOOKUP
//  ---------------------------------------
//  The design says v1 runs on the system's `python3` and a later version can swap in a frozen engine
//  bundled inside the app — without touching the process service. That swap is only possible if the
//  service is written against an abstraction, so this is it.
//
//  Two things matter more than they look:
//
//  1. **A GUI launch has a minimal environment.** A `.app` started from Finder inherits almost no
//     `PATH`, so `python3` on the developer's shell is frequently *not* found. Searching well-known
//     locations and falling back to `PATH` is what makes the difference between an app that works and
//     one that reports "runtime unavailable" on a machine where Python is plainly installed.
//  2. **"Unavailable" is a state, not an exception at launch.** The app must open so the Owner can see
//     *why* — a first-run wizard that cannot explain a missing interpreter is not a first-run wizard.
//     So resolution returns a value, and the reason travels with it.

import Foundation

/// How the engine's interpreter was found, or why it was not.
public enum PythonRuntime: Sendable, Equatable {
    /// A system or user interpreter at a known path.
    case system(URL)
    /// An interpreter bundled inside the app, for a distributable build.
    case bundled(URL)
    /// No interpreter could be found, with a reason suitable for showing a person.
    case unavailable(String)

    /// The executable to launch, when there is one.
    public var executableURL: URL? {
        switch self {
        case .system(let url), .bundled(let url): return url
        case .unavailable: return nil
        }
    }

    /// Whether a runtime is usable.
    public var isAvailable: Bool { executableURL != nil }

    /// A one-line description for the diagnostics panel.
    public var display: String {
        switch self {
        case .system(let url): return "system python3: \(url.path)"
        case .bundled(let url): return "bundled engine: \(url.path)"
        case .unavailable(let reason): return "unavailable: \(reason)"
        }
    }

    /// The version string the interpreter reports, when one can be obtained.
    ///
    /// Obtained by running it, not by guessing from the path: the engine needs 3.11 or later, and the
    /// reliable answer comes from the interpreter itself.
    public var version: String? {
        guard let executable = executableURL else { return nil }
        let process = Process()
        process.executableURL = executable
        process.arguments = ["-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return nil
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let text = String(data: data, encoding: .utf8) else { return nil }
        let version = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return version.isEmpty ? nil : version
    }
}

/// Finds a usable interpreter.
public enum PythonRuntimeResolver {

    /// The minimum interpreter the engine supports.
    ///
    /// 3.11 rather than 3.9 because the engine uses `X | None` in annotations at runtime and
    /// `tomllib`-era stdlib behaviour in places — the exact floor is asserted by a test so it cannot
    /// drift silently.
    public static let minimumVersion = (major: 3, minor: 11)

    /// Where a Python installation is likely to be, in preference order.
    ///
    /// Order matters: a Homebrew or framework Python is usually a newer, better-configured build than
    /// the stub at `/usr/bin/python3`. The system path is last among real candidates and kept because on
    /// a machine with no other Python it is the only one.
    public static func candidatePaths(home: String = NSHomeDirectory()) -> [String] {
        [
            // Apple's developer tooling location, which is where `xcrun` puts an interpreter.
            "/usr/bin/python3",
            // Homebrew on Apple Silicon, then on Intel.
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            // The python.org installer, which names its directories by minor version.
            "\(home)/Library/Python/3.14/bin/python3",
            "\(home)/Library/Python/3.13/bin/python3",
            "\(home)/Library/Python/3.12/bin/python3",
            "\(home)/Library/Python/3.11/bin/python3",
            // Anaconda, a common install on a machine used for ML work.
            "\(home)/anaconda3/bin/python3",
            "\(home)/miniconda3/bin/python3",
            // The versioned formulae Homebrew installs alongside the unversioned link.
            "/opt/homebrew/opt/python@3.13/bin/python3",
            "/opt/homebrew/opt/python@3.12/bin/python3",
            "/opt/homebrew/opt/python@3.11/bin/python3",
        ]
    }

    /// Resolve the interpreter to use.
    ///
    /// - Parameters:
    ///   - preferred: An explicit path, tried first. Passed when the Owner has chosen one.
    ///   - bundled: A frozen engine inside the app bundle, tried before the system search so a
    ///     distributable build does not depend on what the user happens to have installed.
    ///   - environment: Consulted for `PATH`, because a GUI launch has a minimal one.
    /// - Returns: A runtime, or `.unavailable` with a reason naming what was tried.
    public static func resolve(preferred: URL? = nil,
                               bundled: URL? = nil,
                               environment: [String: String] = ProcessInfo.processInfo.environment,
                               home: String = NSHomeDirectory()) -> PythonRuntime {
        var tried: [String] = []

        if let bundled {
            if FileManager.default.isExecutableFile(atPath: bundled.path) {
                return .bundled(bundled)
            }
            tried.append("bundled: \(bundled.path)")
        }

        if let preferred {
            if FileManager.default.isExecutableFile(atPath: preferred.path) {
                if let version = versionOf(preferred), isSupported(version) {
                    return .system(preferred)
                }
                tried.append("preferred: \(preferred.path) (version unsupported)")
            } else {
                tried.append("preferred: \(preferred.path) (not executable)")
            }
        }

        for path in candidatePaths(home: home) {
            guard FileManager.default.isExecutableFile(atPath: path) else { continue }
            if let version = versionOf(URL(fileURLWithPath: path)), isSupported(version) {
                return .system(URL(fileURLWithPath: path))
            }
            tried.append("\(path) (version unsupported)")
        }

        // Last: whatever `PATH` resolves, so a pyenv or virtualenv shim still works when the Owner runs
        // from a terminal.
        if let fromPath = which("python3", environment: environment),
           let version = versionOf(fromPath), isSupported(version) {
            return .system(fromPath)
        } else {
            tried.append("PATH lookup for python3")
        }

        return .unavailable(
            "no Python \(minimumVersion.major).\(minimumVersion.minor)+ interpreter found. "
            + "Tried:\n  " + tried.joined(separator: "\n  ")
            + "\nInstall Python from python.org or Homebrew (`brew install python@3.12`), "
            + "or set AGENTORG_PYTHON to its path."
        )
    }

    /// Resolve, honouring an `AGENTORG_PYTHON` override from the environment.
    public static func resolveFromEnvironment(
        _ environment: [String: String] = ProcessInfo.processInfo.environment,
        bundled: URL? = nil,
        home: String = NSHomeDirectory()
    ) -> PythonRuntime {
        var preferred: URL?
        if let raw = environment["AGENTORG_PYTHON"], !raw.isEmpty {
            preferred = URL(fileURLWithPath: raw)
        }
        return resolve(preferred: preferred, bundled: bundled, environment: environment, home: home)
    }

    // MARK: - Helpers

    /// Ask an interpreter for its version.
    static func versionOf(_ executable: URL) -> (major: Int, minor: Int)? {
        let process = Process()
        process.executableURL = executable
        process.arguments = ["-c", "import sys; print('%d %d' % sys.version_info[:2])"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return nil
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let text = String(data: data, encoding: .utf8) else { return nil }
        let parts = text.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: " ")
        guard parts.count == 2, let major = Int(parts[0]), let minor = Int(parts[1]) else { return nil }
        return (major, minor)
    }

    /// Whether a version meets the engine's floor.
    public static func isSupported(_ version: (major: Int, minor: Int)) -> Bool {
        if version.major > minimumVersion.major { return true }
        if version.major < minimumVersion.major { return false }
        return version.minor >= minimumVersion.minor
    }

    /// Resolve a bare command name against `PATH`.
    static func which(_ name: String, environment: [String: String]) -> URL? {
        let path = environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        for directory in path.split(separator: ":") {
            let candidate = URL(fileURLWithPath: String(directory)).appendingPathComponent(name)
            if FileManager.default.isExecutableFile(atPath: candidate.path) { return candidate }
        }
        return nil
    }
}
