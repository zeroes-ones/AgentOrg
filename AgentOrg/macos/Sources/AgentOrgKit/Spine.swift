//
//  Spine.swift
//  AgentOrgKit
//
//  The three questions the window always answers: where am I, what needs me, what next?
//
//  WHY THERE IS A SPINE AT ALL
//  ---------------------------
//  The audit's central finding was not that the panels were wrong — most of them were good — it was
//  that **there was no spine**. No single place said "you are here, this needs you, do this next".
//  The one panel that tried (Activity) only did so after you found it, and a gate awaiting a decision
//  was visible on one of twelve sidebar rows and nowhere else.
//
//  So the model below is built from the same engine reports the panels use, it is always rendered
//  above whichever destination is showing, and it is a *value*: a pure function of what the engine
//  said. That is the same discipline `GateDisposition` and `NotificationPlanner` follow, and it is
//  what makes the "Next" line assertable without a window.

import Foundation

/// One line of the spine: what the engine, the project and the goal are doing.
public struct SpineModel: Equatable, Sendable {

    /// A decision only a person can take.
    public struct GateLine: Equatable, Sendable {
        public let reason: String
        /// What the gate wants, and what is present. Empty when the engine did not say.
        public let requires: [String]
        public let present: [String]
        /// Whether the console may act on this gate at all.
        ///
        /// This is the one field the Approve/Reject buttons hang off, and it comes straight from
        /// `GateDisposition.isWaitingForHuman` — which is computed from the engine's own refusal
        /// (`waiting_on: owner`), the goal's posture, and whether the engine already decided. The
        /// buttons are therefore rendered **only where they can act**: a control beside a gate the
        /// console is answering itself is a race, and a control beside a gate the engine already
        /// answered is a lie.
        public let canAct: Bool
        /// Why the console is (or is not) going to decide it, in the engine's terms.
        public let why: String

        public init(reason: String, requires: [String] = [], present: [String] = [],
                    canAct: Bool, why: String) {
            self.reason = reason
            self.requires = requires
            self.present = present
            self.canAct = canAct
            self.why = why
        }

        /// What is still missing from the gate's evidence, which is what makes a release decidable.
        public var missing: [String] { requires.filter { !present.contains($0) } }

        /// One line naming the evidence, or nil when the engine reported none.
        public var evidence: String? {
            guard !requires.isEmpty else { return nil }
            let have = present.filter { requires.contains($0) }
            return "\(requires.count) required, \(have.count) present"
                + (missing.isEmpty ? "" : " — missing \(missing.joined(separator: ", "))")
        }
    }

    /// The one action worth taking next, in the engine's own words.
    ///
    /// Deliberately not a Swift-side guess: the engine's `activity.next_action` already orders a gate
    /// above a staffing gap above a stop above a resume, and it can see the ledger and the run's
    /// `stop_reason`, which this app cannot. Recomputing that order here would be a second
    /// implementation of a rule the engine has already resolved — the same mistake `GateDisposition`
    /// exists to avoid.
    public struct NextLine: Equatable, Sendable {
        public let kind: String
        public let label: String
        public let detail: String
        /// The CLI command the engine named, for the cases the app cannot perform itself.
        public let command: String
        /// Whether the engine says a console can carry this action out itself.
        ///
        /// **The engine's answer, not a list of kinds kept here.** This used to be a `switch` over the
        /// kinds `_next_action` can emit, which meant every kind the engine gained was a kind this app
        /// had no opinion about until someone widened the switch — the day `retry` appeared, the app's
        /// silence on it was correct only by accident. The engine now answers with the action
        /// (`activity._NEXT_PERFORMABLE`, sent as `performable`), and this reads it.
        public let performable: Bool
        /// What performing it still needs, in the engine's vocabulary — `""` when nothing does.
        ///
        /// One token is in play: `NextLine.gate`, meaning the offer is the person's only while the gate
        /// is still theirs to answer. That is the one condition the *app* holds — whether the engine has
        /// since answered the gate itself, and what the goal's posture allows, are read from payloads
        /// this line does not carry — so the engine names it and `canPerform` supplies the state.
        public let needs: String

