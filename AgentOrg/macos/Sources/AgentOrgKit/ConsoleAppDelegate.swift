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
//  decisions *are* the background-running behaviour, and a behaviour nobody can assert is a behaviour
//  that regresses silently. A delegate in the executable target can only be exercised by launching the
//  app and closing a window, which is not a test.
//
//  The decisions, each answering to a documented failure:
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
//  4. **Turn a termination signal into that same terminate.** Otherwise decision 3 is unreachable for
//     the one way the app is most often stopped from outside: SIGTERM takes its default action and
//     kills the process where it stands, so no delegate method runs and the engine is left behind.

import AppKit
import Dispatch
import Foundation

/// The app's lifecycle policy, injected with the controller it must stop on quit.
@MainActor
public final class ConsoleAppDelegate: NSObject, NSApplicationDelegate {

    /// Weak, so the delegate does not extend the controller's lifetime and no cycle is created through
    /// the app struct. Set by the `App` immediately after it builds the controller.
    public static weak var controller: OrgController?

    /// The signal sources, held for as long as the delegate lives.
    ///
    /// **Held, not local.** A `DispatchSourceSignal` stops delivering the moment it is released, so a
    /// source created in a local would silence itself as soon as this method returned — an app that
    /// looked like it handled SIGTERM and did not.
    private var terminationSources: [DispatchSourceSignal] = []

    /// True once a termination signal has been turned into a quit.
    ///
    /// The quit it asks for is then already under way, and a second signal must not start a second one.
    /// This is the only piece of state the signal path adds, and it exists because a stopped app that
    /// keeps receiving signals is ordinary: `pkill` then ⌘Q, a supervisor retrying, a script that
    /// signals every process in a group.
    private var quittingFromSignal = false

    /// How a termination signal becomes a quit.
    ///
    /// **`NSApplication.terminate` is the whole of the graceful path**, not a shortcut to it: it asks
    /// `applicationShouldTerminate`, runs `applicationWillTerminate` — which is where the engine is
    /// stopped — and only then exits. Routing a signal through it is what makes `kill` behave exactly
    /// like ⌘Q, rather than like a second shutdown path beside the delegate's own that could drift out
    /// of step with it.
    ///
    /// Injected rather than called directly so a test can watch a signal become a quit without
    /// terminating the test process, which is the one thing a test must not do.
    var quit: () -> Void = { NSApplication.shared.terminate(nil) }

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
        // Installed here, alongside the activation policy and before any window or engine exists, so
        // there is no window in which a signal could arrive and take its default action — which is the
        // gap this closes, and the only way to close it is to be listening before the engine starts.
        installTerminationSignalHandlers()
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

    // MARK: - Termination signals

    /// Make SIGTERM and SIGINT run the app's own quit instead of their default action.
    ///
    /// **The gap this closes, reproduced end to end.** `applicationWillTerminate` runs only when the
    /// app quits *itself* — ⌘Q, the menu's Quit, anything that reaches `NSApplication.terminate`. A
    /// signal delivered by `kill`/`pkill` never gets that far: SIGTERM's default action stops the
    /// process where it stands, so no delegate method runs, the engine is never asked to stop, and it
    /// is left reparented to `launchd` still holding the project. Measured before this existed:
    /// `kill -TERM` on the running `.app` left the engine alive with `ppid 1`, its
    /// `AGENTORG_PARENT_PID` naming a process that no longer existed, and the next launch would have
    /// started a second engine on the same checkpoint.
    ///
    /// **Ignoring the signal is what makes this safe, and it is strictly safer than the default it
    /// replaces.** `signal(SIGTERM, SIG_IGN)` removes the default action *before* the source is
    /// created, so the signal cannot kill this process in the window between the two; from then on
    /// every termination — however it arrives — goes through the same quit ⌘Q uses. The alternative is
    /// the bug: an app that can still be killed outright is an app that can still orphan its engine.
    /// A `DispatchSourceSignal` rather than a C handler because a C handler may not call AppKit, and a
    /// C handler that merely set a flag would have to leave the default action in place for the flag
    /// never to be read — which is exactly the failure being fixed.
    ///
    /// SIGINT is handled for the same reason as SIGTERM and by the same code: it is the other signal
    /// that ends this process without running the app's own shutdown, and the engine's side of the
    /// contract already treats the two alike (see `serve.py`'s `_install_signal_handlers`).
    ///
    /// Idempotent, so a second call — the app activating, a test installing it explicitly — cannot
    /// create a second source that would ask to quit twice.
    func installTerminationSignalHandlers() {
        guard terminationSources.isEmpty else { return }
        for signum in [SIGTERM, SIGINT] {
            // Order matters: the default action has to be gone before the source exists, or the
            // signal is still fatal in the interval between creating the source and its first run.
            signal(signum, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: signum, queue: .main)
            source.setEventHandler { [weak self] in
                // The handler runs on the main queue, which *is* the main actor; `assumeIsolated`
                // states that rather than hopping, so the quit begins in the same turn the signal is
                // delivered instead of a turn later.
                MainActor.assumeIsolated { self?.handleTerminationSignal() }
            }
            source.resume()
            terminationSources.append(source)
        }
    }

    /// A termination signal arrived: leave through the quit path rather than beneath it.
    private func handleTerminationSignal() {
        guard !quittingFromSignal else { return }
        quittingFromSignal = true
        quit()
    }
}
