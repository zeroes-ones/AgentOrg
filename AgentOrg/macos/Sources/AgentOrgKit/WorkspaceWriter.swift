//
//  WorkspaceWriter.swift
//  AgentOrgKit
//
//  Safe file manipulation for the project workspace.
//
//  WHY THIS EXISTS SEPARATELY FROM Foundation
//  ------------------------------------------
//  The app reads and writes files that the engine is *also* writing: manifests, the artifact index,
//  the checkpoint. Two things go wrong without care, and both are silent:
//
//  1. **A torn read.** The engine is mid-write when the UI reads, and the app shows half a file — which
//     parses as valid JSON surprisingly often and means nothing. Coordination and atomic replacement are
//     the fix.
//  2. **A path escape.** A name taken from a manifest, an agent id, or a model's output is *data*. If it
//     contains `..`, or resolves through a symlink to somewhere else, a "write an artifact" becomes a
//     write anywhere the user can reach. Every path here is resolved and checked against the workspace
//     root before anything touches it.
//
//  The engine has its own containment check (`engine/artifacts.py`). This one is not redundant: the app
//  writes files the engine never sees, and a check that only exists on one side of a boundary protects
//  only one side.

import Foundation

/// A file operation that was refused.
public struct WorkspaceError: Error, LocalizedError, Sendable {
    public enum Kind: String, Sendable {
        case escapesWorkspace
        case absolutePath
        case traversalSegment
        case notFound
        case readFailed
        case writeFailed
        case noSpace
        case permissionDenied
    }

    public let kind: Kind
    public let path: String
    public let message: String

    public init(_ kind: Kind, path: String, message: String) {
        self.kind = kind
        self.path = path
        self.message = message
    }

    public var errorDescription: String? { "[\(kind.rawValue)] \(path): \(message)" }
}

/// Contained, coordinated, atomic file access within one project workspace.
public final class WorkspaceWriter: @unchecked Sendable {

    /// The project root. Every path this type touches must resolve inside it.
    ///
    /// Mutable so attaching a different folder does not require building a second writer — and
    /// guarded by `lock`, because containment is decided against this value: a read that saw a
    /// half-updated root could resolve a path against the wrong workspace.
    public private(set) var root: URL

    private let fileManager = FileManager.default
    private let coordinator = NSFileCoordinator()
    private let lock = NSLock()

    public init(root: URL) {
        // Standardised once, so a later comparison is against a resolved path rather than a
        // spelled-out one with a `..` still in it.
        self.root = root.standardizedFileURL.resolvingSymlinksInPath()
    }

    /// Re-root onto another workspace, after a project change.
    ///
    /// The engine is stopped before this is called, so nothing is mid-write; the lock is here so the
    /// containment check can never observe a root that is only half-set.
    public func updateRoot(_ url: URL) {
        let resolved = url.standardizedFileURL.resolvingSymlinksInPath()
        lock.lock()
        defer { lock.unlock() }
        root = resolved
    }

    // MARK: - Containment

    /// Resolve a workspace-relative path, refusing anything that escapes.
    ///
    /// The check runs on the *resolved* path, so a symlink inside the workspace pointing outside is
    /// caught — resolving first is the difference between a real check and a cosmetic one.
    ///
    /// One subtlety is load-bearing: `resolvingSymlinksInPath()` does **not** resolve an *intermediate*
    /// symlink when the final path component does not exist. A write to `link/pwned.txt`, where `link`
    /// is a symlink out of the workspace, would therefore look contained. So each existing ancestor is
    /// resolved in turn, which is what actually catches the escape.
    ///
    /// - Throws: `WorkspaceError` naming the specific problem, because "invalid path" is not actionable
    ///   while "contains a traversal segment" is.
    public func resolve(_ relative: String, mustExist: Bool = false) throws -> URL {
        let trimmed = relative.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            throw WorkspaceError(.notFound, path: relative, message: "the path is empty")
        }
        if trimmed.hasPrefix("/") {
            throw WorkspaceError(.absolutePath, path: trimmed,
                                 message: "a workspace path must be relative")
        }
        let components = trimmed.split(separator: "/").map(String.init)
        if components.contains("..") {
            throw WorkspaceError(.traversalSegment, path: trimmed,
                                 message: "the path contains a '..' segment")
        }

        let candidate = root.appendingPathComponent(trimmed)
        let resolved = WorkspaceWriter.resolveAncestors(candidate,
                                                        stoppingAt: root,
                                                        fileManager: fileManager)

        // `pathComponents` comparison rather than a string prefix: a prefix test would accept
        // `/work/space-evil` for the root `/work/space`.
        let rootComponents = root.pathComponents
        let resolvedComponents = resolved.pathComponents
        guard resolvedComponents.count >= rootComponents.count,
              Array(resolvedComponents.prefix(rootComponents.count)) == rootComponents else {
            throw WorkspaceError(.escapesWorkspace, path: trimmed,
                                 message: "resolved to \(resolved.path), outside \(root.path)")
        }

