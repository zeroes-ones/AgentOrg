//
//  ConsoleView.swift
//  AgentOrg
//
//  The window: a status bar, a run-control bar, the selected panel, and the terminal.
//
//  Accessibility is a requirement here rather than a finishing touch. Every control carries a label, the
//  panels are reachable by keyboard, the terminal is readable by VoiceOver as text, and colour is never
//  the only signal — a state is always spelled out as well as coloured, because a status that exists only
//  as a hue is a status half the people using the app cannot read.

import SwiftUI
import AppKit
import AgentOrgKit

/// Open a folder picker and attach the chosen folder to the org.
///
/// A free function rather than a view method so both the toolbar button and the File menu use the same
/// panel: two pickers configured differently is how one of them ends up allowing files, or returning a
/// path the engine then refuses.
@MainActor
func openProjectPicker(controller: OrgController) {
    let panel = NSOpenPanel()
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.allowsMultipleSelection = false
    panel.canCreateDirectories = false
    panel.prompt = "Attach"
    panel.message = "Choose the project folder the agents should work in."
    panel.directoryURL = URL(fileURLWithPath: controller.projectPath)

    guard panel.runModal() == .OK, let url = panel.url else { return }
    Task { await controller.setProject(url) }
}

/// The banner shown when the engine died during startup.
///
/// This is the fix for a person seeing a healthy-looking app doing nothing: the engine's *reason* is
/// promoted to the top of the window, in full, with the one command that diagnoses it. A transient
/// `notice` was not enough — it faded, and the status bar said "Engine idle", which reads as "fine".
///
/// It is deliberately not dismissible: a fatal engine failure is a real broken state, and a button that
/// hides it would let the app look normal while nothing works. It clears itself when a launch succeeds
/// (the `engine.ready` frame resets the controller's failure).
struct EngineFailureBanner: View {
    @ObservedObject var controller: OrgController
    let message: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("The engine could not start", systemImage: "exclamationmark.triangle.fill")
                .font(.headline)
                .foregroundStyle(.red)
            Text(message)
                .font(.callout)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                Button("Try again") { controller.launch() }
                    .buttonStyle(.borderedProminent)
                    .disabled(!controller.canLaunch)
                    .accessibilityLabel("Retry launching the engine")
                Text("engine.cli doctor")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                Text("checks every precondition and names the one that failed")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.red.opacity(0.12))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("The engine could not start. \(message)")
    }
}

struct ConsoleView: View {
    @ObservedObject var controller: OrgController
    @ObservedObject private var terminal = TerminalVisibility.shared
    /// The selected panel, **persisted across launches**.
    ///
    /// `@SceneStorage` rather than `@State`: the framework-selection reference names "NavigationSplitView
    /// doesn't persist sidebar width" as a real gap, and the same applies to selection — reopening the
    /// app on the panel you were last using is what makes it feel like a tool rather than a form.
    @SceneStorage("console.selectedPanel") private var storedPanel: String = ConsoleTab.org.rawValue
    @State private var note: String = ""
    @State private var instruction: String = ""
    @State private var asConstraint: Bool = false

    /// The persisted string as a tab, falling back to the first panel when it names something unknown.
    ///
    /// A fallback rather than a crash: a stored value from an older build must not leave the window
    /// blank, and `ConsoleTab` gaining or losing a case is a normal thing to happen.
    private var tabBinding: Binding<ConsoleTab> {
        Binding(
            get: { ConsoleTab(rawValue: storedPanel) ?? .org },
            set: { storedPanel = $0.rawValue })
    }

    private var tab: ConsoleTab { tabBinding.wrappedValue }

    var body: some View {
        NavigationSplitView {
            sidebar
        } detail: {
            detail
        }
        .navigationSplitViewStyle(.balanced)
        .task {
            // A first-run launch attempt, so the app is useful the moment it opens rather than showing
            // an idle shell with a button.
            if controller.canLaunch && !controller.engineState.isLive { controller.launch() }
        }
    }

