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
            // **The fields that say how a run ended are `state`, `gated` and `termination` — not
            // `outcome`.** The payload is `RunOutcome.as_dict()`, written by the same dataclass in
            // `engine/host.py` and `engine/orchestrator.py`: `state` is `finished` / `failed` /
            // `gated`, `gated` and `broken` are the booleans behind it, and `termination` is the
            // `TERMINATION_*` word for anything that stopped the run rather than the run ending. The
            // `outcome` key is the *runner's own summary word* and is absent whenever there was no
            // summary — so reading it alone, as this did, meant the gated branch below could not
            // match a real payload: every run parked at a gate was announced to the person as "the
            // run finished", which is the most confusing message this app can send.
            let state = event.payload["state"]?.stringValue ?? ""
            let summary = event.payload["outcome"]?.stringValue ?? ""
            let termination = event.payload["termination"]?.stringValue ?? ""
            // The two old spellings are kept: an `outcome` of `awaiting_human` is the same parking
            // seen through the runner's summary word, and a payload that carries only that must not
            // fall through to "finished".
            let atAGate = event.payload["gated"]?.boolValue == true
                || state == "gated"
                || summary == "awaiting_human" || summary == "gated" || summary == "awaiting_gate"
            if atAGate {
                return NotificationPlan(
                    identifier: "run.end",
                    title: "Run waiting on you",
                    body: "The run stopped at a gate and needs a decision.",
                    urgency: .interrupt,
                    thread: thread)
            }
            // **A run the person or the engine stopped is not an emergency.** `aborted` is the
            // Owner's own stop and `shutdown` is the engine going down and reaping its runner; both
            // arrive as `state: failed` with `killed` set, and interrupting someone for a stop they
            // pressed themselves is the noise that trains them to ignore the banner that matters.
            // The rule is the engine's own word rather than an inference from `killed`.
            if termination == "aborted" || termination == "shutdown" {
                return NotificationPlan(
                    identifier: "run.end",
                    title: "Run stopped",
                    body: termination == "aborted"
                        ? "Stopped at your request — the checkpoint is kept."
                        : "The engine stopped and took the run with it — the checkpoint is kept.",
                    urgency: .inform,
                    thread: thread)
            }
            let broken = event.payload["broken"]?.boolValue == true || state == "failed"
            if broken {
                // A run that broke is work that stopped with nobody watching, which is the case this
                // whole app exists for. The body is the engine's own `error` — the same sentence the
                // host wrote — rather than one composed here.
                let reason = event.payload["error"]?.stringValue ?? ""
                return NotificationPlan(
                    identifier: "run.end",
                    title: "Run failed",
                    body: reason.isEmpty ? "The run stopped before it finished." : reason,
                    urgency: .interrupt,
                    thread: thread)
            }
            return NotificationPlan(
                identifier: "run.end",
                title: "Run finished",
                body: "Outcome: \(summary.isEmpty ? (state.isEmpty ? "unknown" : state) : summary).",
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

/// What became of one banner attempt, as a value the console can read and show.
///
/// **Why this is not a `Bool` and not a sentence.** The delivery already returned a `Bool` and the
/// controller already recorded *something* — a string that said "a notification could not be
/// delivered" whether the app had been denied, the system had refused the request, or this process
/// had no notification centre at all. Those three are not one state and only two of them have an
/// action behind them, so the outcome is a value: which of the four things happened, which banner it
/// was about, the one line to show, and — where there is one — what to do about it.
///
/// It is also what makes the *silence* legible. A person who gets no banner has, until now, had no
/// way to learn whether the app was denied, unable, or simply had nothing worth interrupting them
/// for. The last two of those are "nothing to say"; this is the record that says which.
public struct NotificationOutcome: Equatable, Sendable {

    /// The four things that can happen to a banner attempt.
    public enum Kind: String, Sendable, Equatable {
        /// A banner was posted.
        case delivered
        /// The person (or an earlier prompt) refused this app permission. Changeable in System
        /// Settings, and *not* an error: a refused notification is a decision.
        case denied
        /// This process cannot post a banner at all — no application bundle to own one, which is the
        /// state a `swift run` binary is in. Nothing here is changeable; run the built app instead.
        case unavailable
        /// Authorised, and the request was refused or threw.
        case failed
    }

    public let kind: Kind
    /// The plan's own title, so a person can tell which banner this was about.
    public let title: String
    /// The one line the status bar and the panes show.
    public let sentence: String
    /// What to do about it, or nil when there is nothing to do.
    public let advice: String?
    /// When the attempt finished, so a stale outcome can be told from a current one.
    public let at: Date

    /// Whether this is something a person can act on — false only for a banner that went out, so a
    /// view can show the state without showing a warning about permission nobody needs to change.
    public var needsAttention: Bool { kind != .delivered }

    private init(kind: Kind, title: String, sentence: String, advice: String?, at: Date) {
        self.kind = kind
        self.title = title
        self.sentence = sentence
        self.advice = advice
        self.at = at
    }

    public static func delivered(_ title: String, at: Date = Date()) -> NotificationOutcome {
        NotificationOutcome(kind: .delivered, title: title, sentence: "notified: \(title)",
                            advice: nil, at: at)
    }

    /// Refused. The sentence is the one this console has always shown, kept verbatim so the state a
    /// person already recognises does not change wording under them.
    public static func denied(_ title: String, at: Date = Date()) -> NotificationOutcome {
        NotificationOutcome(
            kind: .denied, title: title,
            sentence: "notifications are off (denied in System Settings)",
            advice: "Turn them on in System Settings → Notifications → AgentOrg, then a run that needs "
                + "you can interrupt you instead of waiting to be noticed. Until then the badge on "
                + "Now and the menu-bar panel are what tell you.",
            at: at)
    }

    /// This build cannot post banners at all. Say *that*, and say which builds can: the alternative
    /// — what the console used to report for this state — was "denied in System Settings", a wrong
    /// instruction pointing at a pane where this process does not appear.
    public static func unavailable(at: Date = Date()) -> NotificationOutcome {
        NotificationOutcome(
            kind: .unavailable, title: "",
            sentence: "this build cannot post notifications — it has no application bundle",
            advice: "A `swift run` binary has no bundle for macOS to attach a banner to, so nothing "
                + "can be posted from it no matter what System Settings says. Run the built "
                + "AgentOrg.app to be notified; everything else here works the same either way.",
            at: at)
    }

    /// Authorised, and the delivery still failed.
    public static func failed(_ title: String, at: Date = Date()) -> NotificationOutcome {
        NotificationOutcome(
            kind: .failed, title: title,
            sentence: "a notification could not be delivered",
            advice: "The system refused the request. The console is unaffected — the spine, the "
                + "badge and the menu-bar panel still show anything that needs you.",
            at: at)
    }
}

/// How the controller delivers a notification.
///
/// A protocol rather than a direct call to `UNUserNotificationCenter` so the controller's *decisions*
/// — which events notify, when authorisation is asked for, and what happens when it is refused — are
/// assertable in a test process that has no notification centre and no way to click Allow.
public protocol ConsoleNotifier: AnyObject, Sendable {
    /// Whether this process can post a notification **at all**.
    ///
    /// Deliberately not `isAuthorized`, and the difference is the whole reason this requirement
    /// exists. A process with no application bundle — a `swift run` binary, or a test — has no
    /// notification centre to own a banner, so `isAuthorized` is `false` and asking for authorisation
    /// returns `false` every time. Reporting *that* as "denied in System Settings" (which is what the
    /// console did, because it could only see the two booleans) sends a person to a System Settings
    /// pane where the app is not even listed. Only one of the two states has an action behind it, so
    /// the console has to be able to tell them apart.
    var isAvailable: Bool { get }
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

    /// The same answer, as the protocol requires it — so a caller holding only `any ConsoleNotifier`
    /// can ask the question without knowing which notifier it holds.
    ///
    /// Computed per read rather than cached: `Bundle.main` is a process-wide constant, so this is a
    /// string comparison against a value that cannot change while the process runs.
    public var isAvailable: Bool { Self.isAvailable }

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
