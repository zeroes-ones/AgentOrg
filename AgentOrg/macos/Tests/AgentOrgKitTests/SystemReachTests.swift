//
//  SystemReachTests.swift
//  AgentOrgKitTests
//
//  The two readings of one capability set: what an agent holds, and what the person can use.
//
//  WHY THIS FILE IS HERMETIC
//  -------------------------
//  `SystemPanelTests` launches the real engine for the assertions that need a live reply, and rightly
//  so. These are the assertions that must hold *whatever* the machine is set to — the split between
//  "held by an agent" and "held by nobody", and the decoding of the acting holder — so they are built
//  from payloads written here. That is also the only way to cover the holder branch at all: the engine
//  the app is built against does **not** send `holder` today (see the gap note in
//  `SystemCapabilities.swift`), so a test against the live engine would skip the branch it is meant to
//  pin. Nothing here launches a process, reads a file, or touches a switch.
//

import XCTest
@testable import AgentOrgKit

final class SystemReachTests: XCTestCase {

    // MARK: - Fixtures

    private func capability(_ grant: String, title: String? = nil) -> SystemCapability {
        SystemCapability(payload: [
            "grant": .string(grant),
            "title": .string(title ?? grant),
            "reaches": .string("what it reaches"),
            "changes": .string(""),
            "caution": .string(""),
            "available": .bool(true),
        ])!
    }

    /// Three capabilities and a roster that holds one of them — the shape the panel draws from.
    private let three = ["system:state", "system:media", "system:open"]

    private func decoded(_ grants: [String] = []) -> [SystemCapability] {
        (grants.isEmpty ? three : grants).map { capability($0) }
    }

    private func agent(_ name: String, _ capabilities: [String]) -> [String: JSONValue] {
        ["id": .string(name.lowercased()), "name": .string(name),
         "capabilities": .array(capabilities.map { .string($0) })]
    }

    /// A `system` reply as the CLI's own holder block spells it (`systemcli.Holder.as_dict`).
    private func holderPayload(_ grants: [String], name: String = "Owner",
                               id: String = "ag_owner") -> [String: JSONValue] {
        ["holder": .object([
            "id": .string(id),
            "name": .string(name),
            "grants": .array(grants.map { .string($0) }),
            "why": .string("the console's own holder — a command you type is the grant"),
        ])]
    }

    // MARK: - The split behind the first figure

    func testTheSplitNamesBothHalvesInTheEnginesOrder() {
        // The figure the header draws counts the first list; the second is the actionable one, and the
        // two must partition the declared set — a capability in neither or in both would make the count
        // disagree with the list it opens onto.
        let reach = SystemReach(capabilities: decoded(),
                                roster: [agent("UX", ["system:state", "read:*"])],
                                system: [:])
        XCTAssertEqual(reach.heldByAnAgent.map(\.grant), ["system:state"])
        XCTAssertEqual(reach.heldByNobody.map(\.grant), ["system:media", "system:open"],
                       "the engine's order is kept, and nothing is lost")
        XCTAssertEqual(reach.heldByAnAgent.count + reach.heldByNobody.count, reach.capabilities.count)
    }

    func testEveryAgentHoldingAGrantIsNamedNotCounted() {
        // "Who can already do this" is the question the figure raises, and a count does not answer it.
        // Sorted, because a roster that re-orders between polls must not look like a change.
        let reach = SystemReach(capabilities: decoded(),
                                roster: [agent("Ravi", ["system:media"]),
                                         agent("Alice", ["system:media"])],
                                system: [:])
        XCTAssertEqual(reach.holders(of: "system:media"), ["Alice", "Ravi"])
        XCTAssertEqual(reach.holders(of: "system:state"), [], "nobody is an answer, not a missing key")
        XCTAssertEqual(reach.holders(of: "system:notify"), [],
                       "an undeclared grant reads empty rather than trapping")
    }

    func testAnEmptyRosterMeansNothingIsHeldRatherThanEverything() {
        // The failure this guards: a split that defaulted the other way would draw "0 of 12" as
        // "everyone may" on a fresh machine.
        let reach = SystemReach(capabilities: decoded(), roster: [], system: [:])
        XCTAssertEqual(reach.heldByAnAgent.count, 0)
        XCTAssertEqual(reach.heldByNobody.count, 3)
        XCTAssertTrue(reach.capabilities.allSatisfy { reach.holders(of: $0.grant).isEmpty })
    }

    // MARK: - The acting holder

