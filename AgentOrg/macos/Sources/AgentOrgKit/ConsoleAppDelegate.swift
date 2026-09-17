//
//  ConsoleAppDelegate.swift
//  AgentOrgKit
//
//  The application lifecycle policy: stay resident, be a real app, and never orphan the engine.
//
//  WHY THIS LIVES IN THE KIT
//  -------------------------
//  `AgentOrgKit` exists so everything without view code is unit-testable — the process bridge, the
//  protocol models, the safe writer. The app-delegate policy belongs there for the same reason: these
//  three decisions *are* the background-running behaviour, and a behaviour nobody can assert is a
//  behaviour that regresses silently. A delegate in the executable target can only be exercised by
//  launching the app and closing a window, which is not a test.
//
//  The three decisions, each answering to a documented failure:
//
//  1. **`applicationShouldTerminateAfterLastWindowClosed` → false.** Closing the window must not quit
//     the app. A goal can still be running, and `macos-menu-bar-apps.md` names the opposite (`true`) as
//     the reason a background app "quits when the last window closes". This is what makes background
//     running true rather than aspirational.
//  2. **`setActivationPolicy(.regular)`.** The reference warns that a non-document app defaults to
//     `.prohibited`: no Dock icon, no app-switcher entry, unresponsive menus. One line, set before any
//     window is shown.
//  3. **Stop the engine on terminate.** Otherwise quitting orphans the engine subprocess, which keeps
//     running the code it was started with and holding the project — so the next launch reuses it and
//     the app appears to run the *old* build however many times it is rebuilt.

import AppKit
import Foundation

/// The app's lifecycle policy, injected with the controller it must stop on quit.
@MainActor
public final class ConsoleAppDelegate: NSObject, NSApplicationDelegate {

    /// Weak, so the delegate does not extend the controller's lifetime and no cycle is created through
    /// the app struct. Set by the `App` immediately after it builds the controller.
    public static weak var controller: OrgController?

    public override init() { super.init() }

    /// A regular app: Dock icon, menu bar, app-switcher entry.
    ///
    /// Set in `applicationWillFinishLaunching` — before any window is shown — because the reference is
    /// explicit that it must precede `activate(ignoringOtherApps:)` and that the default for a
    /// non-document app leaves it invisible.
    public func applicationWillFinishLaunching(_ notification: Notification) {
        // `NSApplication.shared` rather than `NSApp`: the latter is an implicitly-unwrapped optional, so
        // it traps when there is no running application — which is the case in a test process, and was
        // the crash that made this policy untestable. `shared` returns the instance either way.
        NSApplication.shared.setActivationPolicy(.regular)
    }

    /// Stop the engine, so quitting does not leave it running against the same project.
    public func applicationWillTerminate(_ notification: Notification) {
        ConsoleAppDelegate.controller?.stop()
    }

    /// **The background decision.** Closing the last window leaves the app resident with its menu-bar
    /// item rather than quitting; Quit stays explicit (⌘Q) and reachable from the menu bar.
    public func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
