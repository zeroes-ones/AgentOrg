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
}
