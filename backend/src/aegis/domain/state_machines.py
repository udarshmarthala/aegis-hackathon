"""Explicit transition tables.

State rules live in one adjacency table per machine rather than scattered across
service code. That is what lets the entire lifecycle be property-tested without
a database, and it makes an illegal transition a loud failure instead of a
quietly corrupted incident.
"""

from __future__ import annotations

from aegis.core.errors import DomainError
from aegis.domain.enums import ActionState, IncidentState

# Any active incident may be escalated or blocked out of band (operator action,
# budget exhaustion, kill switch), so those targets are unioned in below rather
# than repeated on every row.
_ESCAPE: frozenset[IncidentState] = frozenset(
    {IncidentState.ESCALATED, IncidentState.BLOCKED}
)

_INCIDENT_TRANSITIONS: dict[IncidentState, frozenset[IncidentState]] = {
    IncidentState.RECEIVED: frozenset({IncidentState.TRIAGING}),
    IncidentState.TRIAGING: frozenset({IncidentState.INVESTIGATING, IncidentState.RESOLVED}),
    IncidentState.INVESTIGATING: frozenset({IncidentState.DIAGNOSING, IncidentState.RESOLVED}),
    IncidentState.DIAGNOSING: frozenset(
        {
            IncidentState.INVESTIGATING,  # insufficient evidence, gather more
            IncidentState.DEBUGGING,
            IncidentState.AWAITING_APPROVAL,
            IncidentState.REMEDIATING,
            IncidentState.RESOLVED,
        }
    ),
    IncidentState.DEBUGGING: frozenset(
        {IncidentState.VERIFYING, IncidentState.DIAGNOSING, IncidentState.RESOLVED}
    ),
    IncidentState.VERIFYING: frozenset(
        {
            IncidentState.AWAITING_APPROVAL,
            IncidentState.REMEDIATING,
            IncidentState.DEBUGGING,  # verification failed, iterate on the patch
            IncidentState.RESOLVED,
        }
    ),
    IncidentState.AWAITING_APPROVAL: frozenset(
        {
            IncidentState.REMEDIATING,   # approved
            IncidentState.INVESTIGATING,  # reviewer asked for more evidence
            IncidentState.RESOLVED,       # rejected and closed
        }
    ),
    IncidentState.REMEDIATING: frozenset({IncidentState.MONITORING, IncidentState.VERIFYING}),
    IncidentState.MONITORING: frozenset(
        {
            IncidentState.RESOLVED,
            IncidentState.INVESTIGATING,  # recovery did not hold
        }
    ),
    IncidentState.ESCALATED: frozenset(
        {IncidentState.INVESTIGATING, IncidentState.REMEDIATING, IncidentState.RESOLVED}
    ),
    IncidentState.BLOCKED: frozenset({IncidentState.INVESTIGATING, IncidentState.RESOLVED}),
    IncidentState.RESOLVED: frozenset(),  # terminal
}


def allowed_incident_transitions(src: IncidentState) -> frozenset[IncidentState]:
    base = _INCIDENT_TRANSITIONS[src]
    if src.is_active and src not in _ESCAPE:
        return base | _ESCAPE
    return base


def can_transition_incident(src: IncidentState, dst: IncidentState) -> bool:
    return dst in allowed_incident_transitions(src)


def assert_incident_transition(src: IncidentState, dst: IncidentState) -> None:
    """Raise unless ``src -> dst`` is legal. Self-transitions are always legal."""
    if src == dst:
        return
    if not can_transition_incident(src, dst):
        raise DomainError(
            f"illegal incident transition {src.value} -> {dst.value}",
            code="ILLEGAL_TRANSITION",
            context={
                "from": src.value,
                "to": dst.value,
                "allowed": sorted(s.value for s in allowed_incident_transitions(src)),
            },
        )


_ACTION_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    ActionState.PROPOSED: frozenset({ActionState.POLICY_CHECKED, ActionState.BLOCKED}),
    ActionState.POLICY_CHECKED: frozenset(
        {ActionState.BLOCKED, ActionState.HUMAN_REQUIRED, ActionState.EXECUTING}
    ),
    # An approval that is never acted on expires - a stale approval must never
    # become executable (ESD section 20).
    ActionState.HUMAN_REQUIRED: frozenset(
        {ActionState.APPROVED, ActionState.BLOCKED, ActionState.EXPIRED}
    ),
    ActionState.APPROVED: frozenset({ActionState.EXECUTING, ActionState.EXPIRED}),
    ActionState.EXECUTING: frozenset({ActionState.VERIFYING, ActionState.FAILED}),
    ActionState.VERIFYING: frozenset(
        {ActionState.SUCCESS, ActionState.FAILED, ActionState.ROLLED_BACK}
    ),
    ActionState.FAILED: frozenset({ActionState.ROLLED_BACK}),
    ActionState.SUCCESS: frozenset(),
    ActionState.BLOCKED: frozenset(),
    ActionState.ROLLED_BACK: frozenset(),
    ActionState.EXPIRED: frozenset(),
}


def can_transition_action(src: ActionState, dst: ActionState) -> bool:
    return dst in _ACTION_TRANSITIONS[src]


def assert_action_transition(src: ActionState, dst: ActionState) -> None:
    if src == dst:
        return
    if not can_transition_action(src, dst):
        raise DomainError(
            f"illegal action transition {src.value} -> {dst.value}",
            code="ILLEGAL_TRANSITION",
            context={
                "from": src.value,
                "to": dst.value,
                "allowed": sorted(s.value for s in _ACTION_TRANSITIONS[src]),
            },
        )


def terminal_incident_states() -> frozenset[IncidentState]:
    return frozenset(s for s in IncidentState if s.is_terminal)


def terminal_action_states() -> frozenset[ActionState]:
    return frozenset(s for s in ActionState if s.is_terminal)
