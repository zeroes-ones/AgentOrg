//
//  Setup.swift
//  AgentOrgKit
//
//  Is a run actually possible yet, and if not, which single thing is missing?
//
//  WHY THIS IS A FUNCTION AND NOT A VIEW
//  -------------------------------------
//  The old app's first run was a scavenger hunt, and the reason it was one is that the *question*
//  "can anything run?" had no single answer anywhere in the code. The engine launches happily with no
//  credentials (it falls back to `credentials.example.json`), so nothing looks broken — but no default
//  model resolves, so no agent can bind, and that only surfaced later as an empty hire form two panels
//  away from where the person was standing.
//
//  So the answer is a pure function of the engine's own reported state (`defaults`, `providers`,
//  `workspace`, `engineState`), evaluated here and tested directly. A gate that lives in a view body
//  is a gate nobody asserts, and this one decides whether the whole product works on first launch.

import Foundation

/// The folder-name rules the engine applies, mirrored so the app and the engine agree on *where*.
///
/// `engine/state.py::_slugify` normalises a folder name into the slug grammar, and
/// `Workspace.for_project(slug, root)` builds `<root>/<slug>`. The console has to name the same
/// project the engine will, because the two disagreeing is precisely the slug bug this rewrite fixes:
/// the app was pointed at `projects/demo/` while the engine wrote `projects/console/`, so the run
/// history was permanently empty and nothing said why.
public enum WorkspaceNaming {

    /// A folder name as a valid engine slug.
    ///
    /// Kept deliberately close to the engine's own implementation (lowercase, disallowed runs of
    /// characters collapsed to `_`, leading punctuation dropped) and tested against the same
    /// examples, because a near-miss here is a silently wrong project directory.
    ///
    /// The character test is an explicit ASCII one rather than `isLetter`/`isNumber`: those accept
    /// uppercase and every Unicode letter, so `Foo.Bar` and `café` would pass as valid slugs while the
    /// engine's own `_SLUG_RE` refuses both. The app and the child must share one grammar, or the child
    /// exits with "invalid project name" before it serves a command.
    public static func slug(from name: String) -> String {
        let lowered = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        var out = ""
        var lastWasSeparator = false
        for character in lowered {
            if isSlugCharacter(character) {
                out.append(character)
                lastWasSeparator = false
            } else if !lastWasSeparator {
                out.append("_")
                lastWasSeparator = true
            }
        }
        // Drop any leading punctuation, then any trailing separator the loop left behind — the engine
        // does both, and a trailing `_` produces a different directory from the engine's.
        out = String(out.drop { "._-".contains($0) })
        out = String(out.reversed().drop { $0 == "_" }.reversed())
        return out.isEmpty ? "project" : out
    }

    /// Whether a name is already a valid slug, so a caller does not rewrite one needlessly.
    ///
    /// The same grammar the engine's `_SLUG_RE` enforces, character for character.
    public static func isValidSlug(_ name: String) -> Bool {
        guard let first = name.first, isAlphanumeric(first) else { return false }
        return name.allSatisfy { isSlugCharacter($0) }
    }

    /// The engine's slug alphabet: lowercase ASCII, digits, and `.`, `_`, `-`.
    private static func isSlugCharacter(_ character: Character) -> Bool {
        isAlphanumeric(character) || "._-".contains(character)
    }

    /// Lowercase ASCII `a`–`z` or `0`–`9`.
    ///
    /// Written out rather than using `isLetter`/`isNumber` so there is no way for a Unicode letter or
    /// an uppercase one to slip through into a directory name the engine will reject.
    private static func isAlphanumeric(_ character: Character) -> Bool {
        guard let ascii = character.asciiValue else { return false }
        return (ascii >= 97 && ascii <= 122) || (ascii >= 48 && ascii <= 57)
    }

    /// A display name from a slug, for a row that has only the slug: `my-app` → `My App`.
    public static func displayName(fromSlug slug: String) -> String {
        slug.split(whereSeparator: { "._-".contains($0) })
            .map { $0.prefix(1).uppercased() + $0.dropFirst() }
            .joined(separator: " ")
    }
}

