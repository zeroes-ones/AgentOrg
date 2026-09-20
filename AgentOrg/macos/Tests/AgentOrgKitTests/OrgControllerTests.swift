//
//  OrgControllerTests.swift
//  AgentOrgKitTests
//
//  The view model's derived state — the parts the console renders and a person reads.
//
//  These are tested in the kit rather than through a UI because the interesting behaviour is in the
//  derivations: how health is summarised, how the roster groups, and how cost is *described* so an
//  unmeasured figure never reads as free.
//

import XCTest
@testable import AgentOrgKit

@MainActor
final class OrgControllerTests: XCTestCase {

    private func makeController(root: URL) -> OrgController {
        let settings = OrgController.OrgSettings(
            engineRoot: root, projectPath: root, credentialsPath: nil, libraryRoot: nil)
        // Ephemeral preferences: a test must never write the wizard's answers into the developer's own
        // `UserDefaults`, and the assertions must not depend on what they last were.
        return OrgController(settings: settings, preferences: .ephemeral())
    }

    private func agent(name: String, team: String, state: String) -> [String: JSONValue] {
        [
            "name": .string(name), "title": .string("Developer"), "team": .string(team),
            "state": .string(state), "provider": .string("ollama"),
            "model": .string("qwen2.5-coder:7b"), "skills": .array([.string("backend-developer")]),
            "role": .string("worker"), "kind": .string("ai"), "level": .int(3),
        ]
    }

    func testStartsIdleAndCannotLaunchWithoutARuntime() {
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        XCTAssertEqual(controller.engineState, .idle)
        XCTAssertEqual(controller.engineDiagnostics.count, 1)
    }

    func testLaunchFailsFastWhenTheEngineDirectoryIsMissing() {
        // The bug this prevents: `Process.run()` fails with "The file 'AgentOrg' doesn't exist",
        // which is true but never says *which* path was wrong. A bad engine root must be caught
        // before spawning, with the resolved path named.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        guard controller.canLaunch else { return }   // no interpreter here: nothing to assert
        let problem = controller.launchProblem()
        XCTAssertNotNil(problem, "an engine root of a temp dir must be reported as a problem")
        // The message must name the path it rejected, which is the whole point of the check.
        XCTAssertTrue(problem?.contains(FileManager.default.temporaryDirectory.path) ?? false,
                      "the problem must name the offending path: \(problem ?? "")")
        XCTAssertTrue(problem?.contains("engine/cli.py") ?? false,
                      "the problem must name what it looked for: \(problem ?? "")")
    }

    func testLaunchProblemAcceptsARealEnginePackage() {
        // The engine root is the *package* directory, and `engine/cli.py` lives one level below it —
        // so the check must look for `engine/cli.py`, not `cli.py`, or a correct root is rejected.
        let repository = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // AgentOrgKitTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // macos
            .deletingLastPathComponent()   // AgentOrg
            .deletingLastPathComponent()   // repository root
        let settings = OrgController.OrgSettings.discover(repositoryRoot: repository)
        let controller = OrgController(settings: settings)
        if controller.canLaunch {
            XCTAssertNil(controller.launchProblem(),
                         controller.launchProblem() ?? "")
        }
    }

    func testTheRuntimeProblemIsExposedForTheFirstRunPanel() {        // A first-run panel that cannot explain a missing interpreter is not a first-run panel, so the
        // reason is carried rather than a bare "unavailable".
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        if !controller.canLaunch {
            XCTAssertNotNil(controller.runtimeProblem)
            XCTAssertTrue(controller.runtimeProblem?.contains("python") ?? false)
        }
    }

    func testTheRosterIsEmptyWithoutAnEngine() {
        // The panel renders this as "the engine is not running" rather than as an empty table that
        // looks like real data.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        XCTAssertTrue(controller.agents.isEmpty)
        XCTAssertTrue(controller.rosterByTeam.isEmpty)
        XCTAssertNil(controller.pendingGate)
        XCTAssertNil(controller.proposedGraph)
    }

    func testHealthSummaryCountsEachState() {
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        let health = controller.healthSummary
        for key in ["healthy", "degraded", "quarantined", "other"] {
            XCTAssertNotNil(health[key], "\(key) must be reported even at zero")
        }
    }

    func testCostDescriptionNeverRendersAnUnmeasuredTotalAsZero() {
        // The distinction the whole cost layer exists for.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        // With no status yet, the description says so rather than claiming $0.0000.
        XCTAssertEqual(controller.costDescription, "no data yet")
    }

