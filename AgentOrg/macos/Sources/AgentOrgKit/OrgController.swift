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

/// Which panel the window is showing.
public enum ConsoleTab: String, CaseIterable, Identifiable, Sendable {
    // Seven views, each answering exactly one question. `observability-engineer` is explicit that a
    // dashboard without a single question is sprawl, so the tabs are the questions.
    // The tabs are the questions. `portfolio` is first because it is the *whole* picture — the
    // several orgs one person runs — and every other tab is a view *within* one of them.
    case portfolio = "Portfolio"
    case org = "Org"
    case people = "People"
    case providers = "Providers"
    case improve = "Improve"
    case activity = "Activity"
    case flow = "Flow"
    case progress = "Work"
    case economics = "Cost"
    case context = "Context"
    case resources = "Resources"

    public var id: String { rawValue }

    /// The question this panel answers, shown as its subtitle.
    public var question: String {
        switch self {
        case .portfolio: return "Which orgs am I running, and what is each doing?"
        case .org: return "Who do I have, and is the org healthy?"
        case .people: return "Who can I hire, and what are they on?"
        case .providers: return "Which models can I reach, and with what?"
        case .improve: return "What does the system think is wrong with itself?"
        case .activity: return "What is happening, why, and what do I do next?"
        case .flow: return "Who is working on what, and what crossed between them?"
        case .progress: return "Where is work stuck?"
        case .economics: return "What is this costing?"
        case .context: return "How full are the agents' contexts?"
        case .resources: return "Is the machine coping?"
        }
    }

    /// The question, shortened for a sidebar row.
    ///
    /// A sidebar row has about twenty characters of comfort before it truncates, so the full question
    /// lives in the window subtitle and this carries the gist. Two strings rather than one truncated
    /// one, because an ellipsis in the *middle* of a question is worse than a shorter question.
    public var shortQuestion: String {
        switch self {
        case .portfolio: return "orgs · missions"
        case .org: return "the roster"
        case .people: return "hiring · models"
        case .providers: return "endpoints · keys"
        case .improve: return "proposals · gate"
        case .activity: return "timeline · next step"
        case .flow: return "who · handoffs · back"
        case .progress: return "run · gates · swarm"
        case .economics: return "spend · cache"
        case .context: return "window fullness"
        case .resources: return "cpu · memory"
        }
    }

