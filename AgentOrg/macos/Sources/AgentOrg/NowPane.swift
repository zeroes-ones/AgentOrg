//
//  NowPane.swift
//  AgentOrg
//
//  The Now destination: what is happening, what needs you, and what is it costing.
//
//  WHY THIS IS ONE SCREEN AND NOT FIVE
//  -----------------------------------
//  Activity, Flow, Work, Cost, Context and Resources were six sidebar rows. Three of them were
//  renderings of one live run, and the other three were figures nobody has to act on. Splitting them
//  meant a person had to *remember* which row held the thing they wanted, and the audit's evidence for
//  "much confusing to use" was exactly that.
//
//  They are now three sections in one scrolling destination, in the order a person asks:
//
//  1. **What is happening** — the run's own story, its transport controls, the one place a goal is
//     composed, and the card that manages the goal once one exists (pause, resume, clear, autonomy).
//     Always open.
//  2. **Who is on what** — the board: one row per unit of work, its owner, and what crossed between
//     them. Collapsed until opened, because it is a detail view of section 1.
//  3. **Cost and capacity** — spend, cache, context fullness, the log's health and the engine's paths.
//     Collapsed, because a metric wall is not what a person opens the app to see.
//
//  Progressive disclosure is the fix for the audit's density finding: the panels that need no
//  interaction no longer occupy full screens, and the one thing that needs a decision is at the top of
//  the spine above this. A collapsed section still says what it is for, so nothing is hidden.

import SwiftUI
import AgentOrgKit

// MARK: - Shared pieces

/// A labelled figure. Used everywhere so the console reads consistently.
struct Metric: View {
    let label: String
    let value: String
    var detail: String?
    var tone: Color = .primary

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.system(.title3, design: .rounded)).foregroundStyle(tone)
            if let detail {
                Text(detail).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        // Spoken as one unit: a screen reader reading "2", "healthy", "of 8" as three fragments is worse
        // than one sentence.
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label): \(value)\(detail.map { ", \($0)" } ?? "")")
    }
}

/// A `key: value` line, monospaced so paths and ids line up.
struct KeyValueRow: View {
    let key: String
    let value: String
    var tone: Color = .secondary

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Text(key).font(.caption).foregroundStyle(.secondary).frame(width: 90, alignment: .leading)
            Text(value).font(.system(.caption, design: .monospaced)).foregroundStyle(tone)
                .textSelection(.enabled)
            Spacer()
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(key): \(value)")
    }
}

/// The engine's state tokens, said in words a person reads.
///
/// The engine's own vocabulary is terse and machine-shaped (`FULFILLED`, `needs_review`,
/// `awaiting_owner`) because it is written for logs and for other code. The console was printing those
/// tokens straight into a person's column, so a row read `FULFILLED` — the engine's word, shown as
/// though it were the product's. This is the one place the mapping lives, shaped like `Band.meaning`
/// (AgentOrgKit/Usage.swift): a short phrase per state, and **the token itself** for anything this build
/// does not recognise. A default that invented a word would be the confident-wrong-output failure the
/// rest of this app is built to avoid, and the detail views still show the raw state verbatim — so
/// nothing is lost, only translated.
enum EngineWord {

    /// A handoff state, from the handoff record (`RunStateBrowser`) or the board (`engine/flow.py`).
    ///
    /// Both spellings arrive: the checkpoint writes the state upper-case and the flow fold lower-cases
    /// it, so this compares case-insensitively rather than in two places.
    static func handoff(_ state: String) -> String {
        switch state.uppercased() {
        case "FULFILLED": return "delivered"
        case "VERIFIED": return "checked"
        case "ACCEPTED": return "accepted"
        case "IN_PROGRESS": return "in progress"
        case "REJECTED": return "refused"
        case "BREACHED": return "broke its terms"
        case "ESCALATED": return "raised to you"
        default: return state
        }
    }

    /// A node's status on the board, keyed by the status the runner writes (`engine/flow.py`).
    ///
    /// Only the tokens that are *not* already words are listed: `blocked`, `failed` and `skipped` are
    /// what a person would say, and a mapping that re-said them would be churn with a chance of drift.
    static func board(_ status: String) -> String {
        switch status {
        case "done", "pass": return "finished"
        case "running", "working": return "working now"
        case "pending": return "not started"
        case "needs_review": return "needs a look"
        case "awaiting_owner": return "waiting on you"
        default: return status
        }
    }

    /// Why a step stopped, when the engine recorded the why as a verdict and nothing else.
    ///
    /// A board row's reason is `blocked_by`, which `engine/flow.py:316` (`why_stopped`) folds from the
    /// node record's `summary`, then the node's own `log` entry, then the engine's gloss for the token.
    /// The two tokens that fold knows are glossed **from the engine's own table**, which travels in the
    /// payload the board was built from (`engine/flow.py`'s `stop_words` → `StopWords`): they are not
    /// written here, so `guardrail-blocked` cannot be described one way in the engine and another on
    /// this screen. The rest are this build's, for a stop the engine has no sentence for.
    ///
    /// This is the degradation path, not the main one: a payload from a build whose `blocked_by` is
    /// empty — a workspace written before the fold, or a node whose log entry is not one of the causes
    /// the engine reads — still gets a sentence instead of a bare token. `guardrail-blocked` is the
    /// token a person reported, and the honest reading of it is the engine's own: the step *finished
    /// its work* and what it handed on was refused at the edge. It is a contract failure at the
    /// boundary, not a crash and not a permission denial.
    ///
    /// The vocabulary is the *stopped node's* own, read from the two producers that write one: the
    /// runner (the library's `scripts/workflow-runner.py`: `guardrail-blocked` at `_apply_guardrail`,
    /// `contract-violation` at `_apply_contract`) and this repository's executor (`awaiting_owner` and
    /// `missing_prerequisites`, `engine/executor.py:566` and `:581`). The engine glosses the first pair
    /// and not the second pair, which is why only the second is spelled out below. An empty string
    /// means "no gloss for this token" — the caller keeps the token rather than printing an invented
    /// sentence, which is the rule the rest of this enum follows.
    static func stop(_ verdict: String, words: StopWords) -> String {
        let engine = words.gloss(verdict)
        if !engine.isEmpty { return engine }
        // Below here the engine has no sentence of its own, so these are this build's — said as what
        // the state *is*, and kept short enough to read in a row.
        switch verdict {
        case "awaiting_owner":
            return "a decision point: this step is waiting for you"
        case "missing_prerequisites":
            return "the step needed an artifact that is not there yet"
        default:
            return ""
        }
    }

    /// A subagent's status, keyed by the states `engine/subagents.py` records.
    static func subagent(_ status: String) -> String {
        switch status {
        case "running": return "working now"
        case "done": return "finished"
        case "needs_review": return "needs a look"
        default: return status
        }
    }

    /// A timeline entry's `kind`, keyed by the kinds `engine/activity.py` emits.
    ///
    /// The column is one word wide, so these are the *shortest* honest phrase rather than a sentence —
    /// "a decision" says what a `gate` entry is without wrapping the row.
    static func timeline(_ kind: String) -> String {
        switch kind {
        case "goal": return "the goal"
        case "plan": return "the plan"
        case "run": return "the run"
        case "node": return "a step"
        case "gate": return "a decision"
        case "swarm": return "parallel work"
        case "subagent": return "a subagent"
        case "proposal": return "a self-check"
        case "budget": return "cost"
        default: return kind
        }
    }
}

/// What the console says when the engine is not running.
///
/// `ContentUnavailableView` rather than a hand-built VStack: it is the system's own empty-state view, so
/// spacing, typography, the symbol treatment and the VoiceOver grouping all match the rest of macOS for
/// free. A hand-rolled version looks *almost* right, which is worse than obviously custom.
struct EngineNotRunningView: View {
    @ObservedObject var controller: OrgController

    var body: some View {
        ContentUnavailableView {
            Label(controller.canLaunch ? "The engine is not running" : "No Python interpreter",
                  systemImage: controller.canLaunch ? "power.circle" : "exclamationmark.triangle")
        } description: {
            if let problem = controller.runtimeProblem {
                // The reason, not just the fact: the Owner needs to know what to install.
                Text(problem).textSelection(.enabled)
            } else {
                Text("Start it to see the roster, the run and what it is costing. "
                     + "Nothing is sent to a model until you do.")
            }
        } actions: {
            if controller.canLaunch {
                Button("Start the engine") { controller.launch() }
                    .buttonStyle(.borderedProminent)
                    .accessibilityLabel("Start the engine")
            }
        }
    }
}

/// A section of a destination: a heading, one line saying what it is for, and its content.
///
/// A `DisclosureGroup` for the sections a person does not have to act on. The header keeps the title
/// and the summary *visible while collapsed*, so hiding a section hides its detail rather than the fact
/// that it exists — which is the difference between progressive disclosure and an app that lost a
/// feature.
///
/// **Expansion is a `Binding`, not internal state.** A card that seeded its own `@State` from a flag
/// and was also toggled from outside had two sources of truth for one question, so collapsing the card
/// with the chevron left the stored flag saying "expanded" — and the card reopened itself against the
/// user's intent on the next launch. One binding means the `@AppStorage` behind it *is* the state, and
/// the chevron and any outside toggle cannot disagree.
struct SectionCard<Content: View>: View {
    let title: String
    let symbol: String
    var summary: String?
    /// Nil for a section that is always open; it then has no chevron and cannot be collapsed. That is
    /// the right shape for the one section on a destination a person came to read.
    var expanded: Binding<Bool>?
    @ViewBuilder var content: () -> Content

