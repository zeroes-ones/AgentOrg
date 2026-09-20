//
//  RunStateBrowserTests.swift
//  AgentOrgKitTests
//
//  Reading `.agent_state/` with the engine down.
//
//  WHY REAL FILES AND NOT A MOCK
//  -----------------------------
//  This type's whole job is reading files an *older or newer* engine wrote. The failures it can have
//  are all file failures — a missing directory, a torn final line, a document whose shape this build
//  does not know — and a mock would confirm the code calls the calls it calls while proving nothing
//  about any of them. So each test writes the real shapes `engine/state.py`, `engine/org/handoff.py`
//  and `engine/cachestore.py` produce, and reads them back.
//
//  Containment is asserted here too, because it is the one read this type must *refuse*: the names it
//  joins come from files the engine wrote, and a browser that resolved them itself would be a read
//  anywhere the user can reach.
//

import XCTest
@testable import AgentOrgKit

final class RunStateBrowserTests: XCTestCase {

    private var root: URL!
    private var writer: WorkspaceWriter!
    private var browser: RunStateBrowser!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-browser-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        writer = WorkspaceWriter(root: root)
        browser = RunStateBrowser(writer: writer)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    // MARK: - Fixtures, in the shapes the engine writes

    /// `run_state.json`, as `engine/state.py` + the library runner produce it.
    private func writeRunState(nodes: [String: Any], phase: String = "awaiting_human",
                               node: String = "pm") throws {
        try writer.writeJSON(".agent_state/run_state.json", [
            "workflow": "console",
            "manifest_sha": "973a6bec55f5",
            "created": "2026-09-17T14:38:39Z",
            "updated": "2026-09-17T14:41:25Z",
            "node": node,
            "phase": phase,
            "iteration": 0,
            "budget": ["max_steps": 90, "steps_used": 1],
            "nodes": nodes,
        ])
    }

    /// `handoffs/<id>.json`, as `engine/org/handoff.py`'s `Handoff.as_dict` produces it.
    private func writeHandoff(id: String, state: String = "VERIFIED",
                              status: String = "done", summary: String = "implemented",
                              openQuestions: [Any] = [],
                              origin: String = "developer", target: String = "reviewer",
                              saturation: Double? = nil, window: Int? = nil,
                              createdAt: String = "2026-09-17T14:40:00Z") throws {
        // The budget block, in the shape `executor._agent_budget` writes it: the run's counters plus
        // the *session's* own saturation and window. Those two are the only per-node context figures
        // the engine persists, which is why they are what the Context section reads.
        var budget: [String: Any] = ["tokens": 4100, "steps_used": 3]
        if let saturation { budget["session_saturation"] = saturation }
        if let window { budget["context_window"] = window }
        try writer.writeJSON(".agent_state/handoffs/\(id).json", [
            "handoff_version": "1.0.0",
            "handoff_id": id,
            "kind": "handoff",
            "origin": origin,
            "target": target,
            "state": state,
            "attempt": 1,
            "checksum": String(repeating: "c", count: 64),
            "created_at": createdAt,
            "supersedes": NSNull(),
            "payload": [
                "status": status,
                "summary": summary,
                "artifacts": [["path": "src/app.py", "sha256": "a"]],
                "decisions": [],
                "open_questions": openQuestions,
                "verification_evidence": [],
                "context": [:],
                "budget": budget,
                "next": "review the edge case",
            ],
        ])
    }

    /// `cache/prefix/<hash>.json`, as `engine/cachestore.py`'s `PrefixRecord.as_dict` produces it.
    private func writePrefix(hash: String, skill: String = "code-reviewer") throws {
        try writer.writeJSON(".agent_state/cache/prefix/\(hash).json", [
            "prefix_hash": hash,
            "skill": skill,
            "tool_names": ["read_file", "write_file"],
            "chars": 18_400,
            "first_seen": "2026-09-17T14:00:00Z",
            "last_seen": "2026-09-17T14:40:00Z",
            "observations": 7,
            "runs": ["run_1789655081_console"],
            "last_touch": 12,
            "source": "pin",
        ])
    }

