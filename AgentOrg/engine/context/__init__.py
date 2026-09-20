#!/usr/bin/env python3
"""context — the session lifecycle: measure context, compact it, rotate it, hand it off.

Three distinct lifetimes, and conflating them is the source of most context bugs:

- **Run** — the graph, owned by the workflow runner. Contains many nodes.
- **Node** — one `execute_node` call, owned by the executor. Contains many turns, spans many sessions.
- **Session** — one bounded context window, owned here. Turns, saturation, rotation.

A node can span many sessions, and when a session rotates **the runner never knows** — the node still
returns one result. That is why session state is per-agent, not per-node.

Sub-modules
-----------
session     Session, SessionState, the turn-boundary state machine
projection  the pre-flight estimate and the irreducible/reducible split
compaction  the 70/85/95 ladder with AR-04 verbatim preservation
cachealign  cache-first eviction: one contiguous run, chosen with the store's warm-prefix signal
rotation    the three triggers, the three guards, and session_handoff.json
assembly    new-session prompt ordering by attention zone
"""

from .assembly import AssembledPrompt, AttentionZone, assemble_session_prompt
from .cachealign import (
    CacheVerdict,
    EvictionPlan,
    common_prefix_chars,
    consult_store,
    log_text,
    plan_eviction,
    prefix_hash_of,
)
from .compaction import CompactionAction, CompactionResult, compact, classify_band
from .projection import Component, Projection, estimate_components, project
from .rotation import (
    RotationDecision,
    RotationGuardError,
    RotationTrigger,
    SessionHandoff,
    build_handoff,
    decide_rotation,
)
from .session import (
    Band,
    ContextBudgetError,
    Session,
    SessionError,
    SessionState,
    Turn,
    new_session_id,
)

__all__ = [
    "AssembledPrompt",
    "AttentionZone",
    "Band",
    "CacheVerdict",
    "Component",
    "CompactionAction",
    "CompactionResult",
    "ContextBudgetError",
    "EvictionPlan",
    "Projection",
    "RotationDecision",
    "RotationGuardError",
    "RotationTrigger",
    "Session",
    "SessionError",
    "SessionHandoff",
    "SessionState",
    "Turn",
    "assemble_session_prompt",
    "build_handoff",
    "classify_band",
    "common_prefix_chars",
    "compact",
    "consult_store",
    "decide_rotation",
    "estimate_components",
    "log_text",
    "new_session_id",
    "plan_eviction",
    "prefix_hash_of",
    "project",
]
