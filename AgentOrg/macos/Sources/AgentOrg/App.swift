//
//  App.swift
//  AgentOrg
//
//  The application entry point and the window that hosts the console.
//
//  WHY THE WINDOW IS ONE VIEW WITH A PICKER RATHER THAN SIX WINDOWS
//  ----------------------------------------------------------------
//  The owner's attention is the scarce resource here as much as the model's is. Six windows that must be
//  arranged before a run can be watched would mean the console costs attention instead of saving it. So
//  the panels are tabs, each answering one question, and the terminal is always present because a run is
//  the thing being watched.
//
//  `@MainActor` throughout, and no I/O in any view: everything goes through `OrgController`, which the
//  kit keeps off the main actor where it matters.

import SwiftUI
import AppKit
import AgentOrgKit

@main
struct AgentOrgApp: App {
    /// The controller is created once, at launch, so a run survives a window being closed and reopened.
    @StateObject private var controller: OrgController
    /// The lifecycle policy — resident on window close, regular activation, engine stopped on quit —
    /// lives in `AgentOrgKit.ConsoleAppDelegate` so it can be unit-tested rather than only observed.
    @NSApplicationDelegateAdaptor(ConsoleAppDelegate.self) private var appDelegate

    init() {
        // Resolve the repository root from where this executable actually lives.
        //
        // `Bundle.main.bundleURL` is the *directory* holding the binary (`.build/debug`), not the
        // binary itself — a subtlety that made the previous five-step walk land one level too high
        // (`.../Projects` instead of `.../Projects/Agent`), so the app looked for its engine in a
        // directory that does not exist and sat idle forever with no explanation.
        //
        // The fix is not just the right number of steps, because the layout differs between
        // `swift run`, `swift build`, and a future `.app` bundle. So the root is *searched for*: walk
        // up from the executable until a directory actually contains `AgentOrg/engine/cli.py`, and
        // fall back to the working directory when nothing matches.
        let settings = OrgController.OrgSettings.discover(
            repositoryRoot: AgentOrgApp.repositoryRoot())
        let made = OrgController(settings: settings)
        _controller = StateObject(wrappedValue: made)
        // The delegate holds a weak reference, so a quit handler can stop the engine without the
        // delegate extending the controller's lifetime (or creating a cycle through the app struct).
        ConsoleAppDelegate.controller = made
    }

    /// The repository root, found by walking up until the engine is actually there.
    ///
    /// A structural search rather than a fixed number of `deletingLastPathComponent()` calls: the
    /// count differs between a SwiftPM debug build, a release build, and a bundled `.app`, so a
    /// hardcoded depth is correct in exactly one of them and silently wrong in the others. Looking
    /// for `AgentOrg/engine/cli.py` is true in all of them.
    static func repositoryRoot(fileManager: FileManager = .default) -> URL {
        let start = Bundle.main.executableURL?.deletingLastPathComponent()
            ?? Bundle.main.bundleURL
        var candidate = start
        for _ in 0..<8 {
            if fileManager.fileExists(
                atPath: candidate.appendingPathComponent("AgentOrg/engine/cli.py").path) {
                return candidate
            }
            let parent = candidate.deletingLastPathComponent()
            // At the filesystem root there is nowhere further up.
            if parent.path == candidate.path { break }
            candidate = parent
        }
        // Nothing matched — running from somewhere unrelated. The working directory is the honest
        // guess, and `discover` reports a missing engine rather than pretending otherwise.
        return URL(fileURLWithPath: fileManager.currentDirectoryPath)
    }

    var body: some Scene {
        WindowGroup("AgentOrg") {
            ConsoleView(controller: controller)
                // A floor rather than a fixed size: the sidebar and the terminal both need room, and an
                // owner with a large display should be able to use it.
                .frame(minWidth: 1_040, minHeight: 660)
        }
        .windowToolbarStyle(.unified)
        .windowResizability(.contentMinSize)
        .commands { commands }

        // Preferences in their conventional place, with the conventional shortcut. A `Settings` scene
        // is the whole implementation — the reference notes it replaces ~200 lines of NSViewController
        // and Auto Layout — and macOS puts it under the app menu as "Settings…" with ⌘, for free.
        Settings {
            SettingsView(controller: controller)
        }

        // The background presence. A menu-bar item is what makes "still running" visible and reachable
        // after the window is closed, and it is how a resident app is *quit* deliberately rather than
        // by tidying a window away.
        MenuBarExtra {
            MenuBarPanel(controller: controller)
        } label: {
            // The icon reflects state rather than being a static glyph, so the menu bar answers "is it
            // working?" at a glance — with a template image so it adapts to a dark or light menu bar.
            Image(systemName: menuBarSymbol)
                .accessibilityLabel(menuBarAccessibilityLabel)
        }
        .menuBarExtraStyle(.window)
    }

