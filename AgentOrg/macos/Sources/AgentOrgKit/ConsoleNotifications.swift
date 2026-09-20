//
//  ConsoleNotifications.swift
//  AgentOrgKit
//
//  When the app is allowed to interrupt a person.
//
//  WHY THE DECISION IS SEPARATE FROM THE DELIVERY
//  ----------------------------------------------
//  A notification has two halves that fail differently. *Whether* to notify is a pure function of the
//  event and the goal's state, and it is the half that can be wrong in a way that matters — a banner
//  for every `run.end` of a supervised goal would train a person to ignore the one that needs them.
//  *Delivering* it is `UNUserNotificationCenter`, which cannot be observed in a headless test at all.
//
//  So they are two types. `NotificationPlan` is a value the tests can assert against, exhaustively,
//  with no notification centre in the process. `ConsoleNotifier` is the thin delivery shell, and the
//  controller talks only to the protocol it conforms to — which means a fake can record what was sent
//  without an authorisation prompt appearing on the test machine.
//
//  WHY AUTHORISATION IS LAZY
//  -------------------------
//  Asking on first launch costs a modal to a person who may have opened the app simply to read a
//  roster, and a denied prompt is permanent — there is no second chance to ask. Asking at the first
//  moment a notification would actually be delivered means the request arrives with its own
//  justification. It also means the app must run correctly *denied*: a refused notification is a
//  person's decision, not an error, so every path here degrades to "said nothing" rather than throwing.

import Foundation
import UserNotifications

/// What the console decided to tell a person, and why.
///
/// A value rather than a side effect, because "should this interrupt someone" is the part worth
/// testing and the part that is easy to get wrong. The identifier is derived from the source so two
/// events about the same gate replace each other rather than stacking — a run that reaches a gate,
/// is released by the goal, and reaches another should leave one banner, not three.
public struct NotificationPlan: Equatable, Sendable {
    public enum Urgency: String, Sendable, Equatable {
        /// Work stopped and only a person can restart it.
        case interrupt
        /// Work finished, or stopped for a reason nobody needs to act on now.
        case inform
    }

    public let identifier: String
    public let title: String
    public let body: String
    public let urgency: Urgency
    /// The thread a banner is grouped under, so several notifications about one run do not scatter
    /// across the Notification Centre.
    public let thread: String

    public init(identifier: String, title: String, body: String,
                urgency: Urgency, thread: String) {
        self.identifier = identifier
        self.title = title
        self.body = body
        self.urgency = urgency
        self.thread = thread
    }
}

/// The pure decision: should this engine event notify anyone?
///
/// Deliberately a function of the event *alone*, plus the one booleans it needs. It reads the same
/// fields the UI reads (`waiting_on`, `reason`, `summary`, `outcome`) rather than re-deriving any
/// engine policy — so a gate the engine declined to answer is reported as waiting, and a gate the
/// engine answered never reaches a person as a question.
public enum NotificationPlanner {

    /// A stable prefix so the console's notifications can be told apart from any other app's.
    public static let threadPrefix = "org.agentorg.run"

    /// The plan for one event, or nil when the event is not worth interrupting for.
    ///
    /// - Parameters:
    ///   - event: the decoded engine event.
    ///   - gateIsWaitingOnAHuman: whether the console currently believes a gate is the Owner's. Passed
    ///     in rather than read from the event so that a `human.gate` the engine *declined to answer*
    ///     is the only gate that notifies; a gate the goal released never reaches this path with
    ///     `true`, because the release arrives as `policy.changed`/`human.decision` instead.
    public static func plan(for event: EngineEvent,
                            gateIsWaitingOnAHuman: Bool) -> NotificationPlan? {
        let thread = "\(threadPrefix).\(event.runId ?? "current")"
        switch event.type {
        case "goal.completed":
            let summary = event.payload["summary"]?.stringValue ?? ""
            return NotificationPlan(
                identifier: "goal.completed",
                title: "Goal complete",
                body: summary.isEmpty ? "The objective was reached." : summary,
                urgency: .inform,
                thread: thread)

        case "goal.blocked":
            let reason = event.payload["reason"]?.stringValue ?? ""
            return NotificationPlan(
                identifier: "goal.blocked",
                title: "Goal blocked",
                body: reason.isEmpty ? "The run could not continue." : reason,
                urgency: .interrupt,
                thread: thread)

        case "goal.paused":
            let reason = event.payload["reason"]?.stringValue ?? "manual"
            // A pause at a gate is the gate's own notification arriving a moment later; two banners
            // for one stop is exactly the noise this planner exists to prevent. A pause for budget is
            // a real "I stopped and it was not you" and is worth saying.
            return NotificationPlan(
                identifier: "goal.paused",
                title: reason == "budget_spend" ? "Goal paused: budget reached" : "Goal paused",
                body: reason == "gate"
                    ? "Waiting at a gate."
                    : "Stopped because: \(reason).",
                urgency: .inform,
                thread: thread)

        case "run.end":
            let outcome = event.payload["outcome"]?.stringValue ?? "unknown"
            // A run that ended *at a gate* did not finish — it parked. Telling someone "the run
            // finished" there would be the most confusing message the app could send, because the
            // thing they need to know is that it is waiting.
            if outcome == "awaiting_human" || outcome == "gated" {
                return NotificationPlan(
                    identifier: "run.end",
                    title: "Run waiting on you",
                    body: "The run stopped at a gate and needs a decision.",
                    urgency: .interrupt,
                    thread: thread)
            }
            return NotificationPlan(
                identifier: "run.end",
                title: "Run finished",
                body: "Outcome: \(outcome).",
                urgency: .inform,
                thread: thread)

        case "human.gate":
            // Only a gate the engine said is the Owner's. The engine emits `waiting_on: owner` with
            // its own reason precisely so the console does not have to decide this for itself.
            guard gateIsWaitingOnAHuman else { return nil }
            let reason = event.payload["reason"]?.stringValue ?? "a gate"
            let why = event.payload["why"]?.stringValue ?? ""
            let title: String
            if why.contains("safety control") {
                title = "A safety control fired — your decision"
            } else if why.contains("evidence is not present") {
                title = "A gate has no evidence — your decision"
            } else if why.contains("blocked") {
                title = "A node is blocked — your decision"
            } else {
                title = "Waiting on you at a gate"
            }
            return NotificationPlan(
                identifier: "human.gate",
                title: title,
                body: why.isEmpty ? reason : "\(reason) — \(why)",
                urgency: .interrupt,
                thread: thread)

        default:
            return nil
        }
    }
}

