//
//  Providers.swift
//  AgentOrg
//
//  Two panels, each answering one question:
//
//    Providers — "Which models can I reach, and with what?"
//    People    — "Who can I hire, and what are they on?"
//
//  WHY ADDING A PROVIDER IS A FORM AND NOT A CONFIG FILE
//  ----------------------------------------------------
//  The engine is config-driven and that is a strength, but "edit credentials.json" is not something a
//  person using a native app should have to do to point it at a model. The one thing this must get
//  right is the *order*: test, then save. A form that saved first would leave a bad endpoint in the
//  config for the next run to trip over, so **Test** is what fetches the models and **Save** is only
//  enabled once the shape is complete.
//
//  Accessibility is a requirement here as everywhere: every control is labelled, every state is
//  spelled out as well as coloured, and a key is never rendered back from the engine because the
//  engine never returns one.

import SwiftUI
import AgentOrgKit

// MARK: - Providers

/// Add, edit, test and remove the endpoints the org can use.
struct ProvidersPanel: View {
    @ObservedObject var controller: OrgController

    @State private var draft = ProviderDraft()
    @State private var editing: String?
    @State private var testResult: [String: JSONValue] = [:]
    @State private var headerRows: [HeaderRow] = []

    /// One `Name: value` line in the header editor.
    struct HeaderRow: Identifiable, Equatable {
        let id = UUID()
        var name: String = ""
        var value: String = ""
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                // Why the form's buttons are unavailable. A disabled button with no reason is the
                // worst kind of UI: the user pressed Save, nothing happened, and there was nothing
                // anywhere to read. That is how "I tried to add it and it didn't work" happens.
                if controller.engineState != .running {
                    Label("The engine is not running, so providers cannot be tested or saved. "
                          + "Press ⌘⇧L (Launch Engine) first.",
                          systemImage: "exclamationmark.triangle.fill")
                        .font(.callout)
                        .foregroundStyle(.orange)
                        .accessibilityLabel("The engine is not running; launch it to edit providers")
                }
                if controller.providers.isEmpty {
                    ContentUnavailableView {
                        Label("No providers yet", systemImage: "server.rack")
                    } description: {
                        Text("Add an endpoint below — an OpenAI-compatible host, Anthropic, or a local "
                             + "Ollama. Test it to fetch its models before saving.")
                    }
                } else {
                    ForEach(controller.providers, id: \.self) { provider in
                        ProviderRow(
                            provider: provider,
                            onEdit: { beginEdit(provider) },
                            onRemove: { Task { await controller.removeProvider(
                                id: provider["id"]?.stringValue ?? "") } })
                    }
                }

                Divider()

                editor

                if let path = Optional(controller.providersConfigPath), !path.isEmpty {
                    Text("Saved to \(path) (mode 0600). Keys are never sent back to this window.")
                        .font(.caption2).foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task { await controller.loadProviders() }
    }

    // MARK: the editor

    private var editor: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(editing == nil ? "Add a provider" : "Edit \(editing ?? "")",
                  systemImage: editing == nil ? "plus.circle" : "pencil")
                .font(.headline)

            HStack(spacing: 8) {
                TextField("id (e.g. groq)", text: $draft.id)
                    .textFieldStyle(.roundedBorder)
                    .disabled(editing != nil)
                    .accessibilityLabel("Provider id")
                Picker("", selection: $draft.kind) {
                    Text("OpenAI-compatible").tag("openai")
                    Text("Anthropic").tag("anthropic")
                    Text("Ollama").tag("ollama")
                }
                .labelsHidden()
                .frame(width: 190)
                .accessibilityLabel("Provider kind")
            }

            TextField("base_url (e.g. https://api.groq.com/openai/v1)", text: $draft.baseURL)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("Provider base URL")
            Text("The base, not the full endpoint. For OpenAI-compatible that is the part ending in "
                 + "/v1 — paste a full .../chat/completions and it will be reduced to its base, with "
                 + "a note saying so.")
                .font(.caption2).foregroundStyle(.secondary)

            HStack(spacing: 8) {
                SecureField("api_key (or leave blank and use a variable)", text: $draft.apiKey)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("API key")
                TextField("api_key_env (e.g. GROQ_API_KEY)", text: $draft.apiKeyEnv)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Environment variable holding the key")
            }
            Text("Prefer an environment variable: it is not read into logs or traces. A key typed here "
                 + "is written to credentials.json, which is 0600 and gitignored.")
                .font(.caption2).foregroundStyle(.secondary)

            headerEditor

