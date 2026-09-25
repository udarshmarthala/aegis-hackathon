"""The gate chain, and the only type an executor will accept.

``ValidatedAction`` is the structural heart of the safety model. An agent can
build an ``ActionProposal`` - that is its job - but it has no way to build a
``ValidatedAction``, because the constructor demands a token held privately by
this module and handed out only by ``ActionGate.validate``. There is therefore
no reachable code path in which a model's output becomes an execution without
passing every gate, and that property is enforced by the type system rather
than by reviewer vigilance.

The chain, in order (CLAUDE.md 3.4):

    schema -> evidence -> policy -> authz -> lease -> execute -> verify -> commit

This module owns the first five. ``ActionExecutor`` owns execute; the
verification engine owns the last two.

Every gate is deterministic. No gate consults a model, and no gate reads the
proposal's ``reason`` text to decide anything - a persuasive justification must
not be able to move an action into a lower risk class.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.errors import (
    AuthorizationError,
    EvidenceError,
    PolicyViolation,
    ValidationError,
)
from aegis.core.logging import get_logger
from aegis.domain.enums import ActionState, ActionType, PolicyEffect, RiskTier, Severity
from aegis.domain.models import (
    ActionProposal,
    BlastRadius,
    GateResult,
    PolicyDecision,
    ResourceRef,
    RollbackPlan,
    VerificationPlan,
)
from aegis.evidence.validator import EvidenceValidator, ValidationReport
from aegis.execution.approvals import ApprovalRequest, ApprovalStore
from aegis.execution.leases import Lease, LeaseManager
from aegis.persistence.actions import ActionRepository, StoredAction
from aegis.persistence.audit import AuditEvent, AuditLog
from aegis.policy.context import PolicyContext
from aegis.policy.engine import decide
from aegis.policy.store import PolicyStore
from aegis.policy.tiers import profile_for, risk_tier_for

log = get_logger(__name__)

# Held privately. Possession of this object is what distinguishes "the gate
# chain produced this" from "something constructed this". Moving it out of this
# module, or exporting it, would defeat the entire mechanism.
_GATE_TOKEN: Final = object()


class _Unauthorised(Exception):
    """Raised internally when a caller fabricates a ValidatedAction."""


@dataclass(frozen=True, slots=True)
class ValidatedAction:
    """An action that has passed every pre-execution gate.

    Constructing one outside ``ActionGate`` raises. Executors accept nothing
    else, so there is no way to reach production without the gates having run.
    """

    token: Any
    action: StoredAction
    proposal: ActionProposal
    decision: PolicyDecision
    evidence_report: ValidationReport
    lease: Lease
    approval: ApprovalRequest | None
    validated_at: datetime
    correlation_id: str

    def __post_init__(self) -> None:
        if self.token is not _GATE_TOKEN:
            raise _Unauthorised(
                "ValidatedAction may only be constructed by ActionGate.validate"
            )

    @property
    def action_type(self) -> ActionType:
        return self.action.action_type

    @property
    def risk_tier(self) -> RiskTier:
        return self.decision.risk_tier

    @property
    def target(self) -> ResourceRef:
        return self.proposal.target

    @property
    def idempotency_key(self) -> str:
        return self.action.idempotency_key

    @property
    def was_human_approved(self) -> bool:
        return self.approval is not None

    def still_valid(self, now: datetime) -> bool:
        """Re-checked immediately before the write.

        A lease that lapsed or an approval that expired between validation and
        execution invalidates the authorisation. The gap is small but it is
        exactly where a stale permission would otherwise be used.
        """
        if now >= self.lease.expires_at:
            return False
        return self.approval is None or self.approval.is_usable(now)


@dataclass(frozen=True, slots=True)
class GateRejection:
    """Why an action did not reach execution, in operator language.

    Carries the full gate list, not only the first failure, so an operator who
    clears one blocker learns immediately about the next rather than discovering
    them one deploy at a time.
    """

    action_id: str
    effect: PolicyEffect
    risk_tier: RiskTier
    matched_rule: str
    reasons: list[str]
    gates: list[GateResult]
    needs_approval: bool
    approval_id: str | None = None

    @property
    def summary(self) -> str:
        head = "human approval required" if self.needs_approval else "blocked"
        return f"{head}: {self.matched_rule}"


class ActionGate:
    """Runs the pre-execution chain and mints ``ValidatedAction`` on success."""

    __slots__ = (
        "_actions", "_approvals", "_audit", "_clock", "_evidence",
        "_leases", "_policy_store", "_settings",
    )

    def __init__(
        self,
        *,
        settings: Settings,
        actions: ActionRepository,
        approvals: ApprovalStore,
        leases: LeaseManager,
        policy_store: PolicyStore,
        evidence_validator: EvidenceValidator,
        audit: AuditLog,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._settings = settings
        self._actions = actions
        self._approvals = approvals
        self._leases = leases
        self._policy_store = policy_store
        self._evidence = evidence_validator
        self._audit = audit
        self._clock = clock

    # ---- gate 1: schema -------------------------------------------------- #

    def _gate_schema(self, proposal: ActionProposal) -> None:
        """Structural validity, checked before anything expensive runs.

        Pydantic has already enforced the field-level shape. What remains is the
        set of cross-field rules that make a proposal *executable at all*: a
        known action type, a target in the environment the proposal claims, a
        verification plan whose metric is non-empty, and a non-trivial
        idempotency key.
        """
        problems: list[str] = []

        if proposal.action_type not in set(ActionType):  # pragma: no cover - enum
            problems.append(f"unknown action type {proposal.action_type!r}")

        if proposal.target.environment != proposal.target.environment.strip():
            problems.append("target environment has surrounding whitespace")

        if not proposal.target.resource_id.strip():
            problems.append("target resource_id is empty")

        if len(proposal.idempotency_key) < 8:
            problems.append("idempotency key is too short to be collision-resistant")

        profile = profile_for(proposal.action_type)
        if profile.requires_verification and not proposal.verification.target_metric.strip():
            problems.append(
                f"{proposal.action_type.value} requires a verification plan "
                "with a target metric"
            )

        if proposal.expected_effect.window_seconds <= 0:
            problems.append("expected effect window must be positive")

        if problems:
            raise ValidationError(
                "action proposal failed schema validation",
                context={"problems": problems, "action_type": proposal.action_type.value},
            )

    # ---- gate 2: evidence ------------------------------------------------ #

    async def _gate_evidence(self, proposal: ActionProposal) -> ValidationReport:
        """Citations must resolve, belong to this incident and not be refuted.

        A proposal grounded in evidence from another incident, or in an id the
        model invented, is rejected outright rather than executed with a caveat.
        """
        report = await self._evidence.validate_citations(
            proposal.incident_id, list(proposal.supporting_evidence)
        )
        if not report.valid:
            raise EvidenceError(
                "action proposal is not grounded in valid evidence",
                context={
                    "incident_id": proposal.incident_id,
                    "action_type": proposal.action_type.value,
                    "problems": report.problems,
                },
            )
        return report

    # ---- gate 3: policy -------------------------------------------------- #

    async def _build_context(
        self,
        proposal: ActionProposal,
        *,
        report: ValidationReport,
        severity: Severity,
        diagnosis_confidence: float,
        has_abstained_diagnosis: bool,
        contradicting: int,
        lease_held: bool,
    ) -> PolicyContext:
        """Assemble the complete, explicit input to the policy decision.

        Every field is read from persisted state rather than passed in by the
        caller wherever that is possible, so an agent cannot influence a gate by
        supplying a flattering number. Kill switches load fail-closed: an
        unreadable store yields a state in which everything is engaged.
        """
        kill_switch = await self._policy_store.load_kill_switches()
        environment = proposal.target.environment
        actions_last_hour = await self._policy_store.autonomous_actions_last_hour(environment)

        blast: BlastRadius = proposal.blast_radius
        rollback: RollbackPlan | None = proposal.rollback
        verification: VerificationPlan = proposal.verification

        return PolicyContext(
            action_type=proposal.action_type,
            environment=environment,
            service_id=proposal.target.service_id,
            resource_id=proposal.target.resource_id,
            incident_severity=severity,
            diagnosis_confidence=diagnosis_confidence,
            evidence_count=len(report.resolved),
            tier_a_evidence_count=report.tier_a_count,
            contradicting_evidence_count=contradicting,
            has_abstained_diagnosis=has_abstained_diagnosis,
            blast_radius=blast,
            rollback=rollback,
            verification=verification,
            verification_passed=False,
            autonomy_enabled=self._settings.autonomy_enabled,
            allowed_tiers=self._settings.allowed_tiers,
            kill_switch=kill_switch,
            service_allowlist=self._settings.service_allowlist,
            actions_last_hour=actions_last_hour,
            max_actions_per_hour=self._settings.autonomy_max_actions_per_hour,
            resource_lease_held=lease_held,
            now=self._clock.now(),
            is_production=self._settings.is_production,
        )

    @staticmethod
    def _snapshot(ctx: PolicyContext) -> dict[str, Any]:
        """A replayable record of exactly what policy saw.

        Stored alongside the decision so an auditor can rebuild the context and
        confirm the same rules still produce the same effect.
        """
        return {
            "action_type": ctx.action_type.value,
            "environment": ctx.environment,
            "service_id": ctx.service_id,
            "resource_id": ctx.resource_id,
            "incident_severity": ctx.incident_severity.value,
            "diagnosis_confidence": ctx.diagnosis_confidence,
            "evidence_count": ctx.evidence_count,
            "tier_a_evidence_count": ctx.tier_a_evidence_count,
            "contradicting_evidence_count": ctx.contradicting_evidence_count,
            "has_abstained_diagnosis": ctx.has_abstained_diagnosis,
            "evidence_quality": round(ctx.evidence_quality, 4),
            "blast_radius_size": ctx.blast_radius.size,
            "customer_facing": ctx.blast_radius.customer_facing,
            "has_rollback": ctx.rollback is not None,
            "has_verification": ctx.verification is not None,
            "autonomy_enabled": ctx.autonomy_enabled,
            "allowed_tiers": sorted(ctx.allowed_tiers),
            "kill_switch_engaged": ctx.kill_switch.any_engaged,
            "kill_switch_degraded": ctx.kill_switch.degraded,
            "actions_last_hour": ctx.actions_last_hour,
            "max_actions_per_hour": ctx.max_actions_per_hour,
            "resource_lease_held": ctx.resource_lease_held,
            "is_production": ctx.is_production,
            "decided_for_time": ctx.now.isoformat(),
        }

    # ---- the chain ------------------------------------------------------- #

    async def validate(
        self,
        proposal: ActionProposal,
        *,
        severity: Severity,
        diagnosis_confidence: float,
        has_abstained_diagnosis: bool,
        contradicting_evidence: int = 0,
        correlation_id: str = "",
        holder: str = "worker",
    ) -> ValidatedAction | GateRejection:
        """Run every gate. Returns a ValidatedAction or an explained rejection.

        A rejection is a normal outcome, not an error - most proposals in a
        healthy deployment should stop at the policy or approval gate. Genuine
        faults (a malformed proposal, fabricated citations) raise instead, so
        that a bug is never mistaken for a policy decision.

        The lease is acquired LAST, after policy and authorisation have already
        said yes. Taking it earlier would block other work on the resource while
        a proposal sat waiting for a human, sometimes for the full approval TTL.
        """
        self._gate_schema(proposal)
        report = await self._gate_evidence(proposal)

        stored, created = await self._actions.propose(proposal)
        # An action parked for a human is re-gated in full when the decision
        # arrives - that is the whole point of "approval authorises, it does not
        # bypass". Without this, re-validating an approved action collided with
        # its own idempotency key and was refused as already in progress, so no
        # approved action could ever execute. Once a human has been asked, only
        # a human can release it (see ``awaiting_human`` below).
        awaiting_human = not created and stored.state is ActionState.HUMAN_REQUIRED
        if not created and stored.state is not ActionState.PROPOSED and not awaiting_human:
            # An identical proposal already progressed. Returning its current
            # decision keeps a retried workflow idempotent instead of racing.
            log.info(
                "proposal already progressed; not re-gating",
                action_id=stored.id,
                state=stored.state.value,
            )
            existing = await self._actions.latest_decision(stored.id)
            return GateRejection(
                action_id=stored.id,
                effect=existing.effect if existing else PolicyEffect.REQUIRE_HUMAN,
                risk_tier=risk_tier_for(stored.action_type),
                matched_rule="already_in_progress",
                reasons=[f"action is already {stored.state.value}"],
                gates=[],
                needs_approval=stored.state is ActionState.HUMAN_REQUIRED,
            )

        await self._audit.record(
            event_type=AuditEvent.ACTION_PROPOSED,
            actor=f"agent:{proposal.proposed_by.value}",
            actor_type="agent",
            incident_id=proposal.incident_id,
            resource_type=proposal.target.resource_type,
            resource_id=proposal.target.resource_id,
            detail={
                "action_id": stored.id,
                "action_type": proposal.action_type.value,
                "evidence_count": len(report.resolved),
            },
            correlation_id=correlation_id,
        )

        lease_held = await self._leases.is_held(proposal.target)
        ctx = await self._build_context(
            proposal,
            report=report,
            severity=severity,
            diagnosis_confidence=diagnosis_confidence,
            has_abstained_diagnosis=has_abstained_diagnosis,
            contradicting=contradicting_evidence,
            lease_held=lease_held,
        )
        decision = decide(ctx)
        await self._actions.record_decision(
            action_id=stored.id,
            incident_id=proposal.incident_id,
            decision=decision,
            context_snapshot=self._snapshot(ctx),
        )
        await self._audit.record(
            event_type=AuditEvent.ACTION_POLICY_DECIDED,
            actor="system:policy_engine",
            actor_type="system",
            incident_id=proposal.incident_id,
            resource_type="action",
            resource_id=stored.id,
            detail={
                "effect": decision.effect.value,
                "risk_tier": int(decision.risk_tier),
                "matched_rule": decision.matched_rule,
                "reasons": decision.reasons,
            },
            correlation_id=correlation_id,
        )

        if decision.effect is PolicyEffect.BLOCK:
            await self._actions.transition(stored.id, to=ActionState.BLOCKED)
            await self._audit.record(
                event_type=AuditEvent.ACTION_BLOCKED,
                actor="system:policy_engine",
                actor_type="system",
                incident_id=proposal.incident_id,
                resource_type="action",
                resource_id=stored.id,
                detail={"matched_rule": decision.matched_rule},
                correlation_id=correlation_id,
            )
            return GateRejection(
                action_id=stored.id,
                effect=decision.effect,
                risk_tier=decision.risk_tier,
                matched_rule=decision.matched_rule,
                reasons=list(decision.reasons),
                gates=list(decision.gates),
                needs_approval=False,
            )

        # ---- gate 4: authorisation --------------------------------------- #
        approval: ApprovalRequest | None = None
        # ``awaiting_human``: policy may have relaxed since the request was
        # opened, but an operator is already looking at this exact action. A
        # silent autonomous run underneath their open approval would be worse
        # than waiting for them.
        if decision.effect is PolicyEffect.REQUIRE_HUMAN or awaiting_human:
            granted = await self._approvals.granted_for_action(stored.id)
            now = self._clock.now()
            if granted is not None and granted.is_usable(now):
                approval = granted
                log.info(
                    "using existing human approval",
                    action_id=stored.id,
                    approval_id=granted.id,
                    decided_by=granted.decided_by,
                )
            else:
                # Either nobody has approved this yet, or an approval lapsed.
                # Both mean "not authorised now", but they are recorded
                # distinctly so the operator sees which one happened.
                lapsed = granted is not None
                request = await self._approvals.request(
                    action_id=stored.id,
                    incident_id=proposal.incident_id,
                    requested_by=f"agent:{proposal.proposed_by.value}",
                    correlation_id=correlation_id,
                )
                await self._actions.transition(
                    stored.id, to=ActionState.HUMAN_REQUIRED
                )
                reasons = list(decision.reasons)
                if lapsed:
                    reasons.append(
                        "a previous approval expired before the action could run"
                    )
                return GateRejection(
                    action_id=stored.id,
                    effect=decision.effect,
                    risk_tier=decision.risk_tier,
                    matched_rule=decision.matched_rule,
                    reasons=reasons,
                    gates=list(decision.gates),
                    needs_approval=True,
                    approval_id=request.id,
                )

        # A tier-3 action must never reach here. The policy engine blocks it and
        # the executor registry holds no executor for it, but this third check
        # costs nothing and turns a hypothetical bug in either into a refusal
        # rather than a production mutation.
        if risk_tier_for(proposal.action_type) is RiskTier.HUMAN_ONLY:
            raise PolicyViolation(
                f"{proposal.action_type.value} is tier 3 and has no execution path",
                context={"action_id": stored.id},
            )

        # Autonomous path. Re-assert that autonomy is actually enabled for this
        # tier rather than trusting the decision object alone.
        if (
            decision.effect is PolicyEffect.ALLOW
            and approval is None
            and int(decision.risk_tier) not in self._settings.allowed_tiers
        ):
            raise AuthorizationError(
                "policy allowed an action whose tier is not autonomously permitted",
                context={
                    "action_id": stored.id,
                    "risk_tier": int(decision.risk_tier),
                    "allowed": sorted(self._settings.allowed_tiers),
                },
            )

        # ---- gate 5: lease ------------------------------------------------ #
        lease = await self._leases.acquire(
            proposal.target,
            holder=holder,
            incident_id=proposal.incident_id,
            correlation_id=correlation_id,
        )

        validated = ValidatedAction(
            token=_GATE_TOKEN,
            action=await self._actions.transition(stored.id, to=ActionState.APPROVED),
            proposal=proposal,
            decision=decision,
            evidence_report=report,
            lease=lease,
            approval=approval,
            validated_at=self._clock.now(),
            correlation_id=correlation_id,
        )
        await self._audit.record(
            event_type=AuditEvent.ACTION_VALIDATED,
            actor="system:action_gate",
            actor_type="system",
            incident_id=proposal.incident_id,
            resource_type="action",
            resource_id=stored.id,
            detail={
                "risk_tier": int(decision.risk_tier),
                "autonomous": approval is None,
                "approved_by": approval.decided_by if approval else None,
                "lease_id": lease.id,
            },
            correlation_id=correlation_id,
        )
        log.info(
            "action validated",
            action_id=stored.id,
            action_type=proposal.action_type.value,
            risk_tier=int(decision.risk_tier),
            autonomous=approval is None,
        )
        return validated


__all__ = ["ActionGate", "GateRejection", "ValidatedAction"]
