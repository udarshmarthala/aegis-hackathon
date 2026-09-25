"""Deterministic before/after verification.

The engine never decides whether a remediation worked by asking a model. It
takes a baseline measurement before the action, takes the same measurement after
an observation window, and compares the two against thresholds that were fixed
before either measurement existed.

Three rules carry the safety weight:

1. **A missing measurement is never a pass.** If Prometheus cannot be reached,
   the claim resolves ``UNAVAILABLE`` and the run cannot reach ``VERIFIED``.
   The most dangerous bug a verification system can have is reading an
   observability outage as a healthy service (PRD 13).

2. **The baseline is captured before the action runs.** Measuring afterwards and
   comparing against a remembered number would silently absorb whatever the
   action changed.

3. **Protected metrics are tested with the same machinery as the goal.** A
   remediation that halves the error rate while doubling latency produces
   ``REGRESSION_DETECTED``, not a pass with a footnote.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import SourceUnavailable
from aegis.core.ids import VERIFICATION, new_id
from aegis.core.logging import get_logger
from aegis.domain.enums import (
    ClaimOutcome,
    EvidenceType,
    MetricDirection,
    ServiceHealth,
    SourceType,
    VerificationTestKind,
    VerificationVerdict,
)
from aegis.domain.models import VerificationPlan
from aegis.evidence.store import EvidenceStore
from aegis.execution.ports import RuntimeReadPort
from aegis.telemetry.prometheus import PrometheusClient
from aegis.verification.claims import (
    ClaimResult,
    ClaimTest,
    VerificationClaim,
    decide_verdict,
    summarise,
)

log = get_logger(__name__)

# Enough samples for a mean to mean something. A single scrape either side of a
# restart is noise, not a measurement.
MIN_SAMPLES_FOR_COMPARISON = 3


@dataclass(frozen=True, slots=True)
class Baseline:
    """What the system looked like immediately before the action.

    ``missing`` lists claims whose baseline could not be read. Those claims can
    still be measured afterwards, but no *delta* conclusion is available for
    them, so they resolve INCONCLUSIVE rather than borrowing a default.
    """

    captured_at: datetime
    window_start: float
    window_end: float
    values: dict[str, float] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)

    def value_for(self, claim_id: str) -> float | None:
        return self.values.get(claim_id)


@dataclass(frozen=True, slots=True)
class VerificationRun:
    """A completed verification, ready to persist and to render."""

    id: str
    incident_id: str
    action_id: str | None
    kind: str
    verdict: VerificationVerdict
    results: list[ClaimResult]
    baseline: Baseline
    started_at: datetime
    completed_at: datetime
    notes: str

    @property
    def passed(self) -> bool:
        return self.verdict.is_success

    @property
    def regressions(self) -> list[ClaimResult]:
        return [r for r in self.results if r.is_regression]

    @property
    def unavailable(self) -> list[ClaimResult]:
        return [r for r in self.results if r.outcome is ClaimOutcome.UNAVAILABLE]

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "action_id": self.action_id,
            "kind": self.kind,
            "verdict": self.verdict.value,
            "passed": self.passed,
            "summary": self.notes,
            "claims": [r.as_json() for r in self.results],
            "baseline": {
                "captured_at": self.baseline.captured_at.isoformat(),
                "values": self.baseline.values,
                "missing": self.baseline.missing,
            },
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
        }


def claims_from_plan(
    plan: VerificationPlan,
    *,
    resource_id: str | None,
    service_id: str | None,
) -> list[VerificationClaim]:
    """Turn the action's verification plan into testable claims.

    The goal claim comes from ``target_metric``; each protected metric becomes a
    regression claim with the plan's tolerance. Deriving both from the same plan
    means an action cannot be proposed with a goal but no regression guard.
    """
    target = service_id or resource_id
    claims: list[VerificationClaim] = [
        VerificationClaim(
            id="goal:" + plan.target_metric,
            statement=(
                f"{plan.target_metric} for {target} moves "
                f"{plan.direction.value} past {plan.threshold}"
            ),
            test=ClaimTest(
                kind=VerificationTestKind.METRIC_THRESHOLD,
                metric=plan.target_metric,
                resource_id=target,
                direction=plan.direction,
                threshold=plan.threshold,
                window_seconds=plan.observation_window_s,
            ),
            protected=False,
        )
    ]
    for metric in plan.protected_metrics:
        claims.append(
            VerificationClaim(
                id="protected:" + metric,
                statement=(
                    f"{metric} for {target} does not degrade by more than "
                    f"{plan.regression_tolerance:.0%}"
                ),
                test=ClaimTest(
                    kind=VerificationTestKind.PROTECTED_METRIC,
                    metric=metric,
                    resource_id=target,
                    direction=MetricDirection.STABLE,
                    tolerance=plan.regression_tolerance,
                    window_seconds=plan.observation_window_s,
                ),
                protected=True,
            )
        )
    if target:
        claims.append(
            VerificationClaim(
                id="health:" + target,
                statement=f"{target} reports a healthy runtime state",
                test=ClaimTest(
                    kind=VerificationTestKind.HEALTH_PROBE,
                    resource_id=target,
                    window_seconds=plan.observation_window_s,
                ),
                protected=True,
            )
        )
    return claims


class VerificationEngine:
    """Measures claims against real telemetry and the runtime."""

    __slots__ = ("_clock", "_evidence", "_prometheus", "_runtime")

    def __init__(
        self,
        *,
        prometheus: PrometheusClient,
        evidence: EvidenceStore,
        runtime: RuntimeReadPort | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._prometheus = prometheus
        self._evidence = evidence
        self._runtime = runtime
        self._clock = clock

    # ---- baseline --------------------------------------------------------- #

    async def capture_baseline(
        self, claims: list[VerificationClaim], *, window_seconds: int = 300
    ) -> Baseline:
        """Sample every metric claim before the action runs.

        A claim whose baseline cannot be read is recorded in ``missing`` with the
        reason. That is not a failure of the run - it is a fact about what will
        be knowable afterwards, and it is carried through to the verdict.
        """
        end = time.time()
        start = end - window_seconds
        values: dict[str, float] = {}
        missing: dict[str, str] = {}

        for claim in claims:
            metric = claim.test.metric
            if metric is None:
                continue
            try:
                mean = await self._sample_mean(metric, claim.test.resource_id, start, end)
            except SourceUnavailable as exc:
                missing[claim.id] = str(exc)
                log.warning(
                    "baseline unavailable",
                    claim_id=claim.id,
                    metric=metric,
                    reason=str(exc),
                )
                continue
            if mean is None:
                missing[claim.id] = "no samples in the baseline window"
                continue
            values[claim.id] = mean

        return Baseline(
            captured_at=self._clock.now(),
            window_start=start,
            window_end=end,
            values=values,
            missing=missing,
        )

    async def _sample_mean(
        self, metric: str, resource_id: str | None, start: float, end: float
    ) -> float | None:
        """Mean of a metric over a window, or None when there are no samples.

        ``None`` and ``SourceUnavailable`` are different answers and both reach
        the caller unchanged: one means the metric is quiet, the other means we
        are blind.
        """
        series = await self._prometheus.query_range(
            self._promql_for(metric, resource_id), start=start, end=end, step=15.0
        )
        points = [p.value for s in series for p in s.points]
        if len(points) < MIN_SAMPLES_FOR_COMPARISON:
            return None
        return sum(points) / len(points)

    @staticmethod
    def _promql_for(metric: str, resource_id: str | None) -> str:
        """Build the query from a small template set.

        Callers pass a metric name, never raw PromQL. The named templates are
        the same ones the investigation uses, so a verification measures the
        same quantity the incident was defined by.
        """
        from aegis.telemetry.prometheus import escape_label

        svc = escape_label(resource_id or "")
        selector = f'{{service="{svc}"}}' if svc else "{}"
        templates = {
            "error_rate": (
                f'sum(rate(http_requests_total{{service="{svc}",status=~"5.."}}[1m]))'
                f' / clamp_min(sum(rate(http_requests_total{{service="{svc}"}}[1m])), 0.001)'
            ),
            "latency_p99": (
                "histogram_quantile(0.99, sum by (le) (rate("
                f'http_request_duration_seconds_bucket{{service="{svc}"}}[1m])))'
            ),
            "latency_p50": (
                "histogram_quantile(0.50, sum by (le) (rate("
                f'http_request_duration_seconds_bucket{{service="{svc}"}}[1m])))'
            ),
            "request_rate": f'sum(rate(http_requests_total{{service="{svc}"}}[1m]))',
            "cpu_seconds": f'sum(rate(process_cpu_seconds_total{selector}[1m]))',
            "memory_bytes": f"sum(process_resident_memory_bytes{selector})",
            "saturation": f"max(aegis_saturation_ratio{selector})",
        }
        return templates.get(metric, f"sum(rate({metric}{selector}[1m]))")

    # ---- verification ----------------------------------------------------- #

    async def verify(
        self,
        *,
        incident_id: str,
        claims: list[VerificationClaim],
        baseline: Baseline,
        action_id: str | None = None,
        kind: str = "action",
        observation_window_s: int = 300,
        settle_seconds: float = 0.0,
        record_evidence: bool = True,
    ) -> VerificationRun:
        """Measure every claim after the change and decide a verdict.

        ``settle_seconds`` lets the caller wait for the environment to stabilise
        before measuring. It is an explicit, bounded sleep rather than a retry
        loop: a restarted service that is still warming up would otherwise be
        measured mid-recovery and reported as a failure.
        """
        started_at = self._clock.now()
        if settle_seconds > 0:
            await asyncio.sleep(min(settle_seconds, float(observation_window_s)))

        end = time.time()
        start = end - observation_window_s
        results: list[ClaimResult] = []

        for claim in claims:
            result = await self._evaluate(claim, baseline, start, end)
            results.append(result)

        verdict = decide_verdict(results)
        notes = summarise(results)
        completed_at = self._clock.now()

        run = VerificationRun(
            id=new_id(VERIFICATION),
            incident_id=incident_id,
            action_id=action_id,
            kind=kind,
            verdict=verdict,
            results=results,
            baseline=baseline,
            started_at=started_at,
            completed_at=completed_at,
            notes=notes,
        )

        if record_evidence:
            await self._record_evidence(run)

        log.info(
            "verification complete",
            incident_id=incident_id,
            action_id=action_id,
            verdict=verdict.value,
            claims=len(results),
            regressions=len(run.regressions),
            unavailable=len(run.unavailable),
        )
        return run

    async def _evaluate(
        self,
        claim: VerificationClaim,
        baseline: Baseline,
        start: float,
        end: float,
    ) -> ClaimResult:
        """Measure one claim. Never raises; unreachable sources become UNAVAILABLE."""
        now = self._clock.now()
        before = baseline.value_for(claim.id)

        probes = (
            VerificationTestKind.HEALTH_PROBE,
            VerificationTestKind.INSTANCE_READY,
        )
        if claim.test.kind in probes:
            return await self._evaluate_health(claim, now)

        metric = claim.test.metric
        if metric is None:
            return ClaimResult(
                claim=claim,
                outcome=ClaimOutcome.INCONCLUSIVE,
                before_value=None,
                after_value=None,
                threshold=claim.test.threshold,
                evidence_ids=[],
                detail="claim has no metric and no probe; nothing to measure",
                observed_at=now,
            )

        try:
            after = await self._sample_mean(metric, claim.test.resource_id, start, end)
        except SourceUnavailable as exc:
            return ClaimResult(
                claim=claim,
                outcome=ClaimOutcome.UNAVAILABLE,
                before_value=before,
                after_value=None,
                threshold=claim.test.threshold,
                evidence_ids=[],
                detail=f"metric source unavailable: {exc}",
                observed_at=now,
            )

        if after is None:
            # Genuinely no samples. Distinct from an unreachable source, and
            # distinct from a pass: a service emitting nothing is not proven well.
            return ClaimResult(
                claim=claim,
                outcome=ClaimOutcome.INCONCLUSIVE,
                before_value=before,
                after_value=None,
                threshold=claim.test.threshold,
                evidence_ids=[],
                detail=(
                    f"no samples for {metric} in the observation window; "
                    "absence of data is not evidence of recovery"
                ),
                observed_at=now,
            )

        if claim.protected:
            outcome, detail = self._judge_protected(claim, before, after)
        else:
            outcome, detail = self._judge_goal(claim, before, after)

        return ClaimResult(
            claim=claim,
            outcome=outcome,
            before_value=before,
            after_value=after,
            threshold=claim.test.threshold,
            evidence_ids=[],
            detail=detail,
            observed_at=now,
        )

    @staticmethod
    def _judge_goal(
        claim: VerificationClaim, before: float | None, after: float
    ) -> tuple[ClaimOutcome, str]:
        """Did the target metric reach its threshold in the intended direction?

        The threshold is absolute, so a baseline is not required to decide it.
        The baseline only enriches the explanation.
        """
        threshold = claim.test.threshold
        direction = claim.test.direction
        if threshold is None or direction is None:
            return (
                ClaimOutcome.INCONCLUSIVE,
                "claim declares no threshold or direction",
            )

        moved = "" if before is None else f" (was {before:.6g})"
        if direction is MetricDirection.DECREASE:
            ok = after <= threshold
            return (
                ClaimOutcome.PASS if ok else ClaimOutcome.FAIL,
                f"{claim.test.metric} is {after:.6g}{moved}, "
                f"{'at or below' if ok else 'above'} the target of {threshold:.6g}",
            )
        if direction is MetricDirection.INCREASE:
            ok = after >= threshold
            return (
                ClaimOutcome.PASS if ok else ClaimOutcome.FAIL,
                f"{claim.test.metric} is {after:.6g}{moved}, "
                f"{'at or above' if ok else 'below'} the target of {threshold:.6g}",
            )
        # STABLE: within tolerance of the threshold value.
        tolerance = max(claim.test.tolerance, 0.0)
        ok = abs(after - threshold) <= abs(threshold) * tolerance
        return (
            ClaimOutcome.PASS if ok else ClaimOutcome.FAIL,
            f"{claim.test.metric} is {after:.6g}{moved}, "
            f"target {threshold:.6g} +/- {tolerance:.0%}",
        )

    @staticmethod
    def _judge_protected(
        claim: VerificationClaim, before: float | None, after: float
    ) -> tuple[ClaimOutcome, str]:
        """Did a metric we promised not to break, break?

        Without a baseline this cannot be decided at all. Returning PASS would
        assert something unmeasured, so it returns INCONCLUSIVE and the run
        degrades to PARTIALLY_VERIFIED.
        """
        if before is None:
            return (
                ClaimOutcome.INCONCLUSIVE,
                f"no baseline for {claim.test.metric}; regression cannot be assessed",
            )
        tolerance = max(claim.test.tolerance, 0.0)
        if before == 0:
            # A metric that was zero has no meaningful relative change. Any
            # non-trivial value is treated as a degradation to be looked at.
            degraded = after > 0
            return (
                ClaimOutcome.FAIL if degraded else ClaimOutcome.PASS,
                f"{claim.test.metric} moved from 0 to {after:.6g}",
            )
        delta = (after - before) / abs(before)
        degraded = delta > tolerance
        return (
            ClaimOutcome.FAIL if degraded else ClaimOutcome.PASS,
            f"{claim.test.metric} changed {delta:+.1%} "
            f"({before:.6g} -> {after:.6g}), tolerance {tolerance:.0%}",
        )

    async def _evaluate_health(
        self, claim: VerificationClaim, now: datetime
    ) -> ClaimResult:
        """Ask the runtime whether the service is actually healthy.

        No runtime adapter means the claim is UNAVAILABLE, not satisfied. A
        verification that cannot see the environment has not verified anything
        about it.
        """
        target = claim.test.resource_id
        if self._runtime is None or not self._runtime.available or target is None:
            return ClaimResult(
                claim=claim,
                outcome=ClaimOutcome.UNAVAILABLE,
                before_value=None,
                after_value=None,
                threshold=None,
                evidence_ids=[],
                detail="no runtime adapter is available to probe service health",
                observed_at=now,
            )
        try:
            health = await self._runtime.health(target)
        except Exception as exc:  # noqa: BLE001 - adapter errors are gaps, not crashes
            return ClaimResult(
                claim=claim,
                outcome=ClaimOutcome.UNAVAILABLE,
                before_value=None,
                after_value=None,
                threshold=None,
                evidence_ids=[],
                detail=f"runtime health probe failed: {type(exc).__name__}",
                observed_at=now,
            )

        if health is ServiceHealth.UNKNOWN:
            outcome = ClaimOutcome.INCONCLUSIVE
        elif health is ServiceHealth.HEALTHY:
            outcome = ClaimOutcome.PASS
        else:
            outcome = ClaimOutcome.FAIL

        return ClaimResult(
            claim=claim,
            outcome=outcome,
            before_value=None,
            after_value=None,
            threshold=None,
            evidence_ids=[],
            detail=f"runtime reports {target} as {health.value}",
            observed_at=now,
        )

    async def _record_evidence(self, run: VerificationRun) -> None:
        """Write each claim result as its own evidence item.

        One item per claim rather than one per run: a later diagnosis can then
        cite "latency did not regress" specifically, instead of citing a whole
        verification and leaving a reader to work out which part supported it.
        """
        for result in run.results:
            status_note = "verified" if result.passed else result.outcome.value.lower()
            try:
                await self._evidence.record(
                    incident_id=run.incident_id,
                    source="verification-engine",
                    source_type=SourceType.RUNTIME,
                    evidence_type=EvidenceType.TEST_RESULT,
                    summary=f"[{status_note}] {result.claim.statement} - {result.detail}",
                    structured_value=result.as_json(),
                    resource_id=result.claim.test.resource_id,
                    provenance_uri=(
                        f"verification://{run.id}/claim/{result.claim.id}"
                    ),
                    observed_at=result.observed_at,
                )
            except Exception as exc:  # noqa: BLE001 - never fail a verdict on a write
                log.error(
                    "verification evidence not recorded",
                    verification_id=run.id,
                    claim_id=result.claim.id,
                    error=str(exc),
                )


__all__ = [
    "Baseline",
    "VerificationEngine",
    "VerificationRun",
    "claims_from_plan",
]
