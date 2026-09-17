// swift-tools-version: 5.9
//
// AgentOrg — the native macOS console for the agent organisation.
//
// WHY A PACKAGE RATHER THAN AN .xcodeproj
// ---------------------------------------
// A SwiftPM manifest is text, so it reviews and diffs like everything else in this repository, and
// `swift build` works from a terminal without opening Xcode. Xcode opens a `Package.swift` directly,
// so the same manifest serves both workflows. An `.xcodeproj` would be a binary blob that only Xcode
// can read — and it would be the one part of the project no reviewer could examine.
//
// WHY TWO TARGETS
// ---------------
// `AgentOrgKit` holds everything with no view code: the process bridge, the protocol models, the log
// store, the safe file writer. `AgentOrg` is the executable that imports it and adds SwiftUI.
//
// The split exists so the bridge can be unit-tested without a running UI. A `Process`-based bridge
// that can only be exercised by launching the app is a bridge nobody tests, and process handling is
// exactly the part that must not be guessed at.

import PackageDescription

let package = Package(
    name: "AgentOrg",
    platforms: [
        // macOS 14 is the floor because the bridge uses `@Observable`-era Swift concurrency patterns
        // and `DispatchSourceRead` with modern file-handle APIs.
        .macOS(.v14)
    ],
    products: [
        .library(name: "AgentOrgKit", targets: ["AgentOrgKit"]),
        .executable(name: "AgentOrg", targets: ["AgentOrg"]),
    ],
    targets: [
        .target(
            name: "AgentOrgKit",
            path: "Sources/AgentOrgKit"
        ),
        .executableTarget(
            name: "AgentOrg",
            dependencies: ["AgentOrgKit"],
            path: "Sources/AgentOrg"
        ),
        .testTarget(
            name: "AgentOrgKitTests",
            dependencies: ["AgentOrgKit"],
            path: "Tests/AgentOrgKitTests"
        ),
    ]
)