/// The first thing standing between the person and a run.
///
/// Ordered by *dependency*, not by difficulty: there is no point offering a project choice while no
/// model resolves, because the project question can be answered at any time and the model one cannot.
public enum SetupGate: Equatable, Sendable {
    /// Nothing can be asked because the engine is not up. The reason is the engine's own failure text
    /// when it failed, so the wizard shows the real cause rather than a generic prompt.
    case engineUnavailable(reason: String?)
    /// No default provider and model pair resolves, so no agent can bind.
    case needsModel(why: String)
    /// A default resolves but the provider reported no context window for it — an agent cannot be
    /// bound to a model whose window is unknown, and the engine refuses exactly this.
    case needsWindow(why: String, provider: String, model: String)
    /// The person has not confirmed which folder the agents should work in.
    case needsProject(why: String)
    /// The person has not chosen how much the goals they create may decide alone.
    case needsAutonomy(why: String)
    /// A run is possible. The wizard never shows again.
    case ready

    /// Whether this gate blocks the wizard from going on to the next step.
    public var isBlocking: Bool { self != .ready }

    /// Which step this gate is, as a value that can be compared directly.
    ///
    /// The associated values carry the engine's own wording, which is what makes the wizard honest —
    /// but they also make `gate == .needsAutonomy` impossible to write, and a view switching on the
    /// *kind* of gate is the common case. So the kind is its own small enum and this is how a caller
    /// asks "which step am I on" without matching the message.
    public enum Kind: String, Sendable, CaseIterable {
        case engineUnavailable
        case needsModel
        case needsWindow
        case needsProject
        case needsAutonomy
        case ready
    }

    public var kind: Kind {
        switch self {
        case .engineUnavailable: return .engineUnavailable
        case .needsModel: return .needsModel
        case .needsWindow: return .needsWindow
        case .needsProject: return .needsProject
        case .needsAutonomy: return .needsAutonomy
        case .ready: return .ready
        }
    }

    /// The step number a person is on, for "Step 2 of 3" — one-based, and 3 when ready.
    public var step: Int {
        switch self {
        case .engineUnavailable, .needsModel, .needsWindow: return 1
        case .needsProject: return 2
        case .needsAutonomy: return 3
        case .ready: return 3
        }
    }

    /// Which step of `SetupJourney` this gate is on, by that step's own id.
    ///
    /// The id rather than the number, because the journey's *order* is a list and the number is derived
    /// from it — so adding a step renumbers everything, and a hardcoded `== 2` in a view would then
    /// point at the wrong row. The gate and the journey agree by name, which survives reordering.
    ///
    /// `engineUnavailable` and `needsWindow` both sit on the *model* step: neither one is a separate
    /// question for the person to answer. A stopped engine is a precondition of the whole path, and an
    /// unknown context window is fixed by the same control — testing the provider — that fixes a
    /// missing model.
    public var stepId: String? {
        switch self {
        case .engineUnavailable: return nil
        case .needsModel, .needsWindow: return "model"
        case .needsProject: return "project"
        case .needsAutonomy: return "autonomy"
        case .ready: return nil
        }
    }

    /// Whether the step with this id is already behind the person.
    ///
    /// **The one place "is this step done" is decided**, so the rail's tick marks and the wizard's
    /// blocking behaviour cannot disagree: the gate is evaluated once and every step is compared against
    /// the same answer. A step is done when it comes before the current one.
    ///
    /// `ready` means everything is behind them. `engineUnavailable` means nothing is — not even the
    /// first step, because the question after it cannot be asked until it is answered.
    public func isPast(id stepId: String) -> Bool {
        switch self {
        case .engineUnavailable:
            return false
        case .ready:
            return true
        case .needsModel, .needsWindow, .needsProject, .needsAutonomy:
            guard let current = self.stepId,
                  let currentIndex = SetupJourney.order.firstIndex(of: current),
                  let index = SetupJourney.order.firstIndex(of: stepId) else { return false }
            return index < currentIndex
        }
    }

    /// A short title for the step, used as the wizard's heading.
    public var title: String {
        switch self {
        case .engineUnavailable: return "Start the engine"
        case .needsModel, .needsWindow: return "Choose a model"
        case .needsProject: return "Choose a project"
        case .needsAutonomy: return "Choose how much it decides alone"
        case .ready: return "Ready to run"
        }
    }

    /// The sentence under the heading: what is missing and why it matters.
    public var detail: String {
        switch self {
        case .engineUnavailable(let reason):
            return reason ?? "The engine has not started yet, so there is nothing to ask it."
        case .needsModel(let why): return why
        case .needsWindow(let why, _, _): return why
        case .needsProject(let why): return why
        case .needsAutonomy(let why): return why
        case .ready: return "A model resolves, a project is chosen, and a new goal will use the "
            + "autonomy you picked."
        }
    }
}