    init(title: String, symbol: String, summary: String? = nil,
         expanded: Binding<Bool>? = nil,
         @ViewBuilder content: @escaping () -> Content) {
        self.title = title
        self.symbol = symbol
        self.summary = summary
        self.expanded = expanded
        self.content = content
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let expanded {
                DisclosureGroup(isExpanded: expanded) {
                    content().padding(.top, 6)
                } label: {
                    header
                }
                .accessibilityLabel("\(title). \(summary ?? "")")
            } else {
                header
                content()
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.06))
        .cornerRadius(8)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 2) {
            Label(title, systemImage: symbol).font(.headline)
            if let summary {
                Text(summary).font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Now

/// Now: the destination the app opens on.
struct NowPane: View {
    @ObservedObject var controller: OrgController
    /// Which sections are open, remembered across a relaunch so a person's layout survives.
    @AppStorage("now.showBoard") private var showBoard = false
    @AppStorage("now.showUsage") private var showUsage = false

    var body: some View {
        // The first-run gate replaces this destination entirely — see `SetupWizardView`. Doing it here
        // rather than in the window means Now can never be shown in the state it was designed to avoid:
        // a live-looking screen while nothing can actually run.
        if controller.showsFirstRunWizard {
            SetupWizardView(controller: controller)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    HappeningSection(controller: controller, showBoard: $showBoard)
                    SectionCard(title: "Who is on what", symbol: NowSection.board.symbol,
                                summary: NowSection.board.summary,
                                expanded: $showBoard) {
                        FlowSection(controller: controller)
                    }
                    SectionCard(title: "Cost and capacity", symbol: NowSection.usage.symbol,
                                summary: NowSection.usage.summary,
                                expanded: $showUsage) {
                        UsageSection(controller: controller)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

// MARK: - 1. What is happening

/// The present tense: the engine's own story, the transport controls, the place a goal is composed,
/// and the card that manages the goal once it exists.
///
/// The run-control bar used to sit above every panel with a control for everything — two ways to start
/// a run, three ways to pause a goal, a launch button in six places. Here there is **one** goal field
/// and **one** start button, and the run's transport controls appear only while a run exists, because a
/// Pause button for nothing is a control that cannot act.
struct HappeningSection: View {
    @ObservedObject var controller: OrgController
    /// Whether the board below is open, so the blocked figure can lead to the steps it counts.
    @Binding var showBoard: Bool

    private var report: [String: JSONValue] { controller.activity }
    private var timeline: [[String: JSONValue]] {
        (report["timeline"]?.arrayValue ?? []).compactMap { $0.objectValue }
    }
    private var counts: [String: JSONValue] { report["counts"]?.objectValue ?? [:] }

    /// Whether a run is actually going — which is **not** the same question as whether the engine is.
    ///
    /// `runStatus` is the whole status payload from the engine, so it is non-empty as soon as any poll
    /// has been answered, including on a machine where nothing has ever run. The old test here
    /// (`!runStatus.isEmpty && engineState == .running`) therefore drew the transport bar and the
    /// instruction field for a run that did not exist, and told a first-time user "Start another run".
    /// The engine's own field is `running`, set in *both* branches of `serve._cmd_status` (`:412` idle,
    /// `:432` setdefault), and this is the same expression `App.swift`'s Pause menu item disables on.
    private var runIsLive: Bool { controller.runStatus["running"]?.boolValue == true }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // `engineIsGone`, not `engineState != .running`: the latter is also true while the engine is
            // launching or draining, and during the console's own restart gap — so it swapped this
            // whole section out on every launch, stop and relaunch. See `OrgController.engineIsGone`.
            if controller.engineIsGone {
                EngineNotRunningView(controller: controller)
            } else {
                headline
                // The work this window cannot reach, immediately after this workspace's own headline:
                // both answer "what needs me", and the one a person cannot get to from here is the one
                // that used to be invisible. See `AttentionSection`.
                AttentionSection(controller: controller)
                proposedPlan
                if runIsLive { transport }
                goalComposer
                // The card that owns every durable-goal control — pause, resume, clear, the autonomy
                // picker. It was compiled and never mounted, so a paused goal could not be resumed from
                // the pane and its posture could not be changed at all; `OrgController.setGoalPosture`
                // had this view as its only caller.
                GoalSection(controller: controller)
                metrics
                if !controller.staffingGaps.isEmpty { staffingGaps }
                MissionSection(controller: controller)
                SubagentSection(controller: controller)
                timelineSection
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.06))
        .cornerRadius(8)
        // The read that is not scoped to this workspace, asked for when the destination that shows it
        // appears. `loadWindow` and the slow cadence also fetch it, so this is a first-look rather than
        // the only path to it — but without it a person who opens the app and looks at Now would see no
        // section until the next slow-panel tick (20s), which on a parked run is a long silence.
        .task { await controller.loadAttention() }
    }

    /// The present tense, in the engine's own words.
    private var headline: some View {
        let stop = report["stop_reason"]?.stringValue ?? ""
        let tone: Color = controller.pendingGate != nil ? .orange
            : (stop.isEmpty ? .primary : .red)
        return VStack(alignment: .leading, spacing: 4) {
            // Which org this is about, above the run's own headline and only when there is more than
            // one to be confused between. The headline below describes *an* org's work, so on a
            // multi-org window the first thing a person needs is which one they are reading.
            if controller.portfolioOrgs.count > 1, !controller.activeOrgName.isEmpty {
                Label(controller.activeOrgName, systemImage: "largecircle.fill.circle")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.green)
                    .accessibilityLabel("This is about the org \(controller.activeOrgName)")
            }
            Label(report["headline"]?.stringValue ?? (report.isEmpty
                      // **An absent report is not a report of nothing.** The engine answers `status`
                      // with an activity report even when nothing has ever run, so an empty one means
                      // the first poll has not been answered yet — and saying "nothing is running yet"
                      // there is a claim about the engine's work made from the absence of a reading,
                      // which is the confident-wrong-output this app refuses everywhere else. The
                      // two are distinguishable, so they are said differently.
                      ? "Asking the engine what is happening…"
                      : "Nothing is running yet"),
                  systemImage: controller.pendingGate != nil
                      ? "hand.raised.fill"
                      : (stop.isEmpty ? "dot.radiowaves.left.and.right" : "exclamationmark.triangle.fill"))
                .font(.title3.weight(.semibold))
                .foregroundStyle(tone)
                .textSelection(.enabled)
            if let objective = report["objective"]?.stringValue, !objective.isEmpty {
                Text(objective).font(.callout).foregroundStyle(.secondary).lineLimit(2)
            }
            if !stop.isEmpty {
                // The run's own verdict on why it stopped, stated where the headline is rather than
                // three sections down — a person reading "blocked" needs the reason beside it.
                //
                // `Orchestrator._derive_stop_reason` nearly always answers with a sentence ("pm: a
                // hand-off payload was blocked by the edge guardrail — …"), and then this line is the
                // engine's own words. One state is not a sentence: a checkpoint whose only record of
                // the stop is a bare verdict token, which is the state the board's rows used to be in.
                // There the token is glossed — from the engine's own vocabulary, the same table the
                // board's rows use, so one token is said one way — and the token itself stays in the
                // line rather than being replaced by a paraphrase of it.
                let gloss = EngineWord.stop(stop, words: controller.stopWords)
                Text(gloss.isEmpty ? stop : "\(stop) — \(gloss)")
                    .font(.caption).foregroundStyle(.red).textSelection(.enabled)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.opacity(0.08))
        .cornerRadius(8)
        .accessibilityElement(children: .combine)
    }

    // MARK: the proposed graph — what a person is being asked to approve

    /// The graph the engine proposed, drawn where the decision is.
    ///
    /// **What was invisible.** `manifest.proposed` carries the plan the engine composed from the goal
    /// — its node ids, its gates, its loops, and the staffing gaps that would stop it three nodes in —
    /// and no view read the field it lands in, so the only trace of a plan on screen was the timeline
    /// entry's node count. A person whose run is parked at `awaiting_approval` was being asked to
    /// approve something they could not look at.
    ///
    /// **The button is the engine's to offer.** The console's `approve` command resolves a *gate*
    /// (`Orchestrator.decide`) and raises for a plan, so an Approve built on that would be refused. The
    /// engine now has the command that approves a *graph* and runs it (`approve_plan` — the route
    /// `start` takes, minus the planning), and it says in the payload whether the plan is approvable
    /// (`approvable`, with `reason` when it is not). The card renders that verdict: a button when the
    /// engine will accept the command, the engine's own sentence when it will not. No control is
    /// offered on a state this view inferred for itself.
    @ViewBuilder
    private var proposedPlan: some View {
        if let plan = controller.proposedGraph {
            let nodes = (plan["nodes"]?.arrayValue ?? []).compactMap { $0.stringValue }
            let gates = (plan["gates"]?.arrayValue ?? []).compactMap { $0.stringValue }
            let gaps = (plan["staffing_gaps"]?.arrayValue ?? []).compactMap { $0.objectValue }
            let loops = (plan["loops"]?.arrayValue ?? []).compactMap { $0.objectValue }
            VStack(alignment: .leading, spacing: 5) {
                Label("The plan the engine proposed — \(nodes.count) step(s)",
                      systemImage: "point.topleft.down.to.point.bottomright.curvepath")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.orange)
                    .accessibilityLabel("The engine proposed a plan with \(nodes.count) steps")

                if nodes.isEmpty {
                    // The engine's `nodes` is the plan; a payload without it is a proposal this build
                    // cannot draw, and saying so beats an empty heading that reads as "no steps".
                    Text("The engine proposed a graph but sent no step list with it, so this build "
                         + "cannot draw it. The plan is on disk — the run parked with it.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    // The node ids verbatim, in the engine's own order: they are the names the board,
                    // the timeline and every gate sentence use, so a person reading them here can find
                    // the same step everywhere else.
                    Text(nodes.joined(separator: " · "))
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if !gates.isEmpty {
                    Label("a decision is asked at: \(gates.joined(separator: ", "))",
                          systemImage: "hand.raised")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                if !loops.isEmpty {
                    // `id` and `max_iterations` are the two fields the engine sends, and a loop is the
                    // one part of a plan whose bound a person may want to see before approving.
                    Label("loops: " + loops.map { loop in
                        let id = loop["id"]?.stringValue ?? "?"
                        guard let bound = loop["max_iterations"]?.intValue else { return id }
                        return "\(id) (up to \(bound))"
                    }.joined(separator: ", "), systemImage: "arrow.triangle.2.circlepath")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                if !gaps.isEmpty {
                    // The engine's own reason for each gap, and it is the reason a plan stops early —
                    // the sentence the CLI prints before a run for exactly this purpose.
                    Label("\(gaps.count) capability(ies) nobody holds: " + gaps.map { gap in
                        let skill = gap["skill"]?.stringValue ?? "?"
                        let reason = gap["reason"]?.stringValue ?? ""
                        return reason.isEmpty ? skill : "\(skill) — \(reason)"
                    }.joined(separator: "; "), systemImage: "person.crop.circle.badge.questionmark")
                        .font(.caption2).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Text(approvalSentence(plan))
                    .font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                approveControl(plan)
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.orange.opacity(0.08))
            .cornerRadius(8)
            // **Deliberately not `children: .combine`.** This card holds the Approve button now, and
            // combining the children into one element folds a control into a static label — readable,
            // but not operable by VoiceOver. The heading keeps its own label, so the card still
            // announces what it is, and the button stays a control a person can reach.
        }
    }

    /// What the plan is, in the terms the engine is actually in.
    ///
    /// Two facts and no verdict: whether the plan was composed or adopted, and whether the engine
    /// validated it. Whether it can be *approved* is a separate question, and its answer is the
    /// engine's — rendered by `approveControl` from `approvable`/`reason` rather than guessed here from
    /// the run's phase, which a relaunched console reports as `idle` for a plan the engine still holds.
    private func approvalSentence(_ plan: [String: JSONValue]) -> String {
        let adoption = plan["adopted"]?.boolValue == true
            ? "adopted from disk" : "composed from the goal"
        let check: String
        switch plan["validated"]?.boolValue {
        case .some(true): check = "it validated"
        case .some(false): check = "the engine did not validate it"
        case .none: check = "the engine did not say whether it validated"
        }
        return "\(adoption), \(check)."
    }

    /// The Approve control, or the engine's reason one is not offered.
    ///
    /// **A button whose only outcome is a refusal is worse than no button**, because a person learns the
    /// app is broken rather than that the engine declined. So the engine's verdict travels in the
    /// payload and decides which half renders: the command when the engine will accept it, the engine's
    /// own sentence when it will not. Nothing here infers the state that decides whether a control
    /// exists — the same rule the tool catalogue and the next-action kinds follow.
    @ViewBuilder
    private func approveControl(_ plan: [String: JSONValue]) -> some View {
        if plan["approvable"]?.boolValue == true {
            HStack(spacing: 8) {
                Button {
                    Task { await controller.approvePlan() }
                } label: {
                    Label("Approve and run", systemImage: "checkmark.seal")
                }
                .buttonStyle(.borderedProminent)
                .disabled(controller.engineState != .running)
                .help("Approve this graph and execute it — the engine runs it on the run's own "
                      + "thread, so Pause and Abort stay answerable")
                .accessibilityLabel("Approve this plan and run it")

                Text("the engine runs it on its own thread, so Pause and Abort stay answerable")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        } else {
            Text(notApprovableReason(plan))
                .font(.caption2).foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
    }

    /// The engine's reason this plan cannot be approved — never a reason invented here.
    ///
    /// The engine sends `reason` whenever `approvable` is false, naming the thing that blocks it (no
    /// plan file, an unvalidated graph, a run already in flight). Only the engine's silence — a build
    /// that does not carry the field — falls back to a sentence of ours, and it says exactly that
    /// rather than diagnosing a state this view cannot see.
    private func notApprovableReason(_ plan: [String: JSONValue]) -> String {
        let reason = plan["reason"]?.stringValue ?? ""
        if !reason.isEmpty { return reason }
        return "The engine has not said this plan can be approved, so the console offers no control "
            + "it would refuse."
    }

    /// Pause, resume, abort and the instruction channel — shown only while a run exists.
    ///
    /// The labels name what they pause. The old app had a `Pause` here (the run) and a `Pause` in the
    /// goal section (the goal), both reading "Pause", which is how a person presses the wrong one and
    /// concludes the app is broken.
    @ViewBuilder
    private var transport: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text("The run").font(.subheadline.weight(.medium))
                Button {
                    Task { await controller.pause() }
                } label: {
                    Label("Pause the run", systemImage: "pause.circle")
                }
                .help("Pause at the next node boundary — never mid-generation, which would corrupt state")
                .accessibilityLabel("Pause the run at its next node boundary")

                Button {
                    Task { await controller.resume() }
                } label: {
                    Label("Resume the run", systemImage: "play.circle")
                }
                .help("Resume from the checkpoint")
                .accessibilityLabel("Resume the run")

                Button(role: .destructive) {
                    Task { await controller.abort() }
                } label: {
                    Label("Abort", systemImage: "xmark.octagon")
                }
                .help("Abort the run, keeping its checkpoint")
                .accessibilityLabel("Abort the run")
                Spacer()
                if let phase = controller.runStatus["phase"]?.stringValue {
                    Text(phase).font(.caption.monospaced()).foregroundStyle(.secondary)
                }
            }
            InstructionField(controller: controller)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.blue.opacity(0.06))
        .cornerRadius(8)
    }

    /// The instruction channel: guidance pushed into the run, optionally as a constraint.
    ///
    /// Kept apart from the composer because it steers a run that is already going, while the composer
    /// starts one — two different moments, and the old bar put them side by side with no separation.
    private struct InstructionField: View {
        @ObservedObject var controller: OrgController
        @State private var text: String = ""
        @State private var asConstraint = false

        var body: some View {
            HStack(spacing: 8) {
                TextField("Guide this run — or state a rule it must not break", text: $text)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(send)
                    .accessibilityLabel("Instruction for the run")
                Toggle("Must not be dropped", isOn: $asConstraint)
                    .toggleStyle(.checkbox)
                    .help("A constraint is preserved verbatim across every compaction and rotation")
                    .accessibilityLabel("Make this a non-negotiable constraint")
                Button("Send", action: send)
                    .disabled(text.isEmpty || controller.engineState != .running)
                    .accessibilityLabel("Send the instruction")
            }
        }

        private func send() {
            guard !text.isEmpty else { return }
            let outgoing = text
            let constraint = asConstraint
            text = ""
            Task { await controller.instruct(outgoing, asConstraint: constraint) }
        }
    }

    /// The one place a goal is composed.
    ///
    /// One field, one primary button, one label. The old app had this string in two fields with two
    /// labels ("Goal — what should the org build?" and "What should be achieved?") beside two buttons
    /// that did different things, and the ⌘R shortcut started a run with **no goal at all** while the
    /// visible button used the draft. Now the shortcut and the button call the same method with the
    /// same string, and the difference between "plan only" and "start" is said on the buttons.
    private var goalComposer: some View {
        VStack(alignment: .leading, spacing: 6) {
            // "another" only when there is one. The old test was `runStatus.isEmpty`, which is false as
            // soon as the engine answers a single status poll — so a person who had never started
            // anything was invited to "Start another run".
            Text(runIsLive ? "Start another run" : "Start something")
                .font(.subheadline.weight(.medium))
            HStack(spacing: 8) {
                TextField("Goal — what should the org build?", text: $controller.goalDraft)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { start(dryRun: false) }
                    .accessibilityLabel("What the org should build")

                Button {
                    start(dryRun: false)
                } label: {
                    Label("Start run", systemImage: "flag.checkered")
                }
                .buttonStyle(.borderedProminent)
                .disabled(controller.engineState != .running || controller.goalDraft.isEmpty)
                .help("Plan the goal, show the graph, then execute it")
                .accessibilityLabel("Start a run for this goal")

                Button {
                    start(dryRun: true)
                } label: {
                    Label("Plan only", systemImage: "doc.text.magnifyingglass")
                }
                .disabled(controller.engineState != .running || controller.goalDraft.isEmpty)
                .help("Plan and bind the graph without executing it")
                .accessibilityLabel("Plan this goal without executing it")

                // The durable goal is a *different* thing from a run: it keeps the loop going past a
                // finished model turn. Named fully, so it is not mistaken for a second start button.
                Button("Keep working until done") {
                    let objective = controller.goalDraft
                    Task { await controller.setGoal(objective, arm: true) }
                }
                .disabled(controller.engineState != .running || controller.goalDraft.isEmpty)
                .help("Set a durable goal: the loop continues past a finished model turn until the "
                      + "agent reports the objective done or blocked")
                .accessibilityLabel("Set a durable goal and keep working until it is done")
            }
            if let posture = controller.goalPosturePreference {
                // Stated here because it is what this button will send: a goal set from this window
                // carries the posture the person chose, so the claim is true of every goal it creates.
                Label("New goals here run \(posture.label.lowercased()).",
                      systemImage: posture == .unattended ? "arrow.right.circle" : "hand.raised.fill")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.05))
        .cornerRadius(8)
    }

    private func start(dryRun: Bool) {
        let goal = controller.goalDraft
        Task { await controller.startRun(goal: goal, dryRun: dryRun) }
    }

    /// Progress and presence, which is what "where is it going" reduces to.
    ///
    /// **Five** figures rather than the eight that used to sit across two panels: `Swarm` and
    /// `Continues` were both already said elsewhere (the swarm by its own section, the goal by the goal
    /// card — which is rendered above this, so that claim is now checkable on screen rather than only
    /// in this comment).
    private var metrics: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 22) {
                Metric(label: "Phase", value: report["phase"]?.stringValue ?? "—",
                       detail: report["running"]?.boolValue == true ? "running" : nil,
                       tone: report["running"]?.boolValue == true ? .blue : .primary)
                Metric(label: "Nodes",
                       value: "\(counts["done"]?.intValue ?? 0) of \(counts["nodes"]?.intValue ?? 0)",
                       detail: "done", tone: .green)
                Metric(label: "Blocked", value: "\(blockedCount)",
                       detail: "steps that stopped",
                       tone: blockedCount > 0 ? .red : .secondary)
                Metric(label: "In flight", value: "\(counts["in_flight"]?.intValue ?? 0)")
                let sub = counts["subagents"]?.intValue ?? 0
                Metric(label: "Subagents", value: "\(sub)",
                       detail: counts["swarms"]?.intValue ?? 0 > 0
                           ? "\(counts["swarms"]?.intValue ?? 0) swarm(s)" : nil,
                       tone: sub > 0 ? .blue : .secondary)
            }
            // A bare count names nothing a person can act on. The board below is where each stopped
            // step is named and, now, given the reason it stopped *and* the one action the engine will
            // accept for it, so the figure leads there rather than leaving "need attention" as the
            // whole answer. The reason a row shows is the engine's `blocked_by` when the run recorded
            // one and this build's gloss of the verdict when it did not — the guardrail case, where
            // the run had no `blocked_by` to give.
            if blockedCount > 0 {
                HStack(spacing: 8) {
                    Label("Each stopped step, and why it stopped, is on the board.",
                          systemImage: "exclamationmark.triangle")
                        .font(.caption2).foregroundStyle(.secondary)
                    Button("Show the board") { showBoard = true }
                        .controlSize(.small)
                        .help("Open “Who is on what” below, one row per step")
                        .accessibilityLabel("Open the board below, which names each stopped step")
                }
            }
        }
        .padding(.vertical, 2)
    }

    /// How many steps stopped — the board's own figure, so the number and the place it leads agree.
    ///
    /// The board's `counts.stuck` is the engine's own tally of stopped rows
    /// (`engine/flow.py:1060`, over the single predicate at `:278`), and this figure is what sends a
    /// person to the board, so the two have to be the same number. It used to read the activity
    /// report's `counts.blocked`, which `engine/activity.py:640` still computes as `status == "blocked"`
    /// alone: a step stopped by its completion contract (`needs_review`) or parked at a human gate
    /// (`awaiting_owner`) was counted by the board and not by this figure — so the figure could read 0
    /// while the board read 1, and the sentence under it, which points at the board, would not even be
    /// offered. The activity report stays as the fallback for the one state the board cannot answer in:
    /// its snapshot failed and the engine sent `counts: {}` (`engine/serve.py:543`).
    private var blockedCount: Int {
        if let stuck = controller.flow["counts"]?.objectValue?["stuck"]?.intValue { return stuck }
        return counts["blocked"]?.intValue ?? 0
    }

    /// Unstaffed capabilities, because a plan needing somebody nobody holds is the commonest reason a
    /// run stalls before it starts.
    ///
    /// The list named the gaps and offered no way to close one, which is a panel that reports a problem
    /// and then stops. Hiring happens on the Org destination, so this is a door to it rather than a
    /// second hire form — the same destination the spine's own `hire` action opens.
    private var staffingGaps: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("Capabilities nobody holds", systemImage: "person.crop.circle.badge.questionmark")
                .font(.headline).foregroundStyle(.orange)
            Text("A plan that needs one of these is staffed on the default model, or waits for you.")
                .font(.caption).foregroundStyle(.secondary)
            // Keyed on the gap's own `node_id`: `Binding.staffing_gaps` sets it from the manifest node's
            // id (falling back to the skill). Under `id: \.self` the key included `holders` and
            // `reason`, and `holders` changes the moment somebody is hired — so a row the person was
            // reading was replaced while they read it.
            ForEach(controller.staffingGaps.map { (key: $0["node_id"]?.stringValue ?? "", gap: $0) },
                    id: \.key) { row in
                HStack(spacing: 8) {
                    Text(row.gap["skill"]?.stringValue ?? "?")
                        .font(.system(.caption, design: .monospaced))
                    Text(row.gap["reason"]?.stringValue ?? "")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            HStack(spacing: 8) {
                Text("Hiring is done on Org.").font(.caption2).foregroundStyle(.secondary)
                Button("Open Org") { DestinationRouter.shared.select(.org) }
                    .controlSize(.small)
                    .help("Org is where a hire is made — “Hire a new agent”, with the skill to fill")
                    .accessibilityLabel("Open the Org destination to hire for this gap")
            }
            .padding(.top, 2)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.08))
        .cornerRadius(8)
    }

    /// The history: what happened, newest last, so reading downward is reading forward in time.
    ///
    /// Bounded to the last 40 entries: the engine already caps its report, and a section inside a
    /// scrolling destination is not the place for an unbounded list. The terminal has the whole stream.
    @ViewBuilder
    private var timelineSection: some View {
        if timeline.isEmpty {
            Text("No activity recorded yet. Set a goal or start a run and the timeline fills in.")
                .font(.caption).foregroundStyle(.secondary)
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Label("Timeline", systemImage: "clock.arrow.circlepath")
                    .font(.headline)
                // Keyed on the entry's own content, not on its position in the window.
                //
                // The report is a *rolling* window (`suffix(40)` over a list that grows), so under
                // `id: \.offset` every poll shifted every row's content by one position while the
                // identities stayed put — the worst possible input for a diff. The activity report
                // carries no per-entry id (`node_id` and `agent_id` are correlation, not keys), so the
                // key is built from the fields that identify an entry; see `timelineKey`.
                ForEach(timeline.suffix(40).map { (key: timelineKey($0), entry: $0) }, id: \.key) { row in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: symbol(for: row.entry["tone"]?.stringValue ?? "info"))
                            .foregroundStyle(colour(for: row.entry["tone"]?.stringValue ?? "info"))
                            .accessibilityHidden(true)
                        Text(clock(row.entry["at"]?.stringValue ?? ""))
                            .font(.system(.caption2, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .frame(width: 60, alignment: .leading)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(row.entry["title"]?.stringValue ?? "")
                                .font(.caption)
                            if let detail = row.entry["detail"]?.stringValue, !detail.isEmpty {
                                Text(detail).font(.caption2).foregroundStyle(.secondary)
                                    .lineLimit(2)
                            }
                        }
                        Spacer()
                        Text(EngineWord.timeline(row.entry["kind"]?.stringValue ?? ""))
                            .font(.system(.caption2, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    // One row is one event; VoiceOver should read it as a sentence, not fragments.
                    .accessibilityElement(children: .combine)
                }
            }
        }
    }

    /// The identity of a timeline entry, built from everything the row renders.
    ///
    /// The activity report supplies no per-entry id — `node_id`, `agent_id` and `ref` are correlation
    /// fields, and `ref` is set for a proposal entry only — so this is a content fingerprint rather
    /// than an id, and it is honest about being one. Full rather than selective: an entry is derived
    /// from a record that never changes, so a fingerprint that left a rendered field out could give
    /// two visibly different rows one identity, which is worse than a row being rebuilt.
    private func timelineKey(_ entry: [String: JSONValue]) -> String {
        ["at", "kind", "title", "detail", "node_id", "agent_id", "ref"]
            .map { entry[$0]?.stringValue ?? "" }
            .joined(separator: "|")
    }

    /// `HH:MM:SS` out of an ISO timestamp; the whole timestamp when it is not one (a current-state
    /// row carries no time, and says so rather than showing a fabricated one).
    private func clock(_ timestamp: String) -> String {
        guard timestamp.count >= 19, timestamp.hasPrefix("20") else {
            return timestamp.isEmpty ? "current" : timestamp
        }
        let start = timestamp.index(timestamp.startIndex, offsetBy: 11)
        let end = timestamp.index(start, offsetBy: 8)
        return String(timestamp[start..<end])
    }

    private func colour(for tone: String) -> Color {
        switch tone {
        case "good": return .green
        case "warn": return .orange
        case "bad": return .red
        default: return .secondary
        }
    }

    private func symbol(for tone: String) -> String {
        switch tone {
        case "good": return "checkmark.circle.fill"
        case "warn": return "exclamationmark.triangle.fill"
        case "bad": return "xmark.octagon.fill"
        default: return "circle.dotted"
        }
    }
}

// MARK: - Other workspaces that need you

/// The workspaces under the projects root that are waiting on a person — **and are not this one**.
///
/// WHY THIS IS HERE AND NOT ON RUNS
/// --------------------------------
/// Runs is the destination that works with the engine *stopped*, and it reads one workspace's
/// `.agent_state/` — the folder this window is pointed at. A run parked in a folder the window is not
/// pointed at is invisible there by construction, which is precisely the reported failure: *"Not sure
/// why PM is still blocked Priya… I still don't understand what actions I need to take"*. Now is the
/// destination that answers "what needs me" and the one the app opens on, so the answer belongs here,
/// above the sections that describe this workspace's own run.
///
/// WHAT THIS CAN AND CANNOT DO
/// ---------------------------
/// `serve` is bound to one workspace and every run command it answers acts on that one, so a gate in
/// another project cannot be decided from here — and no control is offered that would pretend otherwise.
/// The one write that is not workspace-scoped is `portfolio_add`, so the control is **Adopt as an org**,
/// using the engine's own payload (`attention.org_link`): the folder becomes an org in the register, the
/// Portfolio section lists it, and switching to it makes every control in this window act on it. The
/// engine's own command is shown beside it as selectable text, because the terminal *can* act on that
/// project directly and this window cannot.
struct AttentionSection: View {
    @ObservedObject var controller: OrgController
    /// The row whose adoption is in flight, keyed by its slug, so only that row shows progress.
    @State private var adopting: String?
    /// The engine's refusal, when it made one — a name it already has, a folder it will not register.
    @State private var refusal: String?

    private var rows: [[String: JSONValue]] { controller.attentionElsewhereRows }

    var body: some View {
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Label("\(rows.count) other workspace\(rows.count == 1 ? "" : "s") need you",
                      systemImage: "hand.raised.fill")
                    .font(.headline).foregroundStyle(.orange)
                    .accessibilityLabel("\(rows.count) other work spaces are waiting on you")
                Text("These are not the project this window is acting on, so its controls cannot decide "
                     + "what they are waiting for. The engine's step for each is below.")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                // Keyed on the workspace's own `path`. The slug and the name repeat — a folder named for
                // its slug is the common case — while the path is what identifies the project, and it is
                // also the field the engine reports for the workspace this window is on.
                ForEach(rows.map { (key: $0["path"]?.stringValue ?? "", row: $0) }, id: \.key) { item in
                    rowView(item.row)
                }
                if let refusal {
                    Label(refusal, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption).foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.orange.opacity(0.08))
            .cornerRadius(8)
        }
    }

    /// One workspace: what it is waiting for, the engine's step, and the one action that reaches it.
    @ViewBuilder
    private func rowView(_ row: [String: JSONValue]) -> some View {
        let action = row["next_action"]?.objectValue ?? [:]
        let org = row["org"]?.objectValue ?? [:]
        let slug = row["slug"]?.stringValue ?? ""
        let registered = org["registered"]?.boolValue == true
        // The routes that are *not* the first one, in the engine's words — the same list a terminal
        // prints under `Next:`. Computed here rather than beside the view that draws them because a
        // declaration inside a nested `ViewBuilder` closure is not something to rely on.
        let alternatives = (action["also"]?.arrayValue ?? []).compactMap { $0.objectValue }
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 7) {
                Text(row["name"]?.stringValue ?? slug)
                    .font(.system(.callout, design: .rounded).weight(.medium))
                    .lineLimit(1)
                Text(row["phase"]?.stringValue ?? "")
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary)
                Spacer()
            }
            if let headline = row["headline"]?.stringValue, !headline.isEmpty {
                Text(headline).font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("Next: \(row["waiting_for"]?.stringValue ?? "")").font(.caption)
            if let detail = action["detail"]?.stringValue, !detail.isEmpty {
                Text(detail).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
            }
            HStack(spacing: 8) {
                if registered {
                    // Already an org: there is nothing to add, and the Portfolio's own switch is the step.
                    Button("Switch to it and decide") {
                        Task { await controller.selectOrg(org["ref"]?.stringValue ?? slug) }
                    }
                    .controlSize(.small).buttonStyle(.borderedProminent)
                    .help("Make this org the one this window describes, so its parked plan and the "
                          + "decision it needs are on Now")
                    .accessibilityLabel("Switch to this org and decide what it is waiting on")
                } else {
                    Button("Adopt as an org") { adopt(row) }
                        .controlSize(.small).buttonStyle(.borderedProminent)
                        .disabled(adopting != nil || controller.engineState != .running)
                        .help("Register this project in the portfolio, using the engine's own payload. "
                              + "It then appears in the Portfolio under Org, where it can be switched to "
                              + "and decided — a run in another folder cannot be acted on without that.")
                        .accessibilityLabel("Adopt this project as an org so it can be acted on")
                }
                if adopting == slug { ProgressView().controlSize(.small) }
                // The command the terminal would run. Shown for the person who would rather act there —
                // and it is the only way to do the two things this window cannot: re-run a graph, or
                // approve a plan in a folder that is not the one this window describes.
                if let command = action["command"]?.stringValue, !command.isEmpty {
                    Text(command)
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .lineLimit(1)
                        .help(command)
                }
            }
            // The routes that are not the first one. A gate whose reason is about a node's *report* is
            // not resolved by approving it, and a run can sit there for days while every surface points
            // at approve — so when the engine's own dossier says the node has already attempted, the
            // other verbs it accepts travel with the action (`activity._gate_action`) and are shown here
            // as well as on the command line. Nothing is composed here: label and command are the
            // engine's, and an alternative without a command is not drawn at all.
            if !alternatives.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(0..<alternatives.count, id: \.self) { index in
                        alternativeView(alternatives[index])
                    }
                }
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.06))
        .cornerRadius(6)
    }

    /// One route that is not the first one: the engine's label for it, and the command it names.
    @ViewBuilder
    private func alternativeView(_ alternative: [String: JSONValue]) -> some View {
        if let command = alternative["command"]?.stringValue, !command.isEmpty {
            Text("or instead — \(alternative["label"]?.stringValue ?? "")")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(command)
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .lineLimit(1)
                .help(command)
        }
    }

    /// Register the row's workspace, from the engine's own payload.
    ///
    /// Nothing here composes a name, a slug or a path: `attention.org_link` already did, because that is
    /// the payload `portfolio_add` will accept and a surface that derived its own would be a second
    /// definition of "what this workspace is called".
    private func adopt(_ row: [String: JSONValue]) {
        let org = row["org"]?.objectValue ?? [:]
        guard let payload = org["adopt"]?.objectValue else { return }
        let slug = row["slug"]?.stringValue ?? ""
        adopting = slug
        refusal = nil
        Task {
            let created = await controller.adoptWorkspace(
                name: payload["name"]?.stringValue ?? slug,
                slug: payload["slug"]?.stringValue ?? slug,
                path: payload["path"]?.stringValue ?? "")
            adopting = nil
            // A refusal is the engine's sentence (`controller.notice`, set by `mutate`), shown here so a
            // failed click reads differently from a click that did nothing.
            if created == nil { refusal = controller.notice }
        }
    }
}

