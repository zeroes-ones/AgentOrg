//
//  PortfolioSection.swift
//  AgentOrg
//
//  The several orgs one person runs, re-homed into the Org destination.
//
//  WHY IT LIVED IN THE SIDEBAR AND NO LONGER DOES
//  ----------------------------------------------
//  "Which orgs am I running, and what is each doing?" was a sidebar row of its own, and the tab enum's
//  own comment claimed it was first "because it is the *whole* picture". It was not: it listed orgs
//  the person had registered, which on a single-org setup is one row saying their own name, and the
//  app opened on Org instead — so the claim in the comment was never true of the behaviour.
//
//  The question is still a real one for somebody running several, so the capability is kept whole and
//  made a **section of Org**, collapsed: the roster is who you have today, and this is the register
//  above it. Nothing was deleted; the row that most people would never use stopped competing for
//  attention with the two they would.
//
//  Two engine behaviours are load-bearing here and are said on screen rather than assumed:
//
//  - The live picture is a **separate, expensive fetch** (`portfolio_live` builds an orchestrator per
//    org), so it is asked for when the section is opened, not on every poll.
//  - The engine keeps a **fleet**, so one org's run does not block another's — which is the whole
//    reason the section exists.

import SwiftUI
import AgentOrgKit

/// The register of orgs, with the two actions that matter: run one, or switch which one the app acts on.
struct PortfolioSection: View {
    @ObservedObject var controller: OrgController