    func testCacheDescriptionSaysUnreportedRatherThanZeroHitRate() {
        // A provider that reports no cache must not be drawn as a confident 0% hit rate, for the
        // same reason an unmeasured cost is never drawn as free.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        XCTAssertEqual(controller.cacheDescription, "prompt cache: not reported by this provider")
    }

    func testSwarmDescriptionSaysNoSwarmRatherThanZeroItems() {
        // Same distinction again: an absent swarm is not a swarm of zero.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        XCTAssertEqual(controller.swarmDescription, "no swarm has run")
        XCTAssertFalse(controller.swarmRunning)
        XCTAssertTrue(controller.swarmItems.isEmpty)
    }

    func testTabsAskOneQuestionEach() {
        // `observability-engineer`: a dashboard with no single question is sprawl. The rewrite reduced
        // twelve flat panels to four destinations *because* five of the old rows were renderings of
        // one live run and two were the same roster twice — the count is asserted so re-growing it
        // without a question is caught here rather than becoming a sidebar nobody can describe.
        //
        // **System is the fifth, and it came with a question rather than a feature.** Every other row
        // answers something about the *work*; this one answers something about the *machine* — what an
        // agent may do on it — and that is not a rendering of the roster, the run, or the model. The
        // discipline the count encodes is the assertion below it: a row must be expressible as a
        // question, and no two rows may ask the same one. A sixth row would have to justify itself the
        // same way, which is the point of pinning the number rather than the list.
        XCTAssertEqual(Destination.allCases.count, 5, "five destinations, one question each")
        for destination in Destination.allCases {
            XCTAssertFalse(destination.question.isEmpty, "\(destination) has no question")
            XCTAssertTrue(destination.question.hasSuffix("?"),
                          "\(destination)'s question should be a question")
            XCTAssertFalse(destination.symbol.isEmpty, "\(destination) has no SF Symbol")
        }
        let questions = Destination.allCases.map(\.question)
        XCTAssertEqual(Set(questions).count, questions.count,
                       "two destinations must not ask the same question: \(questions)")
        XCTAssertEqual(Destination.allCases, [.now, .runs, .org, .system, .setup])
        // Every capability the twelve panels had is re-homed into a section of one of these, so the
        // sections are what must not silently lose a case.
        XCTAssertTrue(NowSection.allCases.contains(.happening))
        XCTAssertTrue(NowSection.allCases.contains(.board))
        XCTAssertTrue(NowSection.allCases.contains(.usage))
        XCTAssertTrue(RunsSection.allCases.contains(.onDisk))
        XCTAssertTrue(RunsSection.allCases.contains(.sessions))
        XCTAssertTrue(RunsSection.allCases.contains(.proposals))
    }

    func testTheAppOpensOnNowAndNotOnASecondaryDestination() {
        // The bug this pins: the enum's comment claimed Portfolio was first "because it is the whole
        // picture" while the view's stored default was `Org`, so the app opened on a read-only roster
        // while the thing that needed a decision sat behind a different row.
        XCTAssertEqual(Destination.initial, .now)
        XCTAssertTrue(Destination.now.question.contains("what do I do next"))
    }

    func testAStoredPanelFromTheOldBuildIsRehomedRatherThanDiscarded() {
        // A person who lived in the Cost panel must reopen somewhere sensible rather than being
        // dropped on an unfamiliar destination as if they had never used the app. Every one of the
        // twelve old names maps onto the destination that absorbed it.
        let expected: [String: Destination] = [
            "Portfolio": .now, "Activity": .now, "Flow": .now, "Work": .now,
            "Cost": .now, "Context": .now, "Resources": .now,
            "History": .runs, "Improve": .runs,
            "Org": .org, "People": .org,
            "Providers": .setup,
        ]
        for (stored, destination) in expected {
            XCTAssertEqual(Destination.migrated(fromStored: stored), destination,
                           "\(stored) should reopen on \(destination)")
        }
        // A current value passes through, and anything unrecognised — an older build, a hand-edited
        // default — resolves to the initial destination rather than leaving the window blank.
        XCTAssertEqual(Destination.migrated(fromStored: "Runs"), .runs)
        XCTAssertEqual(Destination.migrated(fromStored: "System"), .system)
        // Nothing in the old build was a capability list — the toggles lived inside the hire form —
        // so no legacy name maps here, and this passes through only because it now *is* a current
        // name. Asserted so a later migration that reroutes "System" is caught rather than silently
        // sending everyone who used the panel back to Now.
        XCTAssertEqual(Destination.migrated(fromStored: "SomethingElse"), .now)
        XCTAssertEqual(Destination.migrated(fromStored: nil), .now)
        XCTAssertEqual(Destination.migrated(fromStored: "  "), .now)
    }

