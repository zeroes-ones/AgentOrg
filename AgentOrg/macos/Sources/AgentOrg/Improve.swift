//
//  Improve.swift
//  AgentOrg
//
//  The Improve panel: what the system thinks is wrong with itself, and why nothing has changed.
//
//  WHY THIS PANEL SAYS "NOTHING WAS APPLIED" MORE THAN ONCE
//  --------------------------------------------------------
//  The whole safety story is that an unattended loop *proposes* and a person *decides*. If the panel
//  let a reader believe a fix had been applied, the gate would be decoration — so the panel states the
//  opposite plainly, in the header, on every row, and in the empty state. Repetition is the feature.
//
//  It also shows what the boundary **refused**. A list of promotions alone would look like the loop
//  never met a proposal it had to stop, when in fact stopping is the interesting part.

import SwiftUI
import AppKit
import AgentOrgKit

struct ImprovePanel: View {
    @ObservedObject var controller: OrgController
    @State private var showingRefused = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                header

                if controller.engineState != .running {
                    Label("The engine is not running, so a cycle cannot be run. Press ⌘⇧L first.",
                          systemImage: "exclamationmark.triangle.fill")
                        .font(.callout).foregroundStyle(.orange)
                        .accessibilityLabel("The engine is not running")
                }

                if controller.proposals.isEmpty {
                    ContentUnavailableView {
                        Label("No proposals", systemImage: "wand.and.stars")
                    } description: {
                        Text("Run a cycle and the engine will look for defects in its own work — a node "
                             + "rejected the same way repeatedly, a loop that never converges, an agent "
                             + "whose record has gone bad. Anything it finds becomes a proposal you can "
                             + "read. It never edits anything itself.")
                    } actions: {
                        Button("Find issues") { Task { await controller.runImprover() } }
                            .buttonStyle(.borderedProminent)
                            .disabled(controller.engineState != .running || controller.improving)
                    }
                } else {
                    ForEach(controller.proposals, id: \.self) { proposal in
                        ProposalRow(proposal: proposal) {
                            Task { await controller.dismissProposal(
                                id: proposal["proposal_id"]?.stringValue ?? "") }
                        }
                    }
                }

                if !controller.proposalsRefusedList.isEmpty {
                    refusedSection
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task { await controller.loadProposals() }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 18) {
                Metric(label: "Proposals", value: "\(controller.proposals.count)",
                       detail: "awaiting you",
                       tone: controller.proposals.isEmpty ? .secondary : .blue)
                Metric(label: "Refused", value: "\(controller.proposalsRefused)",
                       detail: "boundary working", tone: .secondary)
            }
            // Stated once at the top, so it cannot be missed before scrolling.
            Label("Nothing has been applied. This loop never edits the tree — read a proposal, then "
                  + "apply it yourself if you agree.",
                  systemImage: "hand.raised.fill")
                .font(.caption).foregroundStyle(.secondary)

            HStack(spacing: 8) {
                Button {
                    Task { await controller.runImprover() }
                } label: {
                    Label(controller.improving ? "Looking…" : "Find issues",
                          systemImage: "sparkle.magnifyingglass")
                }
                .disabled(controller.engineState != .running || controller.improving)
                .help("Detect, draft and validate — then stop and wait for you")
                .accessibilityLabel("Run one self-improvement cycle")

                if controller.improving {
                    // A cycle runs the whole behavioural suite, so it takes seconds. Saying so beats a
                    // button that looks hung.
                    ProgressView().controlSize(.small)
                        .accessibilityLabel("A cycle is running")
                }
                Spacer()
                if !controller.proposals.isEmpty {
                    Button("Reveal proposals folder") {
                        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath:
                            controller.proposalsDirectory)
                    }
                    .accessibilityLabel("Reveal the proposals folder in Finder")
                }
            }

            if let result = controller.improveResult["count"]?.intValue, !controller.improving {
                Text("Last cycle considered \(result) finding(s).")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private var refusedSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                showingRefused.toggle()
            } label: {
                Label("\(controller.proposalsRefused) refused by the boundary",
                      systemImage: showingRefused ? "chevron.down" : "chevron.right")
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Show what the safety boundary refused")

            if showingRefused {
                Text("A proposal that would change the machinery which judges this loop — the eval gate, "
                     + "the guardrail, the budget config, credentials — is refused before any model sees "
                     + "it. A system whose safety is enforced by code it can rewrite does not have that "
                     + "property.")
                    .font(.caption2).foregroundStyle(.secondary)
                ForEach(controller.proposalsRefusedList, id: \.self) { entry in
                    HStack(spacing: 6) {
                        Image(systemName: "nosign").foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                        Text(entry["kind"]?.stringValue ?? "?")
                            .font(.system(.caption2, design: .monospaced))
                            .frame(width: 140, alignment: .leading)
                        Text(entry["reason"]?.stringValue ?? "")
                            .font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
        .padding(10)
        .background(Color.secondary.opacity(0.06))
        .cornerRadius(8)
    }
}

/// One proposal, with what it found, what it improves, and what to do about it.
struct ProposalRow: View {
    let proposal: [String: JSONValue]
    let onDismiss: () -> Void

    private var finding: [String: JSONValue] { proposal["finding"]?.objectValue ?? [:] }
    private var validation: [String: JSONValue] { proposal["validation"]?.objectValue ?? [:] }

    private var tone: StatusTone {
        switch finding["severity"]?.stringValue {
        case "critical": return .bad
        case "major": return .attention
        default: return .neutral
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: tone.symbol).foregroundStyle(tone.colour)
                    .accessibilityHidden(true)
                Text(finding["kind"]?.stringValue ?? "?")
                    .font(.system(.callout, design: .monospaced))
                Text(finding["subject"]?.stringValue ?? "")
                    .font(.callout).foregroundStyle(.secondary)
                Spacer()
                Text(finding["severity"]?.stringValue ?? "")
                    .font(.caption2).foregroundStyle(tone.colour)
            }

            Text(finding["detail"]?.stringValue ?? "")
                .font(.caption)

            HStack(spacing: 14) {
                if let improves = validation["improved"]?.arrayValue, !improves.isEmpty {
                    Label("improves " + improves.compactMap { $0.stringValue }.joined(separator: ", "),
                          systemImage: "arrow.up.right.circle")
                        .font(.caption2).foregroundStyle(.green)
                }
                if let regressions = validation["regressions"]?.arrayValue, !regressions.isEmpty {
                    Label("\(regressions.count) regression(s)", systemImage: "exclamationmark.triangle")
                        .font(.caption2).foregroundStyle(.orange)
                }
                if let reason = validation["unvalidatable"]?.stringValue, !reason.isEmpty {
                    Label("not validated here", systemImage: "questionmark.circle")
                        .font(.caption2).foregroundStyle(.orange)
                        .help(reason)
                }
            }

            if let touches = proposal["touches"]?.arrayValue, !touches.isEmpty {
                Text("would change: " + touches.compactMap { $0.stringValue }.joined(separator: ", "))
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary).lineLimit(2)
            }

            HStack(spacing: 8) {
                Text("Nothing applied — read it, then decide.")
                    .font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Button("Hide") { onDismiss() }
                    .accessibilityLabel("Hide this proposal from the list")
            }
        }
        .padding(10)
        .background(tone.colour.opacity(0.06))
        .cornerRadius(8)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "\(finding["kind"]?.stringValue ?? "proposal") on "
            + "\(finding["subject"]?.stringValue ?? "the system"). Nothing has been applied.")
    }
}
