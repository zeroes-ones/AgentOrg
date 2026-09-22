//
//  PreferenceStore.swift
//  AgentOrgKit
//
//  Where a preference lives, as a type — so "no persistence" can be true rather than asserted.
//
//  WHY THIS IS A PROTOCOL AND NOT `UserDefaults` ITSELF
//  ----------------------------------------------------
//  `AppPreferences` and the controller's dismissal list were both parameterised on `UserDefaults`, and
//  the throwaway store both of them offered — `AppPreferences.ephemeral()` — was a
//  `UserDefaults(suiteName: "org.agentorg.ephemeral.<UUID>")`. That is a *persistent* store: a suite
//  name that has never been registered still becomes a real plist in `~/Library/Preferences` on the
//  first write, and `UserDefaults` does not sandbox itself to a temporary directory. Previews and tests
//  used it hundreds of times, so the machine that ran them accumulated hundreds of
//  `org.agentorg.ephemeral.<UUID>.plist` files — one per run, none of them this app's own domain
//  (`dev.agentorg.console`).
//
//  Two things ruled out the obvious repairs:
//
//  - **A temporary `HOME`.** `UserDefaults` resolves its directory from the account database, not from
//    `$HOME`. Measured: with `HOME` pointed at a throwaway directory, `NSHomeDirectory()` and the
//    written plist were both still under the real home, so a test that set `HOME` would be writing
//    somewhere else in name only.
//  - **`removePersistentDomain(forName:)` in teardown.** It clears the *keys*; the file stays. Measured,
//    the plist survives at 42 bytes holding an empty dictionary — which is why the real preferences
//    directory has several hundred 42-byte `org.agentorg.tests.<UUID>.plist` files that a teardown was
//    already calling `removePersistentDomain` on.
//
//  So the store is what gets injected — the way this repo already injects a root, a credentials path
//  and a notifier — and the store a preview or a test is handed keeps its values in a dictionary, where
//  there is nothing to write and therefore nothing left behind.
//

import Foundation

/// The slice of `UserDefaults` these types use.
///
/// Deliberately this small: it is the whole of what `AppPreferences` and the controller's dismissal
/// list ask of a store, so an implementation has nothing to emulate beyond it.
public protocol PreferenceStore: AnyObject {
    func string(forKey key: String) -> String?
    func stringArray(forKey key: String) -> [String]?
    func bool(forKey key: String) -> Bool
    func set(_ value: Any?, forKey key: String)
    func removeObject(forKey key: String)
}

/// The real thing. `UserDefaults` already answers every one of these, so this adds no behaviour.
extension UserDefaults: PreferenceStore {}

/// A store that exists only in memory.
///
/// Nothing it holds reaches a plist, a daemon or the disk, which is the property its callers depend on:
/// a SwiftUI preview and a test both need `AppPreferences`, and neither should leave anything on the
/// machine that ran them.
///
/// `bool(forKey:)` answers for exactly the values this app stores — the three wizard answers, all
/// written as `Bool` — and `false` otherwise, as an absent key does.
public final class InMemoryPreferenceStore: PreferenceStore {

    private var values: [String: Any] = [:]

    public init() {}

    public func string(forKey key: String) -> String? { values[key] as? String }

    public func stringArray(forKey key: String) -> [String]? { values[key] as? [String] }

    public func bool(forKey key: String) -> Bool { values[key] as? Bool ?? false }

    public func set(_ value: Any?, forKey key: String) {
        if let value {
            values[key] = value
        } else {
            values.removeValue(forKey: key)
        }
    }

    public func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}