    func testThePortfolioStartsEmpty() {
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        XCTAssertFalse(controller.hasPortfolio)
        XCTAssertTrue(controller.portfolioOrgs.isEmpty)
        XCTAssertTrue(controller.portfolioRows.isEmpty)
        XCTAssertEqual(controller.activeOrgId, "")
    }

    /// The Activity panel's derived data, which is what the app actually reads.
    func testStaffingGapsAreReadFromTheActivityReport() {
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        // With no engine the report is empty, so the panel shows nothing rather than stale rows.
        XCTAssertTrue(controller.staffingGaps.isEmpty)
    }

    func testTheMissionStartsEmpty() {
        // The mission travels with status; before any poll it is empty rather than fabricated.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        XCTAssertTrue(controller.mission.isEmpty)
    }

    func testTheFlowBoardStartsEmpty() {
        // The board travels with status; before any poll it is empty rather than fabricated, so the
        // panel shows a calm "no work yet" rather than stale rows.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        XCTAssertTrue(controller.flowRows.isEmpty)
        XCTAssertTrue(controller.flowHandoffs.isEmpty)
        XCTAssertEqual(controller.defaultPairLabel, "not set")
    }

    func testCommandsWithNoEngineAreRefusedRatherThanSilentlyDropped() async {
        // A command that vanished would leave the UI showing a state the engine never entered.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        await controller.send("start")
        XCTAssertNotNil(controller.notice)
        XCTAssertTrue(controller.notice?.contains("not running") ?? false)
    }

    func testStoppingWithNoEngineIsHarmless() {
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        controller.stop()
        XCTAssertEqual(controller.engineState, .idle)
    }

    func testSettingsDiscoverPrefersAnExistingCredentialsFile() {
        // The app must open on a real project rather than an empty form.
        let root = FileManager.default.temporaryDirectory
        let settings = OrgController.OrgSettings.discover(repositoryRoot: root)
        // Checked as a *suffix* rather than a substring: `contains("AgentOrg")` also passes for the
        // wrong path this once produced (`…/Projects/AgentOrg`), which is why the bug went unnoticed.
        XCTAssertTrue(settings.engineRoot.path.hasSuffix("/AgentOrg"), settings.engineRoot.path)
        XCTAssertTrue(settings.projectPath.path.contains("projects"))
    }

    func testDiscoverResolvesTheEngineRootRelativeToTheRepositoryRoot() {
        // The regression: the app walked up from `Bundle.main.bundleURL` five times, which is one
        // level too many because bundleURL is already a directory — so it looked for the engine in
        // `…/Projects/AgentOrg`, which does not exist, and the console sat idle with no engine.
        let repository = URL(fileURLWithPath: "/Users/someone/code/Agent")
        let settings = OrgController.OrgSettings.discover(repositoryRoot: repository)
        XCTAssertEqual(settings.engineRoot.path, "/Users/someone/code/Agent/AgentOrg",
                       "the engine root is <repo>/AgentOrg, not a sibling of the repo")
        // `console` and not `demo`. `engine.cli serve` defaults to `--slug console`, so a console
        // that assumed `projects/demo/` read a directory the engine never wrote to: the run history
        // was permanently empty in the managed-project case and nothing on screen explained it.
        XCTAssertEqual(settings.projectPath.path, "/Users/someone/code/Agent/AgentOrg/projects/console",
                       "the default project must be the one the engine defaults to")
    }

    // MARK: - The managed project the engine is told to use

    func testTheManagedSlugIsDerivedFromTheProjectFolder() {
        // The two sides have to name the same directory, so the slug is *derived* rather than stored:
        // a stored copy is exactly what drifted (`demo` in the app, `console` in the engine).
        let settings = OrgController.OrgSettings(
            engineRoot: URL(fileURLWithPath: "/repo/AgentOrg"),
            projectPath: URL(fileURLWithPath: "/repo/AgentOrg/projects/my-app"))
        XCTAssertEqual(settings.managedSlug, "my-app")
        XCTAssertEqual(settings.managedSlug, settings.projectPath.lastPathComponent,
                       "the slug and the directory must be the same string's two uses")
    }