    /// The sidebar: the panels, each with its question as a subtitle.
    ///
    /// A macOS app navigates with a sidebar plus menu-bar commands, not a tab bar — the HIG skill is
    /// explicit that an iOS-style tab control "confuses users and violates HIG" on the desktop. The
    /// subtitle carries the question, so the list explains itself without a separate caption line.
    private var sidebar: some View {
        List(ConsoleTab.allCases, id: \.self, selection: tabBinding) { candidate in
            Label {
                VStack(alignment: .leading, spacing: 1) {
                    Text(candidate.rawValue)
                    Text(candidate.shortQuestion)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            } icon: {
                Image(systemName: candidate.symbol)
            }
            // A per-panel badge, so the sidebar answers "is anything wrong here?" without opening it.
            // A gate waiting on you is the one thing worth surfacing at this level: it is work the app
            // cannot proceed past without a decision, which is exactly what a badge means.
            .badge(badge(for: candidate))
        }
        .navigationSplitViewColumnWidth(min: 190, ideal: 220, max: 280)
        .listStyle(.sidebar)
        .safeAreaInset(edge: .top, spacing: 0) { sidebarHeader }
    }

    @ViewBuilder
    private var sidebarHeader: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 7) {
                Circle().fill(stateColour).frame(width: 8, height: 8)
                    .accessibilityHidden(true)
                Text(stateWord)
                    .font(.callout.weight(.medium))
                    .accessibilityLabel("Engine: \(stateWord)")
                Spacer()
            }
            Text(controller.workspace["name"]?.stringValue
                 ?? URL(fileURLWithPath: controller.projectPath).lastPathComponent)
                .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            if controller.workspace["attached"]?.boolValue == true {
                Label("attached", systemImage: "folder.badge.checkmark")
                    .font(.caption2).foregroundStyle(.green)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Zero renders no badge, which is what we want for "nothing to report".
    ///
    /// SwiftUI's `.badge(Int)` hides the badge at zero, so `Int` (not `Int?`) is the right type here —
    /// and the count is deliberately meaningful rather than decorative: one gate means one decision.
    private func badge(for tab: ConsoleTab) -> Int {
        switch tab {
        case .progress: return controller.pendingGate != nil ? 1 : 0
        // The Portfolio panel badges when *any* org has work waiting on you — a gate or a block — so
        // a person scanning the sidebar sees that one of their companies needs them without opening
        // each one.
        case .portfolio:
            let waiting = controller.portfolioRows.filter {
                ($0["waiting_host"]?.boolValue == true) || (($0["blocked"]?.intValue ?? 0) > 0)
            }.count
            return waiting
        // The Activity panel badges when work is waiting on you — a gate or a block — because that is
        // exactly what "go here and look" means, and it is the one thing the app cannot resolve alone.
        case .activity:
            let waiting = controller.pendingGate != nil
            let blocked = controller.activity["counts"]?.objectValue?["blocked"]?.intValue ?? 0
            return waiting ? 1 : (blocked > 0 ? 1 : 0)
        // The Flow panel badges on stuck work and breached handoffs: either is a row a person should
        // look at, and neither is something the app can resolve alone — the same test the others use.
        case .flow:
            let stuck = controller.flow["counts"]?.objectValue?["stuck"]?.intValue ?? 0
            let breaches = controller.flowHandoffs.filter {
                let state = $0["state"]?.stringValue ?? ""
                return state == "breached" || state == "rejected"
            }.count
            let total = stuck + breaches
            return total
        // A proposal is work waiting on a decision the app cannot make for you, which is exactly what
        // a badge means. So is a gate, and for the same reason.
        case .improve: return controller.proposals.count
        default: return 0
        }
    }

    private var detail: some View {
        VStack(spacing: 0) {
            if let failure = controller.engineFailure {
                EngineFailureBanner(controller: controller, message: failure)
                Divider()
            }
            RunControls(controller: controller, note: $note,
                        instruction: $instruction, asConstraint: $asConstraint)
            Divider()
            HStack(spacing: 0) {
                panel
                    .frame(minWidth: 480, maxWidth: .infinity)
                if terminal.isVisible {
                    Divider()
                    TerminalPane(controller: controller)
                        .frame(minWidth: 360, idealWidth: 460)
                }
            }
            // A status strip at the bottom, where a macOS app puts one: the facts that must stay visible
            // while the content above changes. Xcode's status bar is the model — always present, one line,
            // never competing with the content.
            Divider()
            StatusBar(controller: controller)
        }
        .navigationTitle(tab.rawValue)
        .navigationSubtitle(tab.question)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { await controller.refresh() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(controller.engineState != .running)
                .help("Refresh the roster, run status and cost")
            }
            ToolbarItem(placement: .automatic) {
                Button {
                    terminal.toggle()
                } label: {
                    Label("Terminal", systemImage: terminal.isVisible
                          ? "rectangle.bottomhalf.inset.filled" : "rectangle.bottomhalf.inset.filled")
                }
                .help("Show or hide the terminal")
            }
        }
    }