        /// The engine's word for "this is the person's only while the gate still is theirs".
        ///
        /// A constant rather than a literal in two places, and a *read* of the engine's vocabulary
        /// rather than a second definition of it: `activity._NEXT_PERFORMABLE` is where the token is
        /// written, and the payload carries whatever it says.
        public static let gate = "gate"

        /// No defaults on the two engine answers: they are what the engine said, and a default would let
        /// a caller produce a confident `false` the engine never gave.
        public init(kind: String, label: String, detail: String, command: String,
                    performable: Bool, needs: String) {
            self.kind = kind
            self.label = label
            self.detail = detail
            self.command = command
            self.performable = performable
            self.needs = needs
        }

        /// Whether the console can honestly offer to do this action itself.
        ///
        /// Two fields and one local fact, so no button here is one that silently does nothing:
        ///
        /// * `performable` — the engine's own answer, true for the kinds that lead somewhere in this
        ///   app: `resume` (the goal is paused and the console resumes it), `hire` (opens Org, where a
        ///   hire is made — the console does not re-implement the hire form), `start` (the composer's
        ///   own Start, through `OrgController.startRun`), `investigate` (opens Runs, which is where a
        ///   stopped run's nodes, verdicts and handoffs are read), and `decide` (the gate row above
        ///   carries the buttons).
        /// * `needs == NextLine.gate` — the offer is conditional on the gate being the person's, which
        ///   is `GateDisposition`'s reading of the engine's own refusal, not a rule invented here.
        ///
        /// Everything else is answered `false`, which is the engine's answer too: `retry` names a move
        /// only a command makes — "re-run the graph so <node> gets another attempt", with
        /// `flow.recovery_command`'s command (`engine/activity.py:490`, `engine/flow.py:387`). The
        /// console **cannot** perform it: no `serve` command runs a graph, and its `start` *plans* a
        /// goal and points a new run at a new graph (`engine/serve.py:1168`), which is a different run
        /// and a new spend. The command is shown instead — as it is for a kind this build has never
        /// seen, which is what makes an engine that adds one tomorrow work without an edit here. The
        /// board's own header shows the same move, taken from `flow.next` (`BoardNext`).
        public func canPerform(gateIsWaitingForHuman: Bool) -> Bool {
            if performable { return true }
            return needs == Self.gate && gateIsWaitingForHuman
        }
    }

    /// The engine, as a word and a tone. Never colour alone.
    public let stateWord: String
    public let stateTone: StatusTone
    /// The project the agents are working in, as a person recognises it.
    public let projectName: String
    /// Whether that folder is the person's own or one the engine owns, said in words.
    public let projectDetail: String
    /// The goal's objective, when one is set.
    public let goalObjective: String?
    /// Whether the loop will continue, and why not when it will not.
    public let goalDetail: String?
    public let goalTone: StatusTone

    /// The gate awaiting a decision, when one is.
    public let gate: GateLine?
    /// The next thing to do, when anything is.
    public let next: NextLine?
    /// The first-run hint, when a run is not possible yet. Nil once it is.
    public let setupHint: String?

    public init(stateWord: String, stateTone: StatusTone, projectName: String,
                projectDetail: String, goalObjective: String?, goalDetail: String?,
                goalTone: StatusTone, gate: GateLine?, next: NextLine?,
                setupHint: String?) {
        self.stateWord = stateWord
        self.stateTone = stateTone
        self.projectName = projectName
        self.projectDetail = projectDetail
        self.goalObjective = goalObjective
        self.goalDetail = goalDetail
        self.goalTone = goalTone
        self.gate = gate
        self.next = next
        self.setupHint = setupHint
    }

    /// Whether anything at all needs a person right now.
    ///
    /// The one thing a window title, a menu-bar glyph and a badge all want, decided in one place.
    public var needsAPerson: Bool { gate?.canAct == true }
}