/// The pure evaluation behind the first-run wizard.
///
/// Every input is something the engine reported. Nothing here guesses at a file, and nothing here
/// re-derives engine policy — the same discipline `GateDisposition` follows, and for the same reason:
/// the engine's answer is the authoritative one and a second implementation would disagree with it.
public enum SetupReadiness {

    /// - Parameters:
    ///   - engineIsRunning: whether the engine has sent its readiness frame. Nothing can be asked
    ///     before this, so it short-circuits every other question.
    ///   - engineFailure: the fatal reason, when the engine died during bootstrap.
    ///   - defaults: the `defaults` block from `status`: `provider`, `model`, `context_window`,
    ///     `window_source`, `reason`.
    ///   - providers: the configured providers, so "no provider at all" reads differently from
    ///     "a provider is configured but no default names it" — the two need different next actions.
    ///   - projectConfirmed: whether the person has said which folder to work in.
    ///   - postureChosen: whether the person has chosen the autonomy for goals created here.
    public static func gate(engineIsRunning: Bool,
                            engineFailure: String? = nil,
                            defaults: [String: JSONValue],
                            providers: [[String: JSONValue]],
                            projectConfirmed: Bool,
                            postureChosen: Bool) -> SetupGate {
        guard engineIsRunning else {
            return .engineUnavailable(reason: engineFailure)
        }

        let provider = defaults["provider"]?.stringValue ?? ""
        let model = defaults["model"]?.stringValue ?? ""
        let reason = defaults["reason"]?.stringValue ?? ""

        if providers.isEmpty {
            return .needsModel(why: "No model can be reached because no provider is configured yet. "
                + "Add an endpoint — an OpenAI-compatible host, Anthropic, or a local Ollama — and "
                + "test it to fetch its models.")
        }
        if provider.isEmpty || model.isEmpty {
            // A provider that exists but no default names is the commonest first-run state: the
            // engine starts fine on `credentials.example.json`, so nothing looks broken while no
            // agent can be bound. The reason the engine gave is repeated when it gave one.
            return .needsModel(why: reason.isEmpty
                ? "A provider is configured, but no default model is set, so no agent can be hired "
                    + "onto one. Pick the model everyone should use."
                : reason)
        }

        // A window is what an agent binds to. The engine resolves it from the catalog first and the
        // declared table second; when neither knows it, `defaults.context_window` is absent and a
        // hire is refused — so this is a real gate, not a formality.
        let window = defaults["context_window"]?.intValue
        if window == nil || (window ?? 0) <= 0 {
            let source = defaults["window_source"]?.stringValue ?? ""
            let note = source.isEmpty ? "" : " (the engine looked: \(source))"
            return .needsWindow(
                why: "\(provider)/\(model) has no known context window\(note), and an agent cannot "
                    + "be bound to a model whose window is unknown. Test the provider so its model "
                    + "list is read, or choose a model the provider reports.",
                provider: provider, model: model)
        }

        guard projectConfirmed else {
            return .needsProject(why: "Choose the folder the agents should work in. A folder of your "
                + "own is edited directly; a managed project is one the engine owns.")
        }
        guard postureChosen else {
            return .needsAutonomy(why: "Say how much a goal you set here may decide on its own. "
                + "Unattended lets the engine release the gates it is authorised to release; "
                + "Supervised waits for you at every one.")
        }
        return .ready
    }

    /// The one sentence the spine shows while something is still missing.
    ///
    /// Separate from `gate.detail` because the spine is a single line and the wizard has room for a
    /// paragraph: the spine names the step, the wizard explains it.
    public static func spineHint(for gate: SetupGate) -> String? {
        switch gate {
        case .ready: return nil
        case .engineUnavailable(let reason):
            return reason == nil ? "Start the engine to run anything" : "The engine could not start"
        case .needsModel: return "No default model yet — choose one in Setup"
        case .needsWindow: return "The default model has no known window — fix it in Setup"
        case .needsProject: return "Choose a project in Setup"
        case .needsAutonomy: return "Choose the default autonomy in Setup"
        }
    }
}

