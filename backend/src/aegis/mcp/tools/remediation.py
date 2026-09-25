"""Remediation tools, and a classification worth reading carefully.

Three tools, one write.

``propose_action`` is **read**-classified. It builds an ``ActionProposal`` and
returns it. A proposal is a request with no authority whatsoever: it is not
persisted here, it does not reach an executor, and the type an executor accepts
- ``ValidatedAction`` - cannot be constructed from it without the gate chain.
Classifying a proposal as a write would be worse than pedantic; it would force
the tool to demand a ``ValidatedAction`` it exists to precede, and the agent
would have nothing left to propose with. The proposal's blast radius is measured
from the topology graph rather than taken from the agent, because "this change
is small" is exactly the claim a model should not be trusted to assert.

``request_approval`` is **read**-classified too, and that deserves a sentence.
It does change Aegis state - it opens an approval request - so it is declared
``mutates="aegis_state"``. What it cannot do is change anything outside Aegis,
and it emphatically cannot approve anything: only a human, through the API,
reaches ``ApprovalStore.decide``. The read/write axis on this boundary means
"can this reach the environment", because that is the axis the gate chain
protects. A tool that asks a human for permission is the opposite of a tool that
acts without it.

``execute_validated_action`` is the **only** write tool in Aegis. It requires a
``ValidatedAction``, it hands that action straight to ``ExecutionService``,
which owns execute -> verify -> commit/rollback, and it performs no environment
call of its own. There is no other path from this package to a mutation.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field

from aegis.core.errors import DomainError, ValidationError
from aegis.core.ids import ACTION, new_id
from aegis.core.logging import get_logger
from aegis.domain.enums import (
    ActionType,
    AgentRole,
    EvidenceType,
    MetricDirection,
    SourceType,
)
from aegis.domain.models import (
    ActionProposal,
    BlastRadius,
    ExpectedEffect,
    ResourceRef,
    RollbackPlan,
    VerificationPlan,
)
from aegis.execution.validated import ValidatedAction
from aegis.mcp.deps import ToolDeps
from aegis.mcp.registry import ToolRegistry
from aegis.mcp.tools import support
from aegis.mcp.types import (
    ENVIRONMENTS,
    ToolContext,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSpec,
)
from aegis.policy.tiers import risk_tier_for

log = get_logger(__name__)

ArgValue = str | int | float | bool

# Spelled out rather than derived from the enum: pydantic strict mode rejects a
# plain string for an enum field, and every MCP client sends plain strings.
ActionTypeLiteral = Literal[
    "restart_instance",
    "rerun_health_check",
    "scale_up_bounded",
    "clear_cache_key",
    "rollback_deployment",
    "scale_service",
    "update_config",
    "promote_patch",
    "drain_instance",
    "delete_data",
    "rotate_secret",
    "run_migration",
    "modify_security_policy",
]


# --------------------------------------------------------------------------- #
# models                                                                       #
# --------------------------------------------------------------------------- #


class ProposeActionInput(ToolInput):
    action_type: ActionTypeLiteral
    resource_type: Literal["service", "instance", "deployment", "config", "cache"]
    resource_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=10, max_length=2_000)
    supporting_evidence: list[str] = Field(min_length=1, max_length=50)
    expected_metric: str = Field(min_length=1, max_length=120)
    expected_direction: Literal["increase", "decrease", "stable"]
    expected_threshold: float
    service_id: str | None = Field(default=None, max_length=200)
    arguments: dict[str, ArgValue] = Field(default_factory=dict, max_length=16)
    observation_window_s: int = Field(default=300, ge=30, le=3_600)
    protected_metrics: list[str] = Field(default_factory=list, max_length=10)
    regression_tolerance: float = Field(default=0.05, ge=0.0, le=1.0)
    rollback_strategy: (
        Literal[
            "inverse_action",
            "compensating_action",
            "pipeline_rollback",
            "immutable_redeploy",
        ]
        | None
    ) = None
    rollback_description: str = Field(default="", max_length=500)
    rollback_automatic: bool = False


class ProposalOut(ToolOutput):
    action_id: str
    incident_id: str
    action_type: str
    risk_tier: int
    risk_tier_name: str
    resource_type: str
    resource_id: str
    environment: str
    idempotency_key: str
    supporting_evidence: tuple[str, ...]
    arguments: dict[str, ArgValue]
    expected_metric: str
    expected_direction: str
    expected_threshold: float
    observation_window_s: int
    directly_affected: tuple[str, ...] = ()
    downstream: tuple[str, ...] = ()
    customer_facing: bool = False
    estimated_request_share: float = 0.0
    blast_radius_measured: bool = False
    has_rollback: bool = False
    # Stated on every proposal so no reader of this result can mistake it for a
    # permission. Nothing an agent produces is executable.
    executable: Literal[False] = False
    next_step: str = "submit to ActionGate.validate; execution requires approval by policy"


class RequestApprovalInput(ToolInput):
    action_id: str = Field(min_length=3, max_length=80)
    note: str = Field(default="", max_length=500)


class ApprovalOut(ToolOutput):
    approval_id: str
    action_id: str
    incident_id: str
    requested_at: str
    expires_at: str
    decision: str | None = None
    already_open: bool = False


class ExecuteActionInput(ToolInput):
    # The caller states which action it believes it is executing. If that does
    # not match the ValidatedAction the gate chain minted, the call is refused:
    # a mismatch means the authorisation and the intent have come apart.
    action_id: str = Field(min_length=3, max_length=80)
    settle_seconds: float = Field(default=30.0, ge=0.0, le=300.0)
    observation_window_s: int | None = Field(default=None, ge=30, le=3_600)


class ExecutionReportOut(ToolOutput):
    action_id: str
    incident_id: str
    executed: bool
    succeeded: bool
    final_state: str
    performed: str | None = None
    changed: bool | None = None
    error: str | None = None
    verdict: str | None = None
    verification_id: str | None = None
    rolled_back: bool = False
    rollback_error: str | None = None
    escalated: bool = False
    escalation_reason: str = ""


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def idempotency_key_for(
    incident_id: str,
    action_type: str,
    resource_id: str,
    arguments: dict[str, ArgValue],
) -> str:
    """A stable key for one intent.

    Derived rather than generated so that the same proposal made twice - by a
    retried workflow, or by two investigators reaching the same conclusion -
    collapses into one action instead of two executions.
    """
    parts = [incident_id, action_type, resource_id]
    parts += [f"{k}={arguments[k]!r}" for k in sorted(arguments)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# registration                                                                 #
# --------------------------------------------------------------------------- #


def register(registry: ToolRegistry, deps: ToolDeps) -> None:
    """Declare the remediation tools against an injected dependency set."""

    async def propose_action(context: ToolContext, args: ProposeActionInput) -> ToolOutcome:
        if not context.incident_id:
            raise ValidationError("a proposal must belong to an incident")

        action_type = ActionType(args.action_type)
        tier = risk_tier_for(action_type)
        target = ResourceRef(
            resource_type=args.resource_type,
            resource_id=args.resource_id,
            environment=context.environment,
            service_id=args.service_id,
        )

        # Blast radius is measured, never asserted. If the graph cannot be read
        # the proposal still forms, but it carries an unmeasured radius and the
        # result is marked degraded - and policy treats an unmeasured radius as
        # the risk it is.
        radius = BlastRadius()
        measured = False
        degraded_reason = ""
        if deps.traversal is not None and args.service_id:
            try:
                radius = await deps.traversal.blast_radius(args.service_id)
                measured = True
            except Exception as exc:  # noqa: BLE001 - a proposal survives a graph outage
                degraded_reason = f"blast radius unmeasured: {type(exc).__name__}"
                log.warning(
                    "blast radius could not be measured for a proposal",
                    service_id=args.service_id, incident_id=context.incident_id,
                )
        elif not args.service_id:
            degraded_reason = "blast radius unmeasured: proposal names no service"
        else:
            degraded_reason = "blast radius unmeasured: topology graph is not configured"

        rollback = None
        if args.rollback_strategy is not None:
            rollback = RollbackPlan(
                strategy=args.rollback_strategy,
                description=args.rollback_description or args.rollback_strategy,
                automatic=args.rollback_automatic,
            )

        action_id = new_id(ACTION)
        key = idempotency_key_for(
            context.incident_id, args.action_type, args.resource_id, args.arguments
        )
        # Constructed rather than hand-rolled: ActionProposal's own validators
        # reject an unbounded argument set or a proposal citing no evidence, so
        # a malformed proposal fails here instead of at the gate.
        proposal = ActionProposal(
            id=action_id,
            incident_id=context.incident_id,
            action_type=action_type,
            target=target,
            reason=args.reason,
            arguments=dict(args.arguments),
            supporting_evidence=list(args.supporting_evidence),
            expected_effect=ExpectedEffect(
                metric=args.expected_metric,
                direction=MetricDirection(args.expected_direction),
                threshold=args.expected_threshold,
                window_seconds=args.observation_window_s,
                resource_id=args.resource_id,
            ),
            blast_radius=radius,
            rollback=rollback,
            verification=VerificationPlan(
                target_metric=args.expected_metric,
                direction=MetricDirection(args.expected_direction),
                threshold=args.expected_threshold,
                observation_window_s=args.observation_window_s,
                protected_metrics=list(args.protected_metrics),
                regression_tolerance=args.regression_tolerance,
            ),
            idempotency_key=key,
            proposed_by=AgentRole.REMEDIATION_PLANNER,
            proposed_at=deps.clock.now(),
        )

        value = ProposalOut(
            action_id=proposal.id,
            incident_id=proposal.incident_id,
            action_type=proposal.action_type.value,
            risk_tier=int(tier),
            risk_tier_name=tier.name,
            resource_type=target.resource_type,
            resource_id=target.resource_id,
            environment=target.environment,
            idempotency_key=proposal.idempotency_key,
            supporting_evidence=tuple(proposal.supporting_evidence),
            arguments=dict(proposal.arguments),
            expected_metric=proposal.expected_effect.metric,
            expected_direction=proposal.expected_effect.direction.value,
            expected_threshold=proposal.expected_effect.threshold,
            observation_window_s=proposal.verification.observation_window_s,
            directly_affected=tuple(radius.directly_affected),
            downstream=tuple(radius.downstream),
            customer_facing=radius.customer_facing,
            estimated_request_share=radius.estimated_request_share,
            blast_radius_measured=measured,
            has_rollback=rollback is not None,
        )
        uri = f"aegis://proposal/{proposal.id}"
        if not measured:
            return ToolOutcome(
                value=value, provenance=(uri,),
                degraded=True, degraded_reason=degraded_reason,
            )
        # The proposal itself is not an observation and records no evidence. The
        # blast radius is: it was measured from the graph, and a later reader of
        # this action has to be able to check that measurement.
        ids = await support.record_evidence(
            deps, context, source="neo4j", source_type=SourceType.GRAPH,
            evidence_type=EvidenceType.BLAST_RADIUS,
            summary=(
                f"blast radius for {target.resource_id}: "
                f"{len(radius.directly_affected)} directly affected, "
                f"{len(radius.downstream)} downstream"
            ),
            structured_value={
                "action_type": proposal.action_type.value,
                "directly_affected": list(radius.directly_affected),
                "downstream": list(radius.downstream),
                "customer_facing": radius.customer_facing,
                "estimated_request_share": radius.estimated_request_share,
            },
            provenance_uri=uri, resource_id=target.resource_id,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    async def request_approval(
        context: ToolContext, args: RequestApprovalInput
    ) -> ToolOutcome:
        if not context.incident_id:
            raise ValidationError("an approval request must belong to an incident")
        if deps.approvals is None or deps.actions is None:
            raise DomainError(
                "approvals are not configured; a human cannot be asked from here",
                code="APPROVALS_UNAVAILABLE",
            )

        stored = await deps.actions.get(args.action_id)
        if stored is None:
            raise ValidationError(
                "unknown action", context={"action_id": args.action_id}
            )
        if stored.incident_id != context.incident_id:
            # Scoping an approval to the wrong incident would let one
            # investigation open a decision about another's action.
            raise ValidationError(
                "action belongs to a different incident",
                context={"action_id": args.action_id},
            )

        existing = await deps.approvals.open_for_action(args.action_id)
        approval = await deps.approvals.request(
            action_id=args.action_id,
            incident_id=context.incident_id,
            requested_by=context.caller.subject,
            correlation_id=context.correlation_id or None,
        )
        value = ApprovalOut(
            approval_id=approval.id,
            action_id=approval.action_id,
            incident_id=approval.incident_id,
            requested_at=approval.requested_at.isoformat(),
            expires_at=approval.expires_at.isoformat(),
            decision=approval.decision,
            already_open=existing is not None,
        )
        return ToolOutcome(value=value, provenance=(f"aegis://approval/{approval.id}",))

    async def execute_validated_action(
        context: ToolContext, args: ExecuteActionInput, validated: ValidatedAction
    ) -> ToolOutcome:
        """Hand a gate-validated action to the execution service. Nothing else.

        This function performs no runtime call itself. It cannot: the write
        ports live behind ``ExecutionService``, which owns the execute -> verify
        -> commit/rollback sequence and the lease release.

        The authorisation travels in ``validated``, not in the context, so the
        context is used only for the correlation the invoker already recorded.
        """
        log.info(
            "executing validated action",
            action_id=validated.action.id,
            incident_id=context.incident_id,
            correlation_id=context.correlation_id,
            risk_tier=int(validated.risk_tier),
            human_approved=validated.was_human_approved,
        )
        if deps.execution is None or deps.ports is None:
            raise DomainError(
                "execution service is not configured",
                code="EXECUTION_UNAVAILABLE",
            )
        if args.action_id != validated.action.id:
            raise ValidationError(
                "action_id does not match the validated action",
                context={"requested": args.action_id, "validated": validated.action.id},
            )

        report = await deps.execution.execute(
            validated,
            deps.ports,
            settle_seconds=args.settle_seconds,
            observation_window_s=args.observation_window_s,
        )
        value = ExecutionReportOut(
            action_id=report.action_id,
            incident_id=report.incident_id,
            executed=report.executed,
            succeeded=report.succeeded,
            final_state=report.final_state.value,
            performed=report.outcome.performed if report.outcome else None,
            changed=report.outcome.changed if report.outcome else None,
            error=report.outcome.error if report.outcome else None,
            verdict=report.verification.verdict.value if report.verification else None,
            verification_id=report.verification.id if report.verification else None,
            rolled_back=report.rolled_back,
            rollback_error=(
                report.rollback_outcome.error if report.rollback_outcome else None
            ),
            escalated=report.escalated,
            escalation_reason=report.escalation_reason,
        )
        if report.executed and report.verification is None:
            # Ran, but could not be proven to have worked. Never a success.
            return ToolOutcome(
                value=value,
                provenance=(f"aegis://action/{report.action_id}",),
                degraded=True,
                degraded_reason="action executed but verification did not complete",
            )
        return ToolOutcome(value=value, provenance=(f"aegis://action/{report.action_id}",))

    # ---- specs ----------------------------------------------------------- #

    registry.register(
        ToolSpec(
            name="propose_action",
            description=(
                "Build a remediation proposal with a measured blast radius. Produces a "
                "request only - it grants nothing, persists nothing and executes "
                "nothing."
            ),
            server="remediation",
            input_model=ProposeActionInput,
            output_model=ProposalOut,
            access="read",
            mutates="nothing",
            scope="remediation:propose",
            environments=ENVIRONMENTS,
            timeout_s=25.0,
            retryable=True,
            idempotent=True,
            cost_hint="cheap",
        ),
        propose_action,
    )
    registry.register(
        ToolSpec(
            name="request_approval",
            description=(
                "Ask a human to decide on an already-proposed action. Opens or returns "
                "the one open approval; it can never grant one."
            ),
            server="remediation",
            input_model=RequestApprovalInput,
            output_model=ApprovalOut,
            access="read",
            mutates="aegis_state",
            scope="remediation:approval",
            environments=ENVIRONMENTS,
            timeout_s=15.0,
            # Idempotent by the partial unique index on open approvals, but not
            # auto-retried: a duplicate approval request pages a human twice.
            retryable=False,
            idempotent=True,
            cost_hint="cheap",
        ),
        request_approval,
    )
    registry.register(
        ToolSpec(
            name="execute_validated_action",
            description=(
                "Execute an action that has passed the full gate chain, then verify it. "
                "Requires a ValidatedAction; there is no other way to reach the "
                "environment."
            ),
            server="remediation",
            input_model=ExecuteActionInput,
            output_model=ExecutionReportOut,
            access="write",
            mutates="environment",
            scope="remediation:execute",
            environments=ENVIRONMENTS,
            timeout_s=300.0,
            retryable=False,
            idempotent=False,
            cost_hint="expensive",
            requires_validated_action=True,
        ),
        execute_validated_action,
    )


__all__ = ["idempotency_key_for", "register"]
