//
//  SystemPane.swift
//  AgentOrg
//
//  The System destination: what the agents may do on this machine, and who may do it.
//
//  WHY THIS IS ITS OWN DESTINATION
//  -------------------------------
//  The engine already answers this question in full — `serve._cmd_system` returns every declared
//  capability with what it reaches, what it changes, its caution, whether a tool exists behind it, and
//  the two switches that decide whether any of it is live. Until now the app rendered none of that.
//  The only place a person could see a `system:` grant was a checkbox inside the *hire form*, which
//  made granting an agent the ability to run AppleScript look like a field of the hire rather than a
//  decision about the person's own desktop.
//
//  So the panel is a **reading** of the engine, not a second description of it. No grant name, title,
//  sentence or ordering is written here; `SystemCapabilities.swift` decodes the reply and this draws
//  it. That is deliberate and it is the whole design: a console that paraphrased a permission would go
//  on confidently describing the old one the first time the engine changed it.
//
//  Two exceptions, both of them things the *reply* does not carry and both named where they live: the
//  three allowlist→grant pairs (`ScopesSection.grant(forAllowlist:)`), and the tool catalogue mirrored
//  in `SystemCapabilities.swift` because the payload has no tool names at all — reported there with the
//  engine change that would delete it.
//
//  WHAT IT SAYS THAT THE ENGINE CANNOT SAY ALONE
//  --------------------------------------------
//  "Who holds this" — the roster's grant lists, checked with the same exact-or-wildcard rule the tool
//  registry enforces, so a row cannot show a grant as held while `ToolRegistry.call` would refuse it.
//
//  A capability with no tool is shown, not hidden. `syscap` reports one as `available: false`
//  precisely so a console can survive the tree being mid-build, and dropping it would collapse "not
//  built yet" into "not granted". **All twelve have a tool today**, so that card currently renders
//  never — which is the state the code was written for, not a claim that the branch is dead.
//
//  THE HEADER'S FIGURE, AND WHY IT NEEDED A SENTENCE AROUND IT
//  ----------------------------------------------------------
//  It counted "granted" and left the person asking what 6 of 12 wanted them to do. Three readings of
//  one machine were behind that word — the engine's own `summary` (computed for no holder, so it reads
//  "0 of 12 granted": `syscap.py:324`), the served roster's six, and the CLI's twelve for the Owner —
//  and the pane printed the first as a headline above the second as a figure. So the figure now says
//  *held by an agent*, the headline is derived from the switches, the sentence beneath states the rule
//  that makes the number matter (a capability nobody holds is one no run can use), and the figure is
//  the way into the two lists that name both halves and the place a grant is given. What the pane still
//  cannot state is the person's *own* reach: the reply names no holder (`engine/serve.py:1919`), and the
//  note under the figures says so rather than printing a number nobody can attribute.
//
//  AND IT ACTS
//  -----------
//  For a while this pane was a read-out: it described twelve grants and offered no way to change one
//  of them, and its comment justified that with a fact that stopped being true the moment the engine
//  gained `system_set` — "they live in credentials.json, which the app cannot write". An explanation
//  of a limitation is the first thing an implementer reads, so a stale one is how a missing feature
//  survives. The pane now writes through the three commands that already existed for it —
//  `system_set` for the switches and the allowlists, `system_consent` for the ask-once approvals,
//  `system_invoke` for "show me what this one does" — and it leads with the *one* action the current
//  state calls for rather than leaving a person to infer it from twelve rows.

import SwiftUI
import AgentOrgKit

struct SystemPane: View {
    @ObservedObject var controller: OrgController
    /// Which sections are open, remembered across a relaunch like every other pane's.
    ///
    /// **This one defaults to open, and it used to default to closed.** When the card held nothing but
    /// a read-out of the allowlists, hiding it cost nothing. It now holds the only two controls that
    /// decide whether *any* of the twelve rows is live, and a disclosure that defaults closed over the
    /// switches is how a pane that can act still reads as one that only describes — the person sees
    /// twelve grants, no switch, and concludes the app cannot change them. The stored flag still wins
    /// for anyone who has collapsed it deliberately.
    @AppStorage("system.showScopes") private var showScopes = true
    @AppStorage("system.showUnavailable") private var showUnavailable = true
    /// The two lists behind the header's first figure, open to begin with — for the reason
    /// `showScopes` above is: this block is the answer to "what does that number mean", and a
    /// disclosure that defaults closed over the answer leaves the person with the number and no
    /// reading of it, which is the report this pane was rewritten for. The stored flag still wins once
    /// someone collapses it deliberately.
    @AppStorage("system.showHolders") private var showHolders = true

    private var capabilities: [SystemCapability] { controller.systemCapabilities }

    /// The two readings of the capability set — what an agent holds, and what the person can use — in
    /// one value, so the headline, the figures and the two lists behind them all come from one
    /// derivation. Computed rather than stored: it follows the roster as the roster changes, and the
    /// roster is what decides the count.
    private var reach: SystemReach {
        SystemReach(capabilities: capabilities, roster: controller.roster, system: controller.system)
    }

    /// The grants with a tool behind them, in the engine's own reading order: reads, then state
    /// changes, then the powerful ones.
    private var available: [SystemCapability] { capabilities.filter(\.available) }

