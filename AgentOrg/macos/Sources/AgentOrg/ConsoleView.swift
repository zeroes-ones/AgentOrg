//
//  ConsoleView.swift
//  AgentOrg
//
//  The window: a spine, the selected destination, and the terminal.
//
//  WHAT CHANGED AND WHY
//  --------------------
//  This file used to be a twelve-row sidebar, a two-row run-control bar with a control for everything,
//  and a selected panel. The audit's complaint was that it was "much confusing to use", and the cause
//  was structural rather than cosmetic: the sidebar's rows overlapped (the roster twice, one live run
//  three times, disk state of a run that also had a live view), and there was **no spine** — nothing
//  said "you are here, this needs you, do this next".
//
//  So there are now five destinations (Now / Runs / Org / System / Setup), each section inside them is
//  one of the old panels re-homed rather than deleted, and above all of them sits `SpineView`, which
//  answers those three questions from the engine's own reports and renders a decision control **only
//  where a decision is actually needed**.
//
//  **System is the one row that was not a re-homing.** It answers a question about the machine rather
//  than the work — what an agent may do here — which no earlier panel asked. Before it, the only place
//  a `system:` grant was visible was a checkbox inside the hire form, where granting an agent the
//  ability to run AppleScript read as a field of the hire rather than a decision about the person's
//  own desktop.
//
//  Accessibility is a requirement here rather than a finishing touch, and unchanged from before:
//  every control carries a label, colour is never the only signal, and the terminal is readable by
//  VoiceOver as text.

import SwiftUI
import AppKit
import AgentOrgKit

