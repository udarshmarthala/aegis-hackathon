"""The complete, explicit input to a policy decision.

Policy is a pure function of this object. Everything it needs is passed in, so a
decision can be replayed byte-for-byte from persisted state months later, and a
test can construct any scenario without infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aegis.domain.enums import ActionType, Severity
from aegis.domain.models import BlastRadius, RollbackPlan, VerificationPlan
from aegis.policy.killswitch import KillSwitchState


@dataclass(frozen=True, slots=True)
class PolicyContext:
    # what is being attempted
    action_type: ActionType
    environment: str
    service_id: str | None
    resource_id: str

    # the case for it
    incident_severity: Severity
    diagnosis_confidence: float
    evidence_count: int
    tier_a_evidence_count: int   # direct machine observations only
    contradicting_evidence_count: int
    has_abstained_diagnosis: bool

    # consequences
    blast_radius: BlastRadius
    rollback: RollbackPlan | None
    verification: VerificationPlan | None
    verification_passed: bool

    # operator-configured posture
    autonomy_enabled: bool
    allowed_tiers: frozenset[int]
    kill_switch: KillSwitchState
    service_allowlist: frozenset[str]
    actions_last_hour: int
    max_actions_per_hour: int

    # concurrency
    resource_lease_held: bool

    now: datetime
    is_production: bool = False

    @property
    def evidence_quality(self) -> float:
        """A crude but deterministic 0-1 score.

        Deliberately simple and inspectable: Tier-A corroboration raises it,
        contradictions lower it. An operator can reason about why an action was
        held back without reading model output.
        """
        if self.evidence_count == 0:
            return 0.0
        tier_a_share = self.tier_a_evidence_count / self.evidence_count
        contradiction_share = self.contradicting_evidence_count / self.evidence_count
        volume = min(self.evidence_count / 5.0, 1.0)
        score = (0.5 * tier_a_share) + (0.5 * volume) - contradiction_share
        return max(0.0, min(1.0, score))