    /// The declared grants with no tool yet. Kept beside the working ones rather than buried, because
    /// the two together are what the engine says the machine *will* offer — see the header.
    private var unavailable: [SystemCapability] { capabilities.filter { !$0.available } }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if controller.engineIsGone {
                    EngineNotRunningView(controller: controller)
                } else if capabilities.isEmpty {
                    // Reachable for a frame between the pane appearing and the first reply landing. A
                    // spinner would flash; this says which read is outstanding instead.
                    Label("Asking the engine what these agents may do…", systemImage: "hourglass")
                        .font(.callout).foregroundStyle(.secondary)
                } else {
                    header
                    // One action, before the twelve rows that ask a person to work it out themselves.
                    // The CLI ends every system command with a next step; a console that renders the
                    // same information and stops is the "says multiple things but confused about what
                    // to do" report this pane was written to answer.
                    NextStepCard(controller: controller, showScopes: $showScopes)
                    // Directly after the one action, and only a card below the figures it explains: this
                    // is the list *behind* the header's first number, and the number is what the person
                    // arrived with. Short by design — one line per capability — because the full row for
                    // any of them, with the engine's own prose, is the next section down.
                    SectionCard(title: "Who can use what",
                                symbol: "person.2.badge.key",
                                summary: holderSummary(reach),
                                expanded: $showHolders) {
                        HolderBreakdown(controller: controller, reach: reach)
                    }
                    SectionCard(title: "What the agents may do", symbol: "lock.shield",
                                summary: "every capability the engine declares, what each one reaches, "
                                       + "and what it changes — with a read you can run to see it") {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(available) { capability in
                                CapabilityRow(controller: controller, capability: capability)
                            }
                        }
                    }
                    if !unavailable.isEmpty {
                        SectionCard(title: "Declared, no tool yet",
                                    symbol: "wrench.and.screwdriver",
                                    summary: "\(unavailable.count) capability(ies) the engine names but "
                                           + "cannot yet act on — granting one buys nothing today",
                                    expanded: $showUnavailable) {
                            VStack(alignment: .leading, spacing: 10) {
                                ForEach(unavailable) { capability in
                                    CapabilityRow(controller: controller, capability: capability)
                                }
                            }
                        }
                    }
                    SectionCard(title: "Switches and allowlists", symbol: "switch.2",
                                summary: "the two switches that decide whether any of this is live, and "
                                       + "the lists that narrow what each grant reaches",
                                expanded: $showScopes) {
                        ScopesSection(controller: controller)
                    }
                    SectionCard(title: "One-off approvals", symbol: "hand.raised",
                                summary: "the ten tools the engine refuses until you approve them, once "
                                       + "per agent, and the last decision recorded here") {
                        ConsentSection(controller: controller)
                    }
                    enforcement
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        // The roster travels with `status`, which the window polls, but `system` does not — so it is
        // read here when the pane appears rather than being folded into the two-second poll. The
        // capability list changes when a build changes, not when a run does.
        //
        // `loadRoster` is here because two of this pane's controls need it and nothing else guarantees
        // it: "who holds this" keys off the roster, and an approval has to name a *holder*, so the
        // consent picker would otherwise be empty on a window that opened straight onto this
        // destination. It is one extra round trip on appearance, not on the poll.
        .task {
            await controller.loadSystem()
            await controller.loadRoster()
            await controller.refresh()
        }
    }

    /// The state of the machine tools, the three figures that describe who can use them, and the
    /// sentence that makes the first figure mean something.
    ///
    /// **The headline used to be the engine's `summary` field, and that field is about nobody.**
    /// `syscap.console_payload` assembles the capability *set* and calls `summary([])`
    /// (`engine/syscap.py:324`), so the sentence it hands a console reads "0 of 12 system capabilities
    /// granted" — measured on this machine, where the served roster holds six of the twelve and the
    /// CLI's own holder holds all twelve. A headline saying 0 above a figure saying 6, with the terminal
    /// saying 12, is the report this rewrite answers: three numbers, one word, no subject. So the
    /// headline is now derived from the switches — a fact this pane can see — and every figure carries
    /// the subject it counts. `OrgController.systemSummary` is left alone (a file this change does not
    /// own); it is simply not drawn any more, because there is no reading of "0 of 12 granted" that is
    /// true of anything.
    ///
    /// The "off" case is stated in the first sentence rather than left to a greyed-out list: a person
    /// who opens this pane and sees twelve capabilities has no way to tell from the rows alone that
    /// none of them is reachable, and the engine's `enabled: false` is the fact that says so.
    private var header: some View {
        let on = controller.systemEnabled
        let full = controller.systemFullAccess
        let tone: StatusTone = on ? (full ? .attention : .ok) : .neutral
        let reach = self.reach
        return VStack(alignment: .leading, spacing: 6) {
            Label(headline(on: on, full: full), systemImage: tone.symbol)
                .font(.headline)
                .foregroundStyle(tone.colour)
                .accessibilityLabel(on ? (full ? "System tools are on, with full access."
                                              : "System tools are on, granted per agent.")
                                       : "System tools are off.")
            Text(modeSentence(on: on, full: full))
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            figures(reach)
            Text(heldSentence(reach))
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if reach.owner == nil { ownerGapNote }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.colour.opacity(0.08))
        .cornerRadius(8)
        // `.contain`, not `.combine`: the first figure is a button, and `.combine` would fold it into
        // one spoken element that nobody can press — the same rule `CapabilityRow` follows for its
        // Try-it button. Each part therefore carries its own label.
        .accessibilityElement(children: .contain)
    }

    /// The state, in one line, from the switches rather than from a count.
    private func headline(on: Bool, full: Bool) -> String {
        guard on else { return "System tools are off" }
        return full ? "System tools are on — full access" : "System tools are on — granted per agent"
    }

    /// What the state means for the list below, unchanged from the sentence this pane has always shown.
    private func modeSentence(on: Bool, full: Bool) -> String {
        guard on else {
            return "Turned off in the engine's configuration, so no system tool is offered to any "
                 + "agent. Nothing below is reachable until that changes; this pane describes what "
                 + "would be available if it were on."
        }
        return full
            ? "Full access is on: an agent with the relevant grant may launch any application, run any "
              + "script, and act without asking once. Every call is still recorded."
            : "An agent may use a capability only where it holds that exact grant — the allowlists "
              + "below narrow what each grant reaches."
    }

    /// The figures, each with the subject it counts. The first one is the way into the two lists.
    ///
    /// Every value reads "n of total" rather than a bare n over a bare total: the bare pair is what left
    /// a person asking what 6/12 was 6 *of*, and the shared denominator is only obvious to whoever
    /// wrote it.
    private func figures(_ reach: SystemReach) -> some View {
        let total = reach.capabilities.count
        let unheld = reach.heldByNobody.count
        let unbuilt = controller.systemUnavailableCount
        // The engine's own titles for the grants it reports as unbuilt, so what a person reads names
        // real capabilities instead of leaving a count to be guessed at. Decoded from the reply, because
        // the app keeps no list of its own.
        let unbuiltNames = controller.systemCapabilities.filter { !$0.available }.map(\.title)
        return HStack(alignment: .top, spacing: 18) {
            Button { showHolders.toggle() } label: {
                HStack(alignment: .top, spacing: 5) {
                    Metric(label: "held by an agent",
                           value: "\(reach.heldByAnAgent.count) of \(total)",
                           detail: unheld == 0 ? "every one is held" : "\(unheld) no agent holds")
                    Image(systemName: showHolders ? "chevron.up.circle" : "chevron.down.circle")
                        .font(.caption).foregroundStyle(.secondary).padding(.top, 4)
                }
            }
            .buttonStyle(.plain)
            .help("Which capabilities an agent holds, which none holds, and where to grant one")
            .accessibilityLabel("Capabilities an agent holds: "
                                + "\(reach.heldByAnAgent.count) of \(total). \(unheld) are held by "
                                + "nobody.")
            .accessibilityHint(showHolders ? "Hides the two lists"
                                           : "Shows the two lists, and where to grant one")

            // The second figure was "declared, no tool behind it" — a description of the state with no
            // verb, sitting beside a figure that *is* actionable. A count with no action still reads as
            // a to-do, which is how "6 of 12" became a question about what to do next.
            //
            // The answer is that there is nothing to do, and it is the engine's answer rather than this
            // pane's: `syscap.summary` reports a capability with no tool separately from one no agent
            // holds, because "a capability with no tool yet is a *build* fact, not a permission the
            // agent lacks, and conflating them would have the console asking a person to grant
            // something that would not work" (`engine/syscap.py`). So the sentence names the ones still
            // to be built and says outright that granting one would not help.
            Metric(label: "no tool yet", value: "\(unbuilt) of \(total)",
                   detail: unbuilt == 0
                       ? "every one is built"
                       : "nothing to grant — these are not written yet")
                .help(unbuiltNames.isEmpty
                      ? "Every declared capability has a tool behind it."
                      : "Still to be built: \(unbuiltNames.joined(separator: ", ")). "
                        + "Granting one of these would not make it work — the tool itself does not exist "
                        + "yet, so there is no switch here and nothing to change in the roster.")

            // Only where the engine named the acting holder. A missing holder is not zero — see
            // `ownerGapNote`, which says so in words instead of printing a figure that would be a lie.
            if let owner = reach.owner {
                Metric(label: "\(owner.name) can use",
                       value: "\(reach.ownerReaches.count) of \(total)",
                       detail: owner.grants.joined(separator: ", "))
                    // The engine's own account of where those grants came from. On hover, because the
                    // detail line has room for the grants and not for the reason — and because that
                    // sentence is the engine's, not one this pane would have to invent.
                    .help(owner.why.isEmpty ? owner.grants.joined(separator: ", ") : owner.why)
            }
            Spacer()
        }
        .padding(.top, 2)
    }

    /// What the first figure means: the rule, the consequence of it, and where the missing ones are
    /// granted. This is the sentence the pane never had — it printed a count and left the person to
    /// infer that a capability nobody holds is one no run can use, which is the actionable half.
    private func heldSentence(_ reach: SystemReach) -> String {
        let total = reach.capabilities.count
        let held = reach.heldByAnAgent.count
        let unheld = total - held
        let rule = "An agent is offered a capability only where it holds that grant"
        guard unheld > 0 else {
            return "\(rule) — and all \(total) are held by an agent today, so none of them is out of "
                 + "reach for that reason."
        }
        let consequence = held == 0
            ? "no agent holds one, so no run can use any of them"
            : "the \(unheld) no agent holds cannot be used by any run"
        let dead = controller.systemEnabled
            ? ""
            : " — and with the tools switched off above, none of the \(total) is offered at all"
        return "\(rule), so \(consequence)\(dead). Granting one is a roster edit, not a control here."
    }

    /// Why there is no figure for the person's own reach — and where the other reading of the same
    /// machine lives.
    ///
    /// Not a placeholder for a value this build failed to compute: the field is not in the reply.
    /// `serve._cmd_system` returns `syscap.console_payload` and nothing else (`engine/serve.py:1919`),
    /// which describes the capability set and names no holder; the CLI can answer for a holder only
    /// because it adds one itself (`engine/systemcli.py:628`). That is exactly why the terminal says
    /// "12 of 12 granted" while this pane counts six. Saying so is better than a second number nobody
    /// can attribute — and the moment the engine sends `holder`, `figures` above draws it and this
    /// note disappears on its own.
    private var ownerGapNote: some View {
        Label("Your own reach is not in the engine's reply to this pane — it describes the capability "
              + "set without a holder, so the figures above count agents only. "
              + "`engine.cli system list` answers for you: it names the holder it acts as and that "
              + "holder's own count.",
              systemImage: "person.crop.circle.badge.questionmark")
            .font(.caption2).foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// The one line the "Who can use what" section says while it is collapsed.
    ///
    /// A collapsed section still has to explain itself — the pane's rule everywhere — and here that
    /// means repeating the split, because the split *is* the answer to the figure above. It used to be
    /// the count of capabilities with a tool, which is a different question from who may use them.
    private func holderSummary(_ reach: SystemReach) -> String {
        let total = reach.capabilities.count
        let held = reach.heldByAnAgent.count
        guard total > 0 else { return "nothing declared" }
        guard held < total else {
            return "all \(total) capabilities are held by an agent — nothing to grant for that reason"
        }
        return "\(held) of the \(total) are held by an agent and \(total - held) by nobody — naming "
             + "both, and where a person grants the rest"
    }

    /// Where the refusal a person never sees comes from, said once at the bottom.
    ///
    /// This is the honest limit of the panel: it *writes* the switches, the lists and the approvals, but
    /// the enforcement lives in the tool registry. A person who has just changed four settings and read
    /// twelve rows deserves to know where each one is checked, rather than assuming this pane is the
    /// gate — the write here is what the registry reads, not a gate of its own.
    private var enforcement: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("Where this is enforced", systemImage: "checkmark.shield")
                .font(.callout.weight(.medium))
            Text("Every machine call goes through the engine's tool registry, which checks the agent's "
                 + "own grant, then the allowlist, then the one-off approval for anything that changes "
                 + "state. This pane reads the engine's answer rather than deciding one, so a grant "
                 + "shown here is the grant the registry enforces.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.06))
        .cornerRadius(8)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Who can use what

/// The two lists behind the header's first figure, and the route to changing it.
///
/// **Why this exists at all.** The pane listed twelve capabilities, each with its own "held by X" or
/// "no agent holds this" line, and a count in the header — and a person still asked what the number was
/// asking them to do. A per-row answer does not add up to an answer to "6 of 12 *what*, and so what?":
/// the two facts a person needs are the ones this section states together, *which* capabilities nobody
/// holds and *where* that is changed. Capability-centric rather than grouped by agent, because "which of
/// these no run can use" is the question the count raises; the per-agent view is the roster's, and it is
/// one destination away.
///
/// One line per capability, deliberately: the engine's own prose for each is on the full row below, and
/// repeating twelve paragraphs here would be a second rendering of the same permission.
struct HolderBreakdown: View {
    @ObservedObject var controller: OrgController
    let reach: SystemReach

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            group(title: "Held by an agent", symbol: "person.crop.circle.badge.checkmark",
                  capabilities: reach.heldByAnAgent, tone: .green,
                  empty: "No capability is held by an agent, so no run can reach this Mac.")
            group(title: "Held by nobody", symbol: "person.crop.circle.badge.questionmark",
                  capabilities: reach.heldByNobody, tone: .secondary,
                  empty: "Every declared capability is held by an agent — nothing is out of reach for "
                       + "this reason.")
            grantRoute
        }
    }

    /// One list. The count is in the heading so a collapsed row still says how many; the empty case is
    /// a sentence rather than a blank, because an empty list under a heading reads as a loading state.
    @ViewBuilder
    private func group(title: String, symbol: String, capabilities: [SystemCapability],
                       tone: Color, empty: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("\(title) — \(capabilities.count) of \(reach.capabilities.count)",
                  systemImage: symbol)
                .font(.caption.weight(.medium)).foregroundStyle(tone)
            if capabilities.isEmpty {
                Text(empty).font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ForEach(capabilities) { capability in
                    // An empty holder list is the fact this half of the section exists to state, so it
                    // is written out rather than left as a blank column that would read as a missing
                    // value.
                    let names = reach.holders(of: capability.grant)
                    let who = names.isEmpty ? "no agent holds it" : names.joined(separator: ", ")
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(capability.title).font(.caption)
                        Text(capability.grant)
                            .font(.system(.caption2, design: .monospaced))
                            .foregroundStyle(.tertiary)
                        Spacer()
                        // The names, not a count: "UX" answers "who", and the row below in the list of
                        // twelve shows the same names, so the two cannot read as different facts.
                        Text(who).font(.caption2).foregroundStyle(.secondary)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(capability.title + ", " + capability.grant + ", " + who)
                }
            }
        }
    }

    /// Where granting happens — said in the app's own words *and* one click away.
    ///
    /// `DestinationRouter.shared.select(.org)` is the same pattern `NowPane` and `ConsoleView` use to
    /// move the window; the roster's hire/edit form is the only place a grant is given (`OrgPane`'s
    /// capability editor, whose machine group is titled "On this Mac"). The button moves the window and
    /// nothing more — it cannot open the form on a particular agent from here, so the sentence beside it
    /// names the clicks that remain rather than leaving the person on an unfamiliar pane.
    private var grantRoute: some View {
        VStack(alignment: .leading, spacing: 5) {
            Button {
                DestinationRouter.shared.select(.org)
            } label: {
                Label("Open the roster", systemImage: "person.badge.plus")
            }
            .controlSize(.small)
            .help("Go to the Org destination, where an agent's capabilities are edited")
            .accessibilityLabel("Open the Org destination, where a capability is granted to an agent")

            Text(controller.roster.isEmpty
                 ? "There is nobody to grant to yet — the roster is empty, so hire an agent first. A "
                   + "grant is a roster edit; this pane decides what the machine offers, not who holds "
                   + "what."
                 : "Granting is a roster edit: Org → Edit on the agent → Capabilities → On this Mac. "
                   + "This pane decides what the machine offers, not who holds what.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.05))
        .cornerRadius(6)
    }
}

// MARK: - One capability

/// One declared capability: the engine's words, how it is drawn, who holds it, and — for the grants
/// where a bare call is a read — a button that runs one and shows the answer.
struct CapabilityRow: View {
    @ObservedObject var controller: OrgController
    let capability: SystemCapability

    private var holders: [String] { controller.systemHolders(of: capability.grant) }

    /// The tools under this grant the engine will refuse until the Owner approves one, for one agent.
    /// Named rather than counted, so the row says *what* would ask rather than that something would.
    private var consentTools: [SystemTool] {
        controller.systemToolCatalog.forGrant(capability.grant).filter(\.consentRequired)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            description
                // Combined *before* the actions are attached, deliberately. `.combine` on the whole row
                // would fold a `Button` into a single spoken element, and VoiceOver would then read a
                // description a person cannot act on — the actions have to stay their own elements.
                .accessibilityElement(children: .combine)
                .accessibilityLabel(accessibilityText)

            // A read you can run once, where running it cannot change anything. The row has just said
            // what the grant reaches and what it changes; this is the sentence a person cannot get from
            // prose — what it actually returns — and the engine's refusal, when it is one, is the
            // useful part.
            if let tool = controller.systemToolCatalog.tryable(capability.grant), capability.available {
                TryItRow(controller: controller, tool: tool)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(capability.tone.colour.opacity(capability.available ? 0.05 : 0.02))
        .cornerRadius(6)
    }

    private var description: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(capability.title).font(.callout.weight(.medium))
                Text(capability.grant)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.tertiary)
                Spacer()
                // The state as a glyph *and* a word: a reader who cannot tell the two colours apart
                // still gets the answer, which is the rule `StatusTone` exists to enforce.
                Label(capability.stateWord, systemImage: capability.tone.symbol)
                    .font(.caption2)
                    .foregroundStyle(capability.tone.colour)
            }

            // "reaches", then "changes" — the order the engine writes them in, and the order a person
            // decides in: what does it touch, then what does it alter.
            Label(capability.reaches, systemImage: "arrow.turn.down.right")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Label(capability.effect, systemImage: capability.changesState
                  ? "exclamationmark.arrow.triangle.2.circlepath" : "eye")
                .font(.caption)
                .foregroundStyle(capability.changesState ? .orange : .secondary)
                .fixedSize(horizontal: false, vertical: true)

            if !capability.caution.isEmpty {
                Label(capability.caution, systemImage: "exclamationmark.triangle")
                    .font(.caption2).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !consentTools.isEmpty {
                Label("asks you once, per agent, before it runs: "
                      + consentTools.map(\.name).joined(separator: ", "),
                      systemImage: "hand.raised")
                    .font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            holdersLine
        }
    }

    /// Who in the roster holds this. Named rather than counted: "Alice, Ravi" is an answer to "who can
    /// already do this", and a bare count is not.
    @ViewBuilder
    private var holdersLine: some View {
        if holders.isEmpty {
            Label("no agent holds this", systemImage: "person.crop.circle.badge.questionmark")
                .font(.caption2).foregroundStyle(.secondary)
        } else {
            Label("held by \(holders.joined(separator: ", "))",
                  systemImage: "person.crop.circle.badge.checkmark")
                .font(.caption2).foregroundStyle(.green)
        }
    }

    /// Spoken as the row reads, with the state and the holders included — VoiceOver on a `combine`
    /// element would otherwise drop the two facts a person is scanning for.
    private var accessibilityText: String {
        var parts = ["\(capability.title), \(capability.grant).", capability.reaches]
        parts.append(capability.effect + ".")
        if !capability.caution.isEmpty { parts.append("Caution: " + capability.caution) }
        parts.append(capability.stateWord + ".")
        if !consentTools.isEmpty {
            parts.append("Approval is asked once per agent for "
                         + consentTools.map(\.name).joined(separator: ", ") + ".")
        }
        parts.append(holders.isEmpty ? "No agent holds this."
                                     : "Held by \(holders.joined(separator: ", ")).")
        return parts.joined(separator: " ")
    }
}

