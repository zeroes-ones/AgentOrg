//
//  EngineDerivationTests.swift
//  AgentOrgKitTests
//
//  The surfaces hold no copy of what the engine owns.
//
//  WHY THIS FILE EXISTS
//  --------------------
//  Every drift this app has actually had was one shape: a list, a mapping or a sentence written in
//  Swift that mirrored something the engine already knew — the twelve system grants (six of them
//  written out, six silently missing), the eighteen-entry tool catalogue, the two stop-token sentences,
//  the seven next-action kinds. Each was correct when it was written and quietly wrong later, and each
//  was invisible because a second copy cannot disagree with itself.
//
//  So there are two kinds of guard here, and both are needed:
//
//  1. **Decode tests** — the payload fields the engine added for these surfaces are read correctly,
//     including the states that only appear when something is wrong (an empty reply, a missing field).
//  2. **Source guards** — the Swift files contain no such copy at all. A decode test cannot fail if
//     someone adds a second list beside the decode; reading the source is what catches that.
//
//  The source guards assert as little as they can get away with: a *quoted* grant literal, a *switch
//  case* over kind names, an engine sentence. Prose in a doc comment explaining the rule is exactly
//  what should be there, and these patterns are chosen not to trip on it.
//

import XCTest
@testable import AgentOrgKit

final class EngineDerivationTests: XCTestCase {

    // MARK: - The sources

