//
//  RunsPane.swift
//  AgentOrg
//
//  The Runs destination: what has run here, and what did it leave behind.
//
//  WHY THIS IS A DESTINATION
//  -------------------------
//  This is the only part of the app that works with the engine **stopped**, and that is exactly the
//  moment a person most wants it: the run ended, and they want to know what it did. Everything here
//  reads `.agent_state/` through `RunStateBrowser`, which reads through `WorkspaceWriter`, so every
//  path is containment-checked exactly as the rest of the app's file access is.
//
//  Three old panels live here, re-homed rather than deleted:
//
//  1. **Runs on disk** — the old History panel: the checkpoint, every handoff, and the cache store.
//  2. **Sessions** — `engine.cli session list`, which the GUI never surfaced at all: what the engine
//     archived, per agent, on this machine.
//  3. **Self-checks** — the old Improve panel: what the system thinks is wrong with itself, and the
//     boundary that refused some of it.
//
//  The live view of a run is on Now. The two used to be separate sidebar rows showing the same run
//  from two angles, which is how a person ends up unsure which one is "current".

import SwiftUI
import AppKit
import AgentOrgKit

struct RunsPane: View {
    @ObservedObject var controller: OrgController
    @AppStorage("runs.showSessions") private var showSessions = false
    @AppStorage("runs.showSchedules") private var showSchedules = false
    @AppStorage("runs.showProposals") private var showProposals = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                header
                SectionCard(title: RunsSection.onDisk.rawValue, symbol: RunsSection.onDisk.symbol,
                            summary: RunsSection.onDisk.summary) {
                    DiskRunsSection(controller: controller)
                }
                SectionCard(title: RunsSection.schedules.rawValue, symbol: RunsSection.schedules.symbol,
                            summary: RunsSection.schedules.summary, expanded: $showSchedules) {
                    SchedulesSection(controller: controller)
                }
                SectionCard(title: RunsSection.sessions.rawValue, symbol: RunsSection.sessions.symbol,
                            summary: RunsSection.sessions.summary, expanded: $showSessions) {
                    SessionsSection(controller: controller)
                }
                SectionCard(title: RunsSection.proposals.rawValue, symbol: RunsSection.proposals.symbol,
                            summary: RunsSection.proposals.summary, expanded: $showProposals) {
                    ProposalsSection(controller: controller)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task {
            // Reload on appearance rather than relying on the poll: with the engine down there is no
            // poll, and a pane that only refreshed on a live stream would show whatever it last saw.
            await controller.refreshOfflineState()
            // The schedule is not offline-readable (it lives behind `_cmd_schedules`, which needs the
            // workspace the server holds), so it comes from the engine and is simply absent when it is
            // not running — the section says which of the two it is looking at.
            if controller.engineState == .running { await controller.loadSchedules() }
        }
    }

    /// What this destination is reading, and one control to re-read it.
    ///
    /// The state of the engine is stated rather than assumed: "the engine is not running, so this is
    /// everything it left" is a different sentence from "this is current", and a person needs to know
    /// which one they are looking at.
    ///
    /// It also carries the proposal count, because the **sidebar badges this destination with that
    /// number** — and a badge whose destination says nothing about it leaves the person to hunt for what
    /// the badge meant, behind a section that starts collapsed.
    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Label(controller.engineState.isLive
                      ? "Read from the project as the run goes"
                      : "Read from the project with the engine stopped",
                      systemImage: controller.engineState.isLive
                          ? "externaldrive.badge.checkmark" : "bolt.horizontal.circle")
                    .font(.headline)
                Spacer()
                Button("Refresh from disk") { Task { await controller.refreshOfflineState() } }
                    .controlSize(.small)
                    .accessibilityLabel("Re-read the run state from disk")
            }
            Text("Everything here comes from .agent_state/ inside the project, so it stays correct "
                 + "after the engine stops.")
                .font(.caption).foregroundStyle(.secondary)
            if !controller.proposals.isEmpty {
                HStack(spacing: 8) {
                    Label("\(controller.proposals.count) proposal(s) are waiting for your decision.",
                          systemImage: "hand.raised.fill")
                        .font(.caption).foregroundStyle(.blue)
                    Spacer()
                    if !showProposals {
                        Button("Show them") { showProposals = true }
                            .controlSize(.small)
                            .help("Open the Self-checks section, where each proposal can be read")
                            .accessibilityLabel("Open the Self-checks section")
                    }
                }
            }
            if let error = controller.offlineError {
                // A refused read is stated rather than rendered as an empty list: "nothing has run"
                // and "the state directory could not be read" look identical in a table, and only one
                // of them is a problem.
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.orange)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.08))
        .cornerRadius(8)
    }
}