// MARK: - Scopes and switches

/// The two switches and the three allowlists — read from the engine's reply, written back through
/// `system_set`.
///
/// These are what make a grant a *scope* rather than a boolean — "may open Safari" is not "may script
/// Safari", and `allow_apps` is the difference. **The comment here used to say the panel deliberately
/// offered no control because these "live in `credentials.json`, which the app cannot write".** That
/// stopped being true when `serve._cmd_system_set` landed: the engine writes the section atomically and
/// reloads it in place, and the app had been describing a limitation it no longer had. A pane that
/// describes a switch and cannot set it is a read-out, and the person who came here to decide
/// something was left to hand-edit JSON.
struct ScopesSection: View {
    @ObservedObject var controller: OrgController
    /// Set while the full-access confirmation is up. The toggle itself never flips on its own: turning
    /// it on is the one switch here whose consequence is unbounded, so it is asked about first.
    @State private var confirmFullAccess = false

    /// The lists the engine narrows a grant with, in the order `syscap` reports them.
    static let allowlistKeys = ["allow_apps", "allow_automation", "allow_shortcuts"]

    /// Which grant each list narrows.
    ///
    /// Three entries, and they are the engine's own pairing rather than the panel's opinion: the
    /// handlers name the list they read (`SystemTools.allowed_apps` in `engine/sysctl_tools.py` reads
    /// `allow_apps` for `open_app`, `automation_prefixes` reads `allow_automation`,
    /// `allowed_shortcuts` reads `allow_shortcuts`). Having the grant lets the editor show the engine's
    /// sentence for it beside the list instead of a sentence written here.
    static func grant(forAllowlist key: String) -> String {
        switch key {
        case "allow_apps": return "system:open"
        case "allow_automation": return "system:automation"
        case "allow_shortcuts": return "system:shortcuts"
        default: return ""
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            switches
            Divider()
            ForEach(Self.allowlistKeys, id: \.self) { key in
                AllowlistEditor(controller: controller, key: key,
                                grant: Self.grant(forAllowlist: key))
            }
            Text("These are written to credentials.json by the engine when you change them here, and "
                 + "the engine reloads the section in place — so the next tool call sees the change, and "
                 + "a change made from the terminal shows up here on the next read. Full access is "
                 + "reported beside them rather than on its own because it is what decides whether they "
                 + "bind at all.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .alert("Turn on full access?", isPresented: $confirmFullAccess) {
            Button("Turn on full access", role: .destructive) {
                Task { await controller.setSystem(["allow_full_access": .bool(true)]) }
            }
            Button("Cancel", role: .cancel) { }
        } message: {
            Text(Self.fullAccessConsequence)
        }
    }

    private var switches: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Only the flipped key is sent. The engine treats an absent key as "leave this alone"
            // (`serve._cmd_system_set`), which is what stops a toggle here from writing back a stale
            // copy of the other three settings and silently reverting a change made elsewhere.
            Toggle(isOn: Binding(
                get: { controller.systemEnabled },
                set: { on in Task { await controller.setSystem(["enabled": .bool(on)]) } })) {
                VStack(alignment: .leading, spacing: 1) {
                    Text("enabled").font(.callout.weight(.medium))
                    Text(controller.systemEnabled
                         ? "on: a capability is offered to an agent that holds its grant"
                         : "off: no machine tool is offered to any agent, so every row below is inert")
                        .font(.caption2).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .accessibilityLabel("System tools enabled")

            Toggle(isOn: fullAccessBinding) {
                VStack(alignment: .leading, spacing: 1) {
                    Text("full access").font(.callout.weight(.medium))
                    Text(controller.systemFullAccess
                         ? "on: the three lists below are not consulted and nothing asks you first"
                         : "off: each grant is bounded by its list, and a state change asks you once")
                        .font(.caption2).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .tint(controller.systemFullAccess ? .orange : .accentColor)
            .accessibilityLabel("Full access")
        }
    }

    /// The full-access toggle, with the one asymmetry that matters: **it asks before ON, not before
    /// OFF**. Turning it off can only add restrictions, so a dialog there would be a speed bump on the
    /// safe direction; turning it on lifts three allowlists and the ask-once gate at once, and the
    /// person is owed the sentence before the write, not after.
    private var fullAccessBinding: Binding<Bool> {
        Binding(
            get: { controller.systemFullAccess },
            set: { on in
                if on { confirmFullAccess = true }
                else { Task { await controller.setSystem(["allow_full_access": .bool(false)]) } }
            })
    }

    /// What full access actually does, in the engine's terms.
    ///
    /// Each clause is traceable: the three empty-list refusals are *skipped* under full access
    /// (`sysctl_tools.py:1672` for `open_app`, `:1732` for `run_automation`, `:2171` for
    /// `run_shortcut`), the ask-once gate steps aside (`_needs_consent`, `:1407`), and the record stays
    /// (`:1405-1406` — the call still reaches the log and the ledger). The last clause is the one a
    /// person is most likely to assume the other way, which is why it is stated.
    static let fullAccessConsequence =
        "Full access lifts all three allowlists and steps the ask-once approval aside. An agent that "
        + "holds any machine grant can then launch any application, run any AppleScript, and act on "
        + "this Mac without asking you once — an empty list below stops meaning \u{201C}none\u{201D} and "
        + "starts meaning \u{201C}no list is kept\u{201D}.\n\n"
        + "Every call is still recorded in the run's log and the ledger, so this widens permission "
        + "without blinding the audit. Turn it off again to make the lists bind."
}

// MARK: - One allowlist

/// One allowlist, editable: the names the engine will accept, and the engine's own sentence for the
/// grant the list narrows.
///
/// Edits send the **whole list**, because that is the shape `system_set` takes (`allow_apps` and
/// friends are lists, not deltas) — and because the list here was read from the engine a moment ago
/// and is the only copy the app has. The engine trims and de-blanks what it is given, so a name with
/// stray spaces is not a second entry.
struct AllowlistEditor: View {
    @ObservedObject var controller: OrgController
    let key: String
    let grant: String

    @State private var newName = ""

    private var names: [String] { controller.systemAllowlist(key) }
    private var capability: SystemCapability? {
        controller.systemCapabilities.first { $0.grant == grant }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(key).font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                if let capability {
                    Text("narrows \(capability.title.lowercased())")
                        .font(.caption2).foregroundStyle(.tertiary)
                }
                Spacer()
                Text(names.isEmpty ? "0 names" : "\(names.count) name(s)")
                    .font(.caption2).foregroundStyle(.tertiary)
            }

            // The engine's sentence for the grant this list narrows, not one written here. For
            // `allow_automation` that is the *caution* — the engine's honest line that a prefix is a
            // name check rather than a sandbox — which is the fact a person needs before adding one.
            if let capability {
                Text(capability.reaches)
                    .font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if !capability.caution.isEmpty {
                    Label(capability.caution, systemImage: "exclamationmark.triangle")
                        .font(.caption2).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if names.isEmpty {
                Text(emptySentence)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(controller.systemFullAccess ? Color.secondary : Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ForEach(names, id: \.self) { name in
                    HStack(spacing: 6) {
                        Text(name)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                        Button {
                            write(names.filter { $0 != name })
                        } label: {
                            Image(systemName: "minus.circle")
                        }
                        .buttonStyle(.borderless)
                        .foregroundStyle(.red)
                        .help("Remove \(name) from \(key)")
                        .accessibilityLabel("Remove \(name) from \(key)")
                        Spacer()
                    }
                }
            }

            HStack(spacing: 6) {
                TextField("Add a name", text: $newName)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 220)
                    .onSubmit(add)
                    .accessibilityLabel("A name to add to \(key)")
                Button("Add", action: add)
                    .disabled(newName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityLabel("Add the typed name to \(key)")
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.05))
        .cornerRadius(6)
    }

    /// What an empty list means — and it depends on a switch, which is why this is computed rather
    /// than a constant.
    ///
    /// The sentence here used to be `(none — this grant reaches nothing)` for every empty list. On a
    /// machine with full access on that is **false**: `open_app`, `run_automation` and `run_shortcut`
    /// skip the empty-allowlist refusal entirely when `full_access` is set (`sysctl_tools.py:1672`,
    /// `:1732`, `:2171`), so the list is not a ceiling of "nothing", it is not consulted at all. The
    /// pane was therefore telling a person their AppleScript reached nothing at the exact moment it
    /// reached anything the applications allow.
    private var emptySentence: String {
        if controller.systemFullAccess {
            return "(empty — with full access on no list is kept, so this narrows nothing)"
        }
        // The engine's own words for each refusal, so the sentence a person reads here is the one the
        // tool will hand them: `open_app` says "no application is allowed", `run_automation` "nothing
        // may be run", `run_shortcut` "no Shortcut is allowed".
        switch key {
        case "allow_apps": return "(none — no application may be launched)"
        case "allow_automation": return "(none — nothing may be run)"
        case "allow_shortcuts": return "(none — no Shortcut may be run)"
        default: return "(none — nothing is allowed)"
        }
    }

    private func add() {
        let name = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return }
        newName = ""
        guard !names.contains(name) else { return }
        write(names + [name])
    }

    /// Send the whole list. This is the shape the engine takes, and the only one it can check: a
    /// partial write would force the engine to merge two lists that were read at different times.
    private func write(_ updated: [String]) {
        Task { await controller.setSystem([key: .array(updated.map { .string($0) })]) }
    }
}

// MARK: - Try a read

/// Run one capability's tool once and show the engine's answer, verbatim.
///
/// **Only for a tool that reads and takes no arguments** — `SystemTool.isSafeToTry`. The row above has
/// already said what the grant reaches and what it changes; the one thing prose cannot answer is what
/// it *actually returns*, and that is exactly the question a person has when deciding whether to hand
/// the grant over. Where the engine refuses, the refusal is the answer: it names the missing grant, the
/// allowlist or the ledger gate, so it is shown as it came rather than summarised.
struct TryItRow: View {
    @ObservedObject var controller: OrgController
    let tool: SystemTool

    @State private var running = false
    /// Held here rather than read from `controller.systemResult`. That published field exists and is
    /// never read by anything — and because it has no clear API, a result drawn from it could never be
    /// dismissed. Reported as a gap in `OrgController` rather than worked around by editing a file this
    /// change does not own; local state makes the result dismissable today.
    @State private var result: [String: JSONValue]?

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Button(action: run) {
                    Label(running ? "Running…" : "Try it", systemImage: "play.circle")
                }
                .controlSize(.small)
                .disabled(running || !controller.systemEnabled)
                .accessibilityLabel("Run \(tool.name) once and show the engine's answer")
                // The reason the button is dead, said beside it: a disabled control with no
                // explanation is the same dead end as a switch that buys nothing.
                Text(controller.systemEnabled
                     ? "runs \(tool.name) as you, through the same gate an agent goes through — it "
                       + "only reads, so nothing on the Mac changes"
                     : "system tools are off, so the engine offers no tool to run — turn them on above")
                    .font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let result { answer(result) }
        }
    }

    private func run() {
        Task {
            running = true
            result = await controller.invokeSystem(tool: tool.name)
            running = false
        }
    }

    /// The engine's own document, with the three facts it carries drawn apart: whether the call ran,
    /// whether it was *refused* (a decision about a permission, not a failure), and the text itself.
    @ViewBuilder
    private func answer(_ payload: [String: JSONValue]) -> some View {
        let text = payload["text"]?.stringValue ?? ""
        let refused = payload["refused"]?.boolValue ?? false
        let ok = payload["ok"]?.boolValue ?? false
        let tone: StatusTone = refused ? .attention : (ok ? .ok : .bad)
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Label(refused ? "refused" : (ok ? "ran" : "failed"), systemImage: tone.symbol)
                    .font(.caption2.weight(.medium)).foregroundStyle(tone.colour)
                Spacer()
                Button("Dismiss") { result = nil }
                    .buttonStyle(.link).font(.caption2)
                    .accessibilityLabel("Dismiss the result of \(tool.name)")
            }
            if text.isEmpty {
                // `mutate` reports a command the *engine* refused (an unknown tool, the tools switched
                // off) as a notice rather than a result document, so there is nothing to paste here —
                // saying so points at that sentence instead of drawing an empty box.
                Text("the engine did not return a result document for this call — the notice in the "
                     + "status strip says why")
                    .font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(text)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.primary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.colour.opacity(0.06))
        .cornerRadius(5)
    }
}