// MARK: - The mission — "what is all this for, and which step are we on?"

/// The standing purpose and the ordered objectives that serve it.
///
/// Shown above the goal because it is the layer above it: the goal is *what is being worked on now*,
/// the mission is *why*. It renders the objectives with their state, marks the active one, and offers
/// the one action that starts real work — handing an objective to a goal. It never hides whether the
/// mission is being worked, and it never arms a goal on its own (the button says so).
struct MissionSection: View {
    @ObservedObject var controller: OrgController
    @State private var draft: String = ""

    private var statement: String { controller.mission["statement"]?.stringValue ?? "" }
    private var state: String { controller.mission["state"]?.stringValue ?? "empty" }
    private var live: Bool { controller.mission["live"]?.boolValue ?? false }
    private var objectives: [[String: JSONValue]] {
        (controller.mission["objectives"]?.arrayValue ?? []).compactMap { $0.objectValue }
    }
    private var progress: [String: JSONValue] { controller.mission["progress"]?.objectValue ?? [:] }
    private var nowText: String { controller.mission["now"]?.objectValue?["text"]?.stringValue ?? "" }

    private var tone: Color {
        switch state {
        case "active": return .green
        case "paused": return .orange
        case "blocked": return .red
        case "completed": return .blue
        default: return .secondary
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("The plan this serves", systemImage: "flag.checkered")
                .font(.headline)
                .foregroundStyle(live ? .green : .secondary)

            if statement.isEmpty {
                // A shorter version of the paragraph that used to be here. The model the old text was
                // compensating for is now visible in the field's own label and button, so the
                // explanation is a hint rather than an essay.
                TextField("What is this all for?", text: $draft)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Mission statement")
                HStack(spacing: 8) {
                    Button("Set it") { Task { await controller.setMission(draft) } }
                        .disabled(draft.isEmpty || controller.engineState != .running)
                        .accessibilityLabel("Set the mission")
                    Text("A standing purpose with an ordered list of objectives, worked one at a time.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            } else {
                HStack(spacing: 18) {
                    Metric(label: "State", value: state.capitalized, tone: tone)
                    Metric(label: "Objectives",
                           value: "\(progress["done"]?.intValue ?? 0) of \(progress["total"]?.intValue ?? 0)",
                           detail: "done", tone: .green)
                    Metric(label: "Working", value: live ? "yes" : "no",
                           detail: controller.mission["pause_reason"]?.stringValue,
                           tone: live ? .green : .secondary)
                }

                Text(statement).font(.body).textSelection(.enabled)

                if objectives.isEmpty {
                    Text("No objectives yet — add the first step below.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    // Keyed on the objective itself. The engine offers no objective id — it addresses
                    // them by *index*, which `mission_add` can insert at and `mission_remove` can
                    // delete from, so an index is an address and not an identity. `created_at` and
                    // `text` are each written once and never edited, and the engine refuses a duplicate
                    // text outright, so the pair is stable and unique within a mission. The index is
                    // still what the row displays and what `markObjective` takes.
                    ForEach(objectives.enumerated().map {
                        (index: $0.offset, key: objectiveKey($0.element), objective: $0.element)
                    }, id: \.key) { row in
                        objectiveRow(index: row.index, objective: row.objective)
                    }
                }

                HStack(spacing: 8) {
                    TextField("Next objective", text: $draft)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("New objective")
                    Button("Add") {
                        let text = draft
                        draft = ""
                        Task { await controller.addObjective(text) }
                    }
                    .disabled(draft.isEmpty || controller.engineState != .running)
                    Button(live ? "Pause the mission" : "Arm the mission") {
                        Task { live ? await controller.pauseMission() : await controller.armMission() }
                    }
                    .help("Arming the mission does not spend — “Start” on an objective does")
                    Spacer()
                    if !nowText.isEmpty {
                        // Starting an objective is what hands it to a goal; the label says so, because
                        // a button that begins a spend must not be ambiguous.
                        Button("Work on “\(nowText.prefix(24))”") {
                            Task { await controller.startObjective() }
                        }
                        .buttonStyle(.borderedProminent)
                        .help("Set a goal for the active objective and begin working it")
                    } else if progress["next"]?.stringValue?.isEmpty == false {
                        Button("Work on the next one") { Task { await controller.startObjective() } }
                            .buttonStyle(.borderedProminent)
                    }
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.opacity(0.08))
        .cornerRadius(8)
    }

    /// The identity of an objective, for the list's diffing.
    ///
    /// `created_at` and `text` are the two fields `Mission.add_objective` writes once and nothing edits;
    /// the engine also refuses an exact duplicate, so the pair cannot repeat within one mission. That is
    /// as close to an id as the payload gets — it has none, and an index is not one.
    private func objectiveKey(_ objective: [String: JSONValue]) -> String {
        "\(objective["created_at"]?.stringValue ?? "")|\(objective["text"]?.stringValue ?? "")"
    }

    @ViewBuilder
    private func objectiveRow(index: Int, objective: [String: JSONValue]) -> some View {
        let objectiveState = objective["state"]?.stringValue ?? "pending"
        let mark: String = {
            switch objectiveState {
            case "done": return "checkmark.circle.fill"
            case "active": return "arrow.right.circle.fill"
            case "blocked": return "exclamationmark.triangle.fill"
            case "skipped": return "minus.circle"
            default: return "circle"
            }
        }()
        let rowTone: Color = {
            switch objectiveState {
            case "done": return .green
            case "active": return .blue
            case "blocked": return .red
            default: return .secondary
            }
        }()
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: mark).foregroundStyle(rowTone).accessibilityHidden(true)
            Text("#\(index)").font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.secondary).frame(width: 24, alignment: .leading)
            VStack(alignment: .leading, spacing: 1) {
                Text(objective["text"]?.stringValue ?? "")
                    .font(.caption)
                if let summary = objective["summary"]?.stringValue, !summary.isEmpty {
                    Text(summary).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer()
            if objectiveState == "active" {
                Button("Done") { Task { await controller.markObjective(index, state: "done") } }
                    .buttonStyle(.link)
                    .help("Mark this objective done and move to the next")
                Button("Blocked") {
                    Task { await controller.markObjective(index, state: "blocked",
                                                          summary: "blocked by the Owner") }
                }
                .buttonStyle(.link)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Objective \(index): \(objective["text"]?.stringValue ?? ""), \(objectiveState)")
    }
}

// MARK: - The goal — "is this run going to keep going?"

/// The durable goal, and the controls that make it start, stop and continue.
///
/// The one thing this view must never do is hide whether the loop will continue: `live` is the first
/// fact, spelled out as well as coloured. Its two buttons say **goal**, so they are not confused with
/// the run's transport controls above — the old app had a `Pause` in each with nothing to tell them
/// apart.
struct GoalSection: View {
    @ObservedObject var controller: OrgController

    private var state: String { controller.goal["state"]?.stringValue ?? "cleared" }
    private var live: Bool { controller.goal["live"]?.boolValue ?? false }
    private var objective: String { controller.goal["objective"]?.stringValue ?? "" }

    private var tone: Color {
        switch state {
        case "armed": return .green
        case "paused": return .orange
        case "blocked": return .red
        case "completed": return .blue
        default: return .secondary
        }
    }

    var body: some View {
        // With no goal at all there is nothing to show here: the composer in `HappeningSection` is
        // where a goal is set, and a second empty-state paragraph saying so was the kind of duplicated
        // prose the audit called "paragraphs used as UI".
        if !objective.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Label("The goal", systemImage: "target")
                    .font(.headline)
                    .foregroundStyle(live ? .green : .secondary)

                HStack(spacing: 18) {
                    Metric(label: "State", value: state.capitalized, tone: tone)
                    Metric(label: "Continues", value: live ? "yes" : "no",
                           detail: controller.goal["pause_reason"]?.stringValue,
                           tone: live ? .green : .secondary)
                    Metric(label: "Budget",
                           value: controller.goal["budget_enabled"]?.boolValue == true
                               ? "\(controller.goal["token_budget"]?.intValue ?? 0)"
                               : "none",
                           detail: controller.goal["budget_enabled"]?.boolValue == true
                               ? "tokens per slice" : "runs until done")
                    if let spend = controller.goal["spend"]?.objectValue {
                        Metric(label: "Rounds", value: "\(spend["rounds"]?.intValue ?? 0)")
                        Metric(label: "Tokens", value: "\(spend["tokens"]?.intValue ?? 0)")
                        Metric(label: "Cost",
                               value: spend["cost_usd"]?.doubleValue
                                   .map { String(format: "$%.4f", $0) } ?? "unknown")
                    }
                }

                Text(objective).font(.body).textSelection(.enabled)

                // Autonomy is why a gate did or did not stop the run, so it belongs next to the state.
                // Read from the goal itself, not from the defaults, so this shows what *this*
                // objective chose.
                autonomyControls

                if let summary = controller.goal["summary"]?.stringValue, !summary.isEmpty {
                    Text("complete: \(summary)").font(.caption).foregroundStyle(.blue)
                }
                if let blocked = controller.goal["blocked_reason"]?.stringValue, !blocked.isEmpty {
                    Text("blocked: \(blocked)").font(.caption).foregroundStyle(.red)
                }
                if controller.goal["pause_reason"]?.stringValue == "restored" {
                    // The one safety property worth surfacing: a restored goal is deliberately disarmed.
                    Text("Restored from disk and disarmed. Use Resume to continue it.")
                        .font(.caption2).foregroundStyle(.secondary)
                }

                HStack(spacing: 8) {
                    Button("Pause the goal") { Task { await controller.pauseGoal() } }
                        .disabled(!live)
                        .accessibilityLabel("Pause the goal")
                    Button("Resume the goal") { Task { await controller.resumeGoal() } }
                        .disabled(live)
                        .help("Continue, granting a fresh budget slice; the totals are kept")
                        .accessibilityLabel("Resume the goal")
                    Button("Clear the goal") { Task { await controller.clearGoal() } }
                        .accessibilityLabel("Clear the goal")
                    Spacer()
                    if !live && state == "paused" {
                        Button {
                            // The one case where the whole loop matters: a paused goal is the only way
                            // a run is waiting for a person who is not at a gate.
                            Task { await controller.resumeGoal() }
                        } label: {
                            Label("Continue working", systemImage: "play.circle.fill")
                        }
                        .buttonStyle(.borderedProminent)
                    }
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(tone.opacity(0.08))
            .cornerRadius(8)
        }
    }

    /// The goal's autonomy: one posture control, and what it currently means.
    ///
    /// Reading the posture from the engine's `goal.posture` rather than a local copy is what keeps the
    /// picker and the policy the run is using the same answer. The `unknown` case is rendered as
    /// unknown rather than as one of the two known values: rendering an unrecognised authority as
    /// `unattended` would make the app claim something it was never told.
    @ViewBuilder
    private var autonomyControls: some View {
        let posture = controller.goalPosture
        let decides = controller.goal["decides_gates"]?.boolValue ?? false
        let staffs = controller.goal["staffs_gaps"]?.boolValue ?? false
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 12) {
                Label(decides ? "gates: it decides" : "gates: you decide",
                      systemImage: decides ? "arrow.right.circle" : "hand.raised.fill")
                    .font(.caption)
                    .foregroundStyle(decides ? .blue : .orange)
                Label(staffs ? "missing skills: filled" : "missing skills: reported",
                      systemImage: staffs ? "person.badge.plus" : "person.crop.circle.badge.exclamationmark")
                    .font(.caption)
                    .foregroundStyle(staffs ? .blue : .orange)
                Spacer()
                if posture == .unknown {
                    Label(posture.label, systemImage: "questionmark.circle")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Picker("Autonomy", selection: Binding(
                        get: { posture },
                        set: { chosen in Task { await controller.setGoalPosture(chosen) } })) {
                            ForEach(OrgController.Posture.choices) { choice in
                                Text(choice.label).tag(choice)
                            }
                        }
                        .pickerStyle(.segmented)
                        .labelsHidden()
                        .frame(width: 210)
                        .disabled(controller.engineState != .running)
                        .help(posture.explanation)
                        .accessibilityLabel("How much this goal decides alone")
                        .accessibilityValue(posture.label)
                        .accessibilityHint(posture.explanation)
                }
            }
            // One line, not two paragraphs. The release gate's safety property is the one thing a
            // person choosing Unattended must know, and it fits in a line: the old panel spent a
            // paragraph on it, which is what made the paragraph read as an apology for the control.
            Text("A release, close or spend gate still needs its evidence present and no safety "
                 + "control fired before it is released, whichever of these you pick.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Subagents — "what parallel work is in flight?"

/// The isolated children a run dispatched, each a transcript the parent can page rather than hold.
///
/// Rendered as a tree because that is what it is: these ran *inside* one node, in their own contexts.
/// Selecting one opens its transcript, so the isolation is visible rather than something the user has
/// to take on trust.
struct SubagentSection: View {
    @ObservedObject var controller: OrgController
    @State private var selected: String?

    var body: some View {
        if !controller.subagents.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Label("Work running in parallel", systemImage: "person.3.sequence.fill")
                    .font(.headline)
                Text("Each of these ran inside one step, in its own context, and can be read back.")
                    .font(.caption).foregroundStyle(.secondary)

                // Keyed on `child_id`, which is the engine's own handle for a child (`subagent_read`
                // takes exactly it). Under `id: \.self` the key included `status` and `bytes`, both of
                // which change while the child runs, so the row — and with it the "Read" button under
                // the pointer — was replaced on each progress update.
                ForEach(controller.subagents.map { (key: $0["child_id"]?.stringValue ?? "", child: $0) },
                        id: \.key) { row in
                    let id = row.child["child_id"]?.stringValue ?? "?"
                    HStack(spacing: 10) {
                        Image(systemName: icon(for: row.child))
                            .foregroundStyle(tone(for: row.child))
                            .accessibilityHidden(true)
                        Text(id)
                            .font(.system(.caption, design: .monospaced))
                            .frame(width: 100, alignment: .leading)
                        Text(EngineWord.subagent(row.child["status"]?.stringValue ?? "—"))
                            .font(.caption).foregroundStyle(tone(for: row.child))
                            .frame(width: 90, alignment: .leading)
                        Text(row.child["task"]?.stringValue ?? "")
                            .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        Spacer()
                        if let bytes = row.child["bytes"]?.intValue, bytes > 0 {
                            Text("\(bytes) bytes").font(.caption2).foregroundStyle(.secondary)
                        }
                        Button("Read") {
                            selected = id
                            Task { await controller.loadSubagentTranscript(childId: id) }
                        }
                        .accessibilityLabel("Read the transcript of \(id)")
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(
                        "subagent \(id), \(EngineWord.subagent(row.child["status"]?.stringValue ?? "unknown")), "
                        + "task \(row.child["task"]?.stringValue ?? "")")
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.purple.opacity(0.08))
            .cornerRadius(8)

            if let selected {
                SubagentTranscriptView(controller: controller, childId: selected) { self.selected = nil }
            }
        }
    }

    private func icon(for child: [String: JSONValue]) -> String {
        switch child["status"]?.stringValue {
        case "done": return "checkmark.circle.fill"
        case "needs_review": return "questionmark.circle.fill"
        case "failed": return "xmark.circle.fill"
        default: return "circle.dotted"
        }
    }

    private func tone(for child: [String: JSONValue]) -> Color {
        switch child["status"]?.stringValue {
        case "done": return .green
        case "needs_review": return .orange
        case "failed": return .red
        default: return .secondary
        }
    }
}

/// One child's transcript, a page at a time.
///
/// Paged rather than shown whole for the same reason the agent pages it: a transcript is not bounded by
/// what a panel can sensibly render, and a view that silently truncated would be the visual form of the
/// confident-wrong-output failure. The byte counts are shown so "there is more" is visible.
struct SubagentTranscriptView: View {
    @ObservedObject var controller: OrgController
    let childId: String
    let onClose: () -> Void

    private var text: String { controller.subagentTranscript["text"]?.stringValue ?? "" }
    private var more: Bool { controller.subagentTranscript["more"]?.boolValue ?? false }
    private var next: Int { controller.subagentTranscript["next_offset_bytes"]?.intValue ?? 0 }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Label("Transcript — \(childId)", systemImage: "doc.plaintext")
                    .font(.headline)
                Spacer()
                Button("Close", action: onClose)
                    .accessibilityLabel("Close the transcript")
            }
            ScrollView {
                Text(text.isEmpty ? "(nothing read yet)" : text)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 220)

            HStack(spacing: 10) {
                Text("\(controller.subagentTranscript["returned_bytes"]?.intValue ?? 0) of "
                     + "\(controller.subagentTranscript["total_bytes"]?.intValue ?? 0) bytes")
                    .font(.caption2).foregroundStyle(.secondary)
                if more {
                    Button("Read more") {
                        Task { await controller.loadSubagentTranscript(childId: childId, offset: next) }
                    }
                    .accessibilityLabel("Read the next part of the transcript")
                } else if !text.isEmpty {
                    Text("end of transcript").font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
            }
        }
        .padding(10)
        .background(Color.secondary.opacity(0.08))
        .cornerRadius(8)
    }
}

// MARK: - 2. Who is on what (the board)

/// The board: one row per unit of work, with its owner, its information flow and its progress.
struct FlowSection: View {
    @ObservedObject var controller: OrgController

    private var board: [String: JSONValue] { controller.flow }
    private var rows: [[String: JSONValue]] { controller.flowRows }
    private var handoffs: [[String: JSONValue]] { controller.flowHandoffs }
    private var counts: [String: JSONValue] { board["counts"]?.objectValue ?? [:] }

    /// Whether to show gates among the work. A gate is a node in the graph, so it belongs on the
    /// board, but it has no owner and no handoff — and its decision is already on the spine, so the
    /// default here is *off*: the board is about people and work, and the gate is not either.
    @State private var showGates = false

    private var visibleRows: [[String: JSONValue]] {
        showGates ? rows : rows.filter { $0["is_gate"]?.boolValue != true }
    }

    var body: some View {
        if rows.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Label(board["headline"]?.stringValue ?? "No work is assigned yet.",
                      systemImage: "arrow.triangle.branch")
                    .font(.callout.weight(.medium))
                Text("Set a goal and each piece of work appears here with the agent on it, what it "
                     + "received, and what it handed on.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        } else {
            VStack(alignment: .leading, spacing: 10) {
                metrics
                stuckNote
                Toggle("Show gates among the work", isOn: $showGates)
                    .toggleStyle(.checkbox)
                    .font(.caption)
                    .help("A gate is a step in the graph rather than work somebody owns")
                // Keyed on the row's own `node_id` — `FlowRow.as_dict` sets it from the work item's
                // node, and the board is built from the manifest, whose order is fixed. The index is
                // not: the toggle above adds and removes gate rows *in the middle* of this list, so
                // under `id: \.offset` every row after a gate was re-identified when the toggle moved.
                ForEach(visibleRows.map { (key: $0["node_id"]?.stringValue ?? "", row: $0) },
                        id: \.key) { item in
                    FlowRowView(row: item.row, words: controller.stopWords)
                }
                if !handoffs.isEmpty { handoffSection }
            }
        }
    }

    private var metrics: some View {
        HStack(spacing: 22) {
            Metric(label: "Work", value: "\(counts["total"]?.intValue ?? 0)", detail: "items")
            Metric(label: "Done", value: "\(counts["done"]?.intValue ?? 0)", tone: .green)
            Metric(label: "Working", value: "\(counts["working"]?.intValue ?? 0)", tone: .blue)
            Metric(label: "Waiting", value: "\(counts["waiting"]?.intValue ?? 0)")
            Metric(label: "Stuck", value: "\(stuckCount)", tone: stuckCount > 0 ? .red : .secondary)
            Metric(label: "Handoffs", value: "\(handoffs.count)")
        }
    }

    private var stuckCount: Int { counts["stuck"]?.intValue ?? 0 }

    /// The first stopped step, in the order the board reads — so the header names the row a person
    /// scrolling down would reach first, rather than one the engine counted and this view hides.
    ///
    /// `BoardStop` is the engine's own predicate (`engine/flow.py:278`), so "the first stopped row" here is
    /// the same row the engine picked when it wrote the board's `next`.
    private var firstStuck: [String: JSONValue]? {
        visibleRows.first { BoardStop.isStuck($0) }
    }

    /// The move the engine named for this board, or nil when the engine named none.
    ///
    /// One per board, not one per row: `_next_line` (`engine/flow.py:428`) runs the recovery for the
    /// *run* — re-running the graph gives the stopped node another attempt at the refused payload — and
    /// names the first stopped step in its sentence. So it is shown once, where the board names that
    /// step, rather than repeated under every stopped row naming a node that is not the one above it.
    private var boardNext: BoardNext? {
        BoardNext.parse(board["next"]?.stringValue ?? "")
    }

    /// The figure's own door: the first stopped step, named with its reason and the move that resolves it.
    ///
    /// The board is where a person arrives from the "Blocked" figure above it, which until now named
    /// the stopped step and stopped there — "which one, and what do I do?" was left to be found. This
    /// is also the only place a stopped *gate* can be pointed at, since the gate rows are hidden by
    /// default.
    @ViewBuilder
    private var stuckNote: some View {
        if firstStuck != nil || stuckCount > 0 {
            VStack(alignment: .leading, spacing: 3) {
                if let stuck = firstStuck {
                    let node = stuck["node_id"]?.stringValue ?? "a step"
                    let reason = boardStopReason(stuck, words: controller.stopWords)
                    // The reason is present for every stop this build knows (`boardStopReason`), and
                    // the sentence is still the node and the fact when it is not — what is never done
                    // is inventing a meaning for a token this build does not recognise.
                    Label(reason.map { "\(node) is stuck — \($0.text)" } ?? "\(node) is stuck",
                          systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(colour(for: stuck["tone"]?.stringValue ?? "warn"))
                        .lineLimit(2)
                        .help(reason?.help ?? "the engine reported this step as stopped")
                } else {
                    // No *visible* stopped row, but the engine counted one: the gate rows, which this
                    // board hides by default. Without this the figure above the toggle and the rows
                    // below it disagree with no explanation of why.
                    Text("The \(stuckCount) stopped step(s) here are gates — turn on “Show gates "
                         + "among the work” to see them.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                if let next = boardNext { nextMove(next) }
            }
        }
    }

    /// The engine's own next move: why it is the one, and the command itself.
    ///
    /// The command is selectable text rather than a button, and `BoardNext` says why: the console has
    /// no command that can perform it. Showing it is still the whole difference between "blocked on pm"
    /// and knowing what to do next.
    @ViewBuilder
    private func nextMove(_ next: BoardNext) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            if !next.why.isEmpty {
                Label(next.why, systemImage: "arrow.turn.down.right")
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                    .help("What the engine says resolves what this board is showing")
            }
            Text(next.command)
                .font(.system(.caption2, design: .monospaced))
                .textSelection(.enabled)
                .help("Run this where the engine lives — the console has no command that re-runs a graph")
        }
    }

    /// The transfers themselves, because "the information moved from A to B" is the point of a handoff
    /// — the row view says *that* it moved; this says what moved and how it ended.
    private var handoffSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Handoffs").font(.headline)
            Text("What crossed between two steps, and whether it arrived.")
                .font(.caption).foregroundStyle(.secondary)
            // Keyed on `handoff_id`, which `FlowHandoff.as_dict` supplies and the engine assigns to the
            // transfer itself. The board is folded from a tail-read of the trace, so the *front* of
            // this list moves as the window advances — the one change an index cannot express.
            ForEach(handoffs.map { (key: $0["handoff_id"]?.stringValue ?? "", handoff: $0) },
                    id: \.key) { row in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: symbol(for: row.handoff["tone"]?.stringValue ?? "info"))
                        .foregroundStyle(colour(for: row.handoff["tone"]?.stringValue ?? "info"))
                        .frame(width: 16)
                    VStack(alignment: .leading, spacing: 1) {
                        HStack(spacing: 6) {
                            Text(row.handoff["from_agent"]?.stringValue
                                 ?? row.handoff["from_node"]?.stringValue ?? "?")
                                .font(.system(.callout, design: .rounded).weight(.medium))
                            Image(systemName: "arrow.right").font(.caption2)
                                .foregroundStyle(.secondary)
                            Text(row.handoff["to_agent"]?.stringValue
                                 ?? row.handoff["to_node"]?.stringValue ?? "?")
                                .font(.system(.callout, design: .rounded).weight(.medium))
                            Text(EngineWord.handoff(row.handoff["state"]?.stringValue ?? ""))
                                .font(.caption2.monospaced())
                                .foregroundStyle(colour(for: row.handoff["tone"]?.stringValue ?? "info"))
                        }
                        if let summary = row.handoff["summary"]?.stringValue, !summary.isEmpty {
                            Text(summary).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        } else if let status = row.handoff["payload_status"]?.stringValue, !status.isEmpty {
                            Text(status).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    Spacer()
                }
                .padding(.vertical, 3)
                .padding(.horizontal, 8)
                .background(colour(for: row.handoff["tone"]?.stringValue ?? "info").opacity(0.06))
                .cornerRadius(6)
                .accessibilityElement(children: .combine)
            }
        }
    }

    private func colour(for tone: String) -> Color {
        switch tone {
        case "good": return .green
        case "warn": return .orange
        case "bad": return .red
        case "info": return .blue
        default: return .secondary
        }
    }

    private func symbol(for tone: String) -> String {
        switch tone {
        case "good": return "checkmark.circle.fill"
        case "warn": return "exclamationmark.triangle.fill"
        case "bad": return "xmark.octagon.fill"
        case "info": return "arrow.right.circle"
        default: return "circle.dotted"
        }
    }
}

/// Why a row is not done: the sentence, its tooltip, and whether it exists because the step stopped.
///
/// The last field is what decides how loudly it is said — a stopped step's reason is the one thing the
/// row exists to convey, while an ordinary summary is context — and it is a property of *which* of the
/// three sources the sentence came from rather than something a view can re-derive.
private struct BoardStopReason {
    let text: String
    let help: String
    let isStop: Bool
}

/// Why a board row is not done, as one sentence, with the raw token kept for the tooltip.
///
/// The order is the engine's own — `engine/flow.py:316` (`why_stopped`) reads the same three sources
/// in the same order — and each step is used only when the one before it said nothing:
///
/// 1. `blocked_by`, which is what the engine folded for this row.
/// 2. the node's `summary`, the agent's own words. (The engine's fold reaches this first, so it is
///    belt-and-braces here: a payload from a build that predates the fold still gets a sentence.)
/// 3. a gloss of the `verdict`, for a row whose engine recorded only a token. The runner's
///    `_apply_guardrail` leaves the node at `{status: blocked, verdict: guardrail-blocked}` with no
///    summary at all, so before the fold a stopped-at-the-edge row had *no* reason to show and printed
///    nothing but a token — the state a person reported as "I cannot tell what it means".
///
/// Nil when there is nothing honest to say: a verdict this build does not know is left to the row's
/// verdict column rather than given an invented sentence, which is the same rule `EngineWord` follows
/// for every other token.
///
/// Both the row and the board's header use it, so the same step cannot be described two ways on one
/// screen.
private func boardStopReason(_ payload: [String: JSONValue], words: StopWords) -> BoardStopReason? {
    if let reason = payload["blocked_by"]?.stringValue, !reason.isEmpty {
        return BoardStopReason(text: reason,
                               help: "the run's own recorded reason for this step",
                               isStop: true)
    }
    if let summary = payload["summary"]?.stringValue, !summary.isEmpty {
        return BoardStopReason(text: summary, help: summary, isStop: BoardStop.isStuck(payload))
    }
    let verdict = payload["verdict"]?.stringValue ?? ""
    let gloss = EngineWord.stop(verdict, words: words)
    guard !gloss.isEmpty else { return nil }
    return BoardStopReason(
        text: gloss,
        help: "this is a gloss of the engine's own verdict for the step: \(verdict)",
        isStop: true)
}

/// One unit of work on the board: its owner, its state, its information in and out, and — when it
/// stopped — why it did.
///
/// The reason is the row's job rather than the board's: a person reads rows, and "guardrail-blocked"
/// on one of them was unanswerable without it. The move that resolves it is the *board's*, because the
/// engine names one per board (`engine/flow.py:428`), so it is stated once at the top of the board
/// where the engine names the step it is about.
struct FlowRowView: View {
    let row: [String: JSONValue]
    /// The engine's stop-token vocabulary, from the report this row came out of (`controller.stopWords`).
    let words: StopWords

    private var tone: String { row["tone"]?.stringValue ?? "muted" }

    /// The row as one spoken sentence: who, what state, and why it stopped when it did.
    ///
    /// A property rather than an expression in the view: the chain of `??` fallbacks inside the
    /// `.accessibilityLabel` argument defeated the type checker ("unable to type-check this expression in
    /// reasonable time"), and the label is a value the row has, not a layout decision.
    private var accessibilityText: String {
        let reason = boardStopReason(row, words: words)
        return "\(row["node_id"]?.stringValue ?? "work"), "
            + "\(row["agent_name"]?.stringValue ?? "unassigned"), "
            + "\(EngineWord.board(row["status"]?.stringValue ?? "unknown"))"
            + (reason.map { ", \($0.text)" } ?? "")
    }

    private var colour: Color {
        switch tone {
        case "good": return .green
        case "warn": return .orange
        case "bad": return .red
        case "info": return .blue
        default: return .secondary
        }
    }

    private var symbol: String {
        switch tone {
        case "good": return "checkmark.circle.fill"
        case "warn": return "exclamationmark.triangle.fill"
        case "bad": return "xmark.octagon.fill"
        case "info": return "circle.fill"
        default: return "circle.dotted"
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol).foregroundStyle(colour).frame(width: 16)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 7) {
                    Text(row["node_id"]?.stringValue ?? "?")
                        .font(.system(.callout, design: .rounded).weight(.medium))
                    if let skill = row["skill"]?.stringValue, !skill.isEmpty {
                        Text(skill)
                            .font(.caption2.monospaced())
                            .foregroundStyle(.secondary)
                    }
                    if row["is_gate"]?.boolValue == true {
                        Text("gate:\(row["gate_kind"]?.stringValue ?? "?")")
                            .font(.caption2)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Color.secondary.opacity(0.15))
                            .cornerRadius(4)
                    }
                }
                // What moved: in from whom, out to whom. Absent when nothing has crossed yet, which is
                // itself information.
                HStack(spacing: 10) {
                    if let from = row["received_from"]?.stringValue, !from.isEmpty {
                        Label(from, systemImage: "arrow.down.left")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    if let to = row["sent_to"]?.stringValue, !to.isEmpty {
                        Label(to, systemImage: "arrow.up.right")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
                // Why it stopped. This is the row's half of the answer to "guardrail-blocked on pm":
                // the token means nothing on its own, and the engine's own reason for the row
                // (`blocked_by`) or its own gloss of the verdict is what says what happened. The
                // raw verdict stays in the column on the right, so the token is never lost — only
                // explained. What resolves it is the board's (`stuckNote`), because the engine names
                // one recovery per board rather than one per row.
                if let reason = boardStopReason(row, words: words) {
                    Text(reason.text)
                        .font(.caption)
                        .foregroundStyle(reason.isStop ? colour : .secondary)
                        .lineLimit(2)
                        .help(reason.help)
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(row["agent_name"]?.stringValue ?? "unassigned")
                    .font(.system(.callout, design: .rounded))
                    .foregroundStyle((row["agent_name"]?.stringValue ?? "").isEmpty
                                     ? .orange : .primary)
                Text(EngineWord.board(row["status"]?.stringValue ?? ""))
                    .font(.caption2.monospaced()).foregroundStyle(colour)
                if let verdict = row["verdict"]?.stringValue, !verdict.isEmpty {
                    Text(verdict).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            .frame(width: 130, alignment: .trailing)
        }
        .padding(8)
        .background(colour.opacity(0.06))
        .cornerRadius(6)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }
}

// MARK: - 3. Cost and capacity

/// What this is costing, how full the contexts are, and whether the machine is coping.
///
/// Three of the old full-screen panels folded into one collapsed section. The honesty rules are kept
/// exactly as they were, because they were the best thing about those panels:
///
/// - **"unknown" is never zero.** An unmeasured cost and an unmeasured cache are reported as unknown,
///   with the count of spans that did not report usage.
/// - **A dropped log line is shown**, not hidden.
/// - **A context reading is the engine's own figure**, and a node with no record says so rather than
///   drawing an empty bar.
struct UsageSection: View {
    @ObservedObject var controller: OrgController

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            cost
            Divider()
            context
            Divider()
            capacity
        }
    }

    // MARK: money

    private var cost: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Spend").font(.subheadline.weight(.medium))
            HStack(spacing: 22) {
                let cost = controller.costSummary
                Metric(label: "Runs", value: "\(cost["runs"]?.intValue ?? 0)")
                Metric(label: "Nodes", value: "\(cost["nodes"]?.intValue ?? 0)")
                // A measure of cost per success, not raw cost: a cheap failing run is not cheap.
                Metric(label: "Cost per success",
                       value: cost["cost_per_success_usd"]?.doubleValue
                           .map { String(format: "$%.4f", $0) } ?? "unknown",
                       detail: "the figure that matters")
                Metric(label: "Unmeasured spans",
                       value: "\(cost["cost_unreported_spans"]?.intValue ?? 0)",
                       detail: "their cost is unknown, not zero",
                       tone: (cost["cost_unreported_spans"]?.intValue ?? 0) > 0 ? .orange : .secondary)
                Metric(label: "Cache hit rate",
                       value: controller.cacheSummary["cache_hit_rate"]?.doubleValue
                           .map { String(format: "%.1f%%", $0 * 100) } ?? "unreported",
                       detail: "prompt prefix reused",
                       tone: controller.cacheSummary["cache_reported"]?.boolValue == true
                           ? .green : .secondary)
                if let saving = controller.cacheSummary["cache_saving_usd"]?.doubleValue {
                    Metric(label: "Saved by cache", value: String(format: "$%.4f", saving),
                           detail: "versus no cache at all")
                }
            }
            Text(controller.costDescription)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
            Text(controller.cacheDescription)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
            // The distinction is stated, not implied: an unmeasured total must never be read as free.
            Text("“unknown” means the provider reported no usage. It is not zero.")
                .font(.caption2).foregroundStyle(.secondary)
        }
    }

    // MARK: context

    /// How full each agent's context is — the question the old panel asked and never answered.
    private var context: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("How full the contexts are").font(.subheadline.weight(.medium))
            if controller.contextReadings.isEmpty {
                // Said plainly rather than drawn as a row of empty bars. The engine records a node's
                // saturation at each handoff boundary, so "nothing yet" is a true and useful answer —
                // while a 0% bar would look like a measurement nobody made.
                Text("No context has been measured yet. The engine records each step's own "
                     + "saturation as it hands work on, so this fills in as a run crosses between "
                     + "steps.")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("Each step's session, as it was last measured. The engine compacts at 70%, "
                     + "evicts at 85%, and rotates at 95%.")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                ForEach(controller.contextReadings) { reading in
                    ContextGauge(reading: reading)
                }
            }
        }
    }

    // MARK: capacity

    private var capacity: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("This machine").font(.subheadline.weight(.medium))
            HStack(spacing: 22) {
                Metric(label: "Log lines", value: "\(controller.logs.lines.count)",
                       detail: "of \(controller.logs.stats["capacity"] ?? 0) kept")
                Metric(label: "Dropped", value: "\(controller.logs.droppedCount)",
                       detail: "older lines discarded",
                       tone: controller.logs.hasDropped ? .orange : .secondary)
                Metric(label: "Last event", value: relativeTime,
                       detail: "engine liveness")
            }
            // **Whether this app can tell you anything at all.** The availability belongs here rather
            // than only in the record of an attempt: a person who has never seen a banner needs to be
            // able to find out why by looking, and the two silent states — this build cannot post one,
            // or the app is not allowed to — have different answers. It sits with the machine facts
            // because that is what it is: a permission of this process, not a property of the run.
            notificationRow
            // The paths are what a support conversation needs, so they are here rather than only in
            // Settings — and they are read-only text a person can copy.
            KeyValueRow(key: "runtime", value: controller.runtimeDescription)
            KeyValueRow(key: "project", value: controller.projectPath)
            KeyValueRow(key: "credentials", value: controller.credentialsPath)
            KeyValueRow(key: "skills", value: controller.libraryPath)
            if let error = controller.engineError {
                KeyValueRow(key: "error", value: error, tone: .red)
            }
            if controller.engineDiagnostics.count > 1 {
                // **The second buffer, and the one with no control of its own.** `engineDiagnostics`
                // holds up to 500 lines of the engine's stderr, and the only thing that could clear them
                // was the *terminal's* Clear menu — reachable only by showing the terminal, which this
                // app hides by default. So the block below accumulated in plain sight with no way to
                // remove it, which is exactly the complaint. The count is shown for the same reason: a
                // buffer that holds 500 lines and says "last 8" hides its own growth. The first entry is
                // the controller's own runtime line (the `runtime` row above), so it is not counted.
                HStack(spacing: 8) {
                    Text("Engine diagnostics — \(controller.engineDiagnostics.count - 1) line(s), "
                         + "last 8 shown")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("Clear") { controller.clearEngineDiagnostics() }
                        .controlSize(.small)
                        .help("Remove the engine's stderr lines from the console. The engine's own "
                              + "trace file is a different thing and is not touched.")
                        .accessibilityLabel("Clear the engine's stderr diagnostics")
                }
                Text(controller.engineDiagnostics.suffix(8).joined(separator: "\n"))
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            Text("Local models run one at a time by default: on Apple Silicon the GPU and CPU share "
                 + "one memory pool, so loading two at once swaps the whole machine rather than "
                 + "merely slowing this app.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var relativeTime: String {
        guard let last = controller.lastEventAt else { return "no events yet" }
        let seconds = Int(Date().timeIntervalSince(last))
        return seconds < 2 ? "just now" : "\(seconds)s ago"
    }

    /// Whether a banner is possible here, and what became of the last attempt — the two facts the
    /// console never showed anywhere.
    ///
    /// Three states, and the middle one is the point: a person who has never seen a banner cannot tell
    /// "the app is not allowed" from "nothing has needed me yet", and the *unavailable* case (a
    /// `swift run` build, which has no bundle for macOS to attach a notification to) has nothing to do
    /// with permission at all. The advice under it is the fix, in the one place a person would look
    /// for it.
    ///
    /// **And the control that empties them.** A delivered banner is the one message surface in this app
    /// that nothing else can clear: the terminal has its Clear menu, the engine's stderr lines have the
    /// button below, the status bar's notice has a dismiss button — and Notification Centre kept
    /// everything this app ever posted, including the stops a later event resolved. That is what the
    /// person meant by "no way to clean up any messages", so the control sits with the sentence that
    /// says whether there are any.
    @ViewBuilder
    private var notificationRow: some View {
        let state: (sentence: String, advice: String?, attention: Bool) = {
            if let outcome = controller.notificationOutcome {
                return (outcome.sentence, outcome.advice, outcome.needsAttention)
            }
            if !controller.notificationsAvailable {
                let unable = NotificationOutcome.unavailable()
                return (unable.sentence, unable.advice, true)
            }
            return ("nothing has been sent yet — permission is asked for at the first thing worth "
                    + "telling you", nil, false)
        }()
        VStack(alignment: .leading, spacing: 2) {
            KeyValueRow(key: "notifications", value: state.sentence,
                        tone: state.attention ? .orange : .secondary)
            if let advice = state.advice {
                Text(advice)
                    .font(.caption2).foregroundStyle(.secondary)
                    .padding(.leading, 98)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel(advice)
            }
            HStack(spacing: 8) {
                Button("Clear delivered notifications") { controller.clearDeliveredNotifications() }
                    .controlSize(.small)
                    // Disabled rather than hidden where this build cannot post: the row above already
                    // says why in the engine's absence of a bundle, and a control that vanished would
                    // make "cannot notify" look like "nothing to clear".
                    .disabled(!controller.notificationsAvailable)
                    .help("Take this app's banners back out of Notification Centre. Nothing in the "
                          + "console changes — the spine, the badge and the menu-bar panel still show "
                          + "anything that needs you.")
                    .accessibilityLabel("Clear the notifications AgentOrg delivered to Notification "
                                        + "Centre")
            }
            .padding(.leading, 98)
            .padding(.top, 2)
        }
    }
}

/// One node's context: its measured saturation, the band it falls in, and the window it is a fraction
/// of. Not a legend — a number.
struct ContextGauge: View {
    let reading: ContextReading

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 10) {
                Text(reading.node)
                    .font(.system(.callout, design: .rounded))
                    .frame(width: 120, alignment: .leading)
                Text(reading.handedTo.isEmpty ? "—" : "to \(reading.handedTo)")
                    .font(.caption.monospaced()).foregroundStyle(.secondary)
                    .frame(width: 140, alignment: .leading)
                Label(reading.label, systemImage: reading.band.tone.symbol)
                    .font(.callout.monospaced())
                    .foregroundStyle(reading.band.tone.colour)
                    .frame(width: 90, alignment: .leading)
                Text("of \(reading.window) tokens")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text(reading.band.meaning)
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            // The bar is drawn *under* the row rather than instead of the number, so the figure is
            // the primary reading and the bar is the shape of it.
            BandBar(saturation: reading.saturation)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "\(reading.node), context \(reading.label) full of "
            + "\(reading.window) tokens — \(reading.band.meaning)")
    }
}