    @State private var newOrgName: String = ""
    @State private var newOrgCharter: String = ""
    @State private var goals: [String: String] = [:]
    /// The org just registered, so the section can *show* what was created and where it will work.
    /// Nil until a registration succeeds — and cleared on a refusal, so a stale confirmation cannot sit
    /// under a form whose submit failed.
    @State private var createdOrg: [String: JSONValue]?
    /// The org a person pressed Forget on, held while the confirmation is on screen.
    @State private var pendingRemoval: [String: JSONValue]?
    /// The engine's own account of what removing `pendingRemoval` would take away. Nil while it is
    /// being fetched, which the confirmation says rather than showing a made-up figure.
    @State private var removalPreview: [String: JSONValue]?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // `engineIsGone` rather than `engineState != .running`: the register is read from the last
            // poll, so it is still the right thing to show while the console is restarting the engine —
            // and swapping it for this sentence is what made the section empty on every relaunch.
            if controller.engineIsGone {
                Text("The engine is not running, so the register cannot be read.")
                    .font(.caption).foregroundStyle(.secondary)
            } else if !controller.hasPortfolio {
                emptyState
            } else {
                switcher
                metrics
                // Keyed on the org's own `id` rather than the whole entry: the entry carries `exists`
                // and `path`, so a folder that appears or disappears changed the row's identity under
                // `id: \.self` — and a replaced row is a replaced goal field, mid-typing.
                ForEach(controller.portfolioOrgs.map { (key: $0["id"]?.stringValue ?? "", org: $0) },
                        id: \.key) { row in
                    orgCard(row.org)
                }
            }
            registerOrg
            if let created = createdOrg { createdConfirmation(created) }
        }
        .task {
            // Only when the section is on screen: gathering the live picture builds an orchestrator
            // per org, which is not a cost to pay for a collapsed section on every poll.
            if controller.engineState == .running { await controller.loadPortfolioLive() }
        }
        // **The confirmation names the object and the consequence.** "Are you sure?" is not a
        // confirmation: a person who pressed Forget on the wrong row of three cannot answer it. So the
        // title carries the org's name and slug, and the message carries the two facts the engine
        // supplied — how many bytes stay on disk, and that the engine will not delete them. The preview
        // is fetched when the dialog opens rather than composed here, because both numbers are
        // properties of the folder and only the engine can walk it.
        .alert("Forget “\(pendingRemoval?["name"]?.stringValue ?? "this org")”?",
               isPresented: Binding(
                get: { pendingRemoval != nil },
                set: { if !$0 { pendingRemoval = nil; removalPreview = nil } })) {
            Button("Forget it", role: .destructive) {
                let ref = pendingRemoval?["id"]?.stringValue ?? ""
                pendingRemoval = nil
                removalPreview = nil
                Task { await controller.removeOrg(ref) }
            }
            Button("Cancel", role: .cancel) { pendingRemoval = nil; removalPreview = nil }
        } message: {
            Text(removalMessage)
        }
        .task(id: pendingRemoval?["id"]?.stringValue) {
            guard let ref = pendingRemoval?["id"]?.stringValue else { return }
            removalPreview = await controller.orgRemovalPreview(ref)
        }
    }

    /// What the confirmation says, built from the engine's preview.
    ///
    /// Every claim here has a source: the org's name and slug from the row, the byte count and the
    /// folder path from `portfolio_removal`, and the "the folder is left alone" sentence from
    /// `Portfolio.remove_org`'s own docstring. Nothing is asserted that the engine did not report —
    /// including the case where the preview has not arrived or could not be read, which says so
    /// instead of guessing at a size.
    private var removalMessage: String {
        let name = pendingRemoval?["name"]?.stringValue ?? "this org"
        let slug = pendingRemoval?["slug"]?.stringValue ?? ""
        let labelled = slug.isEmpty ? name : "\(name) (\(slug))"

        var lines = ["\(labelled) is removed from the register. It stops appearing here and in the "
                     + "switcher, and bare commands act on another org."]

        guard let preview = removalPreview else {
            lines.append("Working out what stays on disk…")
            return lines.joined(separator: "\n\n")
        }
        let folder = preview["folder"]?.stringValue ?? ""
        if preview["folder_exists"]?.boolValue == true {
            let bytes = preview["folder_bytes"]?.intValue
            lines.append("**Its folder is NOT deleted.** "
                         + (bytes.map { "\(Self.byteLabel($0)) of run history, rosters and state stay in:" }
                            ?? "Its state stays in:")
                         + "\n\(folder)")
            if let why = preview["can_delete_folder_why"]?.stringValue, !why.isEmpty {
                lines.append("There is no option to delete it here: \(why). Remove the folder yourself "
                             + "in Finder if that is what you want.")
            }
        } else if folder.isEmpty {
            lines.append("It has no folder of its own — it works under this project's managed "
                         + "`projects/` directory.")
        } else {
            lines.append("Its folder is already gone: \(folder)")
        }
        return lines.joined(separator: "\n\n")
    }

    /// Bytes as a person reads them. Binary units, because that is what Finder reports and a size that
    /// disagrees with the folder it is describing is worse than no size.
    static func byteLabel(_ bytes: Int) -> String {
        if bytes >= 1 << 30 { return String(format: "%.1f GB", Double(bytes) / Double(1 << 30)) }
        if bytes >= 1 << 20 { return String(format: "%.1f MB", Double(bytes) / Double(1 << 20)) }
        if bytes >= 1 << 10 { return String(format: "%.1f KB", Double(bytes) / 1024) }
        return "\(bytes) B"
    }

    /// The switcher: which org this window is acting on, and the way to change it.
    ///
    /// This is the control the whole track exists for. Everything else in the window — the roster on
    /// Org, the mission and goal on Now, the run history on Runs — describes *one* org, and before this
    /// there was no way to choose which. The row was in the list and could be marked active; it could
    /// not be changed.
    ///
    /// A `Picker` rather than a row of buttons, for two reasons that are both about a person running
    /// three or more orgs: it is the platform control for "choose one of several", so it gets keyboard
    /// selection, VoiceOver phrasing and the menu behaviour for free; and it stays one fixed-size
    /// control however many orgs are registered, where a button per org grows without bound.
    ///
    /// The active org is stated in words beside the picker *as well as* in it, because the picker shows
    /// only the current selection: a person who cannot see which org the window describes must be able
    /// to read it. Not colour alone — the glyph and the name are the carriers.
    private var switcher: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: "circle.hexagongrid.fill")
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                Text("This window is acting on").font(.caption).foregroundStyle(.secondary)
                Picker("Org this window acts on", selection: activeOrgBinding) {
                    // Keyed on `id`, so testing a provider or an org appearing on disk does not rebuild
                    // the selection control under the person using it.
                    ForEach(controller.portfolioOrgs.map { (key: $0["id"]?.stringValue ?? "", org: $0) },
                            id: \.key) { row in
                        Text(orgLabel(row.org)).tag(row.org["id"]?.stringValue ?? "")
                    }
                }
                .labelsHidden()
                .frame(maxWidth: 320)
                .disabled(controller.engineState != .running)
                .help("Choose which organisation the roster, the mission and the runs in this window "
                      + "describe. Each org keeps running in parallel.")
            }
            // The one fact that must never be ambiguous in a multi-org window, said as a sentence
            // rather than left to the picker's current value.
            HStack(spacing: 6) {
                if controller.activeOrg != nil {
                    Label(controller.activeOrgName, systemImage: "largecircle.fill.circle")
                        .font(.callout.weight(.medium))
                        .foregroundStyle(.green)
                        .accessibilityLabel("Active org: \(controller.activeOrgName)")
                } else {
                    // The active id resolves to nothing: the org was removed from another window or the
                    // CLI. Said plainly, because every other panel is still describing *something* and
                    // a person would otherwise have no way to know the label and the content disagree.
                    Label("the org this window was acting on is no longer registered",
                          systemImage: "exclamationmark.triangle.fill")
                        .font(.caption).foregroundStyle(.orange)
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.green.opacity(0.07))
        .cornerRadius(8)
    }

    /// The org's name with its slug, so the picker entry is identifiable even when two orgs share a
    /// display name — which is exactly the case a person running several is most likely to hit.
    private func orgLabel(_ org: [String: JSONValue]) -> String {
        let name = org["name"]?.stringValue ?? "?"
        let slug = org["slug"]?.stringValue ?? ""
        return slug.isEmpty || slug == name ? name : "\(name) (\(slug))"
    }

    /// Writes the selection straight through to `selectOrg`.
    ///
    /// A binding whose setter is the action, rather than a stored `@State` mirrored into the engine: a
    /// second local copy of "which org is active" is a second source of truth, and the one that would
    /// go stale is the one the engine actually acts on.
    private var activeOrgBinding: Binding<String> {
        Binding(
            get: { controller.activeOrgId },
            set: { ref in
                guard ref != controller.activeOrgId else { return }
                Task { await controller.selectOrg(ref) }
            })
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
            // "unknown" rather than $0.00 for the same reason as everywhere else: a spend nobody has
            // measured is not a spend of zero.
            Metric(label: "Spend", value: totals?["spend_usd"]?.doubleValue
                       .map { String(format: "$%.2f", $0) } ?? "unknown",
                   detail: "across loaded orgs")
            if controller.portfolioLoading { ProgressView().controlSize(.small) }
        }
    }

    /// One org: its identity, what it is doing, and the actions.
    private func orgCard(_ org: [String: JSONValue]) -> some View {
        let id = org["id"]?.stringValue ?? ""
        let row = controller.orgRow(id)
        let enabled = org["enabled"]?.boolValue ?? true
        let isActive = id == controller.activeOrgId
        let running = row["running"]?.boolValue ?? false
        let headline = row["headline"]?.stringValue ?? (org["exists"]?.boolValue == true ? "loaded" : "")
        // Whether this org is stopped pending a decision by a person. The engine's own read: `fleet.py`
        // sets `waiting_host` for a parked gate or a waiting phase, and `blocked` counts node stops. It
        // decides the *emphasis* of the switch control below and nothing else — never whether a control
        // exists, which would be this view inventing the engine's answer.
        let waitingOnPerson = row["waiting_host"]?.boolValue == true || (row["blocked"]?.intValue ?? 0) > 0
        let tone: Color = !enabled ? .secondary
            : (row["waiting_host"]?.boolValue == true ? .orange
               : ((row["blocked"]?.intValue ?? 0) > 0 ? .red : (running ? .blue : .primary)))
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                // Filled against hollow, *and* a word beside it: the app's own rule is that state is
                // never carried by colour alone.
                Image(systemName: isActive ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(isActive ? .green : .secondary)
                    .accessibilityHidden(true)
                Text(org["name"]?.stringValue ?? "?")
                    .font(.headline)
                Text(org["slug"]?.stringValue ?? "")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                if !enabled { Text("disabled").font(.caption2).foregroundStyle(.secondary) }
                if isActive {
                    // The one thing a person must be able to tell at a glance, because every other
                    // control in the window acts on it.
                    Text("active — this window acts on it").font(.caption2).foregroundStyle(.green)
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
            if org["exists"]?.boolValue == false {
                // A register entry whose folder is gone is stated, not silently rendered as idle: the
                // run would fail at launch and this is where that is explained.
                Text("folder not found: \(org["path"]?.stringValue ?? "")")
                    .font(.caption2).foregroundStyle(.orange).lineLimit(1)
            }

            // A row waiting on a person showed the engine's headline ("Waiting on you: approve the
            // release") and then offered exactly one control that could resolve it — named "Switch to
            // this", which is the mechanism rather than what the person gets. So what is being waited
            // for and the step that resolves it now sit together, and the switch is emphasised and named
            // for its outcome. Switching is the real remedy and not a shortcut around one: a parked plan
            // is decided in its own org's context, which is what makes an org the one the window
            // describes.
            if waitingOnPerson {
                Label(isActive
                        ? "waiting on you — its parked plan, and the decision it needs, are on Now"
                        : "waiting on you — its parked plan is decided in that org's own context",
                      systemImage: "hand.raised")
                    .font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityElement(children: .combine)
            }

            HStack(spacing: 8) {
                TextField("Goal for \(org["name"]?.stringValue ?? "this org")",
                          text: binding(for: id))
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Goal for \(org["name"]?.stringValue ?? "this org")")
                Button("Run") {
                    let goal = goals[id] ?? ""
                    goals[id] = ""
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
                    if waitingOnPerson {
                        // The emphasised control on a row that is going nowhere else. "Decide" rather
                        // than "Switch": the switch is what the app does, the decision is what the
                        // person came here to make.
                        Button("Switch to it and decide") { Task { await controller.selectOrg(id) } }
                            .buttonStyle(.borderedProminent)
                            .help("Make this org the one this window describes. Its parked plan, and the "
                                  + "decision it is waiting on, are shown on Now.")
                            .accessibilityLabel("Switch to this org and decide what it is waiting on")
                    } else {
                        Button("Switch to this") { Task { await controller.selectOrg(id) } }
                            .help("Make this org the one the rest of this window describes")
                    }
                }
                // Destructive, so it is separated from the row's ordinary controls and named for what
                // it does to the *register* rather than for the word "delete" — which would promise a
                // folder removal the engine does not perform. The confirmation states the consequence
                // with the engine's own numbers before anything happens.
                Button(role: .destructive) {
                    pendingRemoval = org
                } label: {
                    Label("Forget", systemImage: "trash")
                }
                .disabled(controller.engineState != .running)
                .help("Remove this org from the register. Its folder stays on disk.")
                .accessibilityLabel("Forget \(org["name"]?.stringValue ?? "this org")")
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.opacity(0.06))
        .cornerRadius(8)
    }

    private func binding(for id: String) -> Binding<String> {
        Binding(get: { goals[id] ?? "" }, set: { goals[id] = $0 })
    }

    /// An empty register is a normal answer on a single-org setup, so it is said calmly and the one
    /// step that changes it is right there.
    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("No other orgs registered", systemImage: "building.2")
                .font(.callout.weight(.medium))
            Text("A portfolio is one person and the several orgs they run. Most setups have one — the "
                 + "project above. Register another below to run it in parallel.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Register an org, with the two facts that decide where its work lands.
    ///
    /// Name, charter and folder, because those are what `portfolio add` takes and what a person running
    /// several orgs actually has an opinion about: the name is how they tell them apart, the charter is
    /// what the org is *for* (the engine feeds it to the org's own agents), and the folder is where the
    /// run's files are written. Leaving the folder empty is a first-class choice, not a missing value —
    /// the engine then owns a managed project — so it is labelled as such rather than left blank and
    /// ambiguous.
    private var registerOrg: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Register another org").font(.callout.weight(.medium))
            HStack(spacing: 8) {
                TextField("Its name, e.g. Acme", text: $newOrgName)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("New org name")
                Button("Register") {
                    let name = newOrgName
                    let charter = newOrgCharter
                    newOrgName = ""
                    newOrgCharter = ""
                    createdOrg = nil
                    Task { createdOrg = await controller.addOrg(name: name, charter: charter) }
                }
                .disabled(newOrgName.isEmpty || controller.engineState != .running)
                .help("Register the org. Add its folder below if its work should live in your own "
                      + "repository.")
            }
            TextField("What it is for, e.g. EV charging in Europe", text: $newOrgCharter)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("New org charter")
                .help("The org's charter. Its own agents see this, so it is how the org knows what it "
                      + "is for.")
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.blue.opacity(0.06))
        .cornerRadius(8)
    }

    /// What was created, and where its work will live.
    ///
    /// Shown rather than merely announced, because the folder is the part a person cannot infer: an
    /// org registered with no path gets a managed project under the engine's own projects directory,
    /// and that is a different answer from the one they might have assumed.
    private func createdConfirmation(_ created: [String: JSONValue]) -> some View {
        let name = created["name"]?.stringValue ?? "the org"
        let slug = created["slug"]?.stringValue ?? ""
        let path = created["path"]?.stringValue ?? ""
        return VStack(alignment: .leading, spacing: 3) {
            Label("Registered \(name)\(slug.isEmpty ? "" : " (\(slug))")",
                  systemImage: "checkmark.circle.fill")
                .font(.callout.weight(.medium))
                .foregroundStyle(.green)
            if path.isEmpty {
                Text("Its work will live in a managed project the engine owns, under the projects "
                     + "directory — not in your repository.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("Its work will live in: \(path)")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            Text("Switch to it above to make this window describe it.")
                .font(.caption2).foregroundStyle(.secondary)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.green.opacity(0.07))
        .cornerRadius(8)
        .accessibilityElement(children: .combine)
    }
}