// MARK: - 1. Runs on disk

/// The old History panel: the checkpoint, the handoffs, and the cache store.
struct DiskRunsSection: View {
    @ObservedObject var controller: OrgController
    /// The discard confirmation is on screen. Held here rather than per row so a poll's re-render of
    /// the checkpoint rows cannot dismiss a dialog the person is still reading.
    @State private var pendingDiscard = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            // The engine's account of the last discard, rendered from its reply rather than from a
            // sentence written here — and drawn *outside* the "nothing on disk" branch, because a
            // discard is most often what leaves the panel with no run to show.
            discardResult
            if !controller.hasOfflineState {
                ContentUnavailableView {
                    Label("Nothing on disk yet", systemImage: "externaldrive")
                } description: {
                    Text("A run writes its progress, its handoffs and its cache record into "
                         + "the project's .agent_state/ folder as it goes. Until something has run "
                         + "there is nothing to show.")
                }
            } else {
                runSection
                handoffSection
                cacheSection
            }
        }
        // **The confirmation says what is kept and where the backup goes**, which is the whole reason
        // this is safe to offer where "Forget" a schedule is not: `discard` moves the two checkpoints
        // rather than deleting them. The buttons copy the schedule section's shape — a destructive
        // role on the act, a cancel beside it — and the engine's own refusal for a live run is left
        // to surface (it would be a second liveness rule to pre-empt it here).
        .alert("Discard this settled run?", isPresented: $pendingDiscard) {
            Button("Discard it", role: .destructive) {
                Task { await controller.discardRun() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(discardMessage)
        }
    }

    /// What a discard does, in the terms the engine implements it: the two checkpoints move, the
    /// record stays. The *result* is not described here — the count, the moved entries and the backup
    /// path come from `controller.lastDiscard`, the engine's reply.
    private var discardMessage: String {
        "run_state.json and runner_state.json are moved — never deleted — into a timestamped folder "
        + "under .agent_state/discarded/, so the board stops reporting a node nobody can resolve. "
        + "Everything else is kept: the trace, the handoffs, the ledger, the goal and the cache. "
        + "A run still in flight is refused."
    }

    /// The engine's reply to the last discard, shown verbatim where a person needs it: how many
    /// checkpoints moved, where the backup is, and what was kept.
    @ViewBuilder
    private var discardResult: some View {
        let report = controller.lastDiscard
        if !report.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                if report["discarded"]?.boolValue == true {
                    Label("Discarded \(report["moved"]?.arrayValue?.count ?? 0) checkpoint file(s)",
                          systemImage: "archivebox")
                        .font(.caption).foregroundStyle(.secondary)
                    if let backup = report["backup_dir"]?.stringValue, !backup.isEmpty {
                        // The engine's path, verbatim and selectable: it is the recovery route.
                        Text("backup: \(backup)")
                            .font(.system(.caption2, design: .monospaced))
                            .textSelection(.enabled)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let kept = report["kept"]?.arrayValue, !kept.isEmpty {
                        Text("kept: " + kept.compactMap { $0.stringValue }.joined(separator: ", "))
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                } else if let reason = report["reason"]?.stringValue, !reason.isEmpty {
                    // The no-op's stated reason, passed through: "already clean" must not read as a
                    // failure, and an empty table would not say which it was.
                    Text(reason).font(.caption2).foregroundStyle(.secondary)
                }
            }
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.secondary.opacity(0.06))
            .cornerRadius(6)
            .accessibilityElement(children: .combine)
        }
    }

    // MARK: the run

    @ViewBuilder
    private var runSection: some View {
        if let run = controller.offlineRuns.first {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("The last run").font(.subheadline.weight(.medium))
                    StatusLabel(status: run.phase)
                    Spacer()
                    if !run.updated.isEmpty {
                        Text(run.updated).font(.caption2).foregroundStyle(.secondary)
                    }
                    // The cleanup control the pane was missing: a stuck run could be read here and
                    // acted on nowhere, so a gate the person could not resolve stayed on the board for
                    // ever. It goes through the engine (`discard_run`) rather than moving files from
                    // the app, so the app and the terminal share one implementation and one liveness
                    // rule.
                    Button(role: .destructive) { pendingDiscard = true } label: {
                        Label("Discard run", systemImage: "trash")
                    }
                    .controlSize(.small)
                    .disabled(controller.engineState != .running)
                    .help(controller.engineState == .running
                          ? "Move this run's checkpoints out of the way, so the board stops reporting "
                              + "it. Recoverable — the backup path is shown."
                          : "Discarding is an engine operation, and the engine is not running.")
                    .accessibilityLabel("Discard this settled run's checkpoints")
                }
                HStack(spacing: 22) {
                    Metric(label: "Workflow", value: run.workflow)
                    Metric(label: "Nodes", value: "\(run.nodes.count)")
                    Metric(label: "Blocked", value: "\(run.blockedCount)",
                           tone: run.blockedCount > 0 ? .red : .secondary)
                    Metric(label: "Last node", value: run.node.isEmpty ? "—" : run.node)
                }
                if run.nodes.isEmpty {
                    Text("This run has not reached a node yet.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                // Keyed on the node's name, which is the checkpoint's own key for a node — the same key
                // `status.outcome.nodes` uses. The order here is alphabetical (`lastRun` sorts it), so
                // the index is only *accidentally* stable: one node arriving mid-run re-identifies
                // every row sorted after it.
                ForEach(run.nodes, id: \.name) { node in
                    HStack(spacing: 10) {
                        StatusLabel(status: node.status)
                            .font(.caption)
                            .frame(width: 130, alignment: .leading)
                        Text(node.name)
                            .font(.system(.caption, design: .monospaced))
                            .frame(width: 130, alignment: .leading)
                        if !node.verdict.isEmpty {
                            Text(node.verdict).font(.caption2).foregroundStyle(.secondary)
                        }
                        if node.iterations > 1 {
                            Text("\(node.iterations)× pass(es)").font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("node \(node.name): \(node.status) \(node.verdict)")
                }
            }
        }
    }

    // MARK: handoffs

    private var handoffSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Handoffs").font(.subheadline.weight(.medium))
                Text("\(controller.offlineHandoffs.count)").font(.caption).foregroundStyle(.secondary)
                Spacer()
            }
            if controller.offlineHandoffs.isEmpty {
                Text("Nothing has crossed between two steps yet.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(controller.offlineHandoffs, id: \.id) { handoff in
                HandoffRowView(handoff: handoff,
                               selected: controller.offlineHandoffDetail?.id == handoff.id) {
                    controller.inspectHandoff(handoff.id)
                }
            }
            if let detail = controller.offlineHandoffDetail {
                HandoffDetailView(handoff: detail)
            }
        }
    }

    // MARK: the cache store

    @ViewBuilder
    private var cacheSection: some View {
        if !controller.offlinePrefixes.isEmpty
            || !controller.offlineShapes.isEmpty
            || !controller.offlineSavings.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("The prompt cache").font(.subheadline.weight(.medium))
                Text("Read from cache/ — the durable record of what the prefix cache did, which is what "
                     + "makes “is this run cache-warm?” answerable after a restart.")
                    .font(.caption2).foregroundStyle(.secondary)
                HStack(spacing: 22) {
                    Metric(label: "Pinned prefixes", value: "\(controller.offlinePrefixes.count)")
                    Metric(label: "Shape observations", value: "\(controller.offlineShapes.count)")
                    Metric(label: "Requests", value: "\(controller.offlineSavings.count)")
                }
                ForEach(controller.offlinePrefixes.prefix(12), id: \.id) { prefix in
                    HStack(spacing: 10) {
                        Text(String(prefix.id.prefix(12)))
                            .font(.system(.caption2, design: .monospaced))
                            .frame(width: 90, alignment: .leading)
                        Text(prefix.skill.isEmpty ? "—" : prefix.skill)
                            .font(.caption).frame(width: 130, alignment: .leading)
                        Text(prefix.source).font(.caption2).foregroundStyle(.secondary)
                            .frame(width: 70, alignment: .leading)
                        Text("\(prefix.observations)×").font(.caption2)
                        Spacer()
                        Text("\(prefix.chars) chars").font(.caption2).foregroundStyle(.secondary)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(
                        "prefix \(prefix.id), skill \(prefix.skill), seen \(prefix.observations) times")
                }
                if controller.offlinePrefixes.count > 12 {
                    Text("and \(controller.offlinePrefixes.count - 12) more")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
    }
}

/// One handoff in the offline browser, as a selectable row.
struct HandoffRowView: View {
    let handoff: HandoffSummary
    let selected: Bool
    let onSelect: () -> Void

    private var tone: StatusTone {
        switch handoff.state {
        case "FULFILLED", "VERIFIED": return .ok
        case "REJECTED", "BREACHED": return .bad
        case "ESCALATED": return .attention
        case "IN_PROGRESS", "ACCEPTED": return .active
        default: return .neutral
        }
    }

    var body: some View {
        Button(action: onSelect) {
            HStack(spacing: 8) {
                Image(systemName: tone.symbol).foregroundStyle(tone.colour)
                    .accessibilityHidden(true)
                Text(handoff.origin.isEmpty ? "?" : handoff.origin)
                    .font(.system(.caption, design: .monospaced))
                    .frame(width: 110, alignment: .leading)
                Image(systemName: "arrow.right").font(.caption2).foregroundStyle(.secondary)
                Text(handoff.target.isEmpty ? "?" : handoff.target)
                    .font(.system(.caption, design: .monospaced))
                    .frame(width: 110, alignment: .leading)
                Text(EngineWord.handoff(handoff.state)).font(.caption2).foregroundStyle(tone.colour)
                    .frame(width: 90, alignment: .leading)
                Text(handoff.summary).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                if handoff.attempt > 1 {
                    Text("attempt \(handoff.attempt)").font(.caption2).foregroundStyle(.orange)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.vertical, 2)
        .background(selected ? Color.accentColor.opacity(0.12) : Color.clear)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "handoff \(handoff.origin) to \(handoff.target), \(EngineWord.handoff(handoff.state)), "
            + "\(handoff.summary)")
        .accessibilityHint("Shows what this crossing carried")
    }
}

/// One handoff's payload, which is the reason the typed handoff exists.
struct HandoffDetailView: View {
    let handoff: HandoffSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(handoff.id)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
            if handoff.needsAttention {
                // The two states that mean the crossing did not complete are stated, not left for the
                // reader to infer from a word in a table.
                Label("This handoff did not complete (\(handoff.state)).",
                      systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.red)
            }
            KeyValueRow(key: "status", value: handoff.status)
            KeyValueRow(key: "kind", value: handoff.kind)
            KeyValueRow(key: "created", value: handoff.createdAt)
            if !handoff.artifacts.isEmpty {
                KeyValueRow(key: "artifacts", value: handoff.artifacts.joined(separator: ", "))
            }
            if !handoff.openQuestions.isEmpty {
                // R6 exists because unresolved questions compound; showing them is how a person sees
                // the uncertainty a successor inherited.
                KeyValueRow(key: "open", value: handoff.openQuestions.joined(separator: " · "),
                            tone: .orange)
            }
            // The context figure the node's session had reached when it handed on. Shown here as well
            // as in the Now pane's capacity section, because "this step was nearly full when it
            // passed work on" is part of reading the handoff.
            if handoff.hasContextReading,
               let saturation = handoff.sessionSaturation,
               let window = handoff.contextWindow {
                KeyValueRow(key: "context",
                            value: "\(Int((saturation * 100).rounded()))% of \(window) tokens "
                                + "(\(Band.forSaturation(saturation).meaning))")
            }
            KeyValueRow(key: "summary", value: handoff.summary)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.08))
        .cornerRadius(8)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Handoff \(handoff.id) detail")
    }
}

