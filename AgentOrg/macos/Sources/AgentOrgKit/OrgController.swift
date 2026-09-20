//
//  OrgController.swift
//  AgentOrgKit
//
//  The app's view model: the single place that owns engine state and exposes it to SwiftUI.
//
//  WHY IT IS IN THE KIT RATHER THAN THE APP TARGET
//  -----------------------------------------------
//  Everything substantive here is orchestrating the bridge and the workspace — launch, stream, command,
//  refresh — and none of it is a view. Keeping it in the kit means it can be tested without a running
//  UI, which matters because the interesting failures (a launch that fails, a command that times out, a
//  run that parks at a gate) are the states a person must be able to see.
//
//  It is `@MainActor` because it *is* view state. The bridge does its work off the main actor and hands
//  finished events here; nothing in this file reads a pipe or waits on a process.

import Foundation
import SwiftUI

// The window's destinations are `Destination` (see `Navigation.swift`): Now, Runs, Org, Setup.
// The twelve flat panels this file used to declare are now sections inside those four — the roster
// twice over, the three renderings of one run, and the panel that re-listed Settings all collapsed
// into the destination that answers their question. `Destination.migrated(fromStored:)` maps a
// stored panel name from the old build onto the destination that absorbed it.

/// The app's state, and the only place the engine is driven from.
@MainActor
public final class OrgController: ObservableObject {

    // MARK: - Published state

    @Published public private(set) var engineState: EngineState = .idle
    @Published public private(set) var engineError: String?
    /// A *fatal* engine failure, kept separate from `engineError` so the UI can show a prominent,
    /// persistent failure rather than a transient message. Set when the engine dies during bootstrap —
    /// the case that used to look like a healthy idle engine.
    @Published public private(set) var engineFailure: String?
    @Published public private(set) var projectPath: String = ""
    @Published public private(set) var credentialsPath: String = ""
    @Published public private(set) var libraryPath: String = ""
    /// Lines from the Python engine's own diagnostics, separate from the protocol events.
    @Published public private(set) var engineDiagnostics: [String] = []

    /// Clear the engine's own stderr lines, leaving the event stream alone.
    ///
    /// A separate array from `logs` because it is read as a *block*, not a transcript: the Now pane
    /// renders its last eight lines and Setup reads its first. That block had no way to be cleared —
    /// the terminal's clear button calls `logs.clear()`, which touches the `LogStore` and nothing here
    /// — so both kept showing the top of the session's stderr until the app was quit.
    ///
    /// The runtime line is restored rather than dropped, because it is not engine output at all: it is
    /// this controller's own record of the interpreter it resolved, and Setup reads it *positionally*
    /// (`engineDiagnostics.first`, with the `"runtime: "` prefix dropped), so an empty array there
    /// would print a chopped stderr line as the runtime.
    public func clearEngineDiagnostics() {
        guard engineDiagnostics.count > 1 else { return }   // the header alone: nothing to clear
        engineDiagnostics = ["runtime: \(runtime.display)"]
    }

