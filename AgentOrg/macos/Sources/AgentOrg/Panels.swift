//
//  Panels.swift
//  AgentOrg
//
//  The five panels, each answering exactly one question.
//
//  WHY FIVE AND NOT ONE RICH DASHBOARD
//  -----------------------------------
//  `observability-engineer` is explicit: a dashboard that needs interpreting has failed, and a dashboard
//  without a defined audience question is sprawl. Each panel here answers one question in its subtitle,
//  and neither the org-health panel nor any other exceeds twelve rows without a stated reason — because
//  the failure mode of an ops console is not too little information, it is too much.
//
//  Every panel degrades rather than fails: with the engine not running it says so plainly instead of
//  showing an empty table that looks like real data.

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
                Text("Launch it to see the roster, the run and what it is costing. "
                     + "Nothing is sent to a model until you do.")
            }
        } actions: {
            if controller.canLaunch {
                Button("Launch engine") { controller.launch() }
                    .buttonStyle(.borderedProminent)
                    .accessibilityLabel("Launch the engine")
            }
        }
    }
}

// MARK: - Org — "Who do I have, and is the org healthy?"

struct OrgPanel: View {
    @ObservedObject var controller: OrgController

    var body: some View {
        if controller.agents.isEmpty {
            EngineNotRunningView(controller: controller)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    HStack(spacing: 22) {
                        let health = controller.healthSummary
                        Metric(label: "Agents", value: "\(controller.agents.count)",
                               detail: "\(controller.rosterByTeam.count) team(s)")
                        Metric(label: "Healthy", value: "\(health["healthy"] ?? 0)",
                               detail: "routed normally", tone: .green)
                        Metric(label: "Degraded", value: "\(health["degraded"] ?? 0)",
                               detail: "deprioritised", tone: .orange)
                        Metric(label: "Quarantined", value: "\(health["quarantined"] ?? 0)",
                               detail: "removed from the pool",
                               tone: (health["quarantined"] ?? 0) > 0 ? .red : .secondary)
                    }
                    .padding(.bottom, 4)

                    ForEach(controller.rosterByTeam, id: \.team) { group in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(group.team).font(.headline)
                            ForEach(group.members, id: \.self) { agent in
                                AgentRow(agent: agent)
                            }
                        }
                        .padding(10)
                        .background(Color.secondary.opacity(0.06))
                        .cornerRadius(8)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

struct AgentRow: View {
    let agent: [String: JSONValue]

    private var state: String { agent["state"]?.stringValue ?? "idle" }

    /// Colour *and* a word. A state that exists only as a hue is unreadable to some people, so the text
    /// always carries the meaning and the colour reinforces it.
    private var stateTone: Color {
        switch state {
        case "working": return .blue
        case "quarantined": return .red
        case "blocked", "waiting": return .orange
        case "idle": return .green
        default: return .secondary
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            Text(agent["name"]?.stringValue ?? "?")
                .font(.system(.body, design: .rounded).weight(.medium))
                .frame(width: 110, alignment: .leading)
            Text(agent["title"]?.stringValue ?? "")
                .font(.caption).foregroundStyle(.secondary)
                .frame(width: 160, alignment: .leading)
            Text(state)
                .font(.caption.monospaced())
                .foregroundStyle(stateTone)
                .frame(width: 80, alignment: .leading)
            Text("\(agent["provider"]?.stringValue ?? "?")/\(agent["model"]?.stringValue ?? "?")")
                .font(.caption.monospaced()).foregroundStyle(.secondary)
            Spacer()
            if let skills = agent["skills"]?.arrayValue {
                Text(skills.compactMap { $0.stringValue }.joined(separator: ", "))
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "\(agent["name"]?.stringValue ?? "agent"), \(agent["title"]?.stringValue ?? ""), "
            + "\(state), model \(agent["model"]?.stringValue ?? "unknown")")
    }
}

// MARK: - Work — "Where is work stuck?"

struct ProgressPanel: View {
    @ObservedObject var controller: OrgController
    /// Sort order for the node table. Local view state: a sort is a way of looking at this panel, not
    /// a fact about the run, so it must not be published or persisted.
    @State private var nodeSort: [KeyPathComparator<NodeRow>] = [.init(\.name)]

    /// One node, projected into a type `Table` can sort.
    ///
    /// `Table` sorts on a `Comparable` value through a key path, and a `[String: JSONValue]` has no
    /// such key path — so the projection is what makes the table sortable at all. It also gives the
    /// columns one place to resolve their optional dictionary lookups instead of repeating `?? ""`.
    struct NodeRow: Identifiable, Hashable {
        let id: String
        let name: String
        let status: String
        let verdict: String

        init(_ payload: [String: JSONValue]) {
            self.name = payload["name"]?.stringValue ?? "?"
            self.status = payload["status"]?.stringValue ?? "pending"
            self.verdict = payload["verdict"]?.stringValue ?? ""
            self.id = name
        }
    }

    /// The rows, sorted by the table's current order.
    private var nodeRows: [NodeRow] {
        controller.nodes.map(NodeRow.init).sorted(using: nodeSort)
    }

    var body: some View {
        if controller.engineState == .idle || controller.runStatus.isEmpty {
            EngineNotRunningView(controller: controller)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    HStack(spacing: 22) {
                        Metric(label: "Phase", value: controller.runStatus["phase"]?.stringValue ?? "—",
                               detail: controller.pendingGate != nil ? "waiting on you" : nil,
                               tone: controller.pendingGate != nil ? .orange : .primary)
                        Metric(label: "Nodes", value: "\(controller.nodes.count)")
                        let done = controller.nodes.filter {
                            ["done", "skipped"].contains($0["status"]?.stringValue ?? "")
                        }.count
                        Metric(label: "Completed", value: "\(done)",
                               detail: "of \(controller.nodes.count)", tone: .green)
                        Metric(label: "Gaps",
                               value: "\(controller.runStatus["staffing_gaps"]?.arrayValue?.count ?? 0)",
                               detail: "unstaffed capabilities")
                    }

                    if controller.swarmRunning || !controller.swarmItems.isEmpty {
                        // A swarm belongs in the Work view because it *is* the work: N subagents on
                        // one job. Shown only when there is one, so the panel stays about the run.
                        VStack(alignment: .leading, spacing: 6) {
                            Label(controller.swarmRunning ? "Swarm running" : "Swarm",
                                  systemImage: "person.3.sequence.fill")
                                .font(.headline)
                                .foregroundStyle(controller.swarmRunning ? .blue : .secondary)
                            Text(controller.swarmDescription)
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                            ForEach(Array(controller.swarmItems.prefix(12).enumerated()),
                                    id: \.offset) { _, item in
                                HStack(spacing: 8) {
                                    Image(systemName: item["ok"]?.boolValue == true
                                          ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                                        .foregroundStyle(item["ok"]?.boolValue == true
                                                         ? .green : .orange)
                                    Text(item["item"]?.stringValue ?? "—")
                                        .font(.system(.caption, design: .monospaced))
                                        .lineLimit(1)
                                    if let error = item["error"]?.stringValue, !error.isEmpty {
                                        Text(error).font(.caption2)
                                            .foregroundStyle(.orange).lineLimit(1)
                                    }
                                }
                            }
                            if controller.swarmItems.count > 12 {
                                Text("…and \(controller.swarmItems.count - 12) more")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                    }

                    GoalSection(controller: controller)

                    SubagentSection(controller: controller)

                    if let gate = controller.pendingGate {
                        // The gate is the thing the Owner is here for, so it is shown first and in full.
                        VStack(alignment: .leading, spacing: 6) {
                            Label("Waiting on you", systemImage: "hand.raised.fill")
                                .font(.headline).foregroundStyle(.orange)
                            Text(gate["reason"]?.stringValue ?? "")
                                .font(.body)
                            if let requires = gate["requires"]?.arrayValue,
                               let present = gate["present"]?.arrayValue {
                                Text("requires: \(requires.compactMap { $0.stringValue }.joined(separator: ", "))")
                                    .font(.caption)
                                Text("present: \(present.compactMap { $0.stringValue }.joined(separator: ", "))")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.orange.opacity(0.1))
                        .cornerRadius(8)
                    }

                    if let graph = controller.proposedGraph {
                        VStack(alignment: .leading, spacing: 6) {
                            Label("Graph awaiting approval", systemImage: "point.topleft.down.curvedto.point.bottomright.up")
                                .font(.headline)
                            if let nodes = graph["nodes"]?.arrayValue {
                                Text(nodes.compactMap { $0.stringValue }.joined(separator: " → "))
                                    .font(.system(.caption, design: .monospaced))
                                    .textSelection(.enabled)
                            }
                            if let loop = graph["loops"]?.arrayValue?.first?.objectValue {
                                Text("loop \(loop["id"]?.stringValue ?? "?") · "
                                     + "max \(loop["max_iterations"]?.intValue ?? 0) iterations · "
                                     + "escalates to \(loop["escalate_to"]?.stringValue ?? "?")")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .padding(10)
                        .background(Color.blue.opacity(0.08))
                        .cornerRadius(8)
                    }

                    if !controller.nodes.isEmpty {
                        // A `Table`, not hand-rolled rows: it gives sortable columns, a real selection,
                        // correct row striping and keyboard navigation for free — which is what the
                        // framework-selection reference means by "80% of a typical macOS UI". Sorting
                        // is by `TableColumn` value, so clicking a header is the standard behaviour.
                        Table(nodeRows, sortOrder: $nodeSort) {
                            TableColumn("Node", value: \.name) { row in
                                Text(row.name)
                                    .font(.system(.caption, design: .monospaced))
                            }
                            .width(min: 110, ideal: 140)

                            TableColumn("Status", value: \.status) { row in
                                // Colour and the word together. A status that exists only as a hue is
                                // unreadable to some people, so the text always carries the meaning.
                                Label(row.status, systemImage: symbol(for: row.status))
                                    .foregroundStyle(tone(for: row.status))
                            }
                            .width(min: 110, ideal: 140)

                            TableColumn("Verdict", value: \.verdict) { row in
                                Text(row.verdict).foregroundStyle(.secondary)
                            }
                        }
                        .tableStyle(.inset(alternatesRowBackgrounds: true))
                        .frame(minHeight: 140)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private func tone(for status: String) -> Color {
        StatusTone.forStatus(status).colour
    }

    /// A glyph beside the status word, so the state reads at a glance without relying on colour.
    private func symbol(for status: String) -> String {
        StatusTone.forStatus(status).symbol
    }

    private func tone(for node: [String: JSONValue]) -> Color {
        tone(for: node["status"]?.stringValue ?? "")
    }
}

// MARK: - The goal — "is this run going to keep going?"

/// The durable goal, and the controls that make it start, stop and continue.
///
/// Shown in the Work panel because it answers the same question the panel does — *where is work stuck?*
/// — and because a goal that is paused for a budget reason is exactly that. The one thing this view must
/// never do is hide whether the loop will continue: `live` is the first fact, spelled out as well as
/// coloured.
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
        VStack(alignment: .leading, spacing: 6) {
            Label("Goal", systemImage: "target")
                .font(.headline)
                .foregroundStyle(live ? .green : .secondary)

            if objective.isEmpty {
                Text("No goal set. A goal keeps the run going past a finished model turn — it continues "
                     + "until the agent reports it done or blocked.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack(spacing: 8) {
                    TextField("What should be achieved?", text: $controller.goalDraft)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("Goal objective")
                    Button("Set and continue") {
                        Task { await controller.setGoal(controller.goalDraft, arm: true) }
                    }
                    .disabled(controller.goalDraft.isEmpty || controller.engineState != .running)
                    .accessibilityLabel("Set the goal and start working on it")
                }
            } else {
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
                               value: String(format: "$%.4f", spend["cost_usd"]?.doubleValue ?? 0))
                    }
                }

                Text(objective).font(.body).textSelection(.enabled)

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
                    Button("Pause") { Task { await controller.pauseGoal() } }
                        .disabled(!live)
                        .accessibilityLabel("Pause the goal")
                    Button("Resume") { Task { await controller.resumeGoal() } }
                        .disabled(live)
                        .help("Continue, granting a fresh budget slice; the totals are kept")
                        .accessibilityLabel("Resume the goal")
                    Button("Clear") { Task { await controller.clearGoal() } }
                        .accessibilityLabel("Clear the goal")
                    Spacer()
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.opacity(0.08))
        .cornerRadius(8)
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
                Label("Subagents", systemImage: "person.3.sequence.fill")
                    .font(.headline)

                ForEach(controller.subagents, id: \.self) { child in
                    let id = child["child_id"]?.stringValue ?? "?"
                    HStack(spacing: 10) {
                        Image(systemName: icon(for: child))
                            .foregroundStyle(tone(for: child))
                            .accessibilityHidden(true)
                        Text(id)
                            .font(.system(.caption, design: .monospaced))
                            .frame(width: 100, alignment: .leading)
                        Text(child["status"]?.stringValue ?? "—")
                            .font(.caption).foregroundStyle(tone(for: child))
                            .frame(width: 90, alignment: .leading)
                        Text(child["task"]?.stringValue ?? "")
                            .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        Spacer()
                        if let bytes = child["bytes"]?.intValue, bytes > 0 {
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
                        "subagent \(id), \(child["status"]?.stringValue ?? "unknown"), "
                        + "task \(child["task"]?.stringValue ?? "")")
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

// MARK: - Cost — "What is this costing?"

struct EconomicsPanel: View {
    @ObservedObject var controller: OrgController

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
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
                }

                Text(controller.costDescription)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)

                // Prompt caching, when the provider reports it. This is the line that makes the
                // cache visible: a hit rate nobody can see is a discount nobody can verify.
                HStack(spacing: 10) {
                    Metric(label: "Cache hit rate",
                           value: controller.cacheSummary["cache_hit_rate"]?.doubleValue
                               .map { String(format: "%.1f%%", $0 * 100) } ?? "unreported",
                           detail: "prompt prefix reused",
                           tone: controller.cacheSummary["cache_reported"]?.boolValue == true
                               ? .green : .secondary)
                    Metric(label: "Saved by cache",
                           value: controller.cacheSummary["cache_saving_usd"]?.doubleValue
                               .map { String(format: "$%.4f", $0) } ?? "unknown",
                           detail: "versus no cache at all")
                }
                Text(controller.cacheDescription)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)

                if let budget = controller.runStatus["cost"]?.objectValue,
                   let ceiling = budget["cost_usd"]?.doubleValue, ceiling > 0 {
                    Text("Measured spend: \(String(format: "$%.4f", ceiling))")
                        .font(.caption).foregroundStyle(.secondary)
                }

                // The distinction is stated, not implied: an unmeasured total must never be read as free.
                Text("A figure of \"unknown\" means the provider reported no usage. It is not zero.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

// MARK: - Context — "How full are the agents' contexts?"

struct ContextPanel: View {
    @ObservedObject var controller: OrgController

    var body: some View {
        if controller.agents.isEmpty {
            EngineNotRunningView(controller: controller)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text("Each agent's context is managed per agent: compacted along the ladder at "
                         + "70/85/95%, and rotated when compaction is exhausted or attention has decayed.")
                        .font(.caption).foregroundStyle(.secondary)

                    ForEach(controller.agents, id: \.self) { agent in
                        // Saturation is reported from the agent's own state; without a live figure the
                        // panel says so rather than showing 0%, which would be a lie.
                        ContextGauge(name: agent["name"]?.stringValue ?? "?",
                                     model: agent["model"]?.stringValue ?? "?",
                                     window: agent["context_window"]?.intValue,
                                     state: agent["state"]?.stringValue ?? "idle")
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

struct ContextGauge: View {
    let name: String
    let model: String
    let window: Int?
    let state: String

    var body: some View {
        HStack(spacing: 12) {
            Text(name).font(.system(.body, design: .rounded)).frame(width: 110, alignment: .leading)
            Text(model).font(.caption.monospaced()).foregroundStyle(.secondary)
                .frame(width: 200, alignment: .leading)
            if let window {
                Text("\(window) tokens").font(.caption).foregroundStyle(.secondary)
            } else {
                // An unknown window cannot be bound to an agent; saying so is more useful than a blank.
                Text("window unknown — cannot bind").font(.caption).foregroundStyle(.orange)
            }
            Spacer()
            // The bands, drawn as a bar so the three zones are visible rather than described.
            BandBar()
            Text(state).font(.caption).foregroundStyle(.secondary).frame(width: 80, alignment: .trailing)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(name) on \(model), \(window.map { "\($0) token window" } ?? "unknown window"), \(state)")
    }
}

/// The 70/85/95 ladder, drawn.
struct BandBar: View {
    var body: some View {
        GeometryReader { geometry in
            let width = geometry.size.width
            HStack(spacing: 0) {
                Rectangle().fill(Color.green.opacity(0.45)).frame(width: width * 0.70)
                Rectangle().fill(Color.yellow.opacity(0.55)).frame(width: width * 0.15)
                Rectangle().fill(Color.orange.opacity(0.6)).frame(width: width * 0.10)
                Rectangle().fill(Color.red.opacity(0.7)).frame(width: width * 0.05)
            }
            .cornerRadius(3)
        }
        .frame(width: 180, height: 10)
        // The thresholds are the meaning, so they are spoken rather than only drawn.
        .accessibilityLabel("Compaction ladder: healthy below 70%, warning to 85%, critical to 95%, overflow above")
    }
}

// MARK: - Resources — "Is the machine coping?"

struct ResourcesPanel: View {
    @ObservedObject var controller: OrgController

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 22) {
                    Metric(label: "Log lines", value: "\(controller.logs.lines.count)",
                           detail: "of \(controller.logs.stats["capacity"] ?? 0) kept")
                    Metric(label: "Dropped",
                           value: "\(controller.logs.droppedCount)",
                           detail: "older lines discarded",
                           tone: controller.logs.hasDropped ? .orange : .secondary)
                    Metric(label: "Last event", value: relativeTime,
                           detail: "engine liveness")
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text("Engine").font(.headline)
                    KeyValueRow(key: "state", value: controller.engineState.rawValue)
                    KeyValueRow(key: "runtime", value: runtimeDescription)
                    KeyValueRow(key: "project", value: controller.projectPath)
                    KeyValueRow(key: "credentials", value: controller.credentialsPath)
                    KeyValueRow(key: "library", value: controller.libraryPath)
                    if let error = controller.engineError {
                        KeyValueRow(key: "error", value: error, tone: .red)
                    }
                }

                if !controller.engineDiagnostics.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Engine diagnostics").font(.headline)
                        Text(controller.engineDiagnostics.suffix(12).joined(separator: "\n"))
                            .font(.system(.caption2, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text("Local models").font(.headline)
                    // The guidance that matters most on a laptop, stated where it will be read.
                    Text("Local model concurrency defaults to 1. On Apple Silicon the GPU and CPU share one "
                         + "memory pool, so loading two models at once causes system-wide swap rather than "
                         + "merely slowing this app.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var relativeTime: String {
        guard let last = controller.lastEventAt else { return "no events yet" }
        let seconds = Int(Date().timeIntervalSince(last))
        return seconds < 2 ? "just now" : "\(seconds)s ago"
    }

    private var runtimeDescription: String {
        controller.engineDiagnostics.first.map { String($0.dropFirst("runtime: ".count)) } ?? "unknown"
    }
}

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
