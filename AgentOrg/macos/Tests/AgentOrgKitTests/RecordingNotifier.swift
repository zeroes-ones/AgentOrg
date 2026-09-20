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
    private var requests = 0
    private var authorized: Bool
    private var grantOnRequest: Bool

    /// - Parameters:
    ///   - isAuthorized: whether a banner would be delivered *now*. `false` also covers "not yet asked".
    ///   - willGrant: what `requestAuthorization` answers when it is asked.
    init(isAuthorized: Bool = false, willGrant: Bool = true) {
        self.authorized = isAuthorized
        self.grantOnRequest = willGrant
    }

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
}

private extension NSLock {
    func withLock<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }
}
