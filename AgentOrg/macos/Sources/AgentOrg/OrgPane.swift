//
//  OrgPane.swift
//  AgentOrg
//
//  The Org destination: who I have, and who can I hire.
//
//  WHY ONE ROSTER
//  --------------
//  The old app had this twice. **Org** was a read-only grouped list (`AgentRow`), **People** was an
//  editable flat list (`EditableAgentRow`), and the code itself admitted the overlap in a comment
//  explaining that mixing them "would either put buttons where the org-health view has none or strip
//  the editing affordances out". That is a real tension, and the resolution is not two rows in the
//  sidebar: it is one row per agent that carries **live state and the actions together**, because they
//  describe the same person and a person looking at an agent wants both.
//
//  So there is one list here. Each row shows the agent's live state (from `status.org`, which is the
//  *running* roster) merged with its editable identity (from `agents`, which is the *configured* one),
//  and carries Edit/Retire when the engine says the agent is editable. The Owner principal is shown as
//  authority rather than headcount, which is what the engine already reports.
//
//  The hire form is behind a button rather than stacked below the list: adding somebody is not what
//  most visits to this destination are for, and the audit's density finding was exactly that the two
//  panels needing input buried their forms under everything else.

import SwiftUI
import AgentOrgKit

struct OrgPane: View {
    @ObservedObject var controller: OrgController
    /// Whether the hire/edit form is open, and which agent it is editing.
    @State private var editorOpen = false
    @State private var editingId: String?
    @State private var confirmRetire: [String: JSONValue]?
    /// The register of other orgs, collapsed by default: most setups have one org, and the roster is
    /// what the destination is for.
    @AppStorage("org.showPortfolio") private var showPortfolio = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                // `engineIsGone` rather than `engineState != .running`: see `OrgController.engineIsGone`
                // — the raw comparison is also true while the engine is launching or draining, which is
                // what emptied this destination on every launch, stop and relaunch.
                if controller.engineIsGone {
                    EngineNotRunningView(controller: controller)
                } else {
                    header
                    roster
                    // Also the settled question, for the same reason as the gate above and one more: it
                    // sits *inside* that branch, so `engineState != .running` here was unreachable —
                    // and reading the raw state would now flash this card in and out during a retry.
                    if controller.hasPortfolio || controller.engineIsGone {
                        SectionCard(title: "Other orgs", symbol: "building.2",
                                    summary: "the other organisations this window knows about, and "
                                           + "which one its controls act on",
                                    expanded: $showPortfolio) {
                            PortfolioSection(controller: controller)
                        }
                    }
                    if editorOpen { HireForm(controller: controller, editingId: $editingId) }
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        // All three, because the hire form depends on all three: the roster to list agents, the
        // providers to choose an endpoint, and the models to choose one of *that* provider's models.
        // Loading only the roster was the bug — the provider picker and the model picker were then
        // empty, so a hire appeared impossible even though the engine could serve it.
        .task {
            await controller.loadWindow()
        }
        // **"Retire" is the engine's word and the engine's behaviour, so the dialog keeps it.** A button
        // labelled "Remove" over `agent_retire` would be a lie about state in both directions: it does
        // remove the roster entry, but it keeps everything the agent produced. The message below says
        // exactly which of the two happened. The reason field is still *not* asked for — this dialog
        // has no text input, and the record is written whether or not a reason is given — but the
        // engine does record the termination, so the message no longer claims otherwise.
        .alert("Retire \(confirmRetire?["name"]?.stringValue ?? "this agent")?", isPresented: Binding(
            get: { confirmRetire != nil },
            set: { if !$0 { confirmRetire = nil } })) {
            Button("Retire", role: .destructive) {
                if let id = confirmRetire?["id"]?.stringValue {
                    Task { await controller.retireAgent(id: id) }
                }
                confirmRetire = nil
            }
            Button("Cancel", role: .cancel) { confirmRetire = nil }
        } message: {
            Text(retireMessage)
        }
    }