        if mustExist, !fileManager.fileExists(atPath: resolved.path) {
            throw WorkspaceError(.notFound, path: trimmed, message: "no such file")
        }
        return resolved
    }

    /// Resolve symlinks in every ancestor that exists, walking down from `stoppingAt`.
    ///
    /// Needed because Foundation's own resolution stops at the first missing component, so an
    /// intermediate symlink pointing out of the workspace would go unnoticed. Each existing prefix is
    /// resolved in turn, and the resolved prefix is what the next component is appended to.
    static func resolveAncestors(_ path: URL, stoppingAt root: URL,
                                 fileManager: FileManager) -> URL {
        let rootComponents = root.pathComponents
        let pathComponents = path.pathComponents
        guard pathComponents.count > rootComponents.count else { return root }

        var current = root
        for index in rootComponents.count..<pathComponents.count {
            let next = current.appendingPathComponent(pathComponents[index])
            if fileManager.fileExists(atPath: next.path) {
                // Resolve this existing prefix, so a symlink here is followed rather than stepped over.
                current = next.resolvingSymlinksInPath().standardizedFileURL
            } else {
                // Beyond the first missing component nothing can be a symlink, so the rest is appended
                // literally — which is also cheaper than stat-ing each remaining component.
                var tail = current
                for remaining in index..<pathComponents.count {
                    tail.appendPathComponent(pathComponents[remaining])
                }
                return tail.standardizedFileURL
            }
        }
        return current.standardizedFileURL
    }

    /// Whether a path is inside the workspace. A convenience for a UI check that should not throw.
    public func contains(_ relative: String) -> Bool {
        (try? resolve(relative)) != nil
    }

    // MARK: - Reading

    /// Read a text file, coordinated so a concurrent engine write cannot tear it.
    ///
    /// - Parameter maxBytes: A read above this is refused rather than loaded, so an accidentally huge
    ///   artifact cannot exhaust the app's memory.
    public func readText(_ relative: String, maxBytes: Int = 16 << 20) throws -> String {
        let url = try resolve(relative, mustExist: true)
        var coordinationError: NSError?
        var result: Result<String, Error>?
        coordinator.coordinate(readingItemAt: url, options: [], error: &coordinationError) { target in
            do {
                let attributes = try self.fileManager.attributesOfItem(atPath: target.path)
                if let size = attributes[.size] as? Int, size > maxBytes {
                    throw WorkspaceError(.readFailed, path: relative,
                                         message: "the file is \(size) bytes, over the \(maxBytes) limit")
                }
                result = .success(try String(contentsOf: target, encoding: .utf8))
            } catch {
                result = .failure(error)
            }
        }
        if let coordinationError { throw coordinationError }
        switch result {
        case .success(let text): return text
        case .failure(let error): throw error
        case nil:
            throw WorkspaceError(.readFailed, path: relative, message: "the read produced no result")
        }
    }

    /// Read a JSON object.
    public func readJSON(_ relative: String) throws -> [String: Any] {
        let text = try readText(relative)
        guard let data = text.data(using: .utf8) else {
            throw WorkspaceError(.readFailed, path: relative, message: "not valid UTF-8")
        }
        do {
            guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw WorkspaceError(.readFailed, path: relative, message: "not a JSON object")
            }
            return object
        } catch let error as WorkspaceError {
            throw error
        } catch {
            throw WorkspaceError(.readFailed, path: relative,
                                 message: "invalid JSON: \(error.localizedDescription)")
        }
    }

    // MARK: - Writing

    /// Write a file atomically, coordinated against other writers.
    ///
    /// Temp-then-`replaceItemAt` is what makes the write atomic: a reader sees either the previous
    /// complete content or the new complete content, never a blend. The engine writes its checkpoint the
    /// same way, for the same reason.
    @discardableResult
    public func writeText(_ relative: String, _ text: String) throws -> URL {
        try writeData(relative, Data(text.utf8))
    }

    /// Write JSON atomically.
    @discardableResult
    public func writeJSON(_ relative: String, _ object: [String: Any], pretty: Bool = true) throws -> URL {
        var options: JSONSerialization.WritingOptions = [.sortedKeys]
        if pretty { options.insert(.prettyPrinted) }
        let data: Data
        do {
            data = try JSONSerialization.data(withJSONObject: object, options: options)
        } catch {
            throw WorkspaceError(.writeFailed, path: relative,
                                 message: "cannot serialise the object: \(error.localizedDescription)")
        }
        return try writeData(relative, data)
    }

    /// Write bytes atomically.
    ///
    /// - Throws: `WorkspaceError.noSpace` and `.permissionDenied` distinctly. The two need different
    ///   actions from the user, and a single "write failed" would hide which.
    @discardableResult
    public func writeData(_ relative: String, _ data: Data) throws -> URL {
        let target = try resolve(relative)
        let directory = target.deletingLastPathComponent()

        var coordinationError: NSError?
        var thrown: Error?
        coordinator.coordinate(writingItemAt: target, options: .forReplacing,
                               error: &coordinationError) { destination in
            do {
                try self.fileManager.createDirectory(at: directory,
                                                     withIntermediateDirectories: true)
                // A sibling temp file, so the replacement is within one filesystem and therefore atomic.
                let temp = destination.deletingLastPathComponent()
                    .appendingPathComponent(".\(destination.lastPathComponent).tmp.\(getpid())")
                try data.write(to: temp, options: .atomic)
                if self.fileManager.fileExists(atPath: destination.path) {
                    _ = try self.fileManager.replaceItemAt(destination, withItemAt: temp)
                } else {
                    try self.fileManager.moveItem(at: temp, to: destination)
                }
            } catch {
                thrown = WorkspaceWriter.classify(error, path: relative)
            }
        }
        if let coordinationError { throw coordinationError }
        if let thrown { throw thrown }
        return target
    }

    /// Delete a file, tolerating its absence.
    public func remove(_ relative: String) throws {
        let target = try resolve(relative)
        guard fileManager.fileExists(atPath: target.path) else { return }
        var coordinationError: NSError?
        var thrown: Error?
        coordinator.coordinate(writingItemAt: target, options: .forDeleting,
                               error: &coordinationError) { destination in
            do { try self.fileManager.removeItem(at: destination) }
            catch { thrown = WorkspaceWriter.classify(error, path: relative) }
        }
        if let coordinationError { throw coordinationError }
        if let thrown { throw thrown }
    }

    // MARK: - Inspection

    /// Every file under the workspace, relative and sorted.
    ///
    /// Hidden files are included because the engine's own state (`.agent_state/`) is hidden — the
    /// inspector would otherwise show an empty project.
    ///
    /// The relative path is computed with `pathComponents` rather than by stripping a string prefix.
    /// On macOS `/var` is a symlink to `/private/var`, so the enumerator returns a path whose prefix
    /// does not match the root as spelled — a string replacement would produce a mangled name.
    public func list(under subdirectory: String = ".", includeHidden: Bool = true) throws -> [String] {
        let base = try resolve(subdirectory)
        guard fileManager.fileExists(atPath: base.path) else { return [] }

        // The enumerator resolves symlinks in the path it hands back, so the root is resolved the same
        // way before a comparison is attempted.
        let canonicalRoot = root.resolvingSymlinksInPath().standardizedFileURL
        var result: [String] = []
        let keys: [URLResourceKey] = [.isRegularFileKey]
        guard let enumerator = fileManager.enumerator(at: base,
                                                      includingPropertiesForKeys: keys,
                                                      options: includeHidden ? [] : [.skipsHiddenFiles])
        else { return [] }

        for case let url as URL in enumerator {
            guard (try? url.resourceValues(forKeys: Set(keys)).isRegularFile) == true else { continue }
            if url.lastPathComponent.contains(".tmp.") { continue }  // a write in flight
            let canonical = url.resolvingSymlinksInPath().standardizedFileURL
            guard canonical.pathComponents.count > canonicalRoot.pathComponents.count,
                  Array(canonical.pathComponents.prefix(canonicalRoot.pathComponents.count))
                    == canonicalRoot.pathComponents else { continue }
            let relative = canonical.pathComponents
                .dropFirst(canonicalRoot.pathComponents.count)
                .joined(separator: "/")
            result.append(relative)
        }
        return result.sorted()
    }

    /// Whether a file exists.
    public func exists(_ relative: String) -> Bool {
        guard let url = try? resolve(relative) else { return false }
        return fileManager.fileExists(atPath: url.path)
    }

    /// A file's size, or nil.
    public func size(of relative: String) -> Int? {
        guard let url = try? resolve(relative),
              let attributes = try? fileManager.attributesOfItem(atPath: url.path) else { return nil }
        return attributes[.size] as? Int
    }

    /// The total bytes under the workspace, for the resources panel.
    public func totalBytes() -> Int {
        (try? list())?.compactMap { size(of: $0) }.reduce(0, +) ?? 0
    }

    // MARK: - Error classification

    /// Turn a Foundation error into a typed one.
    ///
    /// `ENOSPC` and `EACCES` are distinguished because the user's next action differs: free disk versus
    /// fix permissions. A single "write failed" would send them looking in the wrong place.
    static func classify(_ error: Error, path: String) -> WorkspaceError {
        let nsError = error as NSError
        if nsError.domain == NSPOSIXErrorDomain {
            switch nsError.code {
            case Int(ENOSPC):
                return WorkspaceError(.noSpace, path: path, message: "the disk is full")
            case Int(EACCES), Int(EPERM):
                return WorkspaceError(.permissionDenied, path: path,
                                      message: "permission denied")
            case Int(ENOENT):
                return WorkspaceError(.notFound, path: path, message: "no such file")
            default:
                break
            }
        }
        if nsError.domain == NSCocoaErrorDomain,
           nsError.code == NSFileWriteNoPermissionError || nsError.code == NSFileReadNoPermissionError {
            return WorkspaceError(.permissionDenied, path: path, message: "permission denied")
        }
        return WorkspaceError(.writeFailed, path: path, message: nsError.localizedDescription)
    }
}
