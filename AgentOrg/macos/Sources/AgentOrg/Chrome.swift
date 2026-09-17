//
//  Chrome.swift
//  AgentOrg
//
//  The app-level chrome: the Settings scene, the menu-bar item, and the shared terminal toggle.
//
//  WHY THESE ARE HERE AND NOT IN A PANEL
//  -------------------------------------
//  A panel answers a question about the *run*. These three answer questions about the *app* — where its
//  engine and project are, what it is doing while the window is closed, and whether the terminal is
//  showing. Keeping them separate is what lets the panels stay about the work.
//
//  The two skills that shaped this file:
//
//  - `swiftui-appkit-selection-guide.md`: "A `Settings` scene in 20 lines vs 200" — so Preferences is a
//    `Form` in a `Settings` scene rather than a hand-built window. macOS then supplies the app-menu
//    entry and the ⌘, shortcut for free, which is exactly the HIG expectation.
//  - `macos-menu-bar-apps.md`: a resident app needs a `MenuBarExtra` with `.menuBarExtraStyle(.window)`
//    and an explicit Quit, because with the last window closed there is otherwise no way out.

import SwiftUI
import AppKit
import AgentOrgKit

// MARK: - Terminal visibility

/// Whether the terminal column is showing.
///
/// A small observable rather than a plain `Bool`, because the toggle lives in the **menu bar**, which is
/// outside the window's view hierarchy — so `@State` in the window cannot be reached from it. One shared
/// source is what keeps the ⌘⇧T command and the menu-bar checkbox from disagreeing.
@MainActor
final class TerminalVisibility: ObservableObject {
    static let shared = TerminalVisibility()
    /// Persisted, so the layout someone chose survives a relaunch. `UserDefaults` rather than
    /// `@SceneStorage` because this object is shared with the menu bar, which is outside the window's
    /// scene and so cannot read scene storage.
    @Published var isVisible: Bool {
        didSet { UserDefaults.standard.set(isVisible, forKey: Self.key) }
    }
    private static let key = "console.terminalVisible"

    private init() {
        // Default to showing it: a run is the thing being watched, and an empty terminal is a useful
        // prompt rather than noise. A stored value wins once the user has expressed a preference.
        if UserDefaults.standard.object(forKey: Self.key) == nil {
            isVisible = true
        } else {
            isVisible = UserDefaults.standard.bool(forKey: Self.key)
        }
    }

    func toggle() { isVisible.toggle() }
}

// MARK: - Settings

/// Preferences: where the engine, the project and the skills live.
///
/// A `Form` with `Section`s, which is the idiomatic macOS settings shape — system spacing, aligned
/// labels, and the right control widths without any manual layout.
struct SettingsView: View {
    @ObservedObject var controller: OrgController

    var body: some View {
        TabView {
            engineTab
                .tabItem { Label("Engine", systemImage: "gearshape.2") }
            projectTab
                .tabItem { Label("Project", systemImage: "folder") }
        }
        .frame(width: 620, height: 380)
        .padding(.top, 8)
    }

    private var engineTab: some View {
        Form {
            Section("Interpreter") {
                LabeledContent("Runtime", value: controller.runtimeDescription)
                LabeledContent("Status", value: controller.engineState.rawValue.capitalized)
                if let error = controller.engineError {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                        .textSelection(.enabled)
                }
            }

            Section("Credentials") {
                LabeledContent("File") {
                    Text(controller.credentialsPath)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .foregroundStyle(.secondary)
                }
                Text("Keys live in this 0600 file. Add or test a provider in the Providers tab; a key is "
                     + "never sent back to this window.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Section {
                HStack {
                    Button("Launch engine") { controller.launch() }
                        .disabled(!controller.canLaunch || controller.engineState.isLive)
                    Button("Stop") { controller.stop() }
                        .disabled(!controller.engineState.isLive)
                    Spacer()
                    Button("Refresh") { Task { await controller.refresh() } }
                        .disabled(controller.engineState != .running)
                }
            }
        }
        .formStyle(.grouped)
    }

    private var projectTab: some View {
        Form {
            Section("Attached project") {
                LabeledContent("Folder") {
                    Text(controller.projectPath)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                }
                if controller.workspace["attached"]?.boolValue == true {
                    Label("The agents work in your own folder. Engine state is kept in "
                          + ".agent_state/ inside it.", systemImage: "checkmark.seal.fill")
                        .font(.caption).foregroundStyle(.green)
                } else {
                    Label("A managed project the engine owns, under AgentOrg/projects/.",
                          systemImage: "shippingbox")
                        .font(.caption).foregroundStyle(.secondary)
                }
                HStack {
                    Button("Choose folder…") { openProjectPicker(controller: controller) }
                        .disabled(!controller.canLaunch)
                    Button("Use a managed project") {
                        Task { await controller.detachProject() }
                    }
                    .disabled(controller.workspace["attached"]?.boolValue != true)
                }
            }

            Section("Skills library") {
                LabeledContent("Root") {
                    Text(controller.libraryPath)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .foregroundStyle(.secondary)
                }
                LabeledContent("Skills available", value: "\(controller.skills.count)")
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - The menu-bar item

/// What the menu bar shows: the state, the one next action, and Quit.
///
/// Deliberately short. A menu-bar item is a glance, not a second console — the reference is explicit
/// that the panel is for reachability (`Show Console`), and everything longer belongs in the window.
struct MenuBarPanel: View {
    @ObservedObject var controller: OrgController
    @ObservedObject private var terminal = TerminalVisibility.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Circle().fill(stateColour).frame(width: 9, height: 9)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 1) {
                    // Spelled out as well as coloured: a status that exists only as a hue is unreadable
                    // to some people, and this is the surface most likely to be glanced at.
                    Text(stateWord).font(.headline)
                    Text(controller.workspace["name"]?.stringValue ?? controller.projectPath)
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("AgentOrg: \(stateWord)")

            if controller.goal["live"]?.boolValue == true || !goalObjective.isEmpty {
                Divider()
                Label(goalObjective.isEmpty ? "No goal" : goalObjective,
                      systemImage: "target")
                    .font(.caption).lineLimit(2)
                    .foregroundStyle(controller.goal["live"]?.boolValue == true ? .green : .secondary)
            }

            if let gate = controller.pendingGate {
                Label(gate["reason"]?.stringValue ?? "Waiting on you",
                      systemImage: "hand.raised.fill")
                    .font(.caption).foregroundStyle(.orange).lineLimit(2)
            }

            Divider()

            if controller.engineState.isLive {
                Button("Stop the engine") { controller.stop() }
            } else {
                Button("Launch the engine") { controller.launch() }
                    .disabled(!controller.canLaunch)
            }
            Button("Show Console") { ConsoleWindow.show() }
            Toggle("Show Terminal", isOn: $terminal.isVisible)

            Divider()
            Button("Quit AgentOrg") { NSApp.terminate(nil) }
                .keyboardShortcut("q")
        }
        .padding(12)
        .frame(width: 300)
    }

    private var goalObjective: String {
        controller.goal["objective"]?.stringValue ?? ""
    }

    private var stateWord: String {
        if controller.goal["live"]?.boolValue == true { return "Goal running" }
        switch controller.engineState {
        case .running: return "Engine running"
        case .failed: return "Engine failed"
        case .launching, .pausing, .terminating: return "Working…"
        default: return "Idle"
        }
    }

    private var stateColour: Color {
        if controller.goal["live"]?.boolValue == true { return .green }
        switch controller.engineState {
        case .running: return .green
        case .failed: return .red
        default: return .secondary
        }
    }
}
