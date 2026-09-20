//
//  App.swift
//  AgentOrg
//
//  The application entry point and the window that hosts the console.
//
//  WHY THE WINDOW IS ONE VIEW WITH FOUR DESTINATIONS
//  ------------------------------------------------
//  The owner's attention is the scarce resource here as much as the model's is. Separate windows that
//  must be arranged before a run can be watched would mean the console costs attention instead of
//  saving it, so the destinations live in one window — and there are four of them, not twelve, because
//  a list whose rows overlap is a list a person has to learn.
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
    /// Which destination the ⌘, command should select. A tiny observable rather than a plain static,
    /// because the menu command and the window have to agree about it and the menu lives outside the
    /// window's view hierarchy.
    @ObservedObject private var router = DestinationRouter.shared

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
                .frame(minWidth: 1_020, minHeight: 640)
        }
        .windowToolbarStyle(.unified)
        .windowResizability(.contentMinSize)
        .commands { commands }

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
    ///
    /// A gate outranks everything: an app that has stopped and needs a person must not be represented
    /// by the same glyph as one working happily, or the whole reason the menu-bar item exists is lost.
    private var menuBarSymbol: String {
        if controller.spine.needsAPerson { return "hand.raised.fill" }
        if controller.engineState == .failed { return "exclamationmark.triangle" }
        if controller.goal["live"]?.boolValue == true { return "target" }
        if controller.engineState == .running { return "circle.fill" }
        return "circle.dotted"
    }

    private var menuBarAccessibilityLabel: String {
        if controller.spine.needsAPerson { return "AgentOrg, a gate is waiting for you" }
        if controller.goal["live"]?.boolValue == true { return "AgentOrg, a goal is running" }
        switch controller.engineState {
        case .running: return "AgentOrg, engine running"
        case .failed: return "AgentOrg, the engine failed"
        default: return "AgentOrg, engine idle"
        }
    }

    /// The menu bar, with the standard shape the HIG expects: state first, the actions that matter,
    /// then Quit last and explicitly.
    ///
    /// Every command here goes through the same controller method the window's own control calls, so
    /// the menu can never do something subtly different from the button beside it. The previous build's
    /// ⌘R started a run with **no goal at all** while the visible Start button used the field's text —
    /// two controls, one shortcut, two behaviours — and that class of divergence is what the shared
    /// call sites below exist to prevent.
    // `@CommandsBuilder`, not `@ViewBuilder`. This property yields `Commands`, and `ViewBuilder`
    // requires `View` — so with the wrong builder every element in the body was checked against the
    // `View` requirement, and the first one was rejected with a message that named the element rather
    // than the real cause: "static method 'buildExpression' requires that 'CommandGroup<EmptyView>'
    // conform to 'View'". Swift 6.4 resolves the ambiguity; Swift 6.3.3, which the CI runner has,
    // does not — so the app built locally and failed on the runner. Found by CI.
    @CommandsBuilder
    private var commands: some Commands {
        // `EmptyView()` is written out rather than leaving the closure empty: an empty body infers the
        // content type, and naming it removes a second inference from the same expression.
        CommandGroup(replacing: .newItem) { EmptyView() }
        CommandGroup(after: .newItem) {
            Button("Open Project…") {
                openProjectPicker(controller: controller) { controller.confirmProject() }
            }
            .keyboardShortcut("o", modifiers: [.command])
            .disabled(!controller.canLaunch)
            Divider()
            Button("Show Console") { ConsoleWindow.show() }
                .keyboardShortcut("0", modifiers: [.command])
        }

        // ⌘, is the conventional Preferences shortcut, and this is where every preference now lives —
        // so it selects the destination rather than opening a second window with a subset of the same
        // controls, which is what the audit found the old `Settings` scene to be.
        CommandGroup(replacing: .appSettings) {
            Button("Setup…") {
                ConsoleWindow.show()
                DestinationRouter.shared.select(.setup)
            }
            .keyboardShortcut(",", modifiers: [.command])
        }

        CommandMenu("Run") {
            if controller.engineState.isLive {
                Button("Stop the Engine") { controller.stop() }
                    .keyboardShortcut(".", modifiers: [.command, .shift])
            } else {
                Button("Start the Engine") { controller.launch() }
                    .keyboardShortcut("l", modifiers: [.command, .shift])
                    .disabled(!controller.canLaunch)
            }
            Divider()
            // The same two methods the Now pane's buttons call, with the same goal string. Both are
            // disabled without a goal, because a run with no goal is refused by the engine anyway —
            // and the old shortcut started one with `goal: nil`, which is how it diverged.
            Button("Start Run") {
                let goal = controller.goalDraft
                Task { await controller.startRun(goal: goal) }
            }
            .keyboardShortcut("r", modifiers: [.command])
            .disabled(controller.engineState != .running || controller.goalDraft.isEmpty)

            Button("Plan Only") {
                let goal = controller.goalDraft
                Task { await controller.startRun(goal: goal, dryRun: true) }
            }
            .keyboardShortcut("r", modifiers: [.command, .shift])
            .disabled(controller.engineState != .running || controller.goalDraft.isEmpty)

            Button("Keep Working Until Done") {
                let goal = controller.goalDraft
                Task { await controller.setGoal(goal, arm: true) }
            }
            .keyboardShortcut("g", modifiers: [.command, .shift])
            .disabled(controller.engineState != .running || controller.goalDraft.isEmpty)

            Divider()
            Button("Pause the Run") { Task { await controller.pause() } }
                .keyboardShortcut(".", modifiers: [.command])
                // Disabled unless a run is actually live. `runStatus` is the whole status payload from
                // the engine, so it is non-empty as soon as any poll has been answered — the old
                // `!runStatus.isEmpty` test therefore disabled Pause exactly when a run existed, and
                // enabled it when the engine was stopped and `pause` could only be refused. Mirrors
                // the goal menu's own `goal["live"] != true` idiom below.
                .disabled(controller.runStatus["running"]?.boolValue != true)
            Button("Refresh") { Task { await controller.refresh() } }
                .keyboardShortcut("r", modifiers: [.command, .option])
        }

        // The goal's own controls, in their own menu, with their own words: "Pause the Goal" is not
        // "Pause the Run", and the two used to share a label as well as a shortcut key.
        CommandMenu("Goal") {
            Button("Pause the Goal") { Task { await controller.pauseGoal() } }
                .disabled(controller.goal["live"]?.boolValue != true)
            Button("Resume the Goal") { Task { await controller.resumeGoal() } }
                .disabled(controller.goal["live"]?.boolValue == true)
            Button("Clear the Goal") { Task { await controller.clearGoal() } }
                .disabled((controller.goal["state"]?.stringValue ?? "cleared") == "cleared")
        }

        CommandGroup(after: .sidebar) {
            Button("Toggle Terminal") { TerminalVisibility.shared.toggle() }
                .keyboardShortcut("t", modifiers: [.command, .shift])
        }
    }
}

/// Which destination a command from outside the window should select.
///
/// A tiny observable rather than a static, because the menu command has to be able to open the window
/// and *then* select a destination — and `@SceneStorage` inside `ConsoleView` is not reachable from the
/// menu. `ConsoleView` mirrors this into its scene storage, so a selection made either way persists.
@MainActor
final class DestinationRouter: ObservableObject {
    static let shared = DestinationRouter()

    @Published var requested: Destination?

    func select(_ destination: Destination) {
        requested = destination
    }

    private init() {}
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