/// Whether a board row stopped short — the engine's own predicate, mirrored so it has one home here.
///
/// `engine/flow.py:278` (`is_stuck`) is the rule the engine itself uses for the board's `Stuck`
/// figure, the row's tone, the headline's sentence and the board's `next`: a node that has not
/// finished, and whose status *or* whose verdict names a stop (`engine/flow.py:130-131`). It is asked
/// before the working/waiting tallies there, "because a row can be *both*" — a checkpoint carrying a
/// `guardrail-blocked` verdict on a status that reads as waiting is a node that stopped, and counting
/// it as waiting would be the figure contradicting the row's own tone.
///
/// A **finished** node is never stopped, whatever verdict it still carries, and that guard is the
/// engine's: the runner keeps a gate's `awaiting_owner` verdict after a release marks its status
/// `done`, so a verdict-keyed test without it reports a node that has already moved on.
///
/// The app used to answer this question in two narrower, different ways — the run list counted
/// `status == "blocked" || verdict == "guardrail-blocked"` (`RunStateBrowser.swift:55` as it was) and
/// the board counted four statuses — so a step stopped by its completion contract read "0 blocked" in
/// the list and "1 stuck" on the board. One predicate, read by the board's rows, the board's header
/// and the run list, is what stops two screens answering one question two ways.
public enum BoardStop {

    /// The statuses that mean a node stopped short (`engine/flow.py:130`, `_STUCK_STATUSES`).
    public static let stuckStatuses: Set<String> = ["blocked", "failed", "needs_review", "awaiting_owner"]

    /// The verdicts that say the same thing when the status does not (`engine/flow.py:131`).
    public static let stuckVerdicts: Set<String> = ["guardrail-blocked", "awaiting_owner"]

    /// The statuses that mean a node is finished, which outrank any verdict left on the record.
    public static let finishedStatuses: Set<String> = ["done", "pass", "skipped"]

    /// Whether a node with this status and verdict stopped short — `engine/flow.py:278`, mirrored.
    public static func isStuck(status: String, verdict: String) -> Bool {
        if finishedStatuses.contains(status) { return false }
        return stuckStatuses.contains(status) || stuckVerdicts.contains(verdict)
    }

    /// Whether one `flow.rows` entry stopped short.
    public static func isStuck(_ row: [String: JSONValue]) -> Bool {
        isStuck(status: row["status"]?.stringValue ?? "", verdict: row["verdict"]?.stringValue ?? "")
    }
}

/// The move the engine names for what the board is showing, taken apart so a person can read it.
///
/// `flow.next` is **one line**, by design: the command, then `systemcli.NEXT_SEP`, then why that
/// command is the one (`engine/flow.py:428`, `_next_line`). The engine shapes it that way so the
/// `--json` value and the terminal line are the same sentence, and so "a caller that wants only the
/// command takes the text before the separator" (`engine/systemcli.py:40`). This is that caller.
///
/// **Why this is text and not a button.** The console may only offer an action it can deliver, and it
/// cannot deliver this one: the engine's recovery for a stopped board is `engine.cli run --slug …
/// --manifest …` (`engine/flow.py:387`, `recovery_command`), and `engine/serve.py` has no command that
/// runs a graph — its `start` *plans* a goal and points a new run at a new graph (`_cmd_start`, `:1168`),
/// which is a different run and a new spend, not a re-run of this manifest. So the command is rendered
/// as selectable text, the way this app already renders a path it cannot open, and it is run where it
/// belongs.
///
/// For the same reason no `takeover` or `reassign` control is offered for a stopped row. The engine's
/// own note on this recovery is that both "change *who* does the work, not whether the contract is
/// met", and both are refused when there is no resumable run to pin (`engine/cli.py:1946`, `:1988`) —
/// so a control here would be one the engine refuses, or one that does not resolve the stop.
public struct BoardNext: Equatable, Sendable {

    /// The command, with the separator and the reason removed.
    public let command: String
    /// Why that command is the one, in the engine's words. Empty when the engine sent no reason.
    public let why: String