    func testAManagedSlugIsNormalisedTheWayTheEngineNormalisesIt() {
        // The engine's `Workspace` refuses a slug that is not `[a-z0-9][a-z0-9._-]*`, and it also
        // *normalises* a folder name it is given. Matching that here means a folder called `My_App`
        // yields the slug the engine would derive from the same folder, not a second, wrong one.
        for (folder, slug) in [("My_App", "my_app"), ("Foo.Bar", "foo.bar"),
                               ("my-app", "my-app"), ("UPPER", "upper"),
                               ("_draft", "draft"), ("weird name!", "weird_name")] {
            let settings = OrgController.OrgSettings(
                engineRoot: URL(fileURLWithPath: "/repo/AgentOrg"),
                projectPath: URL(fileURLWithPath: "/repo/AgentOrg/projects/\(folder)"))
            XCTAssertEqual(settings.managedSlug, slug, "folder \(folder)!")
        }
        // No project path at all falls back to the engine's own default slug, so a controller built
        // without one still launches the engine at the project the console reads.
        let bare = OrgController.OrgSettings(
            engineRoot: URL(fileURLWithPath: "/repo/AgentOrg"),
            projectPath: URL(fileURLWithPath: "/"))
        XCTAssertEqual(bare.managedSlug, "console")
    }

    func testSlugNormalisationRoundTripsThroughTheEnginesOwnGrammar() {
        // A slug the app produces must be one the engine's own `_SLUG_RE` accepts, or the child exits
        // with "invalid project name" before it ever serves a command.
        let pattern = try! NSRegularExpression(pattern: "^[a-z0-9][a-z0-9._-]*$")
        for name in ["My App", "  spaced  ", "9lives", "a/b", "café", "!!!", "x"] {
            let slug = WorkspaceNaming.slug(from: name)
            let range = NSRange(slug.startIndex..<slug.endIndex, in: slug)
            XCTAssertNotNil(pattern.firstMatch(in: slug, range: range),
                            "\(name) produced the invalid slug \(slug)")
        }
        XCTAssertTrue(WorkspaceNaming.isValidSlug("my-app"))
        XCTAssertFalse(WorkspaceNaming.isValidSlug("-leading"))
        XCTAssertFalse(WorkspaceNaming.isValidSlug("has space"))
    }

    func testADisplayNameIsRecoveredFromASlug() {
        // The spine names the project; a raw slug reads like a filename, so it is expanded for the one
        // place a person reads it.
        XCTAssertEqual(WorkspaceNaming.displayName(fromSlug: "my-app"), "My App")
        XCTAssertEqual(WorkspaceNaming.displayName(fromSlug: "acme_corp"), "Acme Corp")
    }

    func testCredentialsAreOnlyReportedWhenTheyActuallyExist() {
        // A path that exists but is not a credentials file must not be offered as one.
        let root = FileManager.default.temporaryDirectory
        let settings = OrgController.OrgSettings.discover(repositoryRoot: root)
        if let path = settings.credentialsPath {
            XCTAssertTrue(FileManager.default.fileExists(atPath: path.path))
        } else {
            XCTAssertFalse(FileManager.default.fileExists(
                atPath: settings.engineRoot.appendingPathComponent("credentials.json").path))
        }
    }

    // MARK: - Drafts

    func testAProviderDraftOmitsFieldsTheUserDidNotFill() {
        // A save must never blank a value it did not set — the whole reason the draft is separate from
        // the payload. An empty key field means "unchanged", not "clear the key".
        var draft = ProviderDraft()
        draft.id = "groq"
        draft.baseURL = "https://api.groq.com/openai/v1"
        let payload = draft.payload()
        XCTAssertEqual(payload["provider_id"], .string("groq"))
        XCTAssertNil(payload["api_key"])
        XCTAssertNil(payload["api_key_env"])
        XCTAssertNil(payload["extra_headers"])
    }

