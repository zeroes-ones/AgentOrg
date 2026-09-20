//
//  SetupReadinessTests.swift
//  AgentOrgKitTests
//
//  The first-run gate: is a run actually possible yet?
//
//  WHY THIS IS THE MOST VALUABLE TEST IN THE REWRITE
//  ------------------------------------------------
//  The audit's biggest finding was not a wrong panel, it was that **nothing looked broken while
//  nothing could run**. The engine starts fine with no credentials (it falls back to
//  `credentials.example.json`), so the app showed a healthy shell — and the fact that no default model
//  resolved, and therefore no agent could bind, only surfaced as an empty hire form two panels away
//  from where the person was standing.
//
//  So the gate is a function, and every branch is asserted here: each blocking case, the order they
//  take precedence in, and — most importantly — the *safe* direction of each unknown. A gate that
//  passes when it should block is the failure this file exists to prevent.
//

import XCTest
@testable import AgentOrgKit

@MainActor
final class SetupReadinessTests: XCTestCase {

    private var suiteName = ""
    private var store: UserDefaults!

    override func setUpWithError() throws {
        // A suite of this test's own, so the assertions are about `AppPreferences` and not about
        // whatever the developer's machine last stored under the same keys.
        suiteName = "org.agentorg.tests.\(UUID().uuidString)"
        store = try XCTUnwrap(UserDefaults(suiteName: suiteName))
    }

    override func tearDownWithError() throws {
        store.removePersistentDomain(forName: suiteName)
    }

    // MARK: - The engine has to be up before anything else can be asked

    func testAStoppedEngineGateReportsTheEnginesOwnReasonForFailing() {
        // The reason, not the fact. A first-run step that says "the engine is not running" when the
        // engine told it exactly why is a step that sends the person to the wrong place.
        let gate = SetupReadiness.gate(
            engineIsRunning: false,
            engineFailure: "no provider could be built from the configuration",
            defaults: [:], providers: [], projectConfirmed: false, postureChosen: false)
        guard case .engineUnavailable(let reason) = gate else {
            return XCTFail("expected engineUnavailable, got \(gate)")
        }
        XCTAssertEqual(reason, "no provider could be built from the configuration")
        XCTAssertEqual(gate.step, 1)
        XCTAssertTrue(gate.isBlocking)
    }

    func testAnEngineThatNeverStartedStillBlocksAndSaysSomethingUseful() {
        let gate = SetupReadiness.gate(engineIsRunning: false, defaults: [:], providers: [],
                                       projectConfirmed: false, postureChosen: false)
        XCTAssertTrue(gate.isBlocking)
        XCTAssertFalse(gate.detail.isEmpty, "a blocking gate must say what to do about it")
    }

    // MARK: - Step one: a model that resolves

    func testNoProvidersAtAllAsksForAnEndpointRatherThanAModel() {
        // The two need different actions from the person: "add an endpoint" versus "pick one of the
        // endpoints you have". Collapsing them into one message sends half the readers to the wrong
        // control.
        let gate = SetupReadiness.gate(
            engineIsRunning: true,
            defaults: [:], providers: [], projectConfirmed: false, postureChosen: false)
        guard case .needsModel(let why) = gate else {
            return XCTFail("expected needsModel, got \(gate)")
        }
        XCTAssertTrue(why.contains("provider"), why)
    }

    func testAProviderWithNoDefaultModelBlocksAndRepeatsTheEnginesOwnReason() {
        // The exact first-run state the audit described: the engine started fine, a provider exists,
        // and no default resolves — so nothing looks broken and no agent can bind.
        let gate = SetupReadiness.gate(
            engineIsRunning: true,
            defaults: ["provider": .string(""), "model": .string(""),
                       "reason": .string("no default is set")],
            providers: [["id": .string("local"), "kind": .string("ollama")]],
            projectConfirmed: true, postureChosen: true)
        guard case .needsModel(let why) = gate else {
            return XCTFail("expected needsModel, got \(gate)")
        }
        XCTAssertEqual(why, "no default is set",
                       "the engine's own explanation is better than one this app invents")
    }