    @ViewBuilder
    private var panel: some View {
        switch tab {
        case .portfolio: PortfolioPanel(controller: controller)
        case .org: OrgPanel(controller: controller)
        case .people: PeoplePanel(controller: controller)
        case .providers: ProvidersPanel(controller: controller)
        case .improve: ImprovePanel(controller: controller)
        case .activity: ActivityPanel(controller: controller)
        case .flow: FlowPanel(controller: controller)
        case .progress: ProgressPanel(controller: controller)
        case .economics: EconomicsPanel(controller: controller)
        case .context: ContextPanel(controller: controller)
        case .resources: ResourcesPanel(controller: controller)
        }
    }

    /// The engine state as a word, so it is never conveyed by colour alone.
    private var stateWord: String {
        if controller.engineFailure != nil { return "Engine failed" }
        if controller.goal["live"]?.boolValue == true { return "Working on a goal" }
        switch controller.engineState {
        case .running: return "Engine running"
        case .failed: return "Engine failed"
        case .launching, .pausing, .terminating: return "Working…"
        default: return "Engine idle"
        }
    }

    private var stateColour: Color {
        if controller.engineFailure != nil { return .red }
        switch controller.engineState {
        case .running: return .green
        case .failed: return .red
        case .launching, .pausing, .terminating: return .orange
        default: return .secondary
        }
    }
}

/// The one-line state of the engine and the run, always visible.
struct StatusBar: View {
    @ObservedObject var controller: OrgController