    func testAProviderDraftSendsBothWhenBothWereFilled() {
        // **This test previously asserted the opposite, and the assertion was the bug.** It pinned
        // "a literal must not be sent when a variable is named", which is the *engine's* preference
        // rule (and still is: `resolve_key` reads the variable first) applied at the wrong layer —
        // the payload builder. Applying it there meant a person who pasted their key and also named
        // the variable silently lost the key before it reached the engine, and the engine then
        // reported "has no API key" to someone who had just supplied one. The engine's writer was
        // fixed to keep both; this builder was dropping it first, so that fix could never take effect
        // from the app.
        //
        // Precedence is unchanged where it belongs. The engine still writes both and still resolves
        // the environment first, so the safer source wins — that is what
        // `test_phase11_serve.py::test_provider_add_keeps_a_pasted_key_even_when_a_variable_is_also_named`
        // protects on the Python side, and this is its Swift counterpart: the key has to *arrive*
        // before anything can prefer the variable over it.
        let draft = ProviderDraft(id: "gw", baseURL: "https://gw/v1",
                                  apiKey: "literal-secret", apiKeyEnv: "GW_KEY")
        let payload = draft.payload()
        XCTAssertEqual(payload["api_key_env"], .string("GW_KEY"))
        XCTAssertEqual(payload["api_key"], .string("literal-secret"),
                       "the key the user typed must cross; the engine decides which one wins")
    }

    func testAProviderDraftSendsALiteralWhenThereIsNoVariable() {
        let draft = ProviderDraft(id: "gw", baseURL: "https://gw/v1", apiKey: "literal-secret")
        XCTAssertEqual(draft.payload()["api_key"], .string("literal-secret"))
    }

    func testAProviderDraftCarriesHeaders() {
        let draft = ProviderDraft(id: "gw", baseURL: "https://gw/v1",
                                  headers: ["X-Tenant": "acme"])
        let headers = draft.payload()["extra_headers"]?.objectValue
        XCTAssertEqual(headers?["X-Tenant"], .string("acme"))
    }

    func testAProviderDraftIsIncompleteWithoutAnIdAndURL() {
        XCTAssertFalse(ProviderDraft().isComplete)
        XCTAssertFalse(ProviderDraft(id: "a").isComplete)
        XCTAssertTrue(ProviderDraft(id: "a", baseURL: "https://x/v1").isComplete)
    }

    func testEditingAProviderStartsWithNoKeyInMemory() {
        // The engine never returns a key, so the field must start empty rather than pretending it knows
        // one — and an untouched save then leaves the stored key alone.
        let existing: [String: JSONValue] = [
            "id": .string("groq"), "kind": .string("openai"),
            "base_url": .string("https://api.groq.com/openai/v1"),
            "api_key_env": .string("GROQ_API_KEY"), "has_key": .bool(true),
        ]
        let draft = ProviderDraft(existing: existing)
        XCTAssertEqual(draft.id, "groq")
        XCTAssertEqual(draft.apiKeyEnv, "GROQ_API_KEY")
        XCTAssertTrue(draft.apiKey.isEmpty)
    }

    func testAnAgentDraftRequiresANameAndSkill() {
        XCTAssertFalse(AgentDraft().isComplete)
        XCTAssertFalse(AgentDraft(name: "Nadia").isComplete)
        XCTAssertTrue(AgentDraft(name: "Nadia", skill: "code-reviewer").isComplete)
    }

    func testAnAgentHirePayloadOmitsEmptyOptionals() {
        let payload = AgentDraft(name: "Nadia", skill: "code-reviewer").hirePayload()
        XCTAssertEqual(payload["name"], .string("Nadia"))
        XCTAssertEqual(payload["skill"], .string("code-reviewer"))
        XCTAssertNil(payload["provider"])
        XCTAssertNil(payload["team"])
        XCTAssertNil(payload["title"])
    }

    func testAnAgentUpdateDoesNotResendAnUnchangedName() {
        // Re-sending the same name would make a model-only edit fail whenever another agent already
        // holds it — a refusal with nothing to do with the change.
        let original = AgentDraft(name: "Nadia", skill: "code-reviewer", provider: "ollama",
                                  model: "qwen2.5-coder:7b")
        var changed = original
        changed.model = "qwen2.5-coder:14b"
        let payload = changed.updatePayload(original: original)
        XCTAssertNil(payload["name"])
        XCTAssertEqual(payload["model"], .string("qwen2.5-coder:14b"))
    }

    func testAnAgentUpdateSendsAChangedName() {
        let original = AgentDraft(name: "Nadia", skill: "code-reviewer")
        var renamed = original
        renamed.name = "Nadia K"
        XCTAssertEqual(renamed.updatePayload(original: original)["name"], .string("Nadia K"))
    }

