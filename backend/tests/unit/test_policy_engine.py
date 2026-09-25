"""Policy engine rules.

The policy engine is the component where a bug is a safety incident, so these
tests assert behaviour rule by rule rather than through a happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aegis.domain.enums import ActionType, PolicyEffect, RiskTier, Severity
from aegis.domain.models import BlastRadius, RollbackPlan, VerificationPlan
from aegis.policy.context import PolicyContext
from aegis.policy.engine import decide
from aegis.policy.killswitch import KillSwitchState

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

ROLLBACK = RollbackPlan(strategy="inverse_action", description="restart again", automatic=True)
VERIFY = VerificationPlan(target_metric="error_rate", direction="decrease", threshold=0.01)


def ctx(**over: object) -> PolicyContext:
    """A context that is ALLOW by default; each test perturbs one dimension."""
    base = {
        "action_type": ActionType.RESTART_INSTANCE,
        "environment": "local",
        "service_id": "local:demo:payment",
        "resource_id": "payment-1",
        "incident_severity": Severity.P2,
        "diagnosis_confidence": 0.9,
        "evidence_count": 6,
        "tier_a_evidence_count": 4,
        "contradicting_evidence_count": 0,
        "has_abstained_diagnosis": False,
        "blast_radius": BlastRadius(directly_affected=["payment"]),
        "rollback": ROLLBACK,
        "verification": VERIFY,
        "verification_passed": True,
        "autonomy_enabled": True,
        "allowed_tiers": frozenset({1}),
        "kill_switch": KillSwitchState(),
        "service_allowlist": frozenset({"local:demo:payment"}),
        "actions_last_hour": 0,
        "max_actions_per_hour": 10,
        "resource_lease_held": False,
        "now": NOW,
        "is_production": False,
    }
    base.update(over)
    return PolicyContext(**base)  # type: ignore[arg-type]


def test_clean_tier1_action_is_allowed() -> None:
    d = decide(ctx())
    assert d.effect is PolicyEffect.ALLOW
    assert d.risk_tier is RiskTier.LOW
    assert d.matched_rule == "all_gates_passed"


@pytest.mark.parametrize(
    "action_type",
    [
        ActionType.DELETE_DATA,
        ActionType.ROTATE_SECRET,
        ActionType.RUN_MIGRATION,
        ActionType.MODIFY_SECURITY_POLICY,
    ],
)
def test_tier3_is_always_blocked(action_type: ActionType) -> None:
    """No combination of confidence or evidence can make tier 3 executable."""
    d = decide(ctx(action_type=action_type, diagnosis_confidence=1.0, evidence_count=100,
                   tier_a_evidence_count=100))
    assert d.effect is PolicyEffect.BLOCK
    assert d.risk_tier is RiskTier.HUMAN_ONLY


def test_global_kill_switch_blocks() -> None:
    d = decide(ctx(kill_switch=KillSwitchState(global_engaged=True, reason="operator")))
    assert d.effect is PolicyEffect.BLOCK
    assert d.matched_rule == "kill_switch"


def test_unreadable_policy_store_fails_closed() -> None:
    d = decide(ctx(kill_switch=KillSwitchState.fail_closed("store unreachable")))
    assert d.effect is PolicyEffect.BLOCK


def test_per_service_kill_switch_blocks_only_that_service() -> None:
    ks = KillSwitchState(services=frozenset({"local:demo:payment"}))
    assert decide(ctx(kill_switch=ks)).effect is PolicyEffect.BLOCK
    assert decide(ctx(kill_switch=ks, service_id="local:demo:checkout",
                      service_allowlist=frozenset({"local:demo:checkout"}))).effect \
        is PolicyEffect.ALLOW


def test_autonomy_disabled_requires_human() -> None:
    d = decide(ctx(autonomy_enabled=False))
    assert d.effect is PolicyEffect.REQUIRE_HUMAN
    assert d.matched_rule == "autonomy_disabled"


def test_tier2_action_requires_human_when_only_tier1_allowed() -> None:
    d = decide(ctx(action_type=ActionType.ROLLBACK_DEPLOYMENT))
    assert d.effect is PolicyEffect.REQUIRE_HUMAN
    assert d.risk_tier is RiskTier.APPROVAL


def test_abstained_diagnosis_requires_human() -> None:
    assert decide(ctx(has_abstained_diagnosis=True)).effect is PolicyEffect.REQUIRE_HUMAN


def test_no_tier_a_evidence_requires_human() -> None:
    d = decide(ctx(tier_a_evidence_count=0))
    assert d.effect is PolicyEffect.REQUIRE_HUMAN
    assert "no_direct_observation" in d.matched_rule or any(
        "Tier-A" in r for r in d.reasons
    )


def test_low_confidence_requires_human() -> None:
    d = decide(ctx(diagnosis_confidence=0.10))
    assert d.effect is PolicyEffect.REQUIRE_HUMAN


def test_missing_rollback_requires_human() -> None:
    d = decide(ctx(rollback=None))
    assert d.effect is PolicyEffect.REQUIRE_HUMAN
    assert any("rollback" in r for r in d.reasons)


def test_oversized_blast_radius_requires_human() -> None:
    big = BlastRadius(directly_affected=["a", "b"], downstream=["c", "d", "e"])
    assert decide(ctx(blast_radius=big)).effect is PolicyEffect.REQUIRE_HUMAN


def test_held_lease_blocks() -> None:
    d = decide(ctx(resource_lease_held=True))
    assert d.effect is PolicyEffect.BLOCK
    assert d.matched_rule == "resource_locked"


def test_rate_limit_blocks() -> None:
    d = decide(ctx(actions_last_hour=10, max_actions_per_hour=10))
    assert d.effect is PolicyEffect.BLOCK


def test_block_outranks_require_human() -> None:
    """Both fire; BLOCK must win regardless of rule order."""
    d = decide(ctx(autonomy_enabled=False, resource_lease_held=True))
    assert d.effect is PolicyEffect.BLOCK


def test_production_non_allowlisted_service_requires_human() -> None:
    d = decide(ctx(is_production=True, environment="production",
                   service_allowlist=frozenset()))
    assert d.effect is PolicyEffect.REQUIRE_HUMAN


def test_decision_records_every_failing_gate() -> None:
    """An operator should see all blockers at once, not one at a time."""
    d = decide(ctx(autonomy_enabled=False, rollback=None, diagnosis_confidence=0.0))
    failed = {g.gate for g in d.gates if not g.passed}
    assert {"autonomy_disabled", "rollback_required", "confidence_floor"} <= failed


def test_decide_is_pure() -> None:
    """Same input, same output - the basis of replayable audit."""
    c = ctx()
    assert decide(c).model_dump() == decide(c).model_dump()


def test_risk_tier_is_independent_of_effect() -> None:
    """Tier comes from the static table even when the action is blocked."""
    d = decide(ctx(action_type=ActionType.ROLLBACK_DEPLOYMENT, autonomy_enabled=False))
    assert d.risk_tier is RiskTier.APPROVAL