    func testTheOwnersReachComesFromTheReplyAndIsCountedByTheSameRule() throws {
        // The Owner is reported as holding `system:*`, which reaches every declared capability — the
        // sentence "you can use all twelve" is this assertion and not a constant in the view. The count
        // is derived here exactly as it is for an agent: same rule, one implementation
        // (`SystemCapability.reached(_:by:)`).
        let reach = SystemReach(capabilities: decoded(),
                                roster: [agent("UX", ["system:state"])],
                                system: holderPayload(["system:*"]))
        let owner = try XCTUnwrap(reach.owner)
        XCTAssertEqual(owner.id, "ag_owner")
        XCTAssertEqual(owner.name, "Owner")
        XCTAssertEqual(owner.grants, ["system:*"])
        XCTAssertFalse(owner.why.isEmpty, "the engine's own sentence travels with it")
        XCTAssertEqual(reach.ownerReaches.count, 3)
        XCTAssertEqual(reach.ownerHasEverything, true)
    }

    func testAPartialHolderReachesOnlyWhatItsGrantsMatch() {
        // A holder named in the reply with an exact grant is not the wildcard: the two figures move
        // independently, which is what stops the panel printing one number for two subjects.
        let reach = SystemReach(capabilities: decoded(),
                                roster: [agent("Sana", ["system:state", "system:media"])],
                                system: holderPayload(["system:media"], name: "Sana", id: "ag_sana"))
        XCTAssertEqual(reach.ownerReaches.map(\.grant), ["system:media"])
        XCTAssertEqual(reach.ownerHasEverything, false)
        XCTAssertEqual(reach.heldByAnAgent.count, 2)
    }

    func testAHolderWithNoGrantsReachesNothingAndSaysSo() {
        // The distinction `ownerHasEverything` is optional for: a holder reported with an empty grant
        // list is a `false` — the person holds nothing — and must not be confused with the state below.
        let reach = SystemReach(capabilities: decoded(), roster: [], system: holderPayload([]))
        XCTAssertEqual(reach.ownerReaches, [])
        XCTAssertEqual(reach.ownerHasEverything, false)
    }

    func testNoHolderInTheReplyIsNilRatherThanZero() {
        // What today's engine produces, measured: `serve._cmd_system` returns `syscap.console_payload`
        // and nothing else, so there is no `holder` key. Nil is "the engine did not say", and the panel
        // prints a sentence about the missing field instead of a figure — collapsing this into `false`
        // would turn a missing field into a claim that the person holds nothing.
        let reach = SystemReach(capabilities: decoded(), roster: [agent("UX", ["system:state"])],
                                system: ["enabled": .bool(true), "full_access": .bool(true)])
        XCTAssertNil(reach.owner)
        XCTAssertEqual(reach.ownerReaches, [])
        XCTAssertNil(reach.ownerHasEverything)
    }

    func testAHolderEntryWithoutAnIdIsIgnoredRatherThanRenderedNameless() {
        // Same rule the capability decoder follows: a row keyed on nothing is not a row. A nameless
        // holder would draw "can use 3 of 3" with an empty subject.
        let reach = SystemReach(capabilities: decoded(), roster: [], system: [
            "holder": .object(["name": .string("Owner"),
                               "grants": .array([.string("system:*")])]),
        ])
        XCTAssertNil(reach.owner)
    }

    // MARK: - One rule, two shapes

    func testTheExactOrWildcardRuleIsTheSameForAnAgentAndForTheActingHolder() {
        // The rule is `ToolRegistry._granted_scoped`: an exact match or `system:*`, and **no prefix
        // matching** — a prefix rule would let `system:state` reach `system:stateful`. Asserted here for
        // the grant-list shape, and crossed against the roster shape, because the two figures in the
        // header are computed from these two calls and must not disagree about one grant.
        XCTAssertTrue(SystemCapability.reached("system:state", by: ["system:state"]))
        XCTAssertTrue(SystemCapability.reached("system:media", by: ["system:*"]))
        XCTAssertFalse(SystemCapability.reached("system:state", by: ["system:stateful"]))
        XCTAssertFalse(SystemCapability.reached("system:state", by: []))
        XCTAssertEqual(SystemCapability.reached("system:open", by: ["system:open"]),
                       SystemCapability.holds("system:open", agent: agent("Sam", ["system:open"])))
        XCTAssertEqual(SystemCapability.reached("system:open", by: ["system:opening"]),
                       SystemCapability.holds("system:open", agent: agent("Sam", ["system:opening"])))
    }

    func testARosterEntryHoldingTheWildcardIsHeldByAnAgent() {
        // The other side of the same rule: an agent granted `system:*` in the roster holds everything,
        // and the split has to say so — otherwise the figure would under-count exactly the machine where
        // someone handed an agent the whole set.
        let reach = SystemReach(capabilities: decoded(), roster: [agent("Ravi", ["system:*"])],
                                system: [:])
        XCTAssertEqual(reach.heldByAnAgent.count, 3)
        XCTAssertEqual(reach.heldByNobody.count, 0)
        XCTAssertEqual(reach.holders(of: "system:open"), ["Ravi"])
    }
}
