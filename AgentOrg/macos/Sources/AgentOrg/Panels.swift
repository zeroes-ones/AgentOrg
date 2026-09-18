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

// MARK: - Activity — "what is happening, why, and what do I do next?"

/// The one panel that answers the question a person actually asks of an autonomous org.
///
/// The engine produces a `activity` report — derived from the run checkpoint, the trace, the goal,
/// the children and the proposals — and this renders it in three moves: the **headline** (what is it
/// doing now), the **timeline** (how it got here), and the **next action** (what, if anything, is
/// waiting on you). That order is deliberate: a person opening this panel wants the present tense
/// first and the history second, not a log dump.
///
/// It degrades rather than fails: with no engine it shows the standard empty state, and with a fresh
/// workspace the engine's own headline says "nothing is running" rather than this inventing one.
struct ActivityPanel: View {
    @ObservedObject var controller: OrgController

    private var report: [String: JSONValue] { controller.activity }
    private var timeline: [[String: JSONValue]] {
        (report["timeline"]?.arrayValue ?? []).compactMap { $0.objectValue }
    }
    private var counts: [String: JSONValue] { report["counts"]?.objectValue ?? [:] }
    private var nextAction: [String: JSONValue] { report["next_action"]?.objectValue ?? [:] }

    var body: some View {
        if controller.engineState == .idle || report.isEmpty {
            EngineNotRunningView(controller: controller)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    headline
                    metrics
                    if !controller.staffingGaps.isEmpty {
                        staffingGaps
                    }
                    nextStep
                    timelineSection
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    /// The present tense: what is happening right now, in the engine's own words.
    private var headline: some View {
        let stop = report["stop_reason"]?.stringValue ?? ""
        let tone: Color = controller.pendingGate != nil ? .orange
            : (stop.isEmpty ? .primary : .red)
        return VStack(alignment: .leading, spacing: 4) {
            Label(report["headline"]?.stringValue ?? "—",
                  systemImage: controller.pendingGate != nil
                      ? "hand.raised.fill"
                      : (stop.isEmpty ? "dot.radiowaves.left.and.right" : "exclamationmark.triangle.fill"))
                .font(.title3.weight(.semibold))
                .foregroundStyle(tone)
                .textSelection(.enabled)
            if let objective = report["objective"]?.stringValue, !objective.isEmpty {
                Text(objective).font(.callout).foregroundStyle(.secondary).lineLimit(2)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.opacity(0.08))
        .cornerRadius(8)
        .accessibilityElement(children: .combine)
    }

    /// Progress and presence, which is what "where is it going" reduces to.
    private var metrics: some View {
        HStack(spacing: 22) {
            Metric(label: "Phase", value: report["phase"]?.stringValue ?? "—",
                   detail: report["running"]?.boolValue == true ? "running" : nil,
                   tone: report["running"]?.boolValue == true ? .blue : .primary)
            Metric(label: "Nodes", value: "\(counts["done"]?.intValue ?? 0) of \(counts["nodes"]?.intValue ?? 0)",
                   detail: "done", tone: .green)
            Metric(label: "Blocked", value: "\(counts["blocked"]?.intValue ?? 0)",
                   detail: "need attention",
                   tone: (counts["blocked"]?.intValue ?? 0) > 0 ? .red : .secondary)
            Metric(label: "In flight", value: "\(counts["in_flight"]?.intValue ?? 0)")
            let sub = counts["subagents"]?.intValue ?? 0
            let swarms = counts["swarms"]?.intValue ?? 0
            Metric(label: "Swarms", value: "\(swarms)",
                   detail: "\(sub) subagent(s)", tone: sub > 0 ? .blue : .secondary)
            Metric(label: "Continues", value: report["going"]?.objectValue?["continues"]?.boolValue == true ? "yes" : "no",
                   detail: report["goal"]?.objectValue?["pause_reason"]?.stringValue,
                   tone: report["going"]?.objectValue?["continues"]?.boolValue == true ? .green : .secondary)
        }
        .padding(.vertical, 2)
    }

    /// Unstaffed capabilities, because a plan needing somebody nobody holds is the commonest reason a
    /// run stalls before it starts.
    private var staffingGaps: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("Unstaffed capabilities", systemImage: "person.crop.circle.badge.questionmark")
                .font(.headline).foregroundStyle(.orange)
            ForEach(controller.staffingGaps, id: \.self) { gap in
                HStack(spacing: 8) {
                    Text(gap["skill"]?.stringValue ?? "?")
                        .font(.system(.caption, design: .monospaced))
                    Text(gap["reason"]?.stringValue ?? "")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.08))
        .cornerRadius(8)
    }

    /// The single next thing to do, when there is one. Nothing is shown when nothing is waiting, so
    /// the absence of this block is itself information: the org does not need you.
    @ViewBuilder
    private var nextStep: some View {
        let kind = nextAction["kind"]?.stringValue ?? "none"
        if kind != "none", let label = nextAction["label"]?.stringValue {
            VStack(alignment: .leading, spacing: 6) {
                Label("Next", systemImage: "arrow.forward.circle.fill")
                    .font(.headline)
                    .foregroundStyle(kind == "decide" ? .orange : .blue)
                Text(label).font(.body)
                if let detail = nextAction["detail"]?.stringValue, !detail.isEmpty {
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                // The action is offered as a button where the app can honestly perform it, and shown
                // as a command where it cannot — a button that silently does nothing is worse than a
                // line telling you what to run.
                HStack(spacing: 8) {
                    if kind == "resume" {
                        Button("Resume the goal") { Task { await controller.resumeGoal() } }
                            .buttonStyle(.borderedProminent)
                    } else if kind == "decide", controller.pendingGate != nil {
                        Button("Approve") { Task { await controller.approve() } }
                            .buttonStyle(.borderedProminent)
                        Button("Reject") { Task { await controller.reject() } }
                    }
                    if let command = nextAction["command"]?.stringValue, !command.isEmpty {
                        Text(command)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.blue.opacity(0.08))
            .cornerRadius(8)
        }
    }

    /// The history: what happened, newest last, so reading downward is reading forward in time.
    @ViewBuilder
    private var timelineSection: some View {
        if timeline.isEmpty {
            Text("No activity recorded yet. Set a goal or start a run and the timeline fills in.")
                .font(.caption).foregroundStyle(.secondary)
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Label("Timeline", systemImage: "clock.arrow.circlepath")
                    .font(.headline)
                ForEach(Array(timeline.enumerated()), id: \.offset) { _, entry in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: symbol(for: entry["tone"]?.stringValue ?? "info"))
                            .foregroundStyle(colour(for: entry["tone"]?.stringValue ?? "info"))
                            .accessibilityHidden(true)
                        Text(clock(entry["at"]?.stringValue ?? ""))
                            .font(.system(.caption2, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .frame(width: 64, alignment: .leading)
                        Text(entry["kind"]?.stringValue ?? "")
                            .font(.system(.caption2, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .frame(width: 78, alignment: .leading)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(entry["title"]?.stringValue ?? "")
                                .font(.caption)
                            if let detail = entry["detail"]?.stringValue, !detail.isEmpty {
                                Text(detail).font(.caption2).foregroundStyle(.secondary)
                                    .lineLimit(2)
                            }
                        }
                        Spacer()
                    }
                    // One row is one event; VoiceOver should read it as a sentence, not fragments.
                    .accessibilityElement(children: .combine)
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.secondary.opacity(0.06))
            .cornerRadius(8)
        }
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

// MARK: - Flow — "Who is working on what, and what crossed between them?"

/// The org board: one row per unit of work, with its owner, its information flow and its progress.
///
/// This is the panel that answers the question a person running an org actually asks when several
/// things are in flight at once — *who is on what, what did they hand over, and what came back* — as
/// opposed to the Activity panel's chronological story. Both are read from the engine, so the CLI's
/// `flow` and this panel cannot disagree.
struct FlowPanel: View {
    @ObservedObject var controller: OrgController

    private var board: [String: JSONValue] { controller.flow }
    private var rows: [[String: JSONValue]] { controller.flowRows }
    private var handoffs: [[String: JSONValue]] { controller.flowHandoffs }
    private var counts: [String: JSONValue] { board["counts"]?.objectValue ?? [:] }

    /// Whether to show gates among the work. A gate is a node in the graph, so it belongs on the
    /// board, but it has no owner and no handoff — a person watching progress may want it out of the
    /// way. A local view preference, so it is `@State` rather than published.
    @State private var showGates = true

    private var visibleRows: [[String: JSONValue]] {
        showGates ? rows : rows.filter { $0["is_gate"]?.boolValue != true }
    }

    var body: some View {
        if controller.engineState == .idle || board.isEmpty {
            EngineNotRunningView(controller: controller)
        } else if rows.isEmpty {
            emptyBoard
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    headline
                    metrics
                    Toggle("Show gates", isOn: $showGates)
                        .toggleStyle(.checkbox)
                        .font(.caption)
                    assignments
                    if !handoffs.isEmpty {
                        handoffSection
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    /// A workspace with no work yet is a normal answer, not an error — say so calmly.
    private var emptyBoard: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Label(board["headline"]?.stringValue ?? "No work is assigned yet.",
                      systemImage: "arrow.triangle.branch")
                    .font(.title3.weight(.semibold))
                Text("Set a goal, and each piece of work appears here with the agent on it, what it "
                     + "received, and what it handed on.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// The present tense: what the board says is happening right now.
    private var headline: some View {
        let stuck = counts["stuck"]?.intValue ?? 0
        let tone: Color = stuck > 0 ? .red : (counts["working"]?.intValue ?? 0) > 0 ? .blue : .green
        return VStack(alignment: .leading, spacing: 4) {
            Label(board["headline"]?.stringValue ?? "—",
                  systemImage: stuck > 0 ? "exclamationmark.triangle.fill" : "person.2.wave.2.fill")
                .font(.title3.weight(.semibold))
                .foregroundStyle(tone)
                .textSelection(.enabled)
            if let goal = board["goal"]?.stringValue, !goal.isEmpty {
                Text(goal).font(.callout).foregroundStyle(.secondary).lineLimit(2)
            }
            if let org = board["org_name"]?.stringValue, !org.isEmpty {
                Text("org \(org)").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.opacity(0.08))
        .cornerRadius(8)
        .accessibilityElement(children: .combine)
    }

    private var metrics: some View {
        HStack(spacing: 22) {
            Metric(label: "Work", value: "\(counts["total"]?.intValue ?? 0)", detail: "items")
            Metric(label: "Done", value: "\(counts["done"]?.intValue ?? 0)", tone: .green)
            Metric(label: "Working", value: "\(counts["working"]?.intValue ?? 0)", tone: .blue)
            Metric(label: "Waiting", value: "\(counts["waiting"]?.intValue ?? 0)")
            Metric(label: "Stuck", value: "\(counts["stuck"]?.intValue ?? 0)",
                   tone: (counts["stuck"]?.intValue ?? 0) > 0 ? .red : .secondary)
            Metric(label: "Handoffs", value: "\(handoffs.count)")
        }
        .padding(.vertical, 2)
    }

    /// One row per unit of work: its owner, its state, and where information came from and went.
    private var assignments: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Work and who owns it").font(.headline)
            ForEach(Array(visibleRows.enumerated()), id: \.offset) { _, row in
                FlowRowView(row: row)
            }
        }
    }

    /// The transfers themselves, because "the information moved from A to B" is the point of a handoff
    /// — the row view says *that* it moved; this says what moved and how it ended.
    private var handoffSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Handoffs").font(.headline)
            ForEach(Array(handoffs.enumerated()), id: \.offset) { _, handoff in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: symbol(for: handoff["tone"]?.stringValue ?? "info"))
                        .foregroundStyle(colour(for: handoff["tone"]?.stringValue ?? "info"))
                        .frame(width: 16)
                    VStack(alignment: .leading, spacing: 1) {
                        HStack(spacing: 6) {
                            Text(handoff["from_agent"]?.stringValue
                                 ?? handoff["from_node"]?.stringValue ?? "?")
                                .font(.system(.callout, design: .rounded).weight(.medium))
                            Image(systemName: "arrow.right").font(.caption2)
                                .foregroundStyle(.secondary)
                            Text(handoff["to_agent"]?.stringValue
                                 ?? handoff["to_node"]?.stringValue ?? "?")
                                .font(.system(.callout, design: .rounded).weight(.medium))
                            Text(handoff["state"]?.stringValue ?? "")
                                .font(.caption2.monospaced())
                                .foregroundStyle(colour(for: handoff["tone"]?.stringValue ?? "info"))
                        }
                        if let summary = handoff["summary"]?.stringValue, !summary.isEmpty {
                            Text(summary).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        } else if let status = handoff["payload_status"]?.stringValue, !status.isEmpty {
                            Text(status).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    Spacer()
                }
                .padding(.vertical, 3)
                .padding(.horizontal, 8)
                .background(colour(for: handoff["tone"]?.stringValue ?? "info").opacity(0.06))
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

/// One unit of work on the board: its owner, its state, and its information in and out.
struct FlowRowView: View {
    let row: [String: JSONValue]

    private var tone: String { row["tone"]?.stringValue ?? "muted" }

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
                if let reason = row["blocked_by"]?.stringValue, !reason.isEmpty {
                    Text(reason).font(.caption).foregroundStyle(colour).lineLimit(2)
                } else if let summary = row["summary"]?.stringValue, !summary.isEmpty {
                    Text(summary).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(row["agent_name"]?.stringValue ?? "unassigned")
                    .font(.system(.callout, design: .rounded))
                    .foregroundStyle((row["agent_name"]?.stringValue ?? "").isEmpty
                                     ? .orange : .primary)
                Text(row["status"]?.stringValue ?? "")
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
        .accessibilityLabel(
            "\(row["node_id"]?.stringValue ?? "work"), "
            + "\(row["agent_name"]?.stringValue ?? "unassigned"), "
            + "\(row["status"]?.stringValue ?? "unknown")")
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

                    MissionSection(controller: controller)

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

// MARK: - Portfolio — "which orgs am I running, and what is each doing?"

/// The several orgs one principal runs, side by side.
///
/// This is the panel that makes the real-world case visible: a person is not one org. They are the CEO
/// of one company, the founder of another, the chair of a third — each with its own agents, missions,
/// goals and budget. The panel lists them, shows each one's mission and spend, marks the active org,
/// and offers the two actions that matter: **run** an org (which the engine keeps running in parallel
/// with the others) and **switch** which org the rest of the app acts on.
///
/// The live picture is fetched when the panel appears, not on every poll, because gathering it builds
/// an orchestrator per org — the cost is wanted exactly when the panel is open, and not otherwise.
struct PortfolioPanel: View {
    @ObservedObject var controller: OrgController
    @State private var newOrgName: String = ""
    @State private var goalDraft: [String: String] = [:]

    var body: some View {
        if controller.engineState == .idle {
            EngineNotRunningView(controller: controller)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    header
                    if controller.hasPortfolio {
                        metrics
                        ForEach(controller.portfolioOrgs, id: \.self) { org in
                            orgCard(org)
                        }
                    } else {
                        emptyState
                    }
                    addOrg
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .task { await controller.loadPortfolioLive() }
        }
    }

    /// The principal, and the fact that everything below is theirs.
    private var header: some View {
        VStack(alignment: .leading, spacing: 3) {
            Label(controller.portfolioPrincipalName.isEmpty
                  ? "No principal yet"
                  : controller.portfolioPrincipalName,
                  systemImage: "person.crop.circle")
                .font(.title3.weight(.semibold))
            Text("The orgs this person runs. The engine keeps a fleet, so one org's run does not "
                 + "block another's.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.06))
        .cornerRadius(8)
        .accessibilityElement(children: .combine)
    }

    private var metrics: some View {
        let rollup = controller.portfolioLive["rollup"]?.objectValue
        let totals = rollup?["totals"]?.objectValue
        let fleet = controller.portfolioLive["fleet"]?.objectValue
        return HStack(spacing: 22) {
            Metric(label: "Orgs", value: "\(totals?["orgs"]?.intValue ?? controller.portfolioOrgs.count)")
            Metric(label: "Running", value: "\(totals?["running"]?.intValue ?? 0)",
                   detail: "at once: \(fleet?["max_concurrent_orgs"]?.intValue ?? 0)", tone: .blue)
            Metric(label: "Needs you", value: "\(totals?["waiting"]?.intValue ?? 0)",
                   detail: "gates", tone: (totals?["waiting"]?.intValue ?? 0) > 0 ? .orange : .secondary)
            Metric(label: "Blocked", value: "\(totals?["blocked"]?.intValue ?? 0)",
                   tone: (totals?["blocked"]?.intValue ?? 0) > 0 ? .red : .secondary)
            Metric(label: "Spend", value: String(format: "$%.2f", totals?["spend_usd"]?.doubleValue ?? 0),
                   detail: "across loaded orgs")
            if controller.portfolioLoading {
                ProgressView().controlSize(.small)
            }
        }
    }

    /// One org: its identity, what it is doing, and the two actions.
    private func orgCard(_ org: [String: JSONValue]) -> some View {
        let id = org["id"]?.stringValue ?? ""
        let row = controller.orgRow(id)
        let enabled = org["enabled"]?.boolValue ?? true
        let isActive = id == controller.activeOrgId
        let running = row["running"]?.boolValue ?? false
        let headline = row["headline"]?.stringValue ?? (org["exists"]?.boolValue == true ? "loaded" : "")
        let tone: Color = !enabled ? .secondary
            : (row["waiting_host"]?.boolValue == true ? .orange
               : ((row["blocked"]?.intValue ?? 0) > 0 ? .red : (running ? .blue : .primary)))
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: isActive ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(isActive ? .green : .secondary)
                    .accessibilityHidden(true)
                Text(org["name"]?.stringValue ?? "?")
                    .font(.headline)
                Text(org["slug"]?.stringValue ?? "")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                if !enabled {
                    Text("disabled").font(.caption2).foregroundStyle(.secondary)
                }
                if isActive {
                    Text("active").font(.caption2).foregroundStyle(.green)
                }
                Spacer()
                if let spend = row["spend_usd"]?.doubleValue {
                    Text(String(format: "$%.4f", spend)).font(.caption).foregroundStyle(.secondary)
                }
            }
            if let charter = org["charter"]?.stringValue, !charter.isEmpty {
                Text(charter).font(.caption).foregroundStyle(.secondary)
            }
            if !headline.isEmpty {
                Text(headline).font(.caption).foregroundStyle(tone)
            }
            if let mission = row["mission"]?.stringValue, !mission.isEmpty {
                Text("mission: \(mission)").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            if let on = row["objective_now"]?.stringValue, !on.isEmpty {
                Text("on: \(on)").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            if org["exists"]?.boolValue == false {
                Text("folder not found: \(org["path"]?.stringValue ?? "")")
                    .font(.caption2).foregroundStyle(.orange).lineLimit(1)
            }

            HStack(spacing: 8) {
                TextField("Goal for \(org["name"]?.stringValue ?? "this org")",
                          text: binding(for: id))
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Goal for \(org["name"]?.stringValue ?? "this org")")
                Button("Run") {
                    let goal = goalDraft[id] ?? ""
                    goalDraft[id] = ""
                    Task { await controller.runOrg(id, goal: goal) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!enabled || controller.engineState != .running)
                .help("Start a run in this org, in parallel with any other org already running")
                if running {
                    Button("Stop") { Task { await controller.stopOrg(id) } }
                        .help("Ask this org's run to pause at its next node boundary")
                }
                if !isActive {
                    Button("Switch to") { Task { await controller.selectOrg(id) } }
                        .help("Make this the org the rest of the app acts on")
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.opacity(0.06))
        .cornerRadius(8)
    }

    private func binding(for id: String) -> Binding<String> {
        Binding(get: { goalDraft[id] ?? "" }, set: { goalDraft[id] = $0 })
    }

    /// The empty state, and the one step to fix it.
    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("No orgs registered", systemImage: "building.2")
                .font(.headline).foregroundStyle(.secondary)
            Text("A portfolio is the person and the several orgs they run. Add the first org below — "
                 + "point it at a folder, and it gets its own agents, missions and budget.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.06))
        .cornerRadius(8)
    }

    private var addOrg: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Register an org", systemImage: "plus.rectangle.on.folder")
                .font(.headline)
            HStack(spacing: 8) {
                TextField("Org name, e.g. Tesla", text: $newOrgName)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("New org name")
                Button("Add") {
                    let name = newOrgName
                    newOrgName = ""
                    Task { await controller.addOrg(name: name) }
                }
                .disabled(newOrgName.isEmpty || controller.engineState != .running)
                .help("Register the org; set its folder from the CLI with `portfolio update`")
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.blue.opacity(0.06))
        .cornerRadius(8)
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
            Label("Mission", systemImage: "flag.checkered")
                .font(.headline)
                .foregroundStyle(live ? .green : .secondary)

            if statement.isEmpty {
                Text("No mission set. A mission is the standing purpose above the goal — an ordered "
                     + "set of objectives worked one at a time, so a long-running org reads as one "
                     + "effort rather than unrelated runs.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack(spacing: 8) {
                    TextField("What is this all for?", text: $draft)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("Mission statement")
                    Button("Set the mission") {
                        Task { await controller.setMission(draft) }
                    }
                    .disabled(draft.isEmpty || controller.engineState != .running)
                    .accessibilityLabel("Set the mission")
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
                    ForEach(Array(objectives.enumerated()), id: \.offset) { index, objective in
                        objectiveRow(index: index, objective: objective)
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
                    Button(live ? "Pause" : "Arm") {
                        Task { live ? await controller.pauseMission() : await controller.armMission() }
                    }
                    .help("Arming the mission does not spend — `Start` on an objective does")
                    Spacer()
                    if !nowText.isEmpty {
                        // Starting an objective is what hands it to a goal; the label says so, because
                        // a button that begins a spend must not be ambiguous.
                        Button("Start “\(nowText.prefix(24))”") {
                            Task { await controller.startObjective() }
                        }
                        .buttonStyle(.borderedProminent)
                        .help("Set a goal for the active objective and begin working it")
                    } else if progress["next"]?.stringValue?.isEmpty == false {
                        Button("Start next") { Task { await controller.startObjective() } }
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

                // Autonomy is why a gate did or did not stop the run, so it belongs next to the state
                // rather than buried in a config file. Read from the goal itself, not the defaults, so
                // this shows what *this* objective chose.
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

    /// The goal's autonomy, as two labels and the switch that restores the human gate.
    ///
    /// Shown, not just configurable: when a run passed a gate the person might have wanted to see, the
    /// reason has to be on the same panel as the state — otherwise the app "did something without me"
    /// with no explanation anywhere.
    @ViewBuilder
    private var autonomyControls: some View {
        let policy = controller.goal["policy"]?.objectValue
        let humanGate = policy?["human_gate"]?.boolValue ?? false
        let decides = controller.goal["decides_gates"]?.boolValue ?? false
        let staffs = controller.goal["staffs_gaps"]?.boolValue ?? false
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 12) {
                Label(decides ? "gates: auto" : "gates: human",
                      systemImage: decides ? "arrow.right.circle" : "hand.raised.fill")
                    .font(.caption)
                    .foregroundStyle(decides ? .blue : .orange)
                Label(staffs ? "gaps: auto" : "gaps: report",
                      systemImage: staffs ? "person.badge.plus" : "person.crop.circle.badge.exclamationmark")
                    .font(.caption)
                    .foregroundStyle(staffs ? .blue : .orange)
                Spacer()
                // The one-click escape hatch: "I want to be involved from here on".
                Button(humanGate ? "Autonomy on" : "Human gate on") {
                    Task { await controller.setGoalHumanGate(!humanGate) }
                }
                .controlSize(.small)
                .help(humanGate
                      ? "Let the org pass the gates it can decide"
                      : "Stop at every gate — you decide, the org does not")
                .accessibilityLabel(humanGate
                                    ? "Turn autonomy on: the org may pass its own gates"
                                    : "Turn the human gate on: stop at every gate")
            }
            Text("A release, close or spend gate is always yours.")
                .font(.caption2).foregroundStyle(.secondary)
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