// MARK: - 2. Schedules

/// The objectives that fire on a clock, and the one action the CLI had and the GUI did not: forgetting
/// one.
///
/// `engine.cli schedules remove` has existed since schedules did; nothing in the app could reach it, so
/// a schedule added once was permanent from the console's point of view. This is that command's window,
/// including its refusals — the engine declines to guess which entry an ambiguous slug means, and that
/// refusal is shown rather than pre-empted, because "removing the wrong schedule is not a mistake that
/// can be undone" is the engine's own reason for being strict.
///
/// The section also states the thing a person is most likely to get wrong about schedules: the *file*
/// fires nothing. A watcher process has to be running (`schedules watch`), so a list of armed entries
/// with no watcher is not a list of things that will happen.
struct SchedulesSection: View {
    @ObservedObject var controller: OrgController
    /// The entry a person pressed Forget on, held while the confirmation is on screen.
    @State private var pendingRemoval: [String: JSONValue]?

    private var counts: [String: JSONValue] { controller.schedule["counts"]?.objectValue ?? [:] }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if controller.engineState != .running {
                Label("The engine is not running, so the schedule cannot be read. It lives in "
                      + ".agent_state/schedules.json, beside the goal it fires.",
                      systemImage: "info.circle")
                    .font(.caption).foregroundStyle(.secondary)
            } else if let error = controller.schedule["load_error"]?.stringValue, !error.isEmpty {
                // An unreadable schedule fires *nothing*, which looks identical to an empty one in a
                // list. The engine reports the difference and it is passed straight through.
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            } else if controller.scheduleEntries.isEmpty {
                Text("Nothing is scheduled here. `engine.cli schedules add \"what to achieve\" --every 6h` "
                     + "writes an entry; a watcher then has to run for it to fire.")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                HStack(spacing: 22) {
                    Metric(label: "Entries", value: "\(counts["total"]?.intValue ?? 0)")
                    Metric(label: "Armed", value: "\(counts["enabled"]?.intValue ?? 0)", tone: .blue)
                    Metric(label: "Paused", value: "\(counts["disabled"]?.intValue ?? 0)",
                           tone: (counts["disabled"]?.intValue ?? 0) > 0 ? .orange : .secondary)
                    if let next = controller.schedule["next_due_at"]?.stringValue, !next.isEmpty {
                        Metric(label: "Next due", value: next, detail: "UTC")
                    }
                }
                // The fact a list of armed entries does not convey on its own.
                Label("An entry fires only while a watcher is running — `engine.cli schedules watch`. "
                      + "The file arms nothing by itself.",
                      systemImage: "clock.badge.exclamationmark")
                    .font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                // Keyed on the entry's own `id`, which `ScheduleEntry.as_dict` supplies and
                // `schedule_remove` takes. Under `id: \.self` the key included `next_due_at`,
                // `last_fired_at` and `history` — every one of which the watcher writes as it fires —
                // so an entry was replaced while its own countdown was ticking.
                ForEach(controller.scheduleEntries.map { (key: $0["id"]?.stringValue ?? "", entry: $0) },
                        id: \.key) { row in
                    scheduleRow(row.entry)
                }
            }
        }
        // **The confirmation names the entry and what it means.** The objective is the only thing that
        // distinguishes two entries at a glance, and the id is what the engine wants when a slug is
        // ambiguous — so both are on screen. "Unrecoverable" is stated because the engine says so:
        // `ScheduleStore.remove` deletes the entry rather than pausing it.
        .alert("Forget this scheduled objective?", isPresented: Binding(
            get: { pendingRemoval != nil },
            set: { if !$0 { pendingRemoval = nil } })) {
            Button("Forget it", role: .destructive) {
                let id = pendingRemoval?["id"]?.stringValue ?? ""
                pendingRemoval = nil
                Task { await controller.removeSchedule(id: id) }
            }
            Button("Cancel", role: .cancel) { pendingRemoval = nil }
        } message: {
            Text(scheduleRemovalMessage)
        }
    }

    private var scheduleRemovalMessage: String {
        let entry = pendingRemoval
        let objective = entry?["objective"]?.stringValue ?? "this objective"
        let id = entry?["id"]?.stringValue ?? ""
        var lines = ["“\(objective)” (\(id)) is deleted from .agent_state/schedules.json."]
        if entry?["last_outcome"]?.stringValue?.isEmpty == false,
           let outcome = entry?["last_outcome"]?.stringValue {
            // Its history goes with it, and it may be the only record of a fire that failed — which is
            // exactly the entry a person is most likely to want to look at before removing it.
            lines.append("Its fire history goes too — the last result was “\(outcome)”"
                         + (entry?["last_detail"]?.stringValue.map { ": \($0)" } ?? "") + ".")
        }
        lines.append("This is not a pause. `schedules enable` can re-arm a paused entry; a removed one "
                     + "is gone, and adding it again is how you get it back.")
        return lines.joined(separator: "\n\n")
    }

    private func scheduleRow(_ entry: [String: JSONValue]) -> some View {
        let enabled = entry["enabled"]?.boolValue ?? false
        let parked = entry["disabled_reason"]?.stringValue ?? ""
        let every = entry["interval_s"]?.intValue
        let tone: Color = !enabled ? .secondary : (parked.isEmpty ? .blue : .orange)
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                // Not colour alone: the glyph and the word both say the state.
                Image(systemName: enabled ? "clock.badge.checkmark" : "clock.badge.xmark")
                    .foregroundStyle(tone)
                    .accessibilityHidden(true)
                Text(enabled ? "armed" : "paused")
                    .font(.caption2).foregroundStyle(tone)
                Text(entry["id"]?.stringValue ?? "?")
                    .font(.system(.caption, design: .monospaced))
                Text(every.map { "every \(Self.durationLabel($0))" } ?? "once")
                    .font(.caption2).foregroundStyle(.secondary)
                if let next = entry["next_due_at"]?.stringValue, !next.isEmpty, enabled {
                    Text("next \(next)").font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
                Button(role: .destructive) { pendingRemoval = entry } label: {
                    Label("Forget", systemImage: "trash")
                }
                .controlSize(.small)
                .help("Delete this entry from the schedule file. Unrecoverable.")
                .accessibilityLabel("Forget the scheduled objective \(entry["objective"]?.stringValue ?? "")")
            }
            Text(entry["objective"]?.stringValue ?? "")
                .font(.caption).lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
            if !parked.isEmpty {
                // Why it stopped is the whole reason the engine disables rather than retries, so it is
                // shown rather than left as a bare "paused".
                Text("paused: \(parked)").font(.caption2).foregroundStyle(.orange).lineLimit(2)
            }
            if let outcome = entry["last_outcome"]?.stringValue, !outcome.isEmpty {
                Text("last: \(outcome)"
                     + (entry["last_detail"]?.stringValue.map { " — \($0)" } ?? ""))
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(2)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.opacity(0.06))
        .cornerRadius(6)
        .accessibilityElement(children: .contain)
    }

    /// Seconds as the CLI writes them back (`6h`, `2d`), so the panel and `schedules list` agree.
    static func durationLabel(_ seconds: Int) -> String {
        if seconds % 86_400 == 0 { return "\(seconds / 86_400)d" }
        if seconds % 3_600 == 0 { return "\(seconds / 3_600)h" }
        if seconds % 60 == 0 { return "\(seconds / 60)m" }
        return "\(seconds)s"
    }
}