/// The whole first-run path, as a value the wizard can show at once.
///
/// WHY THE WHOLE PATH AND NOT JUST THE CURRENT STEP
/// -----------------------------------------------
/// The audit's third failing rule was "whole journey visible". The wizard showed one step at a time and
/// nothing else, so a person could not see how many steps there were, what each was for, where they
/// were in the sequence, or what finishing would get them. "Three things have to be true" is only
/// reassuring if you can see all three and watch them go green.
///
/// So this is derived **from the same `SetupGate`** rather than re-deciding readiness: the gate remains
/// the one authority on what is missing, and each step's `satisfied` is the single comparison
/// `gate.isPast(step)`. Duplicating the readiness predicates here would be a second answer to "is this
/// done" that could disagree with the one the wizard obeys — and a checklist that says a step is
/// finished while the wizard still blocks on it is worse than no checklist.
///
/// The *words* (purpose, what it unlocks) are app copy, not engine policy: they are here rather than in
/// a view body so a test can assert every step explains itself, which is the audit finding itself.
public enum SetupJourney {

    /// Where a step stands, as a value rather than as a colour.
    ///
    /// The audit's rule was that a state must not be signalled by colour alone, and the rail used to
    /// answer that inside its own body — a `(symbol, tone, word)` tuple computed in the view, which is
    /// a fact no test can reach and which the next caller would have had to re-derive. The word and
    /// the glyph live here, so "a done step and an unfinished one are distinguishable without colour"
    /// is an assertion rather than an intention. Only the *tone* stays in the view, because colour is
    /// the one part of it the model has no business choosing.
    public enum StepState: String, Sendable, CaseIterable {
        /// Behind the person: the gate no longer blocks on it.
        case done
        /// The step in hand — the one the gate is blocked on, or the one being looked at.
        case current
        /// Still ahead, and not yet answerable in the order the engine gave.
        case upcoming

        /// The word printed beside the row's title, so the tick is never the only signal.
        public var word: String {
            switch self {
            case .done: return "done"
            case .current: return "in progress"
            case .upcoming: return "not started"
            }
        }

        /// The glyph, which differs in *shape* for each state rather than only in tint.
        public var symbol: String {
            switch self {
            case .done: return "checkmark.circle.fill"
            case .current: return "circle.inset.filled"
            case .upcoming: return "circle"
            }
        }

        public var isDone: Bool { self == .done }
    }

    /// One step of the path, as the rail renders it.
    public struct Step: Sendable, Identifiable, Equatable {
        /// Stable across builds, because it is also the key the engine's own `journey` reports use —
        /// so a step that arrives from the engine can be matched to the row that draws it.
        public let id: String
        /// One-based position, for "Step 2 of 4".
        public let number: Int
        public let title: String
        /// What this step is *for*, in the person's terms.
        public let purpose: String
        /// What becomes possible once it is done — the audit's "what completing it unlocks".
        public let unlocks: String
        /// Where the step stands. **Stored, and the only stored answer** — `satisfied` and `isCurrent`
        /// are derived from it, so a row cannot draw a tick beside the word "not started".
        public let state: StepState
        /// The one command that resolves it at a terminal, or nil when only this window can.
        public let resolution: String?
        public let symbol: String

        /// Whether the step is behind the person.
        public var satisfied: Bool { state.isDone }
        /// Whether this is the step in hand.
        public var isCurrent: Bool { state == .current }
    }

    /// Every step, in dependency order, with the current one marked.
    ///
    /// A computed list rather than a stored one: the state changes under it as the engine reports
    /// back, and a stored copy would be the stale one.
    public static func steps(for gate: SetupGate) -> [Step] {
        let current = effectiveCurrentId(for: gate)
        return all.enumerated().map { index, template in
            Step(id: template.id, number: index + 1, title: template.title,
                 purpose: template.purpose, unlocks: template.unlocks,
                 state: state(of: template.id, satisfied: gate.isPast(id: template.id), current: current),
                 resolution: template.resolution, symbol: template.symbol)
        }
    }

    /// The step a person should act on: the gate's own id, or the first one still ahead.
    ///
    /// The fallback is what `engineUnavailable` needs. That gate names no step id — nothing can be
    /// asked until the engine starts — but the *first* step is plainly the one to act on, and a rail
    /// that marked nothing current would leave a person with no idea where to begin. Deciding it here
    /// rather than in the rail is what stops the view and the model holding two answers to "which step
    /// is this": the rail used to compute the same fallback itself, and a row's word came from one
    /// answer while its mark came from the other.
    public static func effectiveCurrentId(for gate: SetupGate) -> String? {
        if let named = currentId(for: gate) { return named }
        // `ready` answers `isPast` true for every step, so this is nil there without a special case.
        return all.map(\.id).first { !gate.isPast(id: $0) }
    }

