//
//  SystemPanelTests.swift
//  AgentOrgKitTests
//
//  The System panel: what the engine declares, and who may do it.
//
//  WHY THE EXPECTATIONS ARE PARSED OUT OF THE PYTHON
//  -------------------------------------------------
//  Part 1 of this panel is "do not hardcode the capability descriptions in Swift". The same rule has to
//  apply to the *test* of it, or the test becomes the second hand-maintained copy that goes stale: a
//  suite asserting the twelve grants this build happens to know would keep passing while the engine
//  declared thirteen, which is exactly the drift the panel exists to prevent.
//
//  So the declared grants are read out of `engine/config.py` and the tools out of
//  `engine/sysctl_tools.py`, by the same small-regex-over-a-stable-block technique
//  `ProtocolContractTests` already uses for `EventType`. Adding a capability on the Python side fails
//  a test on the next Swift build rather than surfacing as a missing row months later.
//
//  The live assertions launch the **real engine**, because that is the only way to check the thing the
//  panel actually renders: `serve._cmd_system`'s reply, decoded. A stand-in would let the test agree
//  with a payload shape nothing produces.
//

import XCTest
@testable import AgentOrgKit

@MainActor
final class SystemPanelTests: XCTestCase {

    // MARK: - The Python sources, as the sources of truth