// MARK: - One-off approvals

/// The tools the engine refuses until the Owner approves them, once, per agent.
///
/// This is the mechanism the panel used to *mention* — the words consent, ledger and approval appeared
/// once, in a closing paragraph, with no data behind them and no way to give one. The set is
/// `sysctl_tools.CONSENT_REQUIRED`, carried in the engine's own `system` reply (`syscap.tools_payload`)
/// and read from there, so a tool that starts asking for an approval appears here with no second list
/// to maintain — the app has no tool names of its own to go stale.
struct ConsentSection: View {
    @ObservedObject var controller: OrgController

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("A state-changing tool is refused until you approve it for one agent. The approval is a "
                 + "ledger entry at system-tool:<tool>:<agent> — attributable and reviewable, which a "
                 + "flag an agent could set for itself would not be.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if controller.systemFullAccess {
                Label("Full access is on, so the engine skips every one of these asks — approving "
                      + "anything here changes nothing until it is off.",
                      systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("The engine's reply does not report which approvals are live, so this shows what this "
                 + "console last decided, not the ledger's current state. "
                 + "`engine.cli system consent list` prints the ledger.")
                .font(.caption2).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(controller.systemToolCatalog.consentRequired) { tool in
                ConsentRow(controller: controller, tool: tool)
            }
        }
    }
}

