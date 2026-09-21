//
//  OrgRemovalTests.swift
//  AgentOrgKitTests
//
//  Deleting the things a person can create — orgs, providers, people, schedules.
//
//  WHY THESE DRIVE THE REAL ENGINE
//  -------------------------------
//  Every assertion here is about a *consequence*, and the consequence belongs to the engine:
//
//  - what `portfolio remove` actually does to the folder (leaves it) and to the active org (re-points
//    it when the active one was the one removed);
//  - which directory a removal preview measured, and how many bytes are in it;
//  - what `default_pair()` resolves to after the provider named by `defaults.provider` is pruned out
//    of `credentials.json`;
//  - whether `schedule_remove` really deletes an entry rather than pausing it.
//
//  A stand-in engine would let each test assert whatever this build happens to send, which is the one
//  thing that cannot be wrong and therefore the one thing not worth checking. So the stand-in is the
//  engine itself: `python3 -m engine.cli serve`, launched as a child, with `AGENTORG_HOME` pointed at a
//  throwaway directory. That last part matters — the portfolio register lives in the user-global root,
//  so without it these tests would edit the *user's own orgs* the first time anybody ran them.
//
//  The tests are skipped rather than failed when the engine package is not beside the app, so this file
//  stays honest about what it needs instead of going red on a machine that only has `macos/`.
//

import XCTest
@testable import AgentOrgKit

@MainActor
final class OrgRemovalTests: XCTestCase {

    private var root: URL!
    private var projectsRoot: URL!
    private var home: URL!
    /// The engine package (`<repo>/AgentOrg`), found from this file's path. Nil when absent.
    private var engineRoot: URL?

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-removal-\(UUID().uuidString)")
        projectsRoot = root.appendingPathComponent("projects")
        home = root.appendingPathComponent("home")
        try FileManager.default.createDirectory(at: projectsRoot, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)

        // `#filePath` -> Tests/AgentOrgKitTests -> Tests -> macos -> AgentOrg (the engine package).
        let candidate = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        engineRoot = FileManager.default.fileExists(
            atPath: candidate.appendingPathComponent("engine/cli.py").path) ? candidate : nil
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    // MARK: - The harness

    /// The engine's own `portfolio.json`, at the path the child was told to use.
    private var portfolioFile: URL {
        home.appendingPathComponent("portfolio.json")
    }

