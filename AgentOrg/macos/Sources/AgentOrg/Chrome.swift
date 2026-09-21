//
//  Chrome.swift
//  AgentOrg
//
//  The app-level chrome: the menu-bar item, and the shared terminal toggle.
//
//  WHY THESE ARE HERE AND NOT IN A DESTINATION
//  ------------------------------------------
//  A destination answers a question about the *run*. These answer questions about the *app* — what it
//  is doing while the window is closed, and whether the terminal is showing. Keeping them separate is
//  what lets the destinations stay about the work.
//
//  The `Settings` scene is gone on purpose. It held two tabs that duplicated what is now Setup — the
//  interpreter, the credentials path, the launch buttons, the project folder — and the audit found that
//  duplication by reading both. Having one place to configure the app is worth more than matching the
//  convention of a Preferences window, and ⌘, now opens the Setup destination instead, which is where
//  every one of those controls actually lives.

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
        // Default to hiding it: the terminal is a power-user view of a run, and the destinations were
        // redesigned so that everything a person needs — the gate, the next action, the goal — is on
        // the spine without it. Showing it by default put a wall of engine chatter beside a person who
        // had just been told the app was confusing. A stored value wins once they express a preference.
        if UserDefaults.standard.object(forKey: Self.key) == nil {
            isVisible = false
        } else {
            isVisible = UserDefaults.standard.bool(forKey: Self.key)
        }
    }

    func toggle() { isVisible.toggle() }
}

// MARK: - The menu-bar item

/// What the menu bar shows: the state, the one next action, and Quit.
///
/// Deliberately short. A menu-bar item is a glance, not a second console — everything longer belongs in
/// the window. The one thing it *must* do is carry the gate, because a menu-bar app with the window
/// closed has no other way to tell a person that a run is parked waiting for them.
struct MenuBarPanel: View {
    @ObservedObject var controller: OrgController
    @ObservedObject private var terminal = TerminalVisibility.shared

    private var spine: SpineModel { controller.spine }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Label(spine.stateWord, systemImage: spine.stateTone.symbol)
                    .font(.headline)
                    .foregroundStyle(spine.stateTone.colour)
                Spacer()
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("AgentOrg: \(spine.stateWord)")

            Text(spine.projectName)
                .font(.caption).foregroundStyle(.secondary).lineLimit(1)

            if let objective = spine.goalObjective {
                Divider()
                Label(objective, systemImage: "target")
                    .font(.caption).lineLimit(2)
                    .foregroundStyle(spine.goalTone == .ok ? .primary : spine.goalTone.colour)
            }

            // The gate, with its decision controls where the engine says they belong. This is the one
            // panel that matters while the window is shut.
            if let gate = spine.gate {
                Divider()
                Label(gate.reason, systemImage: gate.canAct ? "hand.raised.fill" : "hourglass")
                    .font(.caption).foregroundStyle(gate.canAct ? .orange : .secondary).lineLimit(2)
                // The reason, in both branches. It used to be shown only when the console could *not*
                // act — so the panel that exists for a person whose window is closed told them a gate
                // was waiting, offered Approve and Reject, and said nothing about why it was theirs.
                // The engine's own refusal is in this sentence, and it is the one thing that makes
                // the decision answerable.
                if !gate.why.isEmpty {
                    Text(gate.why).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                }
                if gate.canAct {
                    HStack(spacing: 6) {
                        Button("Approve") { Task { await controller.approve() } }
                            .accessibilityLabel("Approve this gate")
                        Button("Reject") { Task { await controller.reject() } }
                            .accessibilityLabel("Reject this gate")
                    }
                }
            }

            if let next = spine.next, spine.gate == nil {
                Divider()
                Label(next.label, systemImage: "arrow.forward.circle")
                    .font(.caption).lineLimit(2)
                    .foregroundStyle(.secondary)
            }

            Divider()

            // One launch/stop control, named for what it does — the same one the spine carries.
            if controller.engineState.isLive {
                Button("Stop the engine") { controller.stop() }
            } else {
                Button("Start the engine") { controller.launch() }
                    .disabled(!controller.canLaunch)
            }
            Button("Show Console") { ConsoleWindow.show() }
            Toggle("Show Terminal", isOn: $terminal.isVisible)

            Divider()
            Button("Quit AgentOrg") { NSApp.terminate(nil) }
                .keyboardShortcut("q")
        }
        .padding(12)
        .frame(width: 320)
    }
}