    /// Where a step stands, decided once.
    ///
    /// `done` wins over `current` because both answers come from the one gate: a step the gate reports
    /// satisfied is behind the person even when the gate's own `stepId` names it, and drawing it as
    /// "in progress" would be the checklist arguing with the wizard it obeys.
    private static func state(of id: String, satisfied: Bool, current: String?) -> StepState {
        if satisfied { return .done }
        return id == current ? .current : .upcoming
    }

    /// The same path, preferring the engine's own answer when it gave one.
    ///
    /// The engine's `journey()` is authoritative because it is the surface the CLI reads too, so the
    /// terminal and this window say one thing. The local list is the fallback for an engine that
    /// predates the field — and the *state* of every step this build recognises always comes from the
    /// live `SetupGate`, so a step cannot be ticked by a stale engine report while the wizard is still
    /// blocked on it.
    ///
    /// Titles and purposes come from the engine; `symbol`, `number` and `resolution` stay local, because
    /// they are presentation and the engine has no view. A step the engine reports that this build does
    /// not know is still shown, so a newer engine's extra precondition is visible rather than invisible.
    public static func steps(for gate: SetupGate, report: SetupJourneyReport?) -> [Step] {
        let local = steps(for: gate)
        guard let report else { return local }
        return report.steps.enumerated().map { index, remote in
            let known = local.first { $0.id == remote.id }
            return Step(
                id: remote.id,
                number: index + 1,
                title: remote.title.isEmpty ? (known?.title ?? remote.id) : remote.title,
                purpose: remote.purpose.isEmpty ? (known?.purpose ?? "") : remote.purpose,
                unlocks: remote.unlocks.isEmpty ? (known?.unlocks ?? "") : remote.unlocks,
                // The live gate wins over the report: the gate is what the wizard obeys right now.
                state: known?.state ?? (remote.satisfied ? .done : .upcoming),
                resolution: remote.resolves ?? known?.resolution,
                symbol: known?.symbol ?? "circle")
        }
    }

    /// The step a person is looking at, and whether it is the one still to be answered.
    ///
    /// The rail is not decoration: a row is a control that shows that step, so somebody who has
    /// finished a step can go back and read what it was for. That makes "which step is on screen"
    /// a *second* question beside "which step is next" — and the two must not be confused, or
    /// selecting a finished step would silently show the unfinished one's controls.
    ///
    /// Decided here rather than in the view for the reason the rest of this type exists: the fallback
    /// (nothing selected → the step in hand) is a rule, and a rule in a view body is one nobody can
    /// assert. A selection naming a step this build does not have falls back the same way, so a stale
    /// stored selection cannot blank the panel.
    public struct Focus: Sendable, Equatable {
        public let step: Step
        /// Whether this is the step the gate is blocked on — the one with controls under it.
        public let isInHand: Bool
    }

    public static func focus(for gate: SetupGate, selection: String?,
                             report: SetupJourneyReport? = nil) -> Focus? {
        let steps = steps(for: gate, report: report)
        guard !steps.isEmpty else { return nil }
        let inHandId = effectiveCurrentId(for: gate)
        let chosen = selection.flatMap { id in steps.first { $0.id == id } }
            ?? steps.first { $0.id == inHandId }
            ?? steps.first
        guard let chosen else { return nil }
        return Focus(step: chosen, isInHand: chosen.id == inHandId)
    }

    /// The step the person is on, by id, for a rail that marks the current one.
    public static func currentId(for gate: SetupGate) -> String? {
        gate.stepId
    }

    /// The sequence of step ids, in dependency order.
    ///
    /// Read off `all` rather than written twice, so "the order" and "the steps" are one list — and
    /// `SetupGate.isPast(id:)` can compare positions without a second copy that a reorder would miss.
    public static var order: [String] { all.map(\.id) }

    /// How many steps are done, for a progress line.
    public static func completedCount(for gate: SetupGate) -> Int {
        steps(for: gate).filter(\.satisfied).count
    }