    /// Where the engine splits a `next` line (`engine/systemcli.py:96`, `NEXT_SEP`).
    static let separator = "   — "

    /// Take a `flow.next` line apart, or nil when the engine named no move.
    ///
    /// A line with no separator is still a move the engine named, so it is kept whole rather than
    /// dropped — dropping it would be the app deciding the engine's answer was malformed.
    public static func parse(_ line: String) -> BoardNext? {
        let text = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        guard let split = text.range(of: separator) else {
            return BoardNext(command: text, why: "")
        }
        let command = String(text[text.startIndex..<split.lowerBound])
            .trimmingCharacters(in: .whitespaces)
        let why = String(text[split.upperBound...]).trimmingCharacters(in: .whitespaces)
        // A separator with no command before it names nothing to run, so it is not a next move. Kept
        // as a guard rather than dropped: the engine never writes one today, and a line that reaches
        // this must not become a button-shaped empty command if it ever does.
        guard !command.isEmpty else { return nil }
        return BoardNext(command: command, why: why)
    }

    /// The whole line as the engine wrote it — what a person copies when they want both halves.
    public var line: String {
        why.isEmpty ? command : "\(command)\(Self.separator)\(why)"
    }
}

/// Everything the spine is built from, so the builder takes values rather than a live controller.
///
/// A struct rather than eight positional parameters: a builder with eight arguments is one where the
/// call site is unreadable and a test's intent is lost, and this one is called from exactly one place
/// in the app but from several in the tests.
public struct SpineInput: Sendable {
    public var engineState: EngineState
    public var engineFailure: String?
    public var engineError: String?
    /// The `workspace` block from `status`.
    public var workspace: [String: JSONValue]
    /// The project path, used when the engine has not reported a workspace yet.
    public var projectPath: String
    /// The `goal` block from `status`.
    public var goal: [String: JSONValue]
    /// The `activity` report, which carries `next_action`.
    public var activity: [String: JSONValue]
    /// The gate awaiting a decision, if any.
    public var pendingGate: [String: JSONValue]?
    /// The engine's disposition for that gate.
    public var gateDisposition: OrgController.GateDisposition
    /// Whether a run is possible yet — the wizard's verdict.
    public var setupHint: String?

    public init(engineState: EngineState, engineFailure: String? = nil,
                engineError: String? = nil, workspace: [String: JSONValue] = [:],
                projectPath: String = "", goal: [String: JSONValue] = [:],
                activity: [String: JSONValue] = [:],
                pendingGate: [String: JSONValue]? = nil,
                gateDisposition: OrgController.GateDisposition =
                    .waitForHuman(why: "no gate is waiting"),
                setupHint: String? = nil) {
        self.engineState = engineState
        self.engineFailure = engineFailure
        self.engineError = engineError
        self.workspace = workspace
        self.projectPath = projectPath
        self.goal = goal
        self.activity = activity
        self.pendingGate = pendingGate
        self.gateDisposition = gateDisposition
        self.setupHint = setupHint
    }
}

extension SpineModel {
    /// Build the spine from what the engine reported.
    public static func make(_ input: SpineInput) -> SpineModel {
        let (word, tone) = engineWord(input)
        let name = projectName(input)
        let detail = input.workspace["attached"]?.boolValue == true
            ? "your folder — the agents edit it directly"
            : "a managed project the engine owns"

        let objective = input.goal["objective"]?.stringValue ?? ""
        let live = input.goal["live"]?.boolValue == true
        let goalState = input.goal["state"]?.stringValue ?? "cleared"
        let pauseReason = input.goal["pause_reason"]?.stringValue ?? ""

        var goalTone: StatusTone = .neutral
        var goalDetail: String?
        if !objective.isEmpty {
            if live {
                goalTone = .ok
                goalDetail = "continues until the agent reports it done or blocked"
            } else if goalState == "paused" {
                goalTone = .attention
                goalDetail = pauseReason.isEmpty ? "paused" : "paused — \(pauseReason)"
            } else if goalState == "blocked" {
                goalTone = .bad
                goalDetail = input.goal["blocked_reason"]?.stringValue ?? "blocked"
            } else {
                goalDetail = goalState
            }
        } else {
            goalDetail = "no goal set"
        }

        let gate = gateLine(input)
        let next = nextLine(input, gate: gate)

        return SpineModel(
            stateWord: word, stateTone: tone,
            projectName: name, projectDetail: detail,
            goalObjective: objective.isEmpty ? nil : objective,
            goalDetail: goalDetail, goalTone: goalTone,
            gate: gate, next: next,
            setupHint: input.setupHint)
    }