    func testAModelWithNoKnownWindowBlocksBecauseNoAgentCouldBind() {
        // Not a formality: the engine resolves a window from the catalog first and the declared table
        // second, and refuses a hire when neither knows it. A wizard that passed this step would let
        // the person reach a state where hiring is impossible with no explanation.
        let gate = SetupReadiness.gate(
            engineIsRunning: true,
            defaults: ["provider": .string("ollama"), "model": .string("mystery-model")],
            providers: [["id": .string("ollama")]],
            projectConfirmed: true, postureChosen: true)
        guard case .needsWindow(_, let provider, let model) = gate else {
            return XCTFail("expected needsWindow, got \(gate)")
        }
        XCTAssertEqual(provider, "ollama")
        XCTAssertEqual(model, "mystery-model")
        XCTAssertTrue(gate.detail.contains("mystery-model"), gate.detail)
    }

    func testAZeroWindowIsTreatedAsNoWindow() {
        // The engine's own resolution reports `0` when it has nothing usable; reading that as a real
        // window would bind an agent to a context of no size.
        let gate = SetupReadiness.gate(
            engineIsRunning: true,
            defaults: ["provider": .string("p"), "model": .string("m"),
                       "context_window": .int(0)],
            providers: [["id": .string("p")]],
            projectConfirmed: true, postureChosen: true)
        guard case .needsWindow = gate else {
            return XCTFail("expected needsWindow, got \(gate)")
        }
    }

    // MARK: - Steps two and three

    func testAResolvingModelStillBlocksUntilTheProjectIsConfirmed() {
        let gate = SetupReadiness.gate(
            engineIsRunning: true,
            defaults: readyDefaults,
            providers: [["id": .string("p")]],
            projectConfirmed: false, postureChosen: true)
        guard case .needsProject = gate else {
            return XCTFail("expected needsProject, got \(gate)")
        }
        XCTAssertEqual(gate.step, 2)
    }

    func testAConfirmedProjectStillBlocksUntilTheAutonomyIsChosen() {
        // The last step, and the one the wizard exists for: the app must ask before it starts
        // deciding things, because a default chosen by the app is no choice at all.
        let gate = SetupReadiness.gate(
            engineIsRunning: true,
            defaults: readyDefaults,
            providers: [["id": .string("p")]],
            projectConfirmed: true, postureChosen: false)
        guard case .needsAutonomy = gate else {
            return XCTFail("expected needsAutonomy, got \(gate)")
        }
        XCTAssertEqual(gate.step, 3)
        XCTAssertTrue(gate.detail.contains("Unattended"), gate.detail)
    }

    func testEveryAnswerPresentMeansReadyAndTheGateStopsBlocking() {
        let gate = SetupReadiness.gate(
            engineIsRunning: true,
            defaults: readyDefaults,
            providers: [["id": .string("p")]],
            projectConfirmed: true, postureChosen: true)
        XCTAssertEqual(gate, .ready)
        XCTAssertFalse(gate.isBlocking)
        XCTAssertNil(SetupReadiness.spineHint(for: gate),
                     "a ready setup must not leave a hint nagging on the spine")
    }

    // MARK: - The order is by dependency, not by difficulty

    func testTheGateReportsTheEarliestBlockingStepNotTheLastOne() {
        // With nothing done, the model step is the one shown — because the project question can be
        // answered at any time and the model one cannot, so leading with the project would be leading
        // with the question whose answer does not unblock anything.
        let gate = SetupReadiness.gate(
            engineIsRunning: true, defaults: [:], providers: [],
            projectConfirmed: false, postureChosen: false)
        XCTAssertEqual(gate.step, 1)
    }