    /// The directory holding `engine/` and `macos/` — found the way `SystemPanelTests` finds it.
    private static func repositoryRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // AgentOrgKitTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // macos
            .deletingLastPathComponent()   // AgentOrg
    }

    private func source(_ relativePath: String) throws -> String {
        let url = Self.repositoryRoot().appendingPathComponent(relativePath)
        guard let text = try? String(contentsOf: url, encoding: .utf8) else {
            throw XCTSkip("\(relativePath) is not available from this checkout")
        }
        return text
    }

    /// Whether `pattern` occurs in `text`, as a whole-file regex search.
    private func contains(_ pattern: String, in text: String) -> Bool {
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return false }
        return regex.firstMatch(in: text, range: NSRange(text.startIndex..<text.endIndex, in: text))
            != nil
    }

    // MARK: - The tool catalogue, decoded

    private func tool(_ name: String, grant: String = "system:state", mutates: Bool = false,
                      runsWithoutArguments: Bool = true,
                      consentRequired: Bool = false) -> [String: JSONValue] {
        ["name": .string(name), "grant": .string(grant), "mutates": .bool(mutates),
         "runs_without_arguments": .bool(runsWithoutArguments),
         "consent_required": .bool(consentRequired)]
    }

    private func payload(_ tools: [[String: JSONValue]]) -> [String: JSONValue] {
        ["tools": .array(tools.map { .object($0) })]
    }

    func testTheToolListIsDecodedWithTheFlagsTheEngineSent() {
        // Four facts, each the engine's answer rather than a guess: the name `system_invoke` takes, the
        // grant that decides whether it is offered, whether the call changes anything, and whether a
        // bare name is a complete call (read off the entry's own JSON schema by `syscap.tools_payload`).
        let catalog = SystemToolCatalog(payload([
            tool("open_app", grant: "system:open", mutates: true, runsWithoutArguments: false,
                 consentRequired: true),
            tool("read_clipboard", grant: "system:clipboard"),
        ]))
        XCTAssertEqual(catalog.all.map(\.name), ["open_app", "read_clipboard"],
                       "the engine's order is kept, so a panel grouped by grant reads as it declares")
        let open = catalog.all.first
        XCTAssertEqual(open?.grant, "system:open")
        XCTAssertEqual(open?.mutates, true)
        XCTAssertEqual(open?.runsWithoutArguments, false)
        XCTAssertEqual(open?.consentRequired, true)
        XCTAssertEqual(open?.isSafeToTry, false, "a state change is a decision, not a demonstration")
    }

    func testAToolWithNoNameIsDroppedRatherThanOffered() {
        // `system_invoke` takes the name and nothing else identifies a tool, so a row without one is
        // not a row — the same rule a capability row without a grant follows.
        let catalog = SystemToolCatalog(payload([
            ["grant": .string("system:state"), "mutates": .bool(false)],
            tool("system_state"),
        ]))
        XCTAssertEqual(catalog.all.map(\.name), ["system_state"])
    }

    func testAnEmptyReplyOffersNoToolsRatherThanAGuessedList() {
        // Before the first reply the controller holds `[:]`, and the panel must draw its "asking the
        // engine…" state. A list from memory here would be the copy this whole change deleted.
        XCTAssertTrue(SystemToolCatalog([:]).isEmpty)
        XCTAssertTrue(SystemToolCatalog(["tools": .null]).isEmpty)
        XCTAssertFalse(SystemToolCatalog(payload([tool("system_state")])).isEmpty)
    }

    func testTheCatalogGroupsByGrantAndSeparatesTheToolsThatAskFirst() {
        // `forGrant` is what a capability row shows, `consentRequired` is what the approvals section
        // lists, and `tryable` is the one tool worth a "try it" — all three read from the same decoded
        // entries, so a row and the list under it cannot disagree about a tool.
        let catalog = SystemToolCatalog(payload([
            tool("system_state"),
            tool("get_volume", grant: "system:media"),
            tool("set_volume", grant: "system:media", mutates: true, runsWithoutArguments: false,
                 consentRequired: true),
            tool("sleep_now", grant: "system:power", mutates: true, consentRequired: true),
        ]))
        XCTAssertEqual(catalog.forGrant("system:media").map(\.name), ["get_volume", "set_volume"])
        XCTAssertTrue(catalog.forGrant("system:search").isEmpty)
        XCTAssertEqual(catalog.consentRequired.map(\.name), ["set_volume", "sleep_now"])
        XCTAssertEqual(catalog.tryable("system:media")?.name, "get_volume",
                       "the read with no arguments is the one that can be demonstrated safely")
        XCTAssertNil(catalog.tryable("system:power"),
                     "`sleep_now` takes no arguments either, and putting the machine to sleep from a "
                     + "button pressed to find out what a row means is the failure this guards")
        XCTAssertEqual(catalog.grants, ["system:state", "system:media", "system:power"])
    }

    // MARK: - The engine's stop vocabulary, decoded

    private func report(_ table: [String: String]) -> [String: JSONValue] {
        ["stop_words": .object(table.mapValues { .string($0) })]
    }

    func testTheStopWordingIsReadFromTheReportThatCarriesIt() {
        // The engine's own sentence for a token, arriving in the payload rather than being rewritten
        // here. `gloss` answers "" for a token this build does not know, which is what the caller uses
        // to decide to show the token itself.
        let words = StopWords(report(["guardrail-blocked": "refused at the edge, say"]))
        XCTAssertEqual(words.gloss("guardrail-blocked"), "refused at the edge, say")
        XCTAssertEqual(words.gloss("a-token-from-a-later-engine"), "")
        XCTAssertEqual(words.tokens, ["guardrail-blocked"])
    }

    func testAVocabularyThatHasNotArrivedGlossesNothingRatherThanInventingSomething() {
        // A report from a build that predates the key, or no report at all. Empty is the honest state:
        // the row keeps the token the engine recorded.
        XCTAssertTrue(StopWords([:]).isEmpty)
        XCTAssertTrue(StopWords(["stop_words": .null]).isEmpty)
        XCTAssertEqual(StopWords([:]).gloss("guardrail-blocked"), "")
        XCTAssertEqual(StopWords(report(["error": ""])).gloss("error"), "",
                       "an empty sentence is not a sentence")
    }

    func testTheBoardAndTheActivityReportShareOneVocabulary() {
        // Both reports carry the same table — the board renders a row's bare `verdict`, the Now pane the
        // run's `stop_reason` — so the merge is used both ways round and a missing report costs nothing.
        let fromBoard = report(["guardrail-blocked": "one", "contract": "two"])
        let fromActivity = report(["error": "three"])
        XCTAssertEqual(StopWords(fromBoard, fromActivity).tokens,
                       ["contract", "error", "guardrail-blocked"])
        XCTAssertEqual(StopWords(fromBoard).gloss("contract"), "two")
        XCTAssertEqual(StopWords(fromActivity, fromBoard).gloss("error"), "three",
                       "whichever report arrived first still answers")
        XCTAssertEqual(StopWords(report(["a": "first"]), report(["a": "second"])).gloss("a"), "first",
                       "one token is described one way even if two reports somehow disagree")
    }

    // MARK: - The hire and provider vocabularies, decoded

    func testTheHireLevelsAreDecodedFromTheReplyNotListedInSwift() {
        // `people.LEVELS` resolves six names; the hire form spelled five of them, so `mid` — which the
        // engine accepts for `hire --level` — could not be chosen from the app at all. The decode keeps
        // whatever the engine sent, in the engine's order.
        let vocabulary = HireVocabulary([
            "levels": .array(["junior", "practitioner", "mid", "senior", "staff", "principal"]
                .map { .string($0) }),
            "roles": .array(["worker", "reviewer"].map { .string($0) }),
        ])
        XCTAssertEqual(vocabulary.levels,
                       ["junior", "practitioner", "mid", "senior", "staff", "principal"])
        XCTAssertTrue(vocabulary.levels?.contains("mid") == true,
                      "the level the app used to make unreachable")
        XCTAssertEqual(vocabulary.roles, ["worker", "reviewer"])
    }

    func testAVocabularyThatHasNotArrivedIsNilAndNotEmpty() {
        // The distinction the forms draw: a reply with no `levels` (or `kinds`) key is "the engine has
        // not answered", which must not render as an empty picker claiming there are none. An empty
        // array *is* the engine's own answer and is kept as one.
        XCTAssertNil(HireVocabulary([:]).levels)
        XCTAssertNil(HireVocabulary(["levels": .null]).levels)
        XCTAssertNil(ProviderKindVocabulary([:]).kinds)
        XCTAssertEqual(HireVocabulary(["levels": .array([])]).levels, [])
        XCTAssertEqual(ProviderKindVocabulary(["kinds": .array([])]).kinds, [])
    }

    func testTheProviderKindsAreDecodedFromTheReply() {
        // The editor's three tags (`openai`/`anthropic`/`ollama`) were a second copy of
        // `config.SUPPORTED_KINDS`. The reply's `kinds` is the only source now, so a fourth dialect the
        // engine accepts appears without an edit here.
        let vocabulary = ProviderKindVocabulary(
            ["kinds": .array(["openai", "anthropic", "ollama", "gemini"].map { .string($0) })])
        XCTAssertEqual(vocabulary.kinds, ["openai", "anthropic", "ollama", "gemini"])
    }

    // MARK: - The source guards

    func testTheCapabilityFileHasNoToolListAndNoGrantListOfItsOwn() throws {
        // The rule this file exists for: `SystemCapabilities.swift` decodes. It used to hold eighteen
        // `SystemTool(...)` literals and a comment calling itself "a known defect, not a design"; both
        // are gone, and this is what stops them coming back. `"system:*"` is allowed — it is the
        // wildcard *rule* the tool registry enforces, not a grant from a list.
        let text = try source("macos/Sources/AgentOrgKit/SystemCapabilities.swift")
        XCTAssertFalse(contains("SystemTool\\(", in: text),
                       "a hand-written tool row: every tool must come from the engine's reply")
        XCTAssertFalse(contains("\"system:[^*]", in: text),
                       "a grant name written out in Swift: the grants are the engine's to declare")
    }

    func testTheEnginesStopSentenceHasOneHomeAndItIsNotInSwift() throws {
        // One token, one sentence. The engine's table is the home; the app decodes it. If the engine
        // ever drops that wording this fails too — the guard is two-sided on purpose, so a deleted
        // sentence cannot leave both surfaces silently saying nothing.
        //
        // The patterns are the shape a *copy* takes: a `case` over an engine token, and the engine's
        // sentence as a string literal. Doc comments in this app quote both — explaining what a token
        // means is exactly what they should do — so the quoted-token form is deliberately not matched.
        let engine = try source("engine/flow.py")
        XCTAssertTrue(engine.contains("the work finished, but what it handed on was refused at the edge"),
                      "the engine's own gloss for `guardrail-blocked` is the one that is shown")
        let nowPane = try source("macos/Sources/AgentOrg/NowPane.swift")
        XCTAssertFalse(nowPane.contains("case \"guardrail-blocked\""),
                       "the engine's token switched on in Swift — decode `stop_words` instead")
        XCTAssertFalse(nowPane.contains("case \"contract-violation\""),
                       "the engine's token switched on in Swift")
        XCTAssertFalse(nowPane.contains("\"the work finished, but what it handed on"),
                       "the engine's sentence written a second time in Swift")
        XCTAssertFalse(nowPane.contains("\"its result did not satisfy the node's completion contract\""),
                       "the engine's sentence for `contract-violation`, written a second time")
    }

    // MARK: - A thirteenth grant, and a nineteenth tool

    func testAPayloadFromAnEngineWithAGrantAndToolThisBuildHasNeverSeenIsOfferedAsItStands() {
        // The property the whole change exists for, as one test: an engine that declares a thirteenth
        // grant and a nineteenth tool — neither of them mentioned anywhere in this app — produces a
        // capability row, a checkbox in the hire form, a tool to run and an approval to give, with no
        // edit to a single Swift file.
        //
        // The payload below is the shape `syscap.console_payload` returns for that engine; the values
        // were taken from a run of it with `SystemConfig.CAPABILITIES` and `sysctl_tools.CATALOGUE`
        // extended in-process (the `reaches` placeholder is the engine's own wording for a grant that
        // was declared without prose).
        let payload: [String: JSONValue] = [
            "capabilities": .array([.object([
                "grant": .string("system:hologram"),
                "title": .string("Hologram"),
                "reaches": .string("(not yet described — this grant was added to the config without "
                                   + "prose)"),
                "changes": .string(""), "caution": .string(""), "available": .bool(true)])]),
            "tools": .array([.object([
                "name": .string("hologram_cast"), "grant": .string("system:hologram"),
                "mutates": .bool(false), "runs_without_arguments": .bool(true),
                "consent_required": .bool(true)])]),
            "unavailable": .array([]),
        ]

        // The panel's row, and the hire form's checkbox — the form's labelling is the app's prose, keyed
        // by a grant it has never seen, which is why it falls back to a placeholder rather than dropping
        // it.
        let capabilities = SystemCapability.list(from: payload)
        XCTAssertEqual(capabilities.map(\.grant), ["system:hologram"])
        let offered = CapabilityChoice.groups(systemGrants: capabilities.map(\.grant))
            .first { $0.isSystem }?.choices
        XCTAssertEqual(offered?.map(\.grant), ["system:hologram"])
        XCTAssertEqual(offered?.first?.label, "Hologram", "a placeholder, and it still appears")

        // The action surface: the tool to run, and the approval the engine will demand for it.
        let catalog = SystemToolCatalog(payload)
        XCTAssertEqual(catalog.all.map(\.name), ["hologram_cast"])
        XCTAssertEqual(catalog.consentRequired.map(\.name), ["hologram_cast"])
        XCTAssertEqual(catalog.tryable("system:hologram")?.name, "hologram_cast")
        XCTAssertEqual(catalog.tryable("system:hologram")?.isSafeToTry, true)
    }

    func testTheNextLineDoesNotEnumerateTheEnginesKinds() throws {
        // `canPerform` used to be a switch over the kinds `engine/activity.py::_next_action` emits, which
        // meant the app had to be edited every time the engine gained one — `retry` was the kind that
        // proved it. The engine now sends `performable` and `needs`, so no kind name may be compared
        // here; a `case` for one of them is the copy coming back.
        let text = try source("macos/Sources/AgentOrgKit/Spine.swift")
        XCTAssertFalse(contains("case \"(decide|hire|investigate|retry|resume|start|none)\"",
                                in: text),
                       "a switch over the engine's next-action kinds: read `performable`/`needs`")
        XCTAssertTrue(contains("\"performable\"", in: text),
                      "`NextLine` must read the field the engine sends")
        XCTAssertTrue(contains("\"needs\"", in: text), "and the condition it names with it")
    }

    func testTheHireFormHasNoSystemGrantListOfItsOwn() throws {
        // The form renders whatever the caller decoded from the `system` reply. It used to carry a
        // hand-written list of six, and later a list derived from the tool catalogue — which still left
        // a grant with no tool yet out of the form.
        //
        // `Drafts.swift` *does* contain quoted `"system:…"` strings, and they are allowed: they are the
        // keys of `systemWording`, a lookup of the short label a checkbox shows beside a grant. Prose
        // cannot be derived and must be keyed by something; the point of the guard is that no such
        // string decides *which* grants are offered, which is the caller's list and nothing else.
        let text = try source("macos/Sources/AgentOrgKit/Drafts.swift")
        XCTAssertFalse(contains("SystemTools\\.", in: text),
                       "the deleted mirror is still being read")
        XCTAssertFalse(contains("static (var|let) (systemChoices|groups)\\b", in: text),
                       "the form must take the grants as an argument, not hold a list of its own")
    }

    func testTheHireFormSpellsNoLevelListAndTheProviderFormNoKindList() throws {
        // The two copies this change deletes, in the shape they took. `SetupPane.kindWording` *does*
        // contain the three kind names and they are allowed — they are the labels a tag is shown with,
        // keyed by the engine's list, the same allowance `CapabilityChoice.systemWording` takes. What
        // may not appear is a `.tag("openai")` (a kind *offered* from Swift) or a level array (a level
        // offered from Swift).
        let hire = try source("macos/Sources/AgentOrg/OrgPane.swift")
        XCTAssertFalse(contains("\\[\"junior\"", in: hire),
                       "a hand-written level list: the levels are the engine's to declare")
        let setup = try source("macos/Sources/AgentOrg/SetupPane.swift")
        for kind in ["openai", "anthropic", "ollama"] {
            XCTAssertFalse(contains("\\.tag\\(\"\(kind)\"\\)", in: setup),
                           ".tag(\"\(kind)\") offers a kind from Swift; decode the reply's `kinds`")
        }
    }
}