    func testAnAgentDraftRoundTripsFromARosterEntry() {
        let entry: [String: JSONValue] = [
            "name": .string("Nadia"), "skills": .array([.string("code-reviewer")]),
            "provider": .string("ollama"), "model": .string("qwen2.5-coder:7b"),
            "level": .string("senior"), "team": .string("Quality"), "title": .string("Reviewer"),
        ]
        let draft = AgentDraft(existing: entry)
        XCTAssertEqual(draft.name, "Nadia")
        XCTAssertEqual(draft.skill, "code-reviewer")
        XCTAssertEqual(draft.team, "Quality")
        XCTAssertEqual(draft.updatePayload(original: draft)["name"], nil)
    }

    // MARK: - Sidebar metadata

    func testShortQuestionsAreDistinct() {
        // Two identical sidebar subtitles make the list ambiguous about which is which. The short
        // form is generated from the destination, so this asserts the generator over every case.
        let short = Destination.allCases.map(Self.shortQuestion)
        XCTAssertEqual(Set(short).count, short.count, "duplicate short question: \(short)")
        for text in short {
            // A sidebar row has about twenty characters of comfort before it truncates, and an
            // ellipsis in the middle of a phrase is worse than a shorter phrase.
            XCTAssertLessThanOrEqual(text.count, 24, "\(text) will truncate in the sidebar")
            XCTAssertFalse(text.isEmpty)
        }
    }

    func testSymbolsAreDistinct() {
        let symbols = Destination.allCases.map(\.symbol)
        XCTAssertEqual(Set(symbols).count, symbols.count, "duplicate symbol: \(symbols)")
    }

    /// The sidebar's short form, mirrored from the view so the distinctness and length assertions
    /// above are about the strings a person actually reads rather than about placeholder text.
    private static func shortQuestion(_ destination: Destination) -> String {
        switch destination {
        case .now: return "live · next step"
        case .runs: return "past runs · disk"
        case .org: return "people · hiring"
        // The engine off, which is the state a fresh install is in. The live form is
        // "N/12 held" and is checked against the controller in `SystemPanelTests` rather than mirrored
        // here: a hand-copied string is a second definition that goes stale, which is the failure the
        // mirrored forms above are already a compromise on.
        case .system: return "off · what it reaches"
        case .setup: return "model · project"
        }
    }

    func testLaunchingTwiceDoesNotSpawnASecondEngine() {
        // The race this guards: the window's first-run `.task` auto-launches, and the menu command can
        // fire in the same window. Guarding only on `.running` let a second call start a *second* engine
        // on the same project — two processes writing one checkpoint.
        //
        // No interpreter is available pointing at a real engine here, so what is asserted is the guard
        // itself: after one call the state is live, and a second call must not add a second "launching"
        // notice. That is the observable difference between one spawn and two.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        controller.launch()
        let afterFirst = controller.logs.lines.filter {
            $0.text.contains("launching the engine")
        }.count
        controller.launch()
        let afterSecond = controller.logs.lines.filter {
            $0.text.contains("launching the engine")
        }.count
        XCTAssertEqual(afterSecond, afterFirst,
                       "a second launch while one is in flight must be a no-op")
    }

    func testRoutineCommandAcksDoNotFloodTheTerminal() {
        // The app polls `status` every two seconds, so an unfiltered log filled the terminal with an
        // endless "command acknowledged" column and buried the node transitions and gates a person
        // opens it to read. A *refused* ack is the exception: that is the reason a button did nothing,
        // so it stays.
        let controller = makeController(root: FileManager.default.temporaryDirectory)
        let before = controller.logs.lines.count

        controller.handle(EngineEvent(
            v: 1, seq: 1, type: "command.ack", payload: ["cmd_id": .string("c1"), "ok": .bool(true)],
            runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil,
            ts: "2026-09-19T00:00:00.000Z"))
        XCTAssertEqual(controller.logs.lines.count, before,
                       "a successful ack is the poll's heartbeat, not something to log")

        controller.handle(EngineEvent(
            v: 1, seq: 2, type: "command.ack",
            payload: ["cmd_id": .string("c2"), "ok": .bool(false),
                      "error": .string("no run is loaded to approve")],
            runId: nil, agentId: nil, nodeId: nil, sessionId: nil, phase: nil,
            ts: "2026-09-19T00:00:01.000Z"))
        XCTAssertGreaterThan(controller.logs.lines.count, before,
                             "a refused ack must be logged — it explains the dead button")
    }