    func testEachBlockingStepHasItsOwnTitleAndDetail() {
        // A wizard whose steps all read the same is a wizard nobody can tell progress in — and a
        // `Kind` for every gate, so a view can switch on which step it is without matching the text.
        let gates: [SetupGate] = [
            .engineUnavailable(reason: "x"), .needsModel(why: "x"),
            .needsWindow(why: "x", provider: "p", model: "m"),
            .needsProject(why: "x"), .needsAutonomy(why: "x"), .ready,
        ]
        let kinds = gates.map(\.kind)
        XCTAssertEqual(Set(kinds).count, kinds.count, "duplicate kind: \(kinds)")
        XCTAssertEqual(Set(kinds), Set(SetupGate.Kind.allCases),
                       "every kind must be reachable by the gate function")

        // `needsModel` and `needsWindow` share "Choose a model" on purpose: they are one step of the
        // wizard, and the second is the first one's follow-up question — a person should not be told
        // they are on a different step because the window is missing. The *three* steps are distinct.
        let stepTitles = Set([SetupGate.needsModel(why: "x").title,
                              SetupGate.needsProject(why: "x").title,
                              SetupGate.needsAutonomy(why: "x").title])
        XCTAssertEqual(stepTitles.count, 3, "the three steps must read differently")
        XCTAssertEqual(SetupGate.needsModel(why: "x").title,
                       SetupGate.needsWindow(why: "x", provider: "p", model: "m").title)
        for gate in gates {
            XCTAssertFalse(gate.title.isEmpty)
            XCTAssertFalse(gate.detail.isEmpty)
        }
    }

    func testEveryBlockingGateProducesASpineHintAndReadyProducesNone() {
        // The spine is one line, so every blocking gate has to reduce to one — and `ready` must reduce
        // to nothing at all, or the hint becomes a permanent nag.
        for gate in [SetupGate.needsModel(why: "no model"),
                     .needsWindow(why: "no window", provider: "p", model: "m"),
                     .needsProject(why: "no project"),
                     .needsAutonomy(why: "no autonomy"),
                     .engineUnavailable(reason: nil)] {
            let hint = SetupReadiness.spineHint(for: gate)
            XCTAssertNotNil(hint, "\(gate) produced no spine hint")
            XCTAssertFalse(hint?.isEmpty ?? true)
        }
        XCTAssertNil(SetupReadiness.spineHint(for: .ready))
    }

    // MARK: - The app's own remembered answers

    func testAPostureChosenInTheAppIsRememberedAndRoundTrips() {
        // The app cannot write the engine's `goal.default_posture` — there is no command for it — so it
        // remembers the choice and sends it with every goal it sets. This is that memory.
        let preferences = AppPreferences(store: store)
        XCTAssertNil(preferences.chosenPosture, "not chosen yet is not the same as chosen unattended")
        preferences.chosenPosture = .supervised
        XCTAssertEqual(AppPreferences(store: store).chosenPosture, .supervised,
                       "the choice must survive; a preference that does not is not one")
        preferences.chosenPosture = .unattended
        XCTAssertEqual(AppPreferences(store: store).chosenPosture, .unattended)
    }

    func testTheUnknownPostureIsNeverStoredOrOffered() {
        // `unknown` is a rendering state for a posture this build cannot read, not a choice. Storing it
        // would mean the app later sent a posture the engine refuses.
        let preferences = AppPreferences(store: store)
        preferences.chosenPosture = .unknown
        XCTAssertNil(preferences.chosenPosture, "an unreadable posture is not a stored choice")
        XCTAssertFalse(OrgController.Posture.choices.contains(.unknown))
    }

    func testTheWizardAnswersAreAllForgettableSoSetupCanRunAgain() {
        let preferences = AppPreferences(store: store)
        preferences.chosenPosture = .supervised
        preferences.projectConfirmed = true
        preferences.wizardCompleted = true
        preferences.reset()
        XCTAssertNil(preferences.chosenPosture)
        XCTAssertFalse(preferences.projectConfirmed)
        XCTAssertFalse(preferences.wizardCompleted)
    }