    // MARK: - Absence is a normal answer

    func testAFreshWorkspaceReportsNothingRatherThanFailing() {
        // The state directory does not exist. Every accessor must answer "nothing", because a fresh
        // workspace is the normal first-run state and an error dialog for it would be wrong.
        XCTAssertNil(browser.lastRun())
        XCTAssertTrue(browser.handoffs().isEmpty)
        XCTAssertTrue(browser.prefixes().isEmpty)
        XCTAssertTrue(browser.cacheStream(.shapes, limit: 10).isEmpty)
        XCTAssertEqual(browser.stateDirectoryProblem(), nil)
    }

    // MARK: - The run checkpoint

    func testReadsTheRunCheckpointIncludingEveryNode() throws {
        try writeRunState(nodes: [
            "pm": ["status": "blocked", "verdict": "guardrail-blocked", "iterations": 1],
            "developer": ["status": "done", "verdict": "passed", "iterations": 2],
            "reviewer": ["status": "pending", "iterations": 0],
        ])
        let run = try XCTUnwrap(browser.lastRun())
        XCTAssertEqual(run.workflow, "console")
        XCTAssertEqual(run.phase, "awaiting_human")
        XCTAssertEqual(run.node, "pm")
        XCTAssertEqual(run.nodes.map(\.name), ["developer", "pm", "reviewer"],
                       "nodes are listed in name order so the table does not reshuffle")
        // The one thing a person scanning the list needs: which nodes are actually stuck. The set is
        // the engine's own — `engine/flow.py::_counts`, the same one the live board counts — so the
        // list and the board cannot disagree about a run they both describe.
        XCTAssertEqual(run.blockedCount, 1)
        XCTAssertTrue(run.detail.contains("1 blocked"), run.detail)
    }

    func testTheStuckCountFollowsTheStatusAndNotTheVerdict() throws {
        // Two hazards, both real:
        //
        // * A step stopped by something other than the guardrail — its completion contract
        //   (`needs_review`) or a human gate (`awaiting_owner`) — used to read "0 blocked" here while
        //   the board read "1 stuck", so the list and the board answered one question two ways.
        // * A verdict outlives the status it described. The orchestrator's own `_detect_gate` records
        //   that a released gate keeps its `awaiting_owner` verdict after its status becomes `done`, so
        //   a verdict-keyed count would report a node that has already moved on as still stuck.
        try writeRunState(nodes: [
            "pm": ["status": "needs_review", "verdict": "contract-violation", "iterations": 2],
            "architect": ["status": "done", "verdict": "awaiting_owner", "iterations": 1],
            "api": ["status": "running", "iterations": 1],
        ])
        let run = try XCTUnwrap(browser.lastRun())
        XCTAssertEqual(run.blockedCount, 1, "the contract-stopped step counts; the released gate does not")
    }

    func testAStaleCheckpointWithoutAWorkflowIsNotOfferedAsARun() throws {
        // The library runner and the orchestrator once wrote different shapes to this filename. A
        // document without `workflow` is not a run this build understands, and inventing a row for it
        // would put a fabricated entry in the history list.
        try writer.writeJSON(".agent_state/run_state.json", ["run_id": "run_x", "phase": "ready"])
        XCTAssertNil(browser.lastRun())
    }

    func testACorruptCheckpointDoesNotThrowOutOfTheBrowser() throws {
        try writer.writeText(".agent_state/run_state.json", "{not json at all")
        XCTAssertNil(browser.lastRun(), "a corrupt file must read as no run, not as a crash")
    }

    // MARK: - Handoffs