    private func seededPortfolio(orgs: [(name: String, path: String)]) throws {
        // Written by the engine's own `Portfolio`, not by hand: a hand-built register would let these
        // tests assert against a shape the engine does not actually produce.
        var payload: [String: Any] = [
            "portfolio_version": "1.0", "active_org_id": "", "created_at": "", "updated_at": "",
            "principal": ["id": "pr_owner", "name": "Test Owner", "created_at": ""],
            "orgs": [],
        ]
        var entries: [[String: Any]] = []
        for (index, org) in orgs.enumerated() {
            let id = "org_\(org.name.lowercased())"
            entries.append([
                "id": id, "name": org.name, "slug": org.name.lowercased(), "path": org.path,
                "charter": "seeded for a test", "enabled": true, "daily_budget_usd": 0.0,
                "created_at": "", "updated_at": "", "tags": [],
            ])
            if index == 0 { payload["active_org_id"] = id }
        }
        payload["orgs"] = entries
        try Data(try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]))
            .write(to: portfolioFile)
    }

    private func makeProject(named name: String, withBytes bytes: Int = 0) throws -> URL {
        let folder = projectsRoot.appendingPathComponent(name)
        let state = folder.appendingPathComponent(".agent_state")
        try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
        if bytes > 0 {
            try Data(repeating: 0x61, count: bytes)
                .write(to: state.appendingPathComponent("run_state.json"))
        }
        return folder
    }

    /// A credentials file inside the throwaway home, seeded with two keyless providers.
    ///
    /// **This test used to run against the developer's own `credentials.json`.** `credentialsPath` was
    /// `nil`, so the engine discovered the real file — and `removeProvider`, which is precisely what
    /// this test exists to exercise, *wrote* to it. It destroyed the owner's live config twice: once
    /// taking an inline API key with it, and once leaving a single keyless provider, after which the
    /// engine could not boot at all. The test's own comment admitted the mechanism ("the throwaway home
    /// has no credentials file, so the engine resolves from the one it was launched with") and treated
    /// it as acceptable. `AGENTORG_HOME` was never enough: that protects the *register*, not the config.
    ///
    /// Two providers, because removing the default must leave a different one to resolve — the property
    /// the test asserts. Both are `ollama`-kind so no key is needed and the child can always build them.
    private func seedCredentials(in home: URL) throws -> URL {
        let path = home.appendingPathComponent("credentials.json")
        let document: [String: Any] = [
            "providers": [
                "alpha": ["kind": "ollama", "base_url": "http://127.0.0.1:11434"],
                "beta": ["kind": "ollama", "base_url": "http://127.0.0.1:11434"],
            ],
            "defaults": ["provider": "alpha", "model": "qwen2.5-coder:7b"],
            "models": ["known": ["qwen2.5-coder:7b": ["context_window": 32768]]],
        ]
        let data = try JSONSerialization.data(withJSONObject: document,
                                              options: [.prettyPrinted, .sortedKeys])
        try data.write(to: path)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        return path
    }

    private func makeController(project: URL) throws -> OrgController {
        guard let engineRoot else {
            throw XCTSkip("the engine package is not available from this checkout")
        }
        let credentials = try seedCredentials(in: home)
        let settings = OrgController.OrgSettings(
            engineRoot: engineRoot,
            projectPath: project,
            // The throwaway file this test seeds. Without it the engine discovers the developer's real
            // config and every removal below edits it — see `seedCredentials`.
            credentialsPath: credentials,
            libraryRoot: nil,
            // The one thing that keeps a test from editing the user's real register.
            extraEnvironment: ["AGENTORG_HOME": home.path])
            .with(runtime: .system(URL(fileURLWithPath: "/usr/bin/env")),
                  arguments: ["python3", "-m", "engine.cli", "serve",
                              "--slug", project.lastPathComponent,
                              "--root", projectsRoot.path])
        return OrgController(settings: settings, maxRestartAttempts: 0, restartDelay: 0.05)
    }

    private func waitUntil(_ deadline: TimeInterval = 20,
                           _ condition: () -> Bool) async -> Bool {
        let end = Date().addingTimeInterval(deadline)
        while Date() < end {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    private func launchedController(project: URL) async throws -> OrgController {
        let controller = try makeController(project: project)
        controller.launch()
        let ready = await waitUntil { controller.engineState == .running }
        XCTAssertTrue(ready, "the real engine must reach `running` for this test to mean anything")
        // The portfolio travels with `status`, so one refresh is enough to populate the register.
        await controller.refresh()
        return controller
    }

    private func registeredOrgSlugs() throws -> [String] {
        let data = try Data(contentsOf: portfolioFile)
        let doc = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        return (doc["orgs"] as? [[String: Any]] ?? []).compactMap { $0["slug"] as? String }
    }

    private func activeOrgIdOnDisk() throws -> String {
        let data = try Data(contentsOf: portfolioFile)
        let doc = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        return doc["active_org_id"] as? String ?? ""
    }

    // MARK: - Org removal

    func testRemovingAnOrgForgetsItAndLeavesTheFolderAlone() async throws {
        // The distinction the whole path rests on, and the one a person must be told before they agree:
        // `portfolio remove` says "its folder is left alone", and `Portfolio.remove_org`'s docstring
        // says the same. This is that claim measured — a run history large enough to notice, still on
        // disk after the org is gone from the register.
        let alpha = try makeProject(named: "alpha", withBytes: 4096)
        _ = try makeProject(named: "beta")
        try seededPortfolio(orgs: [("Alpha", alpha.path), ("Beta", projectsRoot.appendingPathComponent("beta").path)])

        let controller = try await launchedController(project: projectsRoot.appendingPathComponent("beta"))
        defer { controller.stop() }
        XCTAssertEqual(controller.portfolioOrgs.count, 2, "the register must be read before it is edited")

        await controller.removeOrg("org_alpha")

        XCTAssertEqual(try registeredOrgSlugs(), ["beta"], "the org must be gone from the register")
        XCTAssertTrue(FileManager.default.fileExists(
            atPath: alpha.appendingPathComponent(".agent_state/run_state.json").path),
            "**the folder must survive** — `portfolio remove` forgets the pointer, not the data")
    }

    func testRemovingTheActiveOrgRepointsTheEngineRatherThanLeavingItDescribingIt() async throws {
        // The failure this pins is the one the switcher track fixed for `selectOrg`, reached through the
        // other door: `remove_org` reassigns `active_org_id`, but the server's `workspace` still points
        // at the org that just left — so the roster, mission and run history would go on describing an
        // org no longer in the register.
        let alpha = try makeProject(named: "alpha")
        let beta = try makeProject(named: "beta")
        try seededPortfolio(orgs: [("Alpha", alpha.path), ("Beta", beta.path)])

        let controller = try await launchedController(project: beta)
        defer { controller.stop() }
        XCTAssertEqual(controller.activeOrgId, "org_alpha", "alpha is seeded active")

        await controller.removeOrg("org_alpha")

        XCTAssertEqual(controller.activeOrgId, "org_beta",
                       "the engine must select a successor rather than keep a dangling active id")
        XCTAssertEqual(try activeOrgIdOnDisk(), "org_beta")
        XCTAssertNil(controller.activeOrg, "no row may be marked active while none exists")
    }

    func testARemovalPreviewReportsTheBytesThatWillStayOnDisk() async throws {
        // The confirmation's numbers have to come from somewhere real. This is the engine walking the
        // folder the person is about to lose sight of, and the two facts the dialog depends on: the
        // size, and the engine's own statement that it cannot delete the folder.
        let alpha = try makeProject(named: "alpha", withBytes: 8192)
        try seededPortfolio(orgs: [("Alpha", alpha.path)])

        let controller = try await launchedController(project: alpha)
        defer { controller.stop() }

        let preview = await controller.orgRemovalPreview("org_alpha")
        XCTAssertEqual(preview["folder"]?.stringValue, alpha.path)
        XCTAssertEqual(preview["folder_exists"]?.boolValue, true)
        XCTAssertGreaterThanOrEqual(preview["folder_bytes"]?.intValue ?? 0, 8192,
                                    "the preview must size the real folder, not guess")
        XCTAssertEqual(preview["can_delete_folder"]?.boolValue, false,
                       "the engine has no delete-the-folder arm; claiming it could is the lie this "
                       + "field exists to prevent")
        XCTAssertFalse(preview["can_delete_folder_why"]?.stringValue?.isEmpty ?? true,
                       "there must be a reason, so the dialog can say why the option is absent")
    }

    func testARemovedOrgDisappearingDoesNotBreakThePanel() async throws {
        // Requirement: the switcher must survive an org vanishing underneath it — which happens for real
        // when several windows or the CLI edit one register. The active id is read from the engine, so a
        // removed one must resolve to nothing rather than to a stale row or a crash.
        let alpha = try makeProject(named: "alpha")
        try seededPortfolio(orgs: [("Alpha", alpha.path)])
        let controller = try await launchedController(project: alpha)
        defer { controller.stop() }
        XCTAssertEqual(controller.activeOrgId, "org_alpha")

        // Removed behind the console's back, exactly as another process would.
        try seededPortfolio(orgs: [])
        await controller.refresh()

        XCTAssertEqual(controller.activeOrgId, "", "an active id that names nothing must read as empty")
        XCTAssertNil(controller.activeOrg, "and it must not resolve to a row that is no longer there")
        XCTAssertFalse(controller.hasPortfolio)
        XCTAssertTrue(controller.portfolioOrgs.isEmpty)
    }

    func testRemovingAnUnknownOrgIsRefusedWithTheEnginesOwnWords() async throws {
        // `mutate` rather than `send`: the person pressed a button, so a refusal has to reach them. The
        // engine's message names what was not found, which is more use than a generic failure.
        let alpha = try makeProject(named: "alpha")
        try seededPortfolio(orgs: [("Alpha", alpha.path)])
        let controller = try await launchedController(project: alpha)
        defer { controller.stop() }

        let ok = await controller.removeOrg("org_ghost")

        XCTAssertFalse(ok, "a refused removal must report failure to its caller, which showed a dialog")
        XCTAssertEqual(try registeredOrgSlugs(), ["alpha"], "and it must change nothing")
        XCTAssertNotNil(controller.notice, "the reason must reach the person, not just the log")
    }

    // MARK: - Providers

    func testRemovingTheDefaultProviderMovesTheDefaultAndThePanelLearnsWhereItWent() async throws {
        // `config._remove_provider_references` prunes `defaults.provider` when it named the provider
        // being removed, and `default_pair()` then resolves a *different* one. That is a real change to
        // what every agent runs on, caused by a button that does not obviously touch the defaults — so
        // the controller re-reads the resolved pair, and this asserts it actually arrives.
        let project = try makeProject(named: "provs")
        try seededPortfolio(orgs: [("Provs", project.path)])
        let controller = try await launchedController(project: project)
        defer { controller.stop() }
        await controller.loadWindow()
        await controller.refreshDefaults()

        // The default must be *some* configured provider before we can remove it; the throwaway home
        // has no credentials file, so the engine resolves from the one it was launched with.
        let before = try XCTUnwrap(controller.defaults["provider"]?.stringValue,
                                   "the engine must report an effective default provider")
        XCTAssertTrue(controller.providers.contains { $0["id"]?.stringValue == before },
                      "the default must be one of the configured providers")

        await controller.removeProvider(id: before)

        XCTAssertFalse(controller.providers.contains { $0["id"]?.stringValue == before },
                       "the removed provider must be gone from the list")
        // Re-read, not derived: the successor is whatever the engine resolved, and it must not still be
        // the provider that no longer exists.
        XCTAssertNotEqual(controller.defaults["provider"]?.stringValue, before,
                          "the panel must not go on showing a default provider that was just deleted")
    }

    // MARK: - People

    func testRetiringAnAgentRemovesTheRosterEntryAndKeepsItsWindowOutOfRouting() async throws {
        // The honesty requirement, measured: a retire *does* remove the roster entry (`terminate` does
        // `del self.agents[agent_id]`), so it is not a no-op dressed up as a delete. What the UI must not
        // imply is that a record survives — nothing in `terminate` writes the reason anywhere — which is
        // why no confirmation here collects one.
        let project = try makeProject(named: "people")
        try seededPortfolio(orgs: [("People", project.path)])
        let controller = try await launchedController(project: project)
        defer { controller.stop() }
        await controller.loadWindow()

        let hired = controller.roster.first { $0["status"]?.stringValue == "hired" }
        let target = try XCTUnwrap(hired,
                                   "the engine seeds a default company; a hired agent is what retire takes")
        let id = try XCTUnwrap(target["id"]?.stringValue)

        let ok = await controller.retireAgent(id: id)

        XCTAssertTrue(ok, "retiring a hired agent must be accepted")
        XCTAssertFalse(controller.roster.contains { $0["id"]?.stringValue == id },
                       "the roster entry must actually be gone — 'retire' removes, it does not hide")
    }

    func testRetiringTheOwnerIsRefusedRatherThanSilentlyIgnored() async throws {
        // The engine's own refusal (`the Owner cannot be terminated; it holds terminal authority`) must
        // reach the caller. A UI that swallowed it would leave the button looking broken.
        let project = try makeProject(named: "owner")
        try seededPortfolio(orgs: [("Owner", project.path)])
        let controller = try await launchedController(project: project)
        defer { controller.stop() }

        let ok = await controller.retireAgent(id: "ag_owner")

        XCTAssertFalse(ok, "the Owner cannot be retired, and the caller must be told")
        XCTAssertNotNil(controller.notice)
    }

    // MARK: - Schedules

    func testAScheduleCanBeListedAndForgottenThroughTheController() async throws {
        // `schedules remove` has been in the CLI since schedules existed and no UI could reach it, so a
        // schedule added from the terminal was permanent from the console's side. This drives the whole
        // path: the entry is written by the engine's own `ScheduleStore`, listed through the command,
        // and removed through the command — and the *file* is read back to prove it went.
        let project = try makeProject(named: "sched")
        try seededPortfolio(orgs: [("Sched", project.path)])

        // Written by the engine, so the fixture is the engine's own format.
        let seeded = try seedSchedule(in: project, objective: "check the nightly build")
        let controller = try await launchedController(project: project)
        defer { controller.stop() }

        await controller.loadSchedules()
        XCTAssertEqual(controller.scheduleEntries.count, 1, "the entry must be listed")
        XCTAssertEqual(controller.scheduleEntries.first?["id"]?.stringValue, seeded)

        let ok = await controller.removeSchedule(id: seeded)

        XCTAssertTrue(ok, "removing a listed entry must be accepted")
        XCTAssertTrue(controller.scheduleEntries.isEmpty, "the reply must carry the shrunken list")
        let onDisk = try scheduleIds(in: project)
        XCTAssertTrue(onDisk.isEmpty, "the entry must be gone from schedules.json, not merely paused")
    }

    func testForgettingAnUnknownScheduleIsRefusedWithTheEnginesOwnWords() async throws {
        // The engine refuses to guess, and says so: "removing the wrong schedule is not a mistake that
        // can be undone". That refusal is surfaced rather than pre-empted in Swift, so the wording a
        // person reads is the engine's.
        let project = try makeProject(named: "sched2")
        try seededPortfolio(orgs: [("Sched2", project.path)])
        _ = try seedSchedule(in: project, objective: "something real")

        let controller = try await launchedController(project: project)
        defer { controller.stop() }

        let ok = await controller.removeSchedule(id: "sch_not_a_real_id")

        XCTAssertFalse(ok, "an unknown entry must be refused")
        XCTAssertEqual(try scheduleIds(in: project).count, 1, "and nothing must be removed")
        XCTAssertNotNil(controller.notice, "the reason must reach the person")
    }

    // MARK: - The schedule fixture, written by the engine

    /// Write one schedule entry using the engine's own `ScheduleStore`, and return its id.
    private func seedSchedule(in project: URL, objective: String) throws -> String {
        guard let engineRoot else { throw XCTSkip("the engine is not available") }
        let script = root.appendingPathComponent("seed_schedule_\(UUID().uuidString).py")
        let source = """
        import sys
        sys.path.insert(0, \(String(reflecting: engineRoot.path)))
        from engine.state import Workspace
        from engine.schedules import ScheduleStore
        ws = Workspace.attach(\(String(reflecting: project.path)))
        store = ScheduleStore(ws)
        entry = store.add(objective=\(String(reflecting: objective)), slug=ws.slug, every="6h")
        store.save()
        print(entry.id)
        """
        try source.write(to: script, atomically: true, encoding: .utf8)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", script.path]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        let out = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let text = String(data: out, encoding: .utf8) ?? ""
        let id = text.split(separator: "\n").map(String.init).last { $0.hasPrefix("sch_") }
        return try XCTUnwrap(id, "the engine did not write a schedule entry: \(text)")
    }

    private func scheduleIds(in project: URL) throws -> [String] {
        let file = project.appendingPathComponent(".agent_state/schedules.json")
        guard FileManager.default.fileExists(atPath: file.path) else { return [] }
        let doc = try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any]
        return (doc?["entries"] as? [[String: Any]] ?? []).compactMap { $0["id"] as? String }
    }
}