/// One approval: which holder it is for, and the one decision that changes it.
struct ConsentRow: View {
    @ObservedObject var controller: OrgController
    let tool: SystemTool

    /// Empty means "use the default", which is computed rather than stored so it follows the roster as
    /// it changes; the moment a person picks one, the choice is theirs and is not re-defaulted.
    @State private var holder = ""

    private struct Option: Identifiable {
        let id: String
        let name: String
        /// Whether this agent's own capabilities reach the tool's grant. An approval for an agent that
        /// cannot hold the grant could never be used, so those sort last and say so.
        let holdsGrant: Bool
    }

    private var capability: SystemCapability? {
        controller.systemCapabilities.first { $0.grant == tool.grant }
    }

    /// The agents an approval can be given to. The Owner principal is excluded: it is the authority
    /// giving the approval, and `serve._holder_for` makes the console's own calls *as* the Owner with
    /// `system:*` — an approval for the person's own read would be a gate asking permission of itself.
    private var options: [Option] {
        controller.roster.compactMap { agent in
            guard let id = agent["id"]?.stringValue, !id.isEmpty, id != "ag_owner" else { return nil }
            return Option(id: id, name: agent["name"]?.stringValue ?? id,
                          holdsGrant: SystemCapability.holds(tool.grant, agent: agent))
        }
        .sorted { ($0.holdsGrant ? 0 : 1, $0.name) < ($1.holdsGrant ? 0 : 1, $1.name) }
    }

