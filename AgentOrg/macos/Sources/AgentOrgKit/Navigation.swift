//
//  Navigation.swift
//  AgentOrgKit
//
//  Where the window can go, in four places rather than twelve.
//
//  WHY FOUR AND NOT TWELVE
//  -----------------------
//  The previous sidebar listed twelve panels. Five of them were different renderings of one live run
//  (Activity, Flow, Work, Cost, Context), two were the same roster twice (Org and People), one was
//  disk state of a run another panel showed live (History vs Work), and one re-listed what Settings
//  already listed (Resources). A list whose rows overlap is one a person has to *learn*, and the
//  complaint that produced this rewrite was exactly that: "much confusing to use".
//
//  So the sidebar is now the questions a person actually has, and every capability that used to have a
//  row is a **section inside** one of them. Nothing was deleted, only re-homed — which is why
//  `NowSection` and `RunsSection` exist here: the discipline that each view answers one question is
//  kept, one level down.
//
//  **Four of the rows are re-homings and one is new.** `System` (`SystemPane.swift`) answers a question
//  the twelve panels never asked — what an agent may do on this machine — and the rule the count was
//  reduced under is what admits it: a row must be expressible as one question no other row asks. It is
//  not a rendering of the roster, the run, or the model.
//
//  The type lives in the kit rather than the app target because the *migration* below is logic, and
//  logic that decides what a stored preference means should be a function with a test rather than a
//  `??` in a view body.

import Foundation

/// Which destination the window is showing.
public enum Destination: String, CaseIterable, Identifiable, Sendable {
    case now = "Now"
    case runs = "Runs"
    case org = "Org"
    case system = "System"
    case setup = "Setup"

    public var id: String { rawValue }

    /// The one question this destination answers, shown as the window subtitle.
    ///
    /// **Why there is a fifth row and why it is not sprawl.** The four rows below answer questions
    /// about the *work*. `System` answers the one question about the *machine*: which of the engine's
    /// declared capabilities an agent may actually use here, what each one reaches, and which of them
    /// have no tool behind them yet. That is not a rendering of any of the other four — a grant is not
    /// a roster entry, a run, or a model — and it needed a home of its own because the toggles it
    /// replaces were buried in the hire form, where granting a capability looked like part of hiring
    /// somebody rather than a decision about the person's own machine.
    public var question: String {
        switch self {
        case .now: return "What is happening, and what do I do next?"
        case .runs: return "What has run, and what did it leave behind?"
        case .org: return "Who do I have, and who can I hire?"
        case .system: return "What may the agents do on this machine?"
        case .setup: return "Which model, which project, how much should it decide alone?"
        }
    }

    /// The sidebar glyph. SF Symbols, so it needs no assets and matches the rest of the system.
    public var symbol: String {
        switch self {
        case .now: return "dot.radiowaves.left.and.right"
        case .runs: return "clock.arrow.circlepath"
        case .org: return "person.3"
        // A hand raised over a machine: the same glyph the spine uses for "a person is needed", read
        // here as "a person decides". Deliberately not `gearshape`, which reads as app preferences and
        // would invite the assumption that this edits the machine rather than describing the grant.
        case .system: return "hand.raised.square.on.square"
        case .setup: return "slider.horizontal.3"
        }
    }

    /// The destination the app opens on.
    ///
    /// `now`, not `setup`. The app used to open on the roster — a wall of read-only rows — while the
    /// thing that needed a decision was at the bottom of a different list. Opening on the question
    /// "what needs me" is the whole point of the rewrite.
    public static let initial: Destination = .now

    /// What a *stored* sidebar selection means today.
    ///
    /// `UserDefaults`/scene storage keeps whatever the previous build wrote, and the previous build
    /// wrote one of twelve names. Mapping them rather than discarding them is worth the small function:
    /// a person who lived in the Cost panel reopens on Now with the cost section already open, instead
    /// of being dropped on an unfamiliar destination as if they had never used the app.
    ///
    /// An unrecognised string — a build from the future, a hand-edited default — resolves to
    /// `initial` rather than crashing or leaving the window blank.
    public static func migrated(fromStored raw: String?) -> Destination {
        guard let raw = raw?.trimmingCharacters(in: .whitespaces), !raw.isEmpty else { return .initial }
        if let direct = Destination(rawValue: raw) { return direct }
        switch raw {
        // The live run, in all five of its old renderings, plus the portfolio that used to lead.
        case "Portfolio", "Activity", "Flow", "Work", "Cost", "Context", "Resources":
            return .now
        // What has run: the disk view and the proposals the loop filed.
        case "History", "Improve":
            return .runs
        case "Org", "People":
            return .org
        case "Providers", "Settings", "Defaults":
            return .setup
        default:
            return .initial
        }
    }
}