// MARK: - 3. Sessions

/// What the engine has archived on this machine, per agent.
///
/// This is `engine.cli session list` in the GUI, which the app never surfaced: the sessions the engine
/// seals when it rotates a context at the top of the ladder, or hands work on. It matters because it is
/// the *cost* record of a run — `session list` reports what each one spent — and because an empty list
/// is a meaningful answer: "the engine has not rotated anything here yet" rather than a failure.
struct SessionsSection: View {
    @ObservedObject var controller: OrgController

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if controller.offlineSessions.isEmpty {
                Text("No session has been archived yet. The engine seals one when a context reaches "
                     + "the top of its ladder and rotates, or when a step hands work on — so an empty "
                     + "list means no run here has needed to yet.")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                HStack(spacing: 22) {
                    Metric(label: "Sessions", value: "\(controller.offlineSessions.count)")
                    Metric(label: "Agents", value: "\(agentCount)")
                    Metric(label: "On disk", value: bytesLabel)
                }
                ForEach(controller.offlineSessions) { archive in
                    HStack(spacing: 10) {
                        Image(systemName: "rectangle.stack").foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                        Text(archive.agent)
                            .font(.system(.caption, design: .monospaced))
                            .frame(width: 140, alignment: .leading)
                        Text(archive.session)
                            .font(.system(.caption, design: .monospaced))
                            .frame(width: 180, alignment: .leading)
                        Text("\(archive.files) file(s)").font(.caption2).foregroundStyle(.secondary)
                        Spacer()
                        Text(archive.bytes >= 1024
                             ? String(format: "%.1f KB", Double(archive.bytes) / 1024)
                             : "\(archive.bytes) B")
                            .font(.caption2.monospaced()).foregroundStyle(.secondary)
                        Button("Reveal") {
                            reveal(archive)
                        }
                        .accessibilityLabel("Show this session's files in Finder")
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(
                        "session \(archive.session) for agent \(archive.agent), "
                        + "\(archive.files) files, \(archive.bytes) bytes")
                }
            }
        }
    }

    private var agentCount: Int {
        Set(controller.offlineSessions.map(\.agent)).count
    }

    private var bytesLabel: String {
        let total = controller.offlineSessions.reduce(0) { $0 + $1.bytes }
        return total >= 1 << 20
            ? String(format: "%.1f MB", Double(total) / Double(1 << 20))
            : String(format: "%.1f KB", Double(total) / 1024)
    }

    /// Show the session's folder in Finder — through the controller's containment-checked writer, so a
    /// path that escapes the workspace is refused rather than opened.
    private func reveal(_ archive: RunStateBrowser.SessionArchive) {
        let relative = "\(RunStateBrowser.stateDirectory)/sessions/\(archive.id)"
        guard let url = try? controller.writer.resolve(relative) else {
            // Said rather than silently ignored: a Reveal that did nothing is the kind of control the
            // audit objected to.
            controller.notice = "that session is not inside this project"
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }
}