    /// The steps themselves, in order. The one place the sequence and its words are written.
    private static let all: [(id: String, title: String, purpose: String, unlocks: String,
                              resolution: String?, symbol: String)] = [
        (id: "engine",
         title: "The engine is running",
         purpose: "The engine is the part that actually talks to a model and does the work. This "
            + "window is only a view of it, so nothing can happen until it is up.",
         unlocks: "Everything else — every step below is answered by the engine, so this one is "
            + "first and unskippable.",
         resolution: "python3 -m engine.cli serve",
         symbol: "power.circle"),

        (id: "model",
         title: "A model answers",
         purpose: "A provider is one API you can already reach — an OpenAI-compatible host, "
            + "Anthropic, or a local Ollama — and the model is the specific one the agents use. Pick "
            + "an endpoint you already hold a key for; a local Ollama needs none, which is the one "
            + "choice that works with no account anywhere.",
         unlocks: "Hiring agents. An agent is bound to a model, so with no model resolved the roster "
            + "is empty and no work can start. You know it worked when the engine reaches the "
            + "endpoint and reads its model list — press Test and a green result means it did. When "
            + "it fails, the reason is the engine's own: a missing key names the variable to set, and "
            + "a URL that was the full endpoint rather than the base is corrected and you are told.",
         resolution: "python3 -m engine.cli defaults set --provider P --model M",
         symbol: "server.rack"),

        (id: "project",
         title: "A project to work in",
         purpose: "The folder the agents read and edit. It is either one of your own — edited "
            + "directly — or a folder the engine creates and keeps to itself.",
         unlocks: "Anything that touches a file, and every record of what happened: the roster, the "
            + "run history and the cache all live inside the project.",
         resolution: "python3 -m engine.cli run --project /path/to/your/folder --goal \"…\"",
         symbol: "folder"),

        (id: "autonomy",
         title: "How much it decides alone",
         purpose: "Unattended lets a goal release the gates the engine is authorised to release and "
            + "finish without you. Supervised waits for you at every gate.",
         unlocks: "Setting a goal. From here the org plans the work, hires what it needs, runs it, "
            + "and reports back — with a record you can read afterwards either way.",
         resolution: "python3 -m engine.cli defaults autonomy --posture unattended",
         symbol: "hand.raised"),
    ]
}

/// The engine's own first-run report, decoded from the `journey` field of `status`.
///
/// WHY THIS EXISTS WHEN THE UI CAN COMPUTE THE SAME THING
/// ----------------------------------------------------
/// `SetupJourney` above is evaluated in Swift, from what the engine reported. That works, but it means
/// the *decision* of what needs doing lives in the app: the CLI cannot show a person the same answer,
/// and the two surfaces can drift the first time a precondition changes.
///
/// `engine/onboarding.py::journey()` closes that by answering in the engine — every step with its
/// title, purpose, whether it is satisfied, the one command that resolves it, and what it unlocks — and
/// this decodes that answer so the wizard renders the engine's words. The local `SetupJourney` stays as
/// the fallback for an engine that predates the field (or reports it empty), because a first-run screen
/// that renders *nothing* when the engine is mid-upgrade is worse than one that renders its own copy.
///
/// Not yet present in the engine at the time this was written — see `SetupJourney.engineReport` for how
/// the two are reconciled, and the report accompanying this change.
public struct SetupJourneyReport: Sendable, Equatable {

    /// One step exactly as the engine described it.
    public struct Step: Sendable, Identifiable, Equatable {
        public let id: String
        public let title: String
        public let purpose: String
        public let unlocks: String
        public let satisfied: Bool
        /// The one command that resolves it, when the engine can name one.
        public let resolves: String?
    }

    public let steps: [Step]
    /// The engine's own sentence about the whole path, if it offered one.
    public let summary: String

