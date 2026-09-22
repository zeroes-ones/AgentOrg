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
//
//  WHY A BANNER HAS TO BE TAKABLE BACK
//  -----------------------------------
//  A notification is the one message surface this app puts somewhere it cannot reach: Notification
//  Centre, which keeps it until something removes it. Everything else here can be emptied — the
//  terminal has a Clear menu, the engine's stderr a button, the status bar's notice a dismiss — and the
//  banner had nothing at all, so a gate that had already been answered went on asking for a decision
//  beside a console that was running fine. So the plan carries an identifier out of one vocabulary
//  (`NotificationIdentifier`, one name per *stop*), the notifier can `withdraw` by those names, and the
//  console calls it at the moment the thing a banner announced stops being true: the gate answered, the
//  plan taken, the engine back. Reusing a name is also what makes a second frame about one stop replace
//  the first banner rather than join it.

import Foundation
import UserNotifications

/// What the console decided to tell a person, and why.
///
/// A value rather than a side effect, because "should this interrupt someone" is the part worth
/// testing and the part that is easy to get wrong. The identifier comes from `NotificationIdentifier`,
/// one name per *stop* rather than per event kind, so the frames that describe one stop replace each
/// other rather than stacking — a run that reaches a gate, is released by the goal, and reaches another
/// should leave one banner, not three.
///
/// **And the body carries both halves of what a person needs**: the engine's own sentence about what
/// happened (its `reason`, its `why`, its `error`) and the place in this app where the control for it
/// lives. A banner is read at a glance and then dismissed, so a body that names a state and not an
/// action is a banner a person has to go looking after.
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

/// The identifiers this app posts under — **one per stop, not one per event kind**.
///
/// **Why this is a vocabulary and not a literal at each call site.** The identifier is the thing that
/// decides whether a second banner joins the first in Notification Centre or *replaces* it, and it is
/// the only handle the app has for taking a banner back. Two frames that describe one stop therefore
/// have to agree on it: a run that parks at a gate arrives as `human.gate` and, a moment later, as a
/// gated `run.end`. Two event kinds, one stop — and with an identifier each, the person was left two
/// banners saying the same thing in different words, which is the noise that trains someone to dismiss
/// without reading.
///
/// It is also what the *cleanup* is addressed by: `withdraw` takes banners out by these names, so a
/// gate the person has already answered stops being announced as still waiting.
public enum NotificationIdentifier {
    /// A gate the engine left to a person — carried by `human.gate` and by a run that ended at one.
    public static let gate = "run.gate"
    /// A graph the engine proposed and parked the run on for approval (`manifest.proposed`).
    public static let plan = "run.plan"
    /// A run that ended: finished, stopped, or failed.
    public static let runEnd = "run.end"
    public static let goalCompleted = "goal.completed"
    public static let goalBlocked = "goal.blocked"
    public static let goalPaused = "goal.paused"
    /// **The engine is gone** — carried by both frames that say so: the engine's own fatal `error`
    /// frame, and the process bridge's report that the engine stopped. One stop, so one name: an engine
    /// that reports a fatal error and then dies would otherwise arrive as two banners, the second
    /// replacing the first only if they agreed on this.
    public static let engineFailed = "engine.failed"

    /// Every identifier this build can post under, for a caller that has to take them all back.
    ///
    /// Kept as a list rather than using `removeAllDeliveredNotifications()`, and a test holds it to the
    /// planner's own cases: the identifiers are the same vocabulary the planner posts under, so a plan
    /// this app can post but cannot withdraw would be a banner that stays in Notification Centre for
    /// ever — which is the thing the list exists to make impossible.
    public static let all: [String] = [
        gate, plan, runEnd, goalCompleted, goalBlocked, goalPaused, engineFailed,
    ]
}