    func testReadsEveryHandoffWithItsTypedPayload() throws {
        try writeHandoff(id: "ho_4f21ac")
        let handoffs = browser.handoffs()
        XCTAssertEqual(handoffs.count, 1)
        let handoff = try XCTUnwrap(handoffs.first)
        XCTAssertEqual(handoff.id, "ho_4f21ac")
        XCTAssertEqual(handoff.origin, "developer")
        XCTAssertEqual(handoff.target, "reviewer")
        XCTAssertEqual(handoff.state, "VERIFIED")
        XCTAssertEqual(handoff.summary, "implemented")
        // The artifact may be an object with a `path` or a bare string on the wire; both resolve to
        // the path, because that is what a person needs to open.
        XCTAssertEqual(handoff.artifacts, ["src/app.py"])
        XCTAssertFalse(handoff.needsAttention)
    }

    func testAHandoffThatDidNotCompleteIsFlagged() throws {
        // The states that mean the crossing failed are the ones worth a person's attention, and the
        // browser says so rather than leaving it to be read off a word in a table.
        try writeHandoff(id: "ho_9c02be", state: "REJECTED", status: "needs_review",
                         summary: "handoff propose refused: R6: 4 open questions")
        try writeHandoff(id: "ho_11dd07", state: "FULFILLED")
        let handoffs = browser.handoffs()
        let rejected = try XCTUnwrap(handoffs.first { $0.id == "ho_9c02be" })
        XCTAssertTrue(rejected.needsAttention)
        XCTAssertFalse(handoffs.first { $0.id == "ho_11dd07" }?.needsAttention ?? true)
    }

    func testOpenQuestionsSurviveBothWireShapes() throws {
        // R6 exists because unresolved questions compound, so showing the pile a successor inherited is
        // the point. The engine writes them as objects with a `question` key; older records are bare
        // strings, and both must read.
        try writeHandoff(id: "ho_q", openQuestions: [
            ["question": "which pagination cursor?"],
            "does the index need a migration?",
        ])
        let handoff = try XCTUnwrap(browser.handoffs().first)
        XCTAssertEqual(handoff.openQuestions,
                       ["which pagination cursor?", "does the index need a migration?"])
    }

    func testHandoffsAreNewestFirst() throws {
        try writeHandoff(id: "ho_older")
        try writer.writeJSON(".agent_state/handoffs/ho_newer.json", [
            "handoff_id": "ho_newer", "origin": "qa", "target": "pm", "state": "PROPOSED",
            "created_at": "2026-09-17T15:00:00Z", "payload": ["status": "done", "summary": "later"],
        ])
        XCTAssertEqual(browser.handoffs().map(\.id), ["ho_newer", "ho_older"])
    }

    func testAHandoffWithNoIdIsKeyedByItsFilenameRatherThanDropped() throws {
        // The record is still real — losing it from the browser would hide a crossing that happened.
        try writer.writeJSON(".agent_state/handoffs/orphan.json", [
            "origin": "developer", "target": "qa", "state": "PROPOSED",
            "payload": ["status": "done", "summary": "no id written"],
        ])
        let handoff = try XCTUnwrap(browser.handoffs().first)
        XCTAssertEqual(handoff.id, "orphan")
        XCTAssertEqual(handoff.summary, "no id written")
    }

    // MARK: - The session archive and the context figure

    func testTheContextFigureIsReadFromTheRealBudgetShape() throws {
        // The whole Context feature depends on this one read: the engine writes `session_saturation`
        // and `context_window` into `payload.budget` at each node boundary, and that is the only
        // per-node context figure it persists. If the shape here ever drifts, the panel silently
        // returns to showing a legend and no number — which is the bug this replaced.
        try writeHandoff(id: "ho_ctx", origin: "developer", target: "reviewer",
                         saturation: 0.88, window: 32_768)
        let handoff = try XCTUnwrap(browser.handoffs().first)
        XCTAssertTrue(handoff.hasContextReading)
        XCTAssertEqual(handoff.sessionSaturation ?? 0, 0.88, accuracy: 1e-9)
        XCTAssertEqual(handoff.contextWindow, 32_768)

        let reading = try XCTUnwrap(ContextReading.latestPerNode(from: browser.handoffs()).first)
        XCTAssertEqual(reading.node, "developer")
        XCTAssertEqual(reading.band, .critical)
        XCTAssertEqual(reading.label, "88%")
    }

