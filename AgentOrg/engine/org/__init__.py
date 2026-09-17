#!/usr/bin/env python3
"""org — the organization model: who exists, who reports to whom, who may do what.

A *skill* is a capability; an *agent* is headcount. This package makes that split real: the
same skill can exist as many named agents with different models, budgets, mailboxes and
health records, and a graph node names a capability while the roster supplies the people.

Sub-modules
-----------
agent        AgentSpec, AgentRuntime, AgentKind, AgentLevel, Budget
roster       Org, Team, role templates, persistence
mailbox      per-agent append-only message log
binding      bind a manifest node to an agent (pinned/round-robin/load-balanced/swarm)
policy       autonomy levels, route classes, layered resolution, the safety floor
handoff      the handoff contract lifecycle and the R1–R8 mechanical validators
ledger       the decision gate ledger with SUPERSEDED markers
router       hard filters, scoring, and the confidence threshold
"""

from .agent import (
    AgentError,
    AgentKind,
    AgentLevel,
    AgentRuntime,
    AgentSpec,
    AgentState,
    Budget,
    Cost,
    new_agent_id,
)
from .binding import Binder, BindingError, BindingPolicy, NodeBinding, declared_policy
from .handoff import (
    CONTEXT_ELEMENTS,
    REQUIRED_FIELDS,
    Handoff,
    HandoffError,
    HandoffState,
    ValidationVerdict,
    build_context_pass_through,
    validate_delegation_context,
    validate_handoff,
)
from .ledger import Constraint, DecisionGate, Ledger, LedgerError
from .mailbox import Mailbox, MailboxError, Message, MessageKind
from .delegation import (
    S_INVARIANTS,
    ApprovalTier,
    DelegationError,
    DelegationOutcome,
    HiringDesk,
    LadderEvidence,
    Requisition,
    RequisitionState,
)
from .health import HealthMonitor, HealthState, HealthTransition, ProbeResult, SignalWindow
from .scheduler import Priority, Scheduler, SchedulerError, Ticket, TicketState, Watchdog
from .slo import BurnRate, Objective, Severity, SLOReport, SLOTracker
from .policy import (
    AUTONOMY_ORDER,
    DEFAULT_POLICY,
    SAFETY_FLOOR,
    SCOPE_ORDER,
    Autonomy,
    PolicyError,
    PolicyResolver,
    Resolution,
    RouteClass,
)
from .roster import DEFAULT_TEMPLATES, OWNER_ID, Org, OrgError, RoleTemplate, Team, default_company
from .router import Candidate, RouteContext, RouteDecision, Router, RouterError

__all__ = [
    "AUTONOMY_ORDER",
    "CONTEXT_ELEMENTS",
    "DEFAULT_POLICY",
    "DEFAULT_TEMPLATES",
    "OWNER_ID",
    "REQUIRED_FIELDS",
    "SAFETY_FLOOR",
    "SCOPE_ORDER",
    "AgentError",
    "AgentKind",
    "AgentLevel",
    "AgentRuntime",
    "AgentSpec",
    "AgentState",
    "Autonomy",
    "Binder",
    "BindingError",
    "BindingPolicy",
    "declared_policy",
    "Budget",
    "Candidate",
    "Constraint",
    "Cost",
    "DecisionGate",
    "Handoff",
    "HandoffError",
    "HandoffState",
    "Ledger",
    "LedgerError",
    "Mailbox",
    "MailboxError",
    "Message",
    "MessageKind",
    "NodeBinding",
    "Org",
    "OrgError",
    "PolicyError",
    "PolicyResolver",
    "Resolution",
    "RoleTemplate",
    "RouteClass",
    "RouteContext",
    "RouteDecision",
    "Router",
    "RouterError",
    "Team",
    "ValidationVerdict",
    "build_context_pass_through",
    "default_company",
    "new_agent_id",
    "validate_delegation_context",
    "validate_handoff",
]

__all__ += [
    "ApprovalTier",
    "BurnRate",
    "DelegationError",
    "DelegationOutcome",
    "HealthMonitor",
    "HealthState",
    "HealthTransition",
    "HiringDesk",
    "LadderEvidence",
    "Objective",
    "ProbeResult",
    "Priority",
    "Requisition",
    "RequisitionState",
    "S_INVARIANTS",
    "SLOReport",
    "SLOTracker",
    "Scheduler",
    "SchedulerError",
    "Severity",
    "SignalWindow",
    "Ticket",
    "TicketState",
    "Watchdog",
]