    /// The retire confirmation, in the engine's terms.
    ///
    /// Three facts, each traceable to `engine/org/roster.py::terminate`:
    ///
    /// - the agent **does** leave the roster, so it is out of routing and out of this list;
    /// - what it produced is **kept**, because artifacts live in the run rather than in the roster;
    /// - the decision is **recorded** — `terminate` appends `{id, name, reason, at}` to the org's
    ///   `retired` list, which `people.save` writes, so the roster keeps a record that outlasts the
    ///   agent. Re-hiring the same name is still how a person undoes one.
    ///
    /// The last one is the fact a person is most likely to assume the other way — "retire" suggests a
    /// record that outlasts the agent — which is why it is stated rather than left to the word.
    private var retireMessage: String {
        let name = confirmRetire?["name"]?.stringValue ?? "This agent"
        return "\(name) leaves the roster and is not routed any more.\n\n"
            + "Kept: everything it already produced — artifacts and reports live in the run, not in "
            + "the roster.\n\n"
            + "Recorded: the roster keeps the retirement — the agent's name, id and the time — so the "
            + "decision outlasts it. Hiring it again is how you undo this."
    }

    /// Health and headcount, plus the one action that grows the org.
    private var header: some View {
        HStack(spacing: 22) {
            let health = controller.healthSummary
            Metric(label: "Agents", value: "\(controller.roster.count)",
                   detail: "\(controller.rosterByTeam.count) team(s)")
            Metric(label: "Hired by you", value: "\(hiredCount)", detail: "not built-in", tone: .blue)
            Metric(label: "Working", value: "\(workingCount)",
                   detail: "right now", tone: workingCount > 0 ? .blue : .secondary)
            Metric(label: "Degraded", value: "\(health["degraded"] ?? 0)",
                   detail: "deprioritised", tone: (health["degraded"] ?? 0) > 0 ? .orange : .secondary)
            Metric(label: "Quarantined", value: "\(health["quarantined"] ?? 0)",
                   detail: "removed from the pool",
                   tone: (health["quarantined"] ?? 0) > 0 ? .red : .secondary)
            Spacer()
            Button {
                editorOpen.toggle()
                if !editorOpen { editingId = nil }
            } label: {
                Label(editorOpen ? "Close" : "Hire someone",
                      systemImage: editorOpen ? "xmark.circle" : "person.badge.plus")
            }
            .buttonStyle(.borderedProminent)
            .tint(editorOpen ? .secondary : .accentColor)
            .accessibilityLabel(editorOpen ? "Close the hire form" : "Hire a new agent")
        }
    }