    private var stateColor: Color {
        if controller.engineFailure != nil { return .red }
        switch controller.engineState {
        case .running: return .green
        case .failed: return .red
        case .idle, .finished: return .secondary
        default: return .orange
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            Circle().fill(stateColor).frame(width: 9, height: 9)
                .accessibilityHidden(true)  // the state is spoken below, so the dot is decoration
            // Spelled out as well as coloured: a status that exists only as a hue is unreadable to some.
            Text(controller.engineState.rawValue.capitalized)
                .font(.system(.body, design: .monospaced))
            if let pid = controller.runStatus["pid"]?.intValue {
                Text("pid \(pid)").font(.caption).foregroundStyle(.secondary)
            }
            Divider().frame(height: 14)
            // Whether the agents are in your repository or a managed project is the first thing the
            // console has to say, so it is stated rather than left to be inferred from the path.
            if controller.workspace["attached"]?.boolValue == true {
                Label("attached", systemImage: "folder.badge.checkmark")
                    .font(.caption).foregroundStyle(.green)
                    .help(controller.workspace["path"]?.stringValue ?? controller.projectPath)
                    .accessibilityLabel("Working in an attached folder")
            }
            Text("project: \(URL(fileURLWithPath: controller.projectPath).lastPathComponent)")
                .font(.caption).foregroundStyle(.secondary)
            if controller.goal["live"]?.boolValue == true {
                Divider().frame(height: 14)
                Label("goal: running", systemImage: "target")
                    .font(.caption).foregroundStyle(.green)
                    .accessibilityLabel("A goal is armed and the run will continue")
            } else if let state = controller.goal["state"]?.stringValue, state == "paused",
                      let reason = controller.goal["pause_reason"]?.stringValue {
                Divider().frame(height: 14)
                Label("goal: paused (\(reason))", systemImage: "pause.circle")
                    .font(.caption).foregroundStyle(.orange)
                    .accessibilityLabel("The goal is paused: \(reason)")
            }
            if let phase = controller.runStatus["phase"]?.stringValue {
                Divider().frame(height: 14)
                Text("run: \(phase)").font(.caption)
            }
            Spacer()
            if controller.logs.hasDropped {
                // Shown rather than hidden: a terminal that quietly forgot its oldest lines would make a
                // long run look like it started later than it did.
                Label("\(controller.logs.droppedCount) dropped", systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .accessibilityLabel("\(controller.logs.droppedCount) log lines dropped")
            }
            if let notice = controller.notice {
                Text(notice).font(.caption).foregroundStyle(.orange).lineLimit(1)
                    .accessibilityLabel("Notice: \(notice)")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }
}

/// Launch, approve and reject — the controls that are needed at any moment.
struct RunControls: View {
    @ObservedObject var controller: OrgController
    @Binding var note: String
    @Binding var instruction: String
    @Binding var asConstraint: Bool

    var body: some View {
        VStack(spacing: 6) {
            HStack(spacing: 8) {
                if !controller.engineState.isLive {
                    Button {
                        controller.launch()
                    } label: {
                        Label("Launch", systemImage: "play.circle")
                    }
                    .disabled(!controller.canLaunch)
                    .help("Start the Python engine and begin streaming its events")
                    .accessibilityLabel("Launch the engine")
                } else {
                    Button {
                        controller.stop()
                    } label: {
                        Label("Stop", systemImage: "stop.circle")
                    }
                    .help("Stop the engine, letting it checkpoint first so the run can resume")
                    .accessibilityLabel("Stop the engine")
                }

                TextField("Goal — what should the org build?", text: $controller.goalDraft)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await controller.startRun(goal: controller.goalDraft) } }
                    .accessibilityLabel("Run goal")

                Button {
                    Task { await controller.startRun(goal: controller.goalDraft) }
                } label: {
                    Label("Start run", systemImage: "flag.checkered")
                }
                .disabled(controller.engineState != .running)
                .help("Plan the goal, show the graph, then execute it")
                .accessibilityLabel("Start a run")

                Button {
                    Task { await controller.startRun(goal: controller.goalDraft, dryRun: true) }
                } label: {
                    Label("Plan only", systemImage: "doc.text.magnifyingglass")
                }
                .disabled(controller.engineState != .running)
                .help("Plan and bind the graph without executing it")
                .accessibilityLabel("Plan without executing")

                Divider().frame(height: 16)

                Button {
                    openProjectPicker(controller: controller)
                } label: {
                    Label("Open Project…", systemImage: "folder")
                }
                .disabled(!controller.canLaunch)
                .help("Attach an existing folder: the agents work in your repository, and the "
                      + "engine's state goes in <folder>/.agent_state/")
                .accessibilityLabel("Attach an existing project folder")
            }

            if controller.proposedGraph != nil || controller.pendingGate != nil {
                HStack(spacing: 8) {
                    Image(systemName: "hand.raised.fill").foregroundStyle(.orange)
                        .accessibilityHidden(true)
                    Text(controller.pendingGate != nil ? "Waiting on you at a gate." : "A graph is awaiting approval.")
                        .font(.caption)
                    TextField("Note (recorded, and read by the agents)", text: $note)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("Decision note")
                    Button("Approve") { Task { await controller.approve(note: note) } }
                        .keyboardShortcut("a", modifiers: [.command, .shift])
                        .accessibilityLabel("Approve")
                    Button("Reject") { Task { await controller.reject(note: note) } }
                        .accessibilityLabel("Reject")
                }
            }

            HStack(spacing: 8) {
                TextField("Guide the run, or state a constraint", text: $instruction)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Instruction for the run")
                Toggle("Non-negotiable", isOn: $asConstraint)
                    .toggleStyle(.checkbox)
                    .help("A constraint is preserved verbatim across every compaction and rotation")
                    .accessibilityLabel("Make this a non-negotiable constraint")
                Button("Send") {
                    Task {
                        await controller.instruct(instruction, asConstraint: asConstraint)
                        instruction = ""
                    }
                }
                .disabled(instruction.isEmpty || controller.engineState != .running)
                .accessibilityLabel("Send the instruction")

                Divider().frame(height: 16)

                Button {
                    controller.pause()
                } label: {
                    Image(systemName: "pause.circle")
                }
                .help("Pause at the next node boundary — never mid-generation, which would corrupt state")
                .accessibilityLabel("Pause the run")

                Button {
                    Task { await controller.resume() }
                } label: {
                    Image(systemName: "play.circle")
                }
                .help("Resume from the checkpoint")
                .accessibilityLabel("Resume the run")

                Button {
                    Task { await controller.abort() }
                } label: {
                    Image(systemName: "xmark.octagon")
                }
                .help("Abort the run, keeping its checkpoint")
                .accessibilityLabel("Abort the run")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }
}

/// The terminal: the engine's event stream, live.
struct TerminalPane: View {
    @ObservedObject var controller: OrgController
    @State private var filter: String = ""
    @State private var kind: LogLine.Kind?
    /// Reduce Motion is a system accessibility setting, and the skill's checklist requires checking it
    /// before *all* animations. Here it turns the auto-scroll from an animated glide into an instant
    /// jump: someone who asked for less motion still wants to see the newest line, just not sliding.
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Text("Terminal").font(.headline)
                Spacer()
                Picker("", selection: $kind) {
                    Text("All").tag(LogLine.Kind?.none)
                    Text("Events").tag(LogLine.Kind?.some(.event))
                    Text("Diagnostics").tag(LogLine.Kind?.some(.diagnostic))
                    Text("Problems").tag(LogLine.Kind?.some(.unparsable))
                }
                .pickerStyle(.menu)
                .labelsHidden()
                .frame(width: 120)
                .accessibilityLabel("Filter by line kind")

                TextField("Filter", text: $filter)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 140)
                    .accessibilityLabel("Filter terminal lines")

                Button {
                    controller.logs.clear()
                } label: {
                    Image(systemName: "trash")
                }
                .help("Clear the terminal buffer; the trace file keeps the full history")
                .accessibilityLabel("Clear the terminal")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)

            Divider()

            // A monospaced ScrollView rather than a List: a List re-creates rows on every publish, and
            // the log can publish thousands of lines a minute. Text renders the whole buffer in one pass.
            ScrollViewReader { proxy in
                ScrollView {
                    Text(renderedLines)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)   // an owner must be able to copy an error out
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                        .id("terminal-bottom")
                        .accessibilityLabel("Engine output")
                }
                .onChange(of: controller.logs.lines.count) { _, _ in
                    // Animation only when the system allows motion. `withAnimation(nil)` scrolls
                    // instantly, which is the right behaviour for Reduce Motion — not "no scroll",
                    // which would hide the newest lines from the person who needs them most.
                    withAnimation(reduceMotion ? nil : .linear(duration: 0.1)) {
                        proxy.scrollTo("terminal-bottom", anchor: .bottom)
                    }
                }
            }
        }
    }

    /// The filtered lines as one string, with the kind marked so a diagnostic is distinguishable from an
    /// event without relying on colour.
    private var renderedLines: String {
        let lines = controller.logs.filtered(kind: kind, search: filter.isEmpty ? nil : filter)
        if lines.isEmpty { return "waiting for the engine…" }
        return lines.map { line in
            let mark: String
            switch line.kind {
            case .event: mark = line.isKnownEvent ? " " : "?"
            case .diagnostic: mark = "·"
            case .unparsable: mark = "!"
            case .notice: mark = "»"
            }
            let time = line.timestamp.count >= 19 ? String(line.timestamp.suffix(13).prefix(12)) : ""
            return "\(mark) \(time)  \(line.text)"
        }
        .joined(separator: "\n")
    }
}
