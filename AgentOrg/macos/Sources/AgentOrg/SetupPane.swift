//
//  SetupPane.swift
//  AgentOrg
//
//  Setup, and the first-run wizard that borrows its steps.
//
//  WHY THIS FILE CONTAINS TWO VIEWS THAT LOOK ALIKE
//  ------------------------------------------------
//  The first run is a *gate*: three things must be true before a run is possible, and until they are,
//  the app has nothing to show. Setup is the same three things, editable at leisure, months later.
//  They share the step views below and differ only in framing:
//
//  - `SetupWizardView` is what Now shows instead of itself while `OrgController.showsFirstRunWizard`,
//    and it goes away for good once the answers exist. It is the fix for the audit's biggest finding:
//    the engine starts happily with no credentials (it falls back to `credentials.example.json`), so
//    **nothing looked broken** while no agent could bind — and that only surfaced as an empty hire
//    form two panels away. Nothing looks broken *now* either; the difference is that the wizard says
//    so before the person has to discover it.
//  - `SetupPane` is the same steps as a scrolling destination, always reachable from the sidebar.
//
//  The steps are ordered by dependency and each one refuses to advance until its fact is true:
//  1. **Which model?** provider + model + Test → Save. The test is a real round trip to the endpoint,
//     because only the engine can reach it and a UI that claimed "connected" without asking would be
//     lying about the one thing the button was pressed to find out.
//  2. **Which project?** a folder of the person's own, or a managed project the engine owns.
//  3. **How autonomous?** Unattended or Supervised. This one writes a value the app *sends with every
//     goal it sets*, so the claim is code-true rather than a switch that quietly does nothing —
//     see `OrgController.goalPosturePreference` for why the engine's own default cannot be written
//     from here.

import SwiftUI
import AppKit
import AgentOrgKit

// MARK: - The wizard

/// The three-step gate that replaces Now until a run is actually possible.
///
/// W H Y   T H E   W H O L E   P A T H   I S   O N   S C R E E N
/// -------------------------------------------------------------
/// The audit's third failing rule was "whole journey visible", and its evidence was exact: only the
/// current step was shown, so a person could not see how many steps there were, what each was for,
/// where they were, or what completing it unlocked. "Three things have to be true" is only reassuring
/// if all three are visible and you can watch them go green.
///
/// So the wizard is now a **rail plus a panel**: the rail (`JourneyRail`) draws every step with its
/// state, the panel below carries the controls for the step in hand. The rail comes from
/// `SetupJourney`, with the words preferring the engine's own `journey()` report when there is one, so
/// the terminal and this window say one thing.
///
/// Two accessibility rules the audit found missing are handled here and in `StepCard`:
///
/// - **Reduce Motion** — the rail's step transition animates only when the system allows motion. The
///   audit's finding was that transitions animated unconditionally.
/// - **Reduce Transparency** — the translucent step surface falls back to an opaque one, which is the
///   setting's whole purpose: someone who turned transparency off has asked for solid backgrounds, and
///   translucency here would ignore a request made for readability.
struct SetupWizardView: View {
    @ObservedObject var controller: OrgController

    /// Reduce Motion is a system accessibility setting, and the checklist requires checking it before
    /// *all* animations. Here it decides whether the current step's panel cross-fades in: someone who
    /// asked for less motion still wants to see the new step, just not sliding.
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Which step the person has opened in the rail, or nil for "whatever is in hand".
    ///
    /// Nil is the resting state, so the wizard follows the gate on its own and a person who never
    /// touches the rail sees exactly what they saw before. Touching a row is how someone *looks back*
    /// at a step they have finished — the audit's "whole journey visible" is not satisfied by a rail
    /// you can only read.
    @State private var selection: String?

    private var steps: [SetupJourney.Step] {
        SetupJourney.steps(for: controller.setupGate, report: controller.journey)
    }

    /// The step on screen, with whether it is the one the gate is blocked on.
    private var focus: SetupJourney.Focus? {
        SetupJourney.focus(for: controller.setupGate, selection: selection, report: controller.journey)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                heading
                JourneyRail(steps: steps, selection: selection,
                            onSelect: { id in selection = (selection == id ? nil : id) })
                stepPanel
                // Said at the end rather than the top: the audit's other finding was "paragraphs used
                // as UI", and a wall of explanation above the first question is exactly that. Here it
                // is available the moment a person wonders, and skippable until then.
                HowItWorksCard()
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task {
            await controller.loadWindow()
            // Nothing to ask (a workspace already configured, or one completed in an earlier session)
            // means the wizard retires rather than sitting on "Ready. Setting up your console…" with
            // no step left to press.
            controller.retireWizardIfDone()
        }
        // Re-evaluated as the answers arrive, so the wizard also retires when the *last* step is
        // satisfied by a load rather than by a button — the case that deadlocked on screen.
        .onChange(of: controller.setupGate) { _, _ in controller.retireWizardIfDone() }
    }

    /// What is happening, in one sentence, and where the person is in the sequence.
    private var heading: some View {
        let gate = controller.setupGate
        let done = SetupJourney.completedCount(for: gate)
        let total = steps.count
        return VStack(alignment: .leading, spacing: 6) {
            Label("Let's get this working", systemImage: "wand.and.stars")
                .font(.title2.weight(.semibold))
            Text("Four things have to be true before the org can do anything. The engine starts "
                 + "without them, which is why it is worth doing them here rather than discovering "
                 + "them later. Each one is answered by a control on this screen — the terminal "
                 + "command beside it is the same answer, for anyone who would rather type it.")
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Text("\(done) of \(total) done").font(.caption)
                    .foregroundStyle(done > 0 ? .primary : .secondary)
                ProgressView(value: Double(done), total: Double(max(total, 1)))
                    .progressViewStyle(.linear)
                    .frame(maxWidth: 160)
                    .accessibilityLabel("Setup progress: \(done) of \(total) steps done")
                Spacer()
                Text(gate.title).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.accentColor.opacity(0.08))
        .cornerRadius(8)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Setting up. \(gate.title). \(done) of \(total) steps done.")
    }

    /// The controls for the step in hand — one panel, whichever step that is.
    ///
    /// The step change is animated, and `Reduce Motion` turns that animation **off** rather than
    /// shortening it. The audit's finding was that transitions animated unconditionally; the fix has to
    /// actually drive an animation, or the guard is decoration that suppresses nothing. `.animation(nil)`
    /// means the new step appears instantly, which is what "reduce motion" asks for — not a faster
    /// cross-fade, which is still motion.
    ///
    /// A step opened in the rail that is *not* in hand gets `StepDetail` instead of controls: it is
    /// there to be read, and re-drawing the in-hand panel under a finished step's heading would be a
    /// form labelled with the wrong question.
    @ViewBuilder
    private var stepPanel: some View {
        if let focus, !focus.isInHand {
            StepDetail(step: focus.step, currentTitle: steps.first(where: { $0.isCurrent })?.title,
                       onReturn: { selection = nil })
        } else {
            inHandPanel
            // The current step's *outcome*, from the engine's own words. The rail says what each step
            // unlocks, but the rail is a list a person skims; the step they are working on says it
            // directly under its own controls, which is the one place the audit found it missing.
            if let step = steps.first(where: { $0.isCurrent }) {
                StepOutcome(step: step)
            }
        }
    }