            HStack(spacing: 8) {
                Button {
                    Task { await runTest() }
                } label: {
                    Label("Test and fetch models", systemImage: "bolt.horizontal.circle")
                }
                .disabled(!draft.isComplete || controller.engineState != .running)
                .help("Ask the endpoint for its model list before saving anything")
                .accessibilityLabel("Test this provider and fetch its models")

                Button {
                    Task { await save() }
                } label: {
                    Label(editing == nil ? "Save provider" : "Update provider",
                          systemImage: "square.and.arrow.down")
                }
                .disabled(!draft.isComplete || controller.engineState != .running)
                .accessibilityLabel(editing == nil ? "Save the provider" : "Update the provider")

                if editing != nil {
                    Button("Cancel") { reset() }
                        .accessibilityLabel("Cancel editing")
                }
                Spacer()
            }

            testResultView
        }
        .padding(10)
        .background(Color.blue.opacity(0.06))
        .cornerRadius(8)
    }

    private var headerEditor: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Extra headers").font(.caption)
                Spacer()
                Button {
                    headerRows.append(HeaderRow())
                } label: {
                    Image(systemName: "plus")
                }
                .buttonStyle(.borderless)
                .help("Add a request header — a gateway routing key, an organisation id, and so on")
                .accessibilityLabel("Add a header")
            }
            ForEach($headerRows) { $row in
                HStack(spacing: 6) {
                    TextField("Name", text: $row.name)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("Header name")
                    TextField("Value", text: $row.value)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("Header value")
                    Button {
                        headerRows.removeAll { $0.id == row.id }
                    } label: {
                        Image(systemName: "minus.circle")
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("Remove this header")
                }
            }
        }
    }

    @ViewBuilder
    private var testResultView: some View {
        if !testResult.isEmpty {
            let ok = testResult["ok"]?.boolValue ?? false
            let count = testResult["model_count"]?.intValue ?? 0
            let reason = testResult["reason"]?.stringValue ?? ""
            let note = testResult["note"]?.stringValue ?? ""
            VStack(alignment: .leading, spacing: 4) {
                Label(ok ? "Connected — \(count) model(s)" : "Not usable",
                      systemImage: ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(ok ? .green : .orange)
                    .accessibilityLabel(ok ? "Connected" : "Not usable: \(reason)")
                // Said plainly, because it changes the URL the form holds: a bare "connected" would
                // leave the operator believing the endpoint they pasted was used verbatim.
                if !note.isEmpty {
                    Label(note, systemImage: "wand.and.stars")
                        .font(.caption2).foregroundStyle(.blue)
                        .accessibilityLabel("URL adjusted: \(note)")
                }
                if !reason.isEmpty {
                    Text(reason).font(.caption2).foregroundStyle(.secondary).lineLimit(3)
                }
                if let models = testResult["models"]?.arrayValue, !models.isEmpty {
                    Text(models.prefix(12).compactMap { $0.objectValue?["model_id"]?.stringValue }
                            .joined(separator: ", ")
                         + (models.count > 12 ? " …" : ""))
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
        }
    }

    // MARK: actions

    private func beginEdit(_ provider: [String: JSONValue]) {
        editing = provider["id"]?.stringValue
        draft = ProviderDraft(existing: provider)
        // The engine reports header *names*, not values, so an edit starts with the pairs named but
        // unset — the user re-enters a value rather than the app pretending it knows one.
        headerRows = (provider["headers"]?.arrayValue ?? []).compactMap { $0.stringValue }
            .map { HeaderRow(name: $0, value: "") }
        testResult = [:]
    }

    private func reset() {
        editing = nil
        draft = ProviderDraft()
        headerRows = []
        testResult = [:]
    }

    private func draftWithHeaders() -> ProviderDraft {
        var out = draft
        var headers: [String: String] = [:]
        for row in headerRows {
            let name = row.name.trimmingCharacters(in: .whitespaces)
            if !name.isEmpty { headers[name] = row.value }
        }
        out.headers = headers
        return out
    }

    private func runTest() async {
        let result = await controller.testProvider(draftWithHeaders())
        testResult = result
        // Adopt the corrected base so the form shows what will actually be saved. Leaving the pasted
        // full endpoint in the field while the engine used the base would make the two disagree, and
        // the next Save would look like it changed nothing.
        if let corrected = result["base_url"]?.stringValue, !corrected.isEmpty {
            draft.baseURL = corrected
        }
    }

    private func save() async {
        if await controller.saveProvider(draftWithHeaders()) {
            reset()
        }
        await controller.loadProviders()
    }
}

/// One configured provider, with its reachability and model count.
struct ProviderRow: View {
    let provider: [String: JSONValue]
    let onEdit: () -> Void
    let onRemove: () -> Void

    private var tone: Color {
        switch provider["status"]?.stringValue {
        case "probed": return .green
        case "down", "error", "misconfigured": return .orange
        default: return .secondary
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: provider["has_key"]?.boolValue == true
                  ? "key.fill" : "key")
                .foregroundStyle(provider["has_key"]?.boolValue == true ? .green : .secondary)
                .accessibilityLabel(provider["has_key"]?.boolValue == true
                                    ? "A key is configured" : "No key configured")
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(provider["id"]?.stringValue ?? "?")
                        .font(.system(.body, design: .monospaced))
                    Text(provider["kind"]?.stringValue ?? "")
                        .font(.caption2).foregroundStyle(.secondary)
                    Text(provider["status"]?.stringValue ?? "")
                        .font(.caption2).foregroundStyle(tone)
                }
                Text(provider["base_url"]?.stringValue ?? "")
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Text("\(provider["model_count"]?.intValue ?? 0) model(s)")
                .font(.caption2).foregroundStyle(.secondary)
            Button("Edit", action: onEdit)
                .accessibilityLabel("Edit \(provider["id"]?.stringValue ?? "provider")")
            Button("Remove", action: onRemove)
                .accessibilityLabel("Remove \(provider["id"]?.stringValue ?? "provider")")
        }
        .padding(8)
        .background(tone.opacity(0.06))
        .cornerRadius(6)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - People (the roster)

/// Hire, edit and retire the agents — the console's missing half.
struct PeoplePanel: View {
    @ObservedObject var controller: OrgController

    @State private var draft = AgentDraft()
    @State private var editingId: String?
    @State private var original: AgentDraft?
    @State private var confirmRetire: [String: JSONValue]?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if controller.engineState != .running {
                    Label("The engine is not running, so agents cannot be hired or edited. "
                          + "Press ⌘⇧L (Launch Engine) first.",
                          systemImage: "exclamationmark.triangle.fill")
                        .font(.callout)
                        .foregroundStyle(.orange)
                        .accessibilityLabel("The engine is not running; launch it to edit agents")
                }
                HStack(spacing: 18) {
                    Metric(label: "Agents", value: "\(controller.roster.count)")
                    Metric(label: "Hired", value: "\(hiredCount)",
                           detail: "by you", tone: .green)
                    Metric(label: "Built-in", value: "\(builtInCount)")
                }

                ForEach(controller.roster, id: \.self) { agent in
                    EditableAgentRow(
                        agent: agent,
                        onEdit: { beginEdit(agent) },
                        onRetire: { confirmRetire = agent })
                }

                Divider()
                editor

                if !controller.rosterPath.isEmpty {
                    Text("Saved to \(controller.rosterPath). Editing keeps an agent's id, so its "
                         + "history and health record are preserved.")
                        .font(.caption2).foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        // All three, because the hire form depends on all three: the roster to list agents, the
        // providers to choose an endpoint, and the models to choose one of *that* provider's models.
        // Loading only the roster was the bug — the provider picker and the model picker were then
        // empty, so a hire appeared impossible even though the engine could serve it.
        .task {
            await controller.loadRoster()
            await controller.loadProviders()
            await controller.loadModels()
        }
        .alert("Retire this agent?", isPresented: Binding(
            get: { confirmRetire != nil },
            set: { if !$0 { confirmRetire = nil } })) {
            Button("Retire", role: .destructive) {
                if let id = confirmRetire?["id"]?.stringValue {
                    Task { await controller.retireAgent(id: id, reason: "retired from the console") }
                }
                confirmRetire = nil
            }
            Button("Cancel", role: .cancel) { confirmRetire = nil }
        } message: {
            Text("\(confirmRetire?["name"]?.stringValue ?? "This agent") leaves the roster. Work it "
                 + "already produced is kept.")
        }
    }

    private var editor: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(editingId == nil ? "Hire an agent" : "Edit \(draft.name)",
                  systemImage: editingId == nil ? "person.badge.plus" : "pencil")
                .font(.headline)

            HStack(spacing: 8) {
                TextField("Name", text: $draft.name)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Agent name")
                TextField("Title (optional)", text: $draft.title)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Agent title")
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
                .accessibilityLabel("Agent skill")

                Picker("Level", selection: $draft.level) {
                    ForEach(["junior", "practitioner", "senior", "staff", "principal"], id: \.self) {
                        Text($0.capitalized).tag($0)
                    }
                }
                .frame(width: 160)
                .accessibilityLabel("Agent level")
            }

            HStack(spacing: 8) {
                Picker("Provider", selection: $draft.provider) {
                    Text("Choose a provider…").tag("")
                    ForEach(controller.providers, id: \.self) { provider in
                        Text(provider["id"]?.stringValue ?? "?").tag(provider["id"]?.stringValue ?? "")
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

            HStack(spacing: 8) {
                TextField("Team (optional)", text: $draft.team)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Team")

                Button {
                    Task { await submit() }
                } label: {
                    Label(editingId == nil ? "Hire" : "Save changes",
                          systemImage: editingId == nil ? "person.crop.circle.badge.plus" : "checkmark.circle")
                }
                .disabled(!draft.isComplete || controller.engineState != .running)
                .accessibilityLabel(editingId == nil ? "Hire this agent" : "Save the agent changes")

                if editingId != nil {
                    Button("Cancel") { reset() }
                        .accessibilityLabel("Cancel editing")
                }
                Spacer()
            }

            if editingId != nil {
                Text("Editing keeps \(draft.name)'s identity — its mailbox, session history and health "
                     + "record stay with it.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .background(Color.green.opacity(0.06))
        .cornerRadius(8)
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
                + (error.isEmpty ? "Type the model name, or test the provider in the Providers tab."
                                 : "Test the provider in the Providers tab — \(error)"),
                systemImage: "info.circle")
                .font(.caption2).foregroundStyle(.secondary).lineLimit(3)
        } else if draft.provider.isEmpty {
            Text("Pick a provider, then choose one of the models it reported. Any OpenAI-compatible "
                 + "endpoint works — the list comes from the provider itself.")
                .font(.caption2).foregroundStyle(.secondary)
        }
    }

    /// How many agents the Owner actually hired, and how many are the built-in company.
    ///
    /// Computed outside the view body because Swift does not allow a multi-line closure inside a
    /// string interpolation — and inlining a filter there is exactly what breaks the build.
    private var hiredCount: Int {
        controller.roster.filter { $0["status"]?.stringValue == "hired" }.count
    }

    private var builtInCount: Int {
        controller.roster.filter { $0["status"]?.stringValue == "built-in" }.count
    }

    private func beginEdit(_ agent: [String: JSONValue]) {
        editingId = agent["id"]?.stringValue
        draft = AgentDraft(existing: agent)
        original = draft
    }

    private func reset() {
        editingId = nil
        original = nil
        draft = AgentDraft()
    }

    private func submit() async {
        if let id = editingId {
            await controller.updateAgent(id: id, draft: draft, original: original)
        } else {
            await controller.hireAgent(draft)
        }
        reset()
        await controller.loadRoster()
    }
}

/// One editable agent, with what it is and what it runs on.
///
/// Distinct from the Org panel's `AgentRow`, which is a read-only view of live state: this one
/// carries actions, and mixing the two would either put buttons where the org-health view has none or
/// strip the editing affordances out of the People panel.
struct EditableAgentRow: View {
    let agent: [String: JSONValue]
    let onEdit: () -> Void
    let onRetire: () -> Void

    private var status: String { agent["status"]?.stringValue ?? "built-in" }

    private var tone: Color {
        switch status {
        case "hired": return .green
        case "owner": return .blue
        default: return .secondary
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: status == "owner" ? "crown" :
                    (status == "hired" ? "person.fill.checkmark" : "person.fill"))
                .foregroundStyle(tone)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(agent["name"]?.stringValue ?? "?")
                        .font(.system(.body, design: .monospaced))
                    Text(status).font(.caption2).foregroundStyle(tone)
                    if let title = agent["title"]?.stringValue, !title.isEmpty {
                        Text(title).font(.caption2).foregroundStyle(.secondary)
                    }
                }
                Text((agent["skill"]?.stringValue
                      ?? agent["skills"]?.arrayValue?.first?.stringValue ?? "")
                     + " · \(agent["provider"]?.stringValue ?? "?")/\(agent["model"]?.stringValue ?? "?")")
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            if agent["editable"]?.boolValue == true {
                Button("Edit", action: onEdit)
                    .accessibilityLabel("Edit \(agent["name"]?.stringValue ?? "agent")")
                if status == "hired" {
                    Button("Retire", action: onRetire)
                        .accessibilityLabel("Retire \(agent["name"]?.stringValue ?? "agent")")
                }
            } else {
                Text("authority").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(8)
        .background(tone.opacity(0.06))
        .cornerRadius(6)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(agent["name"]?.stringValue ?? "agent"), \(status), "
                            + "\(agent["provider"]?.stringValue ?? "") \(agent["model"]?.stringValue ?? "")")
    }
}