    /// The engine's state as a word and a tone.
    ///
    /// A *failure* outranks everything: the engine's state can read `idle` after a bootstrap failure,
    /// which is exactly the "nothing looks broken" trap — so the failure is what is said.
    private static func engineWord(_ input: SpineInput) -> (String, StatusTone) {
        if let failure = input.engineFailure, !failure.isEmpty { return ("Engine failed", .bad) }
        if let error = input.engineError, !error.isEmpty, !input.engineState.isLive {
            return ("Engine stopped", .bad)
        }
        if input.goal["live"]?.boolValue == true { return ("Working on a goal", .ok) }
        switch input.engineState {
        case .running: return ("Engine running", .ok)
        case .failed: return ("Engine failed", .bad)
        case .launching, .pausing, .terminating: return ("Working…", .attention)
        case .paused: return ("Engine paused", .attention)
        case .finished: return ("Engine stopped", .neutral)
        case .idle: return ("Engine not running", .neutral)
        }
    }

    private static func projectName(_ input: SpineInput) -> String {
        if let reported = input.workspace["name"]?.stringValue, !reported.isEmpty { return reported }
        if !input.projectPath.isEmpty {
            return URL(fileURLWithPath: input.projectPath).lastPathComponent
        }
        return "no project"
    }

    /// The gate row, from the engine's payload and the one disposition rule.
    private static func gateLine(_ input: SpineInput) -> GateLine? {
        guard let gate = input.pendingGate else { return nil }
        let requires = (gate["requires"]?.arrayValue ?? []).compactMap { $0.stringValue }
        let present = (gate["present"]?.arrayValue ?? []).compactMap { $0.stringValue }
        return GateLine(
            reason: gate["reason"]?.stringValue ?? "a gate is waiting",
            requires: requires,
            present: present,
            canAct: input.gateDisposition.isWaitingForHuman,
            why: input.gateDisposition.why)
    }

    /// The next action, preferring the engine's own report.
    ///
    /// When a gate is the person's, the gate row *is* the next action and saying it twice would be
    /// noise — so the engine's `decide` label is kept as the sentence (it is the engine's own words
    /// and names the gate) while the buttons live on the gate row, where the evidence is.
    ///
    /// `performable` and `needs` are read straight from the action: they are the engine's answer to
    /// whether a console can do this, which is the one thing this app must not work out for itself.
    private static func nextLine(_ input: SpineInput, gate: GateLine?) -> NextLine? {
        let action = input.activity["next_action"]?.objectValue
        let kind = action?["kind"]?.stringValue ?? ""
        let label = action?["label"]?.stringValue ?? ""
        if kind.isEmpty || kind == "none" || label.isEmpty {
            // No engine report yet (the engine may be down, or nothing has run). If a gate is on
            // screen it is still the next thing, and saying so is better than saying nothing.
            if let gate, gate.canAct {
                // Built here rather than decoded, so it carries the same two fields the engine's own
                // `decide` action does — the gate row's `canAct` *is* the condition `needs` names.
                return NextLine(kind: "decide", label: "Decide \(gate.reason)", detail: gate.why,
                                command: "", performable: false, needs: NextLine.gate)
            }
            return nil
        }
        return NextLine(kind: kind, label: label,
                        detail: action?["detail"]?.stringValue ?? "",
                        command: action?["command"]?.stringValue ?? "",
                        performable: action?["performable"]?.boolValue ?? false,
                        needs: action?["needs"]?.stringValue ?? "")
    }
}