// MARK: - 3. Self-checks

/// The self-improvement loop: what it proposed, and what the boundary refused.
///
/// The safety story is that an unattended loop *proposes* and a person *decides*. The panel therefore
/// states "nothing has been applied" in the header and on every row. Repetition was the feature, and
/// it is kept — but the paragraph explaining *why* the boundary exists is now behind the disclosure it
/// belongs to rather than in front of the person who just wants the list.
struct ProposalsSection: View {
    @ObservedObject var controller: OrgController
    @State private var showingRefused = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 18) {
                Metric(label: "Proposals", value: "\(controller.proposals.count)",
                       detail: "awaiting you",
                       tone: controller.proposals.isEmpty ? .secondary : .blue)
                Metric(label: "Refused", value: "\(controller.proposalsRefused)",
                       detail: "the boundary working", tone: .secondary)
                Spacer()
                if controller.improving {
                    // A cycle runs the whole behavioural suite, so it takes seconds. Saying so beats a
                    // button that looks hung.
                    ProgressView().controlSize(.small)
                        .accessibilityLabel("A self-check is running")
                }
                Button {
                    Task { await controller.runImprover() }
                } label: {
                    Label(controller.improving ? "Looking…" : "Look for issues",
                          systemImage: "sparkle.magnifyingglass")
                }
                .disabled(controller.engineState != .running || controller.improving)
                .help("Detect, draft and validate — then stop and wait for you")
                .accessibilityLabel("Run one self-improvement cycle")
                if !controller.proposals.isEmpty {
                    Button("Reveal the files") {
                        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath:
                            controller.proposalsDirectory)
                    }
                    .accessibilityLabel("Reveal the proposals folder in Finder")
                }
            }

            Label("Nothing has been applied. This loop never edits the tree — read a proposal, then "
                  + "apply it yourself if you agree.",
                  systemImage: "hand.raised.fill")
                .font(.caption).foregroundStyle(.secondary)

            if controller.engineState != .running {
                Label("The engine is not running, so a self-check cannot run.", systemImage: "info.circle")
                    .font(.caption).foregroundStyle(.secondary)
            }

            if controller.proposals.isEmpty {
                Text("Run a check and the engine looks for defects in its own work — a step rejected "
                     + "the same way repeatedly, a loop that never converges, an agent whose record has "
                     + "gone bad. Anything it finds becomes a proposal you can read.")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                // Keyed on `proposal_id`, the id the engine names a proposal by and the one
                // `proposal_dismiss` takes. Under `id: \.self` the key included `lifecycle` —
                // `state`, `can_apply`, `why_not` — which `annotate` rewrites as a proposal moves
                // through its states, so the row was replaced by its own status update.
                ForEach(controller.proposals.map { (key: $0["proposal_id"]?.stringValue ?? "", proposal: $0) },
                        id: \.key) { row in
                    ProposalRow(proposal: row.proposal,
                                onOpen: { open(row.proposal) },
                                onDismiss: { Task { await controller.dismissProposal(id: row.key) } })
                }
            }

            if !controller.proposalsRefusedList.isEmpty { refusedSection }

            if let result = controller.improveResult["count"]?.intValue, !controller.improving {
                Text("Last check considered \(result) finding(s).")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private var refusedSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                showingRefused.toggle()
            } label: {
                Label("\(controller.proposalsRefused) refused by the boundary",
                      systemImage: showingRefused ? "chevron.down" : "chevron.right")
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Show what the safety boundary refused")

            if showingRefused {
                Text("A proposal that would change the machinery which judges this loop — the eval gate, "
                     + "the guardrail, the budget config, credentials — is refused before any model sees "
                     + "it. A system whose safety is enforced by code it can rewrite does not have that "
                     + "property.")
                    .font(.caption2).foregroundStyle(.secondary)
                // Keyed on what actually identifies a refusal. These rows are lines of the improver's
                // `rejected.jsonl`, whose records are `{at, kind, subject, path, state, reason}` — no
                // proposal id, because `already_rejected` matches on (kind, subject) and that pair is
                // what the file is for. `at` is the moment of the refusal and never rewritten.
                ForEach(controller.proposalsRefusedList.map { (key: refusalKey($0), entry: $0) },
                        id: \.key) { row in
                    HStack(spacing: 6) {
                        Image(systemName: "nosign").foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                        Text(row.entry["kind"]?.stringValue ?? "?")
                            .font(.system(.caption2, design: .monospaced))
                            .frame(width: 140, alignment: .leading)
                        Text(row.entry["reason"]?.stringValue ?? "")
                            .font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
        .padding(10)
        .background(Color.secondary.opacity(0.06))
        .cornerRadius(8)
    }

    /// The identity of a refused entry, as close to one as `rejected.jsonl` gets.
    private func refusalKey(_ entry: [String: JSONValue]) -> String {
        [entry["at"]?.stringValue ?? "", entry["kind"]?.stringValue ?? "",
         entry["subject"]?.stringValue ?? ""].joined(separator: "|")
    }

    /// Open one proposal's own write-up — the human-readable half the improver writes beside every
    /// proposal JSON, and the thing a person actually needs to *read* before deciding.
    ///
    /// **Read, not applied, and that is a deliberate boundary rather than a missing feature.**
    /// `proposal_apply` exists in the engine (`serve.py::_cmd_proposal_apply`) and every proposal
    /// already arrives carrying the engine's own `lifecycle.can_apply` / `why_not`, so a button is
    /// reachable from here. It is not offered because applying writes into the working tree and re-runs
    /// the behavioural suite: that is a change to somebody's project, and reading a proposal is the part
    /// this pane was missing — a proposal could be *hidden* but never opened, so "read it, then decide"
    /// had no way to read it short of revealing the folder in Finder.
    ///
    /// Resolved through the containment-checked writer exactly as the session Reveal is, so a path that
    /// escapes the project is refused rather than opened.
    private func open(_ proposal: [String: JSONValue]) {
        // The directory name mirrors `engine/improver.py::PROPOSALS_DIRNAME`. The engine sends the file
        // as an absolute path; this app rebuilds its own relative one so containment applies.
        let name = URL(fileURLWithPath: proposal["file"]?.stringValue ?? "").lastPathComponent
        let relative = "\(RunStateBrowser.stateDirectory)/proposals/\(name)"
        guard !name.isEmpty,
              let url = try? controller.writer.resolve(relative),
              FileManager.default.fileExists(atPath: url.path) else {
            controller.notice = "that proposal's write-up is no longer in the project"
            return
        }
        NSWorkspace.shared.open(url)
    }
}

/// One proposal, with what it found, what it improves, and what to do about it.
///
/// The card states "nothing applied" on every row because that is the safety property of this loop, and
/// it offers the two things a person can honestly do with a proposal it has not applied: read it, or
/// hide it.
struct ProposalRow: View {
    let proposal: [String: JSONValue]
    let onOpen: () -> Void
    let onDismiss: () -> Void

    private var finding: [String: JSONValue] { proposal["finding"]?.objectValue ?? [:] }
    private var validation: [String: JSONValue] { proposal["validation"]?.objectValue ?? [:] }

    /// Whether the engine sent a path to this proposal's own write-up. A button that could only report
    /// a missing file is the "control that cannot act" the audit objected to, so it is not drawn.
    private var canOpen: Bool { !(proposal["file"]?.stringValue ?? "").isEmpty }

    private var tone: StatusTone {
        switch finding["severity"]?.stringValue {
        case "critical": return .bad
        case "major": return .attention
        default: return .neutral
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // The reading part is one accessibility element, so VoiceOver gets the sentence. It is a
            // *separate* group from the buttons below: `.combine` over the whole card made the card a
            // single element, which is how the Hide button — and now Open — became unreachable by
            // VoiceOver while looking perfectly clickable.
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    Image(systemName: tone.symbol).foregroundStyle(tone.colour)
                        .accessibilityHidden(true)
                    Text(finding["kind"]?.stringValue ?? "?")
                        .font(.system(.callout, design: .monospaced))
                    Text(finding["subject"]?.stringValue ?? "")
                        .font(.callout).foregroundStyle(.secondary)
                    Spacer()
                    Text(finding["severity"]?.stringValue ?? "")
                        .font(.caption2).foregroundStyle(tone.colour)
                }

                Text(finding["detail"]?.stringValue ?? "")
                    .font(.caption)

                HStack(spacing: 14) {
                    if let improves = validation["improved"]?.arrayValue, !improves.isEmpty {
                        Label("improves " + improves.compactMap { $0.stringValue }.joined(separator: ", "),
                              systemImage: "arrow.up.right.circle")
                            .font(.caption2).foregroundStyle(.green)
                    }
                    if let regressions = validation["regressions"]?.arrayValue, !regressions.isEmpty {
                        Label("\(regressions.count) regression(s)", systemImage: "exclamationmark.triangle")
                            .font(.caption2).foregroundStyle(.orange)
                    }
                    if let reason = validation["unvalidatable"]?.stringValue, !reason.isEmpty {
                        Label("not validated here", systemImage: "questionmark.circle")
                            .font(.caption2).foregroundStyle(.orange)
                            .help(reason)
                    }
                }

                if let touches = proposal["touches"]?.arrayValue, !touches.isEmpty {
                    Text("would change: " + touches.compactMap { $0.stringValue }.joined(separator: ", "))
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(.secondary).lineLimit(2)
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel(
                "\(finding["kind"]?.stringValue ?? "proposal") on "
                + "\(finding["subject"]?.stringValue ?? "the system"). Nothing has been applied.")

            HStack(spacing: 8) {
                Text("Nothing applied — read it, then decide.")
                    .font(.caption2).foregroundStyle(.secondary)
                Spacer()
                if canOpen {
                    Button("Open") { onOpen() }
                        .help("Open this proposal's own write-up. Reading it changes nothing.")
                        .accessibilityLabel("Open this proposal's write-up")
                }
                Button("Hide") { onDismiss() }
                    .accessibilityLabel("Hide this proposal from the list")
            }
        }
        .padding(10)
        .background(tone.colour.opacity(0.06))
        .cornerRadius(8)
    }
}
