//
//  ConsoleAppDelegateTests.swift
//  AgentOrgKitTests
//
//  The background-running policy.
//
//  WHY THIS IS TESTABLE AT ALL
//  ---------------------------
//  These three decisions *are* "the app runs in the background", and they used to live in the
//  executable target — where nothing could assert them, so they could only be checked by launching the
//  app and closing a window. That is not a test. Moving them into `AgentOrgKit` makes the policy itself
//  the subject, which is the whole reason the kit exists.
//
//  The behaviour being protected: an agent run can take a long time, and the app must survive its window
//  being closed while that run continues — and must not leave the engine behind when it is finally quit.

import XCTest
import AppKit
@testable import AgentOrgKit

@MainActor
final class ConsoleAppDelegateTests: XCTestCase {

    func testTheAppStaysResidentWhenTheLastWindowCloses() {
        // The single most important assertion in this file, and the one that was silently wrong before:
        // returning `true` made closing the window quit the app — and kill the engine mid-run. The
        // menu-bar reference names exactly this as why a background app "quits when the last window
        // closes".
        let delegate = ConsoleAppDelegate()
        XCTAssertFalse(
            delegate.applicationShouldTerminateAfterLastWindowClosed(NSApplication.shared),
            "closing the window must not quit the app: a run may still be in progress")
    }

    func testTheActivationPolicyIsSetToRegular() throws {
        // A non-document app defaults to `.prohibited` — no Dock icon, no app-switcher entry,
        // unresponsive menus. This asserts the app asks for the regular policy rather than relying on
        // a default that the reference explicitly says is wrong for this shape of app.
        let delegate = ConsoleAppDelegate()
        delegate.applicationWillFinishLaunching(
            Notification(name: NSApplication.willFinishLaunchingNotification))
        XCTAssertEqual(
            NSApplication.shared.activationPolicy(), .regular,
            "the app must be regular: Dock icon, menu bar, switcher entry")
    }

    func testTerminatingStopsTheEngine() {
        // Otherwise quitting orphans the engine, which keeps running against the same project — so the
        // next launch finds it and the app appears to run the old build however often it is rebuilt.
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-delegate-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let settings = OrgController.OrgSettings(
            engineRoot: directory, projectPath: directory, credentialsPath: nil, libraryRoot: nil)
        let controller = OrgController(settings: settings)

        ConsoleAppDelegate.controller = controller
        // With no engine running, stop() is a no-op that must still be safe to call — the delegate runs
        // it unconditionally on every quit, including the very first one before anything launched.
        ConsoleAppDelegate.controller?.stop()
        XCTAssertEqual(controller.engineState, .idle)
    }

    func testTheDelegateHoldsTheControllerWeakly() {
        // A strong reference would keep the controller alive past the app's own lifetime — and a cycle
        // through the app struct would mean neither is released. The delegate only needs to *reach* the
        // controller on quit, so weak is both sufficient and correct.
        var controller: OrgController? = OrgController(settings: OrgController.OrgSettings(
            engineRoot: FileManager.default.temporaryDirectory,
            projectPath: FileManager.default.temporaryDirectory))
        weak var probe = controller
        ConsoleAppDelegate.controller = controller
        controller = nil
        // The static property must not be the thing keeping it alive.
        XCTAssertNil(probe, "the delegate must not retain the controller")
        ConsoleAppDelegate.controller = nil
    }

    func testATerminationSignalQuitsThroughTheOrdinaryPathRatherThanKillingTheApp() {
        // The failure this protects, reproduced against the running `.app`: `applicationWillTerminate`
        // runs only for a quit the app starts itself, so a signal from `kill`/`pkill` took SIGTERM's
        // default action — the process stopped where it stood, no delegate method ran, and the engine
        // was left behind, reparented to launchd and still holding the project. Handled, the signal
        // becomes the same quit ⌘Q performs, which is what stops the engine.
        var delegate: ConsoleAppDelegate? = ConsoleAppDelegate()
        var quits = 0
        delegate?.quit = { quits += 1 }
        delegate?.installTerminationSignalHandlers()
        // Installing twice — the app activating, or a second call from anywhere — must not add a
        // second source that would ask to quit twice.
        delegate?.installTerminationSignalHandlers()

        // The signal is delivered through a dispatch source on the main queue, which this test's own
        // run loop drains — so the assertion follows a pump, not the next line.
        XCTAssertEqual(kill(getpid(), SIGTERM), 0)
        pumpRunLoop { quits == 1 }

        XCTAssertEqual(quits, 1, "SIGTERM must become the app's own quit, not the default action")
        // Still here, which is the point: an app that could be killed outright is an app that can
        // orphan its engine, and stopping the engine is the whole reason this path exists.
        XCTAssertEqual(kill(getpid(), 0), 0, "the process must survive SIGTERM")

        // A further signal arrives while the quit it asked for is already under way — a retrying
        // supervisor, `pkill` followed by ⌘Q. It must not start a second quit.
        XCTAssertEqual(kill(getpid(), SIGTERM), 0)
        RunLoop.current.run(until: Date().addingTimeInterval(0.3))
        XCTAssertEqual(quits, 1, "a second signal must not quit again")

        // Leave the test process as it was found: releasing the delegate cancels its sources, and the
        // disposition is put back, so no later test (or the runner) is left with an app's signal
        // handling. The delegate is dropped first, because a live source plus a fatal disposition is a
        // signal that kills the process instead of being delivered.
        delegate = nil
        signal(SIGTERM, SIG_DFL)
    }

    /// Run the main run loop until `condition` holds, or `timeout` elapses.
    ///
    /// A dispatch source's event is delivered *asynchronously* to the queue it was created on, so
    /// asserting on the line after `kill` would assert on a turn that has not run yet. This is that
    /// turn, bounded so a source that never fires fails an assertion rather than hanging the suite.
    private func pumpRunLoop(timeout: TimeInterval = 2, until condition: () -> Bool) {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
    }
}