    @ViewBuilder
    private var inHandPanel: some View {
        Group {
            switch controller.setupGate {
            case .engineUnavailable:
                EngineStep(controller: controller)
            case .needsModel, .needsWindow:
                ModelStep(controller: controller)
            case .needsProject:
                ProjectStep(controller: controller, onDone: { controller.confirmProject() })
            case .needsAutonomy:
                AutonomyStep(controller: controller) { controller.completeSetup() }
            case .ready:
                // Reachable for one frame between the last answer and the wizard retiring. Showing
                // the finished state rather than a blank keeps that frame honest — and it is the last
                // chance to say what happens next, which is the audit's "what happens after setup".
                ReadyStep { controller.completeSetup() }
            }
        }
        // One identity per step, so the change is a *change of step* rather than text changing inside
        // one panel — which is what makes "did the step move?" visible at all.
        .id(controller.setupGate.kind.rawValue)
        .transition(.opacity)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.2),
                   value: controller.setupGate.kind.rawValue)
    }
}

/// A step opened in the rail that is not the one in hand: what it is, what it bought, or what it will.
///
/// The audit failed this screen for showing only the current step. A rail with no way back is only
/// half the answer — a person who has just answered "which model" should be able to reopen that row
/// and read what it was for, and someone looking forward should be able to see what the next question
/// is about before reaching it. Both are the same card; only the last line differs, and it says which
/// of the two cases this is rather than leaving the person to infer it from a missing form.
struct StepDetail: View {
    let step: SetupJourney.Step
    /// The step that actually has controls right now, so the way back can name it.
    let currentTitle: String?
    var onReturn: () -> Void

    private var isDone: Bool { step.satisfied }

    var body: some View {
        StepCard(symbol: step.symbol, title: step.title, detail: step.purpose) {
            VStack(alignment: .leading, spacing: 10) {
                Label(step.unlocks, systemImage: "arrow.turn.down.right")
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                if isDone {
                    Label("This step is done.", systemImage: "checkmark.circle.fill")
                        .font(.caption).foregroundStyle(.green)
                } else {
                    Text("This step comes later in the order. It is here so you can see what it asks "
                         + "before you get to it.")
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                // The command is shown for a *finished* step too, unlike the rail's row: this panel is
                // where somebody checks what a step actually took, and that is the answer.
                if let resolution = step.resolution, !resolution.isEmpty {
                    Text(resolution)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                if let currentTitle, !currentTitle.isEmpty {
                    Button {
                        onReturn()
                    } label: {
                        Label("Back to \(currentTitle)", systemImage: "arrow.uturn.backward")
                    }
                    .accessibilityLabel("Back to the step in hand, \(currentTitle)")
                }
            }
        }
    }
}

/// What finishing the step in hand gets you, in the engine's own words.
///
/// A separate card from `StepDetail` because it sits beside a *form* rather than replacing one: it is
/// the one line a person working on a step needs, and it must not turn the step's own controls into a
/// screen to be read. The words are the engine's (`journey()`'s `unlocks`), so the terminal and this
/// window say the same thing about why the step matters.
///
/// The second line answers "how do I know it worked", and it is deliberately the *same* sentence for
/// every step rather than a per-step list. A Swift table of "what to check" would be a second
/// hand-maintained copy of the engine's own preconditions, which is the drift this whole design exists
/// to prevent — and the honest answer is the same one for all four steps: the engine decides, and the
/// rail shows its verdict.
struct StepOutcome: View {
    let step: SetupJourney.Step

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(step.unlocks, systemImage: "arrow.turn.down.right")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Label("This step is done when the engine says so — the mark beside it turns into a tick. "
                  + "Nothing here assumes it worked.",
                  systemImage: "checkmark.seal")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 4)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Finishing this step unlocks: \(step.unlocks). This step is done when the "
                            + "engine says so — the mark beside it turns into a tick.")
    }
}

// MARK: - The journey rail

/// Every step of the path, with where the person is in it.
///
/// A vertical rail rather than a horizontal one because the steps have *titles and purposes*, not just
/// names: a horizontal row of four dots can say "step 2 of 4" and nothing else, which is the state the
/// audit failed the screen for. Each row here carries the number, the title, what it is for, and what
/// it unlocks once done — and a state mark that survives without colour (a filled checkmark versus a
/// ringed dot versus a dimmed circle), because `.green`/`.secondary` alone is invisible to VoiceOver
/// and to a colour-blind reader.
struct JourneyRail: View {
    let steps: [SetupJourney.Step]
    /// The step the person has opened, or nil for "whatever is in hand".
    var selection: String?
    /// Called with a row's id when it is clicked. The wizard toggles, so a second click closes it.
    var onSelect: (String) -> Void

    /// Whether a row is a control at all.
    ///
    /// **Deliberately not every row.** A row is only clickable when pressing it changes what is shown,
    /// and one case changes nothing: the step already in hand. A row that highlights under the pointer
    /// and then does nothing is the audit's "looks tappable but is not", so the affordance is derived
    /// from the same answer the panel uses instead of being drawn on every row for symmetry.
    private func isSelectable(_ step: SetupJourney.Step) -> Bool {
        step.isCurrent != true || selection != nil
    }