    /// The repository root — the directory holding `AgentOrg/` — found the way the app finds it.
    ///
    /// Five levels up from this file: it, `AgentOrgKitTests`, `Tests`, `macos`, `AgentOrg`. That is
    /// what `OrgSettings.discover` expects, because it appends `AgentOrg/engine` itself.
    private static func repositoryRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // AgentOrgKitTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // macos
            .deletingLastPathComponent()   // AgentOrg
            .deletingLastPathComponent()   // the repository root
    }

    /// The directory holding `engine/` — four levels up, one below the repository root.
    ///
    /// A separate function rather than a second use of `repositoryRoot()`, because the two differ by
    /// exactly the `AgentOrg/` component and confusing them is silent: `discover` appends
    /// `AgentOrg/engine` to the root, while a source read appends `engine/…` to *this*.
    private static func engineRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // AgentOrgKitTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // macos
            .deletingLastPathComponent()   // AgentOrg
    }

    private func engineSource(_ relativePath: String) throws -> String {
        let url = Self.engineRoot().appendingPathComponent(relativePath)
        guard let text = try? String(contentsOf: url, encoding: .utf8) else {
            throw XCTSkip("\(relativePath) is not available from this checkout")
        }
        return text
    }

    /// The grants `SystemConfig.CAPABILITIES` declares.
    ///
    /// Scoped to the declaration block rather than to the whole file, so a `system:` name mentioned in
    /// a docstring or a later class cannot contribute a false positive. The block ends at the first
    /// line that closes the tuple at its own indentation — **not** at the first `)`, which is what the
    /// first version of this used: `# Spotlight metadata queries (read-only index)` closes a
    /// parenthesis inside a comment, and the parse silently returned eight of the twelve grants. The
    /// assertion below caught it, which is the only reason this comment is here rather than a bug
    /// report.
    static func declaredGrants(in source: String) -> [String] {
        guard let start = source.range(of: "CAPABILITIES"),
              let end = source.range(of: "\n    )", range: start.upperBound..<source.endIndex)
        else { return [] }
        return literals(in: String(source[start.lowerBound..<end.upperBound]),
                        pattern: "\"(system:[a-z]+)\"")
    }

    /// The grants that have at least one tool, read from the tool module's own catalogue.
    ///
    /// Every `capability="…"` inside `CATALOGUE`, which is where `sysctl_tools` declares what it can
    /// enforce. `syscap._grants_with_tools` reads exactly this tuple, so the Python `available` flag
    /// and this expectation are derived from one list rather than two.
    static func grantsWithTools(in source: String) -> [String] {
        guard let start = source.range(of: "CATALOGUE: tuple[CatalogEntry, ...] = ("),
              // The next top-level declaration, which is where the tuple ends. Bounded this way
              // rather than at the first `)` for the reason given above — every tool's `description`
              // is a parenthesised string, so the first `)` is inside the *first* entry.
              let end = source.range(of: "\nMUTATING_TOOLS", range: start.upperBound..<source.endIndex)
        else { return [] }
        return literals(in: String(source[start.lowerBound..<end.lowerBound]),
                        pattern: "capability=\"(system:[a-z]+)\"")
    }

    /// Distinct first captures of `pattern`, in order.
    private static func literals(in text: String, pattern: String) -> [String] {
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return [] }
        let full = NSRange(text.startIndex..<text.endIndex, in: text)
        var seen = Set<String>()
        var out: [String] = []
        for match in regex.matches(in: text, range: full) {
            guard let range = Range(match.range(at: 1), in: text) else { continue }
            let value = String(text[range])
            if seen.insert(value).inserted { out.append(value) }
        }
        return out
    }

    /// Every tool name in `CATALOGUE`, in the catalogue's order and including repeats.
    ///
    /// The order matters here — it is the order the engine advertises tools in, and the reason the
    /// console's rows read as `syscap` intends — so this one does *not* deduplicate: comparing it with
    /// what the payload carries checks the sequence, not just the set.
    static func toolNames(in source: String) -> [String] {
        guard let start = source.range(of: "CATALOGUE: tuple[CatalogEntry, ...] = ("),
              let end = source.range(of: "\nMUTATING_TOOLS", range: start.upperBound..<source.endIndex)
        else { return [] }
        return ordered(in: String(source[start.lowerBound..<end.lowerBound]),
                       pattern: "name=\"([a-z_]+)\"")
    }

    /// The tools `CONSENT_REQUIRED` names — the ask-once set, read from its own declaration.
    ///
    /// A separate parse rather than a filter over the catalogue, because the two are separate
    /// declarations in `sysctl_tools.py` and it is their *agreement* with the payload that is being
    /// checked. Bounded at the closing `})` of the frozenset, which is the only `})` on its own line
    /// in that block.
    static func consentNames(in source: String) -> [String] {
        guard let start = source.range(of: "CONSENT_REQUIRED: frozenset[str] = frozenset({"),
              let end = source.range(of: "\n})", range: start.upperBound..<source.endIndex)
        else { return [] }
        return literals(in: String(source[start.lowerBound..<end.lowerBound]),
                        pattern: "\"([a-z_]+)\"")
    }

    /// First captures of `pattern`, in order, repeats kept.
    private static func ordered(in text: String, pattern: String) -> [String] {
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return [] }
        let full = NSRange(text.startIndex..<text.endIndex, in: text)
        return regex.matches(in: text, range: full).compactMap { match in
            Range(match.range(at: 1), in: text).map { String(text[$0]) }
        }
    }

    // MARK: - Decoding, with no engine required

    private func payload(_ entries: [[String: JSONValue]]) -> [String: JSONValue] {
        ["capabilities": .array(entries.map { .object($0) })]
    }

    private func entry(_ grant: String, title: String = "T", reaches: String = "R",
                       changes: String = "", caution: String = "",
                       available: Bool = true) -> [String: JSONValue] {
        ["grant": .string(grant), "title": .string(title), "reaches": .string(reaches),
         "changes": .string(changes), "caution": .string(caution), "available": .bool(available)]
    }

    func testDecodesTheEnginesPayloadInTheEngineOrder() {
        // Order is load-bearing: `syscap` runs from harmless reads to powerful grants, so a panel that
        // re-sorted them would put `system:automation` near the top of a list someone is skimming for
        // what is risky. Asserted as "the order the engine sent", not as one particular order.
        let decoded = SystemCapability.list(from: payload([
            entry("system:state", title: "Read machine state"),
            entry("system:media", title: "Control volume"),
            entry("system:automation", title: "Run AppleScript"),
        ]))
        XCTAssertEqual(decoded.map(\.grant),
                       ["system:state", "system:media", "system:automation"])
        XCTAssertEqual(decoded.first?.title, "Read machine state")
    }

    func testARowWithNoGrantIsDroppedRatherThanRenderedBlank() {
        // Only `grant` is required. An entry without one has nothing to key the row on, and rendering
        // it would produce an anonymous line in a list of permissions.
        let decoded = SystemCapability.list(from: payload([
            ["title": .string("nameless")],
            entry("system:state"),
        ]))
        XCTAssertEqual(decoded.map(\.grant), ["system:state"])
    }

    func testAvailableComesFromTheEngineRatherThanFromWhetherItChangesState() throws {
        // The distinction the `available` flag exists for, and the one a panel can get wrong by
        // inferring: a grant with no tool yet still *describes* what it would change. Inferring
        // availability from `changes` would report `system:softwareupdate` as working — a switch a
        // person would grant and then wonder why nothing happened.
        let decoded = SystemCapability.list(from: payload([
            entry("system:softwareupdate", changes: "your operating system", available: false),
        ]))
        let capability = try XCTUnwrap(decoded.first)
        XCTAssertFalse(capability.available)
        XCTAssertTrue(capability.changesState, "it still says what it would change")
        XCTAssertEqual(capability.stateWord, "no tool yet")
        XCTAssertEqual(capability.tone, .neutral,
                       "not built is not a warning — colouring it would send someone hunting a cause")
    }

    func testAReadSaysNothingChangedRatherThanLeavingTheFieldBlank() {
        // A blank line in a column headed "changes" reads as "unknown", and unknown is how a person
        // ends up refusing a harmless read. The negative case is stated.
        let decoded = SystemCapability.list(from: payload([entry("system:state")]))
        XCTAssertEqual(decoded.first?.changesState, false)
        XCTAssertEqual(decoded.first?.effect, "nothing — this only reads")
        XCTAssertEqual(decoded.first?.tone, .ok)
        XCTAssertEqual(decoded.first?.stateWord, "reads only")
    }

    func testAStateChangingGrantKeepsTheEnginesOwnSentence() {
        // The panel must not rewrite the engine's prose into its own shorter phrase: "the volume on
        // your desk, right now" is the sentence a person decides with, and Swift has no business
        // paraphrasing it.
        let decoded = SystemCapability.list(from: payload([
            entry("system:media", changes: "the volume on your desk, right now"),
        ]))
        XCTAssertEqual(decoded.first?.effect, "the volume on your desk, right now")
        XCTAssertEqual(decoded.first?.stateWord, "changes state")
        XCTAssertEqual(decoded.first?.tone, .attention)
    }

    func testAnEmptyPayloadDecodesToNoRowsRatherThanFailing() {
        // Before the first reply the controller holds `[:]`, and the pane must draw its "asking the
        // engine…" state rather than crash or invent rows.
        XCTAssertTrue(SystemCapability.list(from: [:]).isEmpty)
        XCTAssertTrue(SystemCapability.list(from: ["capabilities": .null]).isEmpty)
    }

    // MARK: - Who holds a grant

    private func agent(_ name: String, _ capabilities: [String]) -> [String: JSONValue] {
        ["name": .string(name),
         "capabilities": .array(capabilities.map { .string($0) })]
    }

    func testHoldersMatchesTheEnginesExactOrWildcardRule() {
        // The rule is `tools.ToolRegistry._granted_scoped`: an exact `system:<scope>` match or
        // `system:*`, and deliberately **no prefix matching** — a prefix rule would let
        // `system:state` reach a hypothetical `system:stateful`, a widening nobody asked for. If the
        // panel said a grant was held while the registry refused it, the panel would be confidently
        // wrong about a permission, which is worse than showing nothing.
        let roster = [
            agent("Alice", ["read:*", "system:state"]),
            agent("Ravi", ["system:*"]),
            agent("Nadia", ["read:*", "write:src/**"]),
            agent("Sam", ["system:stateful"]),
        ]
        XCTAssertEqual(SystemCapability.holders(of: "system:state", in: roster), ["Alice", "Ravi"])
        XCTAssertEqual(SystemCapability.holders(of: "system:media", in: roster), ["Ravi"],
                       "the wildcard reaches it, nobody else does")
        XCTAssertEqual(SystemCapability.holders(of: "system:notify", in: roster), ["Ravi"])
        XCTAssertFalse(SystemCapability.holders(of: "system:state", in: roster).contains("Sam"),
                       "`system:stateful` must not be read as `system:state`")
        XCTAssertEqual(SystemCapability.holders(of: "system:open", in: roster), ["Ravi"],
                       "a sibling grant must not leak")
    }

    func testNoHoldersIsAnEmptyListRatherThanAGuess() {
        XCTAssertTrue(SystemCapability.holders(of: "system:media", in: []).isEmpty)
        XCTAssertTrue(SystemCapability.holders(of: "system:media",
                                               in: [agent("Nadia", ["read:*"])]).isEmpty)
    }

    func testHoldersCarriesEveryNameAndSortsThem() {
        // Named rather than counted: "Alice, Ravi" answers "who can already do this". Sorted so a
        // stable roster draws a stable row — an order that shuffled between polls would look like a
        // change that did not happen.
        let roster = [agent("Ravi", ["system:media"]), agent("Alice", ["system:media"])]
        XCTAssertEqual(SystemCapability.holders(of: "system:media", in: roster), ["Alice", "Ravi"])
    }

    // MARK: - Against the real engine

    private func launchedController() async throws -> OrgController {
        let settings = OrgController.OrgSettings.discover(repositoryRoot: Self.repositoryRoot())
        let controller = OrgController(settings: settings, maxRestartAttempts: 0,
                                       preferences: .ephemeral())
        try XCTSkipUnless(controller.canLaunch, "no Python interpreter — nothing to read")
        try XCTSkipIf(controller.launchProblem() != nil,
                      controller.launchProblem() ?? "the engine is not launchable here")
        controller.launch()
        let end = Date().addingTimeInterval(60)
        while Date() < end && controller.engineState != .running {
            try? await Task.sleep(nanoseconds: 25_000_000)
        }
        XCTAssertEqual(controller.engineState, .running,
                       "the real engine must reach `running` via its readiness frame")
        return controller
    }

    func testThePanelRendersExactlyTheGrantsTheEngineDeclares() async throws {
        // The drift guard for Part 1, and the reason the expectations are parsed rather than listed:
        // the panel must show *every* capability `SystemConfig.CAPABILITIES` declares. A build that
        // hardcoded six would pass a hand-written test and silently hide the other six — which is the
        // whole failure mode the engine-side `syscap` module was written to avoid.
        let declared = Self.declaredGrants(in: try engineSource("engine/config.py"))
        XCTAssertEqual(declared.count, 12, "the config declares twelve capabilities")

        let controller = try await launchedController()
        defer { controller.stop() }
        await controller.loadSystem()

        let rendered = controller.systemCapabilities.map(\.grant)
        XCTAssertEqual(Set(rendered), Set(declared),
                       "the panel and the engine must not disagree about which grants exist")
        XCTAssertEqual(rendered.count, Set(rendered).count, "a grant rendered twice: \(rendered)")
        // And in the engine's own reading order, which starts with a read.
        XCTAssertEqual(rendered.first, declared.first)
    }

    func testEveryRowCarriesWhatAPersonNeedsToJudgeIt() async throws {
        // A row with no title or no "reaches" is a switch nobody can judge — `syscap` states this as
        // its own design rule, and this is the assertion that the panel actually receives it.
        let controller = try await launchedController()
        defer { controller.stop() }
        await controller.loadSystem()

        for capability in controller.systemCapabilities {
            XCTAssertFalse(capability.title.isEmpty, capability.grant)
            XCTAssertFalse(capability.reaches.isEmpty, "\(capability.grant) does not say what it reaches")
            XCTAssertFalse(capability.grant.hasPrefix("system:") == false,
                           "\(capability.grant) is not a system grant")
            XCTAssertFalse(capability.effect.isEmpty, "\(capability.grant) has no effect line")
        }
    }

    func testAvailableAgreesWithWhichGrantsHaveTools() async throws {
        // Derived from `sysctl_tools.CATALOGUE` — the tuple `syscap._grants_with_tools` itself reads —
        // rather than from a list written here. This is the assertion that stops the panel offering a
        // switch that buys nothing, and the six newer capabilities are deliberately in the "no tool"
        // set until they are built.
        let withTools = Set(Self.grantsWithTools(in: try engineSource("engine/sysctl_tools.py")))
        XCTAssertGreaterThan(withTools.count, 3, "the catalogue should declare tools")

        let controller = try await launchedController()
        defer { controller.stop() }
        await controller.loadSystem()

        for capability in controller.systemCapabilities {
            XCTAssertEqual(capability.available, withTools.contains(capability.grant),
                           "\(capability.grant): panel says available=\(capability.available), "
                           + "the catalogue says \(withTools.contains(capability.grant))")
        }
        // The declared-but-unbuilt set is reported rather than hidden, which is the point of the flag.
        XCTAssertEqual(controller.systemUnavailableCount,
                       controller.systemCapabilities.filter { !$0.available }.count)
    }

    func testTheSwitchesAndTheSummaryTravelWithTheCapabilities() async throws {
        // The list is inert when `enabled` is off, so the panel cannot draw it from the capabilities
        // alone — these four fields are what let it say *why*. Read from the same reply rather than
        // re-fetched, so the rows and the switches cannot disagree for a frame.
        let controller = try await launchedController()
        defer { controller.stop() }
        await controller.loadSystem()

        // The switches are **reported**, not asserted to be off. Reading the developer's own
        // `credentials.json` made the old assertions here claims about whoever ran the suite: they
        // held only until someone actually turned machine access on, which is the state this project
        // exists to reach. A test that goes red when the feature is used teaches the next person to
        // switch it back off. What this panel must guarantee is the *round trip* — that the engine's
        // answer reaches the UI unchanged — so that is what is checked, plus the internal consistency
        // the panel draws from. That the defaults are off is the engine's contract, asserted in Python
        // where the config can be constructed for the test instead of read from a machine.
        XCTAssertFalse(controller.systemSummary.isEmpty, "the engine reports its own summary")
        XCTAssertTrue(controller.systemSummary.contains("system capabilities granted"),
                      controller.systemSummary)
        // The count the panel draws from must be self-consistent: a grant is "live" when it has a tool
        // behind it. Two numbers describing one thing is how a panel ends up disagreeing with its own
        // headline, and this holds whatever the switches are set to.
        let withTools = controller.systemCapabilities.filter { $0.available }.count
        XCTAssertGreaterThan(withTools, 0, "the catalogue has capabilities behind it")
        XCTAssertEqual(withTools + controller.systemUnavailableCount,
                       controller.systemCapabilities.count,
                       "every row is either backed by a tool or reported as unbuilt")
        // Before the first read nothing is claimed, which draws as "off" rather than an optimistic
        // list — the honest reading of "the engine has not been asked".
        XCTAssertEqual(OrgController(settings: OrgController.OrgSettings.discover(
            repositoryRoot: Self.repositoryRoot()), preferences: .ephemeral()).systemSummary,
            "the engine has not been asked yet")
    }

    func testTheRosterAnswersWhoHoldsEachGrant() async throws {
        // The one question the engine's reply cannot answer for a single panel. The roster travels with
        // `status`, the capabilities with `system`, so the panel reads both — and a grant nobody holds
        // must come back empty rather than as an error.
        let controller = try await launchedController()
        defer { controller.stop() }
        await controller.loadSystem()
        await controller.loadRoster()

        for capability in controller.systemCapabilities {
            let holders = controller.systemHolders(of: capability.grant)
            XCTAssertEqual(holders, holders.sorted(), "names must be stable across polls")
            // The default roster is least privilege — `read:*` and a scoped write — so no agent holds
            // a machine capability until someone grants one. That is the state this panel exists to
            // make visible rather than to change.
            XCTAssertTrue(holders.isEmpty || !holders.contains { $0.isEmpty },
                          "\(capability.grant) produced an empty holder name")
        }
        XCTAssertEqual(controller.systemGrantedCount,
                       controller.systemCapabilities.filter {
                           !controller.systemHolders(of: $0.grant).isEmpty
                       }.count)
    }

    func testLoadingSystemIsOneRoundTripOnAWireTheEngineAnswers() async throws {
        // `loadSystem` goes through the same `fetch` as every other panel read, which is what makes a
        // refusal visible in the terminal rather than silent. Asserted as "the field is populated from
        // the engine" — an implementation that set `system` locally would leave this empty.
        let controller = try await launchedController()
        defer { controller.stop() }
        XCTAssertTrue(controller.system.isEmpty, "nothing is claimed before the read")
        await controller.loadSystem()
        XCTAssertFalse(controller.system.isEmpty, "the engine answered the `system` command")
        XCTAssertFalse(controller.systemCapabilities.isEmpty)
    }

    // MARK: - The catalogue and the grants the app acts on, checked against the engine's sources

    func testTheToolListTheAppHoldsIsTheEnginesCatalogue() async throws {
        // The guard for the deleted mirror. `SystemTools.all` was eighteen hand-written rows pointing at
        // `sysctl_tools.py` line numbers, which meant a tool added to the catalogue was a row the app
        // did not have until someone copied it across. The rows now arrive in the `system` reply, and
        // this compares them with the catalogue *as the engine declares it* — names in the catalogue's
        // own order, and the ask-once set read from `CONSENT_REQUIRED`'s own block.
        let source = try engineSource("engine/sysctl_tools.py")
        let names = Self.toolNames(in: source)
        let consent = Set(Self.consentNames(in: source))
        XCTAssertFalse(names.isEmpty, "could not read `CATALOGUE` — this guard is not testing anything")
        XCTAssertFalse(consent.isEmpty, "could not read `CONSENT_REQUIRED`")

        let controller = try await launchedController()
        defer { controller.stop() }
        await controller.loadSystem()

        let catalog = controller.systemToolCatalog
        XCTAssertEqual(catalog.all.map(\.name), names,
                       "the console's tool list must be the catalogue, in the catalogue's order")
        XCTAssertEqual(Set(catalog.consentRequired.map(\.name)), consent,
                       "the approvals section must list exactly what `CONSENT_REQUIRED` names")
        // A tool that no longer exists is refused by `system_invoke`, so a row for one is a button that
        // can only fail — and the tools are grouped under the grants the *same* reply declared.
        let declared = Set(Self.declaredGrants(in: try engineSource("engine/config.py")))
        for tool in catalog.all {
            XCTAssertTrue(declared.contains(tool.grant),
                          "\(tool.name) is filed under \(tool.grant), which the config does not declare")
        }
        XCTAssertGreaterThan(catalog.all.count, 3, "the catalogue should declare tools")
    }

    func testTheHireFormIsOfferedExactlyTheGrantsTheEngineDeclares() async throws {
        // The end-to-end version of the guard that used to live in `OrgControllerTests` with the
        // engine's list hardcoded beside it. The form takes its grants as an argument and the caller
        // reads them from this reply, so the two halves are now checked against `engine/config.py`
        // itself — a thirteenth grant is offerable the moment the config declares it, tools or no tools.
        let declared = Self.declaredGrants(in: try engineSource("engine/config.py"))
        XCTAssertEqual(declared.count, 12, "the config declares twelve capabilities")

        let controller = try await launchedController()
        defer { controller.stop() }
        await controller.loadSystem()

        let offered = CapabilityChoice.groups(systemGrants: controller.systemCapabilities.map(\.grant))
            .flatMap { $0.choices.map(\.grant) }
            .filter { $0.hasPrefix("system:") }
        XCTAssertEqual(offered, declared,
                       "the hire form and the engine must not disagree about which grants exist")
    }
}