    /// The status glyph, driven by what the app is actually doing.
    private var menuBarSymbol: String {
        if controller.engineState == .failed { return "exclamationmark.triangle" }
        if controller.goal["live"]?.boolValue == true { return "target" }
        if controller.engineState == .running { return "circle.fill" }
        return "circle.dotted"
    }

    private var menuBarAccessibilityLabel: String {
        if controller.goal["live"]?.boolValue == true { return "AgentOrg, a goal is running" }
        switch controller.engineState {
        case .running: return "AgentOrg, engine running"
        case .failed: return "AgentOrg, the engine failed"
        default: return "AgentOrg, engine idle"
        }
    }

    /// The menu bar, with the standard shape the HIG expects: state first, the actions that matter,
    /// then Quit last and explicitly.
    @ViewBuilder
    private var commands: some Commands {
        CommandGroup(replacing: .newItem) { }
        CommandGroup(after: .newItem) {
            Button("Open Project…") { openProjectPicker(controller: controller) }
                .keyboardShortcut("o", modifiers: [.command])
                .disabled(!controller.canLaunch)
            Divider()
            Button("Show Console") { ConsoleWindow.show() }
                .keyboardShortcut("0", modifiers: [.command])
        }
        CommandMenu("Run") {
            Button("Launch Engine") { controller.launch() }
                .keyboardShortcut("l", modifiers: [.command, .shift])
                .disabled(!controller.canLaunch || controller.engineState.isLive)
            Button("Start Run") {
                Task { await controller.startRun(goal: nil) }
            }
            .keyboardShortcut("r", modifiers: [.command])
            .disabled(controller.engineState != .running)
            Button("Pause") { controller.pause() }
                .keyboardShortcut(".", modifiers: [.command])
            Button("Stop Engine") { controller.stop() }
                .keyboardShortcut(".", modifiers: [.command, .shift])
                .disabled(!controller.engineState.isLive)
            Divider()
            Button("Refresh") { Task { await controller.refresh() } }
                .keyboardShortcut("r", modifiers: [.command, .shift])
        }
        CommandMenu("Goal") {
            Button("Set Goal and Continue") {
                Task { await controller.setGoal(controller.goalDraft, arm: true) }
            }
            .keyboardShortcut("g", modifiers: [.command, .shift])
            .disabled(controller.engineState != .running || controller.goalDraft.isEmpty)
            Button("Pause Goal") { Task { await controller.pauseGoal() } }
                .disabled(controller.goal["live"]?.boolValue != true)
            Button("Resume Goal") { Task { await controller.resumeGoal() } }
                .disabled(controller.goal["live"]?.boolValue == true)
            Button("Clear Goal") { Task { await controller.clearGoal() } }
                .disabled((controller.goal["state"]?.stringValue ?? "cleared") == "cleared")
        }
        CommandGroup(after: .sidebar) {
            Button("Toggle Terminal") { TerminalVisibility.shared.toggle() }
                .keyboardShortcut("t", modifiers: [.command, .shift])
        }
    }
}

/// Bringing the console back after the window was closed.
///
/// With `applicationShouldTerminateAfterLastWindowClosed` returning false the app keeps running, so
/// there has to be a way back to the window. `openWindow` is only reachable from inside a scene, hence
/// this small bridge: the menu-bar item and the menu command both go through it.
@MainActor
enum ConsoleWindow {
    static func show() {
        // `NSApp.activate` first, because the app may not be frontmost — a menu-bar app often is not,
        // and a window that opens behind everything reads as "nothing happened".
        NSApp.activate(ignoringOtherApps: true)
        for window in NSApp.windows where window.canBecomeMain {
            window.makeKeyAndOrderFront(nil)
            return
        }
        // No window survived: ask SwiftUI to make one. `WindowGroup` reopens the default scene.
        NSApp.sendAction(#selector(NSApplication.newWindowForTab(_:)), to: nil, from: nil)
    }
}