    var body: some View {
        // The current step is decided by the model, not re-derived here: `JourneyRail` and `JourneyRow`
        // used to answer "which step is current" separately, so a row could print "not started" beside
        // a filled current dot. `SetupJourney.effectiveCurrentId` is the single answer.
        VStack(alignment: .leading, spacing: 0) {
            ForEach(steps) { step in
                JourneyRow(step: step,
                           isSelected: selection == step.id,
                           isSelectable: isSelectable(step),
                           onSelect: { onSelect(step.id) })
                if step.id != steps.last?.id {
                    Divider().padding(.leading, 30)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardSurface()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Where you are in setup")
    }
}

/// One row of the rail: its state, its number, its title, what it is for, and what it unlocks.
struct JourneyRow: View {
    let step: SetupJourney.Step
    /// Whether this row is the one whose detail is under the rail.
    var isSelected: Bool = false
    /// Whether pressing the row does anything.
    var isSelectable: Bool = true
    var onSelect: () -> Void = {}

    /// The state, as a word and a glyph — never colour alone.
    ///
    /// The word and the glyph both come from the model (`SetupJourney.StepState`), so the promise this
    /// row makes is asserted in `SetupJourneyTests` rather than computed in a view body. Only the tone
    /// is chosen here, because it is the one part that is presentation.
    private var mark: (symbol: String, tone: Color, word: String) {
        switch step.state {
        case .done: return (step.state.symbol, .green, step.state.word)
        case .current: return (step.state.symbol, .accentColor, step.state.word)
        case .upcoming: return (step.state.symbol, .secondary, step.state.word)
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: mark.symbol)
                .font(.title3)
                .foregroundStyle(mark.tone)
                .frame(width: 20)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text("\(step.number).").font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                    Text(step.title).font(.callout.weight(.medium))
                        .foregroundStyle(step.satisfied ? .secondary : .primary)
                    Text(mark.word).font(.caption2)
                        .foregroundStyle(mark.tone)
                    Spacer()
                    if isSelectable {
                        // Named for what it opens rather than for the press, because "Show" reads as
                        // a disclosure the person can undo and "Open" reads as a navigation.
                        Image(systemName: isSelected ? "chevron.down" : "chevron.right")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .accessibilityHidden(true)
                    }
                }
                // Purpose and unlock stay visible for every step, done or not. They were briefly
                // hidden once a step was satisfied, to save space — but that removes exactly what the
                // audit failed this screen for: a person who has finished cannot look back and see
                // what each step was for or what it bought them. Hierarchy comes from tone instead of
                // omission, which is cheaper than a screen that hides its own answer.
                Text(step.purpose)
                    .font(.caption)
                    .foregroundStyle(step.satisfied ? .tertiary : .secondary)
                    .fixedSize(horizontal: false, vertical: true)
                // What it unlocks — the half the old wizard never said at all.
                Label(step.unlocks, systemImage: "arrow.turn.down.right")
                    .font(.caption2)
                    .foregroundStyle(step.satisfied ? .tertiary : .secondary)
                    .fixedSize(horizontal: false, vertical: true)
                // The terminal command is shown only while the step is outstanding, and only as an
                // *alternative* to the control under the rail. It used to be the only instruction the
                // row carried, which read as "go to a terminal" for four steps whose buttons are two
                // inches below — the audit's complaint that a GUI was handing out commands. Labelled
                // "or" so it is plainly the second way, not the required one.
                if !step.satisfied, let resolution = step.resolution, !resolution.isEmpty {
                    HStack(alignment: .firstTextBaseline, spacing: 4) {
                        Text("or").font(.caption2).foregroundStyle(.tertiary)
                        Text(resolution)
                            .font(.system(.caption2, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .lineLimit(2)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Or, at a terminal: \(resolution)")
                }
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 4)
        .background(isSelected ? Color.accentColor.opacity(0.08) : .clear)
        .cornerRadius(6)
        // The hit region covers the whole row including its padding, so a click anywhere on the row
        // opens it rather than only on the title's glyphs.
        .contentShape(Rectangle())
        .onTapGesture { if isSelectable { onSelect() } }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Step \(step.number), \(step.title), \(mark.word). "
                            + "\(step.purpose) Unlocks: \(step.unlocks)")
        .accessibilityValue(isSelected ? "shown below" : "")
        .accessibilityHint(isSelectable ? "Shows this step" : "")
        // A row that is not clickable must not advertise itself as a button to VoiceOver either —
        // that is the same false affordance in a channel a sighted check cannot see.
        .accessibilityAddTraits(isSelectable ? .isButton : [])
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

// MARK: - What actually happens

/// What the org does with a goal, in the words a person would use.
///
/// The audit's fourth failing rule: "explains how it works" — nothing told the person what the org
/// actually does, what a run looks like, or what happens after setup finishes. This is that, placed
/// *after* the controls rather than above them, because the audit's other finding was paragraphs used
/// as UI and a wall of text above the first question is precisely that.
///
/// Always rendered where it is used; the collapsing is the caller's business. In Setup it sits inside a
/// `SectionCard` that is closed until asked for, and in the wizard it is the last thing on a screen
/// whose questions are all above it — so the same card serves both without a second copy, and neither
/// caller has to re-derive which of the two shapes it wants.
struct HowItWorksCard: View {
    private let phases: [(symbol: String, title: String, detail: String)] = [
        ("text.badge.checkmark",
         "You set one goal",
         "A sentence in plain words — “document how we could capture a wider market”. That is the "
            + "only thing you have to write."),
        ("arrow.triangle.branch",
         "It plans the work",
         "The engine turns the goal into a small graph of steps and shows it to you before anything "
            + "runs, unless your autonomy setting says otherwise."),
        ("person.3.sequence",
         "It hires and runs",
         "Each step goes to an agent bound to your model. Agents write into the project folder and "
            + "leave a handoff document at every crossing, so you can read who decided what."),
        ("checkmark.seal",
         "You get files and a record",
         "The result lands in the project, with the run's own history beside it in .agent_state/ — "
            + "the trace, the handoffs, the cost and anything the engine had to ask about."),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(phases, id: \.title) { phase in
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: phase.symbol)
                        .font(.title3).foregroundStyle(.secondary).frame(width: 22)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(phase.title).font(.callout.weight(.medium))
                        Text(phase.detail).font(.caption).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(phase.title). \(phase.detail)")
            }
            // The honest limit, said here rather than discovered. A person who chose Unattended
            // expecting no interruptions at all should know what still stops a run.
            Label("Supervised or not, a run still stops if the engine has to change something "
                  + "outside the project or cannot reach the model. Those are the gates you see "
                  + "on Now.",
                  systemImage: "hand.raised")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, 8)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("How it works")
    }
}


/// Step one, when the engine itself is the problem.
struct EngineStep: View {
    @ObservedObject var controller: OrgController

    /// Whether a launch is already in flight.
    ///
    /// The gate answers `.engineUnavailable` for anything short of readiness, so while the bridge was
    /// launching this step read "The engine has not started yet, so there is nothing to ask it" and
    /// offered a Start button — at the very moment the sidebar said "Working…". Two surfaces, one
    /// process, opposite accounts of it. The button was worse than the sentence: `launch()` returns
    /// early while the state is live, so pressing it did nothing at all.
    private var isLaunching: Bool { controller.engineState == .launching }

    /// What to say while the launch runs. The console's own words, for the same reason every other
    /// figure in this app is the engine's: a second account of one launch is a second thing to drift.
    private var launchDetail: String {
        controller.waitingAdvice
            ?? "The engine is starting. This step becomes the model question as soon as it reports ready."
    }

    var body: some View {
        StepCard(symbol: "power.circle",
                 title: isLaunching ? "Starting the engine" : controller.setupGate.title,
                 detail: isLaunching ? launchDetail : controller.setupGate.detail) {
            HStack(spacing: 8) {
                if isLaunching {
                    // No control here, because there is nothing to control: what the person needs is the
                    // state and the deadline it is being held to, not a button that cannot act.
                    Label(controller.launchProgress?.summary(now: Date())
                            ?? "Waiting for the engine to report ready",
                          systemImage: "hourglass")
                        .font(.callout).foregroundStyle(.secondary)
                } else {
                    Button("Start the engine") { controller.launch() }
                        .buttonStyle(.borderedProminent)
                        .disabled(!controller.canLaunch)
                        .accessibilityLabel("Start the engine")
                }
                if let problem = controller.runtimeProblem {
                    // The reason, not the fact: a first-run step that cannot explain a missing
                    // interpreter is not a first-run step.
                    Text(problem).font(.caption).foregroundStyle(.orange)
                        .textSelection(.enabled)
                }
            }
        }
    }
}

// MARK: - Step 1: which model

/// Provider, model, test, save — in that order, because that is the order that cannot leave a broken
/// endpoint in the config.
struct ModelStep: View {
    @ObservedObject var controller: OrgController

    /// Whether the pair resolves but its window does not — a different problem from "no model at all",
    /// and the symbol says which one a person is looking at.
    private var isWindowProblem: Bool { controller.setupGate.kind == .needsWindow }

    var body: some View {
        StepCard(symbol: isWindowProblem ? "gauge" : "server.rack",
                 title: controller.setupGate.title,
                 detail: controller.setupGate.detail) {
            VStack(alignment: .leading, spacing: 12) {
                ProviderEditor(controller: controller,
                               onSaved: { Task { await chooseDefaults() } })
                if !controller.providers.isEmpty {
                    Divider()
                    DefaultPicker(controller: controller) { Task { await chooseDefaults() } }
                }
            }
        }
    }

    /// After a provider is saved, immediately offer the model it reported as the default — the step's
    /// whole point is that the pair resolves, and stopping at "provider added" would leave the person
    /// one discovery away from a runnable state.
    private func chooseDefaults() async {
        await controller.reloadProviderAndModels()
    }
}

/// Add or update an endpoint. Shared by the wizard and Setup, so there is one form to keep right.
struct ProviderEditor: View {
    @ObservedObject var controller: OrgController
    var onSaved: () -> Void

    @State private var draft = ProviderDraft()
    @State private var editing: String?
    @State private var testResult: [String: JSONValue] = [:]
    /// The engine's reply to a save that has happened, as opposed to a test. Kept separately because
    /// the two say different things: a test reports a model count, a save reports the base the engine
    /// stored. See `save()` below for the case this covers.
    @State private var savedResult: [String: JSONValue] = [:]
    @State private var editingExisting = false
    /// The provider a person pressed Remove on, held while the confirmation is on screen.
    @State private var pendingRemoval: [String: JSONValue]?
    /// The engine's reply to a removal that has happened: which endpoint went, and which roster agents
    /// still name it. Published by `removeProvider` into the controller, then read here — see
    /// `removalResultView`.
    @State private var removalResult: [String: JSONValue] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // **Outside the `fields` branch on purpose.** A save that needed no correction hides the
            // form (the provider list takes over), so a note rendered inside it would vanish in the
            // very case it exists for — the person who pasted a full endpoint would be told nothing.
            saveResultView
            removalResultView
            if controller.providers.isEmpty || editingExisting {
                fields
            } else {
                // The configured endpoints first, with one action to add another. A wizard that showed
                // an empty form to somebody who already has a working provider would make them
                // re-enter what the engine already knows.
                // Keyed on the provider's own `id`. The entry carries `status`, `error` and `models`,
                // all of which move when a provider is tested — under `id: \.self` a test replaced
                // every row in this list.
                ForEach(controller.providers.map { (key: $0["id"]?.stringValue ?? "", provider: $0) },
                        id: \.key) { row in
                    ProviderRow(provider: row.provider,
                                onEdit: { beginEdit(row.provider) },
                                onRemove: { pendingRemoval = row.provider })
                }
                Button {
                    editingExisting = true
                    reset()
                } label: {
                    Label("Add another endpoint", systemImage: "plus.circle")
                }
                .accessibilityLabel("Add another provider")
            }
        }
        // **The removal names the endpoint and says what else moves.** The engine prunes
        // `concurrency.per_provider_limits[pid]`, `defaults.provider` and `defaults.reviewer.provider`
        // when they named this endpoint, and `default_pair()` then resolves a *different* provider with
        // its own reason. So removing an endpoint a person did not think was load-bearing can change
        // what every agent runs on — the one consequence here that is not obvious from the button, and
        // the reason the confirmation reads the default pair back rather than just asking "are you
        // sure". The endpoint's last provider cannot be removed at all: the engine refuses, because no
        // launch could read a document with no providers, and `mutate` shows that reason.
        .alert("Remove “\(pendingRemoval?["id"]?.stringValue ?? "this provider")”?",
               isPresented: Binding(
                get: { pendingRemoval != nil },
                set: { if !$0 { pendingRemoval = nil } })) {
            Button("Remove", role: .destructive) {
                let id = pendingRemoval?["id"]?.stringValue ?? ""
                pendingRemoval = nil
                Task { await remove(id) }
            }
            Button("Cancel", role: .cancel) { pendingRemoval = nil }
        } message: {
            Text(providerRemovalMessage)
        }
    }

    /// What removing this endpoint takes with it, built from the engine's own resolution.
    ///
    /// `defaults.provider` is read from the *live* defaults report rather than from the provider entry,
    /// because the engine reports the resolved pair and the declared one separately and they can differ
    /// — an engine that had to repair a dangling default would report a pair the file does not name.
    ///
    /// **It does not claim which agents break.** That is a fact about the roster, and only the engine
    /// knows it — `_cmd_provider_remove` computes it after the removal and returns the names
    /// (`agents`/`agent_count`). A sentence written here could only assert it without naming anybody,
    /// which is exactly the claim this panel no longer makes: `removalResultView` reports the engine's
    /// answer instead.
    private var providerRemovalMessage: String {
        let id = pendingRemoval?["id"]?.stringValue ?? "This endpoint"
        var lines = ["\(id) is deleted from credentials.json, along with its key and any per-provider "
                     + "concurrency limit. It disappears from the model picker immediately."]

        let defaultProvider = controller.defaults["provider"]?.stringValue ?? ""
        if defaultProvider == id {
            // The declared default is *named* by this provider, so the engine will drop the key. The
            // successor is the engine's answer, not a guess — and when it cannot name one, saying that
            // is more useful than naming a placeholder.
            let successor = controller.defaults["model"]?.stringValue ?? ""
            lines.append("**This is the default provider.** Removing it drops `defaults.provider`, and "
                         + "the engine will resolve a different one"
                         + (successor.isEmpty ? "." :
                            " — currently \(successor), chosen by the engine, not by you."))
        } else if !defaultProvider.isEmpty {
            lines.append("Your default endpoint (\(defaultProvider)) is not this one and is unaffected.")
        }
        return lines.joined(separator: "\n\n")
    }

    /// The engine's report on a removal that happened, including which agents it broke.
    ///
    /// **The names come from `provider_remove`'s own reply, not from prose written here.** A roster
    /// agent bound to an endpoint keeps that binding (the roster is re-written verbatim), so a removal
    /// does not re-point it — the agent simply stops being callable, and only the engine can say which
    /// agents those are. `agents`/`agent_count` is its answer, computed from the live roster; before
    /// this the panel asserted the consequence in a sentence of its own with no names in it.
    ///
    /// Shown only when a removal has happened. A refusal (the engine rejects removing the last
    /// provider) surfaces through `mutate`'s notice, and `removeProvider` clears this first so a
    /// refused attempt cannot leave an earlier removal's report looking like its consequence.
    @ViewBuilder
    private var removalResultView: some View {
        if !removalResult.isEmpty {
            let id = removalResult["removed"]?.stringValue ?? ""
            let bound = (removalResult["agents"]?.arrayValue ?? []).compactMap { $0.objectValue }
            let names = bound.map { entry -> String in
                let name = entry["name"]?.stringValue ?? ""
                let agentId = entry["id"]?.stringValue ?? ""
                return agentId.isEmpty ? name : "\(name) (\(agentId))"
            }
            VStack(alignment: .leading, spacing: 4) {
                Label(id.isEmpty ? "Removed" : "Removed \(id)", systemImage: "checkmark.circle.fill")
                    .font(.callout).foregroundStyle(.green)
                    .accessibilityLabel(id.isEmpty ? "Removed" : "Removed \(id)")
                if !names.isEmpty {
                    Text("\(names.count) agent(s) still name this endpoint and can no longer be "
                         + "called: \(names.joined(separator: ", ")). Re-point each one on Org, or add "
                         + "the endpoint back.")
                        .font(.caption2).foregroundStyle(.orange)
                        .accessibilityLabel("\(names.count) agents bound to the removed endpoint: "
                                            + names.joined(separator: ", "))
                }
            }
        }
    }

    /// Remove the endpoint and reload, so the panel shows the engine's post-removal state.
    private func remove(_ id: String) async {
        let ok = await controller.removeProvider(id: id)
        // The reply is read from the controller, where `removeProvider` published it, and only when the
        // removal actually happened — a refused one has its reason in a notice and nothing to report.
        removalResult = ok ? controller.providerRemoval : [:]
        // The default pair is re-read by `removeProvider`; refreshing the catalog too keeps the model
        // picker from offering models that belonged to the endpoint just deleted.
        await controller.reloadProviderAndModels()
    }

    private var fields: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                TextField("id (e.g. groq)", text: $draft.id)
                    .textFieldStyle(.roundedBorder)
                    .disabled(editing != nil)
                    .accessibilityLabel("Provider id")
                Picker("", selection: $draft.kind) {
                    Text("OpenAI-compatible").tag("openai")
                    Text("Anthropic").tag("anthropic")
                    // Named for where it points, not for the vendor. "Ollama" was ambiguous enough to
                    // send someone adding Ollama's *cloud* (`https://ollama.com/v1`, which speaks the
                    // OpenAI dialect) to the `ollama` kind, which targets a **local server's**
                    // `/api/chat` route. The result was `…/chat/completions/api/chat` — a 404 that read
                    // as a broken key or URL, when the only wrong thing was the picker's label.
                    Text("Local Ollama (api/chat)").tag("ollama")
                }
                .labelsHidden()
                .frame(width: 200)
                .accessibilityLabel("Provider kind")
            }