    /// The sidebar glyph. SF Symbols, so it matches the rest of the system and needs no assets.
    public var symbol: String {
        switch self {
        case .portfolio: return "building.2"
        case .org: return "person.3"
        case .people: return "person.badge.plus"
        case .providers: return "server.rack"
        case .improve: return "wand.and.stars"
        case .activity: return "list.bullet.rectangle.portrait"
        case .flow: return "arrow.triangle.branch"
        case .progress: return "point.topleft.down.curvedto.point.bottomright.up"
        case .economics: return "dollarsign.circle"
        case .context: return "gauge.with.dots.needle.bottom.50percent"
        case .resources: return "cpu"
        }
    }
}

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

    /// The roster: names, skills, models, live state.
    @Published public private(set) var agents: [[String: JSONValue]] = []
    /// The current run's status, from the engine's `status` command.
    @Published public private(set) var runStatus: [String: JSONValue] = [:]
    /// A gate the run is waiting on, if any.
    @Published public private(set) var pendingGate: [String: JSONValue]?
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
    /// The portfolio: the principal and every org they run.
    ///
    /// The register travels with status; the *live* picture (each org's mission, spend, blockers)
    /// is a separate, more expensive fetch the Portfolio panel asks for when it is open.
    @Published public private(set) var portfolio: [String: JSONValue] = [:]
    /// The live cross-org picture, when the Portfolio panel has fetched it: rollup + fleet status.
    @Published public private(set) var portfolioLive: [String: JSONValue] = [:]
    /// Whether the live fetch is in flight, so the panel can say so rather than look stale.
    @Published public private(set) var portfolioLoading: Bool = false
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
    @Published public var notice: String?
    /// The objective the user is composing, shared so the toolbar field and the Goal menu agree.
    ///
    /// Held on the controller rather than as `@State` in one view: the File/Run/Goal menus live in the
    /// App scene, not inside the window's view hierarchy, so a `@State` there would be invisible to the
    /// menu and the two would silently disagree about what goal is about to be set.
    @Published public var goalDraft: String = ""
    @Published public private(set) var lastEventAt: Date?

    public let logs: LogStore
    public let writer: WorkspaceWriter

    // MARK: - Dependencies

    private let runtime: PythonRuntime
    private var settings: OrgSettings
    private var service: AgentProcessService?
    private var snapshotTimer: Timer?
    /// The App Nap exemption held while polling. See `startSnapshotting` for why it exists.
    ///
    /// Named `napExemption` rather than `activity` because `activity` is the published activity
    /// *report*; two things called the same thing in one type is how a confusing shadow gets added.
    private var napExemption: (any NSObjectProtocol)?

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
        public static func discover(repositoryRoot: URL, slug: String = "demo") -> OrgSettings {
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
    }

    /// The `logs` parameter is optional rather than defaulted to a new `LogStore()`: a default value is
    /// evaluated in a nonisolated context, and `LogStore` is main-actor state, so the default would be an
    /// actor-isolation error. Creating it lazily here keeps the isolation explicit.
    public init(settings: OrgSettings, logs: LogStore? = nil) {
        self.settings = settings
        self.logs = logs ?? LogStore()
        self.writer = WorkspaceWriter(root: settings.projectPath)
        self.runtime = PythonRuntimeResolver.resolveFromEnvironment()
        self.projectPath = settings.projectPath.path
        self.credentialsPath = settings.credentialsPath?.path ?? "(not set)"
        self.libraryPath = settings.libraryRoot?.path ?? "(auto-discovered)"
        self.engineDiagnostics = ["runtime: \(runtime.display)"]
    }

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
        // `engineRoot` is the *package* directory (`<repo>/AgentOrg`), which is the working directory
        // for `python3 -m engine.cli` — so the module lives one level below it, not inside it.
        let module = root.appendingPathComponent("engine/cli.py")
        if !fm.fileExists(atPath: module.path) {
            return "\(root.path) exists but \(module.path) does not, so it is not the engine package"
        }
        return nil
    }

    /// Launch the engine and begin streaming.
    public func launch() {
        // Guard on *any* live state, not just `.running`. `.launching` means a spawn is already in
        // flight, so a second call during that window would start a **second engine** on the same
        // project — two processes writing one checkpoint. That is exactly the race the app's own
        // first-run `.task` creates when the engine is started from the menu at the same moment.
        guard !engineState.isLive else { return }
        // A fresh launch clears the previous failure, so a fixed config does not leave a stale banner.
        engineFailure = nil
        engineError = nil
        logs.append(notice: "launching the engine…")

        // Check the paths *before* spawning anything. `Process.run()` fails with a message that names
        // whichever path component is missing — "The file 'AgentOrg' doesn't exist" — which is true but
        // says nothing about *which* of the four paths was wrong or what it should have been. Naming
        // the resolved values here is the difference between a five-minute fix and an afternoon.
        if let problem = launchProblem() {
            engineState = .failed
            engineError = problem
            logs.append(notice: "launch failed: \(problem)")
            return
        }

        let config = EngineLaunchConfig(
            runtime: runtime,
            engineRoot: settings.engineRoot,
            projectPath: settings.projectPath,
            credentialsPath: settings.credentialsPath,
            libraryRoot: settings.libraryRoot,
            attachedProjectPath: settings.attachedProject)
        let service = AgentProcessService(config: config)

        service.onStateChange = { [weak self] state in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.engineState = state
                self.engineError = service.lastError?.message
                switch state {
                case .running:
                    // Only now is the engine *usable*: `.running` is set from the readiness frame
                    // (`engine.ready`), not from the spawn. So this notice is a true statement, unlike
                    // the old one that printed "engine running" for a process that had already died.
                    self.logs.append(notice: "engine ready (pid \(service.pid ?? 0))")
                    self.startSnapshotting()
                case .failed:
                    // A failure must be impossible to miss. It goes to the terminal *and* to `notice`,
                    // which the status bar and every panel surface — the whole point, because the old
                    // behaviour showed a healthy-looking engine that was doing nothing.
                    let reason = service.lastError?.message ?? "the engine failed to start"
                    self.logs.append(notice: "engine failed: \(reason)")
                    self.engineFailure = reason
                    self.stopSnapshotting()
                default:
                    if !state.isLive { self.stopSnapshotting() }
                }
            }
        }
        service.onEvent = { [weak self] event in
            Task { @MainActor [weak self] in self?.handle(event) }
        }
        service.onDiagnostic = { [weak self] line in
            Task { @MainActor [weak self] in
                self?.logs.append(diagnostic: line)
                self?.engineDiagnostics.append(line)
                // Bound the diagnostics list the same way the log is bounded.
                if let count = self?.engineDiagnostics.count, count > 500 {
                    self?.engineDiagnostics.removeFirst(count - 500)
                }
            }
        }
        service.onUnparsable = { [weak self] text in
            Task { @MainActor [weak self] in self?.logs.append(unparsable: text) }
        }

        self.service = service
        do {
            try service.launch()
        } catch {
            engineError = error.localizedDescription
            logs.append(notice: "launch failed: \(error.localizedDescription)")
        }
    }

    /// Stop the engine, letting it checkpoint first.
    public func stop() {
        stopSnapshotting()
        service?.terminate()
        logs.append(notice: "engine stopping…")
    }

    // MARK: - Commands

    /// Send a command and log the outcome.
    ///
    /// The acknowledgement is awaited rather than fired and forgotten: a command that was refused must
    /// surface, or the UI would show a state the engine never entered.
    public func send(_ type: String, payload: [String: JSONValue] = [:]) async {
        guard let service else {
            notice = "the engine is not running"
            return
        }
        do {
            _ = try await service.send(type, payload: payload)
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
    public func approve(note: String = "") async {
        var payload: [String: JSONValue] = [:]
        if !note.isEmpty { payload["note"] = .string(note) }
        await send("approve", payload: payload)
        pendingGate = nil
        proposedGraph = nil
        await refresh()
    }

    public func reject(note: String = "") async {
        var payload: [String: JSONValue] = [:]
        if !note.isEmpty { payload["note"] = .string(note) }
        await send("reject", payload: payload)
        pendingGate = nil
        await refresh()
    }

    /// Push guidance into the run. With `asConstraint` it becomes non-negotiable, so the AR-04 machinery
    /// then preserves it verbatim across every compaction and rotation.
    public func instruct(_ text: String, asConstraint: Bool = false) async {
        var payload: [String: JSONValue] = ["text": .string(text)]
        if asConstraint { payload["as_constraint"] = .bool(true) }
        await send("instruct", payload: payload)
    }

    public func pause() { service?.post("pause"); logs.append(notice: "→ pause") }
    public func resume() async { await send("resume"); await refresh() }
    public func abort() async { await send("abort"); await refresh() }

    // MARK: - Attaching a project

    /// Point the org at an existing folder — the user's own repository.
    ///
    /// Stops the engine first, letting it checkpoint, and relaunches with `--project`. A relaunch
    /// rather than a live re-root: the executing subprocess holds the workspace path, and mutating it
    /// underneath a running graph is how a run writes half its artifacts into the previous project.
    /// Stopping at a checkpoint is the same mechanism `pause`/`resume` already uses.
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
            // Give the checkpoint a moment to land. A re-root that raced a write would split the
            // state, which is exactly what stopping first is meant to prevent.
            try? await Task.sleep(nanoseconds: 600_000_000)
        }

        settings.attachedProject = url
        settings.projectPath = url
        projectPath = url.path
        writer.updateRoot(url)
        logs.append(notice: "attached project: \(url.path)")
        logs.append(notice: "engine state will be written to \(url.path)/.agent_state")

        if wasLive {
            launch()
        }
    }

    /// Detach: go back to a managed project under `AgentOrg/projects/`.
    public func detachProject(slug: String = "demo") async {
        let wasLive = engineState.isLive
        if wasLive {
            service?.terminate()
            try? await Task.sleep(nanoseconds: 600_000_000)
        }
        settings.attachedProject = nil
        let managed = settings.engineRoot.appendingPathComponent("projects/\(slug)")
        settings.projectPath = managed
        projectPath = managed.path
        writer.updateRoot(managed)
        logs.append(notice: "using the managed project \(managed.path)")
        if wasLive { launch() }
    }

    // MARK: - The goal

    /// Set the objective and arm the loop.
    public func setGoal(_ objective: String, arm: Bool = true) async {
        var payload: [String: JSONValue] = ["objective": .string(objective)]
        if !arm { payload["no_arm"] = .bool(true) }
        await send("goal_set", payload: payload)
        await refresh()
    }

    /// Turn the human gate on or off for the *current* goal, without changing the objective.
    ///
    /// Re-sets the goal with the same objective and the chosen autonomy. `goal_set` on an unchanged
    /// objective with changed policy replaces the policy and keeps the history, so the escape hatch is
    /// one click rather than "clear it and start again".
    public func setGoalHumanGate(_ enabled: Bool) async {
        let objective = goal["objective"]?.stringValue ?? ""
        guard !objective.isEmpty else { return }
        var payload: [String: JSONValue] = ["objective": .string(objective), "no_arm": .bool(true)]
        if enabled {
            payload["human_gate"] = .bool(true)
        } else {
            payload["auto_approve"] = .bool(true)
            payload["auto_hire"] = .bool(true)
        }
        await send("goal_set", payload: payload)
        await refresh()
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
    /// A separate, deliberate fetch rather than part of every poll: it builds an orchestrator per org,
    /// so doing it on the 2s cadence would read every roster continuously. The panel asks when it is
    /// open, which is exactly when the cost is wanted.
    public func loadPortfolioLive() async {
        portfolioLoading = true
        defer { portfolioLoading = false }
        await fetch("portfolio_live") { [weak self] payload in
            self?.portfolioLive = payload
        }
    }

    /// Register an org from the console.
    public func addOrg(name: String, path: String = "", charter: String = "",
                       dailyBudgetUSD: Double = 0, active: Bool = false) async {
        var payload: [String: JSONValue] = ["name": .string(name)]
        if !path.isEmpty { payload["path"] = .string(path) }
        if !charter.isEmpty { payload["charter"] = .string(charter) }
        if dailyBudgetUSD > 0 { payload["daily_budget_usd"] = .double(dailyBudgetUSD) }
        if active { payload["active"] = .bool(true) }
        await send("portfolio_add", payload: payload)
        await refresh()
    }

    public func removeOrg(_ ref: String) async {
        await send("portfolio_remove", payload: ["org": .string(ref)])
        await refresh()
    }

    /// Make one org the default the console acts on.
    public func selectOrg(_ ref: String) async {
        await send("portfolio_select", payload: ["org": .string(ref)])
        await refresh()
        await loadPortfolioLive()
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
    public func setDefaults(provider: String = "", model: String = "",
                            reviewerModel: String = "", contextWindow: Int? = nil) async {
        var payload: [String: JSONValue] = [:]
        if !provider.isEmpty { payload["provider"] = .string(provider) }
        if !model.isEmpty { payload["model"] = .string(model) }
        if !reviewerModel.isEmpty { payload["reviewer_model"] = .string(reviewerModel) }
        if let contextWindow { payload["context_window"] = .int(contextWindow) }
        await send("defaults_set", payload: payload)
        await refresh()
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

    public var hasPortfolio: Bool { !portfolioOrgs.isEmpty }

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
    @discardableResult
    public func saveProvider(_ draft: ProviderDraft) async -> Bool {
        let ok = await mutate("provider_add", payload: draft.payload()) { [weak self] payload in
            if let list = payload["providers"]?.arrayValue {
                self?.providers = list.compactMap { $0.objectValue }
            }
        }
        await loadModels()
        return ok
    }

    @discardableResult
    public func removeProvider(id: String) async -> Bool {
        let ok = await mutate("provider_remove",
                              payload: ["provider_id": .string(id)]) { [weak self] payload in
            if let list = payload["providers"]?.arrayValue {
                self?.providers = list.compactMap { $0.objectValue }
            }
        }
        await loadModels()
        return ok
    }

    // MARK: - The self-improvement loop

    /// Load what the improver has proposed, and what it refused.
    public func loadProposals() async {
        await fetch("proposals") { [weak self] payload in
            guard let self else { return }
            if let list = payload["proposals"]?.arrayValue {
                self.proposals = list.compactMap { $0.objectValue }
            }
            self.proposalsRefused = payload["refused_count"]?.intValue ?? 0
            if let refused = payload["refused"]?.arrayValue {
                self.proposalsRefusedList = refused.compactMap { $0.objectValue }
            }
            self.proposalsDirectory = payload["directory"]?.stringValue ?? ""
        }
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
                self?.proposals = list.compactMap { $0.objectValue }
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
    public func dismissProposal(id: String) async {
        // Local only: the file is the Owner's to delete, and a command that deleted it for them would
        // be the engine acting on their behalf on the one surface where it must not.
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

    /// Remove an agent from the roster.
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
            guard let self else { return }
            self.runStatus = payload
            if let roster = payload["org"]?.arrayValue {
                self.agents = roster.compactMap { $0.objectValue }
            }
            if let gate = payload["gate"]?.objectValue {
                self.pendingGate = gate
            } else {
                self.pendingGate = nil
            }
            if let nodes = payload["outcome"]?.objectValue?["nodes"]?.objectValue {
                self.nodes = nodes.map { key, value in
                    var entry = value.objectValue ?? [:]
                    entry["name"] = .string(key)
                    return entry
                }
                .sorted { ($0["name"]?.stringValue ?? "") < ($1["name"]?.stringValue ?? "") }
            }
            // The goal, the attached workspace and the subagent tree all travel with status, so one
            // poll keeps every panel current rather than needing three commands the UI might forget.
            if let goal = payload["goal"]?.objectValue { self.goal = goal }
            if let mission = payload["mission"]?.objectValue { self.mission = mission }
            // The portfolio register travels with status, so the Portfolio panel lists the orgs
            // without a second command on every poll.
            if let portfolio = payload["portfolio"]?.objectValue { self.portfolio = portfolio }
            if let workspace = payload["workspace"]?.objectValue { self.workspace = workspace }
            // The activity report travels with status, so the "what is happening" panel is current
            // from the same poll every other panel uses.
            if let activity = payload["activity"]?.objectValue { self.activity = activity }
            // The org board travels the same way, so the Flow panel shows who is on what from the poll
            // every other panel already makes.
            if let flow = payload["flow"]?.objectValue { self.flow = flow }
            // The effective default pair and the default autonomy travel too, so the Providers panel's
            // Defaults editor is current without a second command it would have to remember.
            if let defaults = payload["defaults"]?.objectValue { self.defaults = defaults }
            if let children = payload["subagents"]?.objectValue?["children"]?.arrayValue {
                self.subagents = children.compactMap { $0.objectValue }
            }
            // Proposals travel with status too, so a promoted fix appears without the console having to
            // remember a second command — and the count can badge the sidebar.
            if let proposals = payload["proposals"]?.objectValue {
                if let list = proposals["proposals"]?.arrayValue {
                    self.proposals = list.compactMap { $0.objectValue }
                }
                self.proposalsRefused = proposals["refused_count"]?.intValue ?? 0
                if let refused = proposals["refused"]?.arrayValue {
                    self.proposalsRefusedList = refused.compactMap { $0.objectValue }
                }
                self.proposalsDirectory = proposals["directory"]?.stringValue ?? ""
            }
        }
    }

    /// Load the model catalog, for the agent-editor picker.
    public func loadModels() async {
        await fetch("models") { [weak self] payload in
            if let list = payload["models"]?.arrayValue {
                self?.models = list.compactMap { $0.objectValue }
            }
        }
    }

    private func fetch(_ command: String,
                       apply: @escaping @MainActor ([String: JSONValue]) -> Void) async {
        await fetch(command, payload: [:], apply: apply)
    }

    /// A fetch that carries a payload, for commands that take arguments (paging a transcript).
    private func fetch(_ command: String, payload: [String: JSONValue],
                       apply: @escaping @MainActor ([String: JSONValue]) -> Void) async {
        guard let service, engineState == .running else { return }
        do {
            let response = try await service.send(command, payload: payload)
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
        guard let service, engineState == .running else {
            notice = "the engine is not running"
            return false
        }
        do {
            let response = try await service.send(command, payload: payload)
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
                await self?.refresh()
                // Models are refreshed on the *same* cadence as everything else, not gated behind
                // "no event in the last 6s". That gate was self-defeating: adding a provider emits
                // `model.catalog.refreshed`, which sets `lastEventAt`, which suppressed the very
                // refresh that would have shown the new provider's models. The result was a panel that
                // never populated after a change it had just made — the bug this fixes.
                await self?.loadModels()
            }
        }
        // **`.common`, not `.default`.** A timer scheduled the usual way is paused while a menu is open
        // or a window is being resized — so the panels would freeze precisely when the user is looking
        // at them. Common mode runs through those tracking loops.
        RunLoop.main.add(timer, forMode: .common)
        snapshotTimer = timer
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

    // MARK: - Events

    private func handle(_ event: EngineEvent) {
        lastEventAt = Date()
        logs.append(event)

        switch event.type {
        case "engine.ready":
            // The engine confirmed it is usable. Clearing any failure here is what makes a retry after a
            // fix show as healthy rather than leaving the old banner up.
            engineFailure = nil
            engineError = nil
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
            pendingGate = event.payload
            notice = "waiting on you: \(event.payload["reason"]?.stringValue ?? "a gate")"
        case "human.decision":
            pendingGate = nil
        case "cost.ceiling":
            notice = "the run budget ceiling was reached; the run parked"
        case "guardrail.blocked", "guardrail.block":
            notice = "a guardrail blocked a payload"
        case "leak.detected":
            notice = "a key-shaped string was found in the run state"
        case "run.end":
            notice = "run finished: \(event.payload["outcome"]?.stringValue ?? "unknown")"
        default:
            break
        }
    }

    // MARK: - Derived views

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
