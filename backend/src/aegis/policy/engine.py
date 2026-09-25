"""The deterministic policy engine.

``decide`` is a pure function: same context in, same decision out, forever. It
performs no I/O, consults no model and reads no global state. Three consequences
that matter operationally:

* every decision is replayable from persisted state during an audit
* every rule is exhaustively unit-testable without infrastructure
* an agent can influence the outcome only by supplying different *facts*, never
  by arguing

Rules are ordered and the first decisive one wins. The default at the bottom is
deny, so a rule set that fails to match cannot accidentally permit an action.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from aegis.core.logging import get_logger
from aegis.domain.enums import PolicyEffect, RiskTier
from aegis.domain.models import GateResult, PolicyDecision
from aegis.policy.context import PolicyContext
from aegis.policy.tiers import profile_for, risk_tier_for

log = get_logger(__name__)

POLICY_VERSION: Final = "1.0.0"

_MIN_EVIDENCE_QUALITY: Final[dict[RiskTier, float]] = {
    RiskTier.OBSERVE: 0.0,
    RiskTier.LOW: 0.45,
    RiskTier.APPROVAL: 0.60,
    RiskTier.HUMAN_ONLY: 1.0,
}


class _Outcome:
    """Internal rule result. ``None`` from a rule means 'no opinion'."""

    __slots__ = ("effect", "rule", "reason")

    def __init__(self, effect: PolicyEffect, rule: str, reason: str) -> None:
        self.effect = effect
        self.rule = rule
        self.reason = reason


Rule = Callable[[PolicyContext], "_Outcome | None"]


def _rule_kill_switch(ctx: PolicyContext) -> _Outcome | None:
    engaged, scope = ctx.kill_switch.engaged_for(
        environment=ctx.environment,
        action_type=ctx.action_type,
        service_id=ctx.service_id,
    )
    if engaged:
        return _Outcome(PolicyEffect.BLOCK, "kill_switch", scope)
    return None


def _rule_tier_three(ctx: PolicyContext) -> _Outcome | None:
    """Tier 3 is never executable by any path, approved or not."""
    if risk_tier_for(ctx.action_type) is RiskTier.HUMAN_ONLY:
        return _Outcome(
            PolicyEffect.BLOCK,
            "tier_three_prohibited",
            f"{ctx.action_type.value} is tier 3; no autonomous path exists",
        )
    return None


def _rule_concurrency(ctx: PolicyContext) -> _Outcome | None:
    if ctx.resource_lease_held:
        return _Outcome(
            PolicyEffect.BLOCK,
            "resource_locked",
            f"another action holds the lease on {ctx.resource_id}",
        )
    return None


def _rule_rate_limit(ctx: PolicyContext) -> _Outcome | None:
    if ctx.actions_last_hour >= ctx.max_actions_per_hour:
        return _Outcome(
            PolicyEffect.BLOCK,
            "rate_limited",
            f"{ctx.actions_last_hour} autonomous actions in the last hour "
            f"reaches the limit of {ctx.max_actions_per_hour}",
        )
    return None


def _rule_abstained_diagnosis(ctx: PolicyContext) -> _Outcome | None:
    """Acting on an explicit 'we do not know' is never autonomous."""
    if ctx.has_abstained_diagnosis:
        return _Outcome(
            PolicyEffect.REQUIRE_HUMAN,
            "abstained_diagnosis",
            "diagnosis abstained; a human must decide",
        )
    return None


def _rule_autonomy_disabled(ctx: PolicyContext) -> _Outcome | None:
    if not ctx.autonomy_enabled:
        return _Outcome(
            PolicyEffect.REQUIRE_HUMAN, "autonomy_disabled", "autonomy is disabled"
        )
    tier = risk_tier_for(ctx.action_type)
    if int(tier) not in ctx.allowed_tiers:
        return _Outcome(
            PolicyEffect.REQUIRE_HUMAN,
            "tier_not_permitted",
            f"tier {int(tier)} is not in the autonomous allowlist",
        )
    return None


def _rule_evidence_quality(ctx: PolicyContext) -> _Outcome | None:
    tier = risk_tier_for(ctx.action_type)
    required = _MIN_EVIDENCE_QUALITY[tier]
    quality = ctx.evidence_quality
    if quality < required:
        return _Outcome(
            PolicyEffect.REQUIRE_HUMAN,
            "insufficient_evidence_quality",
            f"evidence quality {quality:.2f} is below {required:.2f} for tier {int(tier)}",
        )
    if ctx.tier_a_evidence_count == 0:
        return _Outcome(
            PolicyEffect.REQUIRE_HUMAN,
            "no_direct_observation",
            "no Tier-A machine observation supports this action",
        )
    return None


def _rule_confidence_floor(ctx: PolicyContext) -> _Outcome | None:
    floor = profile_for(ctx.action_type).min_confidence
    if ctx.diagnosis_confidence < floor:
        return _Outcome(
            PolicyEffect.REQUIRE_HUMAN,
            "confidence_below_floor",
            f"confidence {ctx.diagnosis_confidence:.2f} is below "
            f"{floor:.2f} for {ctx.action_type.value}",
        )
    return None


def _rule_blast_radius(ctx: PolicyContext) -> _Outcome | None:
    profile = profile_for(ctx.action_type)
    size = ctx.blast_radius.size
    if size > profile.max_blast_radius:
        return _Outcome(
            PolicyEffect.REQUIRE_HUMAN,
            "blast_radius_exceeded",
            f"blast radius {size} exceeds {profile.max_blast_radius} "
            f"for {ctx.action_type.value}",
        )
    if ctx.blast_radius.customer_facing and ctx.is_production:
        return _Outcome(
            PolicyEffect.REQUIRE_HUMAN,
            "customer_facing_production",
            "action touches a customer-facing path in production",
        )
    return None


def _rule_rollback_required(ctx: PolicyContext) -> _Outcome | None:
    """ESD section 39: no known safe reversal, no autonomy."""
    if ctx.rollback is None:
        return _Outcome(
            PolicyEffect.REQUIRE_HUMAN,
            "no_rollback_path",
            "no rollback or compensating action is defined",
        )
    return None


def _rule_verification_required(ctx: PolicyContext) -> _Outcome | None:
    if profile_for(ctx.action_type).requires_verification and ctx.verification is None:
        return _Outcome(
            PolicyEffect.REQUIRE_HUMAN,
            "no_verification_plan",
            "success cannot be measured; no verification plan supplied",
        )
    return None


def _rule_production_allowlist(ctx: PolicyContext) -> _Outcome | None:
    if not ctx.is_production:
        return None
    if ctx.service_id and ctx.service_id not in ctx.service_allowlist:
        return _Outcome(
            PolicyEffect.REQUIRE_HUMAN,
            "service_not_allowlisted",
            f"{ctx.service_id} is not allowlisted for autonomous production action",
        )
    return None


_RULES: Final[tuple[Rule, ...]] = (
    _rule_kill_switch,
    _rule_tier_three,
    _rule_concurrency,
    _rule_rate_limit,
    _rule_abstained_diagnosis,
    _rule_autonomy_disabled,
    _rule_evidence_quality,
    _rule_confidence_floor,
    _rule_blast_radius,
    _rule_rollback_required,
    _rule_verification_required,
    _rule_production_allowlist,
)


def decide(ctx: PolicyContext) -> PolicyDecision:
    """Evaluate every rule and return the decision.

    All rules run even after the first decisive one, so the persisted decision
    records *every* reason an action was held back rather than only the first.
    An operator fixing one blocker then learns immediately about the next.
    """
    risk_tier = risk_tier_for(ctx.action_type)  # independent of the effect
    gates: list[GateResult] = []
    decisive: _Outcome | None = None

    for rule in _RULES:
        outcome = rule(ctx)
        name = rule.__name__.removeprefix("_rule_")
        if outcome is None:
            gates.append(GateResult(gate=name, passed=True))
            continue
        gates.append(GateResult(gate=name, passed=False, reason=outcome.reason))
        # BLOCK is strictly stronger than REQUIRE_HUMAN and wins outright.
        if decisive is None or (
            outcome.effect is PolicyEffect.BLOCK and decisive.effect is not PolicyEffect.BLOCK
        ):
            decisive = outcome

    if decisive is None:
        decision = PolicyDecision(
            effect=PolicyEffect.ALLOW,
            risk_tier=risk_tier,
            matched_rule="all_gates_passed",
            reasons=[],
            gates=gates,
            policy_version=POLICY_VERSION,
            decided_at=ctx.now,
        )
    else:
        decision = PolicyDecision(
            effect=decisive.effect,
            risk_tier=risk_tier,
            matched_rule=decisive.rule,
            reasons=[g.reason for g in gates if not g.passed],
            gates=gates,
            policy_version=POLICY_VERSION,
            decided_at=ctx.now,
        )

    log.info(
        "policy decision",
        action_type=ctx.action_type.value,
        effect=decision.effect.value,
        risk_tier=int(risk_tier),
        matched_rule=decision.matched_rule,
        environment=ctx.environment,
        resource_id=ctx.resource_id,
    )
    return decision


__all__ = ["POLICY_VERSION", "decide"]