            TextField("base_url (e.g. https://api.groq.com/openai/v1)", text: $draft.baseURL)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("Provider base URL")
                .help("The base, not the full endpoint — for OpenAI-compatible, the part ending in /v1")

            HStack(spacing: 8) {
                SecureField("api_key (or leave blank and use a variable)", text: $draft.apiKey)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("API key")
                TextField("api_key_env (e.g. GROQ_API_KEY)", text: $draft.apiKeyEnv)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Environment variable holding the key")
            }
            // One line of guidance. The old form spent three paragraphs here, which is the audit's
            // "paragraphs are used as UI" — the field's own label says most of it.
            Text("A variable is safer: it is not read into logs or traces. A key typed here is written "
                 + "to credentials.json, which is 0600 and gitignored.")
                .font(.caption2).foregroundStyle(.secondary)

            HStack(spacing: 8) {
                Button {
                    Task { await runTest() }
                } label: {
                    Label("Test", systemImage: "bolt.horizontal.circle")
                }
                .disabled(!draft.isComplete || controller.engineState != .running)
                .help("Ask the endpoint for its model list before saving anything")
                .accessibilityLabel("Test this provider and fetch its models")
                Button {
                    Task { await save() }
                } label: {
                    Label(editing == nil ? "Save" : "Update", systemImage: "square.and.arrow.down")
                }
                .disabled(!draft.isComplete || controller.engineState != .running)
                .accessibilityLabel(editing == nil ? "Save the provider" : "Update the provider")

                if editing != nil || editingExisting {
                    Button("Cancel") {
                        reset()
                        editingExisting = !controller.providers.isEmpty
                    }
                    .accessibilityLabel("Cancel editing")
                }
                Spacer()
            }