    func testEverySystemGrantOfferedMatchesTheEnginesVocabulary() {
        // The toggle list and the engine's `SystemConfig.CAPABILITIES` must agree. If they drift, the
        // UI offers a grant the engine does not recognise — which reads as a checkbox that does
        // nothing — or hides one it does, which reads as a missing feature. Pinned here because the
        // two live in different languages and nothing else would catch it.
        //
        // The engine side is *read from `engine/config.py`* rather than written out here. It used to be
        // a hand-written list of six, and when the engine grew to twelve the guard did not fire — it
        // was asserting the old vocabulary against itself. A drift guard with a copy of one side
        // hardcoded is the drift it exists to catch, so this parses the declaration instead.
        let offered = CapabilityChoice.groups
            .flatMap { $0.choices.map(\.grant) }
            .filter { $0.hasPrefix("system:") }
        let engine = Self.declaredSystemGrants()
        XCTAssertFalse(engine.isEmpty,
                       "could not read SystemConfig.CAPABILITIES from engine/config.py — the guard is "
                       + "not testing anything until that parse works again")
        XCTAssertEqual(Set(offered), Set(engine),
                       "the console and the engine must offer the same system grants")
        XCTAssertEqual(offered.count, Set(offered).count,
                       "the console must not offer the same grant twice")

        // System grants are grouped apart from file grants, because "may edit code" is not "may act on
        // my desktop" and the UI must not present them as one undifferentiated list.
        let system = CapabilityChoice.groups.filter(\.isSystem)
        XCTAssertEqual(system.count, 1, "system grants belong in exactly one group")
        XCTAssertTrue(system[0].choices.allSatisfy { $0.grant.hasPrefix("system:") })
        XCTAssertFalse(CapabilityChoice.groups.filter { !$0.isSystem }
            .flatMap { $0.choices }.contains { $0.grant.hasPrefix("system:") })

        // Every choice explains itself: a grant with no stated consequence is one nobody can judge.
        for group in CapabilityChoice.groups {
            for choice in group.choices {
                XCTAssertFalse(choice.label.isEmpty)
                XCTAssertFalse(choice.detail.isEmpty, "\(choice.grant) must say what it reaches")
            }
        }
    }

    /// The `system:*` grants the engine declares, read from `engine/config.py`.
    ///
    /// Deliberately parsed rather than written out: the previous version of this guard carried a
    /// hand-written copy of the engine's six-grant vocabulary, so when the engine grew to twelve the
    /// guard still passed — it was comparing one stale list with another. Reading the declaration is
    /// what makes this a guard rather than a restatement.
    static func declaredSystemGrants() -> [String] {
        let repository = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // AgentOrgKitTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // macos
            .deletingLastPathComponent()   // AgentOrg
        let config = repository.appendingPathComponent("engine/config.py")
        guard let source = try? String(contentsOf: config, encoding: .utf8),
              let start = source.range(of: "CAPABILITIES: tuple[str, ...] = ("),
              // Bounded at the tuple's own closing line rather than the first `)`, because every
              // entry carries a trailing comment and one of them mentions a parenthesis.
              let end = source.range(of: "\n    )", range: start.upperBound..<source.endIndex)
        else { return [] }
        let body = String(source[start.upperBound..<end.lowerBound])
        // `system:softwareupdate` has no digits and `system:state` no uppercase, so a lowercase-only
        // class is enough; the grant grammar is fixed by the engine.
        return body.split(separator: "\n").compactMap { line in
            guard let open = line.firstIndex(of: "\""),
                  let close = line[line.index(after: open)...].firstIndex(of: "\"")
            else { return nil }
            let grant = String(line[line.index(after: open)..<close])
            return grant.hasPrefix("system:") ? grant : nil
        }
    }

    func testAGrantPayloadOmitsAnEmptyCapabilityList() {
        // An empty list means "use the skill's default" to the engine, so sending it would be a
        // different statement from sending nothing — and a hire the person never widened would look
        // like one that was narrowed to zero.
        let untouched = AgentDraft(name: "Sana", skill: "code-reviewer")
        XCTAssertNil(untouched.hirePayload()["capabilities"])

        let granted = AgentDraft(name: "SysOp", skill: "backend-developer",
                                 capabilities: ["read:*", "system:state"])
        XCTAssertEqual(granted.hirePayload()["capabilities"],
                       .array([.string("read:*"), .string("system:state")]))
    }
}
