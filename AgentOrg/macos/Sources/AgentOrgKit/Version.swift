//
//  Version.swift
//  AgentOrgKit
//
//  What this build is, for the diagnostics panel and the about box.
//
//  A single source of truth for the version, so a bug report can name it. The engine reports its own
//  version separately; the two are shown side by side because a mismatch between them is itself a
//  diagnosis.
//

import Foundation

public enum AgentOrgKitVersion {
    public static let version = "0.1.0"
    /// The protocol version this kit speaks. Asserted against the engine's own on first contact.
    public static let protocolVersion = Protocol.version

    /// A description suitable for a panel or a log line.
    public static func describe() -> [String: String] {
        let runtime = PythonRuntimeResolver.resolveFromEnvironment()
        return [
            "kit_version": version,
            "protocol_version": String(protocolVersion),
            "runtime": runtime.display,
            "runtime_version": runtime.version ?? "(unknown)",
        ]
    }
}