            testResultView
        }
    }

    @ViewBuilder
    private var testResultView: some View {
        if !testResult.isEmpty {
            let ok = testResult["ok"]?.boolValue ?? false
            let count = testResult["model_count"]?.intValue ?? 0
            let reason = testResult["reason"]?.stringValue ?? ""
            let note = testResult["note"]?.stringValue ?? ""
            VStack(alignment: .leading, spacing: 4) {
                Label(ok ? "Connected — \(count) model(s)" : "Not usable",
                      systemImage: ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(ok ? .green : .orange)
                    .accessibilityLabel(ok ? "Connected, \(count) models"
                                        : "Not usable: \(reason)")
                // Said plainly, because it changes the URL the form holds: a bare "connected" would
                // leave the operator believing the endpoint they pasted was used verbatim.
                if !note.isEmpty {
                    Label(note, systemImage: "wand.and.stars")
                        .font(.caption2).foregroundStyle(.blue)
                        .accessibilityLabel("URL adjusted: \(note)")
                }
                if !reason.isEmpty {
                    Text(reason).font(.caption2).foregroundStyle(.secondary).lineLimit(3)
                }
            }
        }
    }

    /// What a save stored, when the engine had to change something to make it work.
    ///
    /// The case this exists for is the one reported: somebody pastes provider `base_url`
    /// `https://ollama.com/v1/chat/completions` with kind `openai`. The engine is right to accept it —
    /// it strips `/chat/completions` on the way in (`config.normalize_base_url`), because a base is the
    /// part *before* the call — and the save succeeds. But the form held a URL the stored config does
    /// not, and before this the only evidence was a provider that quietly worked.
    ///
    /// `runTest()` already writes the corrected base back into the field, so pressing Test first is
    /// *told* rather than silently corrected. This is the same statement for a person who never pressed
    /// Test: the save is not gated behind it (a test is a network round trip the endpoint may refuse,
    /// and refusing to save over that would make the form unusable offline), so the correction is what
    /// the form owes them instead.
    @ViewBuilder
    private var saveResultView: some View {
        // Not shown while a test result is on screen: the two would sit one above the other saying
        // overlapping things about the same URL, and the test's line is the more specific of the two.
        if !savedResult.isEmpty && testResult.isEmpty {
            let note = savedResult["note"]?.stringValue ?? ""
            let stored = savedResult["base_url"]?.stringValue ?? ""
            VStack(alignment: .leading, spacing: 4) {
                Label("Saved", systemImage: "checkmark.circle.fill")
                    .font(.callout).foregroundStyle(.green)
                    .accessibilityLabel("Saved")
                if !note.isEmpty {
                    Label(note, systemImage: "wand.and.stars")
                        .font(.caption2).foregroundStyle(.blue)
                        .accessibilityLabel("URL adjusted when saving: \(note)")
                } else if !stored.isEmpty {
                    // The plain case, said anyway: a person who never pressed Test has had no other
                    // statement of *where* their endpoint was recorded, and "Saved" alone leaves them
                    // guessing at a URL the next panel shows differently.
                    Text("base URL stored as \(stored)")
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .accessibilityLabel("Base URL stored as \(stored)")
                }
            }
        }
    }

    private func beginEdit(_ provider: [String: JSONValue]) {
        editing = provider["id"]?.stringValue
        editingExisting = true
        draft = ProviderDraft(existing: provider)
        testResult = [:]
        savedResult = [:]
        removalResult = [:]
    }

    private func reset() {
        editing = nil
        draft = ProviderDraft()
        testResult = [:]
        savedResult = [:]
        removalResult = [:]
    }

    private func runTest() async {
        let result = await controller.testProvider(draft)
        testResult = result
        // A test result supersedes a save's, so the two cannot both be on screen.
        savedResult = [:]
        // Adopt the corrected base so the form shows what will actually be saved. Leaving the pasted
        // full endpoint in the field while the engine used the base would make the two disagree, and
        // the next Save would look like it changed nothing.
        if let corrected = result["base_url"]?.stringValue, !corrected.isEmpty {
            draft.baseURL = corrected
        }
    }

    /// Save, and show what the engine stored.
    ///
    /// **The reply is read before `reset()`, and set after it.** `reset()` blanks the form and clears
    /// every result on screen — which is what "the save is finished with" should do — and the note about
    /// what was just written has to survive that, because it is the only statement a person who never
    /// pressed Test will get that their URL was understood rather than used verbatim.
    ///
    /// `saveProvider` publishes the reply into `controller.providerSave` rather than returning it: the
    /// `Bool` is what the caller acts on, and widening the signature for a value already published
    /// would be a second way to ask the same question.
    private func save() async {
        let ok = await controller.saveProvider(draft)
        let reply = controller.providerSave
        if ok {
            reset()
            savedResult = reply
            onSaved()
        }
        await controller.loadProviders()
    }
}

