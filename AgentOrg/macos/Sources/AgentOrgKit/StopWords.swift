//
//  StopWords.swift
//  AgentOrgKit
//
//  The engine's wording for its own stop tokens, carried to the console rather than rewritten here.
//
//  WHY THIS IS A TYPE AND NOT TWO STRINGS IN A SWITCH
//  --------------------------------------------------
//  `guardrail-blocked` is not self-explanatory: the node *finished its work* and what it handed on was
//  refused at the edge. The engine already says so — once, in `engine/flow.py`'s `_STOP_WORDS` — and
//  folds that sentence into a row's `blocked_by`. What it did not do was *send* the table, so the app
//  kept its own copy of the two sentences a person actually meets, worded identically on purpose: one
//  token described two ways on one screen reads as two different states.
//
//  One sentence with two homes is one sentence too many, and the copy is the one that goes stale — the
//  engine can reword `guardrail-blocked` and nothing here would notice. So the table travels in the
//  reports that render tokens (`engine/flow.py`'s `build_flow` and `engine/activity.py`'s
//  `build_activity`, both via `flow.stop_words`), and this decodes it.
//
//  What is *not* here: the app's own glosses for tokens the engine has no sentence for at all — a node
//  parked at a human gate (`awaiting_owner`) or one missing an input (`missing_prerequisites`). Those
//  are this build's wording for states the engine reports and does not describe, they exist in exactly
//  one place (`NowPane.EngineWord`), and moving them into the engine would be inventing engine
//  behaviour to have something to decode.

import Foundation

/// The engine's gloss for each stop token it knows, keyed by the token.
///
/// An empty vocabulary is a real state: before the first report, or on a report from a build that
/// predates the key. It degrades to showing the token itself, which is what the engine actually
/// recorded — the same rule the engine's own gloss follows for a token it does not know.
public struct StopWords: Sendable, Equatable {

    private let words: [String: String]

    /// Decode from one or more reports, in the order they are given.
    ///
    /// Several, because the same table travels in both the board and the activity report: whichever
    /// arrived is the vocabulary, and a report that is missing costs nothing. The first report that
    /// carries a token wins, which only matters if two reports ever disagree — and if they do, the
    /// earlier one is the one whose rows are about to be drawn with it.
    public init(_ reports: [String: JSONValue]...) {
        var words: [String: String] = [:]
        for report in reports {
            let table = report["stop_words"]?.objectValue ?? [:]
            for (token, gloss) in table {
                guard let sentence = gloss.stringValue, !sentence.isEmpty else { continue }
                if words[token] == nil { words[token] = sentence }
            }
        }
        self.words = words
    }

    /// Whether the engine has sent a vocabulary yet.
    public var isEmpty: Bool { words.isEmpty }

    /// The engine's sentence for one token, or "" where it has none.
    ///
    /// Empty is an answer rather than a failure: the caller keeps the token and shows it, because an
    /// invented sentence is the confident-wrong output both surfaces are arranged against.
    public func gloss(_ token: String) -> String { words[token] ?? "" }

    /// Every token the engine glosses, sorted — so a test can state the property rather than a sample.
    public var tokens: [String] { words.keys.sorted() }
}