    /// The roster, grouped by team — one row per agent, with live state and its actions together.
    private var roster: some View {
        VStack(alignment: .leading, spacing: 10) {
            if controller.roster.isEmpty && controller.agents.isEmpty {
                Text("No agents yet. The engine seeds a default company on first run; if this is empty "
                     + "the roster file has not been written.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(controller.rosterByTeam, id: \.team) { group in
                VStack(alignment: .leading, spacing: 6) {
                    Text(group.team).font(.headline)
                    ForEach(rosterRows(group.members), id: \.key) { row in
                        AgentRow(
                            agent: row.agent,
                            // The *editable* entry for this agent, when the engine reports one, so the
                            // row carries both faces: what it is doing now and what it can be changed
                            // to. Matching on id because the two reports key on different fields.
                            editable: editableEntry(for: row.agent),
                            onEdit: {
                                editingId = row.agent["id"]?.stringValue
                                editorOpen = true
                            },
                            onRetire: { confirmRetire = editableEntry(for: row.agent) ?? row.agent })
                    }
                }
                .padding(10)
                .background(Color.secondary.opacity(0.06))
                .cornerRadius(8)
            }
            if !controller.rosterPath.isEmpty {
                Text("Saved to \(controller.rosterPath). Editing keeps an agent's id, so its history "
                     + "and health record are preserved.")
                    .font(.caption2).foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    /// The roster's rows, keyed on the engine's own agent id.
    ///
    /// Keying the `ForEach` on the whole entry (`id: \.self`) made SwiftUI *replace* every row on each
    /// poll rather than update it: a roster entry carries `stats` — `last_active`, `turns`,
    /// `spent_usd` — and `current_task`, all of which move as the agent works, so the row's identity
    /// moved with them. A replaced row is a fresh `@State`, a re-run `onAppear` and re-created AppKit
    /// controls, which is what this flicker was made of. `id` is written once and kept across an edit;
    /// `name` is the fallback for an entry the engine ever reports without one.
    private func rosterRows(_ members: [[String: JSONValue]])
        -> [(key: String, agent: [String: JSONValue])] {
        members.map { (key: $0["id"]?.stringValue ?? $0["name"]?.stringValue ?? "", agent: $0) }
    }

    /// The configured entry behind a live roster row.
    ///
    /// The two engine reports key an agent differently — `status.org` uses `id`, the `agents` command
    /// uses `id` too but carries `editable`/`status` — so matching is by id and a miss falls back to
    /// nothing rather than to a fabricated entry.
    private func editableEntry(for agent: [String: JSONValue]) -> [String: JSONValue]? {
        guard let id = agent["id"]?.stringValue else { return nil }
        return controller.roster.first { $0["id"]?.stringValue == id }
    }

    private var hiredCount: Int {
        controller.roster.filter { $0["status"]?.stringValue == "hired" }.count
    }

    private var workingCount: Int {
        controller.roster.filter { $0["state"]?.stringValue == "working" }.count
    }
}

/// One agent: what it is doing now, what it runs on, and what can be done to it.
///
/// The single row the old `AgentRow` and `EditableAgentRow` collapsed into. The live fields (state,
/// current work) come first because they are why a person opened the destination; the identity and the
/// actions follow. `editable` is what the engine reported, so an uneditable principal is shown as
/// authority rather than with buttons that would be refused.
struct AgentRow: View {
    let agent: [String: JSONValue]
    var editable: [String: JSONValue]?
    var onEdit: (() -> Void)?
    var onRetire: (() -> Void)?

    private var state: String { agent["state"]?.stringValue ?? "idle" }
    private var status: String { editable?["status"]?.stringValue ?? "built-in" }
    private var isEditable: Bool { editable?["editable"]?.boolValue == true }

    private var stateTone: StatusTone { StatusTone.forStatus(state) }

    private var statusTone: Color {
        switch status {
        case "hired": return .green
        case "owner": return .blue
        default: return .secondary
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: status == "owner" ? "crown" :
                    (status == "hired" ? "person.fill.checkmark" : "person.fill"))
                .foregroundStyle(statusTone)
                .frame(width: 18)
                .accessibilityLabel(status == "owner" ? "The Owner, the terminal authority"
                                    : (status == "hired" ? "Hired by you" : "Built-in"))
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 7) {
                    Text(agent["name"]?.stringValue ?? "?")
                        .font(.system(.body, design: .rounded).weight(.medium))
                    if let title = agent["title"]?.stringValue, !title.isEmpty {
                        Text(title).font(.caption).foregroundStyle(.secondary)
                    }
                    Text(status).font(.caption2).foregroundStyle(statusTone)
                }
                HStack(spacing: 8) {
                    // The model, spelled out: this is the fact a person changes most often, and the
                    // old read-only row and the old editable row showed it differently.
                    Label("\(agent["provider"]?.stringValue ?? "?")/\(agent["model"]?.stringValue ?? "?")",
                          systemImage: "cpu")
                        .font(.caption2.monospaced()).foregroundStyle(.secondary)
                    if let window = agent["context_window"]?.intValue, window > 0 {
                        Text("\(window) tokens").font(.caption2).foregroundStyle(.secondary)
                    } else {
                        // An unknown window cannot bind an agent, and saying so is more useful than a
                        // blank — the engine refuses exactly this case.
                        Text("window unknown — cannot bind")
                            .font(.caption2).foregroundStyle(.orange)
                    }
                    if let skills = agent["skills"]?.arrayValue {
                        Text(skills.compactMap { $0.stringValue }.joined(separator: ", "))
                            .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                if let task = agent["current_task"]?.stringValue, !task.isEmpty {
                    Text("on: \(task)").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 3) {
                Label(state, systemImage: stateTone.symbol)
                    .font(.caption.monospaced())
                    .foregroundStyle(stateTone.colour)
                    .accessibilityLabel("State: \(state)")
                HStack(spacing: 6) {
                    if isEditable, let onEdit, let onRetire {
                        Button("Edit", action: onEdit)
                            .controlSize(.small)
                            .accessibilityLabel("Edit \(agent["name"]?.stringValue ?? "this agent")")
                        if status == "hired" {
                            Button("Retire", action: onRetire)
                                .controlSize(.small)
                                // Named for the engine's own operation rather than as "Remove", and the
                                // help says which half is kept — the distinction the confirmation makes
                                // in full. The engine also refuses an agent whose reports are actively
                                // working; that refusal surfaces in its own words rather than being
                                // pre-empted here.
                                .help("Take this agent out of the roster. It is not a delete — what it "
                                      + "produced is kept.")
                                .accessibilityLabel("Retire \(agent["name"]?.stringValue ?? "this agent")")
                        }
                    } else if status == "owner" {
                        Text("authority").font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(
            "\(agent["name"]?.stringValue ?? "agent"), \(agent["title"]?.stringValue ?? ""), "
            + "\(state), model \(agent["model"]?.stringValue ?? "unknown")")
    }
}

// MARK: - Hiring

/// The hire/edit form.
///
/// Opened deliberately rather than stacked under the roster. The fields are the same ones the old
/// People panel had — they were right — with the explanations moved to `help` text and the model
/// picker's empty case kept, because that case is the one that made hiring look impossible when the
/// provider was merely unreachable.
struct HireForm: View {
    @ObservedObject var controller: OrgController
    @Binding var editingId: String?

    @State private var draft = AgentDraft()
    @State private var original: AgentDraft?
    @State private var seeded = false

    private var isEditing: Bool { editingId != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(isEditing ? "Edit \(draft.name)" : "Hire an agent",
                  systemImage: isEditing ? "pencil" : "person.badge.plus")
                .font(.headline)

            HStack(spacing: 8) {
                TextField("Name", text: $draft.name)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Agent name")
                TextField("Title (optional)", text: $draft.title)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Agent title")
                TextField("Team (optional)", text: $draft.team)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 180)
                    .accessibilityLabel("Team")
            }

            HStack(spacing: 8) {
                // A real picker over the library's skills, not a text field: a mistyped skill is
                // refused, and the user would have had no way to know the right spelling.
                Picker("Skill", selection: $draft.skill) {
                    Text("Choose a skill…").tag("")
                    ForEach(controller.skills, id: \.self) { skill in
                        Text(skill).tag(skill)
                    }
                }
                .frame(maxWidth: 320)
                .accessibilityLabel("What this agent does")

                Picker("Level", selection: $draft.level) {
                    ForEach(["junior", "practitioner", "senior", "staff", "principal"], id: \.self) {
                        Text($0.capitalized).tag($0)
                    }
                }
                .frame(width: 170)
                .accessibilityLabel("Seniority level")
            }

            HStack(spacing: 8) {
                Picker("Provider", selection: $draft.provider) {
                    Text("Choose a provider…").tag("")
                    // Keyed on the provider's own `id`, not the whole entry: the entry carries its
                    // discovery status, error text and model list, all of which change when a provider
                    // is tested — so `id: \.self` replaced the picker's rows on every probe.
                    ForEach(controller.providers.map { (key: $0["id"]?.stringValue ?? "", entry: $0) },
                            id: \.key) { row in
                        Text(row.entry["id"]?.stringValue ?? "?")
                            .tag(row.entry["id"]?.stringValue ?? "")
                    }
                }
                .frame(maxWidth: 240)
                .accessibilityLabel("Provider for this agent")

                // A picker **or** a text field, decided by what the provider actually reported.
                //
                // The picker is better when models are known: it cannot be mistyped, and it shows only
                // models that provider serves. But a provider whose listing failed — bad key, unreachable
                // host, an endpoint that reports nothing — would leave the picker empty and make hiring
                // *impossible*, which is the wrong answer for "the model exists, we just could not ask
                // about it". So a typed name is always reachable, and the field says which case it is in.
                if modelsForProvider.isEmpty {
                    TextField(modelPlaceholder, text: $draft.model)
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 320)
                        .accessibilityLabel("Model for this agent, typed")
                } else {
                    Picker("Model", selection: $draft.model) {
                        Text("Choose a model…").tag("")
                        ForEach(modelsForProvider, id: \.self) { model in
                            Text(model).tag(model)
                        }
                    }
                    .frame(maxWidth: 320)
                    .accessibilityLabel("Model for this agent")
                }
            }
            modelHint

            capabilityEditor

            HStack(spacing: 8) {
                Button {
                    Task { await submit() }
                } label: {
                    Label(isEditing ? "Save changes" : "Hire",
                          systemImage: isEditing ? "checkmark.circle" : "person.crop.circle.badge.plus")
                }
                .buttonStyle(.borderedProminent)
                .disabled(!draft.isComplete || controller.engineState != .running)
                .accessibilityLabel(isEditing ? "Save the agent changes" : "Hire this agent")

                Button("Cancel") { close() }
                    .accessibilityLabel("Close the hire form")
                Spacer()
                if isEditing {
                    Text("Editing keeps \(draft.name)'s identity — its history and health record stay "
                         + "with it.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .background(Color.green.opacity(0.06))
        .cornerRadius(8)
        .onAppear(perform: seed)
        .onChange(of: editingId) { _, _ in seed() }
    }

    /// Load the agent being edited into the form, once per selection.
    private func seed() {
        guard let id = editingId else {
            draft = AgentDraft()
            original = nil
            return
        }
        guard let entry = controller.roster.first(where: { $0["id"]?.stringValue == id }) else {
            return
        }
        draft = AgentDraft(existing: entry)
        original = draft
    }

    private func close() {
        editingId = nil
        draft = AgentDraft()
        original = nil
    }

    /// The models of the chosen provider, so the two pickers stay consistent.
    private var modelsForProvider: [String] {
        guard !draft.provider.isEmpty else { return [] }
        let models = controller.models.filter {
            $0["provider_id"]?.stringValue == draft.provider
        }
        return models.compactMap { $0["model_id"]?.stringValue }.sorted()
    }

    /// What the model field says when there is nothing to pick from.
    private var modelPlaceholder: String {
        if draft.provider.isEmpty { return "choose a provider first" }
        return "type a model name, e.g. llama3.1:8b"
    }

    /// Why the model list is empty, said plainly.
    ///
    /// The failure this prevents is a silent one: an empty picker looks like the app is broken, when
    /// the truth may be that the provider is unreachable and the model is perfectly usable. Naming the
    /// reason turns "it doesn't work" into either "fix the endpoint" or "type the name anyway".
    @ViewBuilder
    private var modelHint: some View {
        if !draft.provider.isEmpty && modelsForProvider.isEmpty {
            let row = controller.providers.first { $0["id"]?.stringValue == draft.provider }
            let status = row?["status"]?.stringValue ?? "unknown"
            let error = row?["error"]?.stringValue ?? ""
            Label(
                "No models listed for \(draft.provider) (status: \(status)). "
                + (error.isEmpty ? "Type the model name, or test the provider in Setup."
                                 : "Test the provider in Setup — \(error)"),
                systemImage: "info.circle")
                .font(.caption2).foregroundStyle(.secondary).lineLimit(3)
        } else if draft.provider.isEmpty {
            Text("Pick a provider, then choose one of the models it reported. Any OpenAI-compatible "
                 + "endpoint works — the list comes from the provider itself.")
                .font(.caption2).foregroundStyle(.secondary)
        }
    }

    /// What this agent may reach. Each grant is independent, so a reviewer can be given nothing while
    /// an assistant is given exactly what it needs — and the file grants are separate from the system
    /// ones, because "may edit code" is not "may open apps on my desktop".
    ///
    /// Left untouched, the engine applies its own least-privilege default (read, plus write for a
    /// non-reviewer), so a person who ignores this section never widens anything by accident.
    private var capabilityEditor: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Label("Capabilities", systemImage: "lock.shield")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                if draft.capabilities.isEmpty {
                    Text("default (least privilege)").font(.caption2).foregroundStyle(.tertiary)
                } else {
                    Button("Reset to default") { draft.capabilities = [] }
                        .buttonStyle(.link).font(.caption2)
                        .accessibilityLabel("Clear every capability, using the skill's default instead")
                }
            }
            // Grouped so the consequence of each grant is legible before it is made. The system group
            // is the one that reaches outside the project, and the label says which of them change
            // state on the person's own machine.
            ForEach(CapabilityChoice.groups, id: \.title) { group in
                VStack(alignment: .leading, spacing: 3) {
                    Text(group.title).font(.caption2.weight(.semibold))
                        .foregroundStyle(group.isSystem ? .orange : .secondary)
                    ForEach(group.choices) { choice in
                        Toggle(isOn: binding(for: choice.grant)) {
                            VStack(alignment: .leading, spacing: 0) {
                                Text(choice.label).font(.caption)
                                Text(choice.detail).font(.caption2).foregroundStyle(.tertiary)
                            }
                        }
                        .toggleStyle(.checkbox)
                        .accessibilityLabel("\(choice.label): \(choice.detail)")
                    }
                }
            }
        }
    }

    /// The grant this toggle controls, resolved to a real value.
    private func binding(for grant: String) -> Binding<Bool> {
        Binding(
            get: { draft.capabilities.contains(grant) },
            set: { on in
                var set = Set(draft.capabilities)
                if on { set.insert(grant) } else { set.remove(grant) }
                draft.capabilities = set.sorted()
            })
    }

    private func submit() async {
        if let id = editingId {
            await controller.updateAgent(id: id, draft: draft, original: original)
        } else {
            await controller.hireAgent(draft)
        }
        close()
        await controller.loadRoster()
    }
}
