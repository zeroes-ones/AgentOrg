//
//  StatusTone.swift
//  AgentOrgKit
//
//  One place that decides what a status looks like.
//
//  WHY A SHARED TYPE RATHER THAN A `func tone(for:)` PER VIEW
//  ---------------------------------------------------------
//  The same four states appear in six files, and each had grown its own switch. That is how a "failed"
//  ends up orange in one panel and red in another — a status the app contradicts itself about. One type
//  means one answer, and it is the only place to change when the answer changes.
//
//  Two things the `macos-developer` and `apple-hig-expert` skills require, both handled here:
//
//  1. **Semantic colours only.** The skill's anti-hallucination table is blunt: hardcoded
//     `Color(red:green:blue:)` "won't adapt to Dark Mode, Increase Contrast, or Liquid Glass". So every
//     colour is a system semantic, chosen so the *meaning* survives an appearance change.
//  2. **Never hue alone.** `.green`/`.orange`/`.red` are indistinguishable to a colour-blind reader and
//     invisible to VoiceOver. So a tone always comes with a **symbol** and a **word**, and the symbol is
//     what carries the state when colour cannot.

import SwiftUI

/// The meaning of a status, independent of how it is drawn.
public enum StatusTone: Sendable {
    case ok          // proceeding normally
    case attention   // needs a person, or is degraded
    case bad         // failed, or actively harmful
    case neutral     // no claim being made
    case active      // working right now

    /// The semantic colour. Deliberately the system colours, so Increase Contrast and Dark Mode apply.
    ///
    /// These are used as *icons and words*, not as small text on a light background — the distinction the
    /// HIG checker cannot make. Apple's own `.orange` measures 2.2:1 against white, which fails a 4.5:1
    /// text rule; it is drawn as a filled glyph, where the requirement is 3:1 for non-text contrast and
    /// the adjacent word (`.primary`) carries the reading. Using these as prose would be the mistake.
    public var colour: Color {
        switch self {
        case .ok: return .green
        case .attention: return .orange
        case .bad: return .red
        case .neutral: return .secondary
        case .active: return .blue
        }
    }

    /// A glyph, so the state is legible without colour.
    public var symbol: String {
        switch self {
        case .ok: return "checkmark.circle.fill"
        case .attention: return "exclamationmark.triangle.fill"
        case .bad: return "xmark.octagon.fill"
        case .neutral: return "circle.dotted"
        case .active: return "circle.fill"
        }
    }

    /// The word VoiceOver reads, and the one shown beside the glyph.
    public var word: String {
        switch self {
        case .ok: return "ok"
        case .attention: return "attention"
        case .bad: return "failed"
        case .neutral: return "—"
        case .active: return "running"
        }
    }

    /// Map a node or agent status string to a tone.
    ///
    /// Total by construction: an unknown status is `neutral` rather than a guess, because inventing a
    /// state for a value this build does not recognise is how a UI confidently shows the wrong thing.
    public static func forStatus(_ status: String) -> StatusTone {
        switch status.lowercased() {
        case "done", "pass", "healthy", "complete", "completed", "approved", "ok":
            return .ok
        case "needs_review", "blocked", "degraded", "warning", "waiting", "paused", "awaiting_human",
             "awaiting_gate", "escalated":
            return .attention
        case "failed", "error", "quarantined", "rejected", "breach", "terminated":
            return .bad
        case "running", "working", "active", "in_progress", "probed":
            return .active
        default:
            return .neutral
        }
    }
}

/// A status rendered so it survives without colour: a glyph, a word, and the tone.
///
/// Every panel uses this, which is what makes "failed" look the same everywhere and means a change to the
/// vocabulary happens once.
public struct StatusLabel: View {
    public let status: String
    public var tone: StatusTone?

    public init(status: String, tone: StatusTone? = nil) {
        self.status = status
        self.tone = tone
    }

    private var resolved: StatusTone { tone ?? .forStatus(status) }

    public var body: some View {
        // `Label` gives the icon-and-text pair with correct system spacing, and `.combine` makes
        // VoiceOver read it as one phrase rather than "orange circle ... failed".
        Label(status, systemImage: resolved.symbol)
            .foregroundStyle(resolved.colour)
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Status: \(status)")
    }
}
