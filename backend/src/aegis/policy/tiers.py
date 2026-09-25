"""Static action classification.

Risk tier is a property of the *action type*, resolved from this table alone. It
is never derived from model output, never from the reason text, and never from
how confident an agent sounds. That is the mitigation for an agent talking a
dangerous action into a lower class (ESD section 18).

Changing a value here is a security-relevant change and requires review.
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis.domain.enums import ActionType, RiskTier


@dataclass(frozen=True, slots=True)
class ActionProfile:
    """Everything policy needs to reason about one action type."""

    tier: RiskTier
    idempotent: bool
    reversible: bool
    max_blast_radius: int
    min_confidence: float
    requires_verification: bool = True
    description: str = ""


_PROFILES: dict[ActionType, ActionProfile] = {
    # ---- Tier 1: bounded, idempotent, reversible, small blast radius --------
    ActionType.RESTART_INSTANCE: ActionProfile(
        tier=RiskTier.LOW,
        idempotent=True,
        reversible=True,
        max_blast_radius=1,
        min_confidence=0.70,
        description="Restart one stateless instance whose health check is failing.",
    ),
    ActionType.RERUN_HEALTH_CHECK: ActionProfile(
        tier=RiskTier.LOW,
        idempotent=True,
        reversible=True,
        max_blast_radius=1,
        min_confidence=0.50,
        requires_verification=False,
        description="Re-run a read-only health probe.",
    ),
    ActionType.SCALE_UP_BOUNDED: ActionProfile(
        tier=RiskTier.LOW,
        idempotent=True,
        reversible=True,
        max_blast_radius=2,
        min_confidence=0.75,
        description="Increase replicas within an explicit small bound.",
    ),
    ActionType.CLEAR_CACHE_KEY: ActionProfile(
        tier=RiskTier.LOW,
        idempotent=True,
        reversible=False,  # a cleared key cannot be restored, but it is rebuildable
        max_blast_radius=1,
        min_confidence=0.75,
        description="Evict a specific cache key.",
    ),
    # ---- Tier 2: consequential, human decision required --------------------
    ActionType.ROLLBACK_DEPLOYMENT: ActionProfile(
        tier=RiskTier.APPROVAL,
        idempotent=True,
        reversible=True,
        max_blast_radius=10,
        min_confidence=0.80,
        description="Roll a service back to its previous immutable version.",
    ),
    ActionType.SCALE_SERVICE: ActionProfile(
        tier=RiskTier.APPROVAL,
        idempotent=True,
        reversible=True,
        max_blast_radius=10,
        min_confidence=0.75,
        description="Change replica count outside the tier-1 bound.",
    ),
    ActionType.UPDATE_CONFIG: ActionProfile(
        tier=RiskTier.APPROVAL,
        idempotent=True,
        reversible=True,
        max_blast_radius=10,
        min_confidence=0.85,
        description="Change runtime configuration for a service.",
    ),
    ActionType.PROMOTE_PATCH: ActionProfile(
        tier=RiskTier.APPROVAL,
        idempotent=False,
        reversible=True,
        max_blast_radius=10,
        min_confidence=0.90,
        description="Promote a verified candidate patch toward production.",
    ),
    ActionType.DRAIN_INSTANCE: ActionProfile(
        tier=RiskTier.APPROVAL,
        idempotent=True,
        reversible=True,
        max_blast_radius=5,
        min_confidence=0.80,
        description="Drain and remove an instance from rotation.",
    ),
    # ---- Tier 3: representable so policy can name them, never executable ----
    ActionType.DELETE_DATA: ActionProfile(
        tier=RiskTier.HUMAN_ONLY,
        idempotent=False,
        reversible=False,
        max_blast_radius=0,
        min_confidence=1.0,
        description="Destructive data operation. No autonomous path exists.",
    ),
    ActionType.ROTATE_SECRET: ActionProfile(
        tier=RiskTier.HUMAN_ONLY,
        idempotent=False,
        reversible=False,
        max_blast_radius=0,
        min_confidence=1.0,
        description="Secret rotation. No autonomous path exists.",
    ),
    ActionType.RUN_MIGRATION: ActionProfile(
        tier=RiskTier.HUMAN_ONLY,
        idempotent=False,
        reversible=False,
        max_blast_radius=0,
        min_confidence=1.0,
        description="Irreversible schema migration. No autonomous path exists.",
    ),
    ActionType.MODIFY_SECURITY_POLICY: ActionProfile(
        tier=RiskTier.HUMAN_ONLY,
        idempotent=False,
        reversible=False,
        max_blast_radius=0,
        min_confidence=1.0,
        description="Security policy change. No autonomous path exists.",
    ),
}

# A missing profile must be a hard failure, not a permissive default.
assert set(_PROFILES) == set(ActionType), "every ActionType needs a risk profile"


def profile_for(action_type: ActionType) -> ActionProfile:
    """Return the profile, raising rather than guessing for unknown input."""
    try:
        return _PROFILES[action_type]
    except KeyError as exc:  # pragma: no cover - unreachable while the assert holds
        raise KeyError(f"no risk profile registered for {action_type!r}") from exc


def risk_tier_for(action_type: ActionType) -> RiskTier:
    return profile_for(action_type).tier


def tier_three_actions() -> frozenset[ActionType]:
    return frozenset(a for a, p in _PROFILES.items() if p.tier is RiskTier.HUMAN_ONLY)


def autonomous_candidates() -> frozenset[ActionType]:
    """Action types that could ever be autonomous, before any policy check."""
    return frozenset(a for a, p in _PROFILES.items() if p.tier is RiskTier.LOW)