/// How the controller delivers a notification.
///
/// A protocol rather than a direct call to `UNUserNotificationCenter` so the controller's *decisions*
/// — which events notify, when authorisation is asked for, and what happens when it is refused — are
/// assertable in a test process that has no notification centre and no way to click Allow.
public protocol ConsoleNotifier: AnyObject, Sendable {
    /// Whether a notification would be delivered right now. `false` also covers "not yet asked".
    var isAuthorized: Bool { get async }
    /// Ask once, lazily, at the first moment a notification is actually wanted.
    @discardableResult
    func requestAuthorization() async -> Bool
    /// Deliver a plan. Must not throw: a notification that cannot be shown is never a reason to break
    /// the console, which is why this returns a plain Bool rather than being `throws`.
    @discardableResult
    func deliver(_ plan: NotificationPlan) async -> Bool
}

/// The real notifier, over `UNUserNotificationCenter`.
public final class SystemConsoleNotifier: ConsoleNotifier, @unchecked Sendable {

    /// The notification centre, or nil when this process cannot have one.
    ///
    /// Optional rather than resolved in the initialiser's default argument: `UNUserNotificationCenter
    /// .current()` raises when the process has no application bundle — which is the case in a `swift
    /// test` run, and was a hard crash (signal 6) the first time a test built a controller. A default
    /// argument is evaluated at the call site, so the *default* was the thing crashing. Resolving it
    /// here, behind `isAvailable`, turns "there is no notification centre" into a value every method
    /// already handles.
    private let center: UNUserNotificationCenter?
    /// Cached after the first answer, so a long run does not ask the system on every event. The
    /// authorisation is a per-app setting; re-reading it per event would be a syscall for a value that
    /// cannot change except by a person going to System Settings.
    private let lock = NSLock()
    private var cachedAuthorization: Bool?

    public init(center: UNUserNotificationCenter? = nil) {
        self.center = center ?? (SystemConsoleNotifier.isAvailable ? .current() : nil)
    }

    /// Whether this process can use `UNUserNotificationCenter` at all.
    ///
    /// `UNUserNotificationCenter.current()` raises when the process has no application bundle — which
    /// is the case in a `swift test` run. The check lives here rather than at each construction site so
    /// there is exactly one place that knows this, and so the *decision* not to notify is a returned
    /// value rather than an exception nobody can catch.
    public static var isAvailable: Bool {
        Bundle.main.bundleURL.pathExtension == "app"
    }

    public var isAuthorized: Bool {
        get async {
            guard let center else { return false }
            if let cached = lock.withLock({ cachedAuthorization }) { return cached }
            let settings = await center.notificationSettings()
            let granted = settings.authorizationStatus == .authorized
                || settings.authorizationStatus == .provisional
            lock.withLock { cachedAuthorization = granted }
            return granted
        }
    }

    @discardableResult
    public func requestAuthorization() async -> Bool {
        guard let center else { return false }
        // Already answered (either way): do not ask again. A denied app must not re-prompt — the
        // system will not show it anyway, and a silent no-op every event is worse than one check.
        let settings = await center.notificationSettings()
        if settings.authorizationStatus != .notDetermined {
            let granted = settings.authorizationStatus == .authorized
                || settings.authorizationStatus == .provisional
            lock.withLock { cachedAuthorization = granted }
            return granted
        }
        let granted = (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
        lock.withLock { cachedAuthorization = granted }
        return granted
    }

    @discardableResult
    public func deliver(_ plan: NotificationPlan) async -> Bool {
        guard let center, await isAuthorized else { return false }
        let content = UNMutableNotificationContent()
        content.title = plan.title
        content.body = plan.body
        content.threadIdentifier = plan.thread
        if plan.urgency == .interrupt { content.interruptionLevel = .timeSensitive }
        let request = UNNotificationRequest(identifier: plan.identifier,
                                            content: content, trigger: nil)
        do {
            try await center.add(request)
            return true
        } catch {
            // A refused or failed delivery is never the console's problem. Swallowed on purpose, and
            // the reason is that the *alternative* — surfacing it — would put a banner about a banner
            // in front of someone who is trying to watch a run.
            return false
        }
    }
}

private extension NSLock {
    /// Run a closure under the lock and return its value.
    func withLock<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }
}