/// One configured provider, with its reachability and model count.
struct ProviderRow: View {
    let provider: [String: JSONValue]
    let onEdit: () -> Void
    let onRemove: () -> Void

    private var tone: StatusTone { StatusTone.forStatus(provider["status"]?.stringValue ?? "") }

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: provider["has_key"]?.boolValue == true ? "key.fill" : "key")
                .foregroundStyle(provider["has_key"]?.boolValue == true ? .green : .secondary)
                .accessibilityLabel(provider["has_key"]?.boolValue == true
                                    ? "A key is configured" : "No key configured")
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(provider["id"]?.stringValue ?? "?")
                        .font(.system(.body, design: .monospaced))
                    Text(provider["kind"]?.stringValue ?? "")
                        .font(.caption2).foregroundStyle(.secondary)
                    Label(provider["status"]?.stringValue ?? "", systemImage: tone.symbol)
                        .font(.caption2).foregroundStyle(tone.colour)
                        .accessibilityLabel("Status: \(provider["status"]?.stringValue ?? "unknown")")
                }
                Text(provider["base_url"]?.stringValue ?? "")
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Text("\(provider["model_count"]?.intValue ?? 0) model(s)")
                .font(.caption2).foregroundStyle(.secondary)
            Button("Edit", action: onEdit)
                .accessibilityLabel("Edit \(provider["id"]?.stringValue ?? "provider")")
            Button("Remove", action: onRemove)
                .accessibilityLabel("Remove \(provider["id"]?.stringValue ?? "provider")")
        }
        .padding(8)
        .background(tone.colour.opacity(0.06))
        .cornerRadius(6)
        .accessibilityElement(children: .contain)
    }
}

/// Which provider and model everyone uses, with the engine's own resolution shown beside it.
struct DefaultPicker: View {
    @ObservedObject var controller: OrgController
    var onSet: () -> Void

    @State private var provider = ""
    @State private var model = ""
    @State private var seeded = false