    private var chosen: String { holder.isEmpty ? (options.first?.id ?? "") : holder }

    /// What this console last decided for this tool, when it was the tool in question. The controller's
    /// `systemConsent` store is the engine's reply to the write, so this is the engine's own words
    /// about the last decision — not a local guess.
    private var lastDecision: String? {
        guard controller.systemConsent["tool"]?.stringValue == tool.name,
              let who = controller.systemConsent["agent_id"]?.stringValue, !who.isEmpty
        else { return nil }
        let approved = controller.systemConsent["approved"]?.boolValue ?? false
        return "last recorded here: \(approved ? "approved" : "withdrawn") for \(who)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(tool.name).font(.system(.caption, design: .monospaced))
                if let capability {
                    Text(capability.title).font(.caption2).foregroundStyle(.tertiary)
                }
                Spacer()
                if let lastDecision {
                    Label(lastDecision, systemImage: "checkmark.seal")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            if options.isEmpty {
                Text("no agents in the roster to approve for")
                    .font(.caption2).foregroundStyle(.secondary)
            } else {
                HStack(spacing: 8) {
                    Picker("holder", selection: Binding(get: { chosen }, set: { holder = $0 })) {
                        ForEach(options) { option in
                            Text(option.holdsGrant
                                 ? "\(option.name) — holds \(tool.grant)"
                                 : "\(option.name) — does not hold it").tag(option.id)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.menu)
                    .frame(maxWidth: 240)
                    .accessibilityLabel("Which agent this approval is for")

                    Button("Approve") { decide(true) }
                        .controlSize(.small)
                        .disabled(chosen.isEmpty)
                        .accessibilityLabel("Approve \(tool.name) for the chosen agent, once")
                    Button("Withdraw") { decide(false) }
                        .controlSize(.small)
                        .disabled(chosen.isEmpty)
                        .accessibilityLabel("Withdraw the approval for \(tool.name)")
                }
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.05))
        .cornerRadius(6)
    }

    /// The engine's own call shape: `setSystemConsent(tool:holder:approved:note:)`, which becomes
    /// `{tool, agent_id, approved, note}` on the wire (`serve._cmd_system_consent`). The note is left
    /// empty because `grant_consent` writes its own rationale naming the principal and the holder, and
    /// an invented sentence here would be a second voice in an append-only record.
    private func decide(_ approved: Bool) {
        let target = chosen
        guard !target.isEmpty else { return }
        Task {
            await controller.setSystemConsent(tool: tool.name, holder: target,
                                             approved: approved, note: "")
        }
    }
}

// MARK: - The next step

/// The one action the current state calls for.
///
/// The CLI ends every system command with a next step; this pane used to end with a paragraph, leaving
/// a person to infer from twelve rows what to do. The order below is the order of *consequence*, not of
/// severity: the switch that decides whether anything is live, then the switch that decides how far it
/// reaches, then a grant that is held but unusable, then who could hold anything at all.
enum SystemNextStep: Equatable {
    case turnOn
    case reduceFullAccess
    case fillAllowlist(String)
    case grantSomeone
    case tryARead
    case review