    func testAHandoffWrittenBeforeTheFieldExistedReportsNoContext() throws {
        // An older engine's document has no session fields at all — which must read as "not recorded",
        // never as an empty session. This is the case a naive `?? 0.0` would turn into a healthy bar.
        try writeHandoff(id: "ho_old")
        let handoff = try XCTUnwrap(browser.handoffs().first)
        XCTAssertFalse(handoff.hasContextReading)
        XCTAssertNil(handoff.sessionSaturation)
        XCTAssertTrue(ContextReading.latestPerNode(from: browser.handoffs()).isEmpty)
    }

    func testSessionsAreDiscoveredPerAgentAndContainmentIsRespected() throws {
        // `session list`, in the GUI. The engine archives `sessions/<agent>/<session>/…` when it
        // rotates a context, so an empty result is a real answer — "nothing has rotated here yet".
        try writer.writeText(".agent_state/sessions/ag_7f3a/ses_001/transcript.json", "{}")
        try writer.writeText(".agent_state/sessions/ag_7f3a/ses_001/summary.json", "{}")
        try writer.writeText(".agent_state/sessions/ag_9c1d/ses_002/transcript.json", "{}")
        let sessions = browser.sessions()
        XCTAssertEqual(sessions.count, 2, "one row per agent/session pair")
        let first = try XCTUnwrap(sessions.first { $0.session == "ses_001" })
        XCTAssertEqual(first.agent, "ag_7f3a")
        XCTAssertEqual(first.files, 2)
        XCTAssertGreaterThan(first.bytes, 0)
    }

    func testAFreshWorkspaceHasNoSessionsRatherThanAFailure() {
        XCTAssertTrue(browser.sessions().isEmpty)
    }

    // MARK: - The cache store

    func testReadsPinnedPrefixes() throws {
        try writePrefix(hash: "8d38fde2f8f4d647")
        let prefix = try XCTUnwrap(browser.prefixes().first)
        XCTAssertEqual(prefix.id, "8d38fde2f8f4d647")
        XCTAssertEqual(prefix.skill, "code-reviewer")
        XCTAssertEqual(prefix.observations, 7)
        XCTAssertEqual(prefix.source, "pin")
        XCTAssertEqual(prefix.tools, ["read_file", "write_file"])
    }

    func testReadsAJsonlStreamAndSkipsATornFinalLine() throws {
        // The normal result of a kill mid-write. A reader that threw on it would make the cache panel
        // useless in exactly the situation it exists for: a run that died.
        let lines = [
            #"{"turn":1,"at":"2026-09-17T14:00:00Z","shape_hash":"aa"}"#,
            #"{"turn":2,"at":"2026-09-17T14:01:00Z","shape_hash":"bb"}"#,
            #"{"turn":3,"at":"2026-09-17T14:0"#,
        ]
        try writer.writeText(".agent_state/cache/shapes.jsonl", lines.joined(separator: "\n") + "\n")
        let records = browser.cacheStream(.shapes, limit: 10)
        XCTAssertEqual(records.count, 2, "the torn line must be skipped, not fatal")
        XCTAssertEqual(records.last?["turn"], .int(2))
    }