/// Open a folder picker and attach the chosen folder to the org.
///
/// A free function rather than a view method so both the toolbar button and the File menu use the same
/// panel: two pickers configured differently is how one of them ends up allowing files, or returning a
/// path the engine then refuses.
@MainActor
func openProjectPicker(controller: OrgController, then: (@MainActor () -> Void)? = nil) {
    let panel = NSOpenPanel()
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.allowsMultipleSelection = false
    panel.canCreateDirectories = false
    panel.prompt = "Use this folder"
    panel.message = "Choose the project folder the agents should work in."
    panel.directoryURL = URL(fileURLWithPath: controller.projectPath)

    guard panel.runModal() == .OK, let url = panel.url else { return }
    Task {
        await controller.setProject(url)
        then?()
    }
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
///
/// It also reports the **bounded auto-restart**, because an app that retries silently and an app that
/// has given up look identical otherwise. The count is shown while attempts remain, and the giving-up
/// is stated plainly rather than left as a banner that never changes.
struct EngineFailureBanner: View {
    @ObservedObject var controller: OrgController
    let message: String

    /// What the restart bound is doing right now.
    private var retryState: String {
        // Declined and exhausted read differently on purpose: "cannot be fixed by retrying" tells a
        // person to go and change something, while "used up" would invite them to wait for a last
        // attempt that is not coming.
        if controller.restartDeclined {
            return "A retry cannot fix this, so the console did not retry. Fix the cause, then "
                + "press Try again."
        }
        if controller.gaveUpRestarting {
            return "Automatic retries are used up. Fix the cause, then Try again."
        }
        let remaining = controller.restartAttemptsRemaining
        if remaining == 0 {
            return "Automatic retries are off."
        }
        return "Retrying automatically — \(remaining) attempt(s) left."
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("The engine could not start", systemImage: "exclamationmark.triangle.fill")
                .font(.headline)
                .foregroundStyle(.red)
            Text(message)
                .font(.callout)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Text(retryState)
                .font(.caption)
                .foregroundStyle(controller.gaveUpRestarting ? .red : .secondary)
            HStack(spacing: 10) {
                Button("Try again") { controller.launch() }
                    .buttonStyle(.borderedProminent)
                    .disabled(!controller.canLaunch)
                    .accessibilityLabel("Retry launching the engine")
                // The command is still named for the power user, but it is no longer the *instruction*:
                // the button above is what a person should press, and this is what to run if they want
                // to see every precondition for themselves.
                Text("engine.cli doctor")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .help("Run this in a terminal to see every precondition and the one that failed")
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.red.opacity(0.12))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("The engine could not start. \(message) \(retryState)")
    }
}

// MARK: - The spine

/// The three questions, always answered, always in the same place.
///
/// ```
/// ● Engine running · project my-app · goal: "harden auth"
/// ⚠  A gate is waiting: release — 2 artifacts required, both present
///    [ Approve ]  [ Reject… ]
/// Next: approve the release gate to continue
/// ```
///
/// The order is the order a person asks them: what is going on, what needs me, what do I do about it.
/// The gate row's buttons are rendered **only** when `SpineModel.GateLine.canAct` — which comes from
/// `OrgController.GateDisposition`, the single rule that decides whether the console may forward a
/// decision. That rule deliberately does not re-derive the engine's policy (it cannot see the ledger
/// or the run's `stop_reason`), so this view does not either: it renders the engine's answer.
struct SpineView: View {
    @ObservedObject var controller: OrgController
    let model: SpineModel
    var onOpenSetup: () -> Void

    @State private var decisionNote: String = ""
    @State private var showingNote: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            engineRow
            waitingRow
            if let gate = model.gate {
                gateRow(gate)
            }
            if let next = model.next {
                nextRow(next)
            }
            if let hint = model.setupHint {
                setupRow(hint)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(model.needsAPerson ? Color.orange.opacity(0.10) : Color.secondary.opacity(0.05))
    }

    // MARK: waiting on the engine

    /// What the app is waiting for, and for how long.
    ///
    /// This row exists because the app had two ways to look frozen and no way to *say* it was working.
    /// A launch showed "Working…" with no stage and no elapsed time, and a command that never came back
    /// showed nothing at all for up to ten minutes — so "still starting" and "hung" were the same
    /// picture, and the only recourse was to open the terminal and read it.
    ///
    /// Three deliberate choices, all following `StatusTone`:
    ///
    /// * **A glyph and a word, never a bare spinner.** `.attention`'s symbol plus the sentence carries
    ///   the state for anyone who cannot see the colour, and the sentence names the *stage* — the
    ///   engine's own last diagnostic line where it has said one — rather than a generic "loading".
    /// * **The elapsed time is always shown.** It is the one number that separates slow from wedged, and
    ///   omitting it was the original complaint.
    /// * **No percentage.** The engine reports no progress, so a bar would be an invented figure. The
    ///   honest presentation of an unknown duration is "how long so far", not a confident fraction.
    @ViewBuilder
    private var waitingRow: some View {
        if let summary = controller.waitingSummary {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Label(summary, systemImage: StatusTone.attention.symbol)
                        .font(.callout.weight(.medium))
                        .foregroundStyle(StatusTone.attention.colour)
                        .lineLimit(2)
                        .textSelection(.enabled)
                    Spacer()
                }
                // The advice appears only past the threshold, and it says what to *do*. A launch that
                // has crossed it is almost always a macOS permission prompt for the Documents folder
                // — the failure the plist's own note describes — and naming that is the difference
                // between a person waiting and a person acting.
                if let advice = controller.waitingAdvice {
                    Text(advice)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel(
                [summary, controller.waitingAdvice].compactMap { $0 }.joined(separator: " "))
        }
    }

    // MARK: state, project and goal

    private var engineRow: some View {
        HStack(spacing: 8) {
            Label(model.stateWord, systemImage: model.stateTone.symbol)
                .foregroundStyle(model.stateTone.colour)
                .font(.callout.weight(.medium))
                .accessibilityLabel("Engine: \(model.stateWord)")
            if let pid = controller.runStatus["pid"]?.intValue {
                Text("pid \(pid)").font(.caption2.monospaced()).foregroundStyle(.secondary)
            }
            Divider().frame(height: 13)
            // The project, said in words: a person has to be able to tell "the agents are editing my
            // repository" from "the engine made its own folder", and which one it is decides how much
            // the next run matters.
            Label(model.projectName, systemImage: controller.workspace["attached"]?.boolValue == true
                  ? "folder.badge.checkmark" : "shippingbox")
                .font(.callout)
                .help(model.projectDetail)
                .accessibilityLabel("Project \(model.projectName), \(model.projectDetail)")
            if let objective = model.goalObjective {
                Divider().frame(height: 13)
                Label(objective, systemImage: "target")
                    .font(.callout)
                    .lineLimit(1)
                    .foregroundStyle(model.goalTone == .ok ? .primary : model.goalTone.colour)
                    .help(objective)
                    .accessibilityLabel("Goal: \(objective). \(model.goalDetail ?? "")")
                if let detail = model.goalDetail {
                    Text(detail).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer()
            // The one engine control, in the one place, always visible. It used to appear in six
            // different spots — the run bar, the toolbar, Settings, the menu bar, the Run menu and a
            // panel — which is how a person ends up unsure whether they are looking at the same switch.
            if controller.engineState.isLive {
                Button("Stop the engine") { controller.stop() }
                    .controlSize(.small)
                    .help("Stop the engine, letting it checkpoint first so the run can resume")
                    .accessibilityLabel("Stop the engine")
            } else {
                Button("Start the engine") { controller.launch() }
                    .controlSize(.small)
                    .disabled(!controller.canLaunch)
                    .help("Start the engine and begin streaming its events")
                    .accessibilityLabel("Start the engine")
            }
        }
        .accessibilityElement(children: .contain)
    }

    // MARK: the gate — buttons only where a decision is needed

    @ViewBuilder
    private func gateRow(_ gate: SpineModel.GateLine) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: gate.canAct ? "hand.raised.fill" : "hourglass")
                    .foregroundStyle(gate.canAct ? .orange : .secondary)
                    .accessibilityHidden(true)
                Text("A gate is waiting: \(gate.reason)")
                    .font(.callout.weight(.medium))
                    .textSelection(.enabled)
                if let evidence = gate.evidence {
                    Text(evidence).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("A gate is waiting: \(gate.reason). \(gate.why)")

            // **The reason, always — and it was the half a person could not see.** This sentence is
            // the console's single gate rule (`OrgController.GateDisposition`) as the model carries it,
            // and it used to be rendered only when the buttons were *not*. So a gate the person had to
            // answer showed Approve and Reject and no statement of why it was theirs, while the
            // engine's own refusal is exactly what this sentence holds — "a safety control fired",
            // "the gate's evidence is not present", "the goal is supervised". A gate that is waiting
            // is the one place the reason is worth the line.
            Text(gate.why)
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .help(gate.why)
                // The row above already speaks this sentence as part of its own label, so the visible
                // copy is hidden from VoiceOver rather than read a second time.
                .accessibilityHidden(true)

            if gate.canAct {
                HStack(spacing: 8) {
                    Button("Approve") {
                        Task { await controller.approve(note: decisionNote) }
                    }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut("a", modifiers: [.command, .shift])
                    .accessibilityLabel("Approve this gate")

                    Button("Reject…") { showingNote.toggle() }
                        .accessibilityLabel("Reject this gate, with a note")

                    if showingNote {
                        TextField("Why (recorded, and read by the agents)", text: $decisionNote)
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: 360)
                            .accessibilityLabel("Reason for rejecting")
                        Button("Reject") {
                            Task { await controller.reject(note: decisionNote) }
                        }
                        .accessibilityLabel("Reject this gate")
                    }
                    Spacer()
                    if let missing = gate.missing.isEmpty ? nil : gate.missing {
                        Label("missing \(missing.joined(separator: ", "))",
                              systemImage: "exclamationmark.circle")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
            }
        }
        .padding(.top, 2)
    }

    // MARK: the next action

    @ViewBuilder
    private func nextRow(_ next: SpineModel.NextLine) -> some View {
        HStack(spacing: 8) {
            // The engine's own sentence is one accessibility element, so VoiceOver reads a phrase rather
            // than three fragments — and the buttons beside it stay separately reachable, which a
            // `.combine` on the whole row would have taken away.
            HStack(spacing: 8) {
                Label("Next", systemImage: "arrow.forward.circle.fill")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(next.kind == "decide" ? .orange : .blue)
                Text(next.label).font(.callout).lineLimit(1)
                if !next.detail.isEmpty {
                    Text(next.detail).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Next: \(next.label). \(next.detail)")
            Spacer()
            // A button only for what the app can honestly do. Everything else is shown as the engine's
            // own command, because a button that silently does nothing is worse than a line telling you
            // what to run.
            if next.canPerform(gateIsWaitingForHuman: controller.gateIsWaitingForHuman) {
                nextButtons(next)
            } else if !next.command.isEmpty {
                Text(next.command)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    /// What the app offers for a next action it can actually perform.
    ///
    /// `decide` has no button here on purpose: the gate row above already carries Approve/Reject with
    /// the evidence beside them, and two controls for one decision is the duplication this rewrite
    /// removed.
    @ViewBuilder
    private func nextButtons(_ next: SpineModel.NextLine) -> some View {
        switch next.kind {
        case "resume":
            Button("Resume") { Task { await controller.resumeGoal() } }
                .controlSize(.small)
                .buttonStyle(.borderedProminent)
                .accessibilityLabel("Resume the goal")
        case "hire":
            // The app has a hire form, but it is on Org — so this is a door to the destination that
            // owns hiring rather than a second copy of the form in the spine.
            Button("Hire…") { DestinationRouter.shared.select(.org) }
                .controlSize(.small)
                .buttonStyle(.borderedProminent)
                .help("Open Org, where a hire is made")
                .accessibilityLabel("Open the Org destination to hire for the unstaffed capability")
        case "start":
            // The same call the composer's Start button makes, so "start a run" still has one
            // implementation; the goal is the draft when there is one and otherwise the durable goal
            // the engine's own `start` action is about.
            Button("Start the run") {
                Task { await controller.startRun(goal: startGoal, dryRun: false) }
            }
            .controlSize(.small)
            .buttonStyle(.borderedProminent)
            .help("Start a run for the goal the engine is holding")
            .accessibilityLabel("Start a run for this goal")
        case "investigate":
            // A stopped run is read on Runs: the checkpoint's nodes, their verdicts, and every handoff.
            Button("Open Runs") { DestinationRouter.shared.select(.runs) }
                .controlSize(.small)
                .accessibilityLabel("Open the Runs destination, which shows why the run stopped")
        default:
            EmptyView()
        }
    }

    /// The goal a run started from the spine would use.
    ///
    /// The composer's own draft when the person has typed one, and otherwise the durable goal the engine
    /// already holds — which is the goal its `start` action is *about*, and the only string available
    /// when the spine is being read from a destination that has no composer on it.
    private var startGoal: String? {
        if !controller.goalDraft.isEmpty { return controller.goalDraft }
        let objective = controller.goal["objective"]?.stringValue ?? ""
        return objective.isEmpty ? nil : objective
    }

    // MARK: the first-run hint

    @ViewBuilder
    private func setupRow(_ hint: String) -> some View {
        HStack(spacing: 8) {
            Label(hint, systemImage: "wand.and.stars")
                .font(.callout)
                .foregroundStyle(.orange)
            Spacer()
            Button("Open Setup", action: onOpenSetup)
                .controlSize(.small)
                .buttonStyle(.borderedProminent)
                .accessibilityLabel("Open Setup to finish configuring")
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Setup needed: \(hint)")
    }
}

// MARK: - The window

struct ConsoleView: View {
    @ObservedObject var controller: OrgController
    @ObservedObject private var terminal = TerminalVisibility.shared
    /// Set by a menu command from outside the window (⌘, opens Setup), which cannot reach scene
    /// storage. Mirrored into it below so a selection made either way persists.
    @ObservedObject private var router = DestinationRouter.shared
    /// The selected destination, **persisted across launches**.
    ///
    /// `@SceneStorage` rather than `@State`: reopening the app where you left it is what makes it feel
    /// like a tool rather than a form. A value written by the previous build named one of twelve
    /// panels, and `Destination.migrated(fromStored:)` re-homes it instead of discarding it.
    @SceneStorage("console.destination") private var storedDestination: String = Destination.now.rawValue

    private var destination: Destination {
        Destination.migrated(fromStored: storedDestination)
    }

    private var destinationBinding: Binding<Destination> {
        Binding(get: { destination }, set: { storedDestination = $0.rawValue })
    }

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
            // One round trip wide, not three: the providers, the model catalog and the roster share no
            // data, so awaiting them in turn made opening the window cost their sum.
            await controller.loadWindow()
        }
        // A destination asked for from outside the window — the ⌘, command, or the spine's Open Setup
        // button. Consumed once so a later manual navigation is not undone on the next redraw.
        .onChange(of: router.requested) { _, requested in
            guard let requested else { return }
            storedDestination = requested.rawValue
            router.requested = nil
        }
    }

    /// A four-row sidebar, with the engine's state at the top and nothing else competing with it.
    ///
    /// A macOS app navigates with a sidebar plus menu-bar commands, not a tab bar — the HIG is explicit
    /// that an iOS-style tab control confuses users on the desktop. Each row's subtitle is the *short*
    /// form of its question, and the badge appears only when something on that destination needs a
    /// person, because a badge that is always lit is a badge nobody reads.
    private var sidebar: some View {
        List(Destination.allCases, id: \.self, selection: destinationBinding) { candidate in
            Label {
                VStack(alignment: .leading, spacing: 1) {
                    Text(candidate.rawValue)
                    Text(shortQuestion(candidate))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            } icon: {
                Image(systemName: candidate.symbol)
            }
            .badge(badge(for: candidate))
            .accessibilityLabel("\(candidate.rawValue): \(candidate.question)")
        }
        .navigationSplitViewColumnWidth(min: 190, ideal: 210, max: 280)
        .listStyle(.sidebar)
        .safeAreaInset(edge: .top, spacing: 0) { sidebarHeader }
    }

    /// The short gist of each destination's question, for a ~20-character row.
    ///
    /// Deliberately not a truncated sentence: an ellipsis in the middle of a question is worse than a
    /// shorter question, and these are the words a person would use themselves.
    private func shortQuestion(_ destination: Destination) -> String {
        switch destination {
        case .now:
            // **The one row whose subtitle can name what is waiting, and the only place that can.**
            // Now has two different decisions behind one badge — a gate the engine left to a person,
            // and a graph the engine proposed — and "1" cannot say which; naming it in the row is
            // cheaper than a second badge and answers "what needs me" before the pane is open. With
            // nothing waiting the row describes the destination, like every other row here.
            if controller.gateIsWaitingForHuman { return "a gate needs you" }
            if controller.proposedGraph != nil { return "a plan needs approval" }
            return "live · next step"
        case .runs: return "past runs · disk"
        case .org: return "people · hiring"
        // Describes the destination, like every row above it — it used to read "6/12 held", a bare
        // ratio with no subject, which a person cannot act on and which sat in the navigation on every
        // screen. The owner asked three times what "6/12" meant, which is the test this failed: a
        // number in a menu bar has to say what it counts or it is noise. The count still exists, in
        // the pane that can explain it ("held by an agent", with the two lists and the roster route).
        //
        // What earns a place here instead is the state that *asks* for something: full access lifts
        // the allowlists and the per-action approval, so it is the one fact about this pane worth
        // carrying into the navigation. It is also what the pane's own next step tells you to reduce.
        case .system:
            if !controller.systemEnabled { return "off · nothing may act" }
            if controller.systemFullAccess { return "full access on · who may act" }
            return "machine access · who may act"
        case .setup: return controller.setupGate == .ready ? "model · project" : "finish setup"
        }
    }

    /// Zero renders no badge, which is what we want for "nothing to report".
    private func badge(for destination: Destination) -> Int {
        switch destination {
        // One gate means one decision, and that is the only thing on Now worth badging: everything
        // else on the destination is information a person can read at leisure.
        //
        // **A proposed graph is the second decision, and it was badging nothing.** `manifest.proposed`
        // sets `proposedGraph` and the engine parks the run at `awaiting_approval` until the graph it
        // drew is approved — a run that cannot proceed without a person, which is exactly what a badge
        // means here. Two decisions waiting is two, so the count adds rather than masking the second.
        case .now:
            return (controller.gateIsWaitingForHuman ? 1 : 0)
                + (controller.proposedGraph != nil ? 1 : 0)
        // A proposal is work waiting on a decision the app cannot make for you — the same test.
        case .runs: return controller.proposals.count
        // The Org row carries nothing: a roster needs no attention, and the old app badged panels
        // that were merely *interesting*.
        case .org: return 0
        // Nor does System. It describes permissions rather than asking for a decision — a badge here
        // would light permanently on any machine where an agent holds nothing, which is most of them.
        case .system: return 0
        case .setup: return controller.setupGate == .ready ? 0 : 1
        }
    }

    private var sidebarHeader: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 7) {
                Label(controller.spine.stateWord, systemImage: controller.spine.stateTone.symbol)
                    .font(.callout.weight(.medium))
                    .foregroundStyle(controller.spine.stateTone.colour)
                    .accessibilityLabel("Engine: \(controller.spine.stateWord)")
                Spacer()
                if controller.logs.hasDropped {
                    // Shown rather than hidden: a terminal that quietly forgot its oldest lines would
                    // make a long run look like it started later than it did.
                    Label("\(controller.logs.droppedCount)", systemImage: "exclamationmark.triangle")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                        .accessibilityLabel("\(controller.logs.droppedCount) log lines dropped")
                }
            }
            Text(controller.spine.projectName)
                .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var detail: some View {
        VStack(spacing: 0) {
            // The spine, above every destination and never scrolled away.
            SpineView(controller: controller, model: controller.spine) {
                destinationBinding.wrappedValue = .setup
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Status")

            Divider()

            HStack(spacing: 0) {
                pane
                    .frame(minWidth: 480, maxWidth: .infinity)
                if terminal.isVisible {
                    Divider()
                    TerminalPane(logs: controller.logs,
                                 onClearAll: {
                                     controller.logs.clear()
                                     // The engine's stderr lines are a *second* buffer, on the
                                     // controller, and clearing only `LogStore` left up to 500 of them
                                     // on the Now pane's diagnostics block with no way to remove them
                                     // short of quitting.
                                     controller.clearEngineDiagnostics()
                                 },
                                 onClearKind: { controller.logs.clear(kind: $0) },
                                 onClearDiagnostics: { controller.clearEngineDiagnostics() })
                        .frame(minWidth: 360, idealWidth: 460)
                }
            }
            // A status strip at the bottom, where a macOS app puts one: the facts that must stay
            // visible while the content above changes, and nothing that is already in the spine.
            Divider()
            StatusBar(controller: controller)
        }
        // The failure banner is an **overlay, not a row in the stack**.
        //
        // As a stack row it moved the spine *and* the whole pane down by its own height while it was
        // shown — and it is shown on a cadence: the bounded auto-restart calls `launch()` every 4 s,
        // and `launch()` clears `engineFailure` at its top, so a failing engine cycles banner →
        // cleared → banner and the window jumped each time. Overlaid, the failure is announced with
        // nothing underneath it moving. It is opaque and shadowed because it now sits *over* the
        // spine rather than above it, and a translucent failure over live text reads as a rendering
        // bug.
        .overlay(alignment: .top) {
            if let failure = controller.engineFailure {
                EngineFailureBanner(controller: controller, message: failure)
                    .background(Color(nsColor: .windowBackgroundColor))
                    .shadow(radius: 6, y: 2)
            }
        }
        .navigationTitle(destination.rawValue)
        .navigationSubtitle(subtitle)
        .toolbar { toolbar }
    }

    /// The destination's question, with the org it is about whenever the window is acting on one of
    /// several.
    ///
    /// Deliberately silent on a single-org setup: naming the only org is noise, and the project name is
    /// already in the sidebar header and the spine. It is the *second* org that makes the question
    /// ambiguous, and that is exactly when this appears — so a person looking at the window title of a
    /// window that is describing the wrong org has one place to see it.
    private var subtitle: String {
        guard controller.portfolioOrgs.count > 1, !controller.activeOrgName.isEmpty else {
            return destination.question
        }
        return "\(controller.activeOrgName) · \(destination.question)"
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
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
                // Two different glyphs, which is what makes the control say what it will *do*. Both
                // branches used to name the same symbol, so the icon never changed and the button
                // gave no indication whether the terminal was showing.
                Label(terminal.isVisible ? "Hide Terminal" : "Show Terminal",
                      systemImage: terminal.isVisible
                          // `rectangle.bottomhalf.inset.filled.and.rectangle.inset.filled` — the name this
                          // used to carry — is not in the system set, so this branch rendered no glyph and
                          // SwiftUI logged "No symbol named …" four times per two-second poll for as long
                          // as the app ran. Checked against `NSImage(systemSymbolName:)`, not assumed.
                          ? "rectangle.bottomhalf.inset.filled"
                          : "rectangle.inset.filled")
            }
            .help(terminal.isVisible ? "Hide the terminal" : "Show the terminal")
            .accessibilityLabel(terminal.isVisible ? "Hide the terminal" : "Show the terminal")
        }
    }

    @ViewBuilder
    private var pane: some View {
        switch destination {
        case .now: NowPane(controller: controller)
        case .runs: RunsPane(controller: controller)
        case .org: OrgPane(controller: controller)
        case .system: SystemPane(controller: controller)
        case .setup: SetupPane(controller: controller)
        }
    }
}

/// The one-line state at the bottom of the window.
///
/// Deliberately not a second spine: everything a person must *act* on is above, and this is only the
/// machinery — the pid, the phase, the log's health and the last notice. The old status bar repeated
/// the engine state, the project and the goal, all three of which the spine now says once.
struct StatusBar: View {
    @ObservedObject var controller: OrgController

    var body: some View {
        HStack(spacing: 10) {
            Text(controller.engineState.rawValue)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
            if let phase = controller.runStatus["phase"]?.stringValue {
                Divider().frame(height: 12)
                Text("run: \(phase)").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if let last = controller.lastEventAt {
                // Liveness, which is what tells "quiet" from "wedged" without opening the terminal.
                Text(relative(last)).font(.caption2).foregroundStyle(.secondary)
                    .accessibilityLabel("Last engine event \(relative(last))")
            }
            if let chip = notificationChip {
                // **Why a banner did or did not arrive.** The app asks for notification permission
                // only when it has something to say, so a person who has never seen one cannot tell
                // "the app is not allowed" from "there has been no news" — and two of the three
                // silent states have something to do about them. Shown when the last attempt was not
                // delivered, and *also* when this process cannot post at all, which is known before
                // any attempt: a `swift run` build that notifies nobody must not be silent about it.
                // The full sentence and the advice are the tooltip and the spoken label, because a
                // strip that wrapped would move the window's own content.
                Divider().frame(height: 12)
                Label(chip.text, systemImage: StatusTone.attention.symbol)
                    .font(.caption2)
                    .foregroundStyle(StatusTone.attention.colour)
                    .lineLimit(1)
                    .help(chip.detail)
                    .accessibilityLabel("Notifications: \(chip.detail)")
            }
            if let notice = controller.notice {
                Text(notice).font(.caption).foregroundStyle(.orange).lineLimit(1)
                    .accessibilityLabel("Notice: \(notice)")
                    .help(notice)
                // A notice is rendered bare before this: the strip could say something needed
                // attention and offered no way to acknowledge it, so a person either waited out the
                // controller's own lapse (`defaultNoticeLifetime`, 20 s) or watched it sit there while
                // unrelated work continued. Dismissing is the acknowledgement, and it is the same call
                // the controller already makes when the event a notice announced is resolved — for
                // "the engine cannot be started", 20 seconds is a long time to keep reading it.
                Button {
                    controller.dismissNotice()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Dismiss this notice now — it also lapses on its own")
                .accessibilityLabel("Dismiss the notice")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 5)
    }

    private func relative(_ date: Date) -> String {
        let seconds = Int(Date().timeIntervalSince(date))
        return seconds < 2 ? "just now" : "\(seconds)s ago"
    }

    /// The terse form of a notification outcome, for a strip with room for a phrase and not a
    /// sentence. The full sentence and the advice behind it are the tooltip and the spoken label.
    private func shortNotification(_ outcome: NotificationOutcome) -> String {
        switch outcome.kind {
        case .delivered: return "notifications on"
        case .denied: return "notifications off"
        case .unavailable: return "cannot notify from this build"
        case .failed: return "no banner could be sent"
        }
    }

    /// The notification state worth a chip here, if any.
    ///
    /// Two things earn one, and the second is why this is not just a read of the last outcome: **a
    /// process that can never post a banner is known before any attempt has been made.** A `swift run`
    /// build has no application bundle, so it notifies nobody — and a state that shows nothing until
    /// something is attempted is exactly the silence this is meant to end.
    private var notificationChip: (text: String, detail: String)? {
        if let outcome = controller.notificationOutcome {
            guard outcome.needsAttention else { return nil }
            return (shortNotification(outcome), outcome.advice ?? outcome.sentence)
        }
        guard !controller.notificationsAvailable else { return nil }
        let unable = NotificationOutcome.unavailable()
        return (shortNotification(unable), unable.advice ?? unable.sentence)
    }
}

// MARK: - The terminal

/// The engine's event stream, live.
///
/// Kept for power users, unchanged in substance: monospaced, filterable, kind-marked so a diagnostic
/// is distinguishable from an event without relying on colour.
///
/// Two things about *how* it renders, because both were the difference between a readable terminal and
/// a flickering one:
///
/// * **It observes the `LogStore`, not the controller.** As `@ObservedObject controller` it re-rendered
///   on the 2 s status poll as well as on a log publish — and each of those rebuilds re-joined the whole
///   buffer into one string.
/// * **The string is cached, not computed in `body`.** It is rebuilt when the buffer's `revision` or the
///   filter changes, so a re-layout, a scroll or an unrelated publish costs nothing.
///
/// The auto-scroll has no animation, on purpose. It used to be `scrollTo` inside a 100 ms
/// `withAnimation` on every change of the line count — up to 60 overlapping animations a second while
/// lines streamed — and *that*, not the text, is what "always flashing" was. Anchoring the scroll view
/// to its bottom is the platform's own answer, needs no animation to unwind, and is what Reduce Motion
/// would have asked for anyway, so there is no separate path for that setting.
struct TerminalPane: View {
    @ObservedObject var logs: LogStore
    /// Clearing reaches three buffers: this one, the engine's stderr diagnostics on the controller, and
    /// one kind of line at a time. Passed as actions rather than as the controller so this view stays
    /// free of the controller's two-second publish.
    var onClearAll: () -> Void
    var onClearKind: (LogLine.Kind) -> Void
    var onClearDiagnostics: () -> Void

    @State private var filter: String = ""
    @State private var kind: LogLine.Kind?
    /// What the terminal is showing, rebuilt only when `renderKey` changes.
    @State private var rendered: String = "waiting for the engine…"

    /// The inputs the rendered text is built from.
    ///
    /// `revision` rather than `lines.count`: at capacity the buffer stops growing, so an append that
    /// trims its front leaves the count unchanged while every line has moved by one.
    private var renderKey: String { "\(logs.revision)|\(kind?.rawValue ?? "-")|\(filter)" }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Text("Terminal").font(.headline)
                Spacer()
                Picker("", selection: $kind) {
                    Text("All").tag(LogLine.Kind?.none)
                    Text("Events").tag(LogLine.Kind?.some(.event))
                    Text("Diagnostics").tag(LogLine.Kind?.some(.diagnostic))
                    // Notices are the app's own actions, interleaved with the engine's lines and marked
                    // `»`. They could be filtered (`LogStore.filtered`) and were drawn differently, but
                    // not *selected* — so the one kind of line this app writes was the one kind nobody
                    // could isolate.
                    Text("Notices").tag(LogLine.Kind?.some(.notice))
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

                // A menu rather than one all-or-nothing button: this pane holds three different things
                // — the engine's events, the app's notices and the engine's stderr — and a single
                // control that could take only all of them made clearing a diagnostic flood cost the
                // events too. The kind picker already says what "this kind" means, so the clear agrees
                // with it.
                Menu {
                    Button("Clear all lines and diagnostics", action: onClearAll)
                    if let kind {
                        Button("Clear the \(kindWord(kind)) lines only") { onClearKind(kind) }
                    }
                    Button("Clear the engine's diagnostics only", action: onClearDiagnostics)
                } label: {
                    Image(systemName: "trash")
                }
                .fixedSize()
                // The old tooltip promised the trace file kept "the full history". It does not: the
                // engine's `EventBus` writes `trace.jsonl`, which holds engine *events* — not the
                // notices this app adds, and not the engine's stderr, which arrives through the
                // process bridge and is not in the trace at all. Corrected rather than left as a
                // reassurance that would send someone looking for lines that were never written.
                .help("Clear what this terminal is showing. The engine's trace file keeps its own "
                      + "events — not this app's notices, and not the engine's stderr lines.")
                .accessibilityLabel("Clear the terminal")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)

            Divider()

            // A monospaced ScrollView rather than a List: a List re-creates rows on every publish, and
            // the log can publish thousands of lines a minute. Text renders the whole buffer in one pass.
            ScrollView {
                Text(rendered)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)   // an owner must be able to copy an error out
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .accessibilityLabel("Engine output")
            }
            // New lines arrive at the bottom, so the view is anchored there rather than animated to it.
            .defaultScrollAnchor(.bottom)
        }
        // Built here, not in `body`: one join of up to 20,000 lines, and only when its inputs changed.
        .onChange(of: renderKey, initial: true) { _, _ in rendered = render() }
    }

    /// The word the Clear menu uses for a kind, so the button says what it will remove.
    private func kindWord(_ kind: LogLine.Kind) -> String {
        switch kind {
        case .event: return "event"
        case .diagnostic: return "diagnostic"
        case .unparsable: return "problem"
        case .notice: return "notice"
        }
    }

    /// The filtered lines as one string, with the kind marked so a diagnostic is distinguishable from an
    /// event without relying on colour.
    private func render() -> String {
        let lines = logs.filtered(kind: kind, search: filter.isEmpty ? nil : filter)
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