    var title: String {
        switch self {
        case .turnOn: return "Turn the machine tools on"
        case .reduceFullAccess: return "Reduce full access"
        case .fillAllowlist(let key): return "Fill \(key) — nothing is allowed without it"
        case .grantSomeone: return "Grant a capability to an agent"
        case .tryARead: return "See what one of these grants actually returns"
        case .review: return "Nothing needs your decision"
        }
    }

    var detail: String {
        switch self {
        case .turnOn:
            return "System tools are off in the engine's configuration, so none of the twelve "
                + "capabilities below is offered to any agent. Until you turn them on, this pane "
                + "describes what would be available rather than what is."
        case .reduceFullAccess:
            return "Full access is on, which means the three allowlists are not consulted and no call "
                + "asks you first — an agent holding any system grant can act on this Mac unbounded. "
                + "If that was not a deliberate hand-over, turning it off is the one change that makes "
                + "every other control here mean something."
        case .fillAllowlist(let key):
            return "An agent holds the grant that \(key) narrows, and the list is empty — so the "
                + "engine refuses every call the grant was meant to allow. Add the names you intend to "
                + "allow, or take the grant back from the agent."
        case .grantSomeone:
            return "No agent holds a machine capability, so nothing on this Mac is reachable from a "
                + "run. Grant one in the roster's hire/edit form (Org → Edit on the agent → "
                + "Capabilities → On this Mac) — this pane changes what the machine offers, not who "
                + "holds what."
        case .tryARead:
            return "Nothing here is unbounded and nothing is configured into a dead end. The quickest "
                + "way to answer \u{201C}should I hand this over?\u{201D} is to run one of the reads "
                + "once and see what it returns."
        case .review:
            return "The switches are where you set them and every allowlist that matters has names in "
                + "it. Review the rows below when an agent asks for something new."
        }
    }