    /// Decode a `status` payload, returning nil when the engine did not report a journey.
    ///
    /// Nil rather than an empty report: "the engine has no opinion" and "the engine says there are no
    /// steps" are different facts, and only the first should fall back to the local copy.
    public init?(status: [String: JSONValue]) {
        guard let raw = status["journey"] else { return nil }
        // Accept either the array itself or an object wrapping it, so a richer engine payload (with a
        // summary line, or a schema version) does not become a silent no-op in this build.
        let list: [JSONValue]
        var summary = ""
        if let array = raw.arrayValue {
            list = array
        } else if let object = raw.objectValue {
            list = object["steps"]?.arrayValue ?? []
            summary = object["summary"]?.stringValue ?? ""
        } else {
            return nil
        }
        let decoded = list.compactMap { entry -> Step? in
            guard let object = entry.objectValue else { return nil }
            let id = object["id"]?.stringValue ?? ""
            // A step with no id cannot be matched to the rail that draws it, and inventing one would
            // silently mis-place its tick.
            guard !id.isEmpty else { return nil }
            return Step(
                id: id,
                title: object["title"]?.stringValue ?? "",
                purpose: object["purpose"]?.stringValue ?? "",
                unlocks: object["unlocks"]?.stringValue ?? "",
                satisfied: object["satisfied"]?.boolValue ?? false,
                resolves: (object["resolves"] ?? object["resolution"] ?? object["command"])?.stringValue)
        }
        guard !decoded.isEmpty else { return nil }
        self.steps = decoded
        self.summary = summary
    }
}

/// The choices the app itself remembers, as opposed to the engine's configuration file.
///
/// Two of the first-run answers are the *app's*, not the engine's, and it matters which is which:
///
/// - **The default autonomy the app applies to a goal it creates.** The engine reads its own
///   `goal.default_posture` from `credentials.json`, and there is no command in the protocol that
///   reads or writes it. So the app cannot change the engine's default, and it must not pretend to:
///   it remembers the posture the person chose and sends it on every `goal_set` it issues, which is
///   a claim that is true of every goal set from this window.
/// - **That the project question has been answered.** The engine always has a workspace, so this is a
///   confirmation rather than a discovery — but a confirmation is what makes the wizard finite.
///
/// `UserDefaults` behind an injectable suite, so a test exercises the real code with its own store
/// rather than polluting the user's.
@MainActor
public final class AppPreferences {

    private enum Key {
        static let posture = "setup.goalPosture"
        static let projectConfirmed = "setup.projectConfirmed"
        static let wizardDone = "setup.wizardCompleted"
    }

    private let store: UserDefaults

    /// - Parameter store: defaults to `.standard`. A test passes a suite of its own so the assertions
    ///   are about this type and not about whatever the developer's machine last stored.
    public init(store: UserDefaults = .standard) {
        self.store = store
    }

    /// A store with no persistence, for a preview or a throwaway controller.
    public static func ephemeral() -> AppPreferences {
        AppPreferences(store: UserDefaults(suiteName: "org.agentorg.ephemeral.\(UUID().uuidString)")
            ?? .standard)
    }

    /// The posture a goal set from this app inherits, or nil before one has been chosen.
    ///
    /// Nil rather than defaulting to `unattended` here: the *engine's* default is unattended, but the
    /// wizard must still be able to tell "the person has not answered" from "the person answered
    /// Unattended", because the first is a step the wizard has to show.
    public var chosenPosture: OrgController.Posture? {
        get {
            guard let raw = store.string(forKey: Key.posture) else { return nil }
            // `unknown` is a rendering state, never a choice: a stored value this build cannot read
            // is treated as no choice, so the wizard asks again rather than sending a posture the
            // engine would refuse.
            let posture = OrgController.Posture(rawValue: raw)
            return posture == .unknown ? nil : posture
        }
        set {
            if let newValue, let wire = newValue.wireValue {
                store.set(wire, forKey: Key.posture)
            } else {
                store.removeObject(forKey: Key.posture)
            }
        }
    }

    /// Whether the person has confirmed where the agents should work.
    public var projectConfirmed: Bool {
        get { store.bool(forKey: Key.projectConfirmed) }
        set { store.set(newValue, forKey: Key.projectConfirmed) }
    }

    /// Whether the wizard has ever run to completion.
    ///
    /// Kept separately from "is a run possible right now" because the wizard is supposed to disappear
    /// for good: if a provider is later removed, the spine says so and offers Setup, rather than
    /// throwing the person back into a first-run wizard they have already answered.
    public var wizardCompleted: Bool {
        get { store.bool(forKey: Key.wizardDone) }
        set { store.set(newValue, forKey: Key.wizardDone) }
    }

    /// Forget every app-level choice, so the wizard shows again. For support, and for a test.
    public func reset() {
        store.removeObject(forKey: Key.posture)
        store.removeObject(forKey: Key.projectConfirmed)
        store.removeObject(forKey: Key.wizardDone)
    }
}
