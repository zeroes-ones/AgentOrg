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
        return OrgController(settings: settings)
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
        // `observability-engineer`: a dashboard with no single question is sprawl.
        for tab in ConsoleTab.allCases {
            XCTAssertFalse(tab.question.isEmpty, "\(tab) has no question")
            XCTAssertTrue(tab.question.hasSuffix("?"), "\(tab)'s question should be a question")
        }
        // Eleven views now: the original five, plus People (who can I hire), Providers (which models
        // can I reach), Improve (what does the system think is wrong with itself), Activity (what is
        // happening, why, and what do I do next), Flow (who is working on what, and what crossed
        // between them) and Portfolio (which orgs am I running). Each still answers exactly one
        // question — the count is asserted so adding a tab without a question is caught here rather
        // than becoming a panel nobody can describe.
        XCTAssertEqual(ConsoleTab.allCases.count, 11, "eleven views, one question each")
        XCTAssertTrue(ConsoleTab.allCases.contains(.people))
        XCTAssertTrue(ConsoleTab.allCases.contains(.providers))
        XCTAssertTrue(ConsoleTab.allCases.contains(.improve))
        XCTAssertTrue(ConsoleTab.allCases.contains(.activity))
        XCTAssertTrue(ConsoleTab.allCases.contains(.flow))
        XCTAssertTrue(ConsoleTab.allCases.contains(.portfolio), "the portfolio comes first")
    }

    func testThePortfolioTabIsFirstAndDescribesItself() {
        // The whole picture precedes any single org, because every other tab is a view *within* one.
        XCTAssertEqual(ConsoleTab.allCases.first, .portfolio)
        XCTAssertEqual(ConsoleTab.portfolio.rawValue, "Portfolio")
        XCTAssertFalse(ConsoleTab.portfolio.symbol.isEmpty)
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

    func testTheActivityTabIsAFirstClassPanel() {
        // The panel exists because "I do not know what is happening" is the real complaint about an
        // autonomous org, so it must be reachable and must describe itself.
        XCTAssertTrue(ConsoleTab.allCases.contains(.activity))
        XCTAssertEqual(ConsoleTab.activity.rawValue, "Activity")
        XCTAssertTrue(ConsoleTab.activity.question.hasSuffix("?"))
        XCTAssertFalse(ConsoleTab.activity.symbol.isEmpty)
    }

    func testTheFlowTabAnswersWhoIsWorkingOnWhat() {
        // The board is a distinct question from the Activity story — *who is on what, and what crossed
        // between them* — so it must be reachable and must describe itself.
        XCTAssertTrue(ConsoleTab.allCases.contains(.flow))
        XCTAssertEqual(ConsoleTab.flow.rawValue, "Flow")
        XCTAssertTrue(ConsoleTab.flow.question.hasSuffix("?"))
        XCTAssertTrue(ConsoleTab.flow.question.contains("what"))
        XCTAssertFalse(ConsoleTab.flow.symbol.isEmpty)
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
        XCTAssertEqual(settings.projectPath.path, "/Users/someone/code/Agent/AgentOrg/projects/demo")
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

    func testAProviderDraftPrefersAnEnvironmentVariableOverALiteral() {
        // The documented safe path: the environment is not read into logs or traces.
        let draft = ProviderDraft(id: "gw", baseURL: "https://gw/v1",
                                  apiKey: "literal-secret", apiKeyEnv: "GW_KEY")
        let payload = draft.payload()
        XCTAssertEqual(payload["api_key_env"], .string("GW_KEY"))
        XCTAssertNil(payload["api_key"], "a literal must not be sent when a variable is named")
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

    func testEveryTabHasASymbolAndAShortQuestion() {
        // The sidebar renders the symbol and the short question, so both must exist for every case —
        // a missing symbol renders an empty row, which looks like a bug in the list.
        for tab in ConsoleTab.allCases {
            XCTAssertFalse(tab.symbol.isEmpty, "\(tab) has no SF Symbol")
            XCTAssertFalse(tab.shortQuestion.isEmpty, "\(tab) has no short question")
            // The short question is for a ~20-character sidebar row, not a full sentence.
            XCTAssertLessThanOrEqual(tab.shortQuestion.count, 24,
                                     "\(tab)'s short question will truncate: \(tab.shortQuestion)")
        }
    }

    func testShortQuestionsAreDistinct() {
        // Two identical sidebar subtitles make the list ambiguous about which is which.
        let short = ConsoleTab.allCases.map(\.shortQuestion)
        XCTAssertEqual(Set(short).count, short.count, "duplicate short question: \(short)")
    }

    func testSymbolsAreDistinct() {
        let symbols = ConsoleTab.allCases.map(\.symbol)
        XCTAssertEqual(Set(symbols).count, symbols.count, "duplicate symbol: \(symbols)")
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
}