    func testAJsonlTailKeepsTheNewestRecords() throws {
        let lines = (1...50).map { #"{"turn":\#($0)}"# }.joined(separator: "\n")
        try writer.writeText(".agent_state/cache/savings.jsonl", lines + "\n")
        let records = browser.cacheStream(.savings, limit: 3)
        XCTAssertEqual(records.map { $0["turn"]?.intValue }, [48, 49, 50])
    }

    func testTheTwoCacheStreamsAreReadFromTheirOwnFiles() throws {
        try writer.writeText(".agent_state/cache/shapes.jsonl", #"{"kind":"shape"}"# + "\n")
        try writer.writeText(".agent_state/cache/savings.jsonl", #"{"kind":"saving"}"# + "\n")
        XCTAssertEqual(browser.cacheStream(.shapes).first?["kind"], .string("shape"))
        XCTAssertEqual(browser.cacheStream(.savings).first?["kind"], .string("saving"))
    }

    // MARK: - Containment

    func testAHostileHandoffFilenameEscapesNothing() throws {
        // The names this type joins come from files the engine wrote, which is *data*. A browser that
        // built the path itself would read anywhere the user can reach; going through
        // `WorkspaceWriter` means the same containment check the rest of the app relies on applies.
        let outside = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-browser-outside-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: outside) }
        let secret = outside.appendingPathComponent("secret.json")
        try Data(#"{"handoff_id":"stolen"}"#.utf8).write(to: secret)

        // A traversal name, exactly as a compromised or buggy writer would produce it.
        XCTAssertThrowsError(try writer.readJSON(".agent_state/handoffs/../../secret.json"))
        XCTAssertNil(browser.handoffs().first { $0.id == "stolen" })
    }

    func testAStateDirectorySymlinkedOutOfTheWorkspaceIsReported() throws {
        // Empty and *refused* look identical in a list, and only one of them is a state the user must
        // be told about. This is the case the writer exists to catch.
        let outside = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-browser-link-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: outside) }
        try FileManager.default.createSymbolicLink(
            at: root.appendingPathComponent(".agent_state"), withDestinationURL: outside)

        XCTAssertNotNil(browser.stateDirectoryProblem(),
                        "a state directory resolving outside the workspace must be reported")
    }

    func testAHealthyWorkspaceReportsNoProblem() throws {
        try writeRunState(nodes: ["pm": ["status": "done", "iterations": 1]])
        XCTAssertNil(browser.stateDirectoryProblem())
    }

    // MARK: - The goal, offline

    func testReadsTheGoalDocumentFromDisk() throws {
        // The goal is what says whether a stopped run *meant* to stop: a paused goal and an abandoned
        // run look identical in the checkpoint alone.
        try writer.writeJSON(".agent_state/goal.json", [
            "goal_version": "1.0.0",
            "objective": "add cursor pagination",
            "state": "paused",
            "pause_reason": "restored",
            "policy": ["posture": "unattended"],
        ])
        let goal = browser.goal()
        XCTAssertEqual(goal["objective"], .string("add cursor pagination"))
        XCTAssertEqual(goal["pause_reason"], .string("restored"))
        XCTAssertEqual(goal["policy"]?["posture"], .string("unattended"))
    }

    func testNoGoalDocumentReadsAsEmpty() {
        XCTAssertTrue(browser.goal().isEmpty)
    }

    // MARK: - The jsonl helper

    func testJsonlTailIgnoresBlankLinesAndNonObjects() {
        let text = """
        {"a":1}

        [1,2,3]
        not json
        {"b":2}
        """
        let records = RunStateBrowser.jsonlTail(text, limit: 10)
        XCTAssertEqual(records.count, 2)
        XCTAssertEqual(records.map { $0["a"] ?? $0["b"] }, [.int(1), .int(2)])
    }

    func testJSONValueFromAnyHandlesEveryJSONShape() {
        // The bridge between `WorkspaceWriter.readJSON`'s `[String: Any]` and the app's `JSONValue`.
        XCTAssertEqual(JSONValue.fromAny("x"), .string("x"))
        XCTAssertEqual(JSONValue.fromAny(true), .bool(true))
        XCTAssertEqual(JSONValue.fromAny(3), .int(3))
        XCTAssertEqual(JSONValue.fromAny(NSNull()), .null)
        XCTAssertEqual(JSONValue.fromAny([1, "a"]), .array([.int(1), .string("a")]))
        XCTAssertEqual(JSONValue.fromAny(["k": 1]), .object(["k": .int(1)]))
        XCTAssertNil(JSONValue.fromAny(Date()))
    }
}