    /// The models the chosen provider actually offers, so the model field is a picker when it can be.
    private var modelsForProvider: [String] {
        guard !provider.isEmpty else { return [] }
        let entry = controller.providers.first { $0["id"]?.stringValue == provider }
        let models = (entry?["models"]?.arrayValue ?? []).compactMap {
            $0.objectValue?["model_id"]?.stringValue
        }
        return models.sorted()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("The model everyone runs on").font(.subheadline.weight(.medium))
            HStack(spacing: 8) {
                Picker("Provider", selection: $provider) {
                    Text("Choose…").tag("")
                    // Keyed on `id` for the same reason as the provider list above: a test changes the
                    // entry, and the entry is not the provider's identity.
                    ForEach(controller.providers.map { (key: $0["id"]?.stringValue ?? "", entry: $0) },
                            id: \.key) { row in
                        Text(row.entry["id"]?.stringValue ?? "?").tag(row.entry["id"]?.stringValue ?? "")
                    }
                }
                .frame(maxWidth: 220)
                .accessibilityLabel("Default provider")

                if modelsForProvider.isEmpty {
                    TextField("model id", text: $model)
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 320)
                        .accessibilityLabel("Default model, typed")
                } else {
                    Picker("Model", selection: $model) {
                        Text("Choose…").tag("")
                        ForEach(modelsForProvider, id: \.self) { Text($0).tag($0) }
                    }
                    .frame(maxWidth: 320)
                    .accessibilityLabel("Default model")
                }

                Button("Use this") {
                    let chosenProvider = provider
                    let chosenModel = model
                    Task {
                        await controller.setDefaults(provider: chosenProvider, model: chosenModel)
                        onSet()
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(provider.isEmpty || model.isEmpty || controller.engineState != .running)
                .accessibilityLabel("Use this as the default provider and model")
            }

            // The effective answer, with why. A declared default invalidated by a removed provider
            // resolves to something else — and the person should be told, not left confused.
            HStack(spacing: 8) {
                Label("In use: \(controller.defaultPairLabel)", systemImage: "checkmark.seal")
                    .font(.callout)
                if let window = controller.defaults["context_window"]?.intValue, window > 0 {
                    Text("\(window) tokens").font(.caption2.monospaced()).foregroundStyle(.secondary)
                } else {
                    Label("window unknown — no agent can bind", systemImage: "exclamationmark.triangle")
                        .font(.caption2).foregroundStyle(.orange)
                }
            }
            if let reason = controller.defaults["reason"]?.stringValue, !reason.isEmpty {
                Text("resolved because: \(reason)").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .onAppear(perform: seed)
        .onChange(of: controller.defaults) { _, _ in seed() }
    }

    /// Seed the form from the engine's answer, once — so a poll does not fight what is being typed.
    private func seed() {
        if !seeded {
            provider = controller.defaults["provider"]?.stringValue ?? provider
            model = controller.defaults["model"]?.stringValue ?? model
            seeded = !provider.isEmpty
        }
    }
}

// MARK: - Step 2: which project

/// Where the agents work, with the consequence of each choice said plainly.
struct ProjectStep: View {
    @ObservedObject var controller: OrgController
    var onDone: () -> Void

    var body: some View {
        StepCard(symbol: "folder", title: controller.setupGate.title,
                 detail: controller.setupGate.detail) {
            VStack(alignment: .leading, spacing: 10) {
                // The two options are described as consequences rather than as folders, because that
                // is the difference that matters: one edits the person's own files.
                choice(
                    symbol: "folder.badge.checkmark",
                    title: "Your own code",
                    detail: "The agents work in a folder you choose — a repository you already have. "
                        + "They edit it directly, and the engine keeps its own records in "
                        + ".agent_state/ inside it.",
                    selected: controller.workspace["attached"]?.boolValue == true,
                    action: { openProjectPicker(controller: controller, then: onDone) })

                choice(
                    symbol: "shippingbox",
                    title: "A project the engine owns",
                    detail: "A folder under AgentOrg/projects/ that the engine creates and manages. "
                        + "Nothing of yours is touched.",
                    selected: controller.workspace["attached"]?.boolValue != true,
                    action: {
                        Task {
                            await controller.detachProject()
                            onDone()
                        }
                    })

                HStack(spacing: 8) {
                    Text(controller.projectPath)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .lineLimit(1)
                    Spacer()
                    Button("Choose a folder…") { openProjectPicker(controller: controller, then: onDone) }
                        .disabled(!controller.canLaunch)
                        .accessibilityLabel("Choose a folder the agents should work in")
                }
            }
        }
    }

    @ViewBuilder
    private func choice(symbol: String, title: String, detail: String,
                        selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(selected ? .green : .secondary)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.callout.weight(.medium))
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                if selected {
                    Text("in use").font(.caption2).foregroundStyle(.green)
                }
            }
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? Color.green.opacity(0.08) : Color.secondary.opacity(0.05))
            .cornerRadius(6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(title). \(detail)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

// MARK: - Step 3: how autonomous

/// The autonomy a goal set here inherits, and the engine's own default that the journey checks.
///
/// **Two writes, one control.** Choosing here records the app's preference *and* tells the engine, via
/// `autonomy_set`'s `posture` key. Both are needed and neither substitutes for the other: the
/// preference is what the window attaches to the goals it creates, and the engine's
/// `goal.default_posture` is what `onboard` reads to decide whether this step is done. Until the
/// engine was told, the wizard could retire while the engine still reported "next: Choose how much it
/// decides alone" — and a person who re-ran setup was asked the same question again with nothing on
/// screen saying why.
///
/// So the step refuses to finish on a failed write, and says so. A "Finish" that moved on while the
/// engine disagreed would be the same lie in a different place.
struct AutonomyStep: View {
    @ObservedObject var controller: OrgController
    var onDone: () -> Void

    /// Whether the engine has accepted the choice. Nil until one is made, so "not answered yet" and
    /// "answered but refused" are not drawn as the same state.
    @State private var recorded: Bool?

    var body: some View {
        StepCard(symbol: "hand.raised", title: controller.setupGate.title,
                 detail: controller.setupGate.detail) {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(OrgController.Posture.choices) { posture in
                    Button {
                        Task { recorded = await controller.choosePosture(posture) }
                    } label: {
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: controller.goalPosturePreference == posture
                                  ? "largecircle.fill.circle" : "circle")
                                .foregroundStyle(controller.goalPosturePreference == posture
                                                 ? .green : .secondary)
                                .accessibilityHidden(true)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(posture.label).font(.callout.weight(.medium))
                                Text(posture.explanation)
                                    .font(.caption).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                        }
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(controller.goalPosturePreference == posture
                                    ? Color.green.opacity(0.08) : Color.secondary.opacity(0.05))
                        .cornerRadius(6)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(posture.label). \(posture.explanation)")
                    .accessibilityAddTraits(controller.goalPosturePreference == posture
                                            ? .isSelected : [])
                }
                // What was actually done, said in the two parts it is made of. The first sentence is
                // the app's own claim about the goals it creates; the second is the engine's record,
                // which is the one the checklist above is ticking.
                Text("Every goal you set from this window will use this. It is also written to the "
                     + "engine as the default for new goals, which is what marks this step done.")
                    .font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Button("Finish") { onDone() }
                        .buttonStyle(.borderedProminent)
                        .disabled(controller.goalPosturePreference == nil || recorded != true)
                        .accessibilityLabel("Finish setup")
                    if controller.goalPosturePreference == nil {
                        Text("Choose one to continue.").font(.caption2).foregroundStyle(.secondary)
                    } else if recorded == false {
                        // The engine refused, so the step will still show as outstanding. Say it here
                        // rather than let the person discover it by re-running setup and being asked
                        // the same question with no explanation.
                        Label("The engine has not recorded this, so the step above will still show "
                              + "as outstanding. \(controller.notice ?? "")",
                              systemImage: "exclamationmark.triangle")
                            .font(.caption2).foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }
}

// MARK: - Setup, as a destination

/// The same steps, editable at leisure — plus the two cards that are not steps.
///
/// The order here is `SetupSection.allCases`, and the step sections are titled from it rather than
/// hand-written, so the destination and the wizard cover the same ground: a test asserts every journey
/// step has a section here, which is the drift the audit found between the two.
struct SetupPane: View {
    @ObservedObject var controller: OrgController
    @AppStorage("setup.showAdvanced") private var showAdvanced = false
    @AppStorage("setup.showHowItWorks") private var showHowItWorks = false

    private var steps: [SetupJourney.Step] {
        SetupJourney.steps(for: controller.setupGate, report: controller.journey)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                status
                // The same rail the wizard shows. A person who opens Setup to fix one thing can see
                // what else is outstanding without re-walking the wizard.
                SectionCard(title: SetupSection.journey.rawValue, symbol: SetupSection.journey.symbol,
                            summary: SetupSection.journey.summary) {
                    VStack(alignment: .leading, spacing: 0) {
                        // Not selectable here: this destination already shows every step's sections in
                        // full below, and a row that opened a detail card *above* them would be a
                        // second copy of the answer rather than a way to reach it.
                        ForEach(Array(steps.enumerated()), id: \.element.id) { index, step in
                            JourneyRow(step: step, isSelected: false, isSelectable: false)
                            if index != steps.count - 1 { Divider().padding(.leading, 30) }
                        }
                        if controller.journey == nil {
                            // Said plainly rather than left as a silent difference: on an engine that
                            // does not report a journey, these steps are this app's own copy — and the
                            // tick marks still come from the engine's live readiness, so they are true.
                            Text("These steps are this window's own list; this engine does not report "
                                 + "its own. The ticks still come from the engine's readiness.")
                                .font(.caption2).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(.top, 8)
                        }
                    }
                }
                SectionCard(title: SetupSection.model.rawValue, symbol: SetupSection.model.symbol,
                            summary: SetupSection.model.summary) {
                    VStack(alignment: .leading, spacing: 12) {
                        ProviderEditor(controller: controller, onSaved: {})
                        if !controller.providers.isEmpty {
                            Divider()
                            DefaultPicker(controller: controller, onSet: {})
                        }
                        if !controller.providersConfigPath.isEmpty {
                            Text("Saved to \(controller.providersConfigPath) (mode 0600). Keys are "
                                 + "never sent back to this window.")
                                .font(.caption2).foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                        stepOutcome(for: "model")
                    }
                }
                SectionCard(title: SetupSection.project.rawValue, symbol: SetupSection.project.symbol,
                            summary: SetupSection.project.summary) {
                    VStack(alignment: .leading, spacing: 10) {
                        ProjectChoice(controller: controller)
                        stepOutcome(for: "project")
                    }
                }
                SectionCard(title: SetupSection.autonomy.rawValue,
                            symbol: SetupSection.autonomy.symbol,
                            summary: SetupSection.autonomy.summary) {
                    VStack(alignment: .leading, spacing: 10) {
                        AutonomyStep(controller: controller, onDone: {})
                        stepOutcome(for: "autonomy")
                    }
                }
                SectionCard(title: SetupSection.skills.rawValue, symbol: SetupSection.skills.symbol,
                            summary: SetupSection.skills.summary) {
                    VStack(alignment: .leading, spacing: 6) {
                        KeyValueRow(key: "root", value: controller.libraryPath)
                        KeyValueRow(key: "available", value: "\(controller.skills.count) skill(s)")
                        if controller.skills.isEmpty {
                            // Which read is outstanding, rather than only an empty figure: "0 skill(s)"
                            // beside a button reads as a missing feature, and the list is a round trip
                            // to the engine that this pane has simply not made yet — or cannot, with
                            // the engine down, which is the other thing worth saying.
                            Text(controller.engineState == .running
                                 ? "The list has not been read from the engine yet."
                                 : "The engine is not running, so the list cannot be read — it is a "
                                   + "live round trip to the skill library, not a file on disk.")
                                .font(.caption2).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            Button("Load the list") { Task { await controller.loadRoster() } }
                                .controlSize(.small)
                                .accessibilityLabel("Ask the engine for the skill library")
                                .accessibilityHint("Fills the list of skills an agent can be hired for")
                        }
                    }
                }
                SectionCard(title: SetupSection.howItWorks.rawValue,
                            symbol: SetupSection.howItWorks.symbol,
                            summary: SetupSection.howItWorks.summary,
                            expanded: $showHowItWorks) {
                    HowItWorksCard()
                }
                SectionCard(title: SetupSection.advanced.rawValue, symbol: SetupSection.advanced.symbol,
                            summary: SetupSection.advanced.summary,
                            expanded: $showAdvanced) {
                    VStack(alignment: .leading, spacing: 8) {
                        KeyValueRow(key: "runtime", value: controller.runtimeDescription)
                        KeyValueRow(key: "engine", value: controller.engineDiagnostics.first
                                    .map { String($0.dropFirst("runtime: ".count)) } ?? "unknown")
                        KeyValueRow(key: "credentials", value: controller.credentialsPath)
                        KeyValueRow(key: "project", value: controller.projectPath)
                        Divider()
                        Button("Run the first-time setup again") {
                            controller.restartSetup()
                        }
                        .accessibilityLabel("Run the first-time setup again")
                        Text("Brings back the opening questions. Nothing you have configured is "
                             + "changed.")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task {
            await controller.loadWindow()
        }
    }

    /// The engine's own "what finishing this buys you" line for a step, by id.
    ///
    /// A lookup rather than a written-out line per section, so a step the engine renames or re-purposes
    /// moves this text with it instead of leaving a stale sentence under a control. Nil for a step this
    /// build does not have, so a newer engine's extra precondition simply has no line here rather than
    /// an empty one.
    @ViewBuilder
    private func stepOutcome(for id: String) -> some View {
        if let step = steps.first(where: { $0.id == id }) {
            StepOutcome(step: step)
        }
    }

    /// What is still missing, said once at the top of the destination — so a person opening Setup
    /// because the spine told them to does not have to guess which of the three sections is the one.
    @ViewBuilder
    private var status: some View {
        let gate = controller.setupGate
        HStack(spacing: 8) {
            Label(gate.title, systemImage: gate == .ready ? "checkmark.seal.fill" : "exclamationmark.circle")
                .font(.headline)
                .foregroundStyle(gate == .ready ? .green : .orange)
            Text(gate.detail)
                .font(.caption).foregroundStyle(.secondary)
                .lineLimit(2)
            Spacer()
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background((gate == .ready ? Color.green : Color.orange).opacity(0.08))
        .cornerRadius(8)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Setup: \(gate.title). \(gate.detail)")
    }
}

/// The project choice, without the wizard's step framing.
struct ProjectChoice: View {
    @ObservedObject var controller: OrgController

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Label(controller.workspace["attached"]?.boolValue == true
                      ? "Your own folder" : "A managed project",
                      systemImage: controller.workspace["attached"]?.boolValue == true
                          ? "folder.badge.checkmark" : "shippingbox")
                    .font(.callout)
                Spacer()
                if controller.workspace["attached"]?.boolValue == true {
                    Button("Use a managed project instead") {
                        Task { await controller.detachProject() }
                    }
                    .accessibilityLabel("Switch to a project the engine owns")
                }
            }
            Text(controller.projectPath)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            Text(controller.workspace["attached"]?.boolValue == true
                 ? "The agents edit this folder directly. The engine's records live in "
                     + ".agent_state/ inside it."
                 : "The engine created this folder and owns it. Nothing of yours is touched.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("Choose a folder…") {
                    openProjectPicker(controller: controller) { controller.confirmProject() }
                }
                .disabled(!controller.canLaunch)
                .accessibilityLabel("Choose a folder the agents should work in")
                if !controller.preferences.projectConfirmed {
                    Button("Use the current one") { controller.confirmProject() }
                        .buttonStyle(.borderedProminent)
                        .accessibilityLabel("Confirm the current project")
                } else {
                    Label("Confirmed", systemImage: "checkmark.circle.fill")
                        .font(.caption).foregroundStyle(.green)
                }
            }
        }
    }
}

// MARK: - Shared step chrome

/// The card surface every panel in this file sits on, with the Reduce Transparency fallback.
///
/// W H Y   A   M O D I F I E R   A N D   N O T   A   B A C K G R O U N D   A T   E A C H   S I T E
/// ---------------------------------------------------------------------------------------------
/// The audit's second failing rule was that the translucent step cards had no opaque fallback. The fix
/// is one rule in one place rather than a judgement repeated at ten call sites — because "does this
/// surface need a fallback" is the same question everywhere, and the eleventh call site someone adds is
/// the one that would forget.
///
/// A `.material` rather than a fixed opacity: it is the system's own translucency, so it matches the
/// window chrome for free and adapts to Increase Contrast. When the person has asked for **Reduce
/// Transparency**, the material is dropped entirely for an opaque fill — which is that setting's whole
/// purpose. Someone who turned transparency off did it for readability, and honouring it only *partly*
/// is the same as ignoring it.
///
/// The fill is `ControlBackgroundColor` rather than a literal colour, so it is opaque, follows Dark
/// Mode, and needs no hardcoded value (the audit's passing rule).
extension View {
    func cardSurface() -> some View {
        modifier(CardSurface())
    }
}

private struct CardSurface: ViewModifier {
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    func body(content: Content) -> some View {
        if reduceTransparency {
            content
                .background(Color(nsColor: .controlBackgroundColor))
                .overlay(RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(Color.secondary.opacity(0.25)))
                .clipShape(RoundedRectangle(cornerRadius: 8))
        } else {
            content
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
        }
    }
}

/// One step: what it is, why it matters, and its controls.
struct StepCard<Content: View>: View {
    let symbol: String
    let title: String
    let detail: String
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: symbol).font(.headline)
            Text(detail)
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            content()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardSurface()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(title). \(detail)")
    }
}

// MARK: - What happens after setup

/// Shown once, when the last answer lands, in place of a checklist that has nothing left to say.
///
/// The audit's fourth finding was that nothing told the person what happens *after* setup finishes.
/// The rail answers that per step while work is outstanding; this answers it at the moment work runs
/// out, which is the last opportunity to say it before the wizard disappears for good.
struct ReadyStep: View {
    var onFinish: () -> Void

    var body: some View {
        StepCard(symbol: "checkmark.seal", title: "Everything the org needs is in place",
                 detail: "A model resolves, a project is chosen, and a goal you set from this window "
                    + "will use the autonomy you picked.") {
            VStack(alignment: .leading, spacing: 10) {
                Label("Set a goal on Now, in your own words. You get the files in the project, and a "
                      + "readable record of who did what beside them.",
                      systemImage: "arrow.turn.down.right")
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Open Now", action: onFinish)
                    .buttonStyle(.borderedProminent)
                    .accessibilityLabel("Finish setup and open Now")
            }
        }
    }
}