    /// The roster: names, skills, models, live state.
    @Published public private(set) var agents: [[String: JSONValue]] = []
    /// The current run's status, from the engine's `status` command.
    @Published public private(set) var runStatus: [String: JSONValue] = [:]
    /// A gate the run is waiting on, if any.
    @Published public private(set) var pendingGate: [String: JSONValue]?
    /// The gate id the engine most recently decided on the goal's own authority.
    ///
    /// Kept so the console can tell "the engine released this" from "a person released this": both
    /// clear `pendingGate`, and only the engine's own decision is not the app's to forward. Bounded to
    /// one id because only the gate currently on screen matters — a set of every gate a long run ever
    /// answered would grow without bound to answer a question about one row.
    private var lastGoalDecidedGateId: String?
    /// Why the console is (or is not) going to decide a gate, as the engine reported it. Shown beside
    /// the gate so the person is told the reason rather than inferring it from a missing button.
    @Published public private(set) var gateNote: String?
    /// Node outcomes for the current run.
    @Published public private(set) var nodes: [[String: JSONValue]] = []
    /// The proposed graph, when one is awaiting approval.
    @Published public private(set) var proposedGraph: [String: JSONValue]?
    /// Model choices, from the live catalog.
    @Published public private(set) var models: [[String: JSONValue]] = []
    /// Every configured provider, with its status and model list. No key is ever included.
    @Published public private(set) var providers: [[String: JSONValue]] = []
    /// The file a provider edit writes, so the panel can show it rather than describe it vaguely.
    @Published public private(set) var providersConfigPath: String = ""
    /// The result of the most recent provider test: reachable, why not, and what models it offered.
    @Published public private(set) var providerTest: [String: JSONValue] = [:]
    /// The engine's reply to the most recent provider save: the base it actually stored, and its note
    /// when that differs from what was typed.
    ///
    /// Separate from `providerTest` because the two answer different questions — see `saveProvider`.
    /// Cleared on every save attempt so a correction from an earlier save cannot linger beside a later
    /// one that needed none.
    @Published public private(set) var providerSave: [String: JSONValue] = [:]
    /// The engine's reply to the most recent provider removal: which endpoint went, what is left, and
    /// **which roster agents still name it and so can no longer be called**.
    ///
    /// A third result rather than a reuse of `providerSave`, because a removal answers a question a
    /// save does not: what it broke. The engine computes that from the live roster (`agents`,
    /// `agent_count`) and this publishes its answer verbatim — the pane names the agents from here
    /// rather than asserting the consequence in prose of its own.
    @Published public private(set) var providerRemoval: [String: JSONValue] = [:]
    /// The engine's answer to "what may an agent do on this machine": every declared capability with
    /// what it reaches, what it changes, its caution, and whether a tool exists behind it yet.
    ///
    /// Held as the raw reply rather than only as decoded `SystemCapability` rows, because the panel
    /// also has to say *why* the list is inert — the `enabled` / `full_access` switches and the three
    /// allowlists travel in the same payload, and re-fetching them separately would let the list and
    /// the switches disagree for a frame.
    @Published public private(set) var system: [String: JSONValue] = [:]
    /// The last consent decision and the holder list, so the panel can show what is approved.
    @Published public private(set) var systemConsent: [String: JSONValue] = [:]
    /// The last capability invocation's result — the answer to "what does this grant actually do".
    @Published public private(set) var systemResult: [String: JSONValue] = [:]
    /// The skills an agent can be hired for.
    @Published public private(set) var skills: [String] = []
    /// The roster the console can edit: built-ins, the Owner, and the Owner's hires.
    @Published public private(set) var roster: [[String: JSONValue]] = []
    /// The file a hire writes.
    @Published public private(set) var rosterPath: String = ""
    /// The durable goal: objective, state, whether the loop will continue, and what it has spent.
    @Published public private(set) var goal: [String: JSONValue] = [:]
    /// The activity report: the one ordered story of what the org is doing, why it stopped, and what
    /// is next. Read from the engine rather than assembled here, so the CLI and the app agree.
    @Published public private(set) var activity: [String: JSONValue] = [:]
    /// The org board: which agent has which work, what crossed between them, and what came back.
    ///
    /// Distinct from `activity` on purpose. The activity report is a *story* (what happened, in order);
    /// this is a *board* (one row per unit of work, with its owner and its information flow). Both come
    /// from the engine so the CLI and the app cannot disagree about either.
    @Published public private(set) var flow: [String: JSONValue] = [:]
    /// The effective default provider/model, and how autonomous a goal is by default.
    ///
    /// Read from the engine rather than derived here, because the engine resolves a declared default
    /// against what is actually configured and reachable — a panel that re-implemented that would show
    /// a different answer from the one the run uses.
    @Published public private(set) var defaults: [String: JSONValue] = [:]
    /// The mission: the standing purpose and the ordered objectives that serve it.
    ///
    /// The mission shows the *why* above the goal. It travels with status like the goal, so a panel
    /// can show the active objective and progress from the poll it already makes.
    @Published public private(set) var mission: [String: JSONValue] = [:]
    /// The engine's own first-run journey, when it reported one (`engine/onboarding.py::journey()`).
    ///
    /// Nil on an engine that predates the field, which is why the wizard keeps its own copy of the
    /// path: the *decision* of what needs doing should come from the engine so the CLI and this window
    /// read one answer, but a first-run screen that renders nothing on a slightly older engine is worse
    /// than one that renders its own copy. The words come from whichever exists; the tick marks always
    /// come from `setupGate`, which is live.
    @Published public private(set) var journey: SetupJourneyReport?
    /// The portfolio: the principal and every org they run.
    ///
    /// The register travels with status; the *live* picture (each org's mission, spend, blockers)
    /// is a separate, more expensive fetch the Portfolio panel asks for when it is open.
    @Published public private(set) var portfolio: [String: JSONValue] = [:]
    /// The live cross-org picture, when the Portfolio panel has fetched it: rollup + fleet status.
    @Published public private(set) var portfolioLive: [String: JSONValue] = [:]
    /// Whether the live fetch is in flight, so the panel can say so rather than look stale.
    @Published public private(set) var portfolioLoading: Bool = false
    /// The schedule for this workspace, as the engine reported it.
    ///
    /// Held whole rather than as a bare list, because the parts a person has to act on are not the
    /// entries: `load_error` says the file could not be read (nothing will fire), and `counts` is what
    /// the header reads. Flattening it to `[entries]` would drop the one field that distinguishes an
    /// empty schedule from an unreadable one.
    @Published public private(set) var schedule: [String: JSONValue] = [:]
    /// What the self-improvement loop has proposed, and what it refused.
    ///
    /// Both halves are held: a list that showed only promotions would hide the safety boundary working,
    /// and the refusals are how a person sees that a fix aimed at the eval gate was stopped rather than
    /// silently dropped.
    @Published public private(set) var proposals: [[String: JSONValue]] = []
    /// How many proposals were refused by the boundary. Surfaced so the panel can say it plainly.
    @Published public private(set) var proposalsRefused: Int = 0
    /// The refusals themselves, so the panel can show *what* was stopped and why.
    @Published public private(set) var proposalsRefusedList: [[String: JSONValue]] = []
    /// Where the proposal files live, so the panel can reveal them in Finder.
    @Published public private(set) var proposalsDirectory: String = ""
    /// The proposal ids a person has dismissed, so the next poll cannot put them back.
    ///
    /// **Held here, not just removed from `proposals`.** `dismissProposal` used to drop the row and
    /// nothing else, and the engine includes proposals in every `status` and re-globs the directory each
    /// time — so the row reappeared within two seconds, under the hand that had just removed it. The
    /// dismissal is remembered (and persisted) because dismissing is a decision about the Owner's own
    /// files, and one a relaunch would otherwise undo.
    private var hiddenProposalIds: Set<String>
    /// The key the dismissed ids are stored under, in the injected defaults.
    private static let hiddenProposalsKey = "proposals.hidden"
    /// The last improver cycle's result, so the panel can show what it just did.
    @Published public private(set) var improveResult: [String: JSONValue] = [:]
    /// Whether a cycle is running, so the button can say so instead of appearing to hang.
    @Published public private(set) var improving: Bool = false
    /// The isolated children a run dispatched, as reference frames.
    @Published public private(set) var subagents: [[String: JSONValue]] = []
    /// Where the agents are working: the attached folder, or a managed project.
    @Published public private(set) var workspace: [String: JSONValue] = [:]
    /// The transcript bytes of one child, when a person is inspecting a subagent.
    @Published public private(set) var subagentTranscript: [String: JSONValue] = [:]
    /// A transient message shown in the UI, never silently swallowed.
    ///
    /// **Self-expiring.** This field was assigned by some thirty call sites and cleared by none of
    /// them, so "run finished: …" sat in the status bar for the rest of the session — a sentence about
    /// the past rendered as the present, and one that made the notice following it look as though *it*
    /// had been there all along. Every notice now carries the moment it lapses (`noticeExpiresAt`), the
    /// poll tick drops one that has run out (`expireNoticeIfStale`), and the events that actually
    /// *resolve* a notice — the engine becoming ready, and the gate a notice announced being answered —
    /// clear it at once (`dismissNotice`).
    @Published public var notice: String? {
        didSet {
            guard notice != nil else {
                noticeExpiresAt = nil
                return
            }
            noticeExpiresAt = Date().addingTimeInterval(Self.defaultNoticeLifetime)
            // A wake-up at the moment it lapses. The poll tick sweeps too, but the tick stops when the
            // engine does — and a notice set *by* an engine failure ("a retry cannot fix that") is
            // exactly the one that would otherwise outlive the thing it is about.
            noticeExpiryTask?.cancel()
            noticeExpiryTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(Self.defaultNoticeLifetime * 1_000_000_000))
                guard !Task.isCancelled else { return }
                self?.expireNoticeIfStale(now: Date())
            }
        }
    }

    /// The moment the notice on screen lapses, or nil when there is none.
    private var noticeExpiresAt: Date?
    /// The pending lapse, held so a notice replaced by a newer one is not cleared early by the older
    /// one's scheduled sweep.
    private var noticeExpiryTask: Task<Void, Never>?

    /// How long a notice stays in the status bar.
    ///
    /// Twenty seconds: long enough to read a sentence and act on it, short enough that a stale one is
    /// gone by the time a person looks back at the window. Nothing is *hidden* by a lapse, which is why
    /// the number can be this short: the two things worth acting on have surfaces of their own — a
    /// failure keeps its banner (`engineFailure`), and a decision left to a person keeps its gate row —
    /// so the status bar is only ever the news, never the record.
    public nonisolated static let defaultNoticeLifetime: TimeInterval = 20

    /// Drop a notice whose lifetime has run out.
    ///
    /// Takes `now` rather than reading the clock, so a test asserts the lapse at any elapsed time
    /// instead of waiting twenty seconds — the same seam `LaunchProgress.summary(now:)` uses.
    func expireNoticeIfStale(now: Date = Date()) {
        guard let expiry = noticeExpiresAt, now >= expiry else { return }
        notice = nil
    }

    /// Clear the status-bar notice at a person's request.
    ///
    /// Named rather than left to `notice = nil` at each call site, so clearing it is one behaviour with
    /// one home: a view's dismiss affordance and the terminal's clear button both reach this.
    public func dismissNotice() {
        notice = nil
    }

    /// The objective the user is composing, shared so the toolbar field and the Goal menu agree.
    ///
    /// Held on the controller rather than as `@State` in one view: the File/Run/Goal menus live in the
    /// App scene, not inside the window's view hierarchy, so a `@State` there would be invisible to the
    /// menu and the two would silently disagree about what goal is about to be set.
    @Published public var goalDraft: String = ""
    @Published public private(set) var lastEventAt: Date?

    /// The offline picture of `.agent_state/`, refreshed whenever the engine is not streaming.
    ///
    /// Published so the panels can render run history, handoffs and the cache store with the engine
    /// down — the moment a person most wants to know what a run did, and the moment a console that
    /// only listens to a live child has nothing to say.
    @Published public private(set) var offlineRuns: [RunSummary] = []
    @Published public private(set) var offlineHandoffs: [HandoffSummary] = []
    @Published public private(set) var offlinePrefixes: [PrefixSummary] = []
    @Published public private(set) var offlineShapes: [[String: JSONValue]] = []
    @Published public private(set) var offlineSavings: [[String: JSONValue]] = []
    /// How full each node's context was, the last time the engine recorded it.
    ///
    /// Read from the handoffs on disk (see `ContextReading`), because `status` carries no per-node
    /// saturation and the engine emits no live one. The old Context panel answered "how full are the
    /// agents' contexts?" with a window size and a static legend and never showed a current figure;
    /// this is that figure, and it is empty rather than zero when nothing has measured one.
    @Published public private(set) var contextReadings: [ContextReading] = []
    /// The engine's archived sessions, read from disk.
    @Published public private(set) var offlineSessions: [RunStateBrowser.SessionArchive] = []
    /// A handoff the person has opened, so the browser can show one cross-reference at a time.
    @Published public private(set) var offlineHandoffDetail: HandoffSummary?
    /// Set when reading the state directory itself failed, so an unreadable workspace is reported
    /// rather than rendered as an empty one — the two look identical in a list.
    @Published public private(set) var offlineError: String?
    /// The goal document from disk, carried with the offline snapshot so no view body has to read it.
    ///
    /// A stored field rather than a computed one — see `offlineGoal` for why that matters.
    @Published public private(set) var offlineGoalDocument: [String: JSONValue] = [:]

    /// The engine's reply to the last `discard_run`, held whole so the panel reads the count and the
    /// backup path **from the engine** rather than from `moved`/`backup_dir` it wrote itself.
    ///
    /// The reply is the same dict `engine.cli discard --json` prints, because both surfaces call the
    /// one `Workspace.discard_run`. A Swift sentence assembled here would be a second account of an
    /// operation the engine already describes, and it would go stale the first time the operation
    /// changed.
    @Published public private(set) var lastDiscard: [String: JSONValue] = [:]

    /// How many automatic restarts remain before the console stops trying.
    ///
    /// Published so the failure banner can say "2 attempts left" instead of silently retrying, and so
    /// a person can see the bound approaching rather than being surprised by the app going quiet.
    @Published public private(set) var restartAttemptsRemaining: Int = 0
    /// Whether the bounded auto-restart has given up, so the banner says so plainly rather than
    /// looking like it is still working.
    @Published public private(set) var gaveUpRestarting = false
    /// Whether the failure the console gave up on is one no retry could fix.
    ///
    /// Kept distinct from `gaveUpRestarting` because the two read very differently to a person: "the
    /// retries are used up" invites waiting for the last one, while "this cannot be fixed by retrying"
    /// tells them to go and change something. Collapsing them would make the structural case look like
    /// a transient one and teach people to wait instead of acting.
    @Published public private(set) var restartDeclined = false

    /// Whether the engine is **gone and staying gone**, so a pane should show its "engine not running"
    /// content instead of its own.
    ///
    /// **The question the panes were asking was wrong, and this is the observable that answers it.**
    /// Every destination swapped itself for `EngineNotRunningView` on the first state that was not
    /// `.running` — a predicate that is true of things that are not stops at all:
    ///
    /// - `.launching` and `.terminating`, in which a process *is* expected alive (`EngineState.isLive`),
    ///   and which are the ordinary states of every launch, every stop and every relaunch;
    /// - the gap between a failure and the automatic retry, which is four seconds by design;
    /// - a relaunch after a project change, where the stop and the next launch are one operation.
    ///
    /// Measured on the running app, from its own log: `engine stopping…` → `engine failed: the engine
    /// exited with status 15` → `auto-restart 1/3 in 4s` → `launching the engine…` → `engine ready`,
    /// which emptied every pane for 6.9 s and then refilled it. That is the report this answers: "sees
    /// it going off blank and coming back".
    ///
    /// The rule, in one sentence: **a pane keeps its last content while a process is live or while the
    /// console is bringing one back, and gives way to the placeholder only once the engine has been
    /// absent for `engineAwayGrace` with nothing coming.** A person's Stop still clears the window (the
    /// engine is gone and nothing is bringing it back); a restart or a blip never does. The engine's
    /// real state is never hidden by this: the spine, the status bar and the failure banner all read
    /// `engineState` directly, so "Engine failed" and the restart count are on screen the whole time
    /// the content is being retained.
    @Published public private(set) var engineIsGone: Bool = true

    /// When a notification was last sent, so the console can show that it did (or could not).
    @Published public private(set) var lastNotification: String?

    /// Where the launch has got to, while one is in flight.
    ///
    /// The fix for "Working…" being the whole story: the engine is silent for the entire bootstrap, so
    /// before this the console could not tell a launch that was progressing from one wedged behind a
    /// macOS permission prompt. Published as a value (see `LaunchProgress`) so the row can be asserted
    /// at any elapsed time without a window.
    @Published public private(set) var launchProgress: LaunchProgress?
    /// Commands on the wire for longer than `slowCommandAfter`, so a slow engine is *said* rather than
    /// inferred from a spinner. Held here rather than read from the service on each render because the
    /// service's copy is behind a lock and a view must not touch it.
    @Published public private(set) var slowCommands: [OutstandingCommand] = []
    /// The last `engine.ready` → usable moment, so the launch row can report what the wait actually was
    /// rather than a guess. Nil until a launch completes; kept afterwards so Setup can show it.
    @Published public private(set) var lastLaunchSeconds: TimeInterval?
    /// The launch row's last rendered sentence, so a tick publishes only when the text a person reads
    /// has actually changed. See `tickProgress`.
    private var lastLaunchSummary: String?

    public let logs: LogStore
    public let writer: WorkspaceWriter
    /// The choices the *app* remembers rather than the engine: the posture a goal set here inherits,
    /// and whether the first-run questions have been answered. See `AppPreferences` for why the
    /// distinction matters.
    public let preferences: AppPreferences

    // MARK: - Dependencies

    private let runtime: PythonRuntime
    private var settings: OrgSettings
    /// The engine invocation, resolved once so a caller can substitute a different program.
    private let arguments: [String]
    private var service: AgentProcessService?
    private var snapshotTimer: Timer?
    /// The notification seam. A protocol so the *decisions* around notifying are testable without a
    /// live notification centre — see `ConsoleNotifications.swift` for why the two halves are split.
    private let notifier: any ConsoleNotifier
    /// The auto-restart bound, injected so a test can exhaust it in a fraction of a second.
    private let maxRestartAttempts: Int
    /// A pending restart, held so a burst of failures schedules one retry rather than one per failure.
    private var restartTask: Task<Void, Never>?
    /// The delay before an automatic restart, injected so a test can exhaust the bound in a fraction
    /// of a second rather than waiting twelve. Defaults to the production value.
    private let restartDelay: TimeInterval
    /// How long the engine may be absent before the panes stop showing their last content.
    ///
    /// Injected for the same reason `restartDelay` is: it is a timing decision a test must be able to
    /// drive in milliseconds rather than wait out in seconds. See `engineIsGone` for the rule.
    private let engineAwayGrace: TimeInterval
    /// When the engine was first seen absent with nothing bringing it back, so the grace can be
    /// measured from the *first* such transition rather than re-armed by each later one. Nil while the
    /// engine is live or a return is pending.
    private var engineGoneSince: Date?
    /// The one-shot that decides the presence question once the grace is out. A timer rather than a
    /// poll: the transition that starts the grace is the last event there is to react to.
    private var engineGoneSettleTask: Task<Void, Never>?
    /// The offline reader for `.agent_state/`. Shares the controller's `WorkspaceWriter`, so every
    /// read is contained by the same check the rest of the app relies on.
    private let offline: RunStateBrowser
    /// The App Nap exemption held while polling. See `startSnapshotting` for why it exists.
    ///
    /// Named `napExemption` rather than `activity` because `activity` is the published activity
    /// *report*; two things called the same thing in one type is how a confusing shadow gets added.
    private var napExemption: (any NSObjectProtocol)?
    /// Whether a re-root is waiting for the engine to actually finish stopping.
    ///
    /// Attaching or detaching a project has to stop the engine and start it again, and the stop is
    /// *asynchronous*: `terminate()` sets `.terminating` at once while the process drains in-flight work
    /// (up to ten seconds on EOF), and `launch()` refuses while any live state is set. A fixed sleep
    /// before the relaunch was therefore a race that silently lost whenever the drain outlasted it — and
    /// because a clean exit is not a reported failure, nothing else ever retried, so the engine stayed
    /// stopped with only the log saying why. See `launchOnceStopped`.
    private var relaunchAfterStop = false
    /// When the panels that are not on the two-second cadence were last read. See
    /// `refreshSlowPanelsIfDue`.
    private var lastSlowPanelRefresh: Date?
    /// Where "this proposal is dismissed" is remembered between launches. Injected so a test writes to
    /// a suite of its own rather than to the developer's real defaults.
    private let proposalDismissals: UserDefaults

    /// How long a command may wait for its acknowledgement.
    ///
    /// **The one place this number lives.** It was a 600 s default argument inside
    /// `AgentProcessService`, which is a policy decision buried in the process bridge — the layer that
    /// is supposed to know nothing about what a person is willing to wait for. It is a property of the
    /// console, so it is configured on the console, and the bridge is handed the value. A test injects a
    /// short one instead of waiting ten minutes to assert what a timeout does.
    public nonisolated static let defaultCommandTimeout: TimeInterval =
        AgentProcessService.defaultCommandTimeout

    /// How long a command may be outstanding before the console says so.
    ///
    /// Two seconds, and the trade is worth naming: a `status` poll on a healthy engine answers in
    /// milliseconds, so anything crossing this line is either genuinely slow work (`improve` runs a
    /// whole behavioural suite) or an engine that has stopped answering. Either way the person is
    /// better off being told the command's *name* than watching an unchanged window — and at two
    /// seconds the notice is early enough to be a diagnosis rather than an epitaph.
    public nonisolated static let defaultSlowCommandAfter: TimeInterval = 2

    /// How long the engine may be absent before the panes give way to "the engine is not running".
    ///
    /// 1.5 s, and both ends of that are deliberate. It has to be longer than the console's own async
    /// hops — a state transition is published through a `Task { @MainActor }`, and a relaunch after a
    /// project change sets `.finished` and starts the next launch in the *same* turn — or the grace
    /// would expire inside the console's own plumbing. And it has to be shorter than the four-second
    /// auto-restart delay, so the grace is never what decides the ordinary retry case: that one is
    /// decided by `restartTask`, which is a fact rather than a wait.
    ///
    /// It is not a delay in *reporting* the engine's state: the spine, the status bar and the failure
    /// banner read `engineState` directly and say "Engine failed" the moment it is true. Only the
    /// content underneath waits, and only so that it does not have to be taken away and put back.
    public nonisolated static let defaultEngineAwayGrace: TimeInterval = 1.5

    /// The console's own copies of the two timeouts, injected so both are assertable.
    private let commandTimeout: TimeInterval
    private let slowCommandAfter: TimeInterval
    /// Ticks while a launch or a command is in flight, so the elapsed time on screen advances.
    ///
    /// A timer rather than a recomputed-on-render string: SwiftUI only redraws when something
    /// published changes, so a row that says "12s" would sit at "12s" until an unrelated event arrived
    /// — which is precisely the "is this thing alive?" question the row exists to answer.
    private var progressTimer: Timer?
    /// When the progress tick started, so `launchProgress.elapsed` is measured from the launch rather
    /// than from the first redraw.
    private var launchTickStartedAt: Date?

    /// Everything the app needs to know about where things are.
    public struct OrgSettings: Sendable {
        public var engineRoot: URL
        public var projectPath: URL
        public var credentialsPath: URL?
        public var libraryRoot: URL?
        /// The goal a new run is planned from.
        public var goal: String
        /// When set, the engine is launched with `--project <dir>`: the agents work in this existing
        /// folder, and the engine's state goes in `<dir>/.agent_state/`. Nil means a managed project
        /// under `AgentOrg/projects/`.
        public var attachedProject: URL?

        public init(engineRoot: URL, projectPath: URL, credentialsPath: URL? = nil,
                    libraryRoot: URL? = nil, goal: String = "",
                    attachedProject: URL? = nil) {
            self.engineRoot = engineRoot
            self.projectPath = projectPath
            self.credentialsPath = credentialsPath
            self.libraryRoot = libraryRoot
            self.goal = goal
            self.attachedProject = attachedProject
        }

        /// Discover sensible defaults relative to a workspace, so the app opens on a real project
        /// rather than an empty form — the first-run experience depends on it.
        ///
        /// The slug defaults to `console` because that is what `engine.cli serve` defaults to when it
        /// is given no `--slug` (`engine/serve.py`), and the two sides have to agree on *where* the
        /// run's state lives. They did not, and that was the slug bug: the console said "demo" while
        /// the engine wrote `projects/console/`, so the run history was permanently empty and nothing
        /// explained why. `managedSlug` is now derived from the project path and passed to the child,
        /// so there is exactly one answer.
        public static func discover(repositoryRoot: URL, slug: String = "console") -> OrgSettings {
            let engine = repositoryRoot.appendingPathComponent("AgentOrg/engine").deletingLastPathComponent()
            let credentials = engine.appendingPathComponent("credentials.json")
            let project = repositoryRoot.appendingPathComponent("AgentOrg/projects/\(slug)")
            return OrgSettings(
                engineRoot: engine,
                projectPath: project,
                credentialsPath: FileManager.default.fileExists(atPath: credentials.path)
                    ? credentials : nil,
                libraryRoot: nil)
        }

        /// The same settings with a different runtime and argument list.
        ///
        /// `EngineLaunchConfig` already documents that a caller wanting to run a different program
        /// should not have to defeat a hardcoded module flag — and the console was the one caller that
        /// could not, because `launch()` built the arguments itself. This is that seam, so the
        /// console's own decisions can be exercised against a real child process: a scripted
        /// stand-in engine that emits genuine NDJSON frames and real `command.ack`s.
        public func with(runtime: PythonRuntime, arguments: [String]) -> OrgSettings {
            var copy = self
            copy.runtimeOverride = runtime
            copy.argumentsOverride = arguments
            return copy
        }

        /// An interpreter that overrides discovery. Nil means "resolve from the environment".
        public var runtimeOverride: PythonRuntime?
        /// Arguments that override the default `-m engine.cli serve`.
        public var argumentsOverride: [String]?
        /// Extra environment for the child, merged over the defaults.
        ///
        /// The seam that makes `portfolio_*` commands testable against a **real** engine: the
        /// portfolio register lives in the user-global root (`~/.agentorg/portfolio.json`, honouring
        /// `$AGENTORG_HOME`), so a test that drove org switching against the machine's real register
        /// would edit the user's own orgs. Injecting `AGENTORG_HOME` points the child at a throwaway
        /// directory, and the commands then exercise the engine's actual
        /// `_cmd_portfolio_select`/`_cmd_portfolio_remove` rather than a stand-in that agrees with
        /// whatever this build happens to send.
        public var extraEnvironment: [String: String] = [:]

        /// The managed project's name, derived from the project directory.
        ///
        /// Derived rather than stored, because a stored copy is exactly what drifted: the console held
        /// `"demo"` while the engine's own default was `"console"`. Deriving it from `projectPath`
        /// means the name the engine is told and the directory the console reads cannot disagree —
        /// they are the same string's two uses.
        ///
        /// A path with no useful last component (the filesystem root, an empty path) falls back to
        /// `console`, which is `engine.cli serve`'s own default — so a controller built without a
        /// project still launches the engine somewhere the console can find.
        public var managedSlug: String {
            let name = projectPath.lastPathComponent
            guard !name.isEmpty, name != "/" else { return "console" }
            return WorkspaceNaming.isValidSlug(name) ? name : WorkspaceNaming.slug(from: name)
        }

        public init(engineRoot: URL, projectPath: URL, credentialsPath: URL? = nil,
                    libraryRoot: URL? = nil, goal: String = "",
                    attachedProject: URL? = nil,
                    runtimeOverride: PythonRuntime? = nil,
                    argumentsOverride: [String]? = nil,
                    extraEnvironment: [String: String] = [:]) {
            self.engineRoot = engineRoot
            self.projectPath = projectPath
            self.credentialsPath = credentialsPath
            self.libraryRoot = libraryRoot
            self.goal = goal
            self.attachedProject = attachedProject
            self.runtimeOverride = runtimeOverride
            self.argumentsOverride = argumentsOverride
            self.extraEnvironment = extraEnvironment
        }
    }

    /// The `logs` parameter is optional rather than defaulted to a new `LogStore()`: a default value is
    /// evaluated in a nonisolated context, and `LogStore` is main-actor state, so the default would be an
    /// actor-isolation error. Creating it lazily here keeps the isolation explicit.
    ///
    /// `notifier` and `maxRestartAttempts` are injected for the same reason the writer and the log are
    /// ownable: the notification path and the restart bound are *policy*, not process handling, and a
    /// policy that can only be exercised by launching the app is one nobody asserts. A test supplies a
    /// recording notifier and a small attempt budget; the app supplies the system notifier and the
    /// default.
    ///
    /// `commandTimeout` and `slowCommandAfter` follow that rule too, and for a blunter reason: without
    /// them, asserting anything about a timeout means waiting out the production value.
    ///
    /// `proposalDismissals` is the one store here rather than a value: which proposals a person has
    /// dismissed has to outlive the process, because the engine re-offers every proposal it still finds
    /// on disk. It is injected for the same reason `AppPreferences` is — a test that dismissed a
    /// proposal must not write into the developer's own defaults.
    public init(settings: OrgSettings, logs: LogStore? = nil,
                notifier: ConsoleNotifier? = nil,
                maxRestartAttempts: Int = OrgController.defaultRestartAttempts,
                restartDelay: TimeInterval = OrgController.defaultRestartDelay,
                engineAwayGrace: TimeInterval = OrgController.defaultEngineAwayGrace,
                commandTimeout: TimeInterval = OrgController.defaultCommandTimeout,
                slowCommandAfter: TimeInterval = OrgController.defaultSlowCommandAfter,
                preferences: AppPreferences? = nil,
                proposalDismissals: UserDefaults = .standard) {
        self.settings = settings
        self.logs = logs ?? LogStore()
        self.writer = WorkspaceWriter(root: settings.projectPath)
        self.runtime = settings.runtimeOverride ?? PythonRuntimeResolver.resolveFromEnvironment()
        self.arguments = settings.argumentsOverride ?? ["-m", "engine.cli", "serve"]
        self.projectPath = settings.projectPath.path
        self.credentialsPath = settings.credentialsPath?.path ?? "(not set)"
        self.libraryPath = settings.libraryRoot?.path ?? "(auto-discovered)"
        self.engineDiagnostics = ["runtime: \(runtime.display)"]
        self.notifier = notifier ?? SystemConsoleNotifier()
        self.maxRestartAttempts = max(0, maxRestartAttempts)
        self.restartDelay = restartDelay
        // Floored at zero, so a misconfigured value cannot make the grace a *negative* wait that the
        // settle task would then have to interpret. Zero means "decide at once", which is the honest
        // reading of "do not hold the last content at all".
        self.engineAwayGrace = max(0, engineAwayGrace)
        // A controller starts with its full budget: the count is a *remaining* figure, and starting at
        // zero made the first launch look like it had already given up.
        self.restartAttemptsRemaining = max(0, maxRestartAttempts)
        self.offline = RunStateBrowser(writer: self.writer)
        self.preferences = preferences ?? AppPreferences()
        self.proposalDismissals = proposalDismissals
        self.hiddenProposalIds = Set(
            proposalDismissals.stringArray(forKey: Self.hiddenProposalsKey) ?? [])
        // Floors, not just assignments: a zero timeout would time every command out instantly and a
        // negative one would time it out before it was sent, so a misconfigured value must not be able
        // to turn the console into something that cannot issue a command at all.
        self.commandTimeout = max(1, commandTimeout)
        self.slowCommandAfter = max(0.1, slowCommandAfter)
    }

    /// Three attempts, then stop and say so.
    ///
    /// The number is a trade, and it is worth stating why it is neither one nor unbounded. An engine
    /// that failed to launch for a *transient* reason (a port, a lock, a race with a previous instance
    /// shutting down) benefits from a retry. An engine that failed for a *structural* reason (no
    /// interpreter, a broken config, a missing library) fails identically every time, and retrying it
    /// for ever is a crash-loop that burns the machine and hides the real problem behind a spinner.
    /// Three is enough to ride out the transient case and few enough that a structural one reaches the
    /// banner — where the reason actually is — within seconds.
    ///
    /// `nonisolated` so it can be a default argument: a default is evaluated outside the main actor, and
    /// an isolated constant there would be an error under the Swift 6 language mode.
    public nonisolated static let defaultRestartAttempts = 3
    /// How long the console waits before an automatic retry.
    ///
    /// A short, fixed delay rather than a growing backoff, and the trade is worth naming: the engine's
    /// failures are either gone by four seconds (a lock released, a previous instance reaped) or
    /// structural, and a backoff would stretch the structural case — the one that should reach the
    /// banner fast so the *reason* is on screen instead of a spinner.
    public nonisolated static let defaultRestartDelay: TimeInterval = 4

    // MARK: - Launch

    /// Whether the engine can be launched at all.
    public var canLaunch: Bool { runtime.isAvailable }

    /// The reason the runtime is unusable, for the first-run panel.
    public var runtimeProblem: String? {
        if case .unavailable(let reason) = runtime { return reason }
        return nil
    }

    /// A human-readable description of the interpreter, for the Settings pane.
    ///
    /// A computed property rather than the stored diagnostics string, because Settings wants one line
    /// and the diagnostics list is bounded and append-only for the terminal.
    public var runtimeDescription: String {
        switch runtime {
        case .system(let url): return "system python3 — \(url.path)"
        case .bundled(let url): return "bundled — \(url.path)"
        case .unavailable: return "not found"
        }
    }

    /// Why the engine cannot be launched, or nil when everything it needs is present.
    ///
    /// Deliberately checks the *engine root* and the interpreter, because those are the two paths
    /// that produce a bare "file doesn't exist" from `Process` with no indication of which one is at
    /// fault. The project and credentials paths are reported too when they are missing, since a run
    /// started against neither is a confusing failure later rather than a clear one now.
    public func launchProblem() -> String? {
        let fm = FileManager.default
        guard let interpreter = runtime.executableURL else {
            return runtimeProblem ?? "no Python interpreter was found"
        }
        if !fm.isExecutableFile(atPath: interpreter.path) {
            return "the interpreter at \(interpreter.path) is not executable"
        }
        let root = settings.engineRoot
        if !fm.fileExists(atPath: root.path) {
            return "the engine directory does not exist: \(root.path)\n"
                + "(expected <repository>/AgentOrg — the app finds it by looking for "
                + "AgentOrg/engine/cli.py above its own executable)"
        }
        // A caller that supplied its own invocation is not running the engine package, so none of the
        // module checks apply to it — and applying them would refuse a launch the caller explicitly
        // asked for. This is the seam `EngineLaunchConfig.arguments` already documents.
        guard settings.argumentsOverride == nil else { return nil }
        // **Readable, not merely present.** `fileExists` consults metadata, which macOS answers even
        // when the *contents* are protected — so a repository under `~/Documents` passed every check
        // and the failure landed in the child instead, as a console stuck on "launching the engine…"
        // with nothing on screen naming the cause.
        //
        // Checked *after* the override seam, deliberately: a caller running its own command has no
        // engine module to read, and an earlier placement refused every such launch.
        let probe = root.appendingPathComponent("engine/cli.py")
        if !fm.isReadableFile(atPath: probe.path) {
            return "the engine at \(probe.path) cannot be read — macOS is likely waiting on a "
                + "\"Documents folder\" permission prompt for this app.\n"
                + "Choose Allow, or move the AgentOrg folder somewhere macOS does not protect "
                + "(for example ~/code), then launch again."
        }
        // `engineRoot` is the *package* directory (`<repo>/AgentOrg`), which is the working directory
        // for `python3 -m engine.cli` — so the module lives one level below it, not inside it.
        let module = root.appendingPathComponent("engine/cli.py")
        if !fm.fileExists(atPath: module.path) {
            return "\(root.path) exists but \(module.path) does not, so it is not the engine package"
        }
        return nil
    }

    /// Launch the engine and begin streaming.
    ///
    /// - Parameter isRestartAttempt: true when the *app* is retrying after a failure rather than a
    ///   person asking for a launch. Only a person's launch refills the restart budget — an automatic
    ///   retry that reset its own budget would be the unbounded crash-loop this bound exists to
    ///   prevent.
    public func launch(isRestartAttempt: Bool = false) {
        // Guard on *any* live state, not just `.running`. `.launching` means a spawn is already in
        // flight, so a second call during that window would start a **second engine** on the same
        // project — two processes writing one checkpoint. That is exactly the race the app's own
        // first-run `.task` creates when the engine is started from the menu at the same moment.
        guard !engineState.isLive else { return }
        // A fresh launch clears the previous failure, so a fixed config does not leave a stale banner.
        engineFailure = nil
        engineError = nil
        restartTask?.cancel()
        restartTask = nil
        if !isRestartAttempt {
            // A deliberate launch is a person deciding to try again, so the budget is restored and a
            // previous verdict is withdrawn — otherwise fixing the cause and pressing Try again would
            // leave the app saying it had given up, and the automatic path would never try again for
            // the rest of the session.
            restartAttemptsRemaining = maxRestartAttempts
            gaveUpRestarting = false
            restartDeclined = false
        }
        logs.append(notice: "launching the engine…")
        // Started before the path checks, so the row appears the instant a person presses the button
        // rather than after the filesystem has been consulted — and so the *first* thing they see is
        // "Starting", never a blank window with a disabled button.
        beginLaunchProgress()

        // Check the paths *before* spawning anything. `Process.run()` fails with a message that names
        // whichever path component is missing — "The file 'AgentOrg' doesn't exist" — which is true but
        // says nothing about *which* of the four paths was wrong or what it should have been. Naming
        // the resolved values here is the difference between a five-minute fix and an afternoon.
        if let problem = launchProblem() {
            engineState = .failed
            engineError = problem
            engineFailure = problem
            logs.append(notice: "launch failed: \(problem)")
            // The path check is a *structural* failure: it will not pass on a retry, because the
            // filesystem has not changed. Spending the budget on it would burn three attempts and
            // delay the banner for no chance of success.
            finishLaunchProgress(outcome: "it never started")
            scheduleRestartIfBounded(structural: true)
            return
        }

        // **Name the project, so the engine writes where the console reads.**
        //
        // `engine.cli serve` defaults to `--slug console` when told nothing, while the console used
        // to assume `projects/demo/` — so for the whole managed-project case the engine wrote
        // `projects/console/.agent_state/` while the offline browser read `projects/demo/`, and the
        // run history was permanently empty with nothing on screen explaining it. Passing the slug
        // (and the root, so a non-default workspace root cannot drift either) makes one string serve
        // both purposes: the directory the console reads is the directory the engine was told to use.
        //
        // Only for a *managed* project. An attached folder is passed as `--project`, and the engine
        // derives both the slug and the root from it — adding `--slug`/`--root` alongside would name a
        // different workspace from the one the flag selects.
        var launchArguments = arguments
        if settings.attachedProject == nil {
            let directory = (settings.projectPath.path as NSString).deletingLastPathComponent
            launchArguments += ["--slug", settings.managedSlug, "--root", directory]
        }

        let config = EngineLaunchConfig(
            runtime: runtime,
            engineRoot: settings.engineRoot,
            projectPath: settings.projectPath,
            credentialsPath: settings.credentialsPath,
            libraryRoot: settings.libraryRoot,
            arguments: launchArguments,
            environment: settings.extraEnvironment,
            attachedProjectPath: settings.attachedProject)
        // **The service we are replacing stops writing to this controller.** `onStateChange` captures
        // that service strongly — it reads `service.lastError` and `service.pid` — while the service
        // holds the closure, so the two retain each other: an old service is never deallocated, and a
        // transition it publishes after its successor is installed lands on the *new* engine's state.
        // The consequence is not academic: `engineState` would go non-live (emptying every pane, and
        // re-enabling `launch()`, whose guard is `!engineState.isLive` — a second engine on the same
        // project) while the engine that replaced it is alive and bootstrapping.
        //
        // Clearing the handlers releases the cycle, and the guard in the closure below covers the other
        // half: a callback already queued on the main actor when the replacement happened. Safe here
        // because `launch()` only runs with a non-live state, so the process being forgotten has
        // already exited and its pipes are already detached.
        self.service?.onStateChange = nil
        self.service?.onEvent = nil
        self.service?.onDiagnostic = nil
        self.service?.onUnparsable = nil

        let service = AgentProcessService(config: config, commandTimeout: commandTimeout)

        service.onStateChange = { [weak self] state in
            Task { @MainActor [weak self] in
                guard let self else { return }
                // A transition from a service this console has already replaced is not news about this
                // console. See the note above for what applying one costs.
                guard self.service === service else { return }
                self.engineState = state
                self.engineError = service.lastError?.message
                switch state {
                case .running:
                    // Only now is the engine *usable*: `.running` is set from the readiness frame
                    // (`engine.ready`), not from the spawn. So this notice is a true statement, unlike
                    // the old one that printed "engine running" for a process that had already died.
                    self.logs.append(notice: "engine ready (pid \(service.pid ?? 0))")
                    // The measured wait, recorded rather than described: "the engine reached ready in
                    // 4.2 s" is a fact about this machine, and it is what makes a later 20 s launch
                    // visibly abnormal instead of a matter of opinion.
                    self.finishLaunchProgress(outcome: nil)
                    // The engine answered, so the retry budget is replenished: a run that dies for a
                    // transient reason hours later deserves the same three attempts as the first.
                    self.restartAttemptsRemaining = self.maxRestartAttempts
                    self.gaveUpRestarting = false
                    self.restartDeclined = false
                    // A fresh engine re-reads the panels the two-second poll leaves alone at the next
                    // tick rather than waiting out an interval measured from the *previous* launch — a
                    // schedule armed while the engine was down, or a capability the CLI just granted,
                    // is then a couple of seconds late rather than twenty.
                    self.lastSlowPanelRefresh = nil
                    self.startSnapshotting()
                    Task { await self.refreshOfflineState() }
                case .failed:
                    // A failure must be impossible to miss. It goes to the terminal *and* to `notice`,
                    // which the status bar and every panel surface — the whole point, because the old
                    // behaviour showed a healthy-looking engine that was doing nothing.
                    let reason = service.lastError?.message ?? "the engine failed to start"
                    self.logs.append(notice: "engine failed: \(reason)")
                    self.engineFailure = reason
                    self.finishLaunchProgress(outcome: "it failed")
                    self.stopSnapshotting()
                    // An engine that died is worth telling someone about: the whole reason this app
                    // exists is a run nobody is watching. This is not one of the engine's events — it
                    // is the *bridge's* failure, which is why it is planned directly rather than
                    // decoded from a frame.
                    self.notify(plan: NotificationPlan(
                        identifier: "engine.failed",
                        title: "The engine stopped",
                        body: reason,
                        urgency: .interrupt,
                        thread: NotificationPlanner.threadPrefix))
                    self.scheduleRestartIfBounded(structural: false)
                default:
                    if !state.isLive {
                        self.stopSnapshotting()
                        // A stop *during* a launch is not a ready engine, so the progress row must not
                        // claim one — the launch ended, it just did not succeed.
                        self.finishLaunchProgress(outcome: "it stopped")
                        // The engine stopped for a reason that is not a reported failure (a clean
                        // finish, or a person stopping it). The offline picture is refreshed here so
                        // the panels have something to show the moment the live stream ends.
                        Task { await self.refreshOfflineState() }
                    }
                }
                // **The relaunch a re-root is waiting for.** After the switch, so the stop-side
                // bookkeeping above — stopping the poll, closing out the launch row, re-reading the
                // offline state — has already run against this same transition. `launch()` refuses
                // while any live state is set, which is why the caller cannot simply call it and why
                // this is driven by the transition instead of by a sleep: see `launchOnceStopped`.
                if !state.isLive, self.relaunchAfterStop {
                    self.relaunchAfterStop = false
                    self.launch()
                }
                // Last, so it sees the state this transition left behind *and* whether the relaunch
                // above has already started the next engine. A transition is the only thing that can
                // take the engine away, so this is where the presence question is asked.
                self.refreshEnginePresence()
            }
        }
        service.onEvent = { [weak self] event in
            Task { @MainActor [weak self] in self?.handle(event) }
        }
        service.onDiagnostic = { [weak self] line in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.logs.append(diagnostic: line)
                // Appended only when it is not a repeat of the line already there. The terminal is the
                // record and keeps every line; this array is what the Now pane renders as a *block*,
                // and an engine that reprints the same banner on each restart attempt turned it into
                // the same sentence over and over — republished, and so re-rendered, for news that was
                // already on screen.
                if self.engineDiagnostics.last != line {
                    self.engineDiagnostics.append(line)
                    // Bound the diagnostics list the same way the log is bounded.
                    if self.engineDiagnostics.count > 500 {
                        self.engineDiagnostics.removeFirst(self.engineDiagnostics.count - 500)
                    }
                }
                // **The engine's own words, while a launch is running.** Everything the engine prints
                // before `engine.ready` goes to stderr, and it is the only evidence there is that the
                // bootstrap is moving — so a launch row that ignored it would be reporting silence
                // during the one phase where silence means nothing.
                self.launchProgress?.noted(line)
            }
        }
        service.onUnparsable = { [weak self] text in
            Task { @MainActor [weak self] in self?.logs.append(unparsable: text) }
        }

        self.service = service
        do {
            try service.launch()
            // The spawn returned, so a process exists. Recorded here rather than inferred later, so
            // "a process exists but has said nothing" is distinguishable from "the spawn is in flight".
            launchProgress?.spawned()
        } catch {
            engineError = error.localizedDescription
            engineFailure = error.localizedDescription
            logs.append(notice: "launch failed: \(error.localizedDescription)")
            finishLaunchProgress(outcome: "it never started")
            // A spawn that threw before any process existed is structural (a bad executable, a
            // permission problem), so it is not retried — see `scheduleRestartIfBounded`.
            scheduleRestartIfBounded(structural: true)
        }
    }

    // MARK: - Launch progress

    /// Begin reporting a launch. Called before anything else in `launch()`.
    private func beginLaunchProgress() {
        launchProgress = LaunchProgress(startedAt: Date())
        launchTickStartedAt = Date()
        lastLaunchSummary = nil
        startProgressTicking()
    }

    /// End the reporting, recording how long it took.
    ///
    /// - Parameter outcome: nil when the launch succeeded, otherwise a short phrase for how it ended
    ///   ("it failed", "it stopped"). A launch that ended without reaching ready must not leave a row
    ///   claiming readiness, and it must not silently vanish either — the terminal keeps the reason, and
    ///   this records the elapsed time so the *next* attempt has a baseline to be compared against.
    private func finishLaunchProgress(outcome: String?) {
        guard let progress = launchProgress else { return }
        let seconds = progress.elapsed(now: Date())
        if let outcome {
            logs.append(notice: "engine launch ended after \(String(format: "%.1f", seconds))s: \(outcome)")
        } else {
            // Recorded, not just logged, so Setup can say what this machine's launch actually costs
            // instead of leaving a person to wonder whether four seconds is normal.
            lastLaunchSeconds = seconds
            logs.append(notice: "engine ready in \(String(format: "%.1f", seconds))s"
                        + (progress.lineCount > 0 ? " (\(progress.lineCount) diagnostic line(s))" : ""))
        }
        launchProgress = nil
        lastLaunchSummary = nil
        stopProgressTickingIfIdle()
    }

    /// Tick once a second while there is something whose elapsed time is worth showing.
    ///
    /// One timer drives both the launch row and the slow-command row, because they answer the same
    /// question — "how long has this been going?" — and two timers for one question is two things to
    /// invalidate. `.common` mode for the same reason the poll timer uses it: a tick that stops while a
    /// menu is open would freeze the very number the row exists to keep moving.
    private func startProgressTicking() {
        guard progressTimer == nil else { return }
        let timer = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.tickProgress() }
        }
        RunLoop.main.add(timer, forMode: .common)
        progressTimer = timer
    }

    private func stopProgressTickingIfIdle() {
        guard launchProgress == nil, slowCommands.isEmpty else { return }
        progressTimer?.invalidate()
        progressTimer = nil
        launchTickStartedAt = nil
    }

    /// Refresh the derived figures the UI shows, so their clocks advance.
    ///
    /// The published values themselves are not recomputed — `launchProgress` still holds its start time
    /// and the UI derives the elapsed text from it. What this does is give an idle run loop a reason to
    /// redraw, and refresh the slow-command list from the bridge, which is where the truth lives.
    ///
    /// **Two rules, because a tick that always publishes is a window that always repaints.** A launch is
    /// reported at most once a second, and only while the sentence it renders is actually changing — and
    /// it *stops* at `LaunchProgress.stuckAfter` rather than running for the life of the process. Before
    /// that there was no bound at all: `launchProgress` was cleared only by `engine.ready`, a reported
    /// failure or a stop, so on a machine where the bootstrap hung (the Documents-folder permission
    /// prompt this file documents on `launchProblem`) the app sent `objectWillChange` every second for
    /// ever, re-evaluating every view that holds this controller. A stuck launch is now *reported* — the
    /// advice names the usual cause — and then the repainting stops.
    private func tickProgress() {
        refreshOutstandingCommands()
        // A launch that is over leaves nothing for the timer to say; a command that has been answered
        // leaves the slow list. Both are checked here so the timer stops on its own.
        stopProgressTickingIfIdle()
        guard let progress = launchProgress else { return }
        let now = Date()
        if progress.elapsed(now: now) >= LaunchProgress.stuckAfter {
            let advice = progress.advice(now: now) ?? "the engine did not report ready"
            // To both surfaces: the row it used to live on is about to be cleared, and the status bar
            // lapses (see `defaultNoticeLifetime`) while the terminal keeps its own record.
            notice = advice
            logs.append(notice: advice)
            finishLaunchProgress(
                outcome: "it did not report ready within \(Int(LaunchProgress.stuckAfter))s")
            return
        }
        // `objectWillChange` rather than mutating a published value: the elapsed time is *derived* from
        // the stored start, so there is nothing to store — only a redraw to request. Requested only when
        // the derived sentence differs from the one already on screen, so a tick that lands inside the
        // same second (or that changes nothing a person can read) does not invalidate the window.
        let summary = progress.summary(now: now)
        guard summary != lastLaunchSummary else { return }
        lastLaunchSummary = summary
        objectWillChange.send()
    }

    /// Pull the bridge's list of unanswered commands and keep the ones that have been slow.
    ///
    /// Called from the tick rather than from `send`, because a command that becomes slow does so *by
    /// the passage of time*, not by any event — there is no moment at which to notice it other than the
    /// clock. Reading it from the service rather than counting locally means the list is the bridge's
    /// own truth: a command resolved by a timeout, a termination or an ack leaves it without any
    /// bookkeeping here.
    private func refreshOutstandingCommands() {
        let outstanding = service?.outstandingCommands ?? []
        let threshold = slowCommandAfter
        let now = Date()
        let slow = outstanding
            .filter { $0.elapsed(now: now) >= threshold }
            .sorted { $0.sentAt < $1.sentAt }
        guard slow != slowCommands else { return }
        slowCommands = slow
        if !slow.isEmpty { startProgressTicking() }
    }

    /// Whether anything is currently taking long enough to be worth a row.
    ///
    /// One flag for the UI, so the spine asks a question rather than re-deriving it from two arrays.
    public var isWaitingOnTheEngine: Bool { launchProgress != nil || !slowCommands.isEmpty }

    /// Note that a command just went on the wire, so the slow-command row can appear promptly.
    ///
    /// Called from `send` and `mutate` rather than only from the tick, because the tick is a second
    /// apart and a command that is answered inside that second should never produce a row at all. This
    /// schedules a single check after the threshold elapses, and the check itself re-reads the bridge —
    /// so a command answered in the meantime leaves nothing behind.
    private func noteCommandSent() {
        startProgressTicking()
        let delay = slowCommandAfter
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(max(0, delay) * 1_000_000_000))
            self?.refreshOutstandingCommands()
        }
    }

    /// Stop the engine, letting it checkpoint first.
    ///
    /// Stops deliberately, so the bounded auto-restart does not immediately undo it: a person pressing
    /// Stop and the app restarting the engine four seconds later is the app overruling them.
    public func stop() {
        cancelAutoRestart(reason: "the engine was stopped")
        // A pending re-root does not outrank a person pressing Stop: `relaunchAfterStop` is consumed by
        // the transition it is waiting for, so without this a Stop during an attach's drain would start
        // the engine again a moment later.
        relaunchAfterStop = false
        stopSnapshotting()
        service?.terminate()
        logs.append(notice: "engine stopping…")
        Task { await refreshOfflineState() }
    }

    // MARK: - The bounded auto-restart

    /// Cancel any pending automatic restart, saying why.
    ///
    /// Every path that makes a restart meaningless — a person stopping the engine, a person launching
    /// it themselves, a project change — goes through here, so a stale scheduled retry cannot fire
    /// after the world has moved on and spawn an engine nobody asked for.
    private func cancelAutoRestart(reason: String) {
        // `restartTask` is one half of the presence rule, so the question is re-asked however this
        // returns — including when there was nothing to cancel, because the caller may be the one that
        // changed the state.
        defer { refreshEnginePresence() }
        guard restartTask != nil else { return }
        restartTask?.cancel()
        restartTask = nil
        logs.append(notice: "auto-restart cancelled: \(reason)")
    }

    /// Schedule a restart if the budget allows, or give up and say so.
    ///
    /// The bound is the whole point. An unbounded retry around a structural failure — no interpreter,
    /// a broken config, a missing library — is a crash-loop: it burns a core, fills the terminal with
    /// identical failures, and buries the reason under its own repetition. The manual "Try again"
    /// button already existed and is strictly better in that case, because a person only presses it
    /// once they have changed something. So the automatic path is a *bonus* for the transient case,
    /// bounded so it can never become the thing that hides the problem.
    ///
    /// - Parameter structural: true when the failure cannot plausibly be fixed by waiting — a missing
    ///   path, an interpreter that is not executable, a process that would not spawn. Those are
    ///   **declined rather than spent**: retrying a missing interpreter three times over twelve seconds
    ///   cannot succeed, and pretending to try would teach the person to wait for a retry that will
    ///   never work. Declining is therefore stated in its own words, not as "used up".
    private func scheduleRestartIfBounded(structural: Bool) {
        // Every way out of this function decides whether the console is bringing the engine back, which
        // is half of the presence rule (`engineIsGone`). Asking it once here, on all three exits, is why
        // this is a `defer` rather than three calls: the decision is made *after* the branches above.
        defer { refreshEnginePresence() }
        guard !engineState.isLive else { return }
        guard !structural else {
            restartDeclined = true
            notice = "the engine cannot be started, and a retry cannot fix that. "
                + "The reason is above — then press Try again."
            logs.append(notice: "auto-restart declined: this failure is not transient")
            return
        }
        guard restartAttemptsRemaining > 0 else {
            if !gaveUpRestarting {
                gaveUpRestarting = true
                // Said plainly, because an app that has silently stopped retrying looks identical to
                // one that is about to succeed — and the person would wait instead of acting.
                notice = "the engine could not be started, and the automatic retries are used up. "
                    + "Fix the cause above, then press Try again."
                logs.append(notice: "auto-restart gave up after "
                             + "\(maxRestartAttempts) attempt(s)")
            }
            return
        }
        guard restartTask == nil else { return }   // one retry in flight is enough

        restartAttemptsRemaining -= 1
        let attempt = maxRestartAttempts - restartAttemptsRemaining
        logs.append(notice: "auto-restart \(attempt)/\(maxRestartAttempts) in "
                     + "\(Int(restartDelay))s")
        restartTask = Task { [weak self] in
            let delay = await MainActor.run { [weak self] in self?.restartDelay ?? 0 }
            try? await Task.sleep(nanoseconds: UInt64(max(0, delay) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await MainActor.run { [weak self] in
                guard let self else { return }
                self.restartTask = nil
                self.launch(isRestartAttempt: true)
            }
        }
    }

    // MARK: - Whether the engine is gone, rather than between two of its states

    /// Decide whether the panes should give way to "the engine is not running", and arm the one-shot
    /// that decides it again once the grace is out.
    ///
    /// The rule, spelled out because the shape of it is the fix (see `engineIsGone` for the evidence):
    ///
    /// 1. **A live engine is not gone.** `.launching` and `.terminating` both have a process attached,
    ///    so the last content stays through a launch, a stop's drain, and a relaunch.
    /// 2. **An engine the console is bringing back is not gone either.** A pending auto-restart is a
    ///    promise, not an absence, and it is a fact (`restartTask`) rather than a wait.
    /// 3. **Otherwise the absence has to last.** Inside `engineAwayGrace` the last content stays, so a
    ///    single transition — a stale callback, a stop and a relaunch in two async hops — cannot empty
    ///    the window. Past it, the panes show the placeholder, because an engine that is gone and not
    ///    coming back must not continue to look like one that is running.
    /// 4. **Once gone, it stays gone until the engine is live again.** A further non-running transition
    ///    (`.finished` → `.idle`, say) re-asks nothing: monotonicity here is what stops the placeholder
    ///    from flickering the content back in for a moment.
    private func refreshEnginePresence(now: Date = Date()) {
        if engineState.isLive {
            engineGoneSince = nil
            cancelGoneSettle()
            if engineIsGone { engineIsGone = false }
            return
        }
        // Already showing the placeholder: nothing below can make the window emptier, and rule 4 keeps
        // a later transition from emptying *and* refilling it.
        if engineIsGone { return }
        if restartTask != nil || relaunchAfterStop {
            engineGoneSince = nil
            cancelGoneSettle()
            return
        }
        let since = engineGoneSince ?? now
        engineGoneSince = since
        let waited = now.timeIntervalSince(since)
        guard waited < engineAwayGrace else {
            engineIsGone = true
            cancelGoneSettle()
            return
        }
        // Inside the grace: hold, and arrange to look again when it is out. Re-armed from the *first*
        // absence, so a transition that arrives mid-grace cannot push the decision back for ever.
        cancelGoneSettle()
        let remaining = engineAwayGrace - waited
        engineGoneSettleTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(max(0, remaining) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await MainActor.run { [weak self] in self?.refreshEnginePresence() }
        }
    }

    private func cancelGoneSettle() {
        engineGoneSettleTask?.cancel()
        engineGoneSettleTask = nil
    }

    // MARK: - Notifications

    /// Deliver a plan, tolerating every way it can fail.
    ///
    /// The plan is decided by `NotificationPlanner` (a pure function, exhaustively testable); this
    /// only performs it. Three things are deliberately swallowed: authorisation being refused, the
    /// request failing, and the system not being there at all. A notification is a courtesy on top of
    /// a working console, so *nothing* about it may break the console — the failure mode of a missing
    /// banner is that the person looks at the window, while the failure mode of a throwing notifier is
    /// that the event handler dies and the UI stops updating entirely.
    private func notify(plan: NotificationPlan) {
        let task = Task { [weak self] in
            guard let self else { return }
            // Ask lazily, at the first moment a banner is actually wanted. Asking on launch would cost
            // a modal to someone who opened the app to read a roster, and a denial is permanent.
            var authorized = await self.notifier.isAuthorized
            if !authorized {
                authorized = await self.notifier.requestAuthorization()
            }
            guard authorized else {
                await MainActor.run { [weak self] in
                    self?.lastNotification = "notifications are off (denied in System Settings)"
                    self?.notificationTasks.removeAll { $0.isCancelled }
                }
                return
            }
            let delivered = await self.notifier.deliver(plan)
            await MainActor.run { [weak self] in
                guard let self else { return }
                self.lastNotification = delivered
                    ? "notified: \(plan.title)"
                    : "a notification could not be delivered"
            }
        }
        notificationTasks.append(task)
    }

    /// Wait for every in-flight notification attempt to finish.
    ///
    /// Production never needs this — the attempts are intentionally detached. A test does, because the
    /// interesting outcome (denied, delivered, or silently impossible) is the *result* of an async
    /// attempt, and polling for it would be a race dressed as an assertion.
    public func awaitNotifications() async {
        while !notificationTasks.isEmpty {
            let tasks = notificationTasks
            notificationTasks = []
            for task in tasks { await task.value }
        }
    }

    /// Plan and deliver for one engine event, if the planner says it is worth interrupting for.
    ///
    /// Internal rather than private so the *controller's* use of the notifier — asking lazily, and doing
    /// nothing graceful when denied — can be asserted against a recording fake. That behaviour is the
    /// part that must never break the console, and it lives here rather than in the planner.
    func notify(for event: EngineEvent) {
        // A gate the engine already answered is never a question for a person, so the planner must not
        // be told one is waiting. `gateDisposition` is the single source of that answer.
        let waitingOnAHuman = pendingGate != nil && gateDisposition.isWaitingForHuman
        guard let plan = NotificationPlanner.plan(for: event,
                                                  gateIsWaitingOnAHuman: waitingOnAHuman) else {
            return
        }
        notify(plan: plan)
    }

    /// Whether a delivery attempt is still in flight, so a test can await it rather than guess.
    ///
    /// Exposed because the delivery is deliberately fire-and-forget — it must not block the event
    /// handler — so "did it try, and what happened" is otherwise unobservable without a sleep.
    public var isDeliveringNotification: Bool { !notificationTasks.isEmpty }

    private var notificationTasks: [Task<Void, Never>] = []

    // MARK: - Commands

    /// Send one command to the engine, noting it for the slow-command row.
    ///
    /// **Every command in this class goes through here**, and that is the point: the five call sites
    /// below differ in what they do with the reply — log it, return a flag, apply it to a published
    /// field — but they must all agree on one thing, which is that a command is now outstanding and a
    /// person may need to be told if it stays that way. Adding `noteCommandSent` at each site would make
    /// that agreement a discipline, and the sixth call site someone adds would be the one that forgets.
    private func sendToEngine(_ type: String,
                              payload: [String: JSONValue]) async throws -> [String: JSONValue] {
        guard let service else {
            throw EngineError(.notRunning, "the engine is not running")
        }
        noteCommandSent()
        return try await service.send(type, payload: payload)
    }

    /// Send a command and log the outcome.
    ///
    /// The acknowledgement is awaited rather than fired and forgotten: a command that was refused must
    /// surface, or the UI would show a state the engine never entered.
    public func send(_ type: String, payload: [String: JSONValue] = [:]) async {
        guard service != nil else {
            notice = "the engine is not running"
            return
        }
        do {
            _ = try await sendToEngine(type, payload: payload)
            logs.append(notice: "→ \(type) acknowledged")
        } catch {
            let message = error.localizedDescription
            notice = message
            logs.append(notice: "→ \(type) failed: \(message)")
        }
    }

    /// Start a run from the goal in settings.
    public func startRun(goal: String? = nil, dryRun: Bool = false) async {
        var payload: [String: JSONValue] = [:]
        if let goal, !goal.isEmpty { payload["goal"] = .string(goal) }
        if dryRun { payload["dry_run"] = .bool(true) }
        await send("start", payload: payload)
        await refresh()
    }

    /// Approve the proposed graph or the gate the run is waiting on.
    ///
    /// One command covers both, because they are the same decision at different moments: "may this
    /// proceed".
    ///
    /// The gate is cleared only when the engine **accepted** the command, and that is a fix rather than
    /// a detail: this used to clear it unconditionally, so a refused approval — no run loaded, a gate
    /// already resolved — removed the gate from the UI while the engine still held it. The console then
    /// showed a run that was proceeding when it was in fact still parked, with the one control that
    /// could unstick it gone.
    ///
    /// Returns whether the engine accepted the decision.
    @discardableResult
    public func approve(note: String = "") async -> Bool {
        var payload: [String: JSONValue] = [:]
        if !note.isEmpty { payload["note"] = .string(note) }
        let ok = await sendAccepted("approve", payload: payload)
        if ok {
            // A notice that announced this gate ("waiting on you: …") is stale the moment the decision
            // is made, so it goes with the gate rather than sitting in the status bar for its full
            // lifetime. Guarded on something having actually been waiting, so an approval sent with no
            // gate on screen cannot wipe a notice about something else.
            if pendingGate != nil || proposedGraph != nil { notice = nil }
            pendingGate = nil
            proposedGraph = nil
        }
        await refresh()
        return ok
    }

    @discardableResult
    public func reject(note: String = "") async -> Bool {
        var payload: [String: JSONValue] = [:]
        if !note.isEmpty { payload["note"] = .string(note) }
        let ok = await sendAccepted("reject", payload: payload)
        if ok {
            if pendingGate != nil { notice = nil }
            pendingGate = nil
        }
        await refresh()
        return ok
    }

    /// Send a command and report whether the engine accepted it.
    ///
    /// `send` is the logging form most callers want; this is the one for a caller whose *next* line
    /// depends on the answer — clearing a gate, or telling a person it worked.
    @discardableResult
    private func sendAccepted(_ type: String,
                              payload: [String: JSONValue] = [:]) async -> Bool {
        do {
            _ = try await sendToEngine(type, payload: payload)
            logs.append(notice: "→ \(type) acknowledged")
            return true
        } catch {
            let message = error.localizedDescription
            notice = message
            logs.append(notice: "→ \(type) failed: \(message)")
            return false
        }
    }

    /// Push guidance into the run. With `asConstraint` it becomes non-negotiable, so the AR-04 machinery
    /// then preserves it verbatim across every compaction and rotation.
    ///
    /// The wire key is `constraint`, not `as_constraint`: `serve._cmd_instruct` reads
    /// `payload.get("constraint")` for the flag and takes the text itself from `text`. Sending
    /// `as_constraint` compiled, sent, was acknowledged, and was silently ignored — so ticking
    /// "Non-negotiable" produced an ordinary instruction and the one guarantee the checkbox promises
    /// did not hold. The name of the *parameter* here is the app's business; the payload key is the
    /// engine's contract.
    public func instruct(_ text: String, asConstraint: Bool = false) async {
        var payload: [String: JSONValue] = ["text": .string(text)]
        if asConstraint { payload["constraint"] = .bool(true) }
        await send("instruct", payload: payload)
    }

    /// Pause the run.
    ///
    /// Awaits the acknowledgement like every other command. It used to be `post` — fire-and-forget —
    /// which made a *refused* pause invisible: `serve._cmd_pause` answers `{paused: false, reason}` in
    /// the detail rather than failing when there is nothing to pause, and a caller that never read the
    /// acknowledgement could not tell that from success. A pause button that silently did nothing is
    /// the worst version of it, because the person stops watching.
    ///
    /// Returns whether the engine said it paused, so a caller that wants to say so can.
    @discardableResult
    public func pause() async -> Bool {
        await mutateSilently("pause")
    }

    /// Send a command, returning whether the engine accepted it, without logging the success line.
    ///
    /// `send` appends "→ pause acknowledged" to the terminal for every call; for the periodic commands
    /// that is noise. The refusal path is *not* silent — it sets `notice` and logs — because the whole
    /// point of awaiting the ack is that a refusal must surface.
    @discardableResult
    private func mutateSilently(_ command: String,
                                payload: [String: JSONValue] = [:]) async -> Bool {
        do {
            let detail = try await sendToEngine(command, payload: payload)
            // The engine reports a no-op inside the detail rather than by refusing, so the flag has to
            // be read: `{paused: false}` is a refusal in everything but name.
            if let paused = detail["paused"]?.boolValue, !paused {
                let reason = detail["reason"]?.stringValue ?? "the engine did not pause the run"
                notice = reason
                logs.append(notice: "→ \(command) refused: \(reason)")
                return false
            }
            return true
        } catch {
            let message = error.localizedDescription
            notice = message
            logs.append(notice: "→ \(command) failed: \(message)")
            return false
        }
    }

    public func resume() async { await send("resume"); await refresh() }
    public func abort() async { await send("abort"); await refresh() }

    // MARK: - Attaching a project

    /// Point the org at an existing folder — the user's own repository.
    ///
    /// Stops the engine first, letting it checkpoint, and relaunches with `--project`. A relaunch
    /// rather than a live re-root: the executing subprocess holds the workspace path, and mutating it
    /// underneath a running graph is how a run writes half its artifacts into the previous project.
    /// Stopping at a checkpoint is the same mechanism `pause`/`resume` already uses.
    ///
    /// The relaunch follows the *stop*, not a timer — see `launchOnceStopped`.
    public func setProject(_ url: URL) async {
        guard url.hasDirectoryPath || url.isFileURL else {
            notice = "choose a folder, not a file"
            return
        }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            notice = "that folder does not exist: \(url.path)"
            return
        }

        let wasLive = engineState.isLive
        if wasLive {
            logs.append(notice: "stopping the engine so the run checkpoints before re-rooting…")
            service?.terminate()
        }

        settings.attachedProject = url
        settings.projectPath = url
        projectPath = url.path
        writer.updateRoot(url)
        logs.append(notice: "attached project: \(url.path)")
        logs.append(notice: "engine state will be written to \(url.path)/.agent_state")

        // A scheduled retry belongs to the *previous* workspace, so it is cancelled rather than left
        // to fire against the new one — and the offline panels are reloaded because the durable state
        // they read now lives somewhere else entirely.
        cancelAutoRestart(reason: "the project changed")
        await refreshOfflineState()

        if wasLive { launchOnceStopped() }
    }

    /// Detach: go back to a managed project under `AgentOrg/projects/`.
    public func detachProject(slug: String = "demo") async {
        let wasLive = engineState.isLive
        if wasLive { service?.terminate() }
        settings.attachedProject = nil
        let managed = settings.engineRoot.appendingPathComponent("projects/\(slug)")
        settings.projectPath = managed
        projectPath = managed.path
        writer.updateRoot(managed)
        logs.append(notice: "using the managed project \(managed.path)")
        cancelAutoRestart(reason: "the project changed")
        await refreshOfflineState()
        if wasLive { launchOnceStopped() }
    }

    /// Launch again once the engine has actually stopped, or at once if it already has.
    ///
    /// **Why this exists rather than a sleep.** `launch()` refuses while *any* live state is set
    /// (`guard !engineState.isLive`), and `terminate()` sets `.terminating` immediately while the
    /// process is still draining — it closes the command pipe and waits out the engine's own exit, which
    /// can take up to ten seconds on a run in flight. So the fixed 600 ms sleep this replaces was a coin
    /// toss: whenever the drain outlasted it, the relaunch was a silent no-op, and nothing else ever
    /// tried again — a clean exit reaches `.finished`, which does **not** go through the bounded
    /// auto-restart (only `.failed` does). The engine then stayed down with only the log saying why.
    ///
    /// The guard and the flag are set with no `await` between them, on the main actor, so a state
    /// transition cannot land in the gap; the transition itself performs the launch (see the
    /// `onStateChange` handler in `launch()`), after the stop-side bookkeeping for that same transition.
    private func launchOnceStopped() {
        guard engineState.isLive else {
            launch()
            return
        }
        relaunchAfterStop = true
    }

    // MARK: - The goal

    /// How autonomous a goal set from this window runs.
    ///
    /// **Two things this control does, and they are not the same thing.** It is the authority the app
    /// attaches to every goal it creates — `setGoal` sends the posture with each `goal_set`, so the
    /// claim "goals set here run this way" is made true by the code rather than asserted. And it is
    /// the *engine's* answer to the autonomy step, written to `goal.default_posture` through
    /// `autonomy_set`, which is what makes `onboard` stop reporting the step as outstanding.
    ///
    /// The second half was missing until now, and it was the loudest onboarding failure in the app.
    /// This property was purely local: the wizard asked the question, the window wrote the answer to
    /// `UserDefaults`, `showsFirstRunWizard` went false because *this window's* gate was satisfied —
    /// and the engine's own journey still said "next: Choose how much it decides alone", because
    /// nothing had ever told it. Re-running setup then asked the same question again, with the answer
    /// already stored and no explanation for why it came back. A first-run step a person has answered
    /// that the engine still calls unanswered is the complaint this whole track exists to fix.
    ///
    /// A stored value rather than a published one so a test can substitute its own defaults. Setting
    /// it directly still only records the *app's* preference — the engine write is `choosePosture`,
    /// explicitly, so a caller cannot believe it has told the engine when it has not.
    public var goalPosturePreference: OrgController.Posture? {
        get { preferences.chosenPosture }
        set {
            preferences.chosenPosture = newValue
            objectWillChange.send()
        }
    }

    /// Choose the posture *and* record it in the engine's configuration.
    ///
    /// The one call the autonomy step makes. It exists as its own method rather than inside the setter
    /// because the setter is synchronous and firing a `Task` from it would make "the wizard retired"
    /// and "the engine knows" two independent races — the window would move on whether or not the
    /// write landed, which is precisely the dishonesty being fixed here. This awaits the reply, and
    /// returns whether the engine accepted it, so the step can refuse to advance on a failure.
    ///
    /// A failure is *reported* rather than swallowed: `autonomy_set` refuses an unknown posture and a
    /// missing credentials file, and either one means the journey will still show this step — which
    /// the person needs to know before they wonder why Setup keeps asking.
    @discardableResult
    public func choosePosture(_ posture: Posture) async -> Bool {
        goalPosturePreference = posture
        guard let wire = posture.wireValue else { return false }
        guard service != nil, engineState == .running else {
            // No engine to tell. The preference is still recorded, so the app's own goals carry the
            // posture; the journey will report the step once an engine is up and can be told.
            notice = "the engine is not running, so the default posture is not recorded yet"
            return false
        }
        var ok = false
        await mutate("autonomy_set", payload: ["posture": .string(wire)]) { _ in ok = true }
        if ok {
            await refresh()
        }
        return ok
    }

    /// Set the objective and arm the loop.
    ///
    /// The posture the person chose in the first-run wizard travels with the goal, so the goal's
    /// authority is the one they picked rather than whatever the config file happens to say. Sent
    /// only when one was chosen: with no choice recorded, the engine's own default applies, and
    /// inventing `unattended` here would silently grant an authority nobody asked for.
    public func setGoal(_ objective: String, arm: Bool = true) async {
        await setGoal(objective, arm: arm, posture: goalPosturePreference)
    }

    /// The same, with an explicit posture.
    ///
    /// Split out so the wizard can set the *first* goal with the posture the person just chose, before
    /// the preference has been read back — one command, one round trip, no window where the goal
    /// exists without its authority.
    public func setGoal(_ objective: String, arm: Bool = true,
                        posture: Posture?) async {
        var payload: [String: JSONValue] = ["objective": .string(objective)]
        if !arm { payload["no_arm"] = .bool(true) }
        if let wire = posture?.wireValue { payload["posture"] = .string(wire) }
        await send("goal_set", payload: payload)
        await refresh()
    }

    // MARK: - Posture

    /// How far a goal's autonomy reaches, as the engine defines it.
    ///
    /// A Swift enum rather than a bare string because it is now a *control*: a picker bound to a
    /// `String` would happily offer a value the engine refuses, and an unknown posture arriving from a
    /// newer engine would be indistinguishable from a known one. `unknown` exists so a posture this
    /// build does not recognise is reported as unrecognised rather than silently rendered as
    /// `unattended` — the safe reading of an unknown authority is "I do not know", not "it may act".
    public enum Posture: String, CaseIterable, Identifiable, Sendable {
        case unattended
        case supervised
        case unknown

        public var id: String { rawValue }

        /// The posture as the engine names it, or nil when this build does not know it.
        public var wireValue: String? { self == .unknown ? nil : rawValue }

        /// The two real choices, in the order the picker shows them. `unknown` is deliberately absent:
        /// it is a rendering state, not something a person can choose.
        public static var choices: [Posture] { [.unattended, .supervised] }

        /// The label a person reads.
        public var label: String {
            switch self {
            case .unattended: return "Unattended"
            case .supervised: return "Supervised"
            case .unknown: return "Unknown posture"
            }
        }

        /// What choosing this posture means, in the words the picker's help text uses.
        public var explanation: String {
            switch self {
            case .unattended:
                return "The engine decides the gates it is authorised to decide — including a "
                    + "release, when the gate's evidence is present and no safety control fired. "
                    + "You are told what it decided."
            case .supervised:
                return "Every gate waits for you. Nothing is decided on your behalf."
            case .unknown:
                return "This build does not recognise the posture the engine reported, so it will "
                    + "not act on it. Set one explicitly to take control."
            }
        }
    }

    /// The current goal's posture, read from the snapshot rather than from a local copy.
    ///
    /// Reading it from the engine's own `posture` field is the point: the app's picker and the engine's
    /// policy must be the same answer, and a locally-remembered value would drift the moment the CLI
    /// changed it. The legacy `human_gate` flag inside `policy` is the fallback for an engine that
    /// predates the field, so an older engine still renders a correct control instead of "unknown".
    public var goalPosture: Posture {
        if let raw = goal["posture"]?.stringValue {
            return Posture(rawValue: raw.lowercased()) ?? .unknown
        }
        // No `posture` key at all: this is an engine from before the field existed. The legacy flag
        // is the whole story there — `human_gate: true` meant supervised.
        if let gate = goal["policy"]?.objectValue?["human_gate"]?.boolValue {
            return gate ? .supervised : .unattended
        }
        // Neither field: no goal has been set, or the engine sent a snapshot this build cannot read.
        return .unknown
    }

    /// The current goal's posture, read from the snapshot rather than from a local copy.
    ///
    /// Sends the one word the engine now reads, rather than the two flags it used to infer a posture
    /// from. `no_arm` is repeated so changing the posture cannot restart a paused goal — the person
    /// pressed a picker, not Resume.
    public func setGoalPosture(_ posture: Posture) async {
        guard let wire = posture.wireValue else {
            notice = "this build cannot set that posture"
            return
        }
        let objective = goal["objective"]?.stringValue ?? ""
        guard !objective.isEmpty else {
            notice = "set a goal before choosing how it runs"
            return
        }
        await send("goal_set", payload: [
            "objective": .string(objective),
            "no_arm": .bool(true),
            "posture": .string(wire),
        ])
        await refresh()
    }

    // MARK: - The gate disposition

    /// What the console should do about a gate, decided in one place.
    ///
    /// This is the whole of "auto-resume on a gate", as a value rather than a scattering of `if`s
    /// across the event handler — because the rule is a *safety* rule, and a safety rule that lives in
    /// several branches is one that will eventually be applied inconsistently.
    public enum GateDisposition: Equatable, Sendable {
        /// The engine has not said the goal may answer this gate: it refused it, or it is supervised,
        /// or it said nothing at all. The console shows the gate and sends nothing — the behaviour
        /// that existed before any of this.
        case waitForHuman(why: String)
        /// The engine has *already* decided this gate on the goal's authority. There is nothing to
        /// forward — the decision is recorded — so the console clears the pending gate and says why,
        /// which is what actually removes the click.
        case answeredByGoal(why: String)
        /// The engine has declared the goal may answer this gate and has not itself decided it, so the
        /// console forwards the approval. This is the case the whole feature exists for.
        case forwardApproval(why: String)

        /// The reasons the console may send the decision itself, in one place.
        public var shouldForward: Bool {
            if case .forwardApproval = self { return true }
            return false
        }

        /// Whether the gate on screen is still a question for a person.
        public var isWaitingForHuman: Bool {
            if case .waitForHuman = self { return true }
            return false
        }

        /// The sentence shown beside the gate, so the person is told *why* nothing (or something)
        /// happened rather than having to infer it from the absence of a button.
        public var why: String {
            switch self {
            case .waitForHuman(let why), .answeredByGoal(let why), .forwardApproval(let why):
                return why
            }
        }
    }

    /// The one rule: which gates may the console forward an approval for?
    ///
    /// **The console never invents a decision.** It forwards `approve` only for a gate the engine has
    /// *itself* declared the goal may answer, and it reads that declaration off the wire rather than
    /// re-deriving it. The three engine reports it reads:
    ///
    /// - `human.gate` with `waiting_on: owner` is the engine **refusing**: `_auto_pass` and
    ///   `_release_terminal_gate` in `engine/orchestrator.py` attach that field, with a `why`, to every
    ///   gate they decline to answer. A gate carrying it is the person's, always, whatever the posture
    ///   says — so this is checked *after* the posture but before anything else.
    /// - `handoff`-style absence of `waiting_on` on a `human.gate` while the goal is `unattended` is
    ///   the engine having *reached* a gate without refusing it — the executor's own emission
    ///   (`executor._gate`), which carries no `waiting_on` because it is not a refusal. Silence here
    ///   is the grant: the engine has not claimed the gate, and the posture says the goal may answer.
    /// - A `by: "goal"` decision (`human.decision` / `policy.changed`) means the engine answered the
    ///   gate itself. There is nothing left to forward, so the console only records it.
    ///
    /// What the app deliberately does **not** do is re-implement `_gate_evidence`,
    /// `_gate_is_auto_approvable` or `_UNRELEASABLE_STOP_REASONS`. It cannot see the ledger, the run's
    /// `stop_reason`, or whether an irreversible decision already stands for the gate — so a second
    /// implementation here would be confidently wrong about exactly the cases that matter. The engine
    /// applies those checks and reports `waiting_on: owner` when one fires; the app's job is to read
    /// that answer, not to reach it.
    ///
    /// - Parameters:
    ///   - gate: the gate payload, as the event or the status snapshot delivered it.
    ///   - posture: the goal's posture, from the same snapshot.
    ///   - decidedByGoal: whether the engine has reported a decision for this gate with `by: "goal"`.
    public static func gateDisposition(gate: [String: JSONValue]?,
                                       posture: Posture,
                                       decidedByGoal: Bool = false) -> GateDisposition {
        guard let gate else { return .waitForHuman(why: "no gate is waiting") }

        // The engine's own refusal, which outranks the posture: a goal may be unattended and still be
        // told this particular gate is not its to answer.
        if let waiting = gate["waiting_on"]?.stringValue, waiting == "owner" {
            let why = gate["why"]?.stringValue
                ?? "the engine left this gate for you"
            return .waitForHuman(why: why)
        }

        if decidedByGoal {
            return .answeredByGoal(
                why: gate["why"]?.stringValue ?? "the engine released this gate on the goal's authority")
        }

        // A posture this build cannot read is treated as supervised, never as unattended: the safe
        // reading of an unknown authority is "I do not know", not "it may act on my behalf".
        guard posture == .unattended else {
            return .waitForHuman(
                why: posture == .supervised
                    ? "the goal is supervised, so every gate waits for you"
                    : "this build does not know the goal's posture, so nothing is decided for you")
        }

        // A gate the engine handed to some other party. Forwarding here would be precisely the
        // invention this rule exists to prevent, and it is the case a future engine would add.
        if let waiting = gate["waiting_on"]?.stringValue {
            return .waitForHuman(why: "the engine says this gate is waiting on \(waiting)")
        }

        return .forwardApproval(
            why: "the goal is unattended and the engine left this gate unanswered, so it may answer it")
    }

    /// The disposition for the gate currently shown, if any.
    public var gateDisposition: GateDisposition {
        guard let pendingGate else { return .waitForHuman(why: "no gate is waiting") }
        return Self.gateDisposition(
            gate: pendingGate,
            posture: goalPosture,
            decidedByGoal: lastGoalDecidedGateId == pendingGate["gate_id"]?.stringValue)
    }

    /// Remove the click for a gate the engine answered itself.
    ///
    /// Called when the engine reports a `by: goal` decision. The gate is no longer a question, so
    /// leaving `pendingGate` set would keep a badge on the sidebar and an Approve button in front of a
    /// person for a decision already taken — the app asking them to confirm something the engine has
    /// done and recorded.
    private func clearGateAnsweredByGoal(_ event: EngineEvent) {
        guard let gateId = event.payload["gate_id"]?.stringValue else { return }
        lastGoalDecidedGateId = gateId
        if pendingGate?["gate_id"]?.stringValue == gateId {
            pendingGate = nil
            // The decision is made and recorded, so the status bar's "waiting on you" is now false and
            // goes at once rather than lapsing on its own twenty seconds later.
            notice = nil
        }
    }

    public func pauseGoal() async { await send("goal_pause"); await refresh() }
    public func resumeGoal() async { await send("goal_resume"); await refresh() }
    public func clearGoal() async { await send("goal_clear"); await refresh() }

    // MARK: - The mission

    /// State the standing purpose, with optional objectives.
    public func setMission(_ statement: String, objectives: [String] = [], arm: Bool = false) async {
        var payload: [String: JSONValue] = ["statement": .string(statement)]
        if !objectives.isEmpty { payload["objectives"] = .array(objectives.map { .string($0) }) }
        if arm { payload["arm"] = .bool(true) }
        await send("mission_set", payload: payload)
        await refresh()
    }

    public func addObjective(_ text: String) async {
        await send("mission_add", payload: ["objective": .string(text)])
        await refresh()
    }

    public func removeObjective(at index: Int) async {
        await send("mission_remove", payload: ["index": .int(index)])
        await refresh()
    }

    /// Hand the active (or named) objective to a goal, which is what begins real work.
    public func startObjective(at index: Int? = nil, arm: Bool = true) async {
        var payload: [String: JSONValue] = [:]
        if let index { payload["index"] = .int(index) }
        if !arm { payload["no_arm"] = .bool(true) }
        await send("mission_start", payload: payload)
        await refresh()
    }

    public func markObjective(_ index: Int, state: String, summary: String = "") async {
        var payload: [String: JSONValue] = ["index": .int(index), "state": .string(state)]
        if !summary.isEmpty { payload["summary"] = .string(summary) }
        await send("mission_mark", payload: payload)
        await refresh()
    }

    public func advanceMission(summary: String = "") async {
        var payload: [String: JSONValue] = [:]
        if !summary.isEmpty { payload["summary"] = .string(summary) }
        await send("mission_advance", payload: payload)
        await refresh()
    }

    public func armMission() async { await send("mission_arm"); await refresh() }
    public func pauseMission() async { await send("mission_pause"); await refresh() }
    public func clearMission() async { await send("mission_clear"); await refresh() }

    // MARK: - The portfolio

    /// Load the live cross-org picture: every org's mission, spend and blockers.
    ///
    /// A separate, deliberate fetch rather than part of every two-second poll: it builds an orchestrator
    /// per org, so at that cadence it would read every roster continuously. The panel asks when it is
    /// open, which is when the cost is wanted — and once it *has* been asked for, it is refreshed on the
    /// slow cadence (`refreshSlowPanelsIfDue`) rather than only ever being read on appearance, which is
    /// what left a rollup frozen at the moment the section was first opened.
    public func loadPortfolioLive() async {
        portfolioLoading = true
        defer { portfolioLoading = false }
        await fetch("portfolio_live") { [weak self] payload in
            guard let self else { return }
            // Guarded: a cross-org picture that has not moved must not invalidate the window on a
            // cadence — the fetch already costs an orchestrator per org.
            self.assignIfChanged(\.portfolioLive, payload)
        }
    }

    /// Register an org from the console.
    ///
    /// **Verified, and it says where the org landed.** It used to `send` and `refresh`, which discards
    /// the acknowledgement — so a refused registration (a name the engine rejects, a folder that does
    /// not exist) refreshed to an unchanged list with nothing said, and a person could not tell the
    /// register from their click. `mutate` returns the engine's answer, and the answer carries the
    /// entry it created.
    ///
    /// The created entry is also what makes the console able to *show* the result: the path the engine
    /// resolved is in the reply, and it is the difference between "registered" and "registered, and its
    /// work will live in this folder" — which is the question a person actually has.
    ///
    /// - Returns: the created org entry, or nil when the engine refused.
    @discardableResult
    public func addOrg(name: String, path: String = "", charter: String = "",
                       dailyBudgetUSD: Double = 0, active: Bool = false) async -> [String: JSONValue]? {
        var payload: [String: JSONValue] = ["name": .string(name)]
        if !path.isEmpty { payload["path"] = .string(path) }
        if !charter.isEmpty { payload["charter"] = .string(charter) }
        if dailyBudgetUSD > 0 { payload["daily_budget_usd"] = .double(dailyBudgetUSD) }
        if active { payload["active"] = .bool(true) }
        var created: [String: JSONValue]?
        await mutate("portfolio_add", payload: payload) { [weak self] response in
            created = response["org"]?.objectValue
            // The reply carries the register as well, so the list is current without a second round
            // trip — and from the engine's own write rather than an optimistic append on this side.
            if let portfolio = response["portfolio"]?.objectValue {
                self?.portfolio = portfolio
            }
        }
        await refresh()
        return created
    }

    /// Forget an org from the register. **Its folder and run history are left on disk.**
    ///
    /// **Verified, and it reloads the whole window** — the same two failures `selectOrg` pins, for the
    /// same two reasons:
    ///
    /// 1. It used to `send` and `refresh`, which logs but discards whether the engine accepted, so a
    ///    refused removal (an unknown org) left the row on screen with no reason. `mutate` surfaces it.
    /// 2. Removing the *active* org re-points the engine's workspace, so the roster, mission, goal and
    ///    run history on every other panel now describe a different org. Reloading only the register
    ///    would leave the Org pane listing the departed org's people.
    ///
    /// Because that re-point happens, the app's own reader has to follow it here for exactly the reason
    /// `selectOrg` does — see `adoptRepointedWorkspace`.
    ///
    /// - Returns: whether the engine accepted it. The caller shows the confirmation, so it needs to
    ///   know whether the thing it confirmed actually happened.
    @discardableResult
    public func removeOrg(_ ref: String) async -> Bool {
        let ok = await mutate("portfolio_remove", payload: ["org": .string(ref)]) { [weak self] response in
            guard let self else { return }
            self.adoptRepointedWorkspace(response)
            if let portfolio = response["portfolio"]?.objectValue {
                self.portfolio = portfolio
            }
        }
        await refresh()
        await loadWindow()
        await refreshOfflineState()
        await loadPortfolioLive()
        return ok
    }

    /// What removing one org would take away, and what it would leave behind.
    ///
    /// A deliberate round trip before the confirmation is shown, rather than prose written in Swift.
    /// Two facts have to be true in the sentence a person agrees to — *which* folder stays on disk, and
    /// how many bytes that is — and neither is knowable from the register alone. Composing them here
    /// would make the console a second description of `portfolio remove`'s behaviour, which drifts the
    /// first time the engine's does.
    ///
    /// - Returns: the engine's preview, or `[:]` when it could not be fetched (the caller then says so
    ///   rather than showing an invented figure).
    public func orgRemovalPreview(_ ref: String) async -> [String: JSONValue] {
        var preview: [String: JSONValue] = [:]
        await mutate("portfolio_removal", payload: ["org": .string(ref)]) { response in
            preview = response
        }
        return preview
    }

    // MARK: - Schedules

    /// Load the schedule for this workspace: what is armed, what is due, and what a fire did.
    ///
    /// Loaded on the Runs pane's appearance *and* on the slow poll cadence — see
    /// `refreshSlowPanelsIfDue`, which is what keeps a schedule armed while the pane is open (or armed
    /// by the CLI, or fired since the pane last appeared) from being invisible until the pane is
    /// re-entered. It is not on the two-second poll, because the answer includes a `load_error` that has
    /// to be *said* — an unreadable schedule fires nothing — and a poll that carried that every two
    /// seconds would repeat a sentence nobody has to act on twice.
    public func loadSchedules() async {
        await fetch("schedules") { [weak self] payload in
            guard let self else { return }
            // Guarded: an unchanged schedule must not invalidate the window, which is the rule the
            // two-second poll follows too.
            self.assignIfChanged(\.schedule, payload)
        }
    }

    /// Every scheduled entry, as the engine listed it.
    public var scheduleEntries: [[String: JSONValue]] {
        (schedule["entries"]?.arrayValue ?? []).compactMap { $0.objectValue }
    }

    /// Forget one scheduled entry. **Unrecoverable** — the entry is gone from the file, not paused.
    ///
    /// `schedules remove` in the CLI does the same thing, and the engine refuses an ambiguous slug
    /// rather than guessing which schedule to drop. The removal reply carries the entry back whole, so
    /// a caller can read out what actually went rather than what it asked to go.
    ///
    /// - Returns: whether the engine accepted it.
    @discardableResult
    public func removeSchedule(id: String) async -> Bool {
        let ok = await mutate("schedule_remove",
                              payload: ["schedule_id": .string(id)]) { [weak self] response in
            if let view = response["schedule"]?.objectValue {
                self?.schedule = view
            }
        }
        await loadSchedules()
        return ok
    }

    /// Follow a workspace the engine has moved, so the app's own reader reads where the engine writes.
    ///
    /// **Two commands move it** — `portfolio_select`, and `portfolio_remove` when the *active* org is the
    /// one removed — and both answer with `repointed` and the `workspace` they landed on. The window's
    /// `.agent_state` reader was re-pointed only on project attach/detach, so `offlineRuns`,
    /// `offlineHandoffs`, `offlineSessions`, `offlineGoalDocument` and `contextReadings` all went on
    /// reading the folder the window had just left. The reply is the only thing that knows the resolved
    /// path: the register holds the org, not where it works.
    ///
    /// A *declined* re-point is left alone — a run in flight, or an org whose folder is gone, leaves the
    /// engine where it was, and moving the reader then would split the two.
    ///
    /// - Returns: whether the reader was re-rooted.
    @discardableResult
    private func adoptRepointedWorkspace(_ response: [String: JSONValue]) -> Bool {
        guard response["repointed"]?.boolValue == true,
              let workspace = response["workspace"]?.objectValue else { return false }
        if let path = workspace["path"]?.stringValue, !path.isEmpty {
            writer.updateRoot(URL(fileURLWithPath: path))
            projectPath = path
        }
        self.workspace = workspace
        return true
    }

    /// Make one org the default the console acts on.
    ///
    /// **Verified, and it reloads the whole window.** Two failures this pins, both of which made the
    /// switch look like it worked while nothing changed:
    ///
    /// 1. It used to `send` and `refresh`, which logs but discards whether the engine accepted — so a
    ///    refused switch (an unknown org) left the panel showing the old active row with no reason.
    ///    `mutate` surfaces the refusal and returns it.
    /// 2. It used to reload only the live picture. But the org's *own* data — the roster the Org pane
    ///    lists, the mission and goal the Now pane shows, the runs Runs pane reads off disk — comes
    ///    from the server's workspace, which `portfolio_select` now re-points. Those are separate
    ///    reads, and reloading only the portfolio left every other panel describing the previous org.
    ///    `loadWindow` plus `refreshOfflineState` is the same pair `setProject` uses after re-rooting,
    ///    for exactly the same reason: the durable state the offline panels read now lives elsewhere.
    ///
    /// - Returns: whether the engine accepted the switch.
    @discardableResult
    public func selectOrg(_ ref: String) async -> Bool {
        let ok = await mutate("portfolio_select", payload: ["org": .string(ref)]) { [weak self] response in
            guard let self else { return }
            // **Re-root the app's own reader, not just the engine.** `portfolio_select` re-points the
            // server's workspace and answers with where it moved to; the app's `.agent_state` reader
            // was re-pointed only on project attach/detach, so `offlineRuns`, `offlineHandoffs`,
            // `offlineSessions`, `offlineGoalDocument` and `contextReadings` all went on reading the
            // *previous* org's folder. See `adoptRepointedWorkspace`.
            self.adoptRepointedWorkspace(response)
            // Apply the engine's own answer rather than awaiting a poll to catch up: the active org is
            // the one fact the switcher is about, and it must change on the click, not a poll later.
            guard let active = response["active_org_id"]?.stringValue else { return }
            var updated = self.portfolio
            // Written through the same shape `_cmd_status` publishes, so the row the panel marks active
            // and the id the rest of the window acts on are one value, not two that can disagree.
            updated["active_org_id"] = .string(active)
            if var inner = updated["portfolio"]?.objectValue {
                inner["active_org_id"] = .string(active)
                updated["portfolio"] = .object(inner)
            }
            self.portfolio = updated
            // The engine can decline to re-point — a run is in flight, or the org's folder is gone —
            // and when it does the engine says why. Saying it here is the difference between a switch
            // that failed and a switch that silently did nothing.
            if response["repointed"]?.boolValue == false,
               let why = response["repoint_reason"]?.stringValue, !why.isEmpty {
                self.notice = why
                self.logs.append(notice: "portfolio_select: \(why)")
            }
        }
        await refresh()
        // The org's own roster, mission and goal — and the run history the offline panels read off the
        // new folder. Awaited after the switch so every panel describes the org now selected.
        await loadWindow()
        // The re-root above has already happened (or been refused), so this reads the folder the engine
        // named rather than the one it just left.
        await refreshOfflineState()
        await loadPortfolioLive()
        return ok
    }

    /// Start work in one org, in parallel with any other org already running.
    ///
    /// This is the multi-org autonomy: the engine keeps a Fleet, so a run here does not block a run in
    /// another org. The panel reloads the live picture so the new run appears.
    public func runOrg(_ ref: String, goal: String = "") async {
        var payload: [String: JSONValue] = ["org": .string(ref), "background": .bool(true)]
        if !goal.isEmpty { payload["goal"] = .string(goal) }
        await send("portfolio_run", payload: payload)
        await loadPortfolioLive()
    }

    public func stopOrg(_ ref: String) async {
        await send("portfolio_stop", payload: ["org": .string(ref)])
        await loadPortfolioLive()
    }

    /// The registered orgs, as the console lists them (from the register, not the live picture).
    public var portfolioOrgs: [[String: JSONValue]] {
        (portfolio["orgs"]?.arrayValue ?? []).compactMap { $0.objectValue }
    }

    // MARK: - The org board and the defaults

    /// The board's rows: one per unit of work, with its owner and its information flow.
    public var flowRows: [[String: JSONValue]] {
        (flow["rows"]?.arrayValue ?? []).compactMap { $0.objectValue }
    }

    /// The handoffs the board observed, in order.
    public var flowHandoffs: [[String: JSONValue]] {
        (flow["handoffs"]?.arrayValue ?? []).compactMap { $0.objectValue }
    }

    /// Ask the engine for the board now, rather than waiting for the next poll.
    ///
    /// The board travels with status, so this is only needed when a person opens the panel and wants it
    /// immediately — the poll would populate it a moment later anyway.
    public func refreshFlow() async {
        await send("flow")
        await refresh()
    }

    /// Set the default provider and/or model everyone uses unless told otherwise.
    ///
    /// **Verified, not assumed.** `setDefaults` used to `send` and `refresh`, which logs but discards
    /// whether the engine accepted: a `defaults_set` naming a provider the engine does not have is
    /// refused, and the panel showed the unchanged pair with no reason. The first-run wizard cannot
    /// work that way — it has to know whether the model it just set is real before it can let the
    /// person past step one. So this reports a refusal as a `Bool` and as a `notice`, through the same
    /// `mutate` the roster edits use.
    ///
    /// - Returns: whether the engine accepted the change.
    @discardableResult
    public func setDefaults(provider: String = "", model: String = "",
                            reviewerModel: String = "", contextWindow: Int? = nil) async -> Bool {
        var payload: [String: JSONValue] = [:]
        if !provider.isEmpty { payload["provider"] = .string(provider) }
        if !model.isEmpty { payload["model"] = .string(model) }
        if !reviewerModel.isEmpty { payload["reviewer_model"] = .string(reviewerModel) }
        if let contextWindow { payload["context_window"] = .int(contextWindow) }
        let ok = await mutate("defaults_set", payload: payload) { [weak self] response in
            // `_cmd_defaults_set` answers with the defaults report itself, so the panel is current
            // without a second round trip — and from the *resolution* rather than an echo of the ask.
            if response["provider"]?.stringValue != nil {
                self?.defaults = response
            }
        }
        await refresh()
        return ok
    }

    /// Record that a person chose the project and the autonomy, so the wizard retires.
    ///
    /// Called only once both remaining steps are satisfied, so a half-answered wizard cannot mark
    /// itself done and leave a window in a state it claims is ready.
    public func completeSetup() {
        guard setupGate == .ready else { return }
        preferences.wizardCompleted = true
        objectWillChange.send()
    }

    /// Confirm the folder the agents work in. The engine always has a workspace, so this records the
    /// person's *answer* rather than discovering anything — which is exactly why it lives in the
    /// app's preferences and not in the engine's config.
    public func confirmProject() {
        preferences.projectConfirmed = true
        objectWillChange.send()
    }

    /// Forget the first-run answers, so the wizard shows again from a clean slate. For the Setup
    /// panel's own "run setup again", and for support.
    public func restartSetup() {
        preferences.reset()
        objectWillChange.send()
    }

    /// Set how autonomous a goal is by default. A `nil` switch is left untouched.
    public func setAutonomy(autoPassGates: Bool?, autoHire: Bool?, persistHires: Bool?) async {
        var payload: [String: JSONValue] = [:]
        if let autoPassGates { payload["auto_pass_auto_gates"] = .bool(autoPassGates) }
        if let autoHire { payload["auto_hire_missing"] = .bool(autoHire) }
        if let persistHires { payload["persist_auto_hires"] = .bool(persistHires) }
        await send("autonomy_set", payload: payload)
        await refresh()
    }

    /// The effective default pair, as one readable string.
    public var defaultPairLabel: String {
        let provider = defaults["provider"]?.stringValue ?? ""
        let model = defaults["model"]?.stringValue ?? ""
        if provider.isEmpty && model.isEmpty { return "not set" }
        return "\(provider)/\(model.isEmpty ? "(no model)" : model)"
    }

    /// The default autonomy, as one readable string.
    public var defaultAutonomyLabel: String {
        let autonomy = defaults["autonomy"]?.objectValue
        let gates = (autonomy?["auto_pass_auto_gates"]?.boolValue ?? true) ? "auto" : "human"
        let gaps = (autonomy?["auto_hire_missing"]?.boolValue ?? true) ? "auto" : "report"
        let hires = (autonomy?["persist_auto_hires"]?.boolValue ?? false) ? "persist" : "ephemeral"
        return "gates=\(gates) · gaps=\(gaps) · hires=\(hires)"
    }

    /// The live roll-up rows, when the panel has fetched them. Empty before that fetch.
    public var portfolioRows: [[String: JSONValue]] {
        let rollup = portfolioLive["rollup"]?.objectValue
        return (rollup?["orgs"]?.arrayValue ?? []).compactMap { $0.objectValue }
    }

    /// The one row for an org id, live if fetched, else the register's own row.
    public func orgRow(_ id: String) -> [String: JSONValue] {
        if let live = portfolioRows.first(where: { $0["id"]?.stringValue == id }) { return live }
        return portfolioOrgs.first(where: { $0["id"]?.stringValue == id }) ?? [:]
    }

    public var portfolioPrincipalName: String {
        portfolio["principal"]?.objectValue?["name"]?.stringValue ?? ""
    }

    public var activeOrgId: String { portfolio["active_org_id"]?.stringValue ?? "" }

    /// The org the window is acting on, as a row of the register. Nil when there is no portfolio, or
    /// when the active id names an org that is no longer registered.
    ///
    /// The id and the row are looked up **separately and defensively** because the two can disagree:
    /// `removeOrg` drops an org from the register and the engine re-points `active_org_id`, but a
    /// removal made from elsewhere — the CLI, or a second window — leaves this side holding an id that
    /// no longer resolves for one poll. Returning nil there, rather than a row built from an empty
    /// dictionary, is what lets a caller say "the org this window was acting on is gone" instead of
    /// rendering a blank name as though it were an org.
    public var activeOrg: [String: JSONValue]? {
        let id = activeOrgId
        guard !id.isEmpty else { return nil }
        return portfolioOrgs.first { $0["id"]?.stringValue == id }
    }

    /// The active org's name for a header or title, or empty when it cannot be resolved.
    public var activeOrgName: String { activeOrg?["name"]?.stringValue ?? "" }

    public var hasPortfolio: Bool { !portfolioOrgs.isEmpty }

    // MARK: - The machine

    /// What the agents may do on this Mac, in the engine's own words.
    ///
    /// A read of `serve._cmd_system`, decoded rather than paraphrased: see `SystemCapabilities.swift`
    /// for why nothing about these grants is written in Swift. The panel would otherwise be a second
    /// description of a permission, and the first time the engine changed one the console would go on
    /// confidently describing the old one.
    ///
    /// Loaded as part of the window's fill (`loadWindow`) and again on the slow cadence — see
    /// `refreshSlowPanelsIfDue`. It used to be read only from `SystemPane`'s own `.task`, which runs when
    /// the pane *appears*: on a launch that is still booting that read hit a dead pipe and nothing ever
    /// retried, so the pane described an engine with no machine access at all — the same failure the
    /// readiness handler fixes for providers and models.
    public func loadSystem() async {
        await fetch("system") { [weak self] payload in
            guard let self else { return }
            // Guarded like every other poll field: this now runs on a cadence, and the capability list
            // changes when a build or a config does — not every twenty seconds.
            self.assignIfChanged(\.system, payload)
        }
    }

    /// Every declared capability, with the engine's own prose for each.
    public var systemCapabilities: [SystemCapability] {
        SystemCapability.list(from: system)
    }

    /// Whether the section is switched on at all. Off means the list below is inert: no grant here
    /// reaches a tool, because the registry advertises none of them.
    ///
    /// `nil`-safe: before the first read the payload is empty and this is false, which draws the
    /// section as "off" — the truth until the engine says otherwise, rather than an optimistic list.
    public var systemEnabled: Bool { system["enabled"]?.boolValue ?? false }

    /// Whether `allow_full_access` is on: per-action allowlists and the ask-once consent step aside.
    public var systemFullAccess: Bool { system["full_access"]?.boolValue ?? false }

    /// The engine's count of every grant, so the panel's summary and the list cannot disagree.
    public var systemSummary: String {
        system["summary"]?.stringValue ?? "the engine has not been asked yet"
    }

    /// Which agents hold a grant, for the row's "who has this" line.
    public func systemHolders(of grant: String) -> [String] {
        SystemCapability.holders(of: grant, in: roster)
    }

    /// How many of the declared grants are held by at least one agent in the roster.
    ///
    /// Counted over the capabilities the engine *declared* rather than over the roster's own grant
    /// strings: the roster can name a grant this build does not know (`system:notify` today), and
    /// counting those would report more held grants than there are rows to show them on.
    public var systemGrantedCount: Int {
        systemCapabilities.filter { !systemHolders(of: $0.grant).isEmpty }.count
    }

    /// How many declared grants have no tool behind them yet.
    public var systemUnavailableCount: Int {
        systemCapabilities.filter { !$0.available }.count
    }

    /// The allowlists as the engine holds them, keyed by the payload name.
    ///
    /// Read from the reply rather than kept separately, for the same reason the descriptions are: a
    /// Swift copy of "which apps are allowed" is a second answer that can disagree with the one the
    /// engine enforces, and the disagreement would be invisible until an `open` was refused.
    public func systemAllowlist(_ key: String) -> [String] {
        (system[key]?.arrayValue ?? []).compactMap { $0.stringValue }
    }

    /// Write one or more of the machine-access switches, and adopt what the engine reports back.
    ///
    /// **Only the changed key is sent.** The engine treats an absent key as "leave this alone", which
    /// is what lets a person flip one switch here without silently reverting a change made from the
    /// terminal or another panel in between. Sending the whole payload the UI happens to be holding
    /// would write back a stale copy of every other switch, and the failure would look like the app
    /// "randomly" turning full access off.
    ///
    /// Returns whether the engine accepted it. The published `system` is replaced with the engine's
    /// own reply — not with the value that was requested — so a write the loader then refuses cannot
    /// be drawn as a success.
    @discardableResult
    public func setSystem(_ update: [String: JSONValue]) async -> Bool {
        guard !update.isEmpty else { return false }
        return await mutate("system_set", payload: update) { [weak self] payload in
            self?.system = payload
        }
    }

    /// Grant or withdraw a standing approval for one state-changing tool, for one holder.
    ///
    /// `by` is the person running the console, never an agent id: `sysctl_tools.grant_consent`
    /// refuses a `by` naming an agent, because an approval an agent can give itself is not an
    /// approval. The CLI already works this way (`system consent grant`); this is the same operation
    /// from the panel, and the engine is the one that decides whether it is acceptable.
    @discardableResult
    public func setSystemConsent(tool: String, holder: String, approved: Bool,
                                 note: String = "") async -> Bool {
        await mutate("system_consent", payload: [
            "tool": .string(tool), "agent_id": .string(holder),
            "approved": .bool(approved), "note": .string(note),
        ]) { [weak self] payload in
            if let refreshed = payload["system"]?.objectValue {
                self?.system = refreshed
            }
            self?.systemConsent = payload
        }
    }

    /// Invoke one capability and return the engine's result, so a person can see what a grant does.
    ///
    /// Deliberately a round trip and not a local effect: only the engine can act on the machine, and
    /// only it knows whether this holder is allowed. A refusal comes back as a normal result with
    /// `refused: true` and the reason, which the panel shows verbatim — the point is to learn why it
    /// was refused, so swallowing it would waste the round trip.
    @discardableResult
    public func invokeSystem(tool: String, args: [String: JSONValue] = [:]) async -> [String: JSONValue] {
        var result: [String: JSONValue] = [:]
        await mutate("system_invoke", payload: ["tool": .string(tool), "args": .object(args)]) {
            [weak self] payload in
            self?.systemResult = payload
            result = payload
        }
        return result
    }

    // MARK: - Providers

    /// Load every configured provider, its discovery status and its models.
    public func loadProviders() async {
        await fetch("providers") { [weak self] payload in
            guard let self else { return }
            if let list = payload["providers"]?.arrayValue {
                self.providers = list.compactMap { $0.objectValue }
            }
            self.providersConfigPath = payload["config_path"]?.stringValue ?? ""
        }
    }

    /// Test a provider entry *before* saving it, and fetch its model list.
    ///
    /// Deliberately a round trip rather than a local guess: only the engine can actually reach the
    /// endpoint, and a UI that reported "connected" without asking would be lying about the one thing
    /// the user pressed the button to find out.
    @discardableResult
    public func testProvider(_ draft: ProviderDraft) async -> [String: JSONValue] {
        var result: [String: JSONValue] = [:]
        // `mutate` so a bad configuration surfaces; a *network* failure is a normal answer and comes
        // back inside the payload as `ok: false`, which is the distinction this method exists for.
        await mutate("provider_test", payload: draft.payload()) { [weak self] payload in
            self?.providerTest = payload
            result = payload
        }
        return result
    }

    /// Save a provider (add or update) and reload the list.
    ///
    /// Publishes the engine's own reply into `providerSave` rather than into `providerTest`: the two
    /// are different facts. A *test* answers "can this endpoint be reached, and what models does it
    /// serve"; a *save* answers "what did the engine store, and did it have to change anything". Merging
    /// them would have a save render the previous test's model count, or a "Connected" row with no
    /// models behind it.
    ///
    /// The reply matters because `_cmd_provider_add` returns the **normalised** `base_url` and a `note`
    /// naming what it changed. Somebody who typed `https://ollama.com/v1/chat/completions` into the
    /// base field — the URL the Ollama Cloud docs show, which is the reported case — gets a working
    /// provider either way, because the engine strips the operation on the way in. What they would not
    /// get without this is any statement that it happened, and the stored config would silently differ
    /// from what they typed.
    @discardableResult
    public func saveProvider(_ draft: ProviderDraft) async -> Bool {
        let ok = await mutate("provider_add", payload: draft.payload()) { [weak self] payload in
            guard let self else { return }
            if let list = payload["providers"]?.arrayValue {
                self.providers = list.compactMap { $0.objectValue }
            }
            // Reset first: the *attempt* is itself the news, so a second save that needed no
            // correction must clear the first one's note rather than leaving it on screen.
            self.providerSave = payload
        }
        await loadModels()
        return ok
    }

    @discardableResult
    public func removeProvider(id: String) async -> Bool {
        // Reset first: the *attempt* is the news. A removal the engine refuses (it will not write a
        // document with no providers left) must not leave the previous removal's report on screen,
        // and a stale "these agents broke" would be read as this attempt's consequence.
        providerRemoval = [:]
        let ok = await mutate("provider_remove",
                              payload: ["provider_id": .string(id)]) { [weak self] payload in
            guard let self else { return }
            if let list = payload["providers"]?.arrayValue {
                self.providers = list.compactMap { $0.objectValue }
            }
            // Published, not returned: the caller acts on the `Bool`, and the report is read from here
            // by the pane — the same split `saveProvider` uses for `providerSave`.
            self.providerRemoval = payload
        }
        await loadModels()
        // The default pair *after* the removal, read from the engine rather than guessed. Removing the
        // provider named by `defaults.provider` prunes that key (`config._remove_provider_references`)
        // and the loader then resolves a different pair — a real change nobody asked for, which the
        // panel owes the person a sentence about. Re-read rather than derived here: resolution walks
        // the catalog, and a second implementation of it in Swift would answer differently.
        await refreshDefaults()
        return ok
    }

    /// Re-read the effective default provider/model and the reason it resolved that way.
    ///
    /// Through `fetch` rather than a cached `defaults` field, because the question after a provider
    /// edit is *"what does the organisation run on now"*, and only the engine's own resolution answers
    /// that — a declared default may name a provider that no longer exists.
    public func refreshDefaults() async {
        await fetch("defaults") { [weak self] payload in
            guard let self, payload["provider"] != nil else { return }
            self.defaults = payload
        }
    }

    // MARK: - The self-improvement loop

    /// Load what the improver has proposed, and what it refused.
    public func loadProposals() async {
        await fetch("proposals") { [weak self] payload in
            guard let self else { return }
            if let list = payload["proposals"]?.arrayValue {
                self.proposals = self.visibleProposals(list)
            }
            self.proposalsRefused = payload["refused_count"]?.intValue ?? 0
            if let refused = payload["refused"]?.arrayValue {
                self.proposalsRefusedList = refused.compactMap { $0.objectValue }
            }
            self.proposalsDirectory = payload["directory"]?.stringValue ?? ""
        }
    }

    /// A proposal list with the dismissed ones left out.
    ///
    /// One filter, used by every path that publishes `proposals` — this loader, the poll, and the
    /// improver's own reply — because the engine re-offers everything still on disk, and a single
    /// assignment that forgot the filter would put a dismissed row back on screen until the next poll
    /// took it away again. See `hiddenProposalIds` for why the dismissal is remembered at all.
    private func visibleProposals(_ list: [JSONValue]) -> [[String: JSONValue]] {
        list.compactMap { $0.objectValue }
            .filter { !hiddenProposalIds.contains($0["proposal_id"]?.stringValue ?? "") }
    }

    /// Remember a dismissed proposal id, so the next poll — and the next launch — leaves it hidden.
    private func rememberDismissal(_ id: String) {
        hiddenProposalIds.insert(id)
        proposalDismissals.set(Array(hiddenProposalIds).sorted(), forKey: Self.hiddenProposalsKey)
    }

    /// Run one improver cycle, in the background, and reload the list.
    ///
    /// The cycle runs on the engine's worker thread, so the console keeps answering while it works —
    /// and because it writes proposals rather than applying them, quitting mid-cycle leaves nothing
    /// half-changed. The `improving` flag exists so the button can say "working" rather than looking
    /// hung: a cycle runs the whole behavioural suite, which takes seconds.
    public func runImprover() async {
        improving = true
        defer { improving = false }
        let ok = await mutate("improve", payload: [:]) { [weak self] payload in
            self?.improveResult = payload
            if let list = payload["proposals"]?.arrayValue {
                self?.proposals = self?.visibleProposals(list) ?? []
            }
            self?.proposalsRefused = payload["refused_count"]?.intValue ?? 0
            if let refused = payload["refused"]?.arrayValue {
                self?.proposalsRefusedList = refused.compactMap { $0.objectValue }
            }
            self?.proposalsDirectory = payload["directory"]?.stringValue ?? ""
        }
        if !ok { logs.append(notice: "the improver cycle was refused") }
        await loadProposals()
    }

    /// Remove a proposal from the queue after reading it. **Applies nothing** — the engine never edits
    /// the tree, so accepting one means applying the patch yourself.
    ///
    /// **Local, and remembered.** Local because the file is the Owner's to delete and a command that
    /// deleted it for them would be the engine acting on their behalf on the one surface where it must
    /// not — which means the *engine* keeps offering the proposal on every poll, so the app has to
    /// remember the answer itself (`hiddenProposalIds`) rather than only dropping the row.
    public func dismissProposal(id: String) async {
        rememberDismissal(id)
        proposals.removeAll { $0["proposal_id"]?.stringValue == id }
        logs.append(notice: "proposal \(id) hidden; the file is still in .agent_state/proposals/")
    }

    // MARK: - Agents

    /// Load the editable roster and the skills an agent can be hired for.
    public func loadRoster() async {
        await fetch("agents") { [weak self] payload in
            guard let self else { return }
            if let list = payload["agents"]?.arrayValue {
                self.roster = list.compactMap { $0.objectValue }
            }
            if let names = payload["skills"]?.arrayValue {
                self.skills = names.compactMap { $0.stringValue }.sorted()
            }
            self.rosterPath = payload["roster_path"]?.stringValue ?? ""
        }
    }

    /// Hire a new agent into the roster.
    @discardableResult
    public func hireAgent(_ draft: AgentDraft) async -> Bool {
        let ok = await mutate("hire", payload: draft.hirePayload()) { _ in }
        await loadRoster()
        await refresh()
        return ok
    }

    /// Edit an existing agent, keeping its id — and therefore its history.
    ///
    /// `original` is the draft as it was when editing began, so a field the user did not touch is not
    /// re-sent. That matters for the name: re-sending an unchanged one would make a model-only edit
    /// fail whenever another agent already holds it — a refusal with nothing to do with the change.
    @discardableResult
    public func updateAgent(id: String, draft: AgentDraft, original: AgentDraft? = nil) async -> Bool {
        var payload = draft.updatePayload(original: original)
        payload["agent_id"] = .string(id)
        let ok = await mutate("agent_update", payload: payload) { _ in }
        await loadRoster()
        return ok
    }

    /// Remove an agent from the roster. **This is a retire, not a delete** — and the wording the UI
    /// shows has to be the engine's actual behaviour, not the button's label.
    ///
    /// Read from `engine/people.py::retire_agent` → `engine/org/roster.py::terminate`, whose own
    /// docstring is the whole truth about what this does:
    ///
    /// - The roster entry **is** removed (`del self.agents[agent_id]`), so the agent is gone from
    ///   routing and from the Org pane's list.
    /// - **The termination is recorded.** `terminate` appends `{id, name, reason, at}` to the org's
    ///   `retired` list, which `people.save` writes, so the decision outlasts the agent and survives a
    ///   reload. `reason` is passed through and recorded when given; this app sends an empty one
    ///   because no dialog in it asks for a reason, and the record still holds the name, id and time.
    /// - What the agent *produced* is untouched, because it lives in the run's artifacts rather than in
    ///   the roster.
    ///
    /// The engine also refuses two cases, and both refusals are surfaced rather than pre-empted here:
    /// the Owner cannot be retired at all, and an agent whose reports are actively working cannot be
    /// either — the work they hold would be orphaned mid-flight. `mutate` reports either in the
    /// engine's own words.
    @discardableResult
    public func retireAgent(id: String, reason: String = "") async -> Bool {
        let ok = await mutate("agent_retire",
                              payload: ["agent_id": .string(id),
                                        "reason": .string(reason)]) { _ in }
        await loadRoster()
        return ok
    }

    // MARK: - Subagents

    /// Load every child this run dispatched, for the Work panel's tree.
    public func loadSubagents() async {
        await fetch("subagents") { [weak self] payload in
            if let list = payload["children"]?.arrayValue {
                self?.subagents = list.compactMap { $0.objectValue }
            }
        }
    }

    /// Page one child's transcript, so a person can inspect what a subagent actually did.
    public func loadSubagentTranscript(childId: String, offset: Int = 0,
                                       limit: Int = 8192) async {
        await fetch("subagent_result",
                    payload: ["child_id": .string(childId),
                              "offset_bytes": .int(offset),
                              "limit_bytes": .int(limit)]) { [weak self] payload in
            self?.subagentTranscript = payload
        }
    }

    /// Reassign a node to another agent — the manual form of the router's job.
    public func reassign(node: String, agentId: String) async {
        await send("reassign", payload: ["node": .string(node), "agent_id": .string(agentId)])
        await refresh()
    }

    /// Take a node over as the Owner.
    public func takeover(node: String) async {
        await send("takeover", payload: ["node": .string(node)])
        await refresh()
    }

    // MARK: - Reflection

    /// Pull the current state: roster, run status, gates, models.
    ///
    /// A pull rather than only a push, because a snapshot survives a missed event — and the UI polling on
    /// a timer is more robust than trusting that every event arrived.
    public func refresh() async {
        await fetch("status") { [weak self] payload in
            self?.applyStatus(payload)
        }
    }

    /// Load the panels that fill a window, concurrently.
    ///
    /// **The window used to fill in three round trips.** `loadProviders`, `loadModels` and `loadRoster`
    /// are independent commands that share no data and have no ordering between them, and every call
    /// site ran them one after another with `await` — so opening a window cost the *sum* of three round
    /// trips when it should have cost the maximum of one. On a warm engine that is a few milliseconds
    /// each and invisible; on a cold one, where the first `status` builds an orchestrator, it is the
    /// difference between a window that appears populated and one that fills in visibly, panel by panel.
    ///
    /// `loadSystem` joins them for the same reason they are here rather than in a pane's `.task`: this is
    /// the fill that runs when the engine becomes usable, and the capability description has to be read
    /// *then* or a pane that appeared during the bootstrap renders an engine with no machine access.
    public func loadWindow() async {
        await loadTogether([loadProviders, loadModels, loadRoster, loadSystem])
    }

    /// Reload the provider list and the model catalog together.
    ///
    /// The pair matters after a provider edit: the list to show what was saved, and the catalog to show
    /// what it can serve. They are independent reads of the same change, so they go out at once — the
    /// old sequential pair made a save feel slower than it was, on the one action where a person is
    /// watching for the result.
    public func reloadProviderAndModels() async {
        await loadTogether([loadProviders, loadModels])
    }

    /// Run independent loads together and return when all of them have applied.
    ///
    /// The one implementation of "these do not depend on each other, so send them at once", so a call
    /// site does not have to re-derive it — and so a future one that forgets is visible as a missing
    /// call rather than as a subtly slower window.
    ///
    /// Structured concurrency rather than detached tasks: this does not return until every load has
    /// applied, so a caller that awaits it can then read `providers`, `models` and `roster` and know
    /// they are current. That is the property the `.task` call sites and the tests depend on — with
    /// fire-and-forget children they would return immediately and every assertion would race.
    ///
    /// A `TaskGroup` and not `async let`, because the loads are the *same kind* of work (send a command,
    /// apply the reply) and a group keeps that visible as one rule rather than N special cases.
    ///
    /// The honest limit: this overlaps the *round trips*, not the engine's own work — `serve` executes
    /// commands on one worker — so the saving is the latency of the second and third commands rather
    /// than two thirds of the total. On a local pipe that is single-digit milliseconds; it is the slow
    /// or cold engine, where one command costs seconds, that this is for.
    private func loadTogether(_ loads: [() async -> Void]) async {
        await withTaskGroup(of: Void.self) { group in
            for load in loads {
                group.addTask { @MainActor in await load() }
            }
        }
    }

    /// Apply a `status` snapshot to the console's state.
    ///
    /// Split out of `refresh` so the *mapping* — which is where the gate disposition is recomputed and
    /// therefore where a missed event is made good — can be driven directly by a test without a live
    /// engine. The fetch around it is process handling; this is the part with a rule in it.
    ///
    /// **Two rules, and both are about what happens when the payload has not changed.**
    ///
    /// 1. *A field is published only when its value differs.* `@Published` fires `objectWillChange` on
    ///    every assignment regardless of equality, and it has no per-property granularity: one set is one
    ///    invalidation of every view holding this controller. Assigning these fields straight from the
    ///    payload therefore re-evaluated the whole window every two seconds on an engine where nothing
    ///    had happened. Every assignment below goes through `assignIfChanged`, so an unchanged poll now
    ///    publishes nothing at all.
    /// 2. *A key the payload does not carry means the engine has no value for it, so the field is
    ///    cleared.* Leaving the previous value in place keeps the last thing the engine ever said about
    ///    a field on screen for the rest of the session — a workspace that has gone, or a proposal queue
    ///    that has emptied, would keep rendering as though it were still there. This rule is safe to
    ///    apply to all of them precisely because `status` carries every one of these keys in *both* of
    ///    its shapes (`serve._cmd_status` says so in its own docstring), so an absent key means an
    ///    engine that stopped sending it rather than "no change".
    func applyStatus(_ payload: [String: JSONValue]) {
        assignIfChanged(\.runStatus, payload)
        assignIfChanged(\.agents, (payload["org"]?.arrayValue ?? []).compactMap { $0.objectValue })
        if let gate = payload["gate"]?.objectValue {
            assignIfChanged(\.pendingGate, gate)
            // The poll is a fallback for a missed event, so it must reach the same verdict the event
            // path would — otherwise a gate that arrived while the app was not listening would render
            // with no explanation of what the console is or is not doing about it.
            let disposition = OrgController.gateDisposition(
                gate: gate, posture: goalPosture,
                decidedByGoal: lastGoalDecidedGateId == gate["gate_id"]?.stringValue)
            assignIfChanged(\.gateNote, disposition.why)
        } else {
            assignIfChanged(\.pendingGate, nil)
            assignIfChanged(\.gateNote, nil)
        }
        let nodeRows = (payload["outcome"]?.objectValue?["nodes"]?.objectValue ?? [:])
            .map { key, value -> [String: JSONValue] in
                var entry = value.objectValue ?? [:]
                entry["name"] = .string(key)
                return entry
            }
            .sorted { ($0["name"]?.stringValue ?? "") < ($1["name"]?.stringValue ?? "") }
        assignIfChanged(\.nodes, nodeRows)
        // The goal, the attached workspace and the subagent tree all travel with status, so one poll
        // keeps every panel current rather than needing three commands the UI might forget.
        assignIfChanged(\.goal, payload["goal"]?.objectValue ?? [:])
        assignIfChanged(\.mission, payload["mission"]?.objectValue ?? [:])
        // The engine's own step list, when it has one — cleared when it reports none, so an engine that
        // loses the field does not leave a stale journey on screen.
        assignIfChanged(\.journey, SetupJourneyReport(status: payload))
        // The portfolio register travels with status, so the Portfolio panel lists the orgs without a
        // second command on every poll.
        assignIfChanged(\.portfolio, payload["portfolio"]?.objectValue ?? [:])
        assignIfChanged(\.workspace, payload["workspace"]?.objectValue ?? [:])
        // The activity report travels with status, so the "what is happening" panel is current from the
        // same poll every other panel uses.
        assignIfChanged(\.activity, payload["activity"]?.objectValue ?? [:])
        // The org board travels the same way, so the Flow panel shows who is on what from the poll every
        // other panel already makes.
        assignIfChanged(\.flow, payload["flow"]?.objectValue ?? [:])
        // The effective default pair and the default autonomy travel too, so the Setup panel's defaults
        // editor is current without a second command it would have to remember.
        assignIfChanged(\.defaults, payload["defaults"]?.objectValue ?? [:])
        assignIfChanged(\.subagents, (payload["subagents"]?.objectValue?["children"]?.arrayValue ?? [])
            .compactMap { $0.objectValue })
        // Proposals travel with status too, so a promoted fix appears without the console having to
        // remember a second command — and the count can badge the sidebar. The dismissed ones are
        // filtered out, because the engine re-globs the directory on every poll: a dismissal that was
        // only a local removal came back two seconds later (see `hiddenProposalIds`).
        let proposalPayload = payload["proposals"]?.objectValue ?? [:]
        assignIfChanged(\.proposals, visibleProposals(proposalPayload["proposals"]?.arrayValue ?? []))
        assignIfChanged(\.proposalsRefused, proposalPayload["refused_count"]?.intValue ?? 0)
        assignIfChanged(\.proposalsRefusedList,
                        (proposalPayload["refused"]?.arrayValue ?? []).compactMap { $0.objectValue })
        assignIfChanged(\.proposalsDirectory, proposalPayload["directory"]?.stringValue ?? "")
    }

    /// Publish `value` at `keyPath` only when it differs from what is already published.
    ///
    /// The equality check is the whole point — see the first rule on `applyStatus` — and it is one
    /// helper rather than a guard at every assignment so the rule cannot be forgotten on one of them.
    ///
    /// **A key path rather than an `inout` parameter.** `inout` on a `@Published` property is read *and
    /// then written back* unconditionally when the call returns — and it is the write-back that fires
    /// `objectWillChange` — so an `inout` helper publishes on an unchanged value and defeats the entire
    /// rule. A key path reads through the getter and writes only inside the guard.
    private func assignIfChanged<Value: Equatable>(
        _ keyPath: ReferenceWritableKeyPath<OrgController, Value>, _ value: Value) {
        guard self[keyPath: keyPath] != value else { return }
        self[keyPath: keyPath] = value
    }

    /// Load the model catalog, for the agent-editor picker.
    ///
    /// Guarded like every other poll field: this runs on the same two-second tick as `refresh`, and the
    /// catalog changes when a provider does — not twice a second.
    public func loadModels() async {
        await fetch("models") { [weak self] payload in
            guard let self else { return }
            if let list = payload["models"]?.arrayValue {
                self.assignIfChanged(\.models, list.compactMap { $0.objectValue })
            }
        }
    }

    private func fetch(_ command: String,
                       apply: @escaping @MainActor ([String: JSONValue]) -> Void) async {
        await fetch(command, payload: [:], apply: apply)
    }

    /// A fetch that carries a payload, for commands that take arguments (paging a transcript).
    ///
    /// Goes through `sendToEngine` like everything else, so a `status` poll that stalls is reported in
    /// the UI rather than being the one kind of command that does not appear there. Polls are the most
    /// frequent commands this app sends, which makes them the *most* likely to be the one that hangs —
    /// excluding them would leave the row silent in exactly the case it is most useful.
    private func fetch(_ command: String, payload: [String: JSONValue],
                       apply: @escaping @MainActor ([String: JSONValue]) -> Void) async {
        guard service != nil, engineState == .running else { return }
        do {
            let response = try await sendToEngine(command, payload: payload)
            apply(response)
        } catch {
            // A failed poll is not worth a modal; it is visible as a stale panel and in the log.
            logs.append(notice: "\(command) poll failed: \(error.localizedDescription)")
        }
    }

    /// Run a command that changes something, reporting a refusal to the user **and** the caller.
    ///
    /// A refusal is not a poll failure: the engine throwing means the edit did not happen, and the
    /// user pressed a button expecting it to. So the reason is surfaced as a notice and returned,
    /// rather than being swallowed into the log where a silent no-op would look like success.
    @discardableResult
    private func mutate(_ command: String, payload: [String: JSONValue],
                        apply: @escaping @MainActor ([String: JSONValue]) -> Void) async -> Bool {
        guard service != nil, engineState == .running else {
            notice = "the engine is not running"
            return false
        }
        do {
            let response = try await sendToEngine(command, payload: payload)
            apply(response)
            return true
        } catch {
            let message = error.localizedDescription
            notice = message
            logs.append(notice: "\(command) refused: \(message)")
            return false
        }
    }

    private func startSnapshotting() {
        stopSnapshotting()
        // **Hold an activity assertion while polling is on.** macOS App Nap throttles a background
        // app's timers — and "background" is exactly the state this app is designed to sit in, with the
        // window closed and a long run in progress. Without the assertion the console silently stops
        // updating: the panels freeze at whatever they last showed, and nothing says why. `userInitiated`
        // says the work matters to the user; `idleSystemSleepDisabled` keeps a run progressing while the
        // machine is idle; `suddenTerminationDisabled` means an automatic-termination pass cannot kill
        // the app mid-run. It is released when polling stops, so the app naps normally when idle.
        if napExemption == nil {
            napExemption = ProcessInfo.processInfo.beginActivity(
                options: [.userInitiated, .idleSystemSleepDisabled, .suddenTerminationDisabled],
                reason: "AgentOrg is supervising a run")
            logs.append(notice: "background polling enabled (App Nap exempt while running)")
        }
        let timer = Timer(timeInterval: 2.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                // Before the poll, so the status bar never shows a lapsed sentence beside a fresh
                // panel. This is also the sweep that catches a lapse when the scheduled wake-up was
                // itself deferred (App Nap), which is why the rule is a timestamp rather than a timer.
                self.expireNoticeIfStale()
                await self.refresh()
                // Models are refreshed on the *same* cadence as everything else, not gated behind
                // "no event in the last 6s". That gate was self-defeating: adding a provider emits
                // `model.catalog.refreshed`, which sets `lastEventAt`, which suppressed the very
                // refresh that would have shown the new provider's models. The result was a panel that
                // never populated after a change it had just made — the bug this fixes.
                await self.loadModels()
                // The panels this cadence is too fast (or too cheap) for — see `refreshSlowPanelsIfDue`.
                await self.refreshSlowPanelsIfDue()
            }
        }
        // **`.common`, not `.default`.** A timer scheduled the usual way is paused while a menu is open
        // or a window is being resized — so the panels would freeze precisely when the user is looking
        // at them. Common mode runs through those tracking loops.
        RunLoop.main.add(timer, forMode: .common)
        snapshotTimer = timer
    }

    /// How often the panels that are not on the two-second cadence are re-read.
    ///
    /// **One number for three reads**, because they answer the same kind of question — "what is the
    /// state of a thing that changes on the scale of minutes" — and three cadences would be three things
    /// to keep in step. Twenty seconds is set by what each read costs: the capability description and the
    /// schedule are cheap, and `portfolio_live` builds an orchestrator per org (which is why it was never
    /// on the poll at all), so a person watching any of those panels sees a view that is at most one
    /// screen stale for a twentieth of the read volume.
    public nonisolated static let defaultSlowPanelInterval: TimeInterval = 20

    /// Re-read the panels the two-second poll deliberately leaves alone, once every
    /// `defaultSlowPanelInterval`.
    ///
    /// Every one of these was previously read *only* from its pane's `.task`, and a `.task` runs when the
    /// pane appears — which is before a launching engine is ready — so the read failed, nothing retried
    /// it, and the pane went on describing an empty engine. That is the same failure the readiness
    /// handler already fixes for providers, models and the roster; this covers the panels that can also
    /// change while the window is open. Each is here for its own reason:
    ///
    /// - `system`: read-only and cheap, but it changes when the configuration does — a `system_set` from
    ///   another window, or a grant made with the CLI.
    /// - `schedules`: read from the workspace's own schedule file, which changes when an entry is added,
    ///   removed, or fired.
    /// - `portfolio_live`: the expensive one, so it is fetched only once something has asked for it —
    ///   which is the Portfolio section's own `.task`. This controller cannot see which destination is
    ///   showing, so "has been fetched before" stands in for "is on screen"; a person who never opens
    ///   that section never pays for it.
    private func refreshSlowPanelsIfDue(now: Date = Date()) async {
        if let last = lastSlowPanelRefresh,
           now.timeIntervalSince(last) < Self.defaultSlowPanelInterval { return }
        lastSlowPanelRefresh = now
        await loadSystem()
        await loadSchedules()
        if !portfolioLive.isEmpty { await loadPortfolioLive() }
    }

    private func stopSnapshotting() {
        snapshotTimer?.invalidate()
        snapshotTimer = nil
        // Release the assertion, so an idle app is a good citizen and lets the system nap it.
        if let napExemption {
            ProcessInfo.processInfo.endActivity(napExemption)
            self.napExemption = nil
        }
    }

    // MARK: - Offline run state

    /// Read `.agent_state/` again, for the panels that work with the engine down.
    ///
    /// Deliberately *not* on the 2 s poll: the run checkpoint is a small file but the handoff set and
    /// the cache streams grow with a run, and re-reading hundreds of documents every two seconds to
    /// render a panel nobody has open would cost more than it shows. It runs when the engine's stream
    /// ends (the moment the picture is worth having) and when a person presses Refresh.
    ///
    /// The reads happen **off the main actor**, on a detached task, and only the finished values are
    /// applied here. That is the same rule the process bridge follows and for the same reason: this is
    /// file I/O whose size is set by a run, and a workspace with a few hundred handoffs would stutter
    /// the window if it were read inline. `WorkspaceWriter` is safe to use from another thread — it
    /// coordinates through `NSFileCoordinator` and guards its root with a lock.
    ///
    /// Every read is best-effort by design: a workspace that has never run, or whose state directory
    /// cannot be read, produces empty lists rather than an error dialog. The one exception is the
    /// state directory itself failing containment, which *is* surfaced — a path resolving outside the
    /// workspace is a bug or an attack, not an empty project.
    public func refreshOfflineState() async {
        let browser = offline
        // The goal joins the same detached read as everything else. It is a small file, but the rule
        // here is not about size — it is that nothing in this reload touches the disk on the main actor,
        // and an exception for the one file the offline panel reads first would be exactly the kind of
        // exception that turns into a stall later.
        let snapshot = await Task.detached(priority: .utility) {
            OfflineSnapshot(
                runs: browser.lastRun().map { [$0] } ?? [],
                handoffs: browser.handoffs(),
                prefixes: browser.prefixes(),
                sessions: browser.sessions(),
                shapes: browser.cacheStream(.shapes, limit: 200),
                savings: browser.cacheStream(.savings, limit: 200),
                goal: browser.goal(),
                problem: browser.stateDirectoryProblem())
        }.value

        offlineRuns = snapshot.runs
        offlineHandoffs = snapshot.handoffs
        offlinePrefixes = snapshot.prefixes
        offlineSessions = snapshot.sessions
        offlineShapes = snapshot.shapes
        offlineSavings = snapshot.savings
        offlineGoalDocument = snapshot.goal
        offlineError = snapshot.problem
        // The context readings come from the same reload, because they come from the same documents:
        // a handoff carries the saturation its node's session had reached. Derived here rather than
        // counted in a view, so "how full are the contexts" has one answer with one test.
        contextReadings = ContextReading.latestPerNode(from: snapshot.handoffs)
        // Keep the open handoff in step with the reload, so a re-read does not leave the detail pane
        // showing a record that is no longer in the list.
        if let open = offlineHandoffDetail {
            offlineHandoffDetail = snapshot.handoffs.first { $0.id == open.id }
        } else {
            offlineHandoffDetail = snapshot.handoffs.first
        }
    }

    /// Discard a *settled* run's checkpoints, so the Runs pane and the board stop reporting it.
    ///
    /// The console's control for `engine.cli discard` — the **same engine operation**, reached through
    /// `serve._cmd_discard_run` rather than a Swift copy. The app deliberately does not move the files
    /// itself: the liveness judgement ("is a run in flight") lives with the engine, and only the
    /// engine's reply knows which entries actually moved and where the backup went. A second
    /// implementation here would be able to disagree with the terminal about both.
    ///
    /// The reply is kept in `lastDiscard` so the panel renders the count, the backup path and the kept
    /// list the engine returned rather than a sentence assembled in Swift. A no-op is *not* a refusal —
    /// the engine answers `{discarded: false, reason}` because "already clean" is the state the person
    /// wanted — so it is reported as such and returns true. A **live** run is refused with `ok: false`,
    /// which `mutate` surfaces as a notice and returns false.
    ///
    /// - Returns: whether the engine answered at all.
    @discardableResult
    public func discardRun() async -> Bool {
        let answered = await mutate("discard_run", payload: [:]) { [weak self] response in
            guard let self else { return }
            self.lastDiscard = response
            if response["discarded"]?.boolValue == true {
                let count = response["moved"]?.arrayValue?.count ?? 0
                let backup = response["backup_dir"]?.stringValue ?? ""
                self.notice = "discarded \(count) checkpoint file(s); the backup is \(backup)"
            } else {
                self.notice = response["reason"]?.stringValue ?? "nothing to discard"
            }
        }
        // The checkpoint the pane read is gone (or was already absent), so re-read `.agent_state/`
        // rather than leaving the Runs table describing a run the engine no longer reports.
        await refreshOfflineState()
        return answered
    }

    /// Everything one offline reload produces, carried back from the detached read in one hop.
    private struct OfflineSnapshot: Sendable {
        let runs: [RunSummary]
        let handoffs: [HandoffSummary]
        let prefixes: [PrefixSummary]
        let sessions: [RunStateBrowser.SessionArchive]
        let shapes: [[String: JSONValue]]
        let savings: [[String: JSONValue]]
        let goal: [String: JSONValue]
        let problem: String?
    }

    /// Open one handoff in the browser.
    public func inspectHandoff(_ id: String) {
        offlineHandoffDetail = offlineHandoffs.first { $0.id == id }
    }

    /// The goal as it was last written to disk, for the offline view.
    ///
    /// **Read during the offline reload, not on demand.** This used to be a computed property that
    /// called `offline.goal()` synchronously, which meant a disk read on whatever thread asked — and the
    /// only thing that asks is a SwiftUI view body, which is the main actor. A view whose body reads a
    /// file is a view that can stall the whole window on a slow or contended disk, and it is the exact
    /// failure mode `refreshOfflineState` was already written to avoid. The value now travels with the
    /// rest of the offline snapshot, so this property is a stored read of something already in memory.
    public var offlineGoal: [String: JSONValue] { offlineGoalDocument }

    /// Whether there is anything to show offline, so the panels can tell "no state yet" from
    /// "state this build cannot read" — two situations that both render as an empty list.
    public var hasOfflineState: Bool {
        !offlineRuns.isEmpty || !offlineHandoffs.isEmpty || !offlinePrefixes.isEmpty
    }

    // MARK: - Events

    /// The one place an engine event becomes console state.
    ///
    /// The order inside each case matters and is worth stating once: record what the engine said,
    /// *then* act on the rule, *then* notify. Acting before recording would evaluate the rule against
    /// a stale snapshot — the very mistake `gateDisposition` is centralised to prevent.
    ///
    /// Internal rather than private so a test can drive the event path directly. The alternative — a
    /// scripted child process emitting the frames — would test the pipe (already covered by
    /// `AgentProcessServiceTests`) and not the *rule*, which is the part that can silently decide
    /// something on a person's behalf.
    func handle(_ event: EngineEvent) {
        // Routine acknowledgements are not logged. The app polls `status` every two seconds, and each
        // poll is answered by a `command.ack` — so the terminal filled with an endless column of
        // "command acknowledged", burying the node transitions, handoffs and gate a person opens it to
        // read. Seen on screen in a live run; the fix is to log signal, not the heartbeat.
        //
        // Everything else is still recorded, including a *refused* ack: a command the engine declined
        // is the opposite of routine, and hiding it would hide the reason a button did nothing.
        let isRoutineAck = event.type == "command.ack"
            && (event.payload["ok"]?.boolValue ?? false)
        if !isRoutineAck {
            logs.append(event)
            // **The same filter for the liveness timestamp, and for the same reason.** Stamping this
            // before the filter made the poll itself the event: `lastEventAt` changed on every tick, and
            // with it every view holding this controller — twice a second, for a heartbeat nobody asked
            // to see. What the field answers is "when did the engine last say something worth reacting
            // to", which is exactly what the filter already means.
            lastEventAt = Date()
        }

        switch event.type {
        case "engine.ready":
            // The engine confirmed it is usable. Clearing any failure here is what makes a retry after a
            // fix show as healthy rather than leaving the old banner up.
            engineFailure = nil
            engineError = nil
            // And the same for the status bar: whatever it was still saying is a statement about a state
            // that no longer holds — "engine failed", "the engine is not running", "a retry cannot fix
            // that" — and it would otherwise sit there for its full lifetime after the problem is gone.
            notice = nil
            // **Load what only a running engine can answer.** Every pane loads providers on its own
            // `.task`, which runs when the view appears — and the view appears *before* the engine has
            // booted, because starting it takes seconds. So the load hit a dead pipe, left `providers`
            // empty, and nothing ever retried: the engine reported "ready (5 providers)" in the
            // terminal while the pane said "no provider is configured yet" and offered a form to add
            // one that was already there. Seen on screen in a live run.
            //
            // The roster and skill list have the identical failure mode — both are engine round trips
            // fired from a pane's `.task` — which is why all three are loaded here and not there.
            //
            // Readiness is the event that makes these reads meaningful, so it is where they belong — and
            // they go out together, because nothing orders them. Awaiting them in turn made the moment
            // the engine became usable the slowest moment in the app: three round trips before the first
            // panel had anything in it.
            Task { [weak self] in await self?.loadWindow() }
        case "error":
            // A fatal startup error is the reason the engine is about to die. Captured now, from the
            // frame the engine writes to stdout for exactly this purpose — the app otherwise only ever
            // saw "the engine exited with status 1".
            if event.payload["fatal"]?.boolValue == true {
                let reason = event.payload["message"]?.stringValue ?? "the engine reported a fatal error"
                engineFailure = reason
                notice = reason
            } else {
                notice = event.payload["message"]?.stringValue ?? "the engine reported an error"
            }
        case "manifest.proposed":
            proposedGraph = event.payload
        case "manifest.approved":
            proposedGraph = nil
        case "human.gate":
            handleGate(event)
        case "human.decision", "policy.changed":
            // `policy.changed` is emitted for an Owner instruction and an autonomy edit as well as for a
            // goal's gate decision, so only the gate-decision shape is treated as one. Reading `by`
            // here is what lets the console tell "the engine decided this" from "a person did" — the
            // same distinction the engine's own ledger keeps.
            if event.payload["by"]?.stringValue == "goal",
               event.payload["gate_id"]?.stringValue != nil {
                clearGateAnsweredByGoal(event)
                gateNote = event.payload["why"]?.stringValue
            } else if event.type == "human.decision" {
                pendingGate = nil
            }
        case "cost.ceiling":
            notice = "the run budget ceiling was reached; the run parked"
        case "guardrail.blocked", "guardrail.block":
            notice = "a guardrail blocked a payload"
        case "leak.detected":
            notice = "a key-shaped string was found in the run state"
        case "run.end":
            notice = "run finished: \(event.payload["outcome"]?.stringValue ?? "unknown")"
            // The stream is over, so the durable state is now the better picture — and it is the one
            // that will still be there if the engine never comes back.
            Task { await refreshOfflineState() }
        case "goal.completed":
            // The goal's own verdict, surfaced in the status bar as well as the terminal: a run that
            // finished because the *objective* was met is a different fact from a run that ran out of
            // graph, and the one line a person reads should say which.
            notice = "goal complete: \(event.payload["summary"]?.stringValue ?? "done")"
            Task { await refreshOfflineState() }
        case "goal.blocked":
            notice = "goal blocked: \(event.payload["reason"]?.stringValue ?? "it needs you")"
            Task { await refreshOfflineState() }
        case "goal.paused":
            // Only when the engine paused it for a reason other than a gate: a gate has its own event
            // arriving a moment later, and two notices for one stop is the noise the planner avoids too.
            let reason = event.payload["reason"]?.stringValue ?? "manual"
            if reason != "gate" {
                notice = "goal paused (\(reason))"
            }
        default:
            break
        }
        notify(for: event)
    }

    /// A gate arrived: record the engine's own answer, then act on it — or do not.
    ///
    /// The whole auto-resume path is these two steps and one call to `gateDisposition`. There is no
    /// attempt to work out whether the gate *could* be passed, because the engine has already done
    /// that work and reported the result: a `waiting_on: owner` on this payload is its refusal, with
    /// its reason. The console's job is to remove the click for the gates the engine left to the goal,
    /// and to leave everything else exactly as it was.
    private func handleGate(_ event: EngineEvent) {
        pendingGate = event.payload
        let disposition = Self.gateDisposition(
            gate: event.payload,
            posture: goalPosture,
            // The gate arriving here is by definition not yet decided, so this is always false; it is
            // passed explicitly rather than omitted so the call site reads as the whole rule.
            decidedByGoal: false)

        gateNote = disposition.why
        switch disposition {
        case .forwardApproval(let why):
            // The engine declared the goal may answer this gate and has not answered it. The console
            // forwards the approval — the *engine* makes the decision and applies its own evidence and
            // safety checks; the app only supplies the click. If the engine then refuses (evidence
            // missing, a guardrail fired), it re-emits `human.gate` this time carrying `waiting_on:
            // owner`, and the next pass through here leaves the gate alone.
            logs.append(notice: "auto-approving gate \(event.payload["gate_id"]?.stringValue ?? "?"): \(why)")
            Task { await approve(note: "auto-approved: the goal's posture is unattended") }
        case .waitForHuman(let why):
            notice = "waiting on you: \(event.payload["reason"]?.stringValue ?? "a gate")"
            logs.append(notice: "gate \(event.payload["gate_id"]?.stringValue ?? "?") is yours: \(why)")
        case .answeredByGoal(let why):
            // Reachable when the engine's decision frame arrives *after* the gate frame in the same
            // burst. Treated as recorded, not acted on.
            clearGateAnsweredByGoal(event)
            gateNote = why
        }
    }

    /// Whether the gate currently on screen is a question for a person.
    ///
    /// The one thing the views need, so a button is never offered for a decision the console is about
    /// to make itself — which would be a race between a person's click and the app's own approval.
    public var gateIsWaitingForHuman: Bool {
        pendingGate != nil && gateDisposition.isWaitingForHuman
    }

    // MARK: - The first-run gate and the spine

    /// What is still standing between the person and a run.
    ///
    /// Evaluated from the engine's own reports, through the pure `SetupReadiness.gate`, so the answer
    /// has a test rather than living in a view body. The two app-remembered answers (which project
    /// was confirmed, how much a goal may decide) are read from `preferences`, which is why they are
    /// in `AppPreferences` rather than in `UserDefaults` scattered through the views.
    public var setupGate: SetupGate {
        SetupReadiness.gate(
            engineIsRunning: engineState == .running,
            engineFailure: engineFailure ?? engineError,
            defaults: defaults,
            providers: providers,
            projectConfirmed: preferences.projectConfirmed,
            postureChosen: preferences.chosenPosture != nil)
    }

    /// Whether the first-run wizard should be showing.
    ///
    /// It disappears **for good** once a person has answered the two things only they can answer, even
    /// if the engine later loses its provider: a wizard thrown back in front of someone who has
    /// already answered it is the app forgetting what it was told. The spine's setup hint covers the
    /// later breakage, which is the honest way to say "this stopped working" rather than "start over".
    public var showsFirstRunWizard: Bool {
        guard canLaunch else { return false }
        if preferences.wizardCompleted { return false }
        // **Nothing left to ask means the wizard is done, not that it should sit there.** The retire
        // step lived only on the Autonomy step's button, so a workspace that was *already* configured
        // — a returning person, or anyone whose project and posture were set in an earlier session —
        // reached `.ready` with no step left to press and the console showed "Ready. Setting up your
        // console…" forever, blocking the whole window. Seen on screen in a live run.
        //
        // A pure read: the flag is written by `retireWizardIfDone`, which the view calls, because a
        // computed property that mutates would fire on every render pass.
        return setupGate != .ready
    }

    /// Mark first-run set-up as finished when there is nothing left to ask.
    ///
    /// Called by the console as the gate is evaluated, so a workspace that is already configured
    /// retires the wizard instead of showing a step with no question in it. Idempotent, and a no-op
    /// while any step is genuinely unfinished.
    public func retireWizardIfDone() {
        guard !preferences.wizardCompleted, canLaunch, setupGate == .ready else { return }
        preferences.wizardCompleted = true
        objectWillChange.send()
    }

    /// The spine, built from everything the engine reported.
    public var spine: SpineModel {
        SpineModel.make(SpineInput(
            engineState: engineState,
            engineFailure: engineFailure,
            engineError: engineError,
            workspace: workspace,
            projectPath: projectPath,
            goal: goal,
            activity: activity,
            pendingGate: pendingGate,
            gateDisposition: gateDisposition,
            setupHint: setupHint))
    }

    /// The hint the spine shows while a run is not yet possible, or nil.
    ///
    /// Two things are deliberate here.
    ///
    /// First, **no hint before the wizard has been answered**: while it is showing, the wizard *is* the
    /// next step, and repeating it on the spine above would be the same sentence twice.
    ///
    /// Second, **no hint for a stopped engine once the wizard is done**. A person who pressed Stop has
    /// an engine that is stopped, which is a normal state rather than a configuration problem — and the
    /// spine's own state row already says "Engine not running" with a Start button beside it. A hint
    /// there would be noise. What survives is the case worth pointing at: a configuration that stopped
    /// working (a provider removed, a model that no longer resolves), where the person would otherwise
    /// see a healthy-looking app in which nothing can run.
    public var setupHint: String? {
        let gate = setupGate
        if showsFirstRunWizard {
            // The wizard is on screen and is the next step; the spine must not say it again.
            return nil
        }
        if preferences.wizardCompleted, gate.kind == .engineUnavailable {
            // Stop is a normal state, and the spine's own row covers it.
            return nil
        }
        return SetupReadiness.spineHint(for: gate)
    }

    /// The gate's evidence, as the engine reported it: what it wants and what is present.
    public var gateEvidence: (requires: [String], present: [String]) {
        let gate = pendingGate ?? [:]
        return ((gate["requires"]?.arrayValue ?? []).compactMap { $0.stringValue },
                (gate["present"]?.arrayValue ?? []).compactMap { $0.stringValue })
    }

    // MARK: - Derived views

    /// The one sentence the spine shows about a wait, or nil when nothing is waiting.
    ///
    /// Both waits are reported through one line because they answer the same question — "is this app
    /// doing something?" — and two separate rows would compete for the same glance. The launch wins when
    /// both are true, because nothing else can be meaningful while the engine is still coming up: every
    /// `status` poll during a launch fails at `engineState == .running`, so a slow command during a
    /// launch is really just the launch.
    ///
    /// The command is *named*, which is the whole point: "Starting the engine" and "waiting for
    /// `providers`" are actionable in a way that a spinner is not.
    public var waitingSummary: String? {
        let now = Date()
        if let progress = launchProgress {
            return progress.summary(now: now)
        }
        guard let slowest = slowCommands.first else { return nil }
        let seconds = Int(slowest.elapsed(now: now))
        let others = slowCommands.count - 1
        let rest = others > 0 ? " (and \(others) more)" : ""
        return "Waiting on the engine — `\(slowest.type)` has not answered in \(seconds)s\(rest)"
    }

    /// What to do about a wait that has gone on too long, or nil while it is still normal.
    public var waitingAdvice: String? {
        if let progress = launchProgress {
            return progress.advice(now: Date())
        }
        guard let slowest = slowCommands.first else { return nil }
        let budget = Int(slowest.deadline.timeIntervalSince(slowest.sentAt))
        return "`\(slowest.type)` is still unanswered. A command like `improve` legitimately takes a "
            + "while; anything else this slow usually means the engine is blocked. It will be reported "
            + "as failed after \(budget)s."
    }

    /// The roster grouped by team, so the org chart matches how the org was built.
    public var rosterByTeam: [(team: String, members: [[String: JSONValue]])] {
        var grouped: [String: [[String: JSONValue]]] = [:]
        for agent in agents {
            let team = agent["team"]?.stringValue ?? "(no team)"
            grouped[team.isEmpty ? "(no team)" : team, default: []].append(agent)
        }
        return grouped.sorted { $0.key < $1.key }.map { (team: $0.key, members: $0.value) }
    }

    /// Health states, with the count of each. The org-health panel's headline.
    public var healthSummary: [String: Int] {
        var counts: [String: Int] = ["healthy": 0, "degraded": 0, "quarantined": 0, "other": 0]
        for agent in agents {
            switch agent["state"]?.stringValue {
            case "quarantined": counts["quarantined", default: 0] += 1
            case "blocked", "waiting": counts["degraded", default: 0] += 1
            case "terminated": counts["other", default: 0] += 1
            default: counts["healthy", default: 0] += 1
            }
        }
        return counts
    }

    /// The cost rollup, with an unmeasured figure kept distinct from a free one.
    public var costSummary: [String: JSONValue] { runStatus["cost"]?.objectValue ?? [:] }

    /// The prompt-cache rollup for the current run.
    ///
    /// Every field is optional on purpose: a provider that reports no cache yields no numbers here,
    /// and the panel must say "unreported" rather than draw a confident 0% hit rate.
    public var cacheSummary: [String: JSONValue] { runStatus["cache"]?.objectValue ?? [:] }

    /// The swarm picture: fan-out progress and the last vote tally.
    ///
    /// Empty until something has run, which the panel must render as "no swarm yet" rather than as
    /// zero items — the same distinction the cache and cost views make.
    public var swarmSummary: [String: JSONValue] { runStatus["swarm"]?.objectValue ?? [:] }

    /// Whether a fan-out is in flight right now.
    public var swarmRunning: Bool { swarmSummary["running"]?.boolValue == true }

    /// The activity report's staffing gaps: capabilities the plan needs that nobody holds.
    ///
    /// Read from the activity report rather than re-derived, so the CLI and the app name the same
    /// gaps with the same hire suggestions.
    public var staffingGaps: [[String: JSONValue]] {
        (activity["staffing_gaps"]?.arrayValue ?? []).compactMap { $0.objectValue }
    }

    /// One row per fan-out item: its label, whether it finished, and where it went wrong.
    public var swarmItems: [[String: JSONValue]] {
        guard let fanout = swarmSummary["fanout"]?.objectValue,
              let items = fanout["items"]?.arrayValue else { return [] }
        return items.compactMap { $0.objectValue }
    }

    /// A readable swarm line: how much of a fan-out is done, or that there is none.
    public var swarmDescription: String {
        guard let fanout = swarmSummary["fanout"]?.objectValue else {
            return "no swarm has run"
        }
        let count = fanout["count"]?.intValue ?? 0
        let ok = fanout["succeeded"]?.intValue ?? 0
        let failed = fanout["failed"]?.intValue ?? 0
        var parts = ["fan-out: \(ok)/\(count) done"]
        if failed > 0 {
            // A partial fan-out is the state worth stating plainly: it means work was left undone.
            parts.append("\(failed) failed")
        }
        // A vote and a fan-out are different things and are labelled as such.
        if let vote = swarmSummary["vote"]?.objectValue,
           let tally = vote["tally"]?.objectValue {
            let counts = tally.compactMapValues { $0.intValue }
                .map { "\($0.key)×\($0.value)" }.sorted().joined(separator: ", ")
            parts.append("vote: \(counts)")
        }
        return parts.joined(separator: " · ")
    }

    /// A readable cache line: the hit rate, and what it saved — or an explicit "unreported".
    public var cacheDescription: String {
        let cache = cacheSummary
        guard cache["cache_reported"]?.boolValue == true else {
            return "prompt cache: not reported by this provider"
        }
        let hit = cache["cache_hit_tokens"]?.intValue ?? 0
        let miss = cache["cache_miss_tokens"]?.intValue ?? 0
        let rate = cache["cache_hit_rate"]?.doubleValue
            ?? (hit + miss > 0 ? Double(hit) / Double(hit + miss) : 0)
        var parts = [String(format: "%.1f%% cached", rate * 100),
                     "\(hit) hit / \(miss) miss"]
        // A saving is only stated when it could be computed; "unknown" is not "$0.00".
        if let saving = cache["cache_saving_usd"]?.doubleValue {
            parts.append(String(format: "saved $%.4f", saving))
        } else {
            parts.append("saving unknown")
        }
        return parts.joined(separator: " · ")
    }

    /// A readable cost line, which never renders an unmeasured total as zero.
    public var costDescription: String {
        let cost = costSummary
        guard let runs = cost["runs"]?.intValue else { return "no data yet" }
        let total = cost["cost_usd"]?.doubleValue
        let unreported = cost["cost_unreported_spans"]?.intValue ?? 0
        var parts = ["\(runs) run(s), \(cost["nodes"]?.intValue ?? 0) node(s)"]
        parts.append(total == nil ? "cost unknown" : String(format: "cost $%.4f", total!))
        if unreported > 0 {
            // The distinction that matters: unmeasured is not free.
            parts.append("\(unreported) span(s) did not report usage")
        }
        return parts.joined(separator: " · ")
    }
}