/// The pure decision: should this engine event notify anyone?
///
/// Deliberately a function of the event *alone*, plus the booleans it needs. It reads the same fields
/// the UI reads (`waiting_on`, `reason`, `why`, `approvable`, `state`) rather than re-deriving any
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
    ///   - planIsAwaitingApproval: whether the console is holding a graph the engine proposed and
    ///     parked the run on. **The same kind of fact as the one above, for the other stop that is a
    ///     person's**: a run parked at `awaiting_approval` announces itself as `manifest.proposed` and
    ///     then ends, and the `run.end` it ends with must not be announced a second time. It is passed
    ///     in because the event cannot say it — `run.end` carries the outcome, not the history that led
    ///     to it — and the console is the thing holding that history.
    public static func plan(for event: EngineEvent,
                            gateIsWaitingOnAHuman: Bool,
                            planIsAwaitingApproval: Bool) -> NotificationPlan? {
        let thread = "\(threadPrefix).\(event.runId ?? "current")"
        switch event.type {
        case "goal.completed":
            let summary = event.payload["summary"]?.stringValue ?? ""
            return NotificationPlan(
                identifier: NotificationIdentifier.goalCompleted,
                title: "Goal complete",
                body: summary.isEmpty ? "The objective was reached." : summary,
                urgency: .inform,
                thread: thread)

        case "goal.blocked":
            let reason = event.payload["reason"]?.stringValue ?? ""
            return NotificationPlan(
                identifier: NotificationIdentifier.goalBlocked,
                title: "Goal blocked",
                body: andWhereToAct(reason.isEmpty ? "The goal stopped and needs you" : reason,
                                    "open Now to act on it"),
                urgency: .interrupt,
                thread: thread)

        case "goal.paused":
            let reason = event.payload["reason"]?.stringValue ?? "manual"
            // **A pause at a gate notifies nobody, because the gate already did.** `goal.paused` with
            // `reason: gate` and `human.gate` are one stop seen twice; the pause was de-escalated to
            // `.inform` rather than dropped, which still posted a *second banner with its own
            // identifier* — and a second banner for one stop is the noise this planner exists to
            // prevent, in the module's own words above. The rest of the console already reads it this
            // way: `OrgController`'s `goal.paused` case sets a notice for every reason *except* a gate,
            // and the gate's own row is the thing that says it. A pause for budget is a real "I stopped
            // and it was not you", and is worth saying.
            if reason == "gate" { return nil }
            return NotificationPlan(
                identifier: NotificationIdentifier.goalPaused,
                title: reason == "budget_spend" ? "Goal paused: budget reached" : "Goal paused",
                body: andWhereToAct("Stopped because: \(reason)", "open Now to continue it"),
                urgency: .inform,
                thread: thread)

        case "manifest.proposed":
            // **The stop that notified nobody.** `_cmd_start` and `Orchestrator.prepare` emit this and
            // park the run at `awaiting_approval`, where it stays until a person approves the graph —
            // and the planner had no case for it, so the one engine state that asks a person for
            // something reached them as nothing at all while the sidebar quietly grew a badge. The
            // fields read here are the ones the plan card reads, so the banner and the card say the
            // same thing: the engine's own `approvable`, and its own `reason` when it is not.
            return plan(forProposedGraph: event.payload, thread: thread)

        case "error":
            // **The engine's own report that it is dying, and it notified nobody either.** A `fatal`
            // error frame is what the console already treats as the reason the engine is about to stop
            // (it sets the failure banner from it), but the notification path only ever saw the *bridge*
            // failing afterwards — and an engine that dies without its pipe closing reaches nobody at
            // all. Every other `error` is ordinary traffic: the recorded trace's own is a retryable
            // rate limit, and a banner for that would be a banner per rate-limited minute.
            guard event.payload["fatal"]?.boolValue == true else { return nil }
            let message = event.payload["message"]?.stringValue ?? "the engine reported a fatal error"
            return NotificationPlan(
                identifier: NotificationIdentifier.engineFailed,
                title: "The engine reported a fatal error",
                body: andWhereToAct(message, "the engine is stopping — start it again from Now"),
                urgency: .interrupt,
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
                // **One stop, one banner — and the frame that says the most is the one that speaks.**
                // A run that parks at a gate emits `human.gate` and then this frame, and this frame
                // carries no `why`: announcing both left the person two banners for one stop, the
                // second of them the poorer one. When the gate or a proposed plan is already on screen it
                // has been announced, so this is the same stop seen again and stays quiet.
                //
                // When neither is on screen — a gate whose decision the goal took, a frame dropped — no
                // banner has announced this stop and this frame is all the evidence there is, so it
                // speaks: a missing banner is recoverable, a person who has learned to dismiss banners
                // without reading is not.
                //
                // The one case where this stays quiet and *this process* posted no banner is a console
                // relaunched into a parked run: the poll restores the proposal from the checkpoint, so
                // the plan card is on screen with its Approve button while this frame is suppressed —
                // and a banner from the earlier process said so at the time. That is the trade this rule
                // makes on purpose: the card is the surface that carries the control, and it is where a
                // banner would have sent the person anyway.
                guard !gateIsWaitingOnAHuman, !planIsAwaitingApproval else { return nil }
                return NotificationPlan(
                    identifier: NotificationIdentifier.gate,
                    title: "Run waiting on you",
                    body: "The run stopped at a gate and needs a decision — open Now to decide it.",
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
                    identifier: NotificationIdentifier.runEnd,
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
                // host wrote — rather than one composed here, and the place to read what it left behind
                // is named after it, because "the run failed" alone leaves a person with nowhere to go.
                let reason = event.payload["error"]?.stringValue ?? ""
                return NotificationPlan(
                    identifier: NotificationIdentifier.runEnd,
                    title: "Run failed",
                    body: andWhereToAct(reason.isEmpty ? "The run stopped before it finished" : reason,
                                        "open Runs to read what it left behind"),
                    urgency: .interrupt,
                    thread: thread)
            }
            return NotificationPlan(
                identifier: NotificationIdentifier.runEnd,
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
                identifier: NotificationIdentifier.gate,
                title: title,
                body: andWhereToAct(why.isEmpty ? reason : "\(reason) — \(why)",
                                    "approve or reject it on Now"),
                urgency: .interrupt,
                thread: thread)

        default:
            return nil
        }
    }

    /// The plan for the *bridge's* own failure — the engine process stopped.
    ///
    /// Not a case in `plan(for:)` because it is not one of the engine's events: this is the app's
    /// process bridge reporting that the engine is gone. It lives here so the sentence, the identifier
    /// and the thread a person sees are decided in the one place all the others are — and so a banner
    /// about a dead engine is asserted in a suite rather than composed at a call site no test reaches.
    ///
    /// It shares its identifier with the engine's own fatal `error` frame on purpose: an engine that
    /// reports a fatal error and then dies produces both frames, and one death must leave one banner —
    /// the later one, which is the bridge's account of what actually happened to the process.
    public static func engineStopped(reason: String) -> NotificationPlan {
        NotificationPlan(
            identifier: NotificationIdentifier.engineFailed,
            title: "The engine stopped",
            body: andWhereToAct(reason, "press Try again in AgentOrg"),
            urgency: .interrupt,
            thread: threadPrefix)
    }

    /// The plan for a graph the engine proposed and parked the run on.
    ///
    /// **The engine's own verdict decides the sentence.** `approvable` is the engine saying it will
    /// accept `approve_plan`, and `reason` is its own account of what blocks it — the same two fields
    /// the Now pane's plan card renders (`NowPane.approveControl`). A banner that said only "a plan is
    /// waiting" would leave a person to open the window and find out what the engine thinks, which is
    /// the guessing this console does not do.
    ///
    /// Three branches, because the engine's *silence* is one of them: `approvable: true`, `approvable:
    /// false`, and a payload that carries neither — the last said as not knowing rather than as a
    /// refusal the engine never made. Inside the refusal there is a fourth: a `false` with no `reason`,
    /// which is also reported as what it is instead of filled in with an invented cause.
    static func plan(forProposedGraph payload: [String: JSONValue],
                     thread: String) -> NotificationPlan {
        let nodes = (payload["nodes"]?.arrayValue ?? []).count
        let steps = "\(nodes) step\(nodes == 1 ? "" : "s")"
        let gaps = (payload["staffing_gaps"]?.arrayValue ?? []).count
        let reason = payload["reason"]?.stringValue ?? ""
        switch payload["approvable"]?.boolValue {
        case .some(true):
            let gapNote = gaps == 0
                ? ""
                : " \(gaps) capability(ies) nobody holds — the card names them."
            return NotificationPlan(
                identifier: NotificationIdentifier.plan,
                title: "A plan needs your approval",
                body: andWhereToAct("\(steps) for this run\(gapNote)",
                                    "approve it on Now to run it"),
                urgency: .interrupt,
                thread: thread)
        case .some(false):
            return NotificationPlan(
                identifier: NotificationIdentifier.plan,
                title: "A plan cannot be approved yet",
                body: andWhereToAct(reason.isEmpty
                                        ? "the engine refused this plan without saying why"
                                        : reason,
                                    "the plan is on Now with the engine's verdict"),
                urgency: .interrupt,
                thread: thread)
        case .none:
            return NotificationPlan(
                identifier: NotificationIdentifier.plan,
                title: "A plan is waiting for your decision",
                body: andWhereToAct(steps, "the plan card on Now carries the engine's terms"),
                urgency: .interrupt,
                thread: thread)
        }
    }

    /// The engine's own fact, then the one clause that says where to act on it.
    ///
    /// **Both halves are required of a banner, and the second is the one that was missing.** "The run
    /// is waiting" tells a person a state and not an action; every body here now carries the engine's
    /// sentence (its `reason`, its `why`, its `error`) *and* names the surface in this app that holds the
    /// control. The clause is the app's own business rather than an inference about the engine — it
    /// names a destination or a button this console actually renders — so it cannot become a confident
    /// claim about a state nobody checked.
    static func andWhereToAct(_ fact: String, _ actClause: String) -> String {
        let trimmed = fact.trimmingCharacters(in: .whitespacesAndNewlines)
        let sentence = actClause.prefix(1).uppercased() + actClause.dropFirst() + "."
        guard !trimmed.isEmpty else { return sentence }
        // The engine's sentences usually end in a full stop and sometimes do not; one separator for both
        // is the difference between a banner and a run-on.
        let endsItsSentence = trimmed.hasSuffix(".") || trimmed.hasSuffix("!")
            || trimmed.hasSuffix("?") || trimmed.hasSuffix("…")
        return endsItsSentence
            ? "\(trimmed) \(actClause.prefix(1).uppercased())\(actClause.dropFirst())."
            : "\(trimmed) — \(actClause)."
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
    /// Take banners back out of Notification Centre.
    ///
    /// **The other half of having posted one.** A banner says something about the present — "the run is
    /// waiting on you" — and that stops being true the moment the decision is made. Without this the
    /// app's own answer to a resolved gate was to leave the question in Notification Centre for ever,
    /// where the next person to look at the screen reads a state that no longer holds.
    ///
    /// Synchronous because the system's own call is (`removeDeliveredNotifications(withIdentifiers:)`
    /// returns nothing and cannot fail), so there is nothing here for a caller to await. Not
    /// `removeAllDeliveredNotifications()`: the identifiers are the vocabulary the planner posts under,
    /// so what is withdrawn is exactly what this app said.
    func withdraw(_ identifiers: [String])
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

    /// Take banners back out of Notification Centre, by the names this app posted them under.
    ///
    /// Silent, like every other path here, and for a stronger reason than the delivery: this is called
    /// from the console's ordinary bookkeeping — a gate answered, a plan approved, an engine that came
    /// back — and a failure reported from there would put a modal in front of someone who has just done
    /// the thing the app asked. There is also nothing to report: the call cannot fail, and a process
    /// with no notification centre has nothing to withdraw.
    public func withdraw(_ identifiers: [String]) {
        guard let center, !identifiers.isEmpty else { return }
        center.removeDeliveredNotifications(withIdentifiers: identifiers)
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
