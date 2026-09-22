//
//  LibraryRootTests.swift
//  AgentOrgKitTests
//
//  Pinning the Skills library root — the app's answer to "the engine has to go looking for it".
//
//  WHY THIS UNIT IS WORTH ITS OWN FAMILY
//  -------------------------------------
//  Before this, the console always launched the engine with `libraryRoot: nil`, so the child had to
//  discover a checkout by probing, and on this machine the probe lands on `~/Documents/Projects/Skills`
//  — a path macOS gates behind a permission prompt. The engine reaches it from `library.resolve`
//  *before* it reports ready, so the dialog decided whether the engine started at all. Pinning the
//  root removes the search, which is why the pin has to be applied to the very first spawn.
//
//  Two properties here are the whole point and are asserted in both directions:
//
//  - **The app sends what it pinned**, and sends nothing when the value is cleared — asserted against
//    the child's own environment, not against the config the app built.
//  - **The app does not decide whether a path is usable.** That is `engine/library.py`'s answer, so
//    the tests check that the engine's reply is carried through, and that the candidate list the pane
//    shows is the engine's own `unpinned_search_paths()` rather than a copy this app keeps.
//

import XCTest
@testable import AgentOrgKit

@MainActor
final class LibraryRootTests: XCTestCase {

    // MARK: - Where the sources are