    var symbol: String {
        switch self {
        case .turnOn: return "power.circle"
        case .reduceFullAccess: return "exclamationmark.triangle"
        case .fillAllowlist: return "exclamationmark.bubble"
        case .grantSomeone: return "person.badge.plus"
        case .tryARead: return "play.circle"
        case .review: return "checkmark.circle"
        }
    }

    var tone: StatusTone {
        switch self {
        case .turnOn, .grantSomeone: return .neutral
        case .reduceFullAccess, .fillAllowlist: return .attention
        case .tryARead, .review: return .ok
        }
    }
}

/// The next-step card: one heading, one reason, at most one button.
struct NextStepCard: View {
    @ObservedObject var controller: OrgController
    @Binding var showScopes: Bool

    private var step: SystemNextStep { Self.nextStep(for: controller) }

    var body: some View {
        let step = self.step
        VStack(alignment: .leading, spacing: 8) {
            Label(step.title, systemImage: step.symbol)
                .font(.headline).foregroundStyle(step.tone.colour)
            Text(step.detail)
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            action(for: step)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(step.tone.colour.opacity(0.08))
        .cornerRadius(8)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Next step: \(step.title). \(step.detail)")
    }

    /// At most one control, and only for a step this pane can actually take.
    ///
    /// `grantSomeone` now has one, and the comment here used to say the opposite — that navigating would
    /// need a destination change in `Navigation`/`App`, "a file this change does not own". That was
    /// wrong: `DestinationRouter.shared.select(_:)` exists in `App.swift:235` for exactly this, and
    /// `NowPane` and `ConsoleView` already use it to move the window. The step is still a roster edit and
    /// this button still cannot open the editor on a particular agent — but it takes the person to the
    /// one destination where the edit is possible, and the detail sentence above it names the clicks.
    @ViewBuilder
    private func action(for step: SystemNextStep) -> some View {
        switch step {
        case .turnOn:
            Button("Turn system tools on") {
                Task { await controller.setSystem(["enabled": .bool(true)]) }
            }
            .buttonStyle(.borderedProminent)
        case .reduceFullAccess:
            Button("Turn full access off") {
                Task { await controller.setSystem(["allow_full_access": .bool(false)]) }
            }
            .buttonStyle(.borderedProminent)
        case .fillAllowlist:
            Button("Show the lists") { showScopes = true }
                .buttonStyle(.bordered)
        case .grantSomeone:
            Button("Open the roster") { DestinationRouter.shared.select(.org) }
                .buttonStyle(.borderedProminent)
                .help("Go to Org, where an agent's capabilities are edited")
                .accessibilityLabel("Open the Org destination to grant a capability to an agent")
        case .tryARead, .review:
            EmptyView()
        }
    }

    /// The order is the design. `enabled` first because nothing else is live without it; full access
    /// second because on a full-access machine every list below is decorative; an empty list third, but
    /// **only where an agent actually holds the grant it narrows** — an empty list nobody is scoped by
    /// is a setting, not a problem, and reporting it would be crying wolf.
    static func nextStep(for controller: OrgController) -> SystemNextStep {
        guard controller.systemEnabled else { return .turnOn }
        if controller.systemFullAccess { return .reduceFullAccess }
        for key in ScopesSection.allowlistKeys {
            let grant = ScopesSection.grant(forAllowlist: key)
            if controller.systemAllowlist(key).isEmpty,
               !controller.systemHolders(of: grant).isEmpty {
                return .fillAllowlist(key)
            }
        }
        if controller.systemGrantedCount == 0 { return .grantSomeone }
        if controller.systemToolCatalog.all.contains(where: { $0.isSafeToTry }) { return .tryARead }
        return .review
    }
}
