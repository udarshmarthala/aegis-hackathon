"""The executor registry.

The registry is the second structural guarantee about tier 3, alongside the
policy rule that blocks it. ``DELETE_DATA`` and its siblings are absent from
this table entirely, so even a decision that wrongly said ALLOW would find
nothing to call. Prohibition is expressed by the absence of code rather than by
a check that could be edited away.

Lookup fails closed. An action type with no executor raises rather than
returning ``None``, because a caller that received ``None`` might treat it as
"nothing to do" and mark the action successful.
"""

from __future__ import annotations

from typing import Final

from aegis.core.errors import PolicyViolation
from aegis.core.logging import get_logger
from aegis.domain.enums import ActionType, RiskTier
from aegis.execution.executors import (
    ClearCacheKeyExecutor,
    DrainInstanceExecutor,
    Executor,
    RerunHealthCheckExecutor,
    RestartInstanceExecutor,
    RollbackDeploymentExecutor,
    ScaleServiceExecutor,
    ScaleUpBoundedExecutor,
    UpdateConfigExecutor,
)
from aegis.policy.tiers import risk_tier_for, tier_three_actions

log = get_logger(__name__)

_REGISTRY: Final[dict[ActionType, Executor]] = {
    # tier 1 - bounded, idempotent, autonomous when policy permits
    ActionType.RESTART_INSTANCE: RestartInstanceExecutor(),
    ActionType.RERUN_HEALTH_CHECK: RerunHealthCheckExecutor(),
    ActionType.SCALE_UP_BOUNDED: ScaleUpBoundedExecutor(),
    ActionType.CLEAR_CACHE_KEY: ClearCacheKeyExecutor(),
    # tier 2 - executable only behind a live human approval
    ActionType.ROLLBACK_DEPLOYMENT: RollbackDeploymentExecutor(),
    ActionType.SCALE_SERVICE: ScaleServiceExecutor(),
    ActionType.UPDATE_CONFIG: UpdateConfigExecutor(),
    ActionType.DRAIN_INSTANCE: DrainInstanceExecutor(),
    # PROMOTE_PATCH is handled by the deployment pipeline, not by a direct
    # environment write, so it has no entry here on purpose.
    # tier 3 - deliberately absent. There is nothing to call.
}

# A tier-3 executor appearing here would be a critical safety regression, so the
# assertion runs at import time rather than in a test that might be skipped.
assert not (set(_REGISTRY) & tier_three_actions()), (
    "a tier-3 action type has an executor registered; this is a safety regression"
)


def executor_for(action_type: ActionType) -> Executor:
    """Return the executor, or refuse.

    Refusal is a ``PolicyViolation`` rather than a ``KeyError`` so that callers
    treat it as a safety outcome to be audited, not as a programming bug to be
    caught and ignored.
    """
    executor = _REGISTRY.get(action_type)
    if executor is None:
        tier = risk_tier_for(action_type)
        reason = (
            "tier 3 actions have no execution path"
            if tier is RiskTier.HUMAN_ONLY
            else "no executor is registered for this action type"
        )
        log.error(
            "execution refused: no executor",
            action_type=action_type.value,
            risk_tier=int(tier),
        )
        raise PolicyViolation(
            f"{action_type.value}: {reason}",
            context={"action_type": action_type.value, "risk_tier": int(tier)},
        )
    return executor


def has_executor(action_type: ActionType) -> bool:
    return action_type in _REGISTRY


def executable_action_types() -> frozenset[ActionType]:
    """What Aegis can actually perform, for the action-registry UI surface."""
    return frozenset(_REGISTRY)


__all__ = ["executable_action_types", "executor_for", "has_executor"]