    private static func repositoryRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // AgentOrgKitTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // macos
            .deletingLastPathComponent()   // AgentOrg
            .deletingLastPathComponent()   // the repository root
    }

    /// The directory holding `engine/` — four levels up.
    private static func engineRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    // MARK: - The preference

    func testAnEmptyOrWhitespaceRootIsTheSameStateAsNoRoot() {
        // A cleared field produces `""`, and "I cleared it" has to mean the same thing as "I never
        // set it": let the engine discover. Storing `""` as a value would be a pin at a path named
        // " ", which is a different statement from the one the person made.
        let preferences = AppPreferences(store: InMemoryPreferenceStore())
        XCTAssertNil(preferences.libraryRoot, "unset is nil")

        preferences.libraryRoot = "   "
        XCTAssertNil(preferences.libraryRoot, "whitespace is a cleared field")
        preferences.libraryRoot = ""
        XCTAssertNil(preferences.libraryRoot)
        preferences.libraryRoot = nil
        XCTAssertNil(preferences.libraryRoot)

        preferences.libraryRoot = "  /opt/Skills \n"
        XCTAssertEqual(preferences.libraryRoot, "/opt/Skills", "the stored value is trimmed")
    }

    func testThePinnedRootSurvivesARestartButNotTheWizardsReset() {
        // It is remembered like the other preferences — and deliberately *not* cleared by "run setup
        // again", because it is not one of the wizard's questions but a decision about where a
        // dependency lives. A person re-answering the wizard did not ask to forget it.
        let store = InMemoryPreferenceStore()
        AppPreferences(store: store).libraryRoot = "/opt/Skills"

        let afterRelaunch = AppPreferences(store: store)
        XCTAssertEqual(afterRelaunch.libraryRoot, "/opt/Skills")

        afterRelaunch.reset()
        XCTAssertEqual(afterRelaunch.libraryRoot, "/opt/Skills",
                       "reset forgets the wizard's answers, not where the library is")
    }

    // MARK: - The pin reaches the child

    /// A stand-in engine that records the environment variable it was launched with.
    ///
    /// The assertion this exists for is about the *child's* environment: an app that set a field and
    /// never exported the variable would pass any test of its own state, and the engine would still
    /// search. Each launch appends one line, so a relaunch is visible as a second line.
    private func makeEnvironmentRecorder(in root: URL) throws -> URL {
        let script = root.appendingPathComponent("record_env.py")
        let out = root.appendingPathComponent("env.jsonl")
        let source = """
        import json, os, sys

        with open(\(String(reflecting: out.path)), "a") as handle:
            handle.write(json.dumps({"skills_root": os.environ.get("AGENTORG_SKILLS_ROOT", "")}) + "\\n")
            handle.flush()

        sys.stdout.write(json.dumps({"v": 1, "seq": 1, "type": "engine.ready",
                                     "payload": {"pid": os.getpid(), "providers": []}}) + "\\n")
        sys.stdout.flush()
        for line in sys.stdin:
            pass
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        return out
    }

    private func recordedRoots(at path: URL) -> [String] {
        guard let text = try? String(contentsOf: path, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap { line in
            guard let object = try? JSONSerialization.jsonObject(with: Data(line.utf8))
                    as? [String: Any] else { return nil }
            return object["skills_root"] as? String
        }
    }

    private func waitUntil(_ deadline: TimeInterval = 8,
                           _ condition: () -> Bool) async -> Bool {
        let end = Date().addingTimeInterval(deadline)
        while Date() < end {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 25_000_000)
        }
        return condition()
    }

    func testThePinnedRootIsExportedAndClearingItExportsNothing() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-library-env-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let recorded = try makeEnvironmentRecorder(in: root)

        let preferences = AppPreferences(store: InMemoryPreferenceStore())
        preferences.libraryRoot = "/tmp/pinned-skills"
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil)
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: ["python3", root.appendingPathComponent("record_env.py").path])
        let controller = OrgController(settings: settings, maxRestartAttempts: 0,
                                       restartDelay: 0.05, preferences: preferences)
        // The preference is applied to the settings at construction, so the *first* spawn already
        // carries it — which is the whole point: the launch a person never gets to configure must
        // not discover a library.
        XCTAssertEqual(controller.pinnedLibraryRoot, "/tmp/pinned-skills")

        controller.launch()
        let recordedFirstLaunch = await waitUntil { self.recordedRoots(at: recorded).count == 1 }
        XCTAssertTrue(recordedFirstLaunch, "the child never recorded its environment")
        XCTAssertEqual(recordedRoots(at: recorded), ["/tmp/pinned-skills"],
                       "the engine must be told the root, not left to find one")

        // Clear it: the variable must be *absent*, not empty — an empty value would be a pin at the
        // empty path, which is a different statement from "search for one".
        await controller.setLibraryRoot(nil)
        XCTAssertNil(controller.pinnedLibraryRoot)
        XCTAssertFalse(controller.libraryIsPinned)
        let recordedSecondLaunch = await waitUntil { self.recordedRoots(at: recorded).count == 2 }
        XCTAssertTrue(recordedSecondLaunch, "clearing the pin must relaunch the engine")
        XCTAssertEqual(recordedRoots(at: recorded), ["/tmp/pinned-skills", ""],
                       "cleared means the engine discovers, which is the pre-pin behaviour")
        controller.stop()
    }

    // MARK: - What the engine says is carried through, not re-decided

    func testTheEnginesLibrarySentenceAndCandidatesAreCarriedVerbatim() {
        // The engine's `library` reply, shaped the way `serve._cmd_library` shapes it. Nothing here is
        // interpreted: the sentence is shown as the engine wrote it, and the candidate list is the
        // engine's own — a Swift copy of `unpinned_search_paths()` is the drift this asserts against.
        let controller = OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: FileManager.default.temporaryDirectory,
                projectPath: FileManager.default.temporaryDirectory),
            preferences: .ephemeral())
        controller.applyLibrary([
            "ok": .bool(true),
            "root": .string("/opt/Skills"),
            "detail": .string("/opt/Skills at commit abcdef123456 — capabilities checked; "
                              + "content unpinned"),
            "source": .string("pinned"),
            "search_paths": .array([.string("/home/me/Documents/Projects/Skills"),
                                    .string("/home/me/.zeroes-ones/skills")]),
        ])
        XCTAssertEqual(controller.libraryPath, "/opt/Skills")
        XCTAssertEqual(controller.librarySentence,
                       "/opt/Skills at commit abcdef123456 — capabilities checked; content unpinned")
        XCTAssertEqual(controller.librarySearchPaths,
                       ["/home/me/Documents/Projects/Skills", "/home/me/.zeroes-ones/skills"])
        XCTAssertFalse(controller.libraryWasDiscovered)
        XCTAssertFalse(controller.libraryPinNotUsed, "the engine used the pinned root")
    }

    func testAPinnedRootTheEngineDidNotUseIsSaidOutLoud() {
        // The engine's semantic, and the one silent surprise left: `$AGENTORG_SKILLS_ROOT` is a
        // *preferred candidate* to `library.resolve`, so a path with no runner is skipped and another
        // checkout runs. A console that reported "pinned" here would describe a launch that did not
        // happen, so the mismatch is a state of its own.
        let preferences = AppPreferences(store: InMemoryPreferenceStore())
        preferences.libraryRoot = "/tmp/not-a-library"
        let controller = OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: FileManager.default.temporaryDirectory,
                projectPath: FileManager.default.temporaryDirectory),
            preferences: preferences)
        controller.applyLibrary([
            "ok": .bool(true),
            "root": .string("/opt/Skills"),
            "detail": .string("/opt/Skills at commit abcdef123456 — capabilities checked"),
            "source": .string("fallback"),
            "search_paths": .array([]),
        ])
        XCTAssertTrue(controller.libraryPinNotUsed)
        XCTAssertEqual(controller.libraryPath, "/opt/Skills",
                       "the path reported is the one that resolved, not the one that was typed")
    }

    func testAnUnpinnedControllerKnowsNothingAboutAPathUntilTheEngineAnswers() {
        // The old `"(auto-discovered)"` was a phrase pretending to be a path. An unpinned console
        // genuinely does not know which checkout the engine will use — that is why the engine has to
        // search — so it says so rather than inventing one.
        let controller = OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: FileManager.default.temporaryDirectory,
                projectPath: FileManager.default.temporaryDirectory),
            preferences: .ephemeral())
        XCTAssertEqual(controller.libraryPath, "")
        XCTAssertNil(controller.librarySentence)
        XCTAssertTrue(controller.librarySearchPaths.isEmpty)
        XCTAssertEqual(controller.libraryPathDescription,
                       "the engine will look for it itself (not read yet)")
    }

    // MARK: - The hint for a launch that never reported ready

    func testTheProtectedFolderHintNamesTheEnginesOwnCandidates() {
        // The measured failure: the launch's own folders were readable and the child was held on the
        // Skills checkout the engine discovers for itself. The console cannot name a path it was never
        // told — but once the engine has reported where it *would* look, naming those is the
        // difference between moving one folder and filing another report. The list is the engine's;
        // before it has answered, the sentence stands without a list rather than inventing one.
        let home = FileManager.default.homeDirectoryForCurrentUser
        // Part 1: the launch's own folder is gated and the engine has not answered. The possibility
        // is named and no paths are — the console cannot know them yet.
        let gatedLaunch = OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: home.appendingPathComponent("Documents/somewhere"),
                projectPath: home.appendingPathComponent("Documents/somewhere")),
            preferences: .ephemeral())
        let beforeReply = gatedLaunch.protectedFolderHint(engineIsSilent: true)
        XCTAssertNotNil(beforeReply)
        XCTAssertTrue(beforeReply?.contains("The engine also reads the Skills library") ?? false,
                      beforeReply ?? "nil")
        XCTAssertFalse(beforeReply?.contains("looks for it at:") ?? true,
                       "a list the engine never reported must not be rendered")

        // Part 2: the launch's own folders are outside everything protected, so only the *engine's*
        // candidates can produce the hint — and they do, named verbatim.
        let outside = OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: URL(fileURLWithPath: "/tmp/outside-protection"),
                projectPath: URL(fileURLWithPath: "/tmp/outside-protection")),
            preferences: .ephemeral())
        XCTAssertNil(outside.protectedFolderHint(engineIsSilent: true),
                     "nothing gated is known yet, so nothing is claimed")
        let gated = home.appendingPathComponent("Documents/Projects/Skills").path
        outside.applyLibrary(["search_paths": .array([.string(gated),
                                                      .string("/tmp/elsewhere")])])
        let hint = outside.protectedFolderHint(engineIsSilent: true)
        XCTAssertTrue(hint?.contains(gated) ?? false, hint ?? "nil")
        XCTAssertTrue(hint?.contains("The engine looks for it at: \(gated), /tmp/elsewhere") ?? false,
                      hint ?? "nil")
        // An engine that reported ready never gets the sentence, whatever the paths say.
        XCTAssertNil(outside.protectedFolderHint(engineIsSilent: false))
    }

    // MARK: - Against the real engine

    private func makeLiveController(pin: String?,
                                    environment: [String: String] = [:],
                                    slug: String = "libtest") throws -> OrgController {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-library-live-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        var settings = OrgController.OrgSettings.discover(repositoryRoot: Self.repositoryRoot())
        settings.projectPath = root.appendingPathComponent("project")
        if let pin { settings.libraryRoot = URL(fileURLWithPath: pin) }
        settings.extraEnvironment = environment
        return OrgController(settings: settings, maxRestartAttempts: 0, preferences: .ephemeral())
    }

    private func waitForRunning(_ controller: OrgController,
                                timeout: TimeInterval = 60) async -> Bool {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end && controller.engineState != .running {
            try? await Task.sleep(nanoseconds: 25_000_000)
        }
        return controller.engineState == .running
    }

    /// Poll `library` until the engine reports the given `source`.
    ///
    /// A poll rather than one read after one wait, because a relaunch is a *stop and a start*: the
    /// console's own state transition is published through an async hop, so a `waitForRunning` can
    /// return on the engine that is still going away and the next read answers from the old child.
    /// Waiting on the answer itself is waiting on the fact the assertion is about.
    private func waitForLibrary(_ controller: OrgController, source: String,
                                timeout: TimeInterval = 60) async -> Bool {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end {
            await controller.loadLibrary()
            if controller.library["source"]?.stringValue == source { return true }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        return false
    }

    /// The engine's own `library.unpinned_search_paths()`, run as the engine runs it.
    ///
    /// The guard for the whole "do not keep a copy of the engine's list" rule: the candidate list the
    /// pane renders must equal what the engine computes, so a list written in Swift would fail here.
    private func engineUnpinnedSearchPaths() throws -> [String] {
        guard let interpreter = PythonRuntimeResolver.resolveFromEnvironment().executableURL else {
            throw XCTSkip("no Python interpreter — nothing to ask")
        }
        let process = Process()
        process.executableURL = interpreter
        process.currentDirectoryURL = Self.engineRoot()
        process.arguments = ["-c",
                             "import json; from engine.library import unpinned_search_paths; "
                             + "print(json.dumps(unpinned_search_paths()))"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard let list = try? JSONSerialization.jsonObject(with: data) as? [String] else {
            throw XCTSkip("the engine did not answer with a candidate list")
        }
        return list
    }

    func testThePinnedRootReachesTheRealEngineAndClearingItRestoresDiscovery() async throws {
        // **The end-to-end proof, against the real engine.** `source` is computed from the child's own
        // `os.environ`, so this asserts the variable actually crossed — not that the app built a
        // config — and the cleared case proves discovery is still reachable.
        let expected = Set(try engineUnpinnedSearchPaths())
        let controller = try makeLiveController(pin: nil,
                                                environment: ["AGENTORG_HOME": FileManager.default
                                                    .temporaryDirectory.path])
        try XCTSkipUnless(controller.canLaunch, "no Python interpreter — nothing to read")
        try XCTSkipIf(controller.launchProblem() != nil,
                      controller.launchProblem() ?? "the engine is not launchable here")
        defer { controller.stop() }

        controller.launch()
        let reachedReady = await waitForRunning(controller)
        // **A skip, not an assertion, and the reason is worth stating.** An engine that cannot *start
        // here* is an environment fact rather than a defect in the pinning: this repo's engine requires
        // a Skills checkout to resolve before it emits `engine.ready`, and a runner without one — CI has
        // no `~/Documents/Projects/Skills` or `~/.agentorg/skills` — fails its bootstrap fatally. The
        // guards above cannot see that: `launchProblem()` probes for an interpreter and the engine's own
        // `cli.py`, both of which exist there. Skipping names which reason it was, and the pinning itself
        // is still asserted by the tests that do not need a live engine.
        try XCTSkipUnless(reachedReady,
                          "the real engine did not start here: "
                          + (controller.engineFailure ?? controller.engineError
                             ?? controller.launchProblem() ?? "no reason given"))
        let discoveredRead = await waitForLibrary(controller, source: "discovered")
        XCTAssertTrue(discoveredRead, "the engine must answer `library` with what it discovered")
        let discovered = controller.libraryPath
        XCTAssertFalse(discovered.isEmpty, "the engine reports the checkout it resolved")
        XCTAssertEqual(Set(controller.librarySearchPaths), expected,
                       "the candidates must be the engine's own, not a list kept in Swift")
        XCTAssertTrue(controller.librarySentence?.contains("at commit") ?? false,
                      controller.librarySentence ?? "nil")

        // Pin exactly the root the engine found, so the only thing that changes is where it came
        // from. `source` must flip to `pinned`, which is only true if the child read the variable.
        await controller.setLibraryRoot(discovered)
        XCTAssertEqual(controller.pinnedLibraryRoot, discovered)
        let pinnedRead = await waitForLibrary(controller, source: "pinned")
        XCTAssertTrue(pinnedRead, "the relaunch must report the root as pinned")
        XCTAssertEqual(controller.libraryPath, discovered)
        XCTAssertFalse(controller.libraryPinNotUsed)

        // And clearing it puts the engine back to discovering — the behaviour that must stay
        // reachable.
        await controller.setLibraryRoot(nil)
        let discoveredAgain = await waitForLibrary(controller, source: "discovered")
        XCTAssertTrue(discoveredAgain, "clearing the pin must return the engine to discovery")
    }

    func testARootTheEngineCannotUseIsReportedInTheEnginesOwnWords() async throws {
        // **What the engine answers about a bad root.** A pinned path with no checkout at all fails
        // the bootstrap, and `serve` writes `library.resolve`'s sentence out as a fatal `error` frame
        // precisely so the console can show the reason instead of "exited with status 1". The child is
        // given a throwaway `HOME` so every candidate it would fall back to is empty — otherwise the
        // machine's own checkout would be found and the pin would look like it worked.
        let fakeHome = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-library-fakehome-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: fakeHome, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: fakeHome) }
        let missing = "/tmp/agentorg-not-a-library-\(UUID().uuidString)"

        let controller = try makeLiveController(pin: missing,
                                                environment: ["HOME": fakeHome.path])
        try XCTSkipUnless(controller.canLaunch, "no Python interpreter — nothing to read")
        try XCTSkipIf(controller.launchProblem() != nil,
                      controller.launchProblem() ?? "the engine is not launchable here")
        defer { controller.stop() }

        controller.launch()
        let end = Date().addingTimeInterval(60)
        while Date() < end && controller.engineState != .failed {
            try? await Task.sleep(nanoseconds: 25_000_000)
        }
        XCTAssertEqual(controller.engineState, .failed,
                       "a root that resolves to nothing must not look like a working engine")
        let reason = try XCTUnwrap(controller.engineFailure)
        XCTAssertTrue(reason.contains("Skills library not found"), reason)
        XCTAssertTrue(reason.contains(missing), "the reason must name the root that was pinned")
        XCTAssertTrue(reason.contains("searched:"), "the engine lists what it tried")
    }
}
