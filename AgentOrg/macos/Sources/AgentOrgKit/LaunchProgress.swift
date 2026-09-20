//
//  LaunchProgress.swift
//  AgentOrgKit
//
//  What the console can honestly say while the engine is starting.
//
//  WHY THIS EXISTS
//  ---------------
//  Starting the engine takes about a fifth of a second on a warm machine and considerably longer on a
//  cold one, and while it is happening the engine emits nothing at all: its imports, its config
//  resolution and its skill-library scan all run before the readiness frame, and the app sees silence
//  for the whole of it. The console rendered that silence as the word "Working…" and nothing else — so
//  a launch that was progressing and a launch that had wedged behind a macOS permission prompt looked
//  *identical*, and the only way to tell them apart was to open the terminal and read stderr.
//
//  So this is the difference, as a value: a start time, a stage, and the last thing the engine said.
//  Two things follow from making it a value rather than a `@State` string in a view:
//
//  1. **It is testable without a clock.** `summary(now:)` takes the current time as a parameter, so a
//     test asserts what the row reads at 1 s, at 3 s and at 25 s — instead of sleeping and hoping.
//  2. **The rule about what counts as "stuck" lives in one place.** The advice at the threshold is not
//     decoration: the single most common cause of a launch that never finishes is a TCC prompt waiting
//     off-screen, and saying so is the difference between a person waiting and a person acting.
//
//  It deliberately does *not* claim a percentage. There is no progress to report — the engine does not
//  emit step counts — and a made-up 60% bar would be exactly the confident wrong answer this codebase
//  refuses everywhere else.

import Foundation

/// A launch in flight: when it started, what stage it reached, and what the engine last said.
public struct LaunchProgress: Equatable, Sendable {

    /// How far along the launch has got, in the only terms the app can actually observe.
    ///
    /// There is no `.nearlyDone`: the app learns the engine is usable from the readiness frame and
    /// learns nothing in between, so a fourth stage would be invented rather than read.
    public enum Stage: String, Equatable, Sendable {
        /// The app has asked for a process. Brief — it lasts for the instant before the spawn returns —
        /// but it is the honest state for that instant, and it is not the same as "a process exists".
        case starting
        /// A process exists and has said nothing yet. This is where the whole bootstrap lives: the
        /// interpreter's imports, the config, the credentials, the skill library.
        case bootstrapping
        /// The engine has said something (a diagnostic, or a frame that is not readiness). The last
        /// line is shown, because the engine's own words about what it is doing beat any guess.
        case reporting
        /// The readiness frame arrived.
        case ready

        /// A word for the stage, so it is never conveyed by a spinner alone.
        public var word: String {
            switch self {
            case .starting: return "Starting"
            case .bootstrapping: return "Starting"
            case .reporting: return "Starting"
            case .ready: return "Ready"
            }
        }
    }

    /// When the app asked for the engine.
    public let startedAt: Date
    public private(set) var stage: Stage
    /// The last line the engine wrote to stderr during the launch, or nil if it wrote none.
    public private(set) var lastLine: String?
    /// How many lines the engine wrote during the launch. Bounded in the terminal, and counted here only
    /// because "it has said four things and then gone quiet for 30 s" is a different story from silence.
    public private(set) var lineCount: Int

    public init(startedAt: Date, stage: Stage = .starting, lastLine: String? = nil,
                lineCount: Int = 0) {
        self.startedAt = startedAt
        self.stage = stage
        self.lastLine = lastLine
        self.lineCount = lineCount
    }

    /// How long the launch has been running.
    public func elapsed(now: Date) -> TimeInterval { max(0, now.timeIntervalSince(startedAt)) }

    /// The stage a diagnostic line puts the launch in.
    ///
    /// A line arriving *is* the news — it means the interpreter is up and the engine is doing something
    /// — and the engine's own words are the best available answer to "what", so the line is kept
    /// verbatim rather than mapped onto a guess at a category. Classifying stderr text into stages would
    /// be a second parser for a format the app does not own, and it would break silently the first time
    /// the engine reworded a message.
    public mutating func noted(_ line: String) {
        guard stage != .ready else { return }
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        stage = .reporting
        lastLine = trimmed
        lineCount += 1
    }

    /// A process exists, so the launch has moved past asking for one.
    public mutating func spawned() {
        guard stage == .starting else { return }
        stage = .bootstrapping
    }

    public mutating func ready() { stage = .ready }

    /// Whether the launch is still in flight.
    public var isLive: Bool { stage != .ready }

    /// The sentence the spine shows, or nil when there is nothing left to say.
    ///
    /// - Parameter now: the current time, passed in rather than read, so the text is a pure function of
    ///   the value and the clock and can be asserted at any elapsed time in a test.
    public func summary(now: Date) -> String? {
        guard isLive else { return nil }
        let seconds = Int(elapsed(now: now))
        let clock = "\(seconds)s"
        switch stage {
        case .starting:
            return "Starting the engine…"
        case .bootstrapping:
            // The count is the whole point of the row: without it, "Starting the engine" is a caption,
            // and with it, a person can tell a slow launch from a stuck one.
            return "Starting the engine — \(clock), waiting for it to report ready"
        case .reporting:
            let said = lastLine.map { " — \($0)" } ?? ""
            return "Starting the engine — \(clock)\(said)"
        case .ready:
            return nil
        }
    }

    /// What to do about a launch that is taking a long time, or nil while it is still normal.
    ///
    /// The threshold is not arbitrary. The most common way for this app to hang is not a broken engine
    /// at all — it is macOS waiting on a Documents-folder permission prompt that has opened behind the
    /// window, which `run-macos-app.sh`'s plist notes explain at length. A person who is told that is
    /// one glance away from fixing it; a person watching a spinner is not. Twenty seconds is chosen
    /// because a cold interpreter import on a loaded machine is a few seconds at worst, so crossing
    /// this line means something is waiting rather than working.
    public func advice(now: Date) -> String? {
        let seconds = elapsed(now: now)
        guard isLive, seconds >= LaunchProgress.stuckAfter else { return nil }
        return "Still starting after \(Int(seconds))s. The usual cause is a macOS permission prompt "
            + "for the Documents or Desktop folder — check for one behind this window and choose "
            + "Allow. Otherwise open the terminal for the engine's own diagnostics."
    }

    /// How long a launch may take before the console says something about it.
    ///
    /// A named constant rather than a literal in the view, so the number has one home and a test can
    /// assert the boundary rather than the wording.
    public nonisolated static let stuckAfter: TimeInterval = 20
}