/// The sections inside **Now**, in the order they are shown.
///
/// The order is the point: the present tense first (what is happening), then the board (who is on
/// what), then the figures nobody needs to act on (cost and capacity) — because the old app put the
/// metric walls on full screens of their own and buried the two panels that need input.
public enum NowSection: String, CaseIterable, Identifiable, Sendable {
    case happening = "What is happening"
    case board = "Who is on what"
    case usage = "Cost and capacity"

    public var id: String { rawValue }

    public var symbol: String {
        switch self {
        case .happening: return "list.bullet.rectangle.portrait"
        case .board: return "arrow.triangle.branch"
        case .usage: return "chart.bar"
        }
    }

    /// One line saying what the section is for, so a collapsed section still explains itself.
    public var summary: String {
        switch self {
        case .happening: return "the story so far, newest last"
        case .board: return "one row per piece of work, its owner, and what crossed between them"
        case .usage: return "what this is costing, and how full the agents' contexts are"
        }
    }
}

/// The sections inside **Runs**.
public enum RunsSection: String, CaseIterable, Identifiable, Sendable {
    case onDisk = "Runs on disk"
    case schedules = "Schedules"
    case sessions = "Sessions"
    case proposals = "Self-checks"

    public var id: String { rawValue }

    public var symbol: String {
        switch self {
        case .onDisk: return "externaldrive"
        case .schedules: return "calendar.badge.clock"
        case .sessions: return "rectangle.stack"
        case .proposals: return "wand.and.stars"
        }
    }

    public var summary: String {
        switch self {
        case .onDisk: return "read straight from the project, so it works with the engine stopped"
        case .schedules: return "objectives that fire on a clock — a file arms nothing until a "
                               + "watcher runs"
        case .sessions: return "what the engine remembers running here, and what it cost"
        case .proposals: return "what the system thinks is wrong with itself — nothing is applied"
        }
    }
}

/// The sections inside **Setup**, in the order they are shown.
///
/// A section enum rather than a hardcoded order in the view body, for the same reason the destinations
/// are one: the first-run wizard and this scrolling destination must cover the *same* ground, and the
/// audit found the two had drifted. `SetupJourney.order` is what the wizard walks; these are what the
/// destination shows, and a test asserts every journey step has a section here.
///
/// The last two are deliberately not journey steps: they explain the product and expose the engine's
/// own paths, neither of which is a precondition. Keeping them out of `SetupJourney` is what stops the
/// wizard's "Step 3 of 4" counting a section that asks for nothing.
public enum SetupSection: String, CaseIterable, Identifiable, Sendable {
    case journey = "The path"
    case model = "Model"
    case project = "Project"
    case autonomy = "Autonomy for new goals"
    case skills = "Skills library"
    case howItWorks = "What actually happens"
    case advanced = "Advanced"

    public var id: String { rawValue }

    /// The setup step this section answers, or nil for a section that asks nothing.
    public var resolvesStep: String? {
        switch self {
        case .model: return "model"
        case .project: return "project"
        case .autonomy: return "autonomy"
        case .journey, .skills, .howItWorks, .advanced: return nil
        }
    }

    public var symbol: String {
        switch self {
        case .journey: return "point.topleft.down.to.point.bottomright.curvepath"
        case .model: return "server.rack"
        case .project: return "folder"
        case .autonomy: return "hand.raised"
        case .skills: return "books.vertical"
        case .howItWorks: return "questionmark.circle"
        case .advanced: return "gearshape.2"
        }
    }

    /// One line saying what the section is for, so a collapsed section still explains itself.
    public var summary: String {
        switch self {
        case .journey: return "every step, what each one is for, and what finishing it gets you"
        case .model: return "which endpoints this org can reach, and which one it uses by default"
        case .project: return "where the agents work, and what they will touch"
        case .autonomy: return "how much a goal you start here decides without you"
        case .skills: return "where the agents' skills come from"
        case .howItWorks: return "what the org does with a goal, and what you get at the end"
        case .advanced: return "the engine's own paths, and how to start set-up again"
        }
    }

    /// The sections a person must not be able to collapse away while they still have work in them.
    ///
    /// `journey` is always open because it *is* the answer to "where am I"; the three step sections are
    /// always open because hiding the control someone was told to use is the failure the whole wizard
    /// exists to avoid.
    public var isAlwaysOpen: Bool {
        switch self {
        case .journey, .model, .project, .autonomy: return true
        case .skills, .howItWorks, .advanced: return false
        }
    }
}
