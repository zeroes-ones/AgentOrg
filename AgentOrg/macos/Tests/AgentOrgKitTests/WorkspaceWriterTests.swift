//
//  WorkspaceWriterTests.swift
//  AgentOrgKitTests
//
//  Containment and atomicity, because both fail silently.
//
//  A path escape is a write anywhere the user can reach, produced from *data* — an agent id, a model's
//  filename, a manifest field. A non-atomic write produces a file that parses as valid JSON and means
//  nothing.
//

import XCTest
@testable import AgentOrgKit

final class WorkspaceWriterTests: XCTestCase {

    private var root: URL!
    private var writer: WorkspaceWriter!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-ws-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        writer = WorkspaceWriter(root: root)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    // MARK: - Containment

    func testAcceptsAWorkspaceRelativePath() throws {
        let url = try writer.resolve("docs/prd.md")
        XCTAssertTrue(url.path.hasPrefix(root.path))
    }

    func testRefusesATraversalSegment() {
        XCTAssertThrowsError(try writer.resolve("../escape.txt")) { error in
            guard let workspaceError = error as? WorkspaceError else { return XCTFail("wrong error") }
            XCTAssertEqual(workspaceError.kind, .traversalSegment,
                           "a '..' must be named as the problem, not reported generically")
        }
    }

    func testRefusesANestedTraversal() {
        XCTAssertThrowsError(try writer.resolve("docs/../../escape.txt"))
    }

    func testRefusesAnAbsolutePath() {
        XCTAssertThrowsError(try writer.resolve("/etc/passwd")) { error in
            XCTAssertEqual((error as? WorkspaceError)?.kind, .absolutePath)
        }
    }

    func testRefusesASymlinkThatEscapes() throws {
        // The case a prefix-only check misses: the textual path is inside, the resolved one is not.
        let outside = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-outside-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: outside) }

        let link = root.appendingPathComponent("link")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: outside)

        XCTAssertThrowsError(try writer.resolve("link/pwned.txt")) { error in
            XCTAssertEqual((error as? WorkspaceError)?.kind, .escapesWorkspace)
        }
    }

    func testRefusesASiblingDirectoryWithACommonPrefix() throws {
        // `/tmp/ws-evil` must not be accepted for the root `/tmp/ws`: a string prefix would.
        let sibling = URL(fileURLWithPath: root.path + "-evil")
        try FileManager.default.createDirectory(at: sibling, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: sibling) }

        XCTAssertThrowsError(try writer.resolve("../\(root.lastPathComponent)-evil/x.txt"))
    }

    func testContainsReportsWithoutThrowing() {
        XCTAssertTrue(writer.contains("docs/prd.md"))
        XCTAssertFalse(writer.contains("../escape.txt"))
    }

    // MARK: - Writing

    func testWritesAndReadsBack() throws {
        try writer.writeText("docs/prd.md", "# PRD\n\nRequirements.")
        XCTAssertEqual(try writer.readText("docs/prd.md"), "# PRD\n\nRequirements.")
    }

    func testCreatesIntermediateDirectories() throws {
        try writer.writeText("docs/nested/deep/spec.md", "content")
        XCTAssertTrue(writer.exists("docs/nested/deep/spec.md"))
    }

    func testOverwritesAtomicallyLeavingNoTempFile() throws {
        try writer.writeText("src/app.py", "first")
        try writer.writeText("src/app.py", "second")
        XCTAssertEqual(try writer.readText("src/app.py"), "second")

        // A leftover temp file would show up in the inspector as a real artifact.
        let leftovers = try writer.list().filter { $0.contains(".tmp.") }
        XCTAssertTrue(leftovers.isEmpty, "an atomic write must leave no temp file: \(leftovers)")
    }

    func testWritesAndReadsJSON() throws {
        try writer.writeJSON("run_state.json", ["status": "running", "node": "fixer"])
        let read = try writer.readJSON("run_state.json")
        XCTAssertEqual(read["status"] as? String, "running")
        XCTAssertEqual(read["node"] as? String, "fixer")
    }

    func testRefusesToWriteOutsideTheWorkspace() {
        XCTAssertThrowsError(try writer.writeText("../escaped.txt", "evil"))
    }

    func testReportsInvalidJSONDistinctly() throws {
        try writer.writeText("broken.json", "{not json")
        XCTAssertThrowsError(try writer.readJSON("broken.json")) { error in
            XCTAssertEqual((error as? WorkspaceError)?.kind, .readFailed)
        }
    }

    func testReportsAMissingFileDistinctly() {
        XCTAssertThrowsError(try writer.readText("absent.txt")) { error in
            XCTAssertEqual((error as? WorkspaceError)?.kind, .notFound)
        }
    }

    func testRemoveToleratesAnAbsentFile() throws {
        XCTAssertNoThrow(try writer.remove("never-existed.txt"))
    }

    func testRemoveDeletesAFile() throws {
        try writer.writeText("temp.txt", "x")
        try writer.remove("temp.txt")
        XCTAssertFalse(writer.exists("temp.txt"))
    }

    // MARK: - Inspection

    func testListsFilesIncludingHiddenState() throws {
        try writer.writeText("docs/prd.md", "x")
        try writer.writeText(".agent_state/run_state.json", "{}")
        let files = try writer.list()
        // The engine's state is hidden; excluding it would show an empty inspector.
        XCTAssertTrue(files.contains { $0.contains("prd.md") }, "\(files)")
        XCTAssertTrue(files.contains { $0.contains("run_state.json") }, "\(files)")
    }

    func testListExcludesInFlightTempFiles() throws {
        try writer.writeText("docs/prd.md", "x")
        try Data("partial".utf8).write(to: root.appendingPathComponent(".prd.md.tmp.999"))
        let files = try writer.list()
        XCTAssertFalse(files.contains { $0.contains(".tmp.") }, "\(files)")
    }

    func testReportsFileSizeAndTotal() throws {
        try writer.writeText("a.txt", "12345")
        XCTAssertEqual(writer.size(of: "a.txt"), 5)
        XCTAssertGreaterThan(writer.totalBytes(), 0)
        XCTAssertNil(writer.size(of: "absent.txt"))
    }

    // MARK: - Re-rooting

    func testUpdateRootMovesContainmentToTheNewWorkspace() throws {
        // Attaching another folder must move the boundary with it: a writer still rooted at the old
        // workspace would resolve the new project's paths as escapes.
        let other = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-ws-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: other, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: other) }

        writer.updateRoot(other)
        XCTAssertEqual(writer.root.path, other.resolvingSymlinksInPath().path)
        let url = try writer.resolve("src/app.py")
        XCTAssertTrue(url.path.hasPrefix(other.resolvingSymlinksInPath().path))
    }

    func testUpdateRootStillRefusesAnEscape() throws {
        let other = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentorg-ws-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: other, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: other) }

        writer.updateRoot(other)
        XCTAssertThrowsError(try writer.resolve("../escape.txt"))
    }
}
