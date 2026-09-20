//
//  Usage.swift
//  AgentOrgKit
//
//  How full are the agents' contexts, and what is this costing — with a real number, or an honest
//  "not recorded".
//
//  WHY THIS FILE EXISTS
//  --------------------
//  The old Context panel asked "how full are the agents' contexts?" and answered it with the model's
//  window size and a static 70/85/95 legend. There was no current saturation anywhere on screen. That
//  is not a rendering bug: the figure existed and the panel never read it.
//
//  Where it exists, precisely, matters for honesty:
//
//  - **Per node, durably.** Every handoff the engine writes to `.agent_state/handoffs/<id>.json`
//    carries `payload.budget.session_saturation` and `payload.budget.context_window`, computed at the
//    node boundary (`executor._agent_budget`). It is a real measurement of that node's session, it is
//    already read by `RunStateBrowser`, and it survives the engine stopping.
//  - **Not in `status`.** The live snapshot has no per-node saturation, and the `session.saturation`
//    event type — though declared, and modelled by `ProtocolModels` — is not emitted by anything in
//    this engine build. So there is no live figure to show, and a panel that drew one would be
//    inventing it.
//
//  So this type reads what was recorded, names the node it belongs to, and says plainly when a node
//  has no record. The band thresholds mirror `engine/context/session.py::Band` (70/85/95) because the
//  *colours on the bar must mean what the engine's own ladder means* — a legend that disagreed with
//  the compactor's behaviour would teach the wrong thing.

import Foundation

/// One node's context, as it was last measured.
public struct ContextReading: Identifiable, Sendable, Equatable {
    public let id: String
    /// The node the session belonged to, as the engine names it.
    public let node: String
    /// The node it handed work on to, when the record names one.
    ///
    /// Deliberately *not* called "agent": a handoff's `origin`/`target` are node ids, and the
    /// document does not carry the producing agent's name — so calling this an agent would put a node
    /// id in a field that reads as a person. What it does say is useful: this node's context was this
    /// full when it passed work on.
    public let handedTo: String
    /// Fraction of the usable window in use, in `0...1`.
    public let saturation: Double
    /// The model's window, which is what a saturation is a fraction *of*.
    public let window: Int
    public let recordedAt: String

    /// The band, at the engine's own thresholds.
    public var band: Band { Band.forSaturation(saturation) }

    public init(id: String, node: String, handedTo: String, saturation: Double,
                window: Int, recordedAt: String) {
        self.id = id
        self.node = node
        self.handedTo = handedTo
        self.saturation = saturation
        self.window = window
        self.recordedAt = recordedAt
    }

    /// The figure a person reads, in words as well as a percentage.
    public var label: String { "\(Int((saturation * 100).rounded()))%" }
}

/// The compaction ladder, mirroring `engine/context/session.py`.
public enum Band: String, Sendable, CaseIterable {
    case healthy
    case warning
    case critical
    case overflow

    /// The band a saturation falls in, at the engine's thresholds.
    public static func forSaturation(_ value: Double) -> Band {
        if value >= 0.95 { return .overflow }
        if value >= 0.85 { return .critical }
        if value >= 0.70 { return .warning }
        return .healthy
    }

    /// Where the band starts. The bar's segment boundaries, so the drawing cannot drift from the rule.
    public var lowerBound: Double {
        switch self {
        case .healthy: return 0.0
        case .warning: return 0.70
        case .critical: return 0.85
        case .overflow: return 0.95
        }
    }

    public var tone: StatusTone {
        switch self {
        case .healthy: return .ok
        case .warning: return .attention
        case .critical: return .bad
        case .overflow: return .bad
        }
    }

    /// What being in this band means for the run, in the engine's terms.
    public var meaning: String {
        switch self {
        case .healthy: return "nothing to do — the session has room"
        case .warning: return "the engine compacts at this point"
        case .critical: return "eviction and rotation territory"
        case .overflow: return "the window is exhausted; rotation is required"
        }
    }
}

extension HandoffSummary {
    /// The saturation the node's session had reached, when the handoff recorded it.
    ///
    /// Read from `payload.budget`, which `executor._agent_budget` always populates — and therefore a
    /// key that is *present with a value* or absent because the document predates the field. Absent
    /// must not read as zero, so this is optional all the way through.
    public var sessionSaturation: Double? {
        budget["session_saturation"]?.doubleValue
    }

    /// The node's context window, when the handoff recorded it.
    public var contextWindow: Int? {
        budget["context_window"]?.intValue
    }

    /// Whether this handoff carries a usable measurement at all.
    public var hasContextReading: Bool {
        if let saturation = sessionSaturation, let window = contextWindow,
           saturation > 0, window > 0 { return true }
        return false
    }
}

extension ContextReading {
    /// The readings a set of handoffs implies, newest first and one per node.
    ///
    /// **One per node, newest wins**, because the question is "how full is this agent's context *now*"
    /// and a node that has crossed several boundaries would otherwise appear several times with
    /// progressively older figures — a list that looks like a trend without saying it is one.
    ///
    /// A handoff with no measurement is skipped rather than rendered as zero, which is the whole rule
    /// this file follows: the engine's `session_saturation` is `0.0` when it could not be computed, so
    /// a naive read would draw an empty bar for an unmeasured session and call it healthy.
    public static func latestPerNode(from handoffs: [HandoffSummary]) -> [ContextReading] {
        var byNode: [String: ContextReading] = [:]
        for handoff in handoffs {
            guard handoff.hasContextReading,
                  let saturation = handoff.sessionSaturation,
                  let window = handoff.contextWindow else { continue }
            let node = handoff.origin.isEmpty ? "unknown" : handoff.origin
            let existing = byNode[node]
            // Newest wins, by the record's own timestamp where it has one and by list order (the
            // browser already sorts newest first) otherwise.
            if let existing, handoff.createdAt < existing.recordedAt { continue }
            byNode[node] = ContextReading(
                id: "\(node)/\(handoff.id)",
                node: node,
                handedTo: handoff.target,
                saturation: min(1, max(0, saturation)),
                window: window,
                recordedAt: handoff.createdAt)
        }
        return byNode.values.sorted { $0.node < $1.node }
    }
}