/// The measurement against the engine's own 70/85/95 ladder, as a filled bar.
///
/// The segments are drawn from the *band's* own thresholds (`Band.lowerBound`), not from hardcoded
/// fractions, so the picture cannot drift from the rule the compactor applies.
struct BandBar: View {
    let saturation: Double

    var body: some View {
        GeometryReader { geometry in
            let width = geometry.size.width
            ZStack(alignment: .leading) {
                // The ladder, in outline: where the engine will act.
                HStack(spacing: 0) {
                    ForEach(Band.allCases, id: \.self) { band in
                        Rectangle()
                            .fill(band.tone.colour.opacity(0.16))
                            .frame(width: width * segmentWidth(band))
                    }
                }
                // The measurement.
                Rectangle()
                    .fill(Band.forSaturation(saturation).tone.colour)
                    .frame(width: max(2, width * min(1, max(0, saturation))))
            }
            .cornerRadius(3)
        }
        .frame(maxWidth: .infinity, minHeight: 10, maxHeight: 10)
        .accessibilityHidden(true)
    }

    /// The fraction of the width one band occupies, from the next band's start.
    private func segmentWidth(_ band: Band) -> Double {
        let start = band.lowerBound
        let next = Band.allCases.first { $0.lowerBound > start }?.lowerBound ?? 1.0
        return next - start
    }
}