    func testTheEphemeralStoreDoesNotLeakBetweenInstances() {
        // Used for previews and throwaway controllers; two of them must not share state through
        // `UserDefaults.standard`.
        let first = AppPreferences.ephemeral()
        first.chosenPosture = .supervised
        first.wizardCompleted = true
        let second = AppPreferences.ephemeral()
        XCTAssertNil(second.chosenPosture)
        XCTAssertFalse(second.wizardCompleted)
    }

    // MARK: - The controller's use of the gate

    private func makeController(preferences: AppPreferences) -> OrgController {
        OrgController(
            settings: OrgController.OrgSettings(
                engineRoot: URL(fileURLWithPath: "/nonexistent"),
                projectPath: URL(fileURLWithPath: "/nonexistent/projects/p"),
                credentialsPath: nil, libraryRoot: nil),
            preferences: preferences)
    }

    func testTheControllerBlocksTheWizardUntilBothAppAnswersExist() {
        let preferences = AppPreferences(store: store)
        let controller = makeController(preferences: preferences)
        // No engine at all here, so nothing can be ready — but the two app answers are what the
        // controller *owns*, and they must be reflected the moment they are given.
        controller.applyStatus([
            "defaults": .object(["provider": .string("p"), "model": .string("m"),
                                 "context_window": .int(8192)]),
            "providers": .array([.object(["id": .string("p")])]),
        ])
        controller.confirmProject()
        controller.goalPosturePreference = .unattended
        // Every answer exists, and the gate still blocks — because the engine is not running, and the
        // engine step comes first by dependency. This is the point: the app's own answers are not
        // enough to call a setup ready.
        XCTAssertEqual(controller.setupGate.kind, .engineUnavailable)
        XCTAssertTrue(controller.setupGate.isBlocking)
    }

    func testASpineHintIsSuppressedOnceTheWizardHasBeenCompleted() {
        // The wizard is where a first-run answer belongs. Repeating "choose a model in Setup" on the
        // spine for the rest of the session would be a permanent nag for something already done.
        let preferences = AppPreferences(store: store)
        preferences.wizardCompleted = true
        let controller = makeController(preferences: preferences)
        // A stopped engine is a normal state, not a configuration problem: the spine's own row says
        // "Engine not running" with a Start button beside it, so a hint here would be noise.
        XCTAssertNil(controller.setupHint,
                     "a completed wizard must not leave the first-run hint on the spine")
    }

    func testASpineHintStillWarnsWhenConfigurationStopsWorkingAfterSetup() {
        // The other side of the same rule: once the wizard is done, a *configuration* that stopped
        // working must still be pointed at — that is the "nothing looks broken while nothing can run"
        // trap this whole mechanism exists to close, and completing the wizard must not reopen it.
        //
        // Driven through `SetupReadiness.gate` directly rather than through a controller, because a
        // controller's engine state is a live process: what is being asserted is the *rule* about the
        // hint, not how to start a Python engine in a unit test.
        let gate = SetupReadiness.gate(
            engineIsRunning: true,
            defaults: ["provider": .string(""), "model": .string(""),
                       "reason": .string("the provider was removed")],
            providers: [["id": .string("p")]],
            projectConfirmed: true, postureChosen: true)
        XCTAssertEqual(gate.kind, .needsModel)
        let hint = SetupReadiness.spineHint(for: gate)
        XCTAssertNotNil(hint, "a removed provider must still be pointed at once setup is done")
        XCTAssertTrue(hint?.contains("model") ?? false, hint ?? "nil")
    }

    func testCompletingSetupOnlyMarksDoneWhenEveryAnswerExists() {
        // A half-answered wizard that marked itself done would leave the window in a state it claims
        // is ready, with no way back to the questions.
        let preferences = AppPreferences(store: store)
        let controller = makeController(preferences: preferences)
        controller.completeSetup()
        XCTAssertFalse(preferences.wizardCompleted, "nothing was answered, so nothing is complete")
    }

    private var readyDefaults: [String: JSONValue] {
        ["provider": .string("ollama"), "model": .string("qwen2.5-coder:7b"),
         "context_window": .int(32768)]
    }
}
