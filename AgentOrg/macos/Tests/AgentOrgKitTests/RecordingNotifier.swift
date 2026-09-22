//
//  RecordingNotifier.swift
//  AgentOrgKitTests
//
//  A notification centre that records instead of delivering.
//
//  WHY THIS IS SHARED RATHER THAN LOCAL TO ONE SUITE
//  ------------------------------------------------
//  Two suites need it for different reasons, and both reasons are the point of the `ConsoleNotifier`
//  protocol. `ConsoleNotificationsTests` asserts the *planner* — which events are worth interrupting
//  for — with no controller in the way. `OrgControllerBridgeTests` asserts the *controller's use* of
//  the notifier: that authorisation is asked for lazily, that a denial does not break the console, and
//  that a gate the console is about to answer itself is never sent to a person as a question.
//
//  Neither is observable with `UNUserNotificationCenter`: a test process has no application bundle to
//  ask, and there is no banner to look at. So the fake is the only way these behaviours can be tested
//  at all — which is precisely why the delivery was put behind a protocol.
//

import Foundation
@testable import AgentOrgKit

/// A notifier that records what it was asked to do.
///
/// `@unchecked Sendable` with an internal lock, matching the real notifier: the protocol's methods are
/// `async` and are called from a detached task, so the mutable state has to be guarded honestly.
final class RecordingNotifier: ConsoleNotifier, @unchecked Sendable {

    private let lock = NSLock()
    private var deliveredPlans: [NotificationPlan] = []
    private var withdrawnGroups: [[String]] = []
    private var requests = 0
    private var authorized: Bool
    private var grantOnRequest: Bool
    private let available: Bool

    /// - Parameters:
    ///   - isAuthorized: whether a banner would be delivered *now*. `false` also covers "not yet asked".
    ///   - willGrant: what `requestAuthorization` answers when it is asked.
    ///   - available: whether this process could post a banner at all. Defaults to `true` — a test
    ///     process is not the state under test unless it says so, and the `false` case is the one the
    ///     controller must report as *unavailable* rather than as denied.
    init(isAuthorized: Bool = false, willGrant: Bool = true, available: Bool = true) {
        self.authorized = isAuthorized
        self.grantOnRequest = willGrant
        self.available = available
    }

    var isAvailable: Bool { available }

    /// The plans a delivery was attempted with, in order.
    var delivered: [NotificationPlan] { lock.withLock { deliveredPlans } }
    /// How many times authorisation was requested, so "lazily, once" is assertable.
    var authorizationRequests: Int { lock.withLock { requests } }

    /// Forget the deliveries, keeping the authorisation state.
    ///
    /// Used after a setup step that legitimately notifies, so the assertion can be about the event
    /// under test rather than about everything the controller did to get there.
    func reset() {
        lock.withLock { deliveredPlans.removeAll() }
    }

    /// Every identifier handed to `withdraw`, in the order it was withdrawn, as one flat list.
    ///
    /// Flat rather than grouped because almost every assertion is "was this banner taken back", and the
    /// one that is not — the whole vocabulary — reads better as a set. The *order* is kept so a caller
    /// can still tell the two withdrawals apart if it needs to.
    var withdrawn: [String] { lock.withLock { withdrawnGroups.flatMap { $0 } } }

    var isAuthorized: Bool {
        get async { lock.withLock { authorized } }
    }

    @discardableResult
    func requestAuthorization() async -> Bool {
        lock.withLock {
            requests += 1
            authorized = grantOnRequest
            return grantOnRequest
        }
    }

    @discardableResult
    func deliver(_ plan: NotificationPlan) async -> Bool {
        lock.withLock {
            guard authorized else { return false }
            deliveredPlans.append(plan)
            return true
        }
    }

    /// Record the withdrawal, and report nothing back — the protocol's own shape, because the system
    /// call returns nothing and a console decision cannot depend on removing a banner.
    func withdraw(_ identifiers: [String]) {
        lock.withLock { withdrawnGroups.append(identifiers) }
    }
}

private extension NSLock {
    func withLock<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }
}
