"""Kill switches, evaluated fail-closed.

Four independent scopes (PRD FR-14): global, environment, action type, service.
Any engaged switch blocks. The effective state is the OR of all of them, and an
unreadable store counts as engaged - if Aegis cannot prove autonomy is enabled,
autonomy is off.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aegis.core.logging import get_logger
from aegis.domain.enums import ActionType

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    """An immutable snapshot, loaded once per decision.

    Snapshotting matters: a switch flipping mid-decision must not produce a
    decision derived from two different worlds.
    """

    global_engaged: bool = False
    environments: frozenset[str] = field(default_factory=frozenset)
    action_types: frozenset[ActionType] = field(default_factory=frozenset)
    services: frozenset[str] = field(default_factory=frozenset)
    degraded: bool = False  # store unreadable -> treat everything as engaged
    reason: str = ""

    @classmethod
    def fail_closed(cls, reason: str) -> KillSwitchState:
        """Used when the policy store cannot be read."""
        return cls(global_engaged=True, degraded=True, reason=reason)

    def engaged_for(
        self, *, environment: str, action_type: ActionType, service_id: str | None
    ) -> tuple[bool, str]:
        """Return (engaged, human-readable scope) for this specific action."""
        if self.global_engaged:
            return True, (
                f"global kill switch engaged ({self.reason})"
                if self.reason
                else "global kill switch engaged"
            )
        if environment in self.environments:
            return True, f"autonomy disabled for environment {environment}"
        if action_type in self.action_types:
            return True, f"autonomy disabled for action type {action_type.value}"
        if service_id and service_id in self.services:
            return True, f"autonomy disabled for service {service_id}"
        return False, ""

    @property
    def any_engaged(self) -> bool:
        return bool(
            self.global_engaged or self.environments or self.action_types or self.services
        )
